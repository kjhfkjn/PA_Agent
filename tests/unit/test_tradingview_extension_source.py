"""Tests for the extension data source's primary/context data split."""
from __future__ import annotations

from datetime import datetime, timezone

from pa_agent.config.settings import ContextSettings
from pa_agent.data.tradingview_extension_bridge import TradingViewExtensionBridge
from pa_agent.data.tradingview_extension_source import TradingViewExtensionSource


def _ms(hour: int, minute: int) -> int:
    return int(
        datetime(2026, 10, 23, hour, minute, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def _rows(
    start_ms: int,
    count: int,
    *,
    minutes: int,
) -> list[dict[str, float | int]]:
    rows = []
    for index in range(count):
        price = 100.0 + index * 0.1
        rows.append(
            {
                "time": start_ms + index * minutes * 60_000,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.2,
                "volume": 10.0,
            }
        )
    return rows


def _source_with_bridge() -> TradingViewExtensionSource:
    source = TradingViewExtensionSource()
    source._bridge = TradingViewExtensionBridge()
    source._connected = True
    source.subscribe("MES!", "5m")
    return source


def test_analysis_features_use_configured_context_not_hardcoded_timeframes() -> None:
    source = _source_with_bridge()
    source.configure_context(ContextSettings(timeframes=["30m"]))
    start = _ms(17, 0)
    rows = _rows(start, 100, minutes=5)
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": rows,
            "cutoffTime": rows[-1]["time"],
        }
    )

    features = source.analysis_features()
    assert [feature["timeframe"] for feature in features] == ["30m"]
    assert features[0]["as_of_ms"] == rows[-1]["time"]
    assert features[0]["primary_timeframe"] == "5m"


def test_native_context_future_bars_are_capped_by_execution_cutoff() -> None:
    source = _source_with_bridge()
    source.configure_context(ContextSettings(timeframes=["1h"]))
    start = _ms(17, 0)
    primary = _rows(start, 100, minutes=5)
    cutoff = primary[-1]["time"]
    native = _rows(start - 10 * 60 * 60_000, 20, minutes=60)
    native.append(
        {
            "time": cutoff + 60 * 60_000,
            "open": 9999.0,
            "high": 10_001.0,
            "low": 9998.0,
            "close": 10_000.0,
            "volume": 1.0,
        }
    )
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": primary,
            "cutoffTime": cutoff,
        }
    )
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "60",
            "bars": native,
            "cutoffTime": cutoff + 10 * 60 * 60_000,
        }
    )

    feature = source.analysis_features()[0]
    assert feature["as_of_ms"] == cutoff
    assert feature["data"]["latest_context_bar_open_ms"] <= cutoff
    assert not any(
        level["price"] > 9000
        for side in ("supports", "resistances")
        for level in feature["data"][side]
    )


def test_chart_snapshot_remains_execution_timeframe_only() -> None:
    source = _source_with_bridge()
    start = _ms(17, 0)
    rows = _rows(start, 20, minutes=5)
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": rows,
            "cutoffTime": rows[-1]["time"],
        }
    )

    bars = source.latest_snapshot(10)
    assert len(bars) == 10
    assert bars[0].ts_open == rows[-1]["time"]
    assert all(bar.ts_open <= rows[-1]["time"] for bar in bars)
    assert source._timeframe == "5m"


def test_replay_forward_and_backward_move_every_context_to_same_cutoff() -> None:
    source = _source_with_bridge()
    source.configure_context(ContextSettings(timeframes=["15m", "1h"]))
    start = _ms(17, 0)
    rows = _rows(start, 100, minutes=5)
    native = _rows(start - 20 * 60 * 60_000, 30, minutes=60)
    cutoff_index = 60

    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "60",
            "bars": native,
            "cutoffTime": rows[-1]["time"],
        }
    )
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": rows,
            "cutoffTime": rows[cutoff_index]["time"],
        }
    )
    backward = source.analysis_features()
    assert {feature["as_of_ms"] for feature in backward} == {
        rows[cutoff_index]["time"]
    }
    assert all(
        feature["data"]["latest_context_bar_open_ms"] <= rows[cutoff_index]["time"]
        for feature in backward
    )

    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": rows,
            "cutoffTime": rows[80]["time"],
        }
    )
    forward = source.analysis_features()
    assert {feature["as_of_ms"] for feature in forward} == {rows[80]["time"]}
    assert all(
        feature["data"]["latest_context_bar_open_ms"] <= rows[80]["time"]
        for feature in forward
    )


def test_invalid_session_configuration_returns_explicit_unavailable_features() -> None:
    source = _source_with_bridge()
    source.configure_context(
        ContextSettings(
            timeframes=["30m"],
            session_timezone="Not/A_Timezone",
        )
    )
    start = _ms(17, 0)
    rows = _rows(start, 20, minutes=5)
    source._bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": rows,
            "cutoffTime": rows[-1]["time"],
        }
    )

    feature = source.analysis_features()[0]
    assert feature["available"] is False
    assert feature["as_of_ms"] == rows[-1]["time"]
    assert "时区" in feature["data"]["reason"]
