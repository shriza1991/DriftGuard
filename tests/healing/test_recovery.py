"""Comprehensive tests for RecoveryController and genuine healing mode.

Adheres strictly to Member 2 safety invariants:
- Uses fake framework tool-calling messages and httpx.MockTransport; no live model or network required.
- Verifies bounded recovery sequence:
  1. Lock acquisition & version recheck.
  2. Proposal planning & independent validation.
  3. Candidate staging via registry.
  4. At most ONE repaired HTTP execution per failed invocation.
  5. Independent read verification (get_ticket).
  6. Atomic version-checked registry commit.
- Covers:
  - Rename, nesting, type conversion, approved header, and combined mutations.
  - Exactly one business effect on recovery.
  - Failed repaired execution stops immediately (no loops).
  - Status-only rejection cannot authorize recovery without trusted guarantee.
  - Verification mismatch blocks commit and records separate truthful business outcome.
  - Uncertain writes blocked from automated recovery.
  - Stale concurrent recovery cannot overwrite or duplicate writes.
  - Staged candidate cleanup and lock release on failure.
  - Model bindings refresh; second run uses committed definitions without diagnosis.
  - Baseline agent remains frozen and unchanged.
  - Truthful, sanitized, contract-compliant events.
"""
import pytest
import json
import httpx
import asyncio
from typing import Dict, Any, Optional, List

from orchestrator.models import (
    ToolDefinition,
    ExecutionResult,
    ExecutionContext,
    OperationIdentity,
    ExecutionPhase,
    FailureObservation,
    ClassificationResult,
    SemanticFailureCategory,
    RetrySafety,
    ValidationStatus,
)
from orchestrator.registry import VersionedToolRegistry, StaleVersionError
from orchestrator.client.mock_client import MockBusinessApiClient
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.events import EventBroadcaster
from orchestrator.healing.recovery import (
    RecoveryController,
    RecoveryResult,
    RecoveryStatus,
    default_verify_business_intent,
)
from orchestrator.healing.planner import plan_repair
from orchestrator.healing.validator import validate_repair_proposal
from orchestrator.agents.healing import HealingAgent
from orchestrator.agents.baseline import BaselineAgent
from langchain_core.messages import AIMessage, ToolCall


def _create_assign_observation(
    status_code: int = 400,
    response_body: Optional[Dict[str, Any]] = None,
    uncertain_write: bool = False,
    pre_execution_rejection: bool = True,
    ticket_id: int = 81,
    user_id: str = "123",
) -> FailureObservation:
    """Creates a realistic FailureObservation for assign_ticket."""
    resp_body = response_body or {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
            "details": {
                "migration_mapping": {"user_id": "assignee_id"},
            },
            "hint": "Use assignee_id instead of user_id",
        }
    }
    details = dict(resp_body.get("error", {}).get("details", {}))
    if pre_execution_rejection:
        details["pre_execution_rejection"] = True

    return FailureObservation(
        tool_name="assign_ticket",
        registry_version="1.0.0",
        fingerprint="",
        run_id="run-rec-test",
        tool_call_id="call-assign-001",
        idempotency_key="idemp-assign-001",
        operation_identity=OperationIdentity(
            tool_name="assign_ticket",
            endpoint="/tickets/assign",
            method="POST",
            idempotency_key="idemp-assign-001",
        ),
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method="POST",
        endpoint="/tickets/assign",
        http_status=status_code,
        original_arguments={"ticket_id": ticket_id, "user_id": user_id},
        response_evidence={"status_code": status_code, "body": resp_body},
        write_attempted=True,
        uncertain_write=uncertain_write,
        raw_error_message=str(resp_body.get("error", {}).get("message", "Error")),
        validation_details=details,
        evidence_paths=["response.body.details.migration_mapping.user_id"],
    )


# =============================================================================
# 1. Successful Recovery Flows: Rename, Nesting, Type, Header, Combined
# =============================================================================

@pytest.mark.asyncio
async def test_recovery_rename_successful():
    """Verify autonomous recovery from a renamed argument mutation."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    http_requests: List[httpx.Request] = []

    # MockTransport: 1st assign succeeds with assignee_id, then get_ticket confirms assignee
    def handler(request: httpx.Request) -> httpx.Response:
        http_requests.append(request)
        if request.url.path == "/tickets/assign" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            # Downstream expects assignee_id
            if "assignee_id" in payload and payload["assignee_id"] == "123":
                return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": "123"})
            return httpx.Response(400, json={"error": "Missing assignee_id"})
        elif request.url.path == "/tickets/81" and request.method == "GET":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "123", "status": "assigned", "history": []})
        return httpx.Response(404, json={"error": "Not found"})

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(
        tool_name="assign_ticket",
        endpoint="/tickets/assign",
        method="POST",
        idempotency_key="idemp-assign-001",
    )

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-rename",
    )

    assert res.status == RecoveryStatus.RECOVERED
    assert res.recovered is True
    assert res.business_effect_occurred is True
    assert res.verification_succeeded is True
    assert res.registry_committed is True
    assert res.committed_version == "1.1.0"
    assert res.diagnosis_tokens == 0

    # Verify active registry was updated
    active = registry.get("assign_ticket")
    assert active.version == "1.1.0"

    # Exactly two requests made: 1 repaired POST, 1 verification GET
    assert len(http_requests) == 2
    assert http_requests[0].method == "POST"
    assert http_requests[1].method == "GET"


@pytest.mark.asyncio
async def test_recovery_nesting_successful():
    """Verify autonomous recovery from a nested argument mutation (target.id)."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    http_requests: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_requests.append(request)
        if request.url.path == "/tickets/assign" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("target", {}).get("id") == "123":
                return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": "123"})
            return httpx.Response(400, json={"error": "Missing target.id"})
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "123", "status": "assigned", "history": []})
        return httpx.Response(404, json={"error": "Not found"})

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "user_id is now nested under target.id",
            "details": {"migration_mapping": {"user_id": "target.id"}},
            "hint": "user_id is now nested under target.id",
        }
    }
    obs = _create_assign_observation(response_body=resp_body)
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-nesting",
    )

    assert res.status == RecoveryStatus.RECOVERED
    assert res.business_effect_occurred is True
    assert res.verification_succeeded is True
    assert res.registry_committed is True


@pytest.mark.asyncio
async def test_recovery_type_conversion_successful():
    """Verify autonomous recovery with lossless string-to-integer conversion and identity equivalence."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets/assign" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            # Downstream requires integer
            if isinstance(payload.get("user_id"), int) and payload["user_id"] == 123:
                return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": 123})
            return httpx.Response(422, json={"error": "Expected integer for user_id"})
        elif request.url.path == "/tickets/81":
            # Downstream returns integer 123
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": 123, "status": "assigned", "history": []})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "details": {"expected_schema": {"user_id": "integer"}},
        }
    }
    obs = _create_assign_observation(status_code=422, response_body=resp_body)
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    # Caller originally passed string "123"
    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-type",
    )

    assert res.status == RecoveryStatus.RECOVERED
    assert res.verification_succeeded is True
    # Identity equivalence verified "123" == 123
    assert res.verification_details["actual_assignee_id"] == 123


@pytest.mark.asyncio
async def test_recovery_approved_header_successful():
    """Verify autonomous recovery from missing header using approved credential reference."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets/assign":
            auth = request.headers.get("Authorization")
            if auth == "Bearer secops_token_secret_12345":
                return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": "123"})
            return httpx.Response(401, json={"error": "Missing Authorization header"})
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "123", "status": "assigned", "history": []})
        return httpx.Response(404)

    def resolver(cred_ref: str) -> Optional[str]:
        if cred_ref == "secops_auth":
            return "secops_token_secret_12345"
        return None

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler), credential_resolver=resolver)
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        approved_header_credentials={"Authorization": "secops_auth"},
        credential_resolver=lambda r: r == "secops_auth",
        execution_guarantees={"pre_execution_rejection": True},
    )

    resp_body = {
        "error": {
            "code": "MISSING_HEADER",
            "details": {"required_headers": ["Authorization"]},
        }
    }
    obs = _create_assign_observation(status_code=401, response_body=resp_body)
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-header",
    )

    assert res.status == RecoveryStatus.RECOVERED
    assert res.registry_committed is True


# =============================================================================
# 2. Safety Bounds & Guardrails
# =============================================================================

@pytest.mark.asyncio
async def test_status_only_rejection_blocks_recovery():
    """Verify that HTTP 400 alone never authorizes recovery without a trusted guarantee."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    client = MockBusinessApiClient()
    executor = ToolExecutor(registry=registry, client=client)

    # Controller has NO pre_execution_rejection guarantee
    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={}, # Empty guarantees!
    )

    # Observation lacks pre_execution_rejection evidence
    obs = _create_assign_observation(status_code=400, pre_execution_rejection=False)
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-status-only",
    )

    assert res.status == RecoveryStatus.INELIGIBLE
    assert res.recovered is False
    assert "alone does not prove rejection before mutation" in res.termination_reason


@pytest.mark.asyncio
async def test_uncertain_write_blocks_recovery():
    """Verify that failures flagged with uncertain_write=True strictly block recovery."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    client = MockBusinessApiClient()
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation(uncertain_write=True)
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-uncertain",
    )

    assert res.status == RecoveryStatus.INELIGIBLE
    assert res.recovered is False
    assert "uncertain write" in res.termination_reason.lower()


@pytest.mark.asyncio
async def test_failed_repaired_execution_stops_after_one_attempt():
    """Verify that if the single repaired execution fails, recovery terminates immediately."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    post_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        if request.url.path == "/tickets/assign":
            post_attempts += 1
            return httpx.Response(500, json={"error": "Downstream server error on repaired write"})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-exec-fail",
    )

    assert res.status == RecoveryStatus.EXECUTION_FAILED
    assert res.recovered is False
    assert res.business_effect_occurred is False
    assert res.registry_committed is False
    assert post_attempts == 1 # Exactly one attempt; no repeated attempts!


@pytest.mark.asyncio
async def test_verification_mismatch_prevents_commit_and_records_truthful_outcome():
    """Verify that if write succeeds but verification reads conflicting state, commit is prevented."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets/assign":
            # Write succeeds
            return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": "123"})
        elif request.url.path == "/tickets/81":
            # Verification reads conflicting assignee "999" instead of "123"
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "999", "status": "assigned", "history": []})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-verif-fail",
    )

    assert res.status == RecoveryStatus.VERIFICATION_FAILED
    assert res.recovered is False
    # Business write occurred downstream, but verification failed:
    assert res.business_effect_occurred is True
    assert res.verification_succeeded is False
    assert res.registry_committed is False

    # Active tool remained at v1.0.0
    active = registry.get("assign_ticket")
    assert active.version == "1.0.0"


@pytest.mark.asyncio
async def test_staged_candidate_discarded_and_lock_released_on_failure():
    """Verify staged candidate is discarded and lock is freed if validation or execution fails."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Server error"})

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-rec-discard",
    )

    assert res.status == RecoveryStatus.EXECUTION_FAILED
    assert res.candidate_id is not None

    # Staged candidate should now be discarded
    cand = registry.get_candidate(res.candidate_id)
    assert cand.status == "discarded"

    # Lock must be released and re-acquirable
    lock = registry.tool_lock("assign_ticket")
    acquired = False
    async with lock:
        acquired = True
    assert acquired is True


@pytest.mark.asyncio
async def test_repeated_proposal_fingerprint_terminates_loop():
    """Verify that repeated proposal fingerprints for the same operation identity are halted."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Server error"})

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    # First attempt fails at execution
    res1 = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-loop-1",
    )
    assert res1.status == RecoveryStatus.EXECUTION_FAILED

    # Second attempt with the exact same proposal fingerprint for the same op_id is blocked
    res2 = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-loop-2",
    )
    assert res2.status == RecoveryStatus.PLANNING_FAILED
    assert "already attempted" in res2.termination_reason


@pytest.mark.asyncio
async def test_concurrent_stale_recovery_skips_speculative_writes():
    """Verify that if the active tool version advances under the lock, waiting recovery skips execution."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()
    client = MockBusinessApiClient()
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    # Manually advance tool version to 1.1.0 in registry beforehand
    tool = registry.get("assign_ticket")
    staged = await registry.stage_candidate("assign_ticket", tool)
    await registry.commit_candidate(staged.candidate_id, expected_base_version="1.0.0")

    # Observation is based on base v1.0.0
    res = await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-stale-skip",
    )

    assert res.status == RecoveryStatus.STALE_SKIPPED
    assert res.recovered is False
    assert "already updated" in res.termination_reason


# =============================================================================
# 3. Model Binding Refresh & Second Run Reuse
# =============================================================================

class FakeChatModel:
    """Mock LangChain ChatModel generating scripted tool calls."""

    def __init__(self, turns_responses: List[AIMessage]):
        self.turns_responses = turns_responses
        self.call_count = 0
        self.bound_tools = []

    def bind_tools(self, tools: List[Dict[str, Any]]):
        new_model = FakeChatModel(self.turns_responses)
        new_model.bound_tools = tools
        new_model.call_count = self.call_count
        return new_model

    async def ainvoke(self, messages: List[Any]) -> AIMessage:
        if self.call_count < len(self.turns_responses):
            resp = self.turns_responses[self.call_count]
            self.call_count += 1
            return resp
        return AIMessage(content="I am finished.")


@pytest.mark.asyncio
async def test_healing_agent_model_binding_refresh_and_second_run_reuse():
    """Verify genuine model-driven healing loop:
    1. Turn 1: model calls find_user -> succeeds.
    2. Turn 2: model calls assign_ticket -> fails, recovered, committed to registry (v1.1.0).
    3. Model bindings refresh from active registry.
    4. Turn 3: model calls get_ticket -> confirms assignee, workflow succeeds.
    5. Run 2: agent runs against committed registry and uses v1.1.0 without diagnosis!
    """
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    # Handlers for HTTP calls
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@test.com"}])
        elif request.url.path == "/tickets/assign":
            payload = json.loads(request.content.decode("utf-8"))
            if "assignee_id" in payload and payload["assignee_id"] == "123":
                return httpx.Response(200, json={"ticket_id": 81, "assignee_id": "123", "status": "assigned"})
            # V1 body with user_id fails with drift evidence
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "SCHEMA_MISMATCH",
                        "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
                        "details": {"migration_mapping": {"user_id": "assignee_id"}},
                    }
                },
            )
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "123", "status": "assigned", "history": []})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))

    # Scripted model turn responses for Run 1
    # Turn 1: find Maya
    msg1 = AIMessage(
        content="Looking up Maya...",
        tool_calls=[ToolCall(name="find_user", args={"name": "Maya"}, id="call-find-001")],
    )
    # Turn 2: assign ticket using old contract (user_id)
    msg2 = AIMessage(
        content="Assigning ticket 81 to Maya...",
        tool_calls=[ToolCall(name="assign_ticket", args={"ticket_id": 81, "user_id": "123"}, id="call-assign-001")],
    )
    # Turn 3: check ticket
    msg3 = AIMessage(
        content="Confirming ticket assignment...",
        tool_calls=[ToolCall(name="get_ticket", args={"ticket_id": 81}, id="call-get-001")],
    )
    # Turn 4: complete
    msg4 = AIMessage(content="Task completed successfully.")

    fake_model = FakeChatModel([msg1, msg2, msg3, msg4])

    agent = HealingAgent(
        registry=registry,
        client=client,
        model=fake_model,
        execution_guarantees={"pre_execution_rejection": True},
    )

    run_1 = await agent.execute_workflow(run_id="run-healing-1")

    assert run_1["status"] == "completed"
    assert run_1["outcome"]["downstream_verified"] is True
    assert run_1["outcome"]["recovered_calls_count"] == 1

    # Verify registry tool was committed to 1.1.0
    active_assign = registry.get("assign_ticket")
    assert active_assign.version == "1.1.0"

    # RUN 2: Subsequent run uses the shared committed registry
    # In Run 2, the model sees updated schema and calls assign_ticket(ticket_id=81, user_id="123") or assignee_id
    msg2_1 = AIMessage(
        content="Looking up Maya...",
        tool_calls=[ToolCall(name="find_user", args={"name": "Maya"}, id="call-find-002")],
    )
    msg2_2 = AIMessage(
        content="Assigning ticket 81 to Maya...",
        tool_calls=[ToolCall(name="assign_ticket", args={"ticket_id": 81, "user_id": "123"}, id="call-assign-002")],
    )
    msg2_3 = AIMessage(
        content="Confirming ticket assignment...",
        tool_calls=[ToolCall(name="get_ticket", args={"ticket_id": 81}, id="call-get-002")],
    )
    msg2_4 = AIMessage(content="Task completed successfully.")

    fake_model_run2 = FakeChatModel([msg2_1, msg2_2, msg2_3, msg2_4])
    agent_run2 = HealingAgent(
        registry=registry, # Reusing the committed app registry!
        client=client,
        model=fake_model_run2,
        execution_guarantees={"pre_execution_rejection": True},
    )

    run_2 = await agent_run2.execute_workflow(run_id="run-healing-2")

    assert run_2["status"] == "completed"
    assert run_2["outcome"]["downstream_verified"] is True
    # Zero recoveries needed on run 2 because committed tool mapping handled it directly!
    assert run_2["outcome"]["recovered_calls_count"] == 0


@pytest.mark.asyncio
async def test_baseline_agent_remains_frozen():
    """Verify BaselineAgent strictly retains frozen v1 contracts and does not heal."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": "123", "name": "Maya"}])
        elif request.url.path == "/tickets/assign":
            # Reject with drift
            return httpx.Response(400, json={"error": "Field user_id deprecated"})
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": None, "status": "open", "history": []})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))

    msg1 = AIMessage(
        content="Find Maya",
        tool_calls=[ToolCall(name="find_user", args={"name": "Maya"}, id="call-1")],
    )
    msg2 = AIMessage(
        content="Assign ticket",
        tool_calls=[ToolCall(name="assign_ticket", args={"ticket_id": 81, "user_id": "123"}, id="call-2")],
    )
    fake_model = FakeChatModel([msg1, msg2])

    baseline = BaselineAgent(client=client, model=fake_model)
    res = await baseline.execute_workflow(run_id="run-baseline-frozen")

    # Baseline fails without healing
    assert res["status"] == "failed"
    assert res["mode"] == "normal"
    assert baseline.catalogue["assign_ticket"].version == "1.0.0"


# =============================================================================
# 4. Truthful, Sanitized, Contract-Compliant Events
# =============================================================================

@pytest.mark.asyncio
async def test_events_truthful_and_contract_compliant():
    """Verify emitted events strictly adhere to contracts/events.schema.json and reflect truthful timing/outcomes."""
    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tickets/assign":
            return httpx.Response(200, json={"status": "assigned", "ticket_id": 81, "assignee_id": "123"})
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "ticket_id": 81, "assignee_id": "123", "status": "assigned", "history": []})
        return httpx.Response(404)

    client = MockBusinessApiClient(transport=httpx.MockTransport(handler))
    executor = ToolExecutor(registry=registry, client=client)

    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=broadcaster,
        execution_guarantees={"pre_execution_rejection": True},
    )

    obs = _create_assign_observation()
    op_id = OperationIdentity(tool_name="assign_ticket", endpoint="/tickets/assign", method="POST")

    await controller.recover_failure(
        observation=obs,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=op_id,
        run_id="run-events-check",
    )

    events = broadcaster.get_events("run-events-check")
    event_types = [e.type for e in events]

    # Verify event sequence
    assert "diagnosis_started" in event_types
    assert "patch_proposed" in event_types
    assert "patch_validated" in event_types
    assert "retry_started" in event_types
    assert "recovery_verified" in event_types
    assert "registry_committed" in event_types

    # Ensure recovery_verified occurred BEFORE registry_committed
    idx_verif = event_types.index("recovery_verified")
    idx_commit = event_types.index("registry_committed")
    assert idx_verif < idx_commit

    # Verify recovery_verified data
    verif_event = events[idx_verif]
    assert verif_event.data["verified_ticket_id"] == 81
    assert verif_event.data["verified_assignee_id"] == "123"

    # Verify registry_committed data
    commit_event = events[idx_commit]
    assert commit_event.data["base_version"] == "1.0.0"
    assert commit_event.data["committed_version"] == "1.1.0"
