"""Comprehensive unit test suite for Member 2 Task 6 — Deterministic Repair Proposals.

Adheres strictly to DriftGuard invariants:
- Pure local fixtures; zero network or model dependencies.
- Pure evidence-backed declarative proposal generation.
- Zero LLM diagnosis or heuristic hallucination.
- Strict preservation of caller business parameters (ticket_id, user_id).
- Separation of planning from static validation (Task 7) and recovery execution (Task 8).
- Independent preservation of retry-safety metadata (uncertain writes remain strictly protected).
- Inputs and tool registry state remain completely unmutated.
"""
import copy
import json
from typing import Optional, Dict, Any, List
import pytest
from datetime import datetime, timezone

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    RestrictedOperationType,
    OperationIdentity,
    TimingInfo,
)
from orchestrator.models.observation import ExecutionPhase, FailureObservation
from orchestrator.models.classification import ClassificationResult, SemanticFailureCategory, RetrySafety
from orchestrator.models.proposal import PlanningStatus, PlannedOperation, PlanningResult
from orchestrator.healing.planner import plan_repair, emit_patch_proposed_event
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.events import EventBroadcaster


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
    status_code: int = 422,
    response_body: Optional[dict] = None,
    original_args: Optional[dict] = None,
    uncertain_write: bool = False,
) -> FailureObservation:
    """Helper creating a valid FailureObservation."""
    args = original_args or {"ticket_id": 81, "user_id": "123"}
    body = response_body or {}
    return FailureObservation(
        tool_name=tool.name,
        registry_version=tool.version,
        fingerprint=tool.fingerprint,
        run_id="run-plan-001",
        tool_call_id="call-plan-001",
        idempotency_key="idemp-plan-001",
        operation_identity=OperationIdentity(
            tool_name=tool.name,
            endpoint=tool.endpoint,
            method=tool.method,
            idempotency_key="idemp-plan-001",
        ),
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method=tool.method,
        endpoint=tool.endpoint,
        http_status=status_code,
        original_arguments=args,
        response_evidence={"status_code": status_code, "body": body},
        write_attempted=True,
        uncertain_write=uncertain_write,
        raw_error_message=str(body.get("error", {}).get("message", "Error")),
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
            reason="Uncertain write" if uncertain_write else "Downstream schema rejection",
            uncertain_write=uncertain_write,
            requires_input_adaptation=True,
            safe_if_adapted=(not uncertain_write),
        ),
    )


# =========================================================================
# 1. Explicit Rename Proposal
# =========================================================================
def test_explicit_rename_proposal():
    """Verify explicit field rename evidence generates a RENAME_ARGUMENT proposal."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "category": "schema_mismatch",
            "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
            "details": {
                "missing": ["assignee_id"],
                "unexpected": ["user_id"],
                "migration_mapping": {"user_id": "assignee_id"},
            },
            "migration_hint": "Use assignee_id instead of user_id",
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status == PlanningStatus.PROPOSED
    assert len(result.operations) == 1
    op = result.operations[0]
    assert op.operation_type == RestrictedOperationType.RENAME_ARGUMENT
    assert op.source_path == "user_id"
    assert op.target_path == "assignee_id"
    assert op.value_provenance == "caller_argument:user_id"

    # Candidate preview updates request_mapping body key while preserving source argument
    assert result.candidate_preview is not None
    assert result.candidate_preview.status == "proposed_unvalidated"
    body_map = result.candidate_preview.request_mapping["body"]
    assert "assignee_id" in body_map
    assert body_map["assignee_id"] == "{assignee_id}"
    assert "user_id" not in body_map
    assert body_map["ticket_id"] == "{ticket_id}"


# =========================================================================
# 2. Explicit Nesting Proposal
# =========================================================================
def test_explicit_nesting_proposal():
    """Verify explicit nesting evidence generates a NEST_ARGUMENT proposal."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "category": "schema_mismatch",
            "message": "user_id is now nested under target.id",
            "details": {
                "missing": ["target.id"],
                "unexpected": ["user_id"],
            },
            "migration_hint": "user_id is now nested under target.id",
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status == PlanningStatus.PROPOSED
    assert len(result.operations) == 1
    op = result.operations[0]
    assert op.operation_type == RestrictedOperationType.NEST_ARGUMENT
    assert op.source_path == "user_id"
    assert op.target_path == "target.id"

    # Candidate preview nesting structure
    assert result.candidate_preview is not None
    body_map = result.candidate_preview.request_mapping["body"]
    assert "target" in body_map
    assert body_map["target"]["id"] == "{target.id}"
    assert "user_id" not in body_map
    assert body_map["ticket_id"] == "{ticket_id}"


# =========================================================================
# 3. Explicit Type-Change Proposal
# =========================================================================
def test_explicit_type_change_proposal():
    """Verify expected schema type change generates a CAST_TYPE proposal with declared conversion policy."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "category": "schema_mismatch",
            "message": "Expected integer for user_id, received string '123'",
            "details": {
                "expected_schema": {"ticket_id": "integer", "user_id": "integer"},
                "expected_types": {"user_id": "integer"},
            },
            "migration_hint": "Field 'user_id' expects integer, received 'user_id' string",
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status == PlanningStatus.PROPOSED
    type_ops = [op for op in result.operations if op.operation_type == RestrictedOperationType.CAST_TYPE]
    assert len(type_ops) == 1
    assert type_ops[0].source_path == "user_id"
    assert type_ops[0].conversion_policy == "string_to_integer"
    assert type_ops[0].value_provenance == "caller_argument:user_id"


# =========================================================================
# 4. Explicit Required-Header Proposal
# =========================================================================
def test_explicit_required_header_proposal():
    """Verify newly required header maps through approved credential reference and refuses if unapproved."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "MISSING_HEADER",
            "message": "Required header 'X-Org-Token' is missing",
            "details": {"required_headers": ["X-Org-Token"]},
        }
    }
    obs = _create_observation(tool, status_code=401, response_body=resp_body)
    classification = _create_classification()

    # Case A: Approved configuration available
    approved_cfg = {"X-Org-Token": "org_token_vault"}
    result_approved = plan_repair(tool, obs, classification, approved_header_credentials=approved_cfg)

    assert result_approved.status == PlanningStatus.PROPOSED
    hdr_ops = [op for op in result_approved.operations if op.operation_type == RestrictedOperationType.SET_HEADER]
    assert len(hdr_ops) == 1
    assert hdr_ops[0].target_path == "X-Org-Token"
    assert hdr_ops[0].credential_reference == "org_token_vault"
    assert hdr_ops[0].value_provenance == "approved_credential_reference:org_token_vault"

    # Case B: Unapproved header -> Refusal with unsupported status
    result_unapproved = plan_repair(tool, obs, classification, approved_header_credentials={})
    assert result_unapproved.status == PlanningStatus.UNSUPPORTED
    assert "no approved credential reference" in result_unapproved.reasons[0].lower()
    assert "cannot be fabricated" in result_unapproved.reasons[0].lower()


# =========================================================================
# 5. Complete Combined Proposal with Minimal Unrelated-Field Preservation
# =========================================================================
def test_complete_combined_proposal_with_unrelated_field_preservation():
    """Verify combination of nesting, type change, and header requirements composes coherently in deterministic order."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "POST body migrated in v2: user_id is now nested under target.id as an integer. Authorization header required.",
            "details": {
                "expected_schema": {
                    "ticket_id": "integer",
                    "target": {"id": "integer"},
                },
                "required_headers": ["Authorization"],
                "migration_mapping": {"user_id": "target.id"},
            },
            "hint": "user_id is now nested under target.id",
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()
    approved = {"Authorization": "secops_auth"}

    result = plan_repair(tool, obs, classification, approved_header_credentials=approved)

    assert result.status == PlanningStatus.PROPOSED
    assert len(result.operations) == 3

    # Operations must be ordered deterministically: SET_HEADER -> NEST_ARGUMENT -> CAST_TYPE
    assert result.operations[0].operation_type == RestrictedOperationType.SET_HEADER
    assert result.operations[1].operation_type == RestrictedOperationType.NEST_ARGUMENT
    assert result.operations[2].operation_type == RestrictedOperationType.CAST_TYPE

    # Check candidate preview
    preview = result.candidate_preview
    assert preview is not None
    assert preview.credential_reference == "secops_auth"
    assert "Authorization" in preview.headers
    assert preview.request_mapping["body"]["target"]["id"] == "{target.id}"

    # Unrelated fields strictly preserved
    assert preview.endpoint == "/tickets/assign"
    assert preview.method == "POST"
    assert preview.request_mapping["body"]["ticket_id"] == "{ticket_id}"
    assert preview.response_mapping == tool.response_mapping


# =========================================================================
# 6. Missing Field Without Correspondence Yields Insufficient Evidence
# =========================================================================
def test_missing_field_without_correspondence_yields_insufficient_evidence():
    """Verify missing field error alone without correspondence evidence refuses with insufficient_evidence."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "Field 'assignee_id' is required",
            "details": {
                "missing": ["assignee_id"],
                "unexpected": ["user_id"],
                # Strictly NO migration_mapping and NO migration_hint linking assignee_id to user_id
            },
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status == PlanningStatus.INSUFFICIENT_EVIDENCE
    assert result.proposal is None
    assert any("cannot establish a rename" in r.lower() or "no explicit correspondence" in r.lower() for r in result.reasons)
    assert any("assignee_id" in req for req in result.missing_requirements)


# =========================================================================
# 7. Missing Business Value Refusal (No Invented Data)
# =========================================================================
def test_missing_business_value_refusal():
    """Verify newly required business parameter with no available source in caller arguments refuses."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "Field 'priority_tier' is required for all new assignments",
            "details": {
                "missing": ["priority_tier"],
            },
        }
    }
    # Caller arguments only have ticket_id and user_id
    obs = _create_observation(tool, response_body=resp_body, original_args={"ticket_id": 81, "user_id": "123"})
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status in [PlanningStatus.UNSUPPORTED, PlanningStatus.INSUFFICIENT_EVIDENCE]
    assert any("priority_tier" in r for r in result.reasons)


# =========================================================================
# 8. Conflicting Destination Collision Refusal
# =========================================================================
def test_conflicting_destination_collision_refusal():
    """Verify conflicting evidence mapping two distinct caller arguments to identical destination refuses."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "details": {
                "migration_mapping": {
                    "user_id": "target.id",
                    "ticket_id": "target.id",  # Collision!
                }
            },
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)

    assert result.status == PlanningStatus.UNSUPPORTED
    assert any("collision" in r.lower() for r in result.reasons)


# =========================================================================
# 9. Stale or Mismatched Observation and Snapshot Cannot Produce Proposal
# =========================================================================
def test_stale_or_mismatched_observation_and_snapshot_refusal():
    """Verify planner rejects mismatched tool name, version, or fingerprint as not_eligible."""
    tool = _create_assign_tool()
    obs = _create_observation(tool)
    classification = _create_classification()

    # Mismatched fingerprint
    stale_tool = copy.deepcopy(tool)
    stale_tool.fingerprint = "different_fingerprint_xyz"

    result = plan_repair(stale_tool, obs, classification)
    assert result.status == PlanningStatus.NOT_ELIGIBLE
    assert result.is_eligible is False
    assert "mismatched" in result.reasons[0].lower()


# =========================================================================
# 10. Distinct Source, Wire, and Schema Paths
# =========================================================================
def test_distinct_source_wire_and_schema_paths():
    """Verify source caller argument path (user_id) is distinguished from wire request path (target.id)."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "details": {"migration_mapping": {"user_id": "target.id"}},
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)
    assert result.status == PlanningStatus.PROPOSED
    op = result.operations[0]

    # Source argument is untouched
    assert op.source_path == "user_id"
    # Wire target path is nested
    assert op.target_path == "target.id"
    # Mapping expansion uses template placeholder of the source argument
    assert result.candidate_preview.request_mapping["body"]["target"]["id"] == "{target.id}"


# =========================================================================
# 11. Planner Inputs and Registry State Remain Unchanged
# =========================================================================
def test_planner_inputs_and_registry_remain_unchanged():
    """Verify plan_repair never mutates snapshot, observation, or global tool registry."""
    tool = _create_assign_tool()
    tool_snapshot = copy.deepcopy(tool)

    resp_body = {
        "error": {
            "details": {"migration_mapping": {"user_id": "assignee_id"}},
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    obs_dict_before = obs.model_dump()
    classification = _create_classification()

    registry = VersionedToolRegistry.create_default()
    reg_before = {t.name: t.fingerprint for t in registry.list_active()}

    result = plan_repair(tool, obs, classification)
    assert result.status == PlanningStatus.PROPOSED

    # Tool snapshot must be completely unmodified
    assert tool.model_dump() == tool_snapshot.model_dump()
    # Observation must be completely unmodified
    assert obs.model_dump() == obs_dict_before
    # Registry entries must be completely unmodified
    reg_after = {t.name: t.fingerprint for t in registry.list_active()}
    assert reg_before == reg_after


# =========================================================================
# 12. Deterministic Ordering and Proposal Fingerprint
# =========================================================================
def test_deterministic_ordering_and_proposal_fingerprint():
    """Verify proposal fingerprint is deterministic across dictionary key variations and excludes timestamps/UUIDs."""
    tool = _create_assign_tool()

    resp_body_1 = {
        "error": {
            "details": {
                "required_headers": ["Authorization"],
                "migration_mapping": {"user_id": "target.id"},
                "expected_schema": {"target": {"id": "integer"}},
            }
        }
    }
    resp_body_2 = {
        "error": {
            "details": {
                # Reversed keys
                "expected_schema": {"target": {"id": "integer"}},
                "migration_mapping": {"user_id": "target.id"},
                "required_headers": ["Authorization"],
            }
        }
    }

    obs1 = _create_observation(tool, response_body=resp_body_1)
    obs2 = _create_observation(tool, response_body=resp_body_2)
    classification = _create_classification()
    approved = {"Authorization": "secops_auth"}

    res1 = plan_repair(tool, obs1, classification, approved_header_credentials=approved)
    res2 = plan_repair(tool, obs2, classification, approved_header_credentials=approved)

    assert res1.status == PlanningStatus.PROPOSED
    assert res2.status == PlanningStatus.PROPOSED
    # Deterministic fingerprint must match identically
    assert res1.proposal_fingerprint == res2.proposal_fingerprint
    assert res1.proposal.proposal_id == res2.proposal.proposal_id
    assert [op.operation_type for op in res1.operations] == [op.operation_type for op in res2.operations]


# =========================================================================
# 13. Secrets Are Absent from Proposals, Previews, and Reasons
# =========================================================================
def test_secrets_are_absent_from_proposals_previews_and_reasons():
    """Verify secret credentials or auth tokens never appear in proposal payloads or candidate previews."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "code": "MISSING_HEADER",
            "details": {"required_headers": ["Authorization"]},
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    # Injected approved mapping uses credential reference name, never a raw secret
    approved = {"Authorization": "vault_bearer_ref"}

    result = plan_repair(tool, obs, classification, approved_header_credentials=approved)
    assert result.status == PlanningStatus.PROPOSED

    proposal_json = json.dumps(result.model_dump())
    assert "vault_bearer_ref" in proposal_json
    # Raw secrets must not exist anywhere
    assert "sk-" not in proposal_json
    assert "password" not in proposal_json.lower()
    assert "bearer secret" not in proposal_json.lower()


# =========================================================================
# 14. Prerequisite Regressions: Frozen Observation Immutability & Retry Metadata
# =========================================================================
def test_prerequisite_regressions_frozen_observation_immutability():
    """Verify FailureObservation cannot be mutated via retained caller references or nested dict item assignment."""
    tool = _create_assign_tool()
    caller_dict = {"ticket_id": 81, "user_id": "123"}
    resp_dict = {"status_code": 422, "body": {"error": "schema_mismatch"}}

    obs = FailureObservation(
        tool_name=tool.name,
        registry_version=tool.version,
        fingerprint=tool.fingerprint,
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method="POST",
        endpoint="/tickets/assign",
        original_arguments=caller_dict,
        response_evidence=resp_dict,
    )

    # 1. Mutating retained caller dictionary does NOT affect observation
    caller_dict["ticket_id"] = 999
    caller_dict["injected"] = "malicious"
    assert obs.original_arguments["ticket_id"] == 81
    assert "injected" not in obs.original_arguments

    # 2. Mutating dictionary returned by property does NOT affect observation internal state
    returned_args = obs.original_arguments
    returned_args["ticket_id"] = 777
    assert obs.original_arguments["ticket_id"] == 81

    # 3. RetrySafety metadata distinguishes uncertain write from safe resubmission
    safety_uncertain = RetrySafety(
        safe_to_retry=False,
        reason="Uncertain write",
        uncertain_write=True,
        requires_input_adaptation=False,
        safe_if_adapted=False,
    )
    assert safety_uncertain.uncertain_write is True
    assert safety_uncertain.safe_if_adapted is False
    assert safety_uncertain.is_useless_unchanged_retry is False

    safety_drift = RetrySafety(
        safe_to_retry=False,
        reason="Schema rejection",
        uncertain_write=False,
        requires_input_adaptation=True,
        safe_if_adapted=True,
    )
    assert safety_drift.is_useless_unchanged_retry is True
    assert safety_drift.safe_if_adapted is True


# =========================================================================
# 15. Emit Patch Proposed Event
# =========================================================================
def test_emit_patch_proposed_event():
    """Verify emit_patch_proposed_event publishes unvalidated proposal event and avoids fake validation events."""
    tool = _create_assign_tool()
    resp_body = {
        "error": {
            "details": {"migration_mapping": {"user_id": "assignee_id"}},
        }
    }
    obs = _create_observation(tool, response_body=resp_body)
    classification = _create_classification()

    result = plan_repair(tool, obs, classification)
    assert result.status == PlanningStatus.PROPOSED

    broadcaster = EventBroadcaster()
    emit_patch_proposed_event(broadcaster, run_id="run-plan-evt", planning_result=result)

    events = broadcaster.get_events("run-plan-evt")
    assert len(events) == 1
    event = events[0]
    assert event.type == "patch_proposed"
    assert event.tool == "assign_ticket"
    assert event.data["status"] == "unvalidated_proposal"
    assert event.data["validation_required"] is True
    assert event.data["proposal_id"] == result.proposal.proposal_id

    # Must NOT emit patch_validated or recovery_verified
    assert not any(e.type in ["patch_validated", "recovery_verified"] for e in events)
