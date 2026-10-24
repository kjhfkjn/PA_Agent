"""Extensible features derived from TradingView extension candle snapshots."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

_HOUR_MS = 3_600_000


def _one_hour_bars(rows: list[tuple[int, tuple[float, ...]]]) -> list[dict[str, float | int]]:
    """Aggregate chronological minute-based OHLCV rows into epoch-hour candles."""
    groups: dict[int, list[tuple[int, tuple[float, ...]]]] = defaultdict(list)
    for ts, values in rows:
        groups[(int(ts) // _HOUR_MS) * _HOUR_MS].append((int(ts), values))
    result: list[dict[str, float | int]] = []
    for start, entries in sorted(groups.items()):
        entries.sort(key=lambda row: row[0])
        values = [row[1] for row in entries]
        result.append({
            "time": start,
            "open": float(values[0][0]),
            "high": max(float(v[1]) for v in values),
            "low": min(float(v[2]) for v in values),
            "close": float(values[-1][3]),
            "volume": sum(float(v[4]) for v in values),
        })
    return result


def _hourly_key_levels(bars: list[dict[str, Any]], max_levels: int = 3) -> dict[str, Any]:
    """Pick nearby confirmed 3-bar pivot levels, clustered by a small price tolerance."""
    if len(bars) < 5:
        return {"supports": [], "resistances": [], "bars_available": len(bars)}
    recent = bars[-120:]
    close = float(recent[-1]["close"])
    ranges = [float(bar["high"]) - float(bar["low"]) for bar in recent[-20:]]
    tick_tolerance = max(sum(ranges) / max(1, len(ranges)) * 0.12, abs(close) * 1e-6)
    lows: list[float] = []
    highs: list[float] = []
    for i in range(1, len(recent) - 1):
        prev, bar, nxt = recent[i - 1], recent[i], recent[i + 1]
        low = float(bar["low"])
        high = float(bar["high"])
        if low < float(prev["low"]) and low <= float(nxt["low"]) and low < close:
            lows.append(low)
        if high > float(prev["high"]) and high >= float(nxt["high"]) and high > close:
            highs.append(high)

    def nearest_cluster(values: list[float], reverse: bool) -> list[dict[str, float]]:
        clusters: list[list[float]] = []
        for value in sorted(values, reverse=reverse):
            match = next((cluster for cluster in clusters
                          if abs(sum(cluster) / len(cluster) - value) <= tick_tolerance), None)
            if match is None:
                clusters.append([value])
            else:
                match.append(value)
        return [{"price": round(sum(c) / len(c), 6), "touches": len(c)}
                for c in clusters[:max_levels]]

    return {
        "supports": nearest_cluster(lows, True),
        "resistances": nearest_cluster(highs, False),
        "bars_available": len(recent),
        "last_hour_open_ms": int(recent[-1]["time"]),
    }


def build_tradingview_features(
    rows: list[tuple[int, tuple[float, ...]]],
    *, symbol: str,
    source_timeframe: str,
) -> tuple[dict[str, Any], ...]:
    """Return normalized feature records from the extension's full captured history.

    The feature contract is intentionally generic so more TradingView-derived
    feature types can be appended without changing prompt or GUI plumbing.
    """
    tf = str(source_timeframe).strip().lower()
    if not (tf.endswith("m") and tf[:-1].isdigit() and int(tf[:-1]) < 60):
        return ()
    hourly = _one_hour_bars(rows)
    levels = _hourly_key_levels(hourly)
    if levels["bars_available"] < 5:
        return ()
    return ({
        "source": "tradingview_extension",
        "feature_type": "key_support_resistance",
        "timeframe": "1h",
        "description": "1h 摆动低点支撑和摆动高点阻力；仅作高周期背景参考，保留原分析周期的阶段一支撑阻力字段语义",
        "symbol": symbol,
        "as_of_ms": levels.pop("last_hour_open_ms", None),
        "provenance": f"由扩展捕获的 {source_timeframe} OHLCV 按 UTC 整点聚合为 1h K 线，再识别三棒摆动高低点",
        "data": levels,
    },)
