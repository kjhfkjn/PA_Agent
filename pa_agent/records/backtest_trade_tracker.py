"""Paper-track entry plans against TradingView Bar Replay candles."""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Any

from pa_agent.config.paths import PROJECT_ROOT
from pa_agent.data.datetime_ts import ts_open_to_ms

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_ACTIONABLE_ORDER_TYPES = {"限价单", "突破单", "市价单"}
_TP1_SCALE_OUT_FRACTION = 0.5
_MAX_CHART_MARKERS = 500


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _decision_from_record(record: Any) -> dict[str, Any]:
    value = getattr(record, "stage2_decision", None)
    if not isinstance(value, dict):
        return {}
    decision = value.get("decision")
    return decision if isinstance(decision, dict) else {}


def _bars_from_record(record: Any) -> list[dict[str, Any]]:
    parsed: dict[int, dict[str, Any]] = {}
    for raw in getattr(record, "kline_data", None) or []:
        if not isinstance(raw, dict):
            continue
        try:
            ts = int(ts_open_to_ms(raw.get("ts_open")))
        except (TypeError, ValueError, OverflowError):
            continue
        values = {key: _number(raw.get(key)) for key in ("open", "high", "low", "close")}
        if ts <= 0 or any(value is None for value in values.values()):
            continue
        open_price = float(values["open"])
        high = float(values["high"])
        low = float(values["low"])
        close = float(values["close"])
        if low > high or low > min(open_price, close) or high < max(open_price, close):
            continue
        bar = {"ts_open": ts, **values}
        # Some providers include duplicate timestamps in a snapshot. Keep the
        # last valid value so one candle can never advance a position twice.
        parsed[ts] = bar  # type: ignore[assignment]
    return [parsed[ts] for ts in sorted(parsed)]


def _plan_from_decision(decision: dict[str, Any]) -> dict[str, Any] | None:
    order_type = str(decision.get("order_type") or "").strip()
    direction = str(decision.get("order_direction") or "").strip()
    if order_type not in _ACTIONABLE_ORDER_TYPES or direction not in {"做多", "做空"}:
        return None
    entry = _number(decision.get("entry_price"))
    stop = _number(decision.get("stop_loss_price"))
    target1 = _number(decision.get("take_profit_price"))
    target2 = _number(decision.get("take_profit_price_2"))
    if entry is None or stop is None or target1 is None:
        return None
    if direction == "做多":
        if not stop < entry < target1:
            return None
        if target2 is not None and target2 <= target1:
            return None
    else:
        if not target1 < entry < stop:
            return None
        if target2 is not None and target2 >= target1:
            return None
    return {
        "order_type": order_type,
        "direction": direction,
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target1,
        "take_profit_price_2": target2,
    }


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()


class BacktestTradeTracker:
    """Persist pending entries, fills, open positions, and TP/SL results.

    OHLC bars do not reveal whether the high or low happened first. If both the
    stop and a target are touched in one bar, the tracker records the ambiguity
    and assumes the stop was reached first.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or (PROJECT_ROOT / "records" / "backtest")

    def _paths(self, symbol: str, timeframe: str) -> tuple[Path, Path]:
        safe = re.sub(r"[^A-Za-z0-9_.!-]+", "_", f"{symbol}_{timeframe}")
        return (
            self.directory / f"{safe}.json",
            self.directory / f"{safe}.jsonl",
        )

    def process_analysis(
        self,
        record: Any,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Advance the active paper position using bars since the last run."""
        meta = getattr(record, "meta", None)
        symbol = str(getattr(meta, "symbol", "") or "").strip()
        timeframe = str(getattr(meta, "timeframe", "") or "").strip()
        bars = _bars_from_record(record)
        if not symbol or not timeframe or not bars:
            return None

        state_path, ledger_path = self._paths(symbol, timeframe)
        decision = _decision_from_record(record)
        current_plan = _plan_from_decision(decision)
        analysis_id = str(getattr(meta, "timestamp_local_ms", "") or "")
        has_decision = "order_type" in decision
        events: list[dict[str, Any]] = []

        with _LOCK:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            active = state.get("active_trade")
            latest_bar = bars[-1]
            latest_ts = int(latest_bar["ts_open"])
            if not isinstance(active, dict):
                active = None

            timeline_epoch = int(state.get("timeline_epoch", 0) or 0)
            existing_session_id = str(state.get("session_id") or "")
            effective_session_id = str(session_id or existing_session_id or "legacy")
            chart_markers = list(state.get("chart_markers") or [])
            session_mismatch = bool(state) and existing_session_id != effective_session_id
            if session_mismatch:
                if active and active.get("status") in {"entry_pending", "open"}:
                    active["status"] = "cancelled"
                    active["closed_reason"] = "回测会话已重置，旧挂单/仓位已清空"
                    events.append(self._event(
                        "trade_cancelled", symbol, timeframe, active, analysis_id,
                        latest_ts, reason=active["closed_reason"],
                    ))
                events.append(self._event(
                    "session_reset", symbol, timeframe, None, analysis_id,
                    latest_ts,
                    previous_session_id=existing_session_id or None,
                    session_id=effective_session_id,
                ))
                active = None
                chart_markers = []
                timeline_epoch = 0
                state = {}

            previous_latest_ts = int(state.get("last_bar_ts_open", 0) or 0)
            if previous_latest_ts > latest_ts:
                if active and active.get("status") in {"entry_pending", "open"}:
                    active["status"] = "cancelled"
                    active["closed_reason"] = "TradingView 回放时间倒退，旧挂单/仓位已清空"
                    events.append(self._event(
                        "trade_cancelled", symbol, timeframe, active, analysis_id,
                        latest_ts, reason=active["closed_reason"],
                    ))
                timeline_epoch += 1
                events.append(self._event(
                    "replay_rewound", symbol, timeframe, None, analysis_id,
                    latest_ts,
                    previous_bar_ts_open=previous_latest_ts,
                    timeline_epoch=timeline_epoch,
                    session_id=effective_session_id,
                ))
                active = None
                chart_markers = []
                state = {}

            active_was_in_flight = bool(
                active and active.get("status") in {"entry_pending", "open"}
            )
            if state.get("last_analysis_id") == analysis_id:
                return state

            if active and active.get("status") in {"entry_pending", "open"}:
                for bar in bars:
                    bar_ts = int(bar["ts_open"])
                    last_ts = int(active.get("last_processed_bar_ts", 0) or 0)
                    if bar_ts <= last_ts:
                        continue
                    bar_events = self._process_bar(active, bar, analysis_id)
                    active["last_processed_bar_ts"] = bar_ts
                    for event in bar_events:
                        event.update({
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "trade_id": active["trade_id"],
                            "analysis_id": analysis_id,
                            "session_id": effective_session_id,
                            "timeline_epoch": timeline_epoch,
                        })
                        events.append(event)
                        marker = self._marker_from_event(event, active)
                        if marker is not None:
                            chart_markers.append(marker)
                    if active.get("status") not in {"entry_pending", "open"}:
                        break

            if active and active.get("status") == "entry_pending":
                if has_decision and str(decision.get("order_type") or "") == "不下单":
                    active["status"] = "cancelled"
                    active["closed_reason"] = "最新分析撤销了未成交入场计划"
                    events.append(self._event(
                        "entry_cancelled", symbol, timeframe, active,
                        analysis_id, latest_ts,
                        reason=active["closed_reason"],
                    ))
                elif current_plan is not None and self._plan_changed(active, current_plan):
                    old_plan = self._plan_fields(active)
                    active.update(current_plan)
                    active["last_plan_analysis_id"] = analysis_id
                    events.append(self._event(
                        "pending_plan_revised", symbol, timeframe, active,
                        analysis_id, latest_ts,
                        previous_plan=old_plan,
                    ))
            elif active and active.get("status") == "open":
                # Preserve the actual fill and original order while recording
                # later AI suggestions for audit; they do not rewrite history.
                events.append(self._event(
                    "position_reviewed", symbol, timeframe, active, analysis_id,
                    latest_ts,
                    ai_order_type=decision.get("order_type"),
                    ai_direction=decision.get("order_direction"),
                    ai_entry_price=_number(decision.get("entry_price")),
                    ai_stop_loss_price=_number(decision.get("stop_loss_price")),
                    ai_take_profit_price=_number(decision.get("take_profit_price")),
                ))

            is_terminal = bool(
                active
                and active.get("status")
                in {"closed_take_profit", "closed_stop_loss", "cancelled"}
            )
            should_create_trade = active is None or (
                is_terminal
                and current_plan is not None
                and (
                    not active_was_in_flight
                    or self._plan_changed(active, current_plan)
                )
            )
            if should_create_trade and current_plan is not None:
                active = self._new_trade(
                    symbol, timeframe, current_plan, analysis_id,
                    latest_ts, session_id=effective_session_id,
                    timeline_epoch=timeline_epoch,
                )
                events.append(self._event(
                    "entry_plan_created", symbol, timeframe, active, analysis_id,
                    latest_ts,
                ))

            close = float(latest_bar["close"])
            if active:
                self._mark_to_market(active, close)

            state = {
                "version": 2,
                "symbol": symbol,
                "timeframe": timeframe,
                "session_id": effective_session_id,
                "timeline_epoch": timeline_epoch,
                "updated_at_ms": int(time.time() * 1000),
                "last_analysis_id": analysis_id,
                "last_bar_ts_open": latest_ts,
                "last_close": close,
                "active_trade": active,
                "chart_markers": chart_markers[-_MAX_CHART_MARKERS:],
            }
            for event in events:
                _append_event(ledger_path, event)
            # Keep an audit row for every analyzed replay bar, including bars
            # where a pending order was untouched or an open position remained.
            if not any(
                event.get("event_type") == "bar_observed"
                and event.get("bar_ts_open") == latest_ts
                for event in events
            ):
                _append_event(ledger_path, self._event(
                    "bar_observed", symbol, timeframe, active, analysis_id,
                    latest_ts,
                    close=close,
                    position_status=active.get("status") if active else "flat",
                    unrealized_points=active.get("unrealized_points") if active else None,
                    unrealized_pnl_points=active.get("unrealized_pnl_points") if active else None,
                ))
            self._write_state(state_path, state)
        return state

    @staticmethod
    def _new_trade(
        symbol: str,
        timeframe: str,
        plan: dict[str, Any],
        analysis_id: str,
        plan_bar_ts: int,
        *,
        session_id: str,
        timeline_epoch: int,
    ) -> dict[str, Any]:
        return {
            "trade_id": f"{symbol}|{timeframe}|{session_id}|{timeline_epoch}|{analysis_id}",
            "session_id": session_id,
            "timeline_epoch": timeline_epoch,
            **plan,
            "status": "entry_pending",
            "plan_analysis_id": analysis_id,
            "last_plan_analysis_id": analysis_id,
            "plan_bar_ts_open": plan_bar_ts,
            "last_processed_bar_ts": plan_bar_ts,
            "entry_fill_ts_open": None,
            "fill_price": None,
            "take_profit_1_hit": False,
            "take_profit_2_hit": False,
            "stop_loss_hit": False,
            "ambiguous_exit_bar": False,
            "initial_size_fraction": 1.0,
            "remaining_size_fraction": 1.0,
            "realized_pnl_points": 0.0,
            "unrealized_points": None,
            "unrealized_pnl_points": None,
        }

    @staticmethod
    def _plan_fields(trade: dict[str, Any]) -> dict[str, Any]:
        return {
            key: trade.get(key)
            for key in (
                "order_type", "direction", "entry_price", "stop_loss_price",
                "take_profit_price", "take_profit_price_2",
            )
        }

    @staticmethod
    def _plan_changed(trade: dict[str, Any], plan: dict[str, Any]) -> bool:
        return any(trade.get(key) != value for key, value in plan.items())

    @staticmethod
    def _event(
        event_type: str,
        symbol: str,
        timeframe: str,
        trade: dict[str, Any] | None,
        analysis_id: str,
        bar_ts: int,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "event_type": event_type,
            "recorded_at_ms": int(time.time() * 1000),
            "symbol": symbol,
            "timeframe": timeframe,
            "trade_id": trade.get("trade_id") if trade else None,
            "analysis_id": analysis_id,
            "bar_ts_open": bar_ts,
            "position_status": trade.get("status") if trade else "flat",
            "direction": trade.get("direction") if trade else None,
            "entry_price": trade.get("entry_price") if trade else None,
            "fill_price": trade.get("fill_price") if trade else None,
            "stop_loss_price": trade.get("stop_loss_price") if trade else None,
            "take_profit_price": trade.get("take_profit_price") if trade else None,
            "take_profit_price_2": trade.get("take_profit_price_2") if trade else None,
            "take_profit_1_hit": trade.get("take_profit_1_hit") if trade else False,
            "take_profit_2_hit": trade.get("take_profit_2_hit") if trade else False,
            "stop_loss_hit": trade.get("stop_loss_hit") if trade else False,
            "ambiguous_exit_bar": trade.get("ambiguous_exit_bar") if trade else False,
            "session_id": trade.get("session_id") if trade else None,
            "timeline_epoch": trade.get("timeline_epoch") if trade else None,
            "remaining_size_fraction": trade.get("remaining_size_fraction") if trade else None,
            "realized_pnl_points": trade.get("realized_pnl_points") if trade else None,
            "unrealized_points": trade.get("unrealized_points") if trade else None,
            "unrealized_pnl_points": trade.get("unrealized_pnl_points") if trade else None,
            **extra,
        }

    def _process_bar(
        self,
        trade: dict[str, Any],
        bar: dict[str, Any],
        analysis_id: str,
    ) -> list[dict[str, Any]]:
        if trade["status"] == "entry_pending":
            invalidation_reason = self._entry_invalidation_reason(trade, bar)
            if invalidation_reason:
                trade["status"] = "cancelled"
                trade["closed_reason"] = invalidation_reason
                trade["cancelled_ts_open"] = int(bar["ts_open"])
                return [self._event(
                    "entry_cancelled", "", "", trade, analysis_id,
                    int(bar["ts_open"]),
                    reason=invalidation_reason,
                    bar_open=bar["open"], bar_high=bar["high"],
                    bar_low=bar["low"], bar_close=bar["close"],
                )]
            fill = self._entry_fill(trade, bar)
            if fill is None:
                return [self._event(
                    "entry_pending", "", "", trade, analysis_id,
                    int(bar["ts_open"]),
                    bar_open=bar["open"], bar_high=bar["high"],
                    bar_low=bar["low"], bar_close=bar["close"],
                )]
            trade["status"] = "open"
            trade["fill_price"] = fill
            trade["entry_fill_ts_open"] = int(bar["ts_open"])
            trade["entry_fill_bar_open"] = float(bar["open"])
            fill_event = self._event(
                "entry_filled", "", "", trade, analysis_id, int(bar["ts_open"]),
                bar_open=bar["open"], bar_high=bar["high"],
                bar_low=bar["low"], bar_close=bar["close"],
            )
            fill_event["marker_kind"] = "entry"
            fill_event["marker_label"] = "入场"
            fill_event["marker_price"] = fill
            events = [fill_event]
            # A bar's high/low may have occurred before an intrabar fill. On
            # the entry bar, count an adverse stop touch conservatively but
            # defer profit targets until a later candle.
            events.extend(self._evaluate_exits(trade, bar, entry_bar=True))
            self._mark_to_market(trade, float(bar["close"]))
            fill_event.update(self._position_snapshot(trade))
            return events

        old_status = trade["status"]
        exit_events = self._evaluate_exits(trade, bar)
        self._mark_to_market(trade, float(bar["close"]))
        update_event = self._event(
            "position_bar_update", "", "", trade, analysis_id,
            int(bar["ts_open"]),
            bar_open=bar["open"], bar_high=bar["high"],
            bar_low=bar["low"], bar_close=bar["close"],
            previous_status=old_status,
            exit_reason=trade.get("closed_reason"),
        )
        update_event.update(self._position_snapshot(trade))
        return [update_event, *exit_events]

    @staticmethod
    def _position_snapshot(trade: dict[str, Any]) -> dict[str, Any]:
        return {
            "position_status": trade.get("status"),
            "remaining_size_fraction": trade.get("remaining_size_fraction"),
            "realized_pnl_points": trade.get("realized_pnl_points"),
            "unrealized_points": trade.get("unrealized_points"),
            "unrealized_pnl_points": trade.get("unrealized_pnl_points"),
            "take_profit_1_hit": trade.get("take_profit_1_hit", False),
            "take_profit_2_hit": trade.get("take_profit_2_hit", False),
            "stop_loss_hit": trade.get("stop_loss_hit", False),
            "ambiguous_exit_bar": trade.get("ambiguous_exit_bar", False),
            "exit_reason": trade.get("closed_reason"),
        }

    @staticmethod
    def _mark_to_market(trade: dict[str, Any], close: float) -> None:
        status = str(trade.get("status") or "")
        if status == "open":
            entry_fill = float(trade["fill_price"])
            points = (
                close - entry_fill if trade["direction"] == "做多"
                else entry_fill - close
            )
            trade["unrealized_points"] = points
            trade["unrealized_pnl_points"] = (
                points * float(trade.get("remaining_size_fraction", 1.0))
            )
        elif status.startswith("closed_"):
            trade["unrealized_points"] = 0.0
            trade["unrealized_pnl_points"] = 0.0
        else:
            trade["unrealized_points"] = None
            trade["unrealized_pnl_points"] = None

    @staticmethod
    def _marker_from_event(
        event: dict[str, Any],
        trade: dict[str, Any],
    ) -> dict[str, Any] | None:
        marker_kind = event.get("marker_kind")
        marker_price = _number(event.get("marker_price"))
        if marker_kind not in {"entry", "target", "exit"} or marker_price is None:
            return None
        return {
            "marker_kind": marker_kind,
            "label": str(event.get("marker_label") or ""),
            "price": marker_price,
            "bar_ts_open": int(event["bar_ts_open"]),
            "symbol": event.get("symbol"),
            "timeframe": event.get("timeframe"),
            "trade_id": trade.get("trade_id"),
            "direction": trade.get("direction"),
            "session_id": trade.get("session_id"),
            "timeline_epoch": trade.get("timeline_epoch"),
        }

    @staticmethod
    def _entry_fill(trade: dict[str, Any], bar: dict[str, Any]) -> float | None:
        direction = trade["direction"]
        order_type = trade["order_type"]
        entry = float(trade["entry_price"])
        high, low, open_price = float(bar["high"]), float(bar["low"]), float(bar["open"])
        if order_type == "市价单":
            # Decision is made after the preceding replay candle; use this
            # candle's open as the paper fill rather than backfilling at close.
            return open_price
        if order_type == "限价单":
            touched = low <= entry if direction == "做多" else high >= entry
            if not touched:
                return None
            return min(open_price, entry) if direction == "做多" else max(open_price, entry)
        if order_type == "突破单":
            touched = high >= entry if direction == "做多" else low <= entry
            if not touched:
                return None
            return max(open_price, entry) if direction == "做多" else min(open_price, entry)
        return None

    @staticmethod
    def _entry_invalidation_reason(
        trade: dict[str, Any],
        bar: dict[str, Any],
    ) -> str | None:
        """Reject orders whose gap open has already invalidated their price plan."""
        open_price = float(bar["open"])
        stop = float(trade["stop_loss_price"])
        target1 = float(trade["take_profit_price"])
        long_side = trade["direction"] == "做多"
        if long_side and open_price <= stop:
            return "K线开盘已越过多头止损位，入场计划失效"
        if not long_side and open_price >= stop:
            return "K线开盘已越过空头止损位，入场计划失效"
        if trade.get("order_type") in {"突破单", "市价单"}:
            if long_side and open_price >= target1:
                return "K线开盘已越过多头止盈1，入场计划失去有效盈亏比"
            if not long_side and open_price <= target1:
                return "K线开盘已越过空头止盈1，入场计划失去有效盈亏比"
        return None

    @staticmethod
    def _evaluate_exits(
        trade: dict[str, Any],
        bar: dict[str, Any],
        *,
        entry_bar: bool = False,
    ) -> list[dict[str, Any]]:
        long_side = trade["direction"] == "做多"
        high, low = float(bar["high"]), float(bar["low"])
        open_price = float(bar["open"])
        stop = float(trade["stop_loss_price"])
        target1 = float(trade["take_profit_price"])
        target2 = _number(trade.get("take_profit_price_2"))
        stop_hit = low <= stop if long_side else high >= stop
        target1_hit = high >= target1 if long_side else low <= target1
        target2_hit = (
            high >= target2 if long_side else low <= target2
        ) if target2 is not None else False

        if stop_hit:
            size_fraction = float(trade.get("remaining_size_fraction", 1.0))
            prior_tp1_hit = bool(trade.get("take_profit_1_hit"))
            exit_price = (
                min(open_price, stop) if long_side
                else max(open_price, stop)
            )
            realized_delta = BacktestTradeTracker._realize_position(
                trade, exit_price, size_fraction
            )
            trade["stop_loss_hit"] = True
            trade["ambiguous_exit_bar"] = bool(target1_hit or target2_hit)
            trade["status"] = "closed_stop_loss"
            trade["remaining_size_fraction"] = 0.0
            trade["closed_reason"] = (
                "同根K线触及止损和止盈，按保守规则记为止损优先"
                if trade["ambiguous_exit_bar"]
                else "止盈1触及后，剩余仓位触及止损" if prior_tp1_hit
                else "触及止损"
            )
            trade["exit_ts_open"] = int(bar["ts_open"])
            BacktestTradeTracker._mark_to_market(trade, float(bar["close"]))
            event = BacktestTradeTracker._exit_event(
                trade,
                bar,
                event_type="exit_triggered",
                exit_type="stop_loss",
                exit_price=exit_price,
                size_fraction=size_fraction,
                realized_delta=realized_delta,
            )
            if trade["ambiguous_exit_bar"]:
                event["also_touched_targets"] = [
                    level for level, hit in (("TP1", target1_hit), ("TP2", target2_hit))
                    if hit
                ]
            return [event]

        if entry_bar and (target1_hit or target2_hit):
            trade["entry_bar_target_deferred"] = True
            return []

        events: list[dict[str, Any]] = []
        if target1_hit and not trade.get("take_profit_1_hit"):
            fraction = (
                min(
                    float(trade.get("remaining_size_fraction", 1.0)),
                    _TP1_SCALE_OUT_FRACTION,
                )
                if target2 is not None else float(trade.get("remaining_size_fraction", 1.0))
            )
            target1_fill = max(open_price, target1) if long_side else min(open_price, target1)
            realized_delta = BacktestTradeTracker._realize_position(
                trade, target1_fill, fraction
            )
            trade["remaining_size_fraction"] = max(
                0.0, float(trade.get("remaining_size_fraction", 1.0)) - fraction
            )
            trade["take_profit_1_hit"] = True
            final_tp1 = target2 is None or trade["remaining_size_fraction"] <= 1e-9
            if final_tp1:
                trade["status"] = "closed_take_profit"
                trade["closed_reason"] = "触及止盈1"
                trade["exit_ts_open"] = int(bar["ts_open"])
            BacktestTradeTracker._mark_to_market(trade, float(bar["close"]))
            events.append(BacktestTradeTracker._exit_event(
                trade,
                bar,
                event_type="exit_triggered" if final_tp1 else "take_profit_1_hit",
                exit_type="take_profit_1",
                exit_price=target1_fill,
                size_fraction=fraction,
                realized_delta=realized_delta,
            ))

        if target2_hit and trade.get("remaining_size_fraction", 0.0) > 1e-9:
            remaining = float(trade["remaining_size_fraction"])
            target2_fill = max(open_price, target2) if long_side else min(open_price, target2)
            realized_delta = BacktestTradeTracker._realize_position(
                trade, target2_fill, remaining
            )
            trade["remaining_size_fraction"] = 0.0
            trade["take_profit_2_hit"] = True
            trade["status"] = "closed_take_profit"
            trade["closed_reason"] = "触及止盈2"
            trade["exit_ts_open"] = int(bar["ts_open"])
            BacktestTradeTracker._mark_to_market(trade, float(bar["close"]))
            events.append(BacktestTradeTracker._exit_event(
                trade,
                bar,
                event_type="exit_triggered",
                exit_type="take_profit_2",
                exit_price=target2_fill,
                size_fraction=remaining,
                realized_delta=realized_delta,
            ))
        return events

    @staticmethod
    def _realize_position(
        trade: dict[str, Any],
        exit_price: float,
        size_fraction: float,
    ) -> float:
        entry = float(trade["fill_price"])
        signed_points = (
            exit_price - entry if trade["direction"] == "做多"
            else entry - exit_price
        )
        realized_delta = signed_points * size_fraction
        trade["realized_pnl_points"] = (
            float(trade.get("realized_pnl_points", 0.0)) + realized_delta
        )
        return realized_delta

    @staticmethod
    def _exit_event(
        trade: dict[str, Any],
        bar: dict[str, Any],
        *,
        event_type: str,
        exit_type: str,
        exit_price: float,
        size_fraction: float,
        realized_delta: float,
    ) -> dict[str, Any]:
        labels = {
            "stop_loss": "止损",
            "take_profit_1": "TP1",
            "take_profit_2": "TP2",
        }
        marker_kind = "target" if exit_type.startswith("take_profit") else "exit"
        return BacktestTradeTracker._event(
            event_type,
            "",
            "",
            trade,
            "",
            int(bar["ts_open"]),
            exit_type=exit_type,
            exit_price=exit_price,
            size_fraction_closed=size_fraction,
            realized_pnl_delta=realized_delta,
            exit_reason=trade.get("closed_reason"),
            marker_kind=marker_kind,
            marker_label=labels[exit_type],
            marker_price=exit_price,
            bar_open=bar["open"],
            bar_high=bar["high"],
            bar_low=bar["low"],
            bar_close=bar["close"],
        )

    @staticmethod
    def _write_state(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
