"""Contracts shared by analysis orchestrators and prompt strategies.

The application still stores its canonical result in ``stage1_*`` and
``stage2_*`` record fields.  These protocols keep the execution and prompt
construction seams explicit while that persistence contract remains stable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from pa_agent.data.base import KlineFrame
    from pa_agent.records.schema import AnalysisRecord
    from pa_agent.util.threading import CancelToken, OrchestratorEvent


class AnalysisStrategy(Protocol):
    """Runtime strategy used by the analysis worker.

    Strategies are responsible for translating their execution flow to the
    current ``AnalysisRecord`` and ``OrchestratorEvent`` contracts.
    """

    def submit(
        self,
        frame: KlineFrame,
        cancel_token: CancelToken,
        on_event: Callable[[OrchestratorEvent], None],
        *,
        on_stage1_reasoning: Callable[[str], None] | None = None,
        on_stage1_content: Callable[[str], None] | None = None,
        on_stage2_reasoning: Callable[[str], None] | None = None,
        on_stage2_content: Callable[[str], None] | None = None,
        on_stage_prompt: Callable[[str, str, str], None] | None = None,
        on_stage2_files: Callable[[list[str]], None] | None = None,
        previous_record: AnalysisRecord | None = None,
        incremental_new_bar_count: int | None = None,
    ) -> AnalysisRecord:
        """Run one analysis and return its full or partial record."""


class TwoStagePromptStrategy(Protocol):
    """Prompt builder contract consumed by ``TwoStageOrchestrator``.

    A prompt strategy owns prompt content and message composition.  The
    orchestrator owns API calls, retries, validation, persistence and events.
    """

    def build_stage1(
        self,
        frame: KlineFrame,
        *,
        analysis_mode: str = "original",
    ) -> list[dict[str, Any]]:
        """Build the initial market-diagnosis conversation."""

    def build_incremental_stage1(
        self,
        frame: KlineFrame,
        previous_record: AnalysisRecord,
        new_bar_count: int,
        *,
        analysis_mode: str = "original",
        provider_settings: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Build a diagnosis continuation for newly closed bars."""

    def build_stage2_continuation(
        self,
        *,
        frame: KlineFrame,
        stage1_messages: list[dict[str, Any]],
        stage1_reply_content: str,
        stage1_json: dict[str, Any],
        strategy_files: list[str],
        experience_entries: list[Any],
        decision_stance: str = "conservative",
        previous_record: AnalysisRecord | None = None,
        enable_next_bar_prediction: bool = False,
        provider_settings: Any | None = None,
        structure_flip_cooldown_bars: int = 3,
        analysis_mode: str = "original",
    ) -> list[dict[str, Any]]:
        """Build the trade-decision conversation from validated diagnosis."""


class OutputValidator(Protocol):
    """Contract for validating or normalizing model output."""

    def validate(self, stage: str, content: str, **context: Any) -> Any:
        """Return a validated result or a structured validation failure."""
