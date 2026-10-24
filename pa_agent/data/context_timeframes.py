"""Resolution and session-aware bucketing helpers for higher-timeframe context."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as datetime_time
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MINUTE_MS = 60_000
_TIMEFRAME_RE = re.compile(r"^(\d+)(m|h|d|w|M)$")

# A neutral ordering used only to suggest context periods when the user leaves
# the setting blank. Explicit settings always take precedence.
_AUTO_LADDER_MINUTES = (
    1, 3, 5, 15, 30, 60, 120, 240, 1_440,
)

_CME_BASES = frozenset({
    "MES", "ES", "MNQ", "NQ", "MYM", "YM", "M2K", "RTY",
    "GC", "MGC", "SI", "SIL", "CL", "MCL", "NG", "NGF",
    "HO", "RB", "ZB", "ZN", "ZF", "ZT", "6E", "6B", "6J",
    "6A", "6C", "6S", "LE", "HE", "ZC", "ZS", "ZW", "KE",
})


@dataclass(frozen=True, slots=True)
class MarketSession:
    """Session anchor used to align higher-timeframe candle buckets."""

    timezone: str = "UTC"
    anchor: str = "00:00"
    source: str = "default"

    @property
    def anchor_minutes(self) -> int:
        return parse_session_anchor(self.anchor)


def parse_session_anchor(value: str | None) -> int:
    """Parse ``HH:MM`` into minutes after midnight."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError("交易时段锚点必须使用 HH:MM 格式")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("交易时段锚点超出有效范围")
    return hour * 60 + minute


def normalize_timeframe(value: object) -> str:
    """Return the app's canonical timeframe notation."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if raw.isdigit():
        minutes = int(raw)
        if minutes == 1_440:
            return "1d"
        if minutes == 10_080:
            return "1w"
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    aliases = {
        "D": "1d",
        "1D": "1d",
        "W": "1w",
        "1W": "1w",
        "M": "1M",
        "1MTH": "1M",
    }
    if upper in aliases:
        return aliases[upper]
    if upper.endswith("H") and upper[:-1].isdigit():
        return f"{int(upper[:-1])}h"
    if upper.endswith("D") and upper[:-1].isdigit():
        return f"{int(upper[:-1])}d"
    if upper.endswith("W") and upper[:-1].isdigit():
        return f"{int(upper[:-1])}w"
    if raw.endswith("m") and raw[:-1].isdigit():
        return f"{int(raw[:-1])}m"
    if upper.endswith("M") and upper[:-1].isdigit():
        return f"{int(upper[:-1])}M"
    return raw


def timeframe_to_minutes(value: object) -> int | None:
    """Return fixed-duration timeframe length in minutes.

    Calendar weeks/months are deliberately unsupported for candle aggregation
    because their boundaries depend on exchange calendars, not fixed durations.
    """
    timeframe = normalize_timeframe(value)
    match = _TIMEFRAME_RE.fullmatch(timeframe)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        return None
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60
    if unit == "d":
        return amount * 1_440
    return None


def _setting_value(settings: Any, name: str, default: Any = None) -> Any:
    if settings is None:
        return default
    if isinstance(settings, dict):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _normalize_periods(values: Iterable[object] | object) -> list[str]:
    if isinstance(values, str):
        values = values.replace("，", ",").split(",")
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        normalized = normalize_timeframe(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _auto_context_timeframes(base_minutes: int) -> list[str]:
    higher = [value for value in _AUTO_LADDER_MINUTES if value > base_minutes]
    if not higher:
        return []
    first_threshold = 5 if base_minutes <= 1 else base_minutes * 3
    first = next(
        (value for value in higher if value >= first_threshold),
        higher[0],
    )
    selected = [first]
    desired_count = 3 if base_minutes < 5 else 2
    while len(selected) < desired_count:
        threshold = selected[-1] * 3
        following = next(
            (value for value in _AUTO_LADDER_MINUTES if value >= threshold),
            None,
        )
        if following is None:
            break
        selected.append(following)
    return [_minutes_to_timeframe(value) for value in selected]


def _minutes_to_timeframe(minutes: int) -> str:
    if minutes == 1_440:
        return "1d"
    if minutes == 10_080:
        return "1w"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def resolve_context_timeframes(
    primary_timeframe: object,
    configured: Iterable[object] | object = (),
    by_primary: dict[str, Iterable[object]] | None = None,
) -> list[str]:
    """Resolve configured higher timeframes without hard-coding a workflow.

    ``by_primary`` is an exact-match override (``*`` is also accepted). If no
    explicit periods remain, a neutral ladder suggests two or three higher
    resolutions. Every selected period must be strictly higher and divisible
    by the execution timeframe.
    """
    primary = normalize_timeframe(primary_timeframe)
    base_minutes = timeframe_to_minutes(primary)
    if base_minutes is None:
        return []

    selected = _normalize_periods(configured)
    overrides = {
        normalize_timeframe(key): value
        for key, value in (by_primary or {}).items()
    }
    override = overrides.get(primary, overrides.get("*"))
    if override is not None:
        selected = _normalize_periods(override)
    if not selected:
        selected = _auto_context_timeframes(base_minutes)

    result: list[str] = []
    for timeframe in selected:
        target_minutes = timeframe_to_minutes(timeframe)
        if (
            target_minutes is None
            or target_minutes <= base_minutes
            or target_minutes % base_minutes
        ):
            continue
        if timeframe not in result:
            result.append(timeframe)
    return result


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().split(":")[-1]


def _is_cme_symbol(normalized: str) -> bool:
    core = normalized.rstrip("!")
    if core.endswith("=F"):
        core = core[:-2]
    if core in _CME_BASES:
        return True
    match = re.fullmatch(r"([A-Z0-9]+?)([FGHJKMNQUVXZ]\d{1,2})", core)
    return bool(match and match.group(1) in _CME_BASES)


def infer_market_session(symbol: str) -> MarketSession:
    """Infer a sensible session anchor from common instrument families."""
    normalized = _normalize_symbol(symbol).replace("1!", "!")
    bare = re.sub(r"(?:M|MICRO|CASH|PERP|USDT)?$", "", normalized)

    if _is_cme_symbol(normalized):
        return MarketSession(
            timezone="America/Chicago",
            anchor="17:00",
            source="inferred_cme",
        )
    if normalized.endswith("USDT"):
        return MarketSession(
            timezone="UTC",
            anchor="00:00",
            source="inferred_crypto",
        )
    if (
        any(token in normalized for token in ("XAU", "XAG"))
        or re.fullmatch(r"[A-Z]{6}", bare)
    ):
        return MarketSession(
            timezone="America/New_York",
            anchor="17:00",
            source="inferred_fx",
        )
    if re.fullmatch(r"\d{6}", normalized):
        return MarketSession(
            timezone="Asia/Shanghai",
            anchor="09:30",
            source="inferred_cn_equity",
        )
    if re.fullmatch(r"[A-Z]{1,3}\d{1,3}", normalized):
        return MarketSession(
            timezone="Asia/Shanghai",
            anchor="21:00",
            source="inferred_cn_futures",
        )
    if re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", normalized):
        return MarketSession(
            timezone="America/New_York",
            anchor="09:30",
            source="inferred_us_equity",
        )
    return MarketSession()


def _coerce_session(value: Any) -> MarketSession | None:
    if value is None:
        return None
    if isinstance(value, MarketSession):
        return value
    if isinstance(value, dict):
        timezone = str(value.get("timezone", "") or "")
        anchor = str(value.get("anchor", "") or "")
    else:
        timezone = str(getattr(value, "timezone", "") or "")
        anchor = str(getattr(value, "anchor", "") or "")
    if not timezone and not anchor:
        return None
    return MarketSession(
        timezone=timezone or "UTC",
        anchor=anchor or "00:00",
        source="configured",
    )


def resolve_market_session(symbol: str, settings: Any = None) -> MarketSession:
    """Resolve a symbol-specific session override, then global settings, then inference."""
    inferred = infer_market_session(symbol)
    by_symbol = _setting_value(settings, "session_by_symbol", {}) or {}
    normalized = _normalize_symbol(symbol)
    normalized_overrides = {
        _normalize_symbol(key): value
        for key, value in by_symbol.items()
    }
    configured = _coerce_session(
        normalized_overrides.get(normalized, normalized_overrides.get("*"))
    )

    timezone = str(configured.timezone if configured else "") or str(
        _setting_value(settings, "session_timezone", "") or ""
    )
    anchor = str(configured.anchor if configured else "") or str(
        _setting_value(settings, "session_anchor", "") or ""
    )
    if not timezone and not anchor:
        return inferred
    resolved = MarketSession(
        timezone=timezone or inferred.timezone,
        anchor=anchor or inferred.anchor,
        source="configured",
    )
    validate_market_session(resolved)
    return resolved


def validate_market_session(session: MarketSession) -> None:
    """Raise ``ValueError`` when a session timezone or anchor is invalid."""
    parse_session_anchor(session.anchor)
    try:
        ZoneInfo(session.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知交易时段时区: {session.timezone}") from exc


def align_bucket_start(
    timestamp_ms: int | float,
    target_timeframe: object,
    session: MarketSession,
) -> int:
    """Align a timestamp to a session-anchored higher-timeframe bucket."""
    target_minutes = timeframe_to_minutes(target_timeframe)
    if target_minutes is None:
        raise ValueError(f"无法按固定时长聚合周期 {target_timeframe!r}")
    validate_market_session(session)

    target_ms = target_minutes * _MINUTE_MS
    zone = ZoneInfo(session.timezone)
    local = datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=zone)
    anchor_time = datetime_time(
        hour=session.anchor_minutes // 60,
        minute=session.anchor_minutes % 60,
    )
    anchor = datetime.combine(local.date(), anchor_time, tzinfo=zone)
    if local < anchor:
        anchor -= timedelta(days=1)
    elapsed_ms = int(local.timestamp() * 1000) - int(anchor.timestamp() * 1000)
    bucket_index = elapsed_ms // target_ms
    bucket_start = anchor + timedelta(milliseconds=bucket_index * target_ms)
    return int(bucket_start.timestamp() * 1000)


def bucket_end_ms(
    start_ms: int | float,
    target_timeframe: object,
) -> int:
    """Return the exclusive end timestamp of a fixed-duration bucket."""
    minutes = timeframe_to_minutes(target_timeframe)
    if minutes is None:
        raise ValueError(f"无法计算周期 {target_timeframe!r} 的固定时长")
    return int(start_ms) + minutes * _MINUTE_MS
