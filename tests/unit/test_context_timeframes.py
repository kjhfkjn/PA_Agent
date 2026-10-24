"""Tests for configurable multi-timeframe context and session alignment."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pa_agent.config.settings import ContextSettings, MarketSessionSettings
from pa_agent.data.context_timeframes import (
    MarketSession,
    align_bucket_start,
    infer_market_session,
    resolve_context_timeframes,
    resolve_market_session,
    timeframe_to_minutes,
)


def _ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(
        datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def test_auto_context_periods_scale_with_execution_timeframe() -> None:
    assert resolve_context_timeframes("1m") == ["5m", "15m", "1h"]
    assert resolve_context_timeframes("5m") == ["15m", "1h"]
    assert resolve_context_timeframes("15m") == ["1h", "4h"]
    assert resolve_context_timeframes("1h") == ["4h", "1d"]


def test_explicit_context_periods_override_auto_policy() -> None:
    assert resolve_context_timeframes("5m", ["30m", "2h"]) == ["30m", "2h"]
    assert resolve_context_timeframes(
        "15m",
        ["30m"],
        {"15m": ["1h", "4h"]},
    ) == ["1h", "4h"]
    assert resolve_context_timeframes("1h", ["15m", "90m", "4h"]) == ["4h"]


def test_timeframe_parsing_supports_tradingview_minutes() -> None:
    assert timeframe_to_minutes("60") == 60
    assert timeframe_to_minutes("240") == 240
    assert timeframe_to_minutes("D") == 1_440


def test_cme_session_uses_chicago_1700_anchor() -> None:
    session = infer_market_session("CME_MINI:MES1!")
    assert session.timezone == "America/Chicago"
    assert session.anchor == "17:00"


def test_chinese_futures_are_not_misclassified_as_cme() -> None:
    session = infer_market_session("RB0")
    assert session.timezone == "Asia/Shanghai"
    assert session.anchor == "21:00"
    assert session.source == "inferred_cn_futures"


def test_bucket_alignment_uses_session_anchor_not_utc_midnight() -> None:
    session = MarketSession("America/Chicago", "17:00", "test")
    # 2026-10-23 22:05 UTC is 17:05 in Chicago (CDT).
    assert align_bucket_start(_ms(2026, 10, 23, 22, 5), "4h", session) == _ms(
        2026, 10, 23, 22
    )
    # The next local trading day starts at 17:00 CDT on 2026-10-24.
    assert align_bucket_start(_ms(2026, 10, 24, 15, 30), "1d", session) == _ms(
        2026, 10, 23, 22
    )


def test_symbol_session_override_wins_over_inference() -> None:
    settings = ContextSettings(
        session_by_symbol={
            "MES!": MarketSessionSettings(
                timezone="America/New_York",
                anchor="18:00",
            )
        }
    )
    session = resolve_market_session("MES!", settings)
    assert session.timezone == "America/New_York"
    assert session.anchor == "18:00"
    assert session.source == "configured"


def test_invalid_session_anchor_is_rejected() -> None:
    settings = ContextSettings(session_anchor="25:00")
    with pytest.raises(ValueError, match="锚点"):
        resolve_market_session("MES!", settings)
