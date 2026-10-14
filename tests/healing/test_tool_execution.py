"""Targeted test suite for Member 2 Task 3 — Normal Tool Execution.

Tests strictly adhere to repository guidelines:
- Zero live services or LLM dependencies (pure httpx.MockTransport / local ASGI).
- Verifies:
  1. Invalid arguments cause ZERO HTTP calls.
  2. Original caller arguments remain unchanged.
  3. Declared path/query/body/header mappings drive requests.
  4. Candidate snapshot changes execution without altering active registry state.
  5. No silent type conversion or fabricated default fields.
  6. Missing credentials fail before HTTP; secrets never leak.
  7. Same logical operation preserves idempotency identity; distinct operations differ.
  8. Response mappings and validation extract data accurately.
  9. Post-write response parsing failure records uncertain_write=True with ZERO retries.
  10. Wrong assignee despite HTTP 200 fails workflow verification.
  11. Ambiguous or missing user stops workflow before any ticket assignment.
  12. Normal 3-tool workflow verifies the intended assignment end-to-end.
  13. Shared registry definitions persist across runs (no per-run reset).
"""
import copy
import json
import pytest
import httpx
from datetime import datetime, timezone

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    FailureCategory,
    ExecutionContext,
)
from orchestrator.registry import VersionedToolRegistry
from orchestrator.client.mock_client import MockBusinessApiClient, MockApiClientError
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.workflow import WorkflowExecutionManager


# =========================================================================
# Test 1: Invalid arguments cause zero HTTP calls
# =========================================================================
@pytest.mark.asyncio
async def test_invalid_arguments_cause_zero_http_calls():
    calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    # assign_ticket requires ticket_id: integer, user_id: string
    # Invalid: ticket_id is a string, user_id is missing
    invalid_args = {"ticket_id": "not_an_int"}
    result = await executor.execute("assign_ticket", invalid_args)

    assert result.is_success is False
    assert result.http_status == 0
    assert result.failure is not None
    assert result.failure.failure_category == FailureCategory.VALIDATION_FAILURE
    assert result.failure.error_code == "ARGUMENT_VALIDATION_ERROR"
    assert "ticket_id" in result.failure.normalized_errors or "user_id" in result.failure.normalized_errors

    # Zero HTTP requests dispatched
    assert len(calls) == 0


# =========================================================================
# Test 2: Original caller arguments remain unchanged (immutability)
# =========================================================================
@pytest.mark.asyncio
async def test_original_inputs_remain_unchanged():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    caller_args = {"ticket_id": 81, "user_id": "123", "extra_metadata": {"level": 1}}
    args_copy_before = copy.deepcopy(caller_args)

    result = await executor.execute("assign_ticket", caller_args)
    assert result.is_success is True

    # Caller args must remain strictly identical
    assert caller_args == args_copy_before
    assert caller_args["extra_metadata"] == {"level": 1}


# =========================================================================
# Test 3: Declared path/query/body/header mappings drive requests
# =========================================================================
@pytest.mark.asyncio
async def test_declared_mappings_drive_requests():
    recorded_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        if "/users" in str(request.url):
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@test.com"}])
        elif "/tickets/assign" in str(request.url):
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})
        elif "/tickets/81" in str(request.url):
            return httpx.Response(200, json={"id": 81, "status": "assigned", "assignee_id": "123", "history": []})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    # 1. find_user -> GET query params
    await executor.execute("find_user", {"name": "Maya"})
    req1 = recorded_requests[0]
    assert req1.method == "GET"
    assert req1.url.path == "/users"
    assert req1.url.query.decode() == "name=Maya"
    assert req1.headers.get("Accept") == "application/json"

    # 2. assign_ticket -> POST body & headers
    await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})
    req2 = recorded_requests[1]
    assert req2.method == "POST"
    assert req2.url.path == "/tickets/assign"
    assert req2.headers.get("Content-Type") == "application/json"
    assert "Idempotency-Key" in req2.headers
    body2 = json.loads(req2.content.decode())
    assert body2 == {"ticket_id": 81, "user_id": "123"}

    # 3. get_ticket -> GET path params
    await executor.execute("get_ticket", {"ticket_id": 81})
    req3 = recorded_requests[2]
    assert req3.method == "GET"
    assert req3.url.path == "/tickets/81"


# =========================================================================
# Test 4: Candidate snapshot changes execution without changing active registry
# =========================================================================
@pytest.mark.asyncio
async def test_candidate_snapshot_changes_execution_without_mutating_registry():
    recorded_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    # Construct staged candidate definition with nested body mapping
    candidate_def = ToolDefinition(
        name="assign_ticket",
        version="1.1.0",
        description="Candidate with nested target object",
        endpoint="/tickets/assign",
        method="POST",
        headers={"Content-Type": "application/json"},
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "ticket_id": {"type": "integer"},
                "user_id": {"type": "string"}
            },
            required=["ticket_id", "user_id"]
        ),
        request_mapping={
            "body": {
                "ticket_id": "{ticket_id}",
                "target": {"id": "{user_id}"}
            }
        },
        response_mapping={"status": "$.status"}
    )
    candidate_def.fingerprint = candidate_def.compute_fingerprint()

    # Execute using explicit candidate_snapshot
    res = await executor.execute(
        "assign_ticket",
        {"ticket_id": 81, "user_id": "123"},
        candidate_snapshot=candidate_def
    )
    assert res.is_success is True
    assert res.registry_version == "1.1.0"
    assert res.fingerprint == candidate_def.fingerprint

    # Inspect the dispatched request: it should have used candidate's nested body
    sent_body = json.loads(recorded_requests[0].content.decode())
    assert sent_body == {"ticket_id": 81, "target": {"id": "123"}}

    # Verify active registry is UNCHANGED (still 1.0.0 with flat body)
    active_def = registry.get("assign_ticket")
    assert active_def.version == "1.0.0"
    assert active_def.request_mapping["body"] == {"ticket_id": "{ticket_id}", "user_id": "{user_id}"}


# =========================================================================
# Test 5: No silent type conversion or fabricated fields
# =========================================================================
@pytest.mark.asyncio
async def test_no_silent_type_conversion_or_fabricated_fields():
    sent_body = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_body
        sent_body = json.loads(request.content.decode())
        return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    # Assert exact types: ticket_id is integer (not str '81'), user_id is string
    assert isinstance(sent_body["ticket_id"], int)
    assert sent_body["ticket_id"] == 81
    assert isinstance(sent_body["user_id"], str)
    assert sent_body["user_id"] == "123"

    # Assert no extra or fabricated fields
    assert set(sent_body.keys()) == {"ticket_id", "user_id"}


# =========================================================================
# Test 6: Missing credentials fail before HTTP; secrets never leak
# =========================================================================
@pytest.mark.asyncio
async def test_missing_credentials_fail_before_http_and_secrets_never_leak():
    calls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})

    transport = httpx.MockTransport(mock_handler)
    # Resolver returns None (missing credential)
    client = MockBusinessApiClient(
        base_url="http://mock.test",
        transport=transport,
        credential_resolver=lambda ref: None
    )
    registry = VersionedToolRegistry.create_default()

    # Modify tool in registry copy to require credential
    tool_with_cred = registry.get("assign_ticket")
    tool_with_cred.credential_reference = "vault_secops_token"

    executor = ToolExecutor(registry=registry, client=client)
    res = await executor.execute(tool_with_cred, {"ticket_id": 81, "user_id": "123"})

    assert res.is_success is False
    assert res.http_status == 0
    assert res.failure.failure_category == FailureCategory.CREDENTIAL_FAILURE
    assert res.failure.error_code == "MISSING_CREDENTIAL"
    # Zero HTTP requests made
    assert len(calls) == 0

    # Now test with resolved secret: ensure secret does not leak in result representations
    secret_value = "super_secret_auth_token_9999"
    client_with_auth = MockBusinessApiClient(
        base_url="http://mock.test",
        transport=transport,
        credential_resolver=lambda ref: secret_value if ref == "vault_secops_token" else None
    )
    executor_with_auth = ToolExecutor(registry=registry, client=client_with_auth)
    res_auth = await executor_with_auth.execute(tool_with_cred, {"ticket_id": 81, "user_id": "123"})
    assert res_auth.is_success is True

    # Downstream request received the header
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == f"Bearer {secret_value}"

    # Secret string must NOT appear in ExecutionResult output or string representations
    res_str = str(res_auth.model_dump())
    assert secret_value not in res_str


# =========================================================================
# Test 7: Idempotency identity preserved on replay, distinct for different ops
# =========================================================================
@pytest.mark.asyncio
async def test_idempotency_identity_handling():
    dispatched_headers = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        dispatched_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    stable_key = "idemp-logical-ticket-81-assign"
    ctx1 = ExecutionContext(run_id="run-A", idempotency_key=stable_key)

    # 1st call with ctx1
    res1 = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"}, context=ctx1)
    # 2nd call (replay of same logical operation) with ctx1
    res2 = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"}, context=ctx1)

    assert dispatched_headers[0]["idempotency-key"] == stable_key
    assert dispatched_headers[1]["idempotency-key"] == stable_key

    # Distinct operation without pre-set key receives a different idempotency key
    ctx2 = ExecutionContext(run_id="run-B")
    res3 = await executor.execute("assign_ticket", {"ticket_id": 82, "user_id": "456"}, context=ctx2)
    key3 = dispatched_headers[2]["idempotency-key"]

    assert key3 != stable_key
    assert key3.startswith("dg-idemp-")
    # run_id is NOT conflated with idempotency_key
    assert key3 != "run-B"


# =========================================================================
# Test 8: Response mappings extract declared fields accurately
# =========================================================================
@pytest.mark.asyncio
async def test_response_mappings_extraction():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/users" in str(request.url):
            return httpx.Response(200, json=[
                {"id": "123", "name": "Maya", "email": "maya@company.org", "department": "Security"}
            ])
        elif "/tickets/81" in str(request.url):
            return httpx.Response(200, json={
                "id": 81,
                "title": "System alert",
                "status": "assigned",
                "assignee_id": "123",
                "history": [{"action": "assigned", "assignee_id": "123"}]
            })
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    # find_user mapping: $[0].id -> user_id, $[0].name -> name, $[0].email -> email
    res_user = await executor.execute("find_user", {"name": "Maya"})
    assert res_user.is_success is True
    assert res_user.outcome == {
        "user_id": "123",
        "name": "Maya",
        "email": "maya@company.org"
    }

    # get_ticket mapping: $.id -> ticket_id, $.status -> status, $.assignee_id -> assignee_id, $.history -> history
    res_ticket = await executor.execute("get_ticket", {"ticket_id": 81})
    assert res_ticket.is_success is True
    assert res_ticket.outcome["ticket_id"] == 81
    assert res_ticket.outcome["status"] == "assigned"
    assert res_ticket.outcome["assignee_id"] == "123"
    assert len(res_ticket.outcome["history"]) == 1


# =========================================================================
# Test 9: Post-write response failure causes no retry and records uncertainty
# =========================================================================
@pytest.mark.asyncio
async def test_post_write_response_failure_records_uncertainty_and_no_retries():
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # HTTP 200, but HTML/unparseable body instead of JSON
        return httpx.Response(200, content=b"<html>Bad Gateway Proxy Response</html>", headers={"Content-Type": "text/html"})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    executor = ToolExecutor(registry=registry, client=client)

    result = await executor.execute("assign_ticket", {"ticket_id": 81, "user_id": "123"})

    assert result.is_success is False
    assert result.http_status == 200
    assert result.failure is not None
    assert result.failure.failure_category == FailureCategory.RESPONSE_PARSING_FAILURE
    # A write succeeded downstream but response was unparseable: MUST record uncertain_write
    assert result.failure.uncertain_write is True

    # Must NOT automatically retry a write
    assert call_count == 1


# =========================================================================
# Test 10: Wrong assignee despite HTTP 200 fails workflow verification
# =========================================================================
@pytest.mark.asyncio
async def test_wrong_assignee_despite_http_200_fails_verification():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/users" in str(request.url):
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@test.com"}])
        elif "/tickets/assign" in str(request.url):
            # Assignment returned 200 OK
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})
        elif "/tickets/81" in str(request.url):
            # But read verification shows assignee is someone else! (e.g. 999)
            return httpx.Response(200, json={"id": 81, "status": "assigned", "assignee_id": "999", "history": []})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    manager = WorkflowExecutionManager(registry=registry, client=client)

    run = await manager.execute_workflow(run_id="run-verify-fail", mode="normal")

    # Workflow MUST fail verification despite HTTP 200 on all calls
    assert run["status"] == "failed"
    assert run["outcome"]["verified"] is False
    assert "Verification mismatch" in run["error"]
    assert "999" in run["error"]


# =========================================================================
# Test 11: Ambiguous or missing user stops workflow before assignment
# =========================================================================
@pytest.mark.asyncio
async def test_ambiguous_or_missing_user_stops_workflow_before_assignment():
    assign_called = False

    def mock_missing_user(request: httpx.Request) -> httpx.Response:
        nonlocal assign_called
        if "/users" in str(request.url):
            return httpx.Response(200, json=[])  # Empty directory
        elif "/tickets/assign" in str(request.url):
            assign_called = True
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned"})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_missing_user)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    manager = WorkflowExecutionManager(client=client)

    run = await manager.execute_workflow(run_id="run-missing-user", mode="normal")

    assert run["status"] == "failed"
    assert "Could not find user Maya" in run["error"]
    assert assign_called is False

    # Also test multi-user ambiguity
    def mock_multi_users(request: httpx.Request) -> httpx.Response:
        nonlocal assign_called
        if "/users" in str(request.url):
            return httpx.Response(200, json=[
                {"id": "123", "name": "Maya", "email": "maya1@test.com"},
                {"id": "456", "name": "Maya", "email": "maya2@test.com"}
            ])
        elif "/tickets/assign" in str(request.url):
            assign_called = True
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned"})
        return httpx.Response(404, json={})

    transport_multi = httpx.MockTransport(mock_multi_users)
    client_multi = MockBusinessApiClient(base_url="http://mock.test", transport=transport_multi)
    manager_multi = WorkflowExecutionManager(client=client_multi)
    run_multi = await manager_multi.execute_workflow(run_id="run-multi-user", mode="normal")

    assert run_multi["status"] == "failed"
    assert "Ambiguous user resolution" in run_multi["error"]
    assert assign_called is False


# =========================================================================
# Test 12: Normal three-tool workflow verifies intended assignment end-to-end
# =========================================================================
@pytest.mark.asyncio
async def test_normal_three_tool_workflow_verified():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/users" in str(request.url):
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@test.com"}])
        elif "/tickets/assign" in str(request.url):
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})
        elif "/tickets/81" in str(request.url):
            return httpx.Response(200, json={"id": 81, "status": "assigned", "assignee_id": "123", "history": []})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    registry = VersionedToolRegistry.create_default()
    manager = WorkflowExecutionManager(registry=registry, client=client)

    run = await manager.execute_workflow(run_id="run-normal-success", mode="normal")

    assert run["status"] == "completed"
    assert run["outcome"]["verified"] is True
    assert run["outcome"]["target_user_name"] == "Maya"
    assert run["outcome"]["resolved_user_id"] == "123"
    assert run["outcome"]["verified_assignee_id"] == "123"
    assert run["outcome"]["ticket_id"] == 81
    assert run["outcome"]["execution_type"] == "deterministic_normal_workflow"


# =========================================================================
# Test 13: Workflows share injected registry; committed definitions remain visible
# =========================================================================
@pytest.mark.asyncio
async def test_shared_registry_persists_committed_definitions_across_runs():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/users" in str(request.url):
            return httpx.Response(200, json=[{"id": "123", "name": "Maya", "email": "maya@test.com"}])
        elif "/tickets/assign" in str(request.url):
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})
        elif "/tickets/81" in str(request.url):
            return httpx.Response(200, json={"id": 81, "status": "assigned", "assignee_id": "123", "history": []})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(mock_handler)
    client = MockBusinessApiClient(base_url="http://mock.test", transport=transport)
    shared_registry = VersionedToolRegistry.create_default()

    # Stage and commit a new candidate on the shared registry
    candidate_def = shared_registry.get("assign_ticket")
    candidate_def.description = "Updated description for commit"
    cand = await shared_registry.stage_candidate("assign_ticket", candidate_def, base_version="1.0.0")
    committed = await shared_registry.commit_candidate(cand.candidate_id, expected_base_version="1.0.0")
    assert committed.version == "1.1.0"

    manager = WorkflowExecutionManager(registry=shared_registry, client=client)

    # First run
    run1 = await manager.execute_workflow(run_id="run-shared-1")
    assert run1["status"] == "completed"
    # Registry must NOT have been reset to 1.0.0
    assert manager.get_registry().get("assign_ticket").version == "1.1.0"

    # Second run
    run2 = await manager.execute_workflow(run_id="run-shared-2")
    assert run2["status"] == "completed"
    assert manager.get_registry().get("assign_ticket").version == "1.1.0"
