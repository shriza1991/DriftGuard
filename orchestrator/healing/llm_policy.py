"""Central, fail-closed policy for repair-model invocation decisions."""
from __future__ import annotations

from enum import Enum
import re

from pydantic import BaseModel

from orchestrator.models.classification import ClassificationResult, SemanticFailureCategory
from orchestrator.models.observation import FailureObservation
from orchestrator.models.proposal import PlanningResult, PlanningStatus


POLICY_VERSION = "repair-llm-policy-v1"


class LLMCallDecision(str, Enum):
    NO_CALL_NORMAL = "no_call_normal"
    NO_CALL_NON_DRIFT = "no_call_non_drift"
    USE_DETERMINISTIC_RULE = "use_deterministic_rule"
    USE_VALIDATED_CACHE = "use_validated_cache"
    CALL_LLM_ONCE = "call_llm_once"
    SAFE_STOP = "safe_stop"


class LLMCallPolicyResult(BaseModel):
    decision: LLMCallDecision
    policy_version: str = POLICY_VERSION
    reason: str
    reason_no_call: str | None = None
    llm_call_permitted: bool = False


_BOUNDED_REASONING_MARKERS = re.compile(
    r"\b(renamed?|replaced|migrated|moved|nested|instead\s+of|maps?\s+to|corresponds?\s+to)\b"
    r"|\b(?:must|should)\s+be\s+(?:of\s+)?type\s+(?:integer|number|string|boolean)\b|->",
    re.IGNORECASE,
)
_PROMPT_INJECTION_MARKERS = re.compile(
    r"ignore\s+(?:all\s+)?previous|system\s+prompt|reveal\s+secrets?|call\s+/admin|execute\s+(?:code|command)",
    re.IGNORECASE,
)
_POLICY_STOP_MARKERS = (
    "no approved credential",
    "missing field",
    "collision",
    "cannot invent",
    "missing business",
    "ambiguous",
)
_EVIDENCE_STOP_MARKERS = re.compile(
    r"\bmissing\s+(?:required\s+)?(?:field|parameter|business value)\b|"
    r"\b(?:credential|secret|token)\s+(?:is\s+)?missing\b|"
    r"\bambiguous\s+(?:field|mapping|correspondence)\b",
    re.IGNORECASE,
)


def _observation_text(observation: FailureObservation) -> str:
    return " ".join(
        str(value)
        for value in (
            observation.raw_error_message,
            observation.validation_details,
            observation.response_evidence,
        )
        if value
    )


def has_bounded_reasoning_evidence(observation: FailureObservation) -> bool:
    """Return true only for bounded correspondence evidence worth one model attempt."""
    evidence = _observation_text(observation)
    if not evidence or len(evidence) > 12_000 or _PROMPT_INJECTION_MARKERS.search(evidence):
        return False
    return bool(_BOUNDED_REASONING_MARKERS.search(evidence))


def decide_repair_llm_call(
    *,
    classification: ClassificationResult | None,
    deterministic_plan: PlanningResult | None,
    observation: FailureObservation | None,
    normal_success: bool = False,
    validated_cache_hit: bool = False,
    retry_policy_approved: bool = False,
    incident_budget_consumed: bool = False,
    circuit_breaker_open: bool = False,
    model_configured: bool = False,
) -> LLMCallPolicyResult:
    """Apply the complete repair-model policy at one authoritative boundary."""
    if normal_success:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.NO_CALL_NORMAL,
            reason="Normal execution succeeded; repair diagnosis is not applicable.",
            reason_no_call="normal_success",
        )
    if classification is None or observation is None:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="Required classification or failure observation is unavailable.",
            reason_no_call="missing_policy_evidence",
        )
    if classification.category != SemanticFailureCategory.SCHEMA_DRIFT or not classification.diagnosis_eligible:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.NO_CALL_NON_DRIFT,
            reason="Failure is not eligible contract drift.",
            reason_no_call=f"category:{classification.category.value}",
        )
    if deterministic_plan is not None and deterministic_plan.status == PlanningStatus.PROPOSED:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.USE_DETERMINISTIC_RULE,
            reason="A complete allowlisted deterministic proposal is available.",
            reason_no_call="deterministic_proposal_complete",
        )
    if validated_cache_hit:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.USE_VALIDATED_CACHE,
            reason="An exact-scope validated cache candidate is available for revalidation.",
            reason_no_call="validated_cache_hit",
        )
    if observation.uncertain_write or not retry_policy_approved:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="The operation does not have an approved adapted-retry policy.",
            reason_no_call="retry_policy_not_approved",
        )
    reasons = " ".join(deterministic_plan.reasons if deterministic_plan else []).lower()
    if any(marker in reasons for marker in _POLICY_STOP_MARKERS) or _EVIDENCE_STOP_MARKERS.search(
        _observation_text(observation)
    ):
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="Deterministic policy already requires an authoritative stop.",
            reason_no_call="deterministic_policy_stop",
        )
    if not has_bounded_reasoning_evidence(observation):
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="Evidence is insufficient or unsafe for bounded model reasoning.",
            reason_no_call="insufficient_bounded_evidence",
        )
    if incident_budget_consumed:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="The incident repair-model budget has already been consumed.",
            reason_no_call="incident_budget_consumed",
        )
    if circuit_breaker_open:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="The repair-model circuit breaker is open.",
            reason_no_call="circuit_breaker_open",
        )
    if not model_configured:
        return LLMCallPolicyResult(
            decision=LLMCallDecision.SAFE_STOP,
            reason="Repair-model configuration is unavailable.",
            reason_no_call="model_not_configured",
        )
    return LLMCallPolicyResult(
        decision=LLMCallDecision.CALL_LLM_ONCE,
        reason="Eligible drift requires one bounded reasoning attempt.",
        llm_call_permitted=True,
    )
