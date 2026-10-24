"""Timeframe-agnostic context features derived from TradingView snapshots."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from pa_agent.data.context_timeframes import (
    MarketSession,
    align_bucket_start,
    bucket_end_ms,
    resolve_context_timeframes,
    timeframe_to_minutes,
)


def _clean_rows(
    rows: list[tuple[int, tuple[float, ...]]],
    cutoff_time_ms: int | None,
) -> list[tuple[int, tuple[float, ...]]]:
    unique: dict[int, tuple[float, ...]] = {}
    for timestamp, values in rows:
        timestamp_ms = int(timestamp)
        if cutoff_time_ms is not None and timestamp_ms > cutoff_time_ms:
            continue
        if len(values) < 5:
            continue
        unique[timestamp_ms] = tuple(float(value) for value in values[:5])
    return sorted(unique.items())


def _aggregate(
    rows: list[tuple[int, tuple[float, ...]]],
    *,
    base_minutes: int,
    target_timeframe: str,
    session: MarketSession,
) -> list[dict[str, float | int | bool]]:
    """Aggregate chronological OHLCV into session-anchored target candles."""
    target_minutes = timeframe_to_minutes(target_timeframe)
    if target_minutes is None:
        return []

    groups: dict[int, list[tuple[int, tuple[float, ...]]]] = defaultdict(list)
    for timestamp, values in rows:
        start = align_bucket_start(timestamp, target_timeframe, session)
        groups[start].append((int(timestamp), values))

    result: list[dict[str, float | int | bool]] = []
    base_ms = base_minutes * 60_000
    for start, entries in sorted(groups.items()):
        entries.sort(key=lambda row: row[0])
        values = [entry[1] for entry in entries]
        first_source_open = entries[0][0]
        last_source_open = entries[-1][0]
        result.append({
            "time": start,
            "end_time": bucket_end_ms(start, target_timeframe),
            "open": float(values[0][0]),
            "high": max(float(value[1]) for value in values),
            "low": min(float(value[2]) for value in values),
            "close": float(values[-1][3]),
            "volume": sum(float(value[4]) for value in values),
            "source_bars": len(values),
            "expected_source_bars": max(1, target_minutes // base_minutes),
            "complete": (
                first_source_open <= start + base_ms
                and bucket_end_ms(start, target_timeframe) <= last_source_open + base_ms
            ),
        })
    return result


def _native_candles(
    rows: list[tuple[int, tuple[float, ...]]],
    *,
    target_timeframe: str,
    base_minutes: int,
    cutoff_time_ms: int,
) -> list[dict[str, float | int | bool]]:
    """Convert native target-timeframe snapshots, excluding bars that close after cutoff."""
    target_minutes = timeframe_to_minutes(target_timeframe)
    if target_minutes is None:
        return []
    result: list[dict[str, float | int | bool]] = []
    for timestamp, values in _clean_rows(rows, cutoff_time_ms):
        complete = timestamp + target_minutes * 60_000 <= cutoff_time_ms + base_minutes * 60_000
        result.append({
            "time": timestamp,
            "end_time": timestamp + target_minutes * 60_000,
            "open": float(values[0]),
            "high": float(values[1]),
            "low": float(values[2]),
            "close": float(values[3]),
            "volume": float(values[4]),
            "source_bars": target_minutes // max(base_minutes, 1),
            "expected_source_bars": target_minutes // max(base_minutes, 1),
            "complete": complete,
            "native": True,
        })
    return result


def _levels(bars: list[dict[str, Any]], limit: int = 120) -> dict[str, Any]:
    """Return nearby confirmed swing levels with range-based clustering."""
    recent = bars[-limit:]
    if len(recent) < 5:
        return {"supports": [], "resistances": [], "bars_available": len(recent)}
    close = float(recent[-1]["close"])
    ranges = [float(bar["high"]) - float(bar["low"]) for bar in recent[-20:]]
    tolerance = max(sum(ranges) / len(ranges) * 0.12, abs(close) * 1e-6)
    lows: list[float] = []
    highs: list[float] = []
    for index in range(1, len(recent) - 1):
        previous, bar, following = recent[index - 1:index + 2]
        low, high = float(bar["low"]), float(bar["high"])
        if low < float(previous["low"]) and low <= float(following["low"]) and low < close:
            lows.append(low)
        if high > float(previous["high"]) and high >= float(following["high"]) and high > close:
            highs.append(high)

    def clusters(values: list[float], reverse: bool) -> list[dict[str, float]]:
        groups: list[list[float]] = []
        for value in sorted(values, reverse=reverse):
            match = next(
                (
                    group
                    for group in groups
                    if abs(sum(group) / len(group) - value) <= tolerance
                ),
                None,
            )
            if match is None:
                groups.append([value])
            else:
                match.append(value)
        return [
            {"price": round(sum(group) / len(group), 6), "touches": len(group)}
            for group in groups[:3]
        ]

    return {
        "supports": clusters(lows, True),
        "resistances": clusters(highs, False),
        "bars_available": len(recent),
        "last_bar_open_ms": int(recent[-1]["time"]),
    }


def _base_feature(
    *,
    symbol: str,
    source_timeframe: str,
    context_timeframe: str,
    cutoff_time_ms: int,
    method: str,
    description: str,
) -> dict[str, Any]:
    return {
        "source": "tradingview_extension",
        "feature_type": "key_support_resistance",
        "symbol": symbol,
        "timeframe": context_timeframe,
        "as_of_ms": cutoff_time_ms,
        "method": method,
        "bars_used": 0,
        "available": False,
        "description": description,
        "provenance": "",
        "data": {},
        "primary_timeframe": source_timeframe,
    }


def build_unavailable_context_features(
    *,
    symbol: str,
    source_timeframe: str,
    context_timeframes: list[str],
    cutoff_time_ms: int,
    reason: str,
) -> tuple[dict[str, Any], ...]:
    """Return explicit unavailable records when context setup itself is invalid."""
    records = []
    for context_timeframe in context_timeframes:
        feature = _base_feature(
            symbol=symbol,
            source_timeframe=source_timeframe,
            context_timeframe=context_timeframe,
            cutoff_time_ms=cutoff_time_ms,
            method="not_computed",
            description=f"{context_timeframe} 摆动支撑阻力高周期上下文",
        )
        feature["provenance"] = "上下文设置无效，未执行聚合"
        feature["data"] = {"reason": reason}
        records.append(feature)
    return tuple(records)


def build_tradingview_features(
    rows: list[tuple[int, tuple[float, ...]]],
    *,
    symbol: str,
    source_timeframe: str,
    cutoff_time_ms: int | None = None,
    context_timeframes: list[str] | None = None,
    native_snapshots: dict[str, list[tuple[int, tuple[float, ...]]]] | None = None,
    session: MarketSession | None = None,
) -> tuple[dict[str, Any], ...]:
    """Build one normalized feature record per configured context timeframe.

    Every current context candle is rebuilt from execution-timeframe rows up to
    ``cutoff_time_ms``. Native higher-timeframe snapshots may only fill older
    history before that overlap. A missing/stale native snapshot can therefore
    never masquerade as the current context candle.
    """
    base_minutes = timeframe_to_minutes(source_timeframe)
    if base_minutes is None:
        return ()
    session = session or MarketSession()
    contexts = context_timeframes
    if contexts is None:
        contexts = resolve_context_timeframes(source_timeframe)
    native_snapshots = native_snapshots or {}

    chronological_all = _clean_rows(rows, None)
    if not chronological_all:
        return ()
    inferred_cutoff = int(chronological_all[-1][0])
    cutoff = int(cutoff_time_ms) if cutoff_time_ms is not None else inferred_cutoff
    chronological = _clean_rows(chronological_all, cutoff)
    records: list[dict[str, Any]] = []

    for context_timeframe in contexts:
        target_minutes = timeframe_to_minutes(context_timeframe)
        feature = _base_feature(
            symbol=symbol,
            source_timeframe=source_timeframe,
            context_timeframe=context_timeframe,
            cutoff_time_ms=cutoff,
            method="session_aligned_ohlcv_aggregation+confirmed_3bar_pivots",
            description=f"{context_timeframe} 摆动支撑阻力高周期上下文",
        )
        if target_minutes is None:
            feature["data"] = {"reason": "该周期不是固定时长，暂不支持聚合"}
            records.append(feature)
            continue
        if target_minutes <= base_minutes or target_minutes % base_minutes:
            feature["data"] = {
                "reason": f"{context_timeframe} 必须高于且可整除主交易周期 {source_timeframe}"
            }
            records.append(feature)
            continue
        if not chronological:
            feature["data"] = {"reason": "截止时间之前没有主周期 K 线"}
            records.append(feature)
            continue

        derived = _aggregate(
            chronological,
            base_minutes=base_minutes,
            target_timeframe=context_timeframe,
            session=session,
        )
        current_start = align_bucket_start(cutoff, context_timeframe, session)
        current_bar = next(
            (bar for bar in reversed(derived) if int(bar["time"]) == current_start),
            None,
        )
        if current_bar is None:
            feature["data"] = {
                "reason": "主周期数据尚未覆盖当前上下文 K 线",
                "current_context_bar_open_ms": current_start,
            }
            records.append(feature)
            continue

        usable_derived = [
            bar for bar in derived if bool(bar["complete"]) or bar is current_bar
        ]
        first_derived_start = int(usable_derived[0]["time"])
        native = native_snapshots.get(context_timeframe, [])
        native_history = [
            bar
            for bar in _native_candles(
                native,
                target_timeframe=context_timeframe,
                base_minutes=base_minutes,
                cutoff_time_ms=cutoff,
            )
            if int(bar["time"]) < first_derived_start and bool(bar["complete"])
        ]
        combined = (native_history + usable_derived)[-500:]
        levels = _levels(combined)
        bars_used = int(levels["bars_available"])
        latest_source_bars = int(current_bar["source_bars"])
        current_complete = bool(current_bar["complete"])
        coverage_gap_ms = 0
        if native_history:
            coverage_gap_ms = max(
                0,
                first_derived_start - int(native_history[-1]["end_time"]),
            )

        feature["bars_used"] = bars_used
        feature["available"] = bars_used >= 5
        feature["provenance"] = (
            f"主周期={source_timeframe}；按 {session.timezone} 交易时段锚点 "
            f"{session.anchor} 聚合；截止={cutoff}ms；"
            f"当前上下文桶来自主周期续接，原生高周期历史={len(native_history)} 根"
        )
        feature["data"] = {
            **levels,
            "base_timeframe": source_timeframe,
            "context_timeframe": context_timeframe,
            "session_timezone": session.timezone,
            "session_anchor": session.anchor,
            "session_source": session.source,
            "native_history_bars": len(native_history),
            "derived_bars": len(derived),
            "input_bars": len(combined),
            "latest_context_bar_open_ms": current_start,
            "latest_context_bar_end_ms": bucket_end_ms(current_start, context_timeframe),
            "latest_context_bar_source_bars": latest_source_bars,
            "latest_context_bar_expected_source_bars": int(
                current_bar["expected_source_bars"]
            ),
            "latest_context_bar_complete": current_complete,
            "coverage_gap_ms": coverage_gap_ms,
            "current_data_view": "primary_timeframe_rolling_aggregation",
        }
        if not feature["available"]:
            feature["data"]["reason"] = (
                f"仅有 {bars_used} 根可计算上下文 K 线，至少需要 5 根；"
                "不会使用旧快照冒充当前数据"
            )
        records.append(feature)
    return tuple(records)
