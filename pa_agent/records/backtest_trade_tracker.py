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
    parsed: list[dict[str, Any]] = []
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
        bar = {"ts_open": ts, **values}
        parsed.append(bar)  # type: ignore[arg-type]
    return sorted(parsed, key=lambda item: item["ts_open"])


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

    def process_analysis(self, record: Any) -> dict[str, Any] | None:
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
            if state.get("last_analysis_id") == analysis_id:
                return state
            active = state.get("active_trade")
            latest_bar = bars[-1]
            if not isinstance(active, dict):
                active = None

            if active and active.get("status") in {"entry_pending", "open"}:
                last_ts = int(active.get("last_processed_bar_ts", 0) or 0)
                for bar in bars:
                    if int(bar["ts_open"]) <= last_ts:
                        continue
                    event = self._process_bar(active, bar, analysis_id)
                    active["last_processed_bar_ts"] = int(bar["ts_open"])
                    event.update({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "trade_id": active["trade_id"],
                        "analysis_id": analysis_id,
                    })
                    events.append(event)
                    if active.get("status") not in {"entry_pending", "open"}:
                        break

            if active and active.get("status") == "entry_pending":
                if has_decision and str(decision.get("order_type") or "") == "不下单":
                    active["status"] = "cancelled"
                    active["closed_reason"] = "最新分析撤销了未成交入场计划"
                    events.append(self._event(
                        "entry_cancelled", symbol, timeframe, active,
                        analysis_id, int(latest_bar["ts_open"]),
                        reason=active["closed_reason"],
                    ))
                elif self._plan_changed(active, current_plan):
                    old_plan = self._plan_fields(active)
                    active.update(current_plan)
                    active["last_plan_analysis_id"] = analysis_id
                    events.append(self._event(
                        "pending_plan_revised", symbol, timeframe, active,
                        analysis_id, int(latest_bar["ts_open"]),
                        previous_plan=old_plan,
                    ))
            elif active and active.get("status") == "open":
                # Preserve the actual fill and original order while recording
                # later AI suggestions for audit; they do not rewrite history.
                events.append(self._event(
                    "position_reviewed", symbol, timeframe, active, analysis_id,
                    int(latest_bar["ts_open"]),
                    ai_order_type=decision.get("order_type"),
                    ai_direction=decision.get("order_direction"),
                    ai_entry_price=_number(decision.get("entry_price")),
                    ai_stop_loss_price=_number(decision.get("stop_loss_price")),
                    ai_take_profit_price=_number(decision.get("take_profit_price")),
                ))

            if (
                (active is None or active.get("status") in {"closed_take_profit", "closed_stop_loss", "cancelled"})
                and current_plan is not None
            ):
                active = self._new_trade(
                    symbol, timeframe, current_plan, analysis_id,
                    int(latest_bar["ts_open"]),
                )
                events.append(self._event(
                    "entry_plan_created", symbol, timeframe, active, analysis_id,
                    int(latest_bar["ts_open"]),
                ))

            close = float(latest_bar["close"])
            if active and active.get("status") == "open":
                entry_fill = float(active["fill_price"])
                active["unrealized_points"] = (
                    close - entry_fill if active["direction"] == "做多"
                    else entry_fill - close
                )
            elif active:
                active["unrealized_points"] = None

            state = {
                "version": 1,
                "symbol": symbol,
                "timeframe": timeframe,
                "updated_at_ms": int(time.time() * 1000),
                "last_analysis_id": analysis_id,
                "last_bar_ts_open": int(latest_bar["ts_open"]),
                "last_close": close,
                "active_trade": active,
            }
            for event in events:
                _append_event(ledger_path, event)
            # Keep an audit row for every analyzed replay bar, including bars
            # where a pending order was untouched or an open position remained.
            if not any(
                event.get("event_type") == "bar_observed"
                and event.get("bar_ts_open") == int(latest_bar["ts_open"])
                for event in events
            ):
                _append_event(ledger_path, self._event(
                    "bar_observed", symbol, timeframe, active, analysis_id,
                    int(latest_bar["ts_open"]),
                    close=close,
                    position_status=active.get("status") if active else "flat",
                    unrealized_points=active.get("unrealized_points") if active else None,
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
    ) -> dict[str, Any]:
        return {
            "trade_id": f"{symbol}|{timeframe}|{analysis_id}",
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
            "unrealized_points": None,
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
            **extra,
        }

    def _process_bar(
        self,
        trade: dict[str, Any],
        bar: dict[str, Any],
        analysis_id: str,
    ) -> dict[str, Any]:
        if trade["status"] == "entry_pending":
            fill = self._entry_fill(trade, bar)
            if fill is None:
                return self._event(
                    "entry_pending", "", "", trade, analysis_id,
                    int(bar["ts_open"]),
                    bar_open=bar["open"], bar_high=bar["high"],
                    bar_low=bar["low"], bar_close=bar["close"],
                )
            trade["status"] = "open"
            trade["fill_price"] = fill
            trade["entry_fill_ts_open"] = int(bar["ts_open"])
            trade["entry_fill_bar_open"] = float(bar["open"])
            fill_event = self._event(
                "entry_filled", "", "", trade, analysis_id, int(bar["ts_open"]),
                bar_open=bar["open"], bar_high=bar["high"],
                bar_low=bar["low"], bar_close=bar["close"],
            )
            self._evaluate_exits(trade, bar)
            fill_event.update({
                "position_status": trade["status"],
                "take_profit_1_hit": trade["take_profit_1_hit"],
                "take_profit_2_hit": trade["take_profit_2_hit"],
                "stop_loss_hit": trade["stop_loss_hit"],
                "ambiguous_exit_bar": trade["ambiguous_exit_bar"],
                "exit_reason": trade.get("closed_reason"),
            })
            return fill_event

        old_status = trade["status"]
        self._evaluate_exits(trade, bar)
        return self._event(
            "position_bar_update", "", "", trade, analysis_id,
            int(bar["ts_open"]),
            bar_open=bar["open"], bar_high=bar["high"],
            bar_low=bar["low"], bar_close=bar["close"],
            previous_status=old_status,
            exit_reason=trade.get("closed_reason"),
            unrealized_points=trade.get("unrealized_points"),
        )

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
        touched = high >= entry if direction == "做多" else low <= entry
        if not touched:
            return None
        return max(open_price, entry) if direction == "做多" else min(open_price, entry)

    @staticmethod
    def _evaluate_exits(trade: dict[str, Any], bar: dict[str, Any]) -> None:
        long_side = trade["direction"] == "做多"
        high, low = float(bar["high"]), float(bar["low"])
        stop = float(trade["stop_loss_price"])
        target1 = float(trade["take_profit_price"])
        target2 = _number(trade.get("take_profit_price_2"))
        stop_hit = low <= stop if long_side else high >= stop
        target1_hit = high >= target1 if long_side else low <= target1
        target2_hit = (
            high >= target2 if long_side else low <= target2
        ) if target2 is not None else False

        if stop_hit and (target1_hit or target2_hit):
            trade["stop_loss_hit"] = True
            trade["take_profit_1_hit"] = trade["take_profit_1_hit"] or target1_hit or target2_hit
            trade["take_profit_2_hit"] = trade["take_profit_2_hit"] or target2_hit
            trade["ambiguous_exit_bar"] = True
            trade["status"] = "closed_stop_loss"
            trade["closed_reason"] = "同根K线触及止损和止盈，按保守规则记为止损优先"
            trade["exit_ts_open"] = int(bar["ts_open"])
            return

        if stop_hit:
            trade["stop_loss_hit"] = True
            trade["status"] = "closed_stop_loss"
            trade["closed_reason"] = (
                "止盈1触及后，剩余仓位触及止损"
                if trade["take_profit_1_hit"] else "触及止损"
            )
            trade["exit_ts_open"] = int(bar["ts_open"])
            return

        if target1_hit or target2_hit:
            trade["take_profit_1_hit"] = True
        if target2_hit:
            trade["take_profit_2_hit"] = True
            trade["status"] = "closed_take_profit"
            trade["closed_reason"] = "触及止盈2"
            trade["exit_ts_open"] = int(bar["ts_open"])
        elif target1_hit and target2 is None:
            trade["status"] = "closed_take_profit"
            trade["closed_reason"] = "触及止盈1"
            trade["exit_ts_open"] = int(bar["ts_open"])

    @staticmethod
    def _write_state(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
