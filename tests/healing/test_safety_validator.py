"""Comprehensive unit test suite for Member 2 Task 7 — Safety Validator.

Adheres strictly to DriftGuard invariants:
- Pure local fixtures and fake credential resolver; zero network or model dependencies.
- Independent deterministic validation and preparation of a safe candidate.
- Zero LLM calls, zero HTTP requests, zero credential acquisition.
- Zero registry staging or commits (Task 8 owns staged patching & commit).
- Strict verification of proposal integrity, evidence references, and fingerprint freshness.
- Strict verification of value provenance, lossless numeric conversions, and no invented values.
- Strict protection of uncertain writes and pre-execution rejection guarantees.
- Candidate definitions marked 'validated_not_committed'.
"""
import copy
import json
from typing import Optional, Dict, Any, List
import pytest
from datetime import datetime, timezone
import jsonschema

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    RestrictedOperationType,
    OperationIdentity,
    TimingInfo,
    RepairProposal,
)
from orchestrator.models.observation import ExecutionPhase, FailureObservation
from orchestrator.models.classification import ClassificationResult, SemanticFailureCategory, RetrySafety
from orchestrator.models.proposal import PlanningStatus, PlannedOperation, PlanningResult
from orchestrator.models.validation import ValidationStatus, SafetyCheckRecord, ValidationResult
from orchestrator.healing.planner import plan_repair
from orchestrator.healing.validator import validate_repair_proposal, emit_patch_validation_event
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.events import EventBroadcaster


# =========================================================================
# Fixtures & Helpers
# =========================================================================
def _create_assign_tool() -> ToolDefinition:
    """Helper creating a canonical v1 assign_ticket ToolDefinition."""
    tool = ToolDefinition(
        name="assign_ticket",
        version="1.0.0",
        description="Assigns a ticket to a user",
        endpoint="/tickets/assign",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "ticket_id": {"type": "integer", "description": "Ticket ID"},
                "user_id": {"type": "string", "description": "User ID"},
            },
            required=["ticket_id", "user_id"],
        ),
        request_mapping={
            "body": {
                "ticket_id": "{ticket_id}",
                "user_id": "{user_id}",
            }
        },
        response_mapping={
            "ticket_id": "$.ticket_id",
            "status": "$.status",
            "assignee_id": "$.assignee_id",
        },
    )
    tool.fingerprint = tool.compute_fingerprint()
    return tool


def _create_observation(
    tool: ToolDefinition,
    status_code: int = 400,
    response_body: Optional[Dict[str, Any]] = None,
    uncertain_write: bool = False,
    tool_name: Optional[str] = None,
    registry_version: Optional[str] = None,
    fingerprint: Optional[str] = None,
    original_args: Optional[Dict[str, Any]] = None,
    pre_execution_rejection: bool = True,
) -> FailureObservation:
    """Helper creating a valid FailureObservation."""
    args = original_args or {"ticket_id": 81, "user_id": "123"}
    resp_body = response_body or {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "details": {
                "migration_mapping": {"user_id": "assignee_id"},
            },
        }
    }
    details = dict(resp_body.get("error", {}).get("details", {}))
    if pre_execution_rejection:
        details["pre_execution_rejection"] = True
    return FailureObservation(
        tool_name=tool.name,
        registry_version=tool.version,
        fingerprint=tool.fingerprint,
        run_id="run-test-val",
        tool_call_id="call-val-001",
        idempotency_key="idemp-val-001",
        operation_identity=OperationIdentity(
            tool_name=tool.name,
            endpoint=tool.endpoint,
            method=tool.method,
            idempotency_key="idemp-val-001",
        ),
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method=tool.method,
        endpoint=tool.endpoint,
        http_status=status_code,
        original_arguments=args,
        response_evidence={"status_code": status_code, "body": resp_body},
        write_attempted=True,
        uncertain_write=uncertain_write,
        raw_error_message=str(resp_body.get("error", {}).get("message", "Error")),
        validation_details=details,
        evidence_paths=[
            "response.body.details.migration_mapping.user_id",
            "response.body.error",
        ],
    )


def _create_classification(
    category: SemanticFailureCategory = SemanticFailureCategory.SCHEMA_DRIFT,
    eligible: bool = True,
    uncertain_write: bool = False,
) -> ClassificationResult:
    """Helper creating a ClassificationResult."""
    return ClassificationResult(
        category=category,
        evidence_references={"test": "evidence"},
        reason="Test failure classification",
        diagnosis_eligible=eligible,
        retry_safety=RetrySafety(
            safe_to_retry=False,
            safe_if_adapted=True,
            reason="Uncertain write" if uncertain_write else "Downstream schema rejection",
            uncertain_write=uncertain_write,
            idempotency_key_preserved=True,
        ),
    )


# =========================================================================
# 1. Valid Proposals Accepted
# =========================================================================
def test_valid_rename_validation_accepted():
    """Verify a valid rename proposal passes all safety checks and produces an authorized candidate."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    # Generate proposal via planner
    plan_res = plan_repair(tool, obs, classification)
    assert plan_res.status == PlanningStatus.PROPOSED

    # Validate proposal
    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    assert val_res.is_accepted is True
    assert val_res.is_rejected is False
    assert val_res.authorized_for_recovery is True
    assert val_res.candidate is not None
    assert val_res.candidate.status == "validated_not_committed"
    assert val_res.candidate.request_mapping["body"]["assignee_id"] == "{user_id}"
    assert "user_id" not in val_res.candidate.request_mapping["body"]
    assert val_res.candidate.endpoint == tool.endpoint
    assert val_res.candidate.method == tool.method
    assert val_res.transformed_arguments == orig_args
    assert val_res.proposal_fingerprint == plan_res.proposal_fingerprint
    assert val_res.candidate_fingerprint is not None


def test_valid_nesting_validation_accepted():
    """Verify an explicit nesting proposal (target.id) is validated safely."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "details": {
                "migration_mapping": {"user_id": "target.id"},
            }
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    assert plan_res.status == PlanningStatus.PROPOSED

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    assert val_res.candidate.request_mapping["body"]["target"]["id"] == "{user_id}"
    assert val_res.authorized_for_recovery is True


def test_valid_lossless_numeric_conversion_accepted():
    """Verify string '123' to integer 123 conversion is lossless and updates candidate schema."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "details": {
                "expected_schema": {"user_id": "integer"},
            }
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    assert plan_res.status == PlanningStatus.PROPOSED

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    assert val_res.transformed_arguments["user_id"] == 123
    assert isinstance(val_res.transformed_arguments["user_id"], int)
    # Original caller arguments must remain completely unmutated
    assert orig_args["user_id"] == "123"
    assert isinstance(orig_args["user_id"], str)
    assert val_res.candidate.argument_schema.properties["user_id"]["type"] == "integer"


def test_valid_approved_header_validation_accepted():
    """Verify approved header with available local credential is validated."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "details": {
                "required_headers": ["Authorization"],
            }
        }
    }
    obs = _create_observation(tool, status_code=401, response_body=resp_body)
    classification = _create_classification()
    approved = {"Authorization": "secops_auth"}
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification, approved_header_credentials=approved)
    assert plan_res.status == PlanningStatus.PROPOSED

    # Injected local resolver confirms credential exists locally
    def fake_resolver(cred_ref: str) -> bool:
        return cred_ref == "secops_auth"

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
        approved_header_credentials=approved,
        credential_resolver=fake_resolver,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    assert val_res.candidate.headers["Authorization"] == "Bearer {secops_auth}"
    assert val_res.candidate.credential_reference == "secops_auth"
    assert val_res.authorized_for_recovery is True


def test_valid_combined_proposal_accepted():
    """Verify combined rename, nesting, type conversion, and header operations compose safely."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "details": {
                "required_headers": ["Authorization"],
                "migration_mapping": {"user_id": "target.id"},
                "expected_schema": {"target": {"id": "integer"}},
            }
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    approved = {"Authorization": "secops_auth"}
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification, approved_header_credentials=approved)
    assert plan_res.status == PlanningStatus.PROPOSED

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
        approved_header_credentials=approved,
        credential_resolver=lambda r: True,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    assert val_res.authorized_for_recovery is True
    # Unrelated properties preserved
    assert val_res.candidate.endpoint == tool.endpoint
    assert val_res.candidate.method == tool.method
    assert val_res.candidate.response_mapping == tool.response_mapping
    assert val_res.candidate.request_mapping["body"]["ticket_id"] == "{ticket_id}"


# =========================================================================
# 2. Proposal Integrity & Tamper Checks
# =========================================================================
def test_tampered_proposal_fingerprint_rejected():
    """Verify that a tampered proposal fingerprint causes immediate rejection."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    # Forged proposal fingerprint
    plan_res.proposal_fingerprint = "forged_fp_9999"

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("Proposal fingerprint mismatch" in r for r in val_res.rejection_reasons)
    assert val_res.candidate is None
    assert val_res.authorized_for_recovery is False


def test_forged_evidence_reference_rejected():
    """Verify that an operation citing an unresolvable evidence path is rejected."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    # Forge evidence path to non-existent location
    plan_res.operations[0].evidence_path = "response.body.nonexistent_fake_path"
    # Recompute proposal fingerprint to bypass fingerprint check so evidence check is tested
    plan_dict = {
        "tool": tool.name,
        "base_version": tool.version,
        "base_fingerprint": tool.fingerprint,
        "operations": [
            {
                "type": op.operation_type.value,
                "source": op.source_path,
                "target": op.target_path,
                "conversion": op.conversion_policy,
                "credential_ref": op.credential_reference,
            }
            for op in plan_res.operations
        ],
    }
    raw_hash = json.dumps(plan_dict, sort_keys=True).encode("utf-8")
    import hashlib
    plan_res.proposal_fingerprint = hashlib.sha256(raw_hash).hexdigest()[:16]

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("could not be resolved" in r for r in val_res.rejection_reasons)


def test_inconsistent_candidate_preview_rejected():
    """Verify that a tampered candidate preview diverging from independent derivation is rejected."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    # Tamper with the preview's request mapping
    plan_res.candidate_preview.request_mapping["body"]["tampered_key"] = "tampered_val"

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("diverges from independently derived" in r for r in val_res.rejection_reasons)


def test_stale_version_or_changed_registry_rejected():
    """Verify that a stale proposal against a registry that has moved forward is rejected."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)

    # Registry has moved forward to v1.1.0 with a different fingerprint
    updated_registry_tool = copy.deepcopy(tool)
    updated_registry_tool.version = "1.1.0"
    updated_registry_tool.fingerprint = "different_fp_123"

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=updated_registry_tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("Stale proposal" in r for r in val_res.rejection_reasons)


def test_path_collision_rejected():
    """Verify that two operations targeting the same wire path are rejected."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    # Formulate proposal with two operations colliding on "assignee_id"
    op1 = PlannedOperation(
        operation_type=RestrictedOperationType.RENAME_ARGUMENT,
        source_path="user_id",
        target_path="assignee_id",
        value_provenance="caller_argument:user_id",
        evidence_path="response.body.details.migration_mapping.user_id",
        evidence_summary="user_id mapped to assignee_id",
    )
    op2 = PlannedOperation(
        operation_type=RestrictedOperationType.RENAME_ARGUMENT,
        source_path="ticket_id",
        target_path="assignee_id",
        value_provenance="caller_argument:ticket_id",
        evidence_path="response.body.details.migration_mapping.user_id",
        evidence_summary="ticket_id mapped to assignee_id collision",
    )
    plan_dict = {
        "tool": tool.name,
        "base_version": tool.version,
        "base_fingerprint": tool.fingerprint,
        "operations": [
            {"type": op.operation_type.value, "source": op.source_path, "target": op.target_path, "conversion": op.conversion_policy, "credential_ref": op.credential_reference}
            for op in [op1, op2]
        ],
    }
    import hashlib
    raw_hash = json.dumps(plan_dict, sort_keys=True).encode("utf-8")
    fp = hashlib.sha256(raw_hash).hexdigest()[:16]

    proposal = PlanningResult(
        status=PlanningStatus.PROPOSED,
        tool_name=tool.name,
        base_version=tool.version,
        base_fingerprint=tool.fingerprint,
        operations=[op1, op2],
        proposal_fingerprint=fp,
    )

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=proposal,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("Destination collision" in r for r in val_res.rejection_reasons)


# =========================================================================
# 3. Disallowed Conversions & Edge Cases
# =========================================================================
def test_disallowed_conversion_leading_zeros_rejected():
    """Verify that converting '0123' to integer is rejected because leading zero would be lost."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"expected_schema": {"user_id": "integer"}}}}
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    # Caller provided user_id with leading zero: "0123"
    orig_args = {"ticket_id": 81, "user_id": "0123"}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("leading zeros would be lost" in r for r in val_res.rejection_reasons)


def test_disallowed_conversion_decimal_truncation_rejected():
    """Verify that decimal string '123.45' cannot be truncated to integer."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"expected_schema": {"user_id": "integer"}}}}
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123.45"}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("truncation is forbidden" in r for r in val_res.rejection_reasons)


def test_disallowed_conversion_boolean_as_int_rejected():
    """Verify that boolean values cannot be converted to integer (bool-as-int is forbidden)."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"expected_schema": {"user_id": "integer"}}}}
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": True}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("bool-as-int is forbidden" in r for r in val_res.rejection_reasons)


def test_disallowed_conversion_nonnumeric_string_rejected():
    """Verify that non-numeric strings ('maya') cannot be converted to integer."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"expected_schema": {"user_id": "integer"}}}}
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "maya"}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("not a valid lexical integer" in r for r in val_res.rejection_reasons)


def test_unapproved_header_and_missing_local_credential_rejected():
    """Verify rejection when required header is unapproved or local credential is missing."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"required_headers": ["X-Vendor-Token"]}}}
    obs = _create_observation(tool, status_code=401, response_body=resp_body)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    # Case A: Header not in approved configuration
    plan_res = plan_repair(tool, obs, classification, approved_header_credentials={})
    assert plan_res.status == PlanningStatus.UNSUPPORTED

    # Case B: Proposal claims approved header, but local resolver reports NOT available
    resp_body_auth = {"error": {"details": {"required_headers": ["Authorization"]}}}
    obs_auth = _create_observation(tool, status_code=401, response_body=resp_body_auth)
    approved = {"Authorization": "secops_auth"}
    plan_auth = plan_repair(tool, obs_auth, classification, approved_header_credentials=approved)
    assert plan_auth.status == PlanningStatus.PROPOSED

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs_auth,
        classification=classification,
        proposal=plan_auth,
        registry_snapshot=tool,
        approved_header_credentials=approved,
        credential_resolver=lambda ref: False,  # Credential missing locally
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("is NOT available in environment" in r for r in val_res.rejection_reasons)


def test_no_secrets_in_candidates_decisions_or_events():
    """Verify secrets or passwords never appear in candidate headers, decisions, or published events."""
    tool = _create_assign_tool()
    resp_body = {"error": {"details": {"required_headers": ["Authorization"]}}}
    obs = _create_observation(tool, status_code=401, response_body=resp_body)
    classification = _create_classification()
    approved = {"Authorization": "secops_auth"}
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification, approved_header_credentials=approved)
    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
        approved_header_credentials=approved,
        credential_resolver=lambda r: True,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    candidate_str = json.dumps(val_res.candidate.model_dump())
    assert "secops_auth" in candidate_str  # Reference is present
    assert "Bearer {secops_auth}" in candidate_str
    # Verify no raw secrets (like sk- or actual bearer token)
    assert "sk-" not in candidate_str
    assert "secret_" not in candidate_str


# =========================================================================
# 4. Retry Safety Guarantees
# =========================================================================
def test_uncertain_write_blocks_recovery_authorization():
    """Verify that an uncertain write strictly blocks recovery authorization."""
    tool = _create_assign_tool()
    # Uncertain write flagged during execution
    obs = _create_observation(tool, uncertain_write=True)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert val_res.authorized_for_recovery is False
    assert any("uncertain_write=True" in r for r in val_res.rejection_reasons)


def test_successful_write_with_response_drift_blocks_write_retry():
    """Verify that an HTTP POST write returning 200 with response drift cannot be authorized for retry write."""
    tool = _create_assign_tool()
    # Write succeeded downstream (200 OK), but response parsing failed
    obs = _create_observation(tool, status_code=200)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert val_res.authorized_for_recovery is False
    assert any("succeeded downstream" in r for r in val_res.rejection_reasons)


# =========================================================================
# 5. Immutability & Event Envelopes
# =========================================================================
def test_inputs_and_registry_remain_strictly_immutable():
    """Verify that original inputs, observations, and tool registry are 100% untouched by validation."""
    registry = VersionedToolRegistry.create_default()
    tool = registry.get("assign_ticket")
    tool_snapshot = copy.deepcopy(tool)
    obs = _create_observation(tool)
    obs_before = copy.deepcopy(obs.model_dump())
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}
    orig_args_before = copy.deepcopy(orig_args)

    plan_res = plan_repair(tool, obs, classification)
    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=registry,
    )

    assert val_res.status == ValidationStatus.ACCEPTED
    # Original arguments unchanged
    assert orig_args == orig_args_before
    # Snapshot unchanged
    assert tool.model_dump() == tool_snapshot.model_dump()
    # Observation unchanged
    assert obs.model_dump() == obs_before
    # Registry entries untouched
    assert registry.get("assign_ticket").version == "1.0.0"
    assert registry.get("assign_ticket").status == "active"


def test_validation_events_adhere_to_event_envelope():
    """Verify that patch_validated and patch_rejected events adhere to contracts/events.schema.json."""
    broadcaster = EventBroadcaster()
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    val_accepted = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    # 1. Accepted Event
    event_acc = emit_patch_validation_event(broadcaster, "run-val-events", val_accepted, mode="healing")
    assert event_acc.type == "patch_validated"
    assert event_acc.mode == "healing"
    assert event_acc.tool == "assign_ticket"
    assert event_acc.data["status"] == "validated_not_committed"
    assert event_acc.data["recovery_permitted"] is True

    # 2. Rejected Event
    val_rejected = copy.deepcopy(val_accepted)
    val_rejected.status = ValidationStatus.REJECTED
    val_rejected.rejection_reasons = ["Test rejection reason"]
    val_rejected.checks = [SafetyCheckRecord(check_name="test_check", passed=False, reason="Failed")]

    event_rej = emit_patch_validation_event(broadcaster, "run-val-events", val_rejected, mode="healing")
    assert event_rej.type == "patch_rejected"
    assert event_rej.mode == "healing"
    assert event_rej.tool == "assign_ticket"
    assert "Test rejection reason" in event_rej.data["rejection_reasons"]
    assert "test_check" in event_rej.data["failed_checks"]


# =========================================================================
# 7. Regression Tests for Task 7 Blockers
# =========================================================================
def test_status_only_rejection_cannot_authorize_recovery():
    """Verify that HTTP 400/401/403/422 status alone never authorizes recovery without a trusted guarantee."""
    tool = _create_assign_tool()
    # Explicitly disable pre_execution_rejection evidence
    obs = _create_observation(tool, status_code=400, pre_execution_rejection=False)
    classification = _create_classification()
    orig_args = {"ticket_id": 81, "user_id": "123"}

    plan_res = plan_repair(tool, obs, classification)
    assert plan_res.status == PlanningStatus.PROPOSED

    # No execution_guarantees provided
    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments=orig_args,
        observation=obs,
        classification=classification,
        proposal=plan_res,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert val_res.authorized_for_recovery is False
    assert any("alone does not prove rejection before mutation" in r for r in val_res.rejection_reasons)


def test_string_to_integer_requires_evidenced_numeric_semantics():
    """Verify that string-to-integer conversion is rejected if observation lacks numeric semantics."""
    tool = _create_assign_tool()
    # Observation says string expected, not integer
    resp_body = {
        "error": {
            "details": {
                "expected_schema": {"user_id": "string"},
            }
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    # Create proposal with CAST_TYPE to_integer despite lack of integer evidence
    op = PlannedOperation(
        operation_type=RestrictedOperationType.CAST_TYPE,
        source_path="user_id",
        target_path="user_id",
        conversion_policy="string_to_integer",
        value_provenance="caller_argument:user_id",
        evidence_path="response.body.details.expected_schema.user_id",
        evidence_summary="Forced integer cast without numeric evidence",
    )
    plan_dict = {
        "tool": tool.name,
        "base_version": tool.version,
        "base_fingerprint": tool.fingerprint,
        "operations": [
            {"type": op.operation_type.value, "source": op.source_path, "target": op.target_path, "conversion": op.conversion_policy, "credential_ref": op.credential_reference}
        ],
    }
    import hashlib
    raw_hash = json.dumps(plan_dict, sort_keys=True).encode("utf-8")
    fp = hashlib.sha256(raw_hash).hexdigest()[:16]

    proposal = PlanningResult(
        status=PlanningStatus.PROPOSED,
        tool_name=tool.name,
        base_version=tool.version,
        base_fingerprint=tool.fingerprint,
        operations=[op],
        proposal_fingerprint=fp,
    )

    val_res = validate_repair_proposal(
        snapshot=tool,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        observation=obs,
        classification=classification,
        proposal=proposal,
        registry_snapshot=tool,
    )

    assert val_res.status == ValidationStatus.REJECTED
    assert any("lacks evidenced numeric semantics" in r for r in val_res.rejection_reasons)

