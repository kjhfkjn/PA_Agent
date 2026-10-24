"""Loopback HTTP bridge for the PA Agent TradingView Edge extension."""
from __future__ import annotations

import hmac
import json
import logging
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 18765
EXTENSION_ID = "halamgjeepconahbedlpgiaemiciddbf"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_BARS = 10_000


def _normalize_symbol(symbol: str) -> str:
    value = str(symbol or "").strip().upper().split(":")[-1]
    # TradingView continuous futures are displayed as MES1! while the app
    # commonly uses MES! as its instrument name.
    value = value.replace("1!", "!")
    return value


def _normalize_timeframe(value: str) -> str:
    raw = str(value or "").strip()
    upper = raw.upper()
    # TradingView resolutions use bare minute counts (5, 60) and D/W/M.
    # The app uses lowercase m/h/d/w and uppercase M for calendar months.
    if raw.isdigit():
        minutes = int(raw)
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    aliases = {"D": "1d", "1D": "1d", "W": "1w", "1W": "1w", "M": "1M", "1MTH": "1M"}
    if upper in aliases:
        return aliases[upper]
    if upper.endswith("H") and upper[:-1].isdigit():
        return upper[:-1] + "h"
    if upper.endswith("D") and upper[:-1].isdigit():
        return upper[:-1] + "d"
    if upper.endswith("W") and upper[:-1].isdigit():
        return upper[:-1] + "w"
    if raw.endswith("m") and raw[:-1].isdigit():
        return raw
    if upper.endswith("M") and upper[:-1].isdigit():
        return upper
    return raw


class TradingViewExtensionBridge:
    """Own the local receiver and the most recent chart snapshots."""

    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT) -> None:
        self.host = host
        self.port = port
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_seen: float | None = None
        self._page_url = ""
        self._browser_symbol = ""
        self._browser_resolution = ""
        self._desired_symbol = ""
        self._desired_timeframe = ""

    def start(self) -> None:
        if self._server is not None:
            return
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PAAgentBridge/1"

            def _headers(self, status: int = 200) -> None:
                origin = self.headers.get("Origin", "")
                self.send_response(status)
                if origin == EXTENSION_ORIGIN:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
                    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                    self.send_header(
                        "Access-Control-Allow-Headers", "Content-Type, X-PA-Token"
                    )
                    if self.headers.get("Access-Control-Request-Private-Network") == "true":
                        self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                self._headers(status)
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                if self.headers.get("Origin") != EXTENSION_ORIGIN:
                    self._json(403, {"ok": False, "error": "extension origin denied"})
                    return
                self._headers(204)

            def do_GET(self) -> None:  # noqa: N802
                # MV3 service-worker fetches with extension host permission may
                # omit Origin entirely. A web page still cannot read this
                # response because we do not grant it CORS access.
                origin = self.headers.get("Origin", "")
                if origin not in ("", EXTENSION_ORIGIN):
                    logger.warning(
                        "Rejected TradingView extension bridge GET from origin %r",
                        origin,
                    )
                    self._json(403, {"ok": False, "error": "extension origin denied"})
                    return
                if urlparse(self.path).path != "/api/v1/status":
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                self._json(200, bridge.status())

            def do_POST(self) -> None:  # noqa: N802
                # Accept an absent Origin only when the random per-process
                # bridge token below is valid. Browser pages cannot read the
                # bootstrap token without a matching CORS origin.
                origin = self.headers.get("Origin", "")
                if origin not in ("", EXTENSION_ORIGIN):
                    logger.warning(
                        "Rejected TradingView extension bridge POST from origin %r",
                        origin,
                    )
                    self._json(403, {"ok": False, "error": "extension origin denied"})
                    return
                if not hmac.compare_digest(
                    self.headers.get("X-PA-Token", ""), bridge._token
                ):
                    self._json(401, {"ok": False, "error": "invalid bridge token"})
                    return
                route = urlparse(self.path).path
                if route not in ("/api/v1/heartbeat", "/api/v1/klines"):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 2 or length > MAX_PAYLOAD_BYTES:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("invalid request body")
                    if route.endswith("heartbeat"):
                        bridge.accept_heartbeat(payload)
                    else:
                        bridge.accept_snapshot(payload)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)[:160]})
                    return
                self._json(200, {"ok": True})

            def log_message(self, fmt: str, *args: object) -> None:
                # Never log request bodies, headers, or bridge credentials.
                logger.debug("TV extension bridge: %s", fmt % args)

        try:
            server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            raise RuntimeError(
                f"PA Agent 本机桥接端口 {self.port} 无法打开：{exc}"
            ) from exc
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="tradingview-extension-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info("TradingView Edge extension bridge listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "token": self._token,
                "extensionConnected": self._last_seen is not None
                and time.monotonic() - self._last_seen < 15,
                "lastSeen": self._last_seen,
                "pageUrl": self._page_url,
                "symbol": self._browser_symbol,
                "resolution": self._browser_resolution,
                "desiredSymbol": self._desired_symbol,
                "desiredTimeframe": self._desired_timeframe,
                "barCount": sum(len(item["bars"]) for item in self._snapshots.values()),
            }

    def set_subscription(self, symbol: str, timeframe: str) -> None:
        with self._lock:
            self._desired_symbol = symbol
            self._desired_timeframe = timeframe

    def accept_heartbeat(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._last_seen = time.monotonic()
            self._page_url = str(payload.get("pageUrl", ""))[:500]

    def accept_snapshot(self, payload: dict[str, Any]) -> None:
        symbol = str(payload.get("symbol", "")).strip()
        resolution = str(payload.get("resolution", "")).strip()
        rows = payload.get("bars")
        if not symbol or not resolution or not isinstance(rows, list):
            raise ValueError("symbol, resolution, and bars are required")
        if len(rows) > MAX_BARS:
            rows = rows[-MAX_BARS:]
        bars: dict[int, tuple[float, float, float, float, float]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row["time"])
                if ts < 10_000_000_000:
                    ts *= 1000
                ohlcv = tuple(float(row[key]) for key in ("open", "high", "low", "close", "volume"))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            o, high, low, close, volume = ohlcv
            if (
                not all(math.isfinite(value) for value in ohlcv)
                or ts <= 0
                or low <= 0
                or high < low
                or not low <= o <= high
                or not low <= close <= high
                or volume < 0
            ):
                continue
            bars[ts] = ohlcv  # type: ignore[assignment]
        if not bars:
            return
        key = (_normalize_symbol(symbol), _normalize_timeframe(resolution))
        with self._lock:
            self._last_seen = time.monotonic()
            self._browser_symbol = symbol
            self._browser_resolution = resolution
            self._page_url = str(payload.get("pageUrl", self._page_url))[:500]
            self._snapshots[key] = {
                "symbol": symbol,
                "resolution": resolution,
                "bars": dict(sorted(bars.items())[-MAX_BARS:]),
                "receivedAt": self._last_seen,
            }
        logger.debug("Received %d TradingView bars for %s %s", len(bars), symbol, resolution)

    def get_snapshot(self, symbol: str, timeframe: str, count: int) -> list[tuple[int, tuple[float, ...]]]:
        key = (_normalize_symbol(symbol), _normalize_timeframe(timeframe))
        with self._lock:
            snapshot = self._snapshots.get(key)
            browser_symbol = self._browser_symbol
            browser_resolution = self._browser_resolution
        if snapshot is None:
            if browser_symbol:
                detail = f"Edge 正在捕获 {browser_symbol} {browser_resolution}"
            else:
                detail = "等待 Edge 扩展连接 TradingView 图表"
            raise RuntimeError(detail)
        rows = snapshot["bars"]
        return list(rows.items())[-max(1, int(count)):][::-1]
