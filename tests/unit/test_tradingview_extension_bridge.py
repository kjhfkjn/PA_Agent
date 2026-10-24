"""Tests for per-symbol/per-timeframe TradingView extension snapshots."""
from __future__ import annotations

from pa_agent.data.tradingview_extension_bridge import TradingViewExtensionBridge

_BASE_MS = 1_700_000_000_000


def _bar(timestamp: int) -> dict[str, float | int]:
    return {
        "time": timestamp,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
    }


def test_cutoff_is_isolated_per_symbol_and_timeframe() -> None:
    bridge = TradingViewExtensionBridge()
    bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": [
                _bar(_BASE_MS),
                _bar(_BASE_MS + 1_000),
                _bar(_BASE_MS + 2_000),
            ],
            "cutoffTime": _BASE_MS + 1_000,
            "source": "test",
        }
    )
    bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "60",
            "bars": [
                _bar(_BASE_MS),
                _bar(_BASE_MS + 60_000),
                _bar(_BASE_MS + 120_000),
            ],
            "cutoffTime": _BASE_MS,
            "cutoffSource": "legend",
            "source": "test",
        }
    )

    assert [row[0] for row in bridge.get_snapshot("MES!", "5m", 10)] == [
        _BASE_MS + 1_000,
        _BASE_MS,
    ]
    assert [row[0] for row in bridge.get_snapshot("MES!", "1h", 10)] == [_BASE_MS]
    assert bridge.snapshot_cutoff("MES!", "5m") == _BASE_MS + 1_000
    assert bridge.snapshot_cutoff("MES!", "1h") == _BASE_MS
    snapshots = {item["timeframe"]: item for item in bridge.status()["snapshots"]}
    assert snapshots["1h"]["cutoffSource"] == "legend"


def test_explicit_execution_cutoff_filters_native_context_snapshot() -> None:
    bridge = TradingViewExtensionBridge()
    bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "60",
            "bars": [
                _bar(_BASE_MS),
                _bar(_BASE_MS + 60_000),
                _bar(_BASE_MS + 120_000),
            ],
            "cutoffTime": _BASE_MS + 120_000,
            "source": "test",
        }
    )

    rows = bridge.get_snapshot(
        "MES!", "1h", 10, cutoff_time_ms=_BASE_MS + 60_000
    )
    assert [row[0] for row in rows] == [_BASE_MS + 60_000, _BASE_MS]


def test_status_reports_each_context_snapshot() -> None:
    bridge = TradingViewExtensionBridge()
    bridge.set_subscription("MES!", "5m")
    bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "5",
            "bars": [_bar(_BASE_MS)],
            "cutoffTime": _BASE_MS,
        }
    )
    bridge.accept_snapshot(
        {
            "symbol": "MES1!",
            "resolution": "60",
            "bars": [_bar(_BASE_MS)],
            "cutoffTime": _BASE_MS,
        }
    )

    status = bridge.status()
    assert status["historyCutoff"] == _BASE_MS
    assert status["historyCutoffSource"] == "unknown"
    assert status["recentBars"][-1]["time"] == _BASE_MS
    assert status["recentBars"][-1]["close"] == 100.5
    assert {(item["timeframe"], item["cutoffTime"]) for item in status["snapshots"]} == {
        ("5m", _BASE_MS),
        ("1h", _BASE_MS),
    }
