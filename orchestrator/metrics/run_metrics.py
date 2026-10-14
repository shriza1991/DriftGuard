"""Typed models and computation for per-run metrics and token usage."""
from typing import Dict, Any, Optional, List, Union
from pydantic import BaseModel, Field


class ModelUsageBreakdown(BaseModel):
    """Token usage breakdown per purpose or session."""
    input_tokens: Union[int, str] = "unavailable"
    output_tokens: Union[int, str] = "unavailable"
    total_tokens: Union[int, str] = "unavailable"
    is_complete: bool = False
    status: str = "unavailable"  # "complete" | "incomplete" | "unavailable"


class RunMetrics(BaseModel):
    """Detailed measured metrics for an individual execution run."""
    run_id: str
    mode: str
    execution_type: Optional[str] = None
    status: str
    duration_ms: Optional[float] = None
    total_turns: int = 0
    tool_dispatch_count: int = 0
    original_dispatch_count: int = 0
    repaired_dispatch_count: int = 0
    repair_attempts_count: int = 0
    repair_rejections_count: int = 0
    verified_recoveries_count: int = 0
    registry_commits_count: int = 0
    recovery_duration_ms: Optional[float] = None  # from first drift failure to verified read
    commit_latency_ms: Optional[float] = None     # separate measurement for atomic commit
    failed_recovery_duration_ms: Optional[float] = None  # measured on failure/refusal
    agent_token_usage: ModelUsageBreakdown = Field(default_factory=ModelUsageBreakdown)
    diagnosis_token_usage: ModelUsageBreakdown = Field(default_factory=ModelUsageBreakdown)
    session_token_usage: ModelUsageBreakdown = Field(default_factory=ModelUsageBreakdown)
    audit_status: str = "complete"


def compute_usage_breakdown(usages: List[Any]) -> ModelUsageBreakdown:
    """Computes a truthful token usage breakdown from a list of model usage records.
    
    If any record is missing usage or marked incomplete:
    - If some subtotal counts exist, preserves known subtotals with status='incomplete'
    - Otherwise status='unavailable'
    When complete:
    - input_tokens + output_tokens == total_tokens
    """
    if not usages:
        return ModelUsageBreakdown(
            input_tokens="unavailable",
            output_tokens="unavailable",
            total_tokens="unavailable",
            is_complete=False,
            status="unavailable",
        )

    all_complete = True
    has_known_numbers = False
    total_input = 0
    total_output = 0

    for u in usages:
        inp = getattr(u, "prompt_tokens", None)
        out = getattr(u, "completion_tokens", None)
        comp = getattr(u, "is_complete", True)

        if not comp or inp is None or out is None:
            all_complete = False

        if inp is not None:
            total_input += inp
            has_known_numbers = True
        if out is not None:
            total_output += out
            has_known_numbers = True

    if all_complete:
        return ModelUsageBreakdown(
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            is_complete=True,
            status="complete",
        )
    elif has_known_numbers:
        return ModelUsageBreakdown(
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            is_complete=False,
            status="incomplete",
        )
    else:
        return ModelUsageBreakdown(
            input_tokens="unavailable",
            output_tokens="unavailable",
            total_tokens="unavailable",
            is_complete=False,
            status="unavailable",
        )


def combine_session_usage(
    agent_usage: ModelUsageBreakdown,
    diagnosis_usage: ModelUsageBreakdown,
) -> ModelUsageBreakdown:
    """Combines agent and diagnosis usage into session totals, including each call exactly once.
    
    Does NOT combine session total with agent-only input/output under the same label.
    """
    agent_has_nums = isinstance(agent_usage.input_tokens, int) and isinstance(agent_usage.output_tokens, int)
    diag_has_nums = isinstance(diagnosis_usage.input_tokens, int) and isinstance(diagnosis_usage.output_tokens, int)

    if not agent_has_nums and not diag_has_nums:
        return ModelUsageBreakdown(
            input_tokens="unavailable",
            output_tokens="unavailable",
            total_tokens="unavailable",
            is_complete=False,
            status="unavailable",
        )

    inp = 0
    out = 0
    if agent_has_nums:
        inp += agent_usage.input_tokens  # type: ignore
        out += agent_usage.output_tokens  # type: ignore
    if diag_has_nums:
        inp += diagnosis_usage.input_tokens  # type: ignore
        out += diagnosis_usage.output_tokens  # type: ignore

    is_complete = agent_usage.is_complete and diagnosis_usage.is_complete
    status = "complete" if is_complete else "incomplete"

    return ModelUsageBreakdown(
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
        is_complete=is_complete,
        status=status,
    )
