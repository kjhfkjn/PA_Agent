"""Selectable LLM analysis prompt modes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pa_agent.config.settings import Settings

AnalysisModeKey = Literal["original", "optimized", "compact_two_stage"]


@dataclass(frozen=True)
class GuiAnalysisMode:
    """A selectable analysis process mode."""

    key: AnalysisModeKey
    label: str
    description: str


_MODES: tuple[GuiAnalysisMode, ...] = (
    GuiAnalysisMode(
        key="original",
        label="策略树模式（完整提示词）",
        description="沿用原有完整策略树提示词，保留全部分析规则。",
    ),
    GuiAnalysisMode(
        key="compact_two_stage",
        label="精简两阶段（PA/ICT）",
        description=(
            "用短提示词分析市场结构与入场条件；保留有效 K 线、风险回报和校验，"
            "决策结果仍可用于策略树可视化。"
        ),
    ),
)

_MODE_BY_KEY = {mode.key: mode for mode in _MODES}
# ``optimized`` was accepted by an earlier prompt-mode scaffold but did not
# select a distinct prompt path. Keep old saved values mapped to the legacy mode.
_MODE_BY_KEY["optimized"] = _MODE_BY_KEY["original"]  # type: ignore[index]


def analysis_mode_choices() -> list[tuple[str, str]]:
    """Return selectable modes in display order."""
    return [(mode.key, mode.label) for mode in _MODES]


def get_analysis_mode(key: str) -> GuiAnalysisMode:
    """Return an analysis mode by key, raising KeyError for unknown keys."""
    return _MODE_BY_KEY[key]  # type: ignore[index]


def apply_analysis_mode(settings: "Settings", key: str) -> GuiAnalysisMode:
    """Apply *key* to in-memory settings used by the next GUI analysis."""
    mode = get_analysis_mode(key)
    # Store in settings if the field exists (graceful degradation)
    if hasattr(settings.general, "analysis_mode"):
        settings.general.analysis_mode = mode.key
    return mode


def infer_analysis_mode_key(settings: "Settings | None") -> str:
    """Best-effort mode match for current settings."""
    if settings is None:
        return "original"
    mode = str(getattr(settings.general, "analysis_mode", "original") or "original")
    if mode in _MODE_BY_KEY:
        return _MODE_BY_KEY[mode].key  # type: ignore[index]
    return "original"
