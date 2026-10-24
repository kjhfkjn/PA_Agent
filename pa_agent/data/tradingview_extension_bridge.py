"""Loopback HTTP bridge for the PA Agent TradingView Edge extension."""
from __future__ import annotations

import hmac
import base64
import hashlib
import json
import logging
import math
import secrets
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from pa_agent.data.context_timeframes import normalize_timeframe

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
    return normalize_timeframe(value)


class TradingViewExtensionBridge:
    """Own the local receiver and the most recent chart snapshots."""

    def __init__(self, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT) -> None:
        self.host = host
        self.port = port
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._event_condition = threading.Condition(self._lock)
        self._events: list[dict[str, Any]] = []
        self._next_event_id = 1
        self._event_session_id = secrets.token_urlsafe(12)
        self._live_analysis: dict[str, Any] = {
            "analysisId": "",
            "symbol": "",
            "timeframe": "",
            "stage1Thinking": "",
            "stage1Answer": "",
            "stage2Thinking": "",
            "stage2Answer": "",
            "decision": None,
            "error": "",
            "status": "idle",
        }
        self._stopping = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        # Replay cutoffs belong to a chart snapshot. A global cutoff lets a
        # later update from another timeframe silently truncate this chart.
        self._last_seen: float | None = None
        self._page_url = ""
        self._browser_symbol = ""
        self._browser_resolution = ""
        self._desired_symbol = ""
        self._desired_timeframe = ""
        self._backtest_enabled = False
        self._backtest_updated_at: float | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        with self._event_condition:
            self._stopping = False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PAAgentBridge/1"
            protocol_version = "HTTP/1.1"

            def _headers(self, status: int = 200, content_length: int | None = None) -> None:
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
                if content_length is not None:
                    self.send_header("Content-Length", str(content_length))
                self.end_headers()

            def _json(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                self._headers(status, len(body))
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                if self.headers.get("Origin") != EXTENSION_ORIGIN:
                    self._json(403, {"ok": False, "error": "extension origin denied"})
                    return
                self._headers(204, 0)

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
                route = urlparse(self.path).path
                if route == "/api/v1/events":
                    self._websocket_events()
                    return
                if route != "/api/v1/status":
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                self._json(200, bridge.status())

            def _websocket_events(self) -> None:
                if self.headers.get("Upgrade", "").lower() != "websocket":
                    self._json(400, {"ok": False, "error": "websocket upgrade required"})
                    return
                client_key = self.headers.get("Sec-WebSocket-Key", "")
                if not client_key:
                    self._json(400, {"ok": False, "error": "missing websocket key"})
                    return
                accept = base64.b64encode(
                    hashlib.sha1(
                        (client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()

                def read_frame() -> tuple[int, bytes] | None:
                    header = self.rfile.read(2)
                    if len(header) != 2:
                        return None
                    first, second = header
                    opcode = first & 0x0F
                    length = second & 0x7F
                    if length == 126:
                        extended = self.rfile.read(2)
                        if len(extended) != 2:
                            return None
                        length = struct.unpack("!H", extended)[0]
                    elif length == 127:
                        extended = self.rfile.read(8)
                        if len(extended) != 8:
                            return None
                        length = struct.unpack("!Q", extended)[0]
                    if length > 1_000_000:
                        return None
                    mask = self.rfile.read(4) if second & 0x80 else b""
                    payload = self.rfile.read(length)
                    if len(payload) != length:
                        return None
                    if mask:
                        payload = bytes(
                            value ^ mask[index % 4]
                            for index, value in enumerate(payload)
                        )
                    return opcode, payload

                def send_frame(payload: bytes, opcode: int = 1) -> None:
                    length = len(payload)
                    if length < 126:
                        header = bytes((0x80 | opcode, length))
                    elif length <= 0xFFFF:
                        header = bytes((0x80 | opcode, 126)) + struct.pack("!H", length)
                    else:
                        header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", length)
                    self.connection.sendall(header + payload)

                try:
                    self.connection.settimeout(5)
                    first = read_frame()
                    if first is None or first[0] != 1:
                        return
                    try:
                        subscription = json.loads(first[1].decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        subscription = {}
                    if (
                        not isinstance(subscription, dict)
                        or subscription.get("type") != "subscribe"
                        or not hmac.compare_digest(
                            str(subscription.get("token", "")), bridge._token
                        )
                    ):
                        send_frame(
                            json.dumps({
                                "type": "error",
                                "error": "invalid subscription",
                            }).encode()
                        )
                        return
                    try:
                        last_id = max(0, int(subscription.get("afterEventId", 0)))
                    except (TypeError, ValueError, OverflowError):
                        last_id = 0
                    self.connection.settimeout(None)
                    send_frame(json.dumps({
                        "type": "ready",
                        "latestEventId": bridge.latest_event_id(),
                        "eventSessionId": bridge.event_session_id,
                        "liveState": bridge.live_state(),
                    }).encode())
                    while not bridge.stopping:
                        events = bridge.events_after(last_id, timeout=15)
                        if not events:
                            # Regular traffic keeps the MV3 service worker alive.
                            send_frame(json.dumps({"type": "keepalive"}).encode())
                            continue
                        for item in events:
                            send_frame(json.dumps(item, ensure_ascii=False).encode())
                            last_id = int(item["eventId"])
                    try:
                        send_frame(b"", opcode=8)
                    except OSError:
                        pass
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    try:
                        self.connection.settimeout(None)
                    except OSError:
                        pass

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
                if route not in (
                    "/api/v1/heartbeat",
                    "/api/v1/klines",
                    "/api/v1/backtest",
                ):
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
                    elif route.endswith("backtest"):
                        bridge.set_backtest_enabled(bool(payload.get("enabled")))
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
        with self._event_condition:
            self._stopping = True
            self._event_condition.notify_all()
        server.shutdown()
        server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            key = (
                _normalize_symbol(self._desired_symbol),
                _normalize_timeframe(self._desired_timeframe),
            )
            snapshot = self._snapshots.get(key)
            rows = snapshot["bars"] if snapshot else {}
            cutoff = snapshot.get("cutoffTime") if snapshot else None
            effective_rows = (
                {ts: value for ts, value in rows.items() if ts <= cutoff}
                if cutoff is not None
                else rows
            )
            recent_bars = [
                {
                    "time": timestamp,
                    "open": values[0],
                    "high": values[1],
                    "low": values[2],
                    "close": values[3],
                    "volume": values[4],
                }
                for timestamp, values in sorted(effective_rows.items())[-5:]
            ]
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
                "backtestEnabled": self._backtest_enabled,
                "backtestUpdatedAt": self._backtest_updated_at,
                "latestEventId": self._next_event_id - 1,
                "eventSessionId": self._event_session_id,
                "barCount": sum(len(item["bars"]) for item in self._snapshots.values()),
                "historyCutoff": cutoff,
                "historyCutoffSource": snapshot.get("cutoffSource", "unknown")
                if snapshot
                else "unknown",
                "displayBarCount": len(effective_rows),
                "displayLastBarTime": max(effective_rows) if effective_rows else None,
                "recentBars": recent_bars,
                "snapshots": [
                    {
                        "symbol": item["symbol"],
                        "timeframe": item["timeframe"],
                        "barCount": len(item["bars"]),
                        "cutoffTime": item.get("cutoffTime"),
                        "cutoffSource": item.get("cutoffSource", "unknown"),
                        "source": item.get("source", "tradingview_extension"),
                        "receivedAt": item.get("receivedAt"),
                    }
                    for item in self._snapshots.values()
                ],
            }

    def set_subscription(self, symbol: str, timeframe: str) -> None:
        with self._lock:
            self._desired_symbol = symbol
            self._desired_timeframe = timeframe

    def accept_heartbeat(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._last_seen = time.monotonic()
            self._page_url = str(payload.get("pageUrl", ""))[:500]

    def set_backtest_enabled(self, enabled: bool) -> None:
        """Expose the extension's replay mode to the GUI auto-analysis loop."""
        with self._lock:
            self._backtest_enabled = bool(enabled)
            self._backtest_updated_at = time.time()
            self._event_condition.notify_all()

    @property
    def stopping(self) -> bool:
        with self._lock:
            return self._stopping

    @property
    def backtest_enabled(self) -> bool:
        with self._lock:
            return self._backtest_enabled

    def latest_event_id(self) -> int:
        with self._lock:
            return self._next_event_id - 1

    @property
    def event_session_id(self) -> str:
        return self._event_session_id

    def events_after(self, event_id: int, *, timeout: float) -> list[dict[str, Any]]:
        with self._event_condition:
            events = [item for item in self._events if int(item["eventId"]) > event_id]
            if not events and not self._stopping:
                self._event_condition.wait(timeout=timeout)
                events = [item for item in self._events if int(item["eventId"]) > event_id]
            return events

    def live_state(self) -> dict[str, Any]:
        """Return the current in-memory analysis stream for connected extensions."""
        with self._lock:
            return dict(self._live_analysis)

    def publish_analysis_stream(
        self,
        *,
        event: str,
        symbol: str,
        timeframe: str,
        analysis_id: str,
        stage: str = "",
        channel: str = "",
        text: str = "",
        decision: dict[str, Any] | None = None,
        bar_time: int | None = None,
        error: str = "",
    ) -> None:
        """Publish live model output to the extension; data stays in process memory."""
        stage_key = "stage1" if stage == "stage1" else "stage2"
        with self._event_condition:
            if event == "started":
                self._live_analysis = {
                    "analysisId": str(analysis_id)[:100],
                    "symbol": _normalize_symbol(symbol),
                    "timeframe": _normalize_timeframe(timeframe),
                    "stage1Thinking": "",
                    "stage1Answer": "",
                    "stage2Thinking": "",
                    "stage2Answer": "",
                    "decision": None,
                    "error": "",
                    "status": "分析中",
                }
            elif str(self._live_analysis.get("analysisId", "")) != str(analysis_id):
                return
            elif event == "token" and channel in {"thinking", "answer"}:
                key = f"{stage_key}{'Thinking' if channel == 'thinking' else 'Answer'}"
                self._live_analysis[key] = (
                    str(self._live_analysis.get(key, "")) + str(text)
                )[-120_000:]
            elif event == "decision":
                self._live_analysis["decision"] = decision or {}
                self._live_analysis["status"] = "已完成"
            elif event == "completed":
                self._live_analysis["status"] = "已完成"
            elif event == "failed":
                self._live_analysis["status"] = "分析失败"
                self._live_analysis["error"] = str(error)[:1000]
            elif event == "cancelled":
                self._live_analysis["status"] = "已取消"
            item: dict[str, Any] = {
                "type": "llm_stream",
                "eventId": self._next_event_id,
                "event": event,
                "analysisId": str(analysis_id)[:100],
                "symbol": _normalize_symbol(symbol),
                "timeframe": _normalize_timeframe(timeframe),
                "createdAt": int(time.time() * 1000),
            }
            if event == "token":
                item.update(stage=stage_key, channel=channel, text=str(text)[:8000])
            elif event == "decision":
                item["decision"] = decision or {}
            if event in {"completed", "failed"} and bar_time is not None:
                item["barTime"] = int(bar_time)
            if event == "failed" and error:
                item["error"] = str(error)[:1000]
            self._next_event_id += 1
            self._events.append(item)
            self._events = self._events[-4096:]
            self._event_condition.notify_all()

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
                "timeframe": _normalize_timeframe(resolution),
                "resolution": resolution,
                "bars": dict(sorted(bars.items())[-MAX_BARS:]),
                "receivedAt": self._last_seen,
                "cutoffTime": None,
                "cutoffSource": "unknown",
                "source": str(payload.get("source", "tradingview_extension"))[:80],
            }
            self._snapshots[key]["cutoffSource"] = str(
                payload.get("cutoffSource", "unknown")
            )[:40]
            try:
                raw_cutoff = payload.get("cutoffTime")
                cutoff = int(raw_cutoff) if raw_cutoff is not None else None
                cutoff = cutoff if cutoff and cutoff > 0 else None
                self._snapshots[key]["cutoffTime"] = cutoff
            except (TypeError, ValueError, OverflowError):
                self._snapshots[key]["cutoffTime"] = None
        logger.debug("Received %d TradingView bars for %s %s", len(bars), symbol, resolution)

    def get_snapshot(
        self, symbol: str, timeframe: str, count: int, *, cutoff_time_ms: int | None = None,
        apply_capture_cutoff: bool = True,
    ) -> list[tuple[int, tuple[float, ...]]]:
        key = (_normalize_symbol(symbol), _normalize_timeframe(timeframe))
        with self._lock:
            snapshot = self._snapshots.get(key)
            browser_symbol = self._browser_symbol
            browser_resolution = self._browser_resolution
            capture_cutoff = snapshot.get("cutoffTime") if snapshot else None
        if snapshot is None:
            if browser_symbol:
                detail = f"Edge 正在捕获 {browser_symbol} {browser_resolution}"
            else:
                detail = "等待 Edge 扩展连接 TradingView 图表"
            raise RuntimeError(detail)
        rows = snapshot["bars"]
        effective_cutoff = cutoff_time_ms
        if effective_cutoff is None and apply_capture_cutoff:
            effective_cutoff = capture_cutoff
        if effective_cutoff is not None:
            rows = {ts: value for ts, value in rows.items() if ts <= effective_cutoff}
            if not rows:
                raise RuntimeError(
                    "所选历史截止时间之前没有已捕获的 K 线；"
                    "请先在 TradingView 加载该历史区间"
                )
        return list(rows.items())[-max(1, int(count)):][::-1]

    def snapshot_cutoff(self, symbol: str, timeframe: str) -> int | None:
        key = (_normalize_symbol(symbol), _normalize_timeframe(timeframe))
        with self._lock:
            snapshot = self._snapshots.get(key)
            return snapshot.get("cutoffTime") if snapshot else None
