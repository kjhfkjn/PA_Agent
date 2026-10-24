"""Tests for cutoff-aware higher-timeframe feature construction."""
from __future__ import annotations

from datetime import datetime, timezone

from pa_agent.data.context_timeframes import MarketSession
from pa_agent.data.tradingview_features import build_tradingview_features

SESSION = MarketSession("America/Chicago", "17:00", "test")


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(
        datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def _rows(
    start_ms: int,
    count: int,
    *,
    minutes: int,
    base_price: float = 100.0,
) -> list[tuple[int, tuple[float, ...]]]:
    rows = []
    for index in range(count):
        price = base_price + index * 0.1
        rows.append(
            (
                start_ms + index * minutes * 60_000,
                (price, price + 0.5, price - 0.5, price + 0.2, 10.0),
            )
        )
    return rows


def test_each_configured_context_gets_metadata_and_current_primary_aggregation() -> None:
    start = _ms(2026, 10, 23, 22)
    rows = _rows(start, 100, minutes=5)
    cutoff = rows[-1][0]
    features = build_tradingview_features(
        rows,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=cutoff,
        context_timeframes=["15m", "1h"],
        session=SESSION,
    )

    assert [feature["timeframe"] for feature in features] == ["15m", "1h"]
    for feature in features:
        assert feature["available"] is True
        assert feature["source"] == "tradingview_extension"
        assert feature["method"]
        assert feature["bars_used"] >= 5
        assert feature["as_of_ms"] == cutoff
        assert feature["data"]["current_data_view"] == (
            "primary_timeframe_rolling_aggregation"
        )
        assert feature["data"]["latest_context_bar_open_ms"] <= cutoff
        assert feature["data"]["session_timezone"] == "America/Chicago"


def test_future_primary_rows_are_excluded_by_cutoff() -> None:
    start = _ms(2026, 10, 23, 22)
    rows = _rows(start, 100, minutes=5)
    cutoff = rows[49][0]
    features = build_tradingview_features(
        rows,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=cutoff,
        context_timeframes=["15m", "1h"],
        session=SESSION,
    )
    for feature in features:
        assert feature["as_of_ms"] == cutoff
        assert feature["data"]["latest_context_bar_open_ms"] <= cutoff


def test_latest_context_bar_marks_forming_vs_closed_by_session_boundary() -> None:
    start = _ms(2026, 10, 23, 22)
    rows = _rows(start, 20, minutes=5)
    feature = build_tradingview_features(
        rows,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=rows[1][0],
        context_timeframes=["15m"],
        session=SESSION,
    )[0]
    assert feature["data"]["latest_context_bar_complete"] is False

    feature = build_tradingview_features(
        rows,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=rows[2][0],
        context_timeframes=["15m"],
        session=SESSION,
    )[0]
    assert feature["data"]["latest_context_bar_complete"] is True


def test_native_history_fills_only_older_context_before_primary_coverage() -> None:
    primary_start = _ms(2026, 10, 23, 22)
    primary = _rows(primary_start, 24, minutes=5)
    cutoff = primary[-1][0]
    native_start = primary_start - 20 * 60 * 60_000
    native = _rows(native_start, 20, minutes=60, base_price=80.0)
    native.append(
        (
            cutoff + 60 * 60_000,
            (9999.0, 10_001.0, 9998.0, 10_000.0, 1.0),
        )
    )

    feature = build_tradingview_features(
        primary,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=cutoff,
        context_timeframes=["1h"],
        native_snapshots={"1h": native},
        session=SESSION,
    )[0]

    assert feature["available"] is True
    assert feature["data"]["native_history_bars"] > 0
    assert feature["data"]["latest_context_bar_open_ms"] <= cutoff
    assert feature["data"]["latest_context_bar_source_bars"] > 0
    assert not any(
        level["price"] > 9000
        for side in ("supports", "resistances")
        for level in feature["data"][side]
    )


def test_missing_context_history_is_explicitly_unavailable() -> None:
    start = _ms(2026, 10, 23, 22)
    primary = _rows(start, 100, minutes=5)
    cutoff = primary[-1][0]
    feature = build_tradingview_features(
        primary,
        symbol="MES!",
        source_timeframe="5m",
        cutoff_time_ms=cutoff,
        context_timeframes=["1d"],
        session=SESSION,
    )[0]

    assert feature["available"] is False
    assert feature["bars_used"] < 5
    assert "不会使用旧快照冒充当前数据" in feature["data"]["reason"]


def test_unsupported_context_timeframe_is_marked_unavailable() -> None:
    start = _ms(2026, 10, 23, 22)
    primary = _rows(start, 20, minutes=5)
    feature = build_tradingview_features(
        primary,
        symbol="MES!",
        source_timeframe="5m",
        context_timeframes=["1w"],
        session=SESSION,
    )[0]

    assert feature["available"] is False
    assert feature["bars_used"] == 0
    assert "固定时长" in feature["data"]["reason"]
