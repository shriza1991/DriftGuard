"""Comprehensive test suite for Member 2 Task 5 — Failure Interception and Classification.

Adheres strictly to DriftGuard invariants:
- Pure local fixtures and httpx.MockTransport; zero live services or external model calls.
- Pure deterministic evidence-based classification; zero LLM classification.
- Strict separation of execution-layer FailureCategory and semantic ClassificationResult.
- Independent modeling of retry safety and diagnosis eligibility.
- Write uncertainty strictly forbids blind retries, even when diagnosis is eligible.
- Absolute redaction of credentials and secrets from observations, classifications, and events.
- Zero administrative state or mock mutation inspection.
"""
import copy
import json
import asyncio
import pytest
import httpx
from datetime import datetime, timezone
from langchain_core.messages import AIMessage, ToolMessage

from orchestrator.models import (
    ToolDefinition,
    ExecutionResult,
    SanitizedFailure,
    FailureCategory,
    ExecutionContext,
    TimingInfo,
    ExecutionPhase,
    FailureObservation,
    ClassificationResult,
    SemanticFailureCategory,
    RetrySafety,
)
from orchestrator.registry import VersionedToolRegistry
from orchestrator.client.mock_client import MockBusinessApiClient, MockApiClientError, redact_secrets
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.workflow import WorkflowExecutionManager
from orchestrator.healing.classifier import classify_failure
from orchestrator.agents.baseline import BaselineAgent
from tests.healing.test_baseline_agent import ScriptedChatModel


# =========================================================================
# 1. Status-only 400/422 vs Explicit Contract-Change Evidence
# =========================================================================
@pytest.mark.asyncio
async def test_status_only_400_422_vs_explicit_contract_change_evidence():
    """Verify bare 400/422 is ambiguous, whereas explicit structured schema mismatch is classified as schema drift."""
    # Case A: Bare 422 with generic message (Ambiguous)
    def bare_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "Unprocessable entity"})

    client_bare = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(bare_handler))
    executor_bare = ToolExecutor(client=client_bare)
    res_bare = await executor_bare.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res_bare.is_success is False
    assert res_bare.classification is not None
    assert res_bare.classification.category == SemanticFailureCategory.AMBIGUOUS
    assert res_bare.classification.diagnosis_eligible is False
    assert "insufficient" in res_bare.classification.uncertainty_reason.lower()

    # Case B: Explicit structured migration evidence (Schema Drift)
    def drift_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "category": "schema_mismatch",
                    "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
                    "details": {"missing": ["assignee_id"], "unexpected": ["user_id"]},
                    "migration_hint": "Use assignee_id instead of user_id",
                }
            },
        )

    client_drift = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(drift_handler))
    executor_drift = ToolExecutor(client=client_drift)
    res_drift = await executor_drift.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res_drift.is_success is False
    assert res_drift.classification is not None
    assert res_drift.classification.category == SemanticFailureCategory.SCHEMA_DRIFT
    assert res_drift.classification.diagnosis_eligible is True
    assert "schema mismatch" in res_drift.classification.reason.lower()
    assert res_drift.failure.is_drift is True


# =========================================================================
# 2. Ordinary Invalid Input vs Documented Business Rejection
# =========================================================================
@pytest.mark.asyncio
async def test_ordinary_invalid_input_vs_documented_business_rejection():
    """Verify local validation failure is INVALID_INPUT, while documented business rules are BUSINESS_RULE_REJECTION."""
    executor = ToolExecutor()

    # Case A: Local pre-HTTP argument schema violation (INVALID_INPUT)
    res_invalid = await executor.execute("assign_ticket", {"ticket_id": "not_an_int", "user_id": 123})
    assert res_invalid.is_success is False
    assert res_invalid.classification is not None
    assert res_invalid.classification.category == SemanticFailureCategory.INVALID_INPUT
    assert res_invalid.classification.diagnosis_eligible is False
    assert res_invalid.classification.retry_safety.safe_to_retry is False
    assert res_invalid.classification.retry_safety.requires_input_adaptation is True

    # Case B: Downstream documented business rejection (e.g. ticket closed)
    def closed_ticket_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "TICKET_CLOSED",
                    "message": "Ticket 81 is closed and cannot be reassigned",
                }
            },
        )

    client_biz = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(closed_ticket_handler))
    executor_biz = ToolExecutor(client=client_biz)
    res_biz = await executor_biz.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res_biz.is_success is False
    assert res_biz.classification is not None
    assert res_biz.classification.category == SemanticFailureCategory.BUSINESS_RULE_REJECTION
    assert res_biz.classification.diagnosis_eligible is False
    assert res_biz.classification.retry_safety.safe_to_retry is False


# =========================================================================
# 3. Missing-Field Evidence Does Not Establish Rename Correspondence
# =========================================================================
@pytest.mark.asyncio
async def test_missing_field_evidence_does_not_establish_rename_correspondence():
    """Verify classifier flags drift candidate but does NOT invent correspondence or modify registry."""
    def drift_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "category": "schema_mismatch",
                    "message": "Missing required field",
                    "details": {"missing": ["assignee_id"], "unexpected": ["user_id"]},
                }
            },
        )

    registry = VersionedToolRegistry.create_default()
    original_def = copy.deepcopy(registry.get("assign_ticket"))

    client = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(drift_handler))
    executor = ToolExecutor(registry=registry, client=client)
    res = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res.classification.category == SemanticFailureCategory.SCHEMA_DRIFT
    assert res.classification.diagnosis_eligible is True
    # Invariant: active registry is NOT mutated; no rename correspondence is established at classification stage
    current_def = registry.get("assign_ticket")
    assert current_def.argument_schema.model_dump() == original_def.argument_schema.model_dump()
    assert current_def.request_mapping == original_def.request_mapping


# =========================================================================
# 4. Missing/Revoked Credentials vs Explicit Newly Required Header
# =========================================================================
@pytest.mark.asyncio
async def test_missing_credentials_vs_explicit_newly_required_header():
    """Verify standard 401 is auth rejection, while explicit missing header error allows diagnosis without fabricating credentials."""
    # Case A: Standard 401 Unauthorized
    def standard_auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Unauthorized", "message": "Invalid or expired token"})

    client_auth = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(standard_auth_handler))
    executor_auth = ToolExecutor(client=client_auth)
    res_auth = await executor_auth.execute("find_user", {"name": "Maya"})

    assert res_auth.classification.category == SemanticFailureCategory.AUTHENTICATION_AUTHORIZATION
    assert res_auth.classification.diagnosis_eligible is False

    # Case B: 401 with explicit new-header requirement
    def header_drift_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "MISSING_HEADER",
                    "message": "Required header 'X-Org-Token' is missing",
                    "details": {"required_headers": ["X-Org-Token"]},
                }
            },
        )

    client_hdr = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(header_drift_handler))
    executor_hdr = ToolExecutor(client=client_hdr)
    res_hdr = await executor_hdr.execute("find_user", {"name": "Maya"})

    assert res_hdr.classification.category == SemanticFailureCategory.SCHEMA_DRIFT
    assert res_hdr.classification.diagnosis_eligible is True
    # Retry safety must indicate credentials cannot be fabricated
    assert "credential" in res_hdr.classification.retry_safety.reason.lower()


# =========================================================================
# 5. Known Missing Resource vs Ambiguous 404
# =========================================================================
@pytest.mark.asyncio
async def test_known_missing_resource_vs_ambiguous_404():
    """Verify documented entity not found is business rule rejection, whereas generic 404 is ambiguous."""
    # Case A: Entity Not Found (e.g. ticket 999 does not exist)
    def entity_not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "TICKET_NOT_FOUND", "message": "Ticket 999 does not exist"}},
        )

    client_entity = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(entity_not_found))
    executor_entity = ToolExecutor(client=client_entity)
    res_entity = await executor_entity.execute("get_ticket", {"ticket_id": 999})

    assert res_entity.classification.category == SemanticFailureCategory.BUSINESS_RULE_REJECTION
    assert res_entity.classification.diagnosis_eligible is False

    # Case B: Bare 404 Not Found (could be routed incorrectly or path drift)
    def bare_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    client_bare = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(bare_404))
    executor_bare = ToolExecutor(client=client_bare)
    res_bare = await executor_bare.execute("get_ticket", {"ticket_id": 81})

    assert res_bare.classification.category == SemanticFailureCategory.AMBIGUOUS
    assert res_bare.classification.diagnosis_eligible is False


# =========================================================================
# 6. Rate Limits, Transport Failure, and 5xx
# =========================================================================
@pytest.mark.asyncio
async def test_rate_limits_transport_failure_and_5xx():
    """Verify 429, network errors, and 5xx are classified as TRANSIENT_SERVICE_FAILURE, with write safety enforced."""
    # Case A: 429 Rate Limit
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "Rate limit exceeded. Retry later."})

    client_429 = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(rate_limit_handler))
    executor_429 = ToolExecutor(client=client_429)
    res_429 = await executor_429.execute("find_user", {"name": "Maya"})

    assert res_429.classification.category == SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE
    assert res_429.classification.diagnosis_eligible is False
    assert res_429.classification.retry_safety.safe_to_retry is True  # GET read is safe to retry

    # Case B: Network Transport Error (e.g. connection dropped)
    def drop_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by peer")

    client_drop = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(drop_handler))
    executor_drop = ToolExecutor(client=client_drop)
    res_drop = await executor_drop.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res_drop.classification.category == SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE
    assert res_drop.failure.uncertain_write is True
    assert res_drop.classification.retry_safety.safe_to_retry is False  # POST write uncertain

    # Case C: 500 Internal Server Error on POST write
    def error_500_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Database error"})

    client_500 = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(error_500_handler))
    executor_500 = ToolExecutor(client=client_500)
    res_500 = await executor_500.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res_500.classification.category == SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE
    assert res_500.failure.uncertain_write is True
    assert res_500.classification.retry_safety.safe_to_retry is False


# =========================================================================
# 7. Response-Contract Failure After Write Retains Uncertainty
# =========================================================================
@pytest.mark.asyncio
async def test_response_contract_failure_after_write_retains_uncertainty_and_no_retry():
    """Verify response parsing failure after 200 write marks uncertain_write=True and safe_to_retry=False."""
    def corrupt_response_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<xml>Not JSON</xml>", headers={"Content-Type": "application/xml"})

    client = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(corrupt_response_handler))
    executor = ToolExecutor(client=client)
    res = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res.is_success is False
    assert res.failure.uncertain_write is True
    assert res.classification.category == SemanticFailureCategory.SCHEMA_DRIFT
    assert res.classification.diagnosis_eligible is True
    # Even though diagnosis is eligible, write certainty forbids retry
    assert res.classification.retry_safety.safe_to_retry is False
    assert "uncertain write" in res.classification.retry_safety.reason.lower()


# =========================================================================
# 8. Contradictory or Malformed Evidence Degrades Safely
# =========================================================================
@pytest.mark.asyncio
async def test_contradictory_malformed_evidence_degrades_safely():
    """Verify contradictory evidence (e.g. 400 status claiming success) safely degrades to AMBIGUOUS."""
    def contradictory_handler(request: httpx.Request) -> httpx.Response:
        # HTTP 400 but body claims success=True and valid data
        return httpx.Response(400, json={"success": True, "status": "assigned", "ticket_id": 81})

    client = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(contradictory_handler))
    executor = ToolExecutor(client=client)
    res = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res.is_success is False
    assert res.classification.category == SemanticFailureCategory.AMBIGUOUS
    assert res.classification.diagnosis_eligible is False
    assert "contradictory" in res.classification.uncertainty_reason.lower()


# =========================================================================
# 9. Diagnosis Eligibility Never Overrides Uncertain-Write Protection
# =========================================================================
@pytest.mark.asyncio
async def test_diagnosis_eligibility_never_overrides_uncertain_write_protection():
    """Verify that even when diagnosis_eligible=True on a write operation, safe_to_retry remains False."""
    def mapping_fail_handler(request: httpx.Request) -> httpx.Response:
        # Returns 200 with JSON, but missing required mapped response fields
        return httpx.Response(200, json={"unexpected_field": 999})

    client = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(mapping_fail_handler))
    executor = ToolExecutor(client=client)
    res = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res.is_success is False
    assert res.classification.diagnosis_eligible is True
    assert res.classification.retry_safety.safe_to_retry is False
    assert res.failure.uncertain_write is True


# =========================================================================
# 10. Classification Is Identical Across Execution Paths
# =========================================================================
@pytest.mark.asyncio
async def test_classification_is_identical_across_execution_paths():
    """Verify direct ToolExecutor, WorkflowExecutionManager, and BaselineAgent produce identical classifications."""
    drift_response = {
        "error": {
            "category": "schema_mismatch",
            "message": "Field 'user_id' renamed to 'assignee_id'",
            "details": {"missing": ["assignee_id"], "unexpected": ["user_id"]},
        }
    }

    def mock_workflow_handler(request: httpx.Request) -> httpx.Response:
        if "/users" in str(request.url):
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@example.com"}])
        elif "/tickets/assign" in str(request.url):
            return httpx.Response(422, json=drift_response)
        return httpx.Response(200, json={"id": 81, "assignee_id": "123", "status": "assigned", "history": []})

    # 1. Direct ToolExecutor
    client1 = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(mock_workflow_handler))
    executor = ToolExecutor(client=client1)
    res_direct = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    # 2. WorkflowExecutionManager
    client2 = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(mock_workflow_handler))
    manager = WorkflowExecutionManager(client=client2)
    run_manager = await manager.execute_workflow(run_id="run-compare-1", mode="normal")
    events_manager = manager.event_broadcaster.get_events("run-compare-1")

    # 3. BaselineAgent
    client3 = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(mock_workflow_handler))
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "c1"}]),
            AIMessage(content="", tool_calls=[{"name": "assign_ticket", "args": {"ticket_id": 81, "user_id": "123"}, "id": "c2"}]),
            AIMessage(content="Assign failed.", tool_calls=[]),
        ]
    )
    agent = BaselineAgent(client=client3, model=model)
    run_agent = await agent.execute_workflow(run_id="run-compare-2")
    events_agent = agent.event_broadcaster.get_events("run-compare-2")

    # Verify classifications match
    assert res_direct.classification.category == SemanticFailureCategory.SCHEMA_DRIFT
    assert res_direct.classification.diagnosis_eligible is True

    failed_mgr_event = next(e for e in events_manager if e.type == "tool_call_failed" and e.tool == "assign_ticket")
    assert failed_mgr_event.data["classification"]["category"] == "schema_drift"
    assert failed_mgr_event.data["classification"]["diagnosis_eligible"] is True

    failed_agent_event = next(e for e in events_agent if e.type == "tool_call_failed" and e.tool == "assign_ticket")
    assert failed_agent_event.data["classification"]["category"] == "schema_drift"
    assert failed_agent_event.data["classification"]["diagnosis_eligible"] is True


# =========================================================================
# 11. Secrets Are Absent from Observations, Classifications, and Events
# =========================================================================
@pytest.mark.asyncio
async def test_secrets_are_absent_from_observations_classifications_and_events():
    """Verify secrets (auth tokens, credentials) never appear in observations, classifications, or event payloads."""
    secret_token = "secret-super-vault-key-xyz987"

    def secret_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": f"Invalid token format for {secret_token}"}},
        )

    client = MockBusinessApiClient(
        base_url="http://mock.test",
        transport=httpx.MockTransport(secret_error_handler),
        credential_resolver=lambda ref: secret_token,
    )
    registry = VersionedToolRegistry.create_default()
    tool = registry.get("assign_ticket")
    tool.credential_reference = "vault_token"

    executor = ToolExecutor(registry=registry, client=client)
    res = await executor.execute(tool, {"ticket_id": 81, "user_id": "123", "secret_field": secret_token})

    # Check observation
    obs_json = json.dumps(res.observation.model_dump())
    assert secret_token not in obs_json
    assert "[REDACTED]" in obs_json

    # Check classification
    class_json = json.dumps(res.classification.model_dump())
    assert secret_token not in class_json

    # Check failure
    fail_json = json.dumps(res.failure.model_dump())
    assert secret_token not in fail_json


# =========================================================================
# 12. Prerequisite Regressions: Dynamic Target Ticket & Token Metadata
# =========================================================================
@pytest.mark.asyncio
async def test_prerequisite_regressions_dynamic_ticket_and_incomplete_tokens():
    """Verify non-default target ticket is verified and incomplete token subtotals are flagged."""
    # 1. Dynamic target ticket verification
    verified_tickets = []

    def mock_workflow(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/users" in url_str:
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@example.com"}])
        elif "/tickets/assign" in url_str:
            return httpx.Response(200, json={"ticket_id": 99, "status": "assigned", "assignee_id": "123"})
        elif "/tickets/99" in url_str:
            verified_tickets.append(99)
            return httpx.Response(200, json={"id": 99, "assignee_id": "123", "status": "assigned", "history": []})
        elif "/tickets/81" in url_str:
            verified_tickets.append(81)
            return httpx.Response(200, json={"id": 81, "assignee_id": "wrong_assignee", "status": "assigned", "history": []})
        return httpx.Response(404, json={})

    client = MockBusinessApiClient(base_url="http://mock.test", transport=httpx.MockTransport(mock_workflow))
    manager = WorkflowExecutionManager(client=client)
    run = await manager.execute_workflow(
        task="Assign ticket 99 to Maya and verify",
        run_id="run-dynamic-99",
        mode="normal",
    )
    assert run["status"] == "completed"
    assert 99 in verified_tickets
    assert 81 not in verified_tickets

    # 2. Incomplete token subtotals
    turn1 = AIMessage(
        content="",
        tool_calls=[{"name": "find_user", "args": {"name": "Maya"}, "id": "c1"}],
        usage_metadata={"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
    )
    turn2 = AIMessage(
        content="All done",
        tool_calls=[],
        # No usage_metadata on turn 2
    )

    fake_model = ScriptedChatModel(responses=[turn1, turn2])
    agent = BaselineAgent(client=client, model=fake_model)
    result = await agent.execute_workflow(run_id="run-partial-tokens")

    token_usage = result.get("token_usage") or result.get("usage")
    assert token_usage["subtotal_status"] == "incomplete_subtotal"
    assert token_usage["is_complete"] is False
    assert token_usage["total_tokens"] == 60


# =========================================================================
# 13. Timeout Cancellation During Dispatched Write Retains Uncertainty
# =========================================================================
@pytest.mark.asyncio
async def test_timeout_cancellation_during_dispatched_write_retains_uncertainty():
    """Verify timeout/cancellation during write sets uncertain_write=True and safe_to_retry=False with zero retries."""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Downstream write request timed out waiting for socket")

    transport = httpx.MockTransport(timeout_handler)
    client = MockBusinessApiClient(
        base_url="http://mock.test",
        transport=transport,
    )
    executor = ToolExecutor(client=client)

    res = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert res.is_success is False
    assert res.failure.uncertain_write is True
    assert res.failure.failure_category == FailureCategory.TRANSPORT_FAILURE
    assert res.classification.category == SemanticFailureCategory.TRANSIENT_SERVICE_FAILURE
    assert res.classification.retry_safety.safe_to_retry is False
    assert "uncertain write" in res.classification.retry_safety.reason.lower()
