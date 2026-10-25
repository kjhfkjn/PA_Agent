"""Tests for replay order fills, position accounting, and chart-marker events."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pa_agent.records.backtest_trade_tracker import BacktestTradeTracker

_BASE_MS = 1_700_000_000_000
_BAR_MS = 5 * 60 * 1000


def _bar(
    index: int,
    *,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
) -> dict:
    return {
        "ts_open": _BASE_MS + index * _BAR_MS,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10,
    }


def _decision(
    *,
    order_type: str = "限价单",
    direction: str = "做多",
    entry: float = 100.0,
    stop: float = 95.0,
    target1: float = 105.0,
    target2: float | None = 110.0,
) -> dict:
    return {
        "order_type": order_type,
        "order_direction": direction,
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target1,
        "take_profit_price_2": target2,
    }


def _record(
    analysis_id: int,
    bars: list[dict],
    decision: dict | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        meta=SimpleNamespace(
            symbol="MES!",
            timeframe="5m",
            timestamp_local_ms=analysis_id,
        ),
        kline_data=bars,
        stage2_decision={"decision": decision} if decision is not None else None,
    )


def test_limit_fill_defers_entry_bar_target_then_accounts_tp1_and_tp2(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    state = tracker.process_analysis(
        _record(1, [_bar(0)], _decision()),
        session_id="replay-1",
    )
    assert state["active_trade"]["status"] == "entry_pending"

    # The entry candle trades through both entry and TP1. Since OHLC cannot
    # prove TP1 happened after the entry, it must not book that target.
    state = tracker.process_analysis(
        _record(
            2,
            [_bar(0), _bar(1, high=106.0, low=99.0, close=103.0)],
            _decision(),
        ),
        session_id="replay-1",
    )
    trade = state["active_trade"]
    assert trade["status"] == "open"
    assert trade["fill_price"] == 100.0
    assert trade["take_profit_1_hit"] is False
    assert trade["unrealized_points"] == pytest.approx(3.0)
    assert trade["unrealized_pnl_points"] == pytest.approx(3.0)
    assert [item["marker_kind"] for item in state["chart_markers"]] == ["entry"]

    state = tracker.process_analysis(
        _record(
            3,
            [
                _bar(0),
                _bar(1, high=106.0, low=99.0, close=103.0),
                _bar(2, open_=102.0, high=106.0, low=101.0, close=104.0),
            ],
            {"order_type": "不下单"},
        ),
        session_id="replay-1",
    )
    trade = state["active_trade"]
    assert trade["status"] == "open"
    assert trade["take_profit_1_hit"] is True
    assert trade["remaining_size_fraction"] == pytest.approx(0.5)
    assert trade["realized_pnl_points"] == pytest.approx(2.5)
    assert trade["unrealized_pnl_points"] == pytest.approx(2.0)

    state = tracker.process_analysis(
        _record(
            4,
            [
                _bar(0),
                _bar(1, high=106.0, low=99.0, close=103.0),
                _bar(2, open_=102.0, high=106.0, low=101.0, close=104.0),
                _bar(3, open_=105.0, high=111.0, low=104.0, close=108.0),
            ],
            {"order_type": "不下单"},
        ),
        session_id="replay-1",
    )
    trade = state["active_trade"]
    assert trade["status"] == "closed_take_profit"
    assert trade["take_profit_2_hit"] is True
    assert trade["remaining_size_fraction"] == 0.0
    assert trade["realized_pnl_points"] == pytest.approx(7.5)
    assert trade["unrealized_pnl_points"] == 0.0
    assert [item["label"] for item in state["chart_markers"]] == [
        "入场",
        "TP1",
        "TP2",
    ]

    ledger_path = tmp_path / "MES!_5m.jsonl"
    event_types = [json.loads(line)["event_type"] for line in ledger_path.read_text().splitlines()]
    assert "entry_filled" in event_types
    assert "take_profit_1_hit" in event_types
    assert "exit_triggered" in event_types


def test_ambiguous_entry_bar_stops_first_without_crediting_target(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    tracker.process_analysis(
        _record(1, [_bar(0)], _decision()),
        session_id="replay-1",
    )

    state = tracker.process_analysis(
        _record(
            2,
            [_bar(0), _bar(1, high=106.0, low=94.0, close=101.0)],
            _decision(),
        ),
        session_id="replay-1",
    )
    trade = state["active_trade"]
    assert trade["status"] == "closed_stop_loss"
    assert trade["ambiguous_exit_bar"] is True
    assert trade["stop_loss_hit"] is True
    assert trade["take_profit_1_hit"] is False
    assert trade["realized_pnl_points"] == pytest.approx(-5.0)
    assert [item["marker_kind"] for item in state["chart_markers"]] == ["entry", "exit"]


def test_limit_entry_is_cancelled_when_open_gaps_beyond_its_stop(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    tracker.process_analysis(
        _record(1, [_bar(0)], _decision()),
        session_id="replay-1",
    )
    state = tracker.process_analysis(
        _record(
            2,
            [_bar(0), _bar(1, open_=94.0, high=101.0, low=93.0, close=96.0)],
            _decision(),
        ),
        session_id="replay-1",
    )
    assert state["active_trade"]["status"] == "cancelled"
    assert state["active_trade"]["fill_price"] is None
    assert state["chart_markers"] == []


def test_stop_entry_is_cancelled_when_open_gaps_past_tp1(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    tracker.process_analysis(
        _record(
            1,
            [_bar(0)],
            _decision(order_type="突破单", entry=101.0),
        ),
        session_id="replay-1",
    )
    state = tracker.process_analysis(
        _record(
            2,
            [_bar(0), _bar(1, open_=106.0, high=108.0, low=105.0, close=107.0)],
            _decision(order_type="突破单", entry=101.0),
        ),
        session_id="replay-1",
    )
    assert state["active_trade"]["status"] == "cancelled"
    assert state["active_trade"]["fill_price"] is None
    assert state["chart_markers"] == []


@pytest.mark.parametrize(
    ("order_type", "direction", "bar", "expected"),
    [
        ("限价单", "做多", {"open": 98.0, "high": 101.0, "low": 97.0}, 98.0),
        ("限价单", "做空", {"open": 102.0, "high": 103.0, "low": 99.0}, 102.0),
        ("突破单", "做多", {"open": 103.0, "high": 104.0, "low": 99.0}, 103.0),
        ("突破单", "做空", {"open": 97.0, "high": 101.0, "low": 96.0}, 97.0),
    ],
)
def test_order_type_fill_prices(order_type, direction, bar, expected) -> None:
    trade = {
        "order_type": order_type,
        "direction": direction,
        "entry_price": 100.0,
    }
    assert BacktestTradeTracker._entry_fill(trade, bar) == expected


def test_stop_order_waits_until_entry_price_is_crossed() -> None:
    trade = {
        "order_type": "突破单",
        "direction": "做多",
        "entry_price": 101.0,
    }
    bar = {"open": 100.0, "high": 100.5, "low": 99.0}
    assert BacktestTradeTracker._entry_fill(trade, bar) is None


def test_unactionable_decision_keeps_pending_order_without_crashing(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    tracker.process_analysis(
        _record(1, [_bar(0)], _decision()),
        session_id="replay-1",
    )
    state = tracker.process_analysis(
        _record(
            2,
            [_bar(0), _bar(1, open_=100.5, high=101.0, low=100.5, close=100.8)],
            {"order_type": "观望"},
        ),
        session_id="replay-1",
    )
    assert state["active_trade"]["status"] == "entry_pending"


def test_new_session_and_replay_rewind_clear_old_position(tmp_path) -> None:
    tracker = BacktestTradeTracker(tmp_path)
    state = tracker.process_analysis(
        _record(1, [_bar(5)], _decision()),
        session_id="replay-1",
    )
    assert state["active_trade"]["status"] == "entry_pending"

    state = tracker.process_analysis(
        _record(2, [_bar(5)], None),
        session_id="replay-2",
    )
    assert state["session_id"] == "replay-2"
    assert state["active_trade"] is None
    assert state["chart_markers"] == []

    state = tracker.process_analysis(
        _record(3, [_bar(0)], None),
        session_id="replay-2",
    )
    assert state["timeline_epoch"] == 1
    assert state["active_trade"] is None
    events = [
        json.loads(line)["event_type"]
        for line in (tmp_path / "MES!_5m.jsonl").read_text().splitlines()
    ]
    assert "session_reset" in events
    assert "replay_rewound" in events


def test_invalid_order_geometry_is_not_added_to_backtest(tmp_path) -> None:
    state = BacktestTradeTracker(tmp_path).process_analysis(
        _record(1, [_bar(0)], _decision(stop=105.0)),
        session_id="replay-1",
    )
    assert state["active_trade"] is None
