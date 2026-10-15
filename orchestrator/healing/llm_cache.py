"""Exact-scope cache keying and safe reconstruction for validated LLM repairs."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit

from orchestrator.client.mock_client import redact_secrets
from orchestrator.healing.contract_patch import derive_contract
from orchestrator.healing.llm_policy import POLICY_VERSION
from orchestrator.models import RepairProposal, RestrictedOperationType, ToolDefinition
from orchestrator.models.classification import ClassificationResult
from orchestrator.models.observation import FailureObservation
from orchestrator.models.proposal import PlannedOperation, PlanningResult, PlanningStatus


CACHE_KEY_VERSION = "repair-cache-key-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def argument_shape(value: Any) -> Any:
    """Represent structure and scalar types without retaining caller values."""
    if isinstance(value, dict):
        return {str(key): argument_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return {"type": "array", "items": [argument_shape(item) for item in value[:8]]}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def safe_provider_origin(origin: str) -> str:
    """Return only scheme/host/port, excluding credentials, paths, and queries."""
    parsed = urlsplit(origin or "")
    if not parsed.scheme or not parsed.hostname:
        return "local-or-unknown"
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def build_repair_cache_key(
    *,
    provider_identity: str,
    provider_origin: str,
    tenant_scope: str,
    snapshot: ToolDefinition,
    observation: FailureObservation,
    classification: ClassificationResult,
    prompt_version: str,
    response_schema_version: str,
    model_provider: str,
    model_name: str,
) -> tuple[str, Dict[str, str], str]:
    """Build a secret-free exact key covering every required isolation dimension."""
    clean_error = redact_secrets({
        "category": classification.category.value,
        "status": observation.http_status,
        "validation_details": observation.validation_details,
        "response_evidence": observation.response_evidence,
        "raw_error_message": observation.raw_error_message,
    })
    evidence = redact_secrets({
        "paths": observation.evidence_paths,
        "details": observation.validation_details,
        "response": observation.response_evidence,
        "message": observation.raw_error_message,
    })
    provider_scope = {
        "identity": provider_identity,
        "origin": safe_provider_origin(provider_origin),
    }
    model_scope = {"provider": model_provider, "model": model_name}
    dimensions = {
        "key_version": CACHE_KEY_VERSION,
        "provider_scope_hash": _hash(provider_scope),
        "endpoint_hash": _hash({"endpoint": observation.endpoint, "method": observation.method.upper()}),
        "tenant_scope_hash": _hash(tenant_scope or "default"),
        "tool_hash": _hash(snapshot.name),
        "contract_hash": _hash({
            "version": snapshot.version,
            "fingerprint": snapshot.fingerprint or snapshot.compute_fingerprint(),
        }),
        "error_hash": _hash(clean_error),
        "argument_shape_hash": _hash(argument_shape(observation.original_arguments)),
        "evidence_hash": _hash(evidence),
        "policy_hash": _hash(POLICY_VERSION),
        "prompt_hash": _hash(prompt_version),
        "response_schema_hash": _hash(response_schema_version),
        "model_configuration_hash": _hash(model_scope),
    }
    return _hash(dimensions), dimensions, dimensions["model_configuration_hash"]


_OPERATION_FIELDS = {
    "operation_type", "source_path", "target_path", "conversion_policy",
    "credential_reference", "value_provenance", "evidence_path", "evidence_summary",
}


def planning_result_from_cached_operations(
    *,
    snapshot: ToolDefinition,
    observation: FailureObservation,
    classification: ClassificationResult,
    operations: Iterable[Dict[str, Any]],
    expected_proposal_fingerprint: str,
    original_interaction_id: str,
) -> PlanningResult:
    """Rebuild declarative cached data; corruption is rejected before validation."""
    planned: list[PlannedOperation] = []
    for raw in operations:
        if not isinstance(raw, dict) or set(raw) - _OPERATION_FIELDS:
            raise ValueError("Cached operation contains an invalid shape or extra fields")
        planned.append(PlannedOperation.model_validate(raw))
    if not planned:
        raise ValueError("Cached repair has no operations")

    base_fingerprint = snapshot.fingerprint or snapshot.compute_fingerprint()
    fingerprint_payload = {
        "tool": snapshot.name,
        "base_version": snapshot.version,
        "base_fingerprint": base_fingerprint,
        "operations": [{
            "type": op.operation_type.value,
            "source": op.source_path,
            "target": op.target_path,
            "conversion": op.conversion_policy,
            "credential_ref": op.credential_reference,
        } for op in planned],
    }
    proposal_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    if proposal_fingerprint != expected_proposal_fingerprint:
        raise ValueError("Cached proposal fingerprint does not match reconstructed operations")

    preview, _ = derive_contract(snapshot, planned)
    preview.status = "proposed_unvalidated"
    proposal = RepairProposal(
        proposal_id=f"prop-cache-{proposal_fingerprint[:12]}",
        tool=snapshot.name,
        base_version=snapshot.version,
        target_version="1.1.0",
        restricted_operations=[RestrictedOperationType(op.operation_type) for op in planned],
        evidence_references={
            "proposal_fingerprint": proposal_fingerprint,
            "observation_fingerprint": observation.fingerprint,
            "source": "validated_cache",
            "original_interaction_id": original_interaction_id,
        },
        transform_spec={
            "operations": [op.model_dump() for op in planned],
            "status": "unvalidated_proposal",
            "source": "validated_cache",
        },
    )
    return PlanningResult(
        status=PlanningStatus.PROPOSED,
        tool_name=snapshot.name,
        base_version=snapshot.version,
        base_fingerprint=base_fingerprint,
        proposal=proposal,
        operations=planned,
        candidate_preview=preview,
        proposal_fingerprint=proposal_fingerprint,
        reasons=["Exact-scope cached declarative patch reconstructed for current validation."],
        required_validation_checks=[
            "verify_caller_argument_immutability", "verify_no_invented_business_values",
            "verify_lossless_type_conversion", "verify_approved_credential_resolution",
            "verify_idempotent_retry_safety",
        ],
        evidence_references=proposal.evidence_references,
        retry_safety=classification.retry_safety,
        source="validated_cache",
    )
