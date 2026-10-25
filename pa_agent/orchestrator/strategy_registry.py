"""Registry for selectable PA analysis execution strategies."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any

from pa_agent.orchestrator.contracts import AnalysisStrategy, OutputValidator


@dataclass(frozen=True)
class AnalysisComponents:
    """Shared application services available to strategy factories.

    Components are optional because strategies have different needs.  A
    strategy factory validates only the services its own flow requires.
    """

    client: Any = None
    assembler: Any = None
    prompt_strategy: Any = None
    router: Any = None
    validator: OutputValidator | None = None
    pending_writer: Any = None
    exp_reader: Any = None
    settings: Any = None


StrategyFactory = Callable[[AnalysisComponents], AnalysisStrategy]


class AnalysisStrategyRegistry:
    """Maps stable strategy names to orchestrator factories.

    Factories receive one ``AnalysisComponents`` object.  This keeps
    strategy-specific execution flows out of the GUI wiring and lets new
    strategies reuse the existing clients, validators, prompt builders and
    record writer.
    """

    def __init__(self) -> None:
        self._factories: dict[str, StrategyFactory] = {}
        self._lock = RLock()

    def register(
        self,
        name: str,
        factory: StrategyFactory,
        *,
        replace: bool = False,
    ) -> None:
        """Register *factory* under a configuration-safe name."""
        normalized = str(name or "").strip().lower()
        if not normalized or any(not (ch.isalnum() or ch in "_-") for ch in normalized):
            raise ValueError(
                "Analysis strategy names may contain only letters, digits, '_' and '-'."
            )
        if not callable(factory):
            raise TypeError("Analysis strategy factory must be callable.")
        with self._lock:
            if normalized in self._factories and not replace:
                raise ValueError(f"Analysis strategy {normalized!r} is already registered.")
            self._factories[normalized] = factory

    def create(
        self,
        name: str,
        components: AnalysisComponents,
    ) -> AnalysisStrategy:
        """Create a strategy or raise an actionable error for unknown names."""
        normalized = str(name or "").strip().lower()
        with self._lock:
            factory = self._factories.get(normalized)
            available = sorted(self._factories)
        if factory is None:
            choices = ", ".join(available) or "(none)"
            raise KeyError(
                f"Unknown analysis strategy {normalized!r}. Registered strategies: {choices}."
            )
        return factory(components)

    def names(self) -> tuple[str, ...]:
        """Return registered names in sorted order."""
        with self._lock:
            return tuple(sorted(self._factories))


analysis_strategy_registry = AnalysisStrategyRegistry()


def _create_two_stage_strategy(components: AnalysisComponents) -> AnalysisStrategy:
    # Lazy import keeps registry discovery independent from the legacy
    # two-stage implementation module.
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator

    prompt_strategy = (
        components.prompt_strategy
        if components.prompt_strategy is not None
        else components.assembler
    )
    missing = [
        name
        for name, value in (
            ("client", components.client),
            ("router", components.router),
            ("validator", components.validator),
            ("pending_writer", components.pending_writer),
            ("exp_reader", components.exp_reader),
            ("assembler or prompt_strategy", prompt_strategy),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "The two_stage analysis strategy is missing required components: "
            + ", ".join(missing)
        )
    return TwoStageOrchestrator(
        client=components.client,
        assembler=components.assembler,
        router=components.router,
        validator=components.validator,
        pending_writer=components.pending_writer,
        exp_reader=components.exp_reader,
        settings=components.settings,
        prompt_strategy=components.prompt_strategy,
    )


analysis_strategy_registry.register("two_stage", _create_two_stage_strategy)


def register_analysis_strategy(
    name: str,
    factory: StrategyFactory,
    *,
    replace: bool = False,
) -> None:
    """Register an application strategy before the next analysis is built."""
    analysis_strategy_registry.register(name, factory, replace=replace)


def create_analysis_strategy(
    name: str,
    components: AnalysisComponents,
) -> AnalysisStrategy:
    """Construct the configured analysis strategy."""
    return analysis_strategy_registry.create(name, components)


def available_analysis_strategies() -> tuple[str, ...]:
    """Return strategy keys available to configuration and diagnostics."""
    return analysis_strategy_registry.names()
