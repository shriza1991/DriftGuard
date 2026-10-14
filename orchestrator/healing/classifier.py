"""Deterministic failure classifier and retry safety evaluator.

Adheres strictly to Member 2 safety invariants:
- Pure deterministic rules based strictly on observable runtime evidence.
- Zero reliance on out-of-band scenario names, mutation admin state, or hidden code.
- Explicit distinction between execution-layer failure category and semantic classification.
- Missing field/type error alone cannot establish a rename or semantic correspondence.
- Independent modeling of retry safety and diagnosis eligibility:
  uncertain writes are NEVER safe to retry, regardless of diagnosis eligibility.
- Contradictory, malformed, or insufficient evidence safely degrades to AMBIGUOUS.
"""
from typing import Dict, Any, List, Optional
import re

from orchestrator.models.observation import ExecutionPhase, FailureObservation
from orchestrator.models.classification import (
    SemanticFailureCategory,
    RetrySafety,
    ClassificationResult,
)


KNOWN_BUSINESS_ERROR_CODES = {
    "TICKET_CLOSED",
    "TICKET_ALREADY_ASSIGNED",
    "TICKET_LOCKED",
    "USER_INACTIVE",
    "INVALID_STATE_TRANSITION",
    "BUSINESS_RULE_VIOLATION",
    "RESOURCE_EXHAUSTED_BUSINESS",
    "TICKET_NOT_FOUND",
    "USER_NOT_FOUND",
}

KNOWN_DRIFT_ERROR_CODES = {
    "SCHEMA_MISMATCH",
    "TYPE_MISMATCH",
    "NESTING_MISMATCH",
    "UNEXPECTED_FIELD",
    "MISSING_REQUIRED_FIELD",
    "COMBINED_SCHEMA_MISMATCH",
    "REQUIRED_HEADER_MISSING",
    "HEADER_SCHEMA_MISMATCH",
}


def _is_idempotent_method(method: str) -> bool:
    """Returns True if the HTTP method is semantically idempotent."""
    return method.upper() in ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"]


def _evaluate_retry_safety(
    observation: FailureObservation,
    category: SemanticFailureCategory,
    diagnosis_eligible: bool,
) -> RetrySafety:
    """Evaluates retry safety independently of classification and diagnosis eligibility.
    
    CRITICAL INVARIANT:
    - Uncertain write outcomes (transport drop or post-write parsing failure) are NEVER safe to retry.
    - Idempotency-Key headers alone do NOT prove downstream server-side deduplication.
    - Diagnosis eligibility does NOT authorize repeating an unverified write.
    """
    is_idemp = _is_idempotent_method(observation.method)

    # 1. Uncertain write safety check (highest precedence)
    if observation.uncertain_write:
        return RetrySafety(
            safe_to_retry=False,
            reason="Uncertain write outcome: downstream write state is uncertain; re-dispatching risks creating duplicate mutations or state corruption.",
            uncertain_write=True,
            idempotent_method=is_idemp,
            requires_input_adaptation=False,
        )

    # 2. Pre-execution failures (rejected before HTTP dispatch)
    if observation.execution_phase in [
        ExecutionPhase.PRE_VALIDATION,
        ExecutionPhase.CREDENTIAL_RESOLUTION,
        ExecutionPhase.REQUEST_MAPPING,
    ]:
        return RetrySafety(
            safe_to_retry=False,
            reason="Operation failed before HTTP dispatch; repeating identical call without adapting inputs or credentials will fail.",
            uncertain_write=False,
            idempotent_method=is_idemp,
            requires_input_adaptation=True,
        )

    # 3. Post-write response parsing failure
    if observation.execution_phase == ExecutionPhase.RESPONSE_PARSING and observation.write_attempted:
        return RetrySafety(
            safe_to_retry=False,
            reason="Uncertain write outcome: write was dispatched and processed, but response parsing failed. Repeating the write is strictly forbidden.",
            uncertain_write=True,
            idempotent_method=is_idemp,
            requires_input_adaptation=False,
        )

    # 4. Authentication / Authorization requirement
    if observation.http_status in [401, 403]:
        return RetrySafety(
            safe_to_retry=False,
            reason="Authentication/authorization failure: caller lacks required credentials or permissions. Credentials cannot be fabricated and permissions cannot be bypassed; retry requires valid authentication credentials.",
            uncertain_write=observation.uncertain_write,
            idempotent_method=is_idemp,
            requires_input_adaptation=True,
        )

    # 5. Downstream schema drift on writes
    if category == SemanticFailureCategory.SCHEMA_DRIFT and observation.write_attempted:
        return RetrySafety(
            safe_to_retry=False,
            reason="Downstream server rejected the request schema. Repeating identical payload will deterministically fail; input adaptation is required.",
            uncertain_write=False,
            idempotent_method=is_idemp,
            requires_input_adaptation=True,
        )

    # 6. Transient service failures on idempotent reads
    if category == SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE:
        if is_idemp:
            return RetrySafety(
                safe_to_retry=True,
                reason="Idempotent read operation experienced transient failure; safe to retry after backoff.",
                uncertain_write=False,
                idempotent_method=True,
                requires_input_adaptation=False,
            )
        else:
            return RetrySafety(
                safe_to_retry=False,
                reason="Non-idempotent mutation write experienced transient error; requires state verification before retry.",
                uncertain_write=observation.write_attempted,
                idempotent_method=False,
                requires_input_adaptation=False,
            )

    # 7. Default for business rule, auth, invalid input, or ambiguous
    return RetrySafety(
        safe_to_retry=False,
        reason=f"Operation cannot be retried safely under category '{category.value}'. Repeating call requires input or schema adaptation.",
        uncertain_write=observation.uncertain_write,
        idempotent_method=is_idemp,
        requires_input_adaptation=(category in [SemanticFailureCategory.INVALID_INPUT, SemanticFailureCategory.SCHEMA_DRIFT]),
    )


def classify_failure(observation: FailureObservation) -> ClassificationResult:
    """Deterministically classifies runtime failures into semantic categories based strictly on observed evidence."""
    try:
        return _classify_internal(observation)
    except Exception as exc:
        # Graceful degradation on unexpected processing errors
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AMBIGUOUS, False)
        return ClassificationResult(
            category=SemanticFailureCategory.AMBIGUOUS,
            evidence_references={"exception": str(exc)},
            reason="Classifier encountered internal exception evaluating evidence; degrading safely to ambiguous.",
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason=f"Classifier error: {str(exc)}",
        )


def _classify_internal(observation: FailureObservation) -> ClassificationResult:
    status = observation.http_status
    val_details = observation.validation_details or {}
    resp_ev = observation.response_evidence or {}
    evidence_refs: Dict[str, Any] = {}

    # Extract body dictionary whether nested under "body" or top-level
    body_dict: Dict[str, Any] = {}
    if isinstance(resp_ev.get("body"), dict):
        body_dict = resp_ev["body"]
    elif isinstance(resp_ev, dict):
        body_dict = resp_ev

    err_obj = body_dict.get("error") if isinstance(body_dict.get("error"), dict) else body_dict

    # -------------------------------------------------------------------------
    # 1. Pre-execution Local Validation Phase
    # -------------------------------------------------------------------------
    if observation.execution_phase == ExecutionPhase.PRE_VALIDATION:
        evidence_refs["validation_errors"] = val_details
        reason = (
            "Local argument schema validation rejected caller arguments prior to HTTP dispatch. "
            "Indicates invalid input from the caller, not downstream schema drift."
        )
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.INVALID_INPUT, False)
        return ClassificationResult(
            category=SemanticFailureCategory.INVALID_INPUT,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason=None,
        )

    # -------------------------------------------------------------------------
    # 2. Pre-execution Credential Resolution Phase
    # -------------------------------------------------------------------------
    if observation.execution_phase == ExecutionPhase.CREDENTIAL_RESOLUTION:
        evidence_refs["credential_reference"] = val_details.get("credential_reference")
        reason = (
            "Required credential reference could not be resolved by the local credential resolver prior to HTTP dispatch."
        )
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AUTHENTICATION_AUTHORIZATION, False)
        return ClassificationResult(
            category=SemanticFailureCategory.AUTHENTICATION_AUTHORIZATION,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason=None,
        )

    # -------------------------------------------------------------------------
    # 3. Pre-execution Request Mapping Phase
    # -------------------------------------------------------------------------
    if observation.execution_phase == ExecutionPhase.REQUEST_MAPPING:
        evidence_refs["mapping_details"] = val_details
        reason = "Tool request mapping specification contains unsupported keys or failed variable expansion."
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.INVALID_INPUT, False)
        return ClassificationResult(
            category=SemanticFailureCategory.INVALID_INPUT,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason=None,
        )

    # -------------------------------------------------------------------------
    # 4. Response Parsing Phase
    # -------------------------------------------------------------------------
    if observation.execution_phase == ExecutionPhase.RESPONSE_PARSING:
        evidence_refs["response_evidence"] = resp_ev
        evidence_refs["validation_details"] = val_details
        # If write occurred, response parsing failure suggests response-contract drift
        reason = (
            "Downstream HTTP endpoint returned success (2xx), but the response body failed JSON parsing "
            "or violated declared response mapping expectations. Identifies potential response-contract drift candidate."
        )
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.SCHEMA_DRIFT, True)
        return ClassificationResult(
            category=SemanticFailureCategory.SCHEMA_DRIFT,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=True,
            retry_safety=safety,
            uncertainty_reason=None,
        )

    # -------------------------------------------------------------------------
    # 5. Transient Infrastructure Failures (Transport drops, 429, 5xx)
    # -------------------------------------------------------------------------
    if (
        status in [429, 500, 502, 503, 504]
        or status is None
        or (status == 0 and observation.execution_phase == ExecutionPhase.HTTP_DISPATCH)
    ):
        evidence_refs["http_status"] = status
        evidence_refs["transport_error"] = val_details.get("transport_error") or resp_ev.get("error")
        reason = (
            f"Transient service or infrastructure failure (HTTP status: {status or 'network_drop'}). "
            "No contract drift indicated."
        )
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE, False)
        return ClassificationResult(
            category=SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason=None,
        )

    # -------------------------------------------------------------------------
    # 6. HTTP 401 / 403 Authentication & Authorization
    # -------------------------------------------------------------------------
    if status in [401, 403]:
        # Check if structured response evidence explicitly indicates a newly required header or header schema drift
        has_header_drift_evidence = False
        header_evidence_details = {}

        details_obj = err_obj.get("details") or body_dict.get("details") or val_details.get("details") or {}
        hint_text = str(err_obj.get("hint") or body_dict.get("hint") or val_details.get("hint") or "")
        msg_text = str(err_obj.get("message") or body_dict.get("message") or val_details.get("message") or "")
        code_text = str(err_obj.get("code") or err_obj.get("category") or body_dict.get("code") or "").upper()

        if code_text in ["MISSING_HEADER", "HEADER_SCHEMA_MISMATCH", "REQUIRED_HEADER_MISSING"]:
            has_header_drift_evidence = True
            header_evidence_details = details_obj or {"code": code_text}

        if isinstance(details_obj, dict):
            if any(k in details_obj for k in ["required_headers", "missing_headers", "required_header", "missing_header"]):
                has_header_drift_evidence = True
                header_evidence_details = details_obj

        if "header" in hint_text.lower() or "header" in msg_text.lower():
            if any(k in msg_text.lower() or k in hint_text.lower() for k in ["required", "missing", "migrated", "expected"]):
                has_header_drift_evidence = True

        if has_header_drift_evidence:
            evidence_refs["http_status"] = status
            evidence_refs["header_drift_details"] = header_evidence_details or hint_text or msg_text
            reason = (
                f"HTTP {status} with explicit structured evidence indicating a newly required or modified request header. "
                "Identifies header drift candidate. (Note: does not imply credentials exist or permissions can be bypassed)."
            )
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.SCHEMA_DRIFT, True)
            return ClassificationResult(
                category=SemanticFailureCategory.SCHEMA_DRIFT,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=True,
                retry_safety=safety,
                uncertainty_reason=None,
            )
        else:
            evidence_refs["http_status"] = status
            evidence_refs["response"] = resp_ev
            reason = f"HTTP {status} indicates standard authentication failure or permission denial without contract drift evidence."
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AUTHENTICATION_AUTHORIZATION, False)
            return ClassificationResult(
                category=SemanticFailureCategory.AUTHENTICATION_AUTHORIZATION,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=False,
                retry_safety=safety,
                uncertainty_reason=None,
            )

    # -------------------------------------------------------------------------
    # 7. HTTP 404 Missing Resource vs Interface Drift
    # -------------------------------------------------------------------------
    if status == 404:
        code_str = str(err_obj.get("code") or err_obj.get("category") or body_dict.get("code") or val_details.get("code") or "").upper()
        msg_str = str(err_obj.get("message") or body_dict.get("message") or val_details.get("message") or "")

        # Check for documented missing business resource
        is_known_resource_missing = (
            code_str in ["TICKET_NOT_FOUND", "USER_NOT_FOUND", "RESOURCE_NOT_FOUND"]
            or bool(re.search(r"\b(ticket|user)\s+\d+\s+not\s+found\b", msg_str, re.IGNORECASE))
            or bool(re.search(r"\buser\s+['\"][^'\"]+['\"]\s+not\s+found\b", msg_str, re.IGNORECASE))
        )

        if is_known_resource_missing:
            evidence_refs["http_status"] = 404
            evidence_refs["resource_error"] = msg_str or code_str
            reason = "Documented business entity not found in downstream service (business rejection, not endpoint drift)."
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.BUSINESS_RULE_REJECTION, False)
            return ClassificationResult(
                category=SemanticFailureCategory.BUSINESS_RULE_REJECTION,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=False,
                retry_safety=safety,
                uncertainty_reason=None,
            )
        else:
            evidence_refs["http_status"] = 404
            evidence_refs["raw_response"] = resp_ev
            reason = "HTTP 404 received without structured resource error details; cannot distinguish missing resource from route change."
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AMBIGUOUS, False)
            return ClassificationResult(
                category=SemanticFailureCategory.AMBIGUOUS,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=False,
                retry_safety=safety,
                uncertainty_reason="HTTP 404 does not distinguish a non-existent business entity from a changed endpoint URL.",
            )

    # -------------------------------------------------------------------------
    # 8. HTTP 400 / 422 (Schema Drift vs Business Rejection vs Ambiguous)
    # -------------------------------------------------------------------------
    if status in [400, 422]:
        code_val = str(err_obj.get("code") or err_obj.get("category") or body_dict.get("code") or body_dict.get("category") or val_details.get("error_code") or "").upper()
        details_dict = err_obj.get("details") or body_dict.get("details") or val_details.get("details") or {}
        hint_val = err_obj.get("hint") or err_obj.get("migration_hint") or body_dict.get("hint") or body_dict.get("migration_hint") or val_details.get("migration_hint")
        msg_val = str(err_obj.get("message") or body_dict.get("message") or val_details.get("message") or "")

        # A. Check for Contradictory Evidence First
        # e.g. status 400 but body claims success=True or valid outcome
        has_contradiction = (
            body_dict.get("success") is True
            or err_obj.get("success") is True
            or (isinstance(details_dict, dict) and details_dict.get("success") is True)
        )
        if has_contradiction:
            evidence_refs["http_status"] = status
            evidence_refs["contradiction"] = "HTTP 4xx status but body indicates success: true"
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AMBIGUOUS, False)
            return ClassificationResult(
                category=SemanticFailureCategory.AMBIGUOUS,
                evidence_references=evidence_refs,
                reason="Downstream response contains contradictory status and body evidence.",
                diagnosis_eligible=False,
                retry_safety=safety,
                uncertainty_reason="Contradictory evidence: HTTP error status code contradicts response body success payload.",
            )

        # B. Check for Documented Business Rule Rejection
        if code_val in KNOWN_BUSINESS_ERROR_CODES:
            evidence_refs["code"] = code_val
            evidence_refs["message"] = msg_val
            reason = f"Downstream business domain logic rejected the operation with documented business rule: '{code_val}'."
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.BUSINESS_RULE_REJECTION, False)
            return ClassificationResult(
                category=SemanticFailureCategory.BUSINESS_RULE_REJECTION,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=False,
                retry_safety=safety,
                uncertainty_reason=None,
            )

        # C. Check for Explicit Contract Drift Evidence
        has_explicit_drift_evidence = False
        drift_reasons: List[str] = []

        if code_val in KNOWN_DRIFT_ERROR_CODES:
            has_explicit_drift_evidence = True
            drift_reasons.append(f"error_code='{code_val}'")

        if isinstance(details_dict, dict) and any(
            k in details_dict for k in ["expected_schema", "unexpected", "missing", "received_payload", "expected_type"]
        ):
            has_explicit_drift_evidence = True
            drift_reasons.append("structured_schema_details_present")

        if hint_val:
            has_explicit_drift_evidence = True
            drift_reasons.append("migration_hint_present")

        if has_explicit_drift_evidence:
            evidence_refs["code"] = code_val
            evidence_refs["details"] = details_dict
            evidence_refs["migration_hint"] = hint_val
            evidence_refs["message"] = msg_val

            # CRITICAL SAFETY INVARIANT:
            # A missing field or type error alone CANNOT establish a rename or semantic correspondence!
            reason = (
                f"Structured downstream validation evidence ({', '.join(drift_reasons)}) indicates API schema drift / schema mismatch. "
                "Candidate identified for repair diagnosis. "
                "(Note: Observed missing field or type error alone does not prove field rename or correspondence; "
                "semantic correspondence requires diagnostic validation)."
            )
            safety = _evaluate_retry_safety(observation, SemanticFailureCategory.SCHEMA_DRIFT, True)
            return ClassificationResult(
                category=SemanticFailureCategory.SCHEMA_DRIFT,
                evidence_references=evidence_refs,
                reason=reason,
                diagnosis_eligible=True,
                retry_safety=safety,
                uncertainty_reason=None,
            )

        # D. Status-only 400/422 without structured details
        evidence_refs["http_status"] = status
        evidence_refs["raw_body"] = resp_ev
        reason = (
            f"HTTP {status} returned without structured schema details, migration hints, or recognized business error codes. "
            "Evidence is insufficient to distinguish contract drift from invalid caller input."
        )
        safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AMBIGUOUS, False)
        return ClassificationResult(
            category=SemanticFailureCategory.AMBIGUOUS,
            evidence_references=evidence_refs,
            reason=reason,
            diagnosis_eligible=False,
            retry_safety=safety,
            uncertainty_reason="Status code alone without structured schema details is insufficient for classification.",
        )

    # -------------------------------------------------------------------------
    # 9. All other HTTP statuses / Unhandled Responses -> Ambiguous
    # -------------------------------------------------------------------------
    evidence_refs["http_status"] = status
    evidence_refs["raw_response"] = resp_ev
    reason = f"Unhandled or unrecognized HTTP status {status}; evidence is insufficient for reliable classification."
    safety = _evaluate_retry_safety(observation, SemanticFailureCategory.AMBIGUOUS, False)
    return ClassificationResult(
        category=SemanticFailureCategory.AMBIGUOUS,
        evidence_references=evidence_refs,
        reason=reason,
        diagnosis_eligible=False,
        retry_safety=safety,
        uncertainty_reason=f"Unrecognized response status: {status}",
    )
