# Analysis strategy extension points

`analysis_strategy` selects the execution flow. `analysis_mode` selects prompt
content within a flow; the two settings are deliberately separate.

## Current flow

The default `two_stage` strategy keeps the existing market-diagnosis then
trade-decision process. It is registered in
`pa_agent/orchestrator/strategy_registry.py`; the GUI asks that registry to
build the configured strategy instead of importing `TwoStageOrchestrator`
directly.

The strategy registry receives an `AnalysisComponents` object. Strategies can
use only the services they need, so a future direct-output strategy does not
have to depend on the strategy-file router or experience reader. Strategy
implementations expose `submit(...)` as described by `AnalysisStrategy`.

To add one, implement the existing worker-facing `submit` contract, then
register a factory during application initialization:

```python
from pa_agent.orchestrator.strategy_registry import (
    AnalysisComponents,
    register_analysis_strategy,
)


def build_single_stage(components: AnalysisComponents):
    return SingleStageOrchestrator(
        client=components.client,
        validator=components.validator,
        pending_writer=components.pending_writer,
        prompt_strategy=components.prompt_strategy,
        settings=components.settings,
    )


register_analysis_strategy("single_stage", build_single_stage)
```

Register it during application initialization, before the first analysis.
Then set `general.analysis_strategy` to `"single_stage"` in `settings.json`.
Unknown names fail with a message listing registered strategies.

## Prompt changes

`TwoStageOrchestrator` accepts a `TwoStagePromptStrategy`. It only asks that
object to build the Stage 1, incremental Stage 1 and Stage 2 message lists;
model calls, streaming, validation retries, fallback handling and record
persistence stay in the orchestrator. If no prompt strategy is injected, the
existing `PromptAssembler` remains the default.

For prompt experiments that keep the two-stage flow, implement
`TwoStagePromptStrategy` and expose it as `AppContext.prompt_strategy`. For a
different flow, let the strategy own its prompt contract and use
`AnalysisComponents.prompt_strategy` or `AnalysisComponents.assembler` as
appropriate.

The shared `validator` follows `OutputValidator` and is available to all
strategies. Strategies that need the existing bounded feedback retry can use
`validate_with_retry` from `validation_retry.py`; a different validation
policy can be implemented without changing prompt construction or the
strategy registry.

## Compatibility boundary

The current GUI worker and persisted record schema still expose the legacy
`stage1_*` and `stage2_*` fields/events. A new strategy should map its
intermediate and final results into that contract until the record and UI
event model are made strategy-neutral. The selected strategy name is stored
in `RecordMeta.analysis_strategy` for auditability.
