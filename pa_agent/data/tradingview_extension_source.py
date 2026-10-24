"""TradingView data source backed by the Edge chart-reading extension."""
from __future__ import annotations

import logging
import math
from typing import Any

from pa_agent.data.base import DataSourceTransientError, KlineBar
from pa_agent.data.context_timeframes import (
    MarketSession,
    normalize_timeframe,
    resolve_context_timeframes,
    resolve_market_session,
    timeframe_to_minutes,
)
from pa_agent.data.tradingview import TradingViewSource
from pa_agent.data.tradingview_extension_bridge import TradingViewExtensionBridge

logger = logging.getLogger(__name__)


class TradingViewExtensionSource(TradingViewSource):
    """Read validated candles captured from the active TradingView chart tab.

    Subclassing TradingViewSource keeps existing GUI exchange controls and
    callbacks compatible while replacing its tvDatafeed transport entirely.
    """

    def __init__(self) -> None:
        super().__init__()
        self._bridge = TradingViewExtensionBridge()
        self._connected = False
        self._context_settings: Any = None

    def connect(self) -> None:
        try:
            self._bridge.start()
        except RuntimeError as exc:
            raise DataSourceTransientError(str(exc)) from exc
        self._connected = True
        logger.info("TradingView Edge extension source started")

    def disconnect(self) -> None:
        self._connected = False
        self._bridge.stop()

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in self.supported_timeframes():
            raise ValueError(f"Unsupported timeframe: {timeframe!r}")
        self._symbol = symbol.strip()
        self._timeframe = normalize_timeframe(timeframe)
        self._bridge.set_subscription(self._symbol, self._timeframe)

    def configure_context(self, settings: Any) -> None:
        """Attach the persisted multi-timeframe context settings."""
        self._context_settings = settings

    def unsubscribe(self) -> None:
        self._symbol = ""
        self._timeframe = ""

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("PA Agent 的 TradingView 扩展桥接未启动")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("TradingView 扩展未订阅品种/周期")
        try:
            rows = self._bridge.get_snapshot(self._symbol, self._timeframe, n)
        except RuntimeError as exc:
            raise DataSourceTransientError(str(exc)) from exc
        if not rows:
            raise DataSourceTransientError("Edge 扩展尚未捕获有效 K 线")

        bars: list[KlineBar] = []
        for index, (ts_ms, values) in enumerate(rows):
            o, high, low, close, volume = values
            if (
                not all(math.isfinite(v) for v in values)
                or high < low
                or not low <= o <= high
                or not low <= close <= high
                or volume < 0
            ):
                continue
            bars.append(KlineBar(
                seq=index + 1,
                ts_open=ts_ms,
                open=o,
                high=high,
                low=low,
                close=close,
                volume=volume,
                closed=index > 0,
            ))
        if not bars:
            raise DataSourceTransientError("扩展捕获的数据未通过 PA Agent 校验")
        return bars

    def analysis_features(self) -> tuple[dict, ...]:
        """Get cutoff-aware higher-timeframe features for the execution chart."""
        if not self._connected or not self._symbol or not self._timeframe:
            return ()
        from pa_agent.data.tradingview_features import (
            build_tradingview_features,
            build_unavailable_context_features,
        )

        try:
            rows = self._bridge.get_snapshot(self._symbol, self._timeframe, 10_000)
        except RuntimeError:
            return ()
        if not rows:
            return ()
        enabled = getattr(self._context_settings, "enabled", True)
        if not enabled:
            return ()
        configured = getattr(self._context_settings, "timeframes", [])
        by_primary = getattr(self._context_settings, "timeframes_by_primary", {})
        context_timeframes = resolve_context_timeframes(
            self._timeframe,
            configured,
            by_primary,
        )
        if not context_timeframes:
            return ()
        # Use the execution chart's replay cutoff for every timeframe. Native
        # context snapshots captured later must never leak future candles into
        # a historical analysis.
        cutoff = self._bridge.snapshot_cutoff(self._symbol, self._timeframe)
        if cutoff is None:
            cutoff = max(int(timestamp) for timestamp, _ in rows)
        try:
            session: MarketSession = resolve_market_session(
                self._symbol,
                self._context_settings,
            )
        except ValueError as exc:
            logger.warning("TradingView context session invalid: %s", exc)
            return build_unavailable_context_features(
                symbol=self._symbol,
                source_timeframe=self._timeframe,
                context_timeframes=context_timeframes,
                cutoff_time_ms=cutoff,
                reason=str(exc),
            )
        native: dict[str, list[tuple[int, tuple[float, ...]]]] = {}
        for target_timeframe in context_timeframes:
            try:
                native[target_timeframe] = self._bridge.get_snapshot(
                    self._symbol,
                    target_timeframe,
                    10_000,
                    cutoff_time_ms=cutoff,
                )
            except RuntimeError:
                pass
        return build_tradingview_features(
            rows, symbol=self._symbol, source_timeframe=self._timeframe,
            cutoff_time_ms=cutoff,
            context_timeframes=context_timeframes,
            native_snapshots=native,
            session=session,
        )

    @staticmethod
    def _timeframe_minutes(timeframe: str) -> int | None:
        return timeframe_to_minutes(timeframe)

    @property
    def bridge_status(self) -> dict[str, object]:
        return self._bridge.status()
