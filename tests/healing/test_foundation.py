"""Targeted verification checks for Member 2 Task 1 — Interfaces and foundation.
Uses httpx.MockTransport and local fixtures only; zero LLM or live network dependency.
"""
import json
import pytest
import jsonschema
from pathlib import Path
import httpx

from orchestrator.models import (
    ToolDefinition,
    ToolArgumentSchema,
    ExecutionResult,
    SanitizedFailure,
    RepairProposal,
    PatchDecision,
    RestrictedOperationType,
    RunStatus,
    RunEvent,
)
from orchestrator.client.mock_client import (
    MockBusinessApiClient,
    MockAdminApiClient,
    MockApiClientError,
    redact_secrets,
)
from orchestrator.fixtures.event_examples import (
    get_normal_run_example,
    get_tool_failure_example,
    get_patch_rejection_example,
    get_terminal_status_example,
)
from orchestrator.app import app


@pytest.fixture
def events_schema():
    schema_path = Path("contracts/events.schema.json")
    assert schema_path.exists(), "contracts/events.schema.json must exist"
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_typed_models_contract_compliance():
    """Verify internal models instantiate, validate types, and compute fingerprints."""
    tool = ToolDefinition(
        name="assign_ticket",
        version="1.0.0",
        description="Assigns ticket",
        endpoint="/tickets/assign",
        method="POST",
        argument_schema=ToolArgumentSchema(
            type="object",
            properties={
                "ticket_id": {"type": "integer"},
                "user_id": {"type": "string"}
            },
            required=["ticket_id", "user_id"]
        )
    )
    fp = tool.compute_fingerprint()
    assert len(fp) == 16
    assert isinstance(fp, str)

    # Test RepairProposal
    proposal = RepairProposal(
        proposal_id="prop-001",
        tool="assign_ticket",
        base_version="1.0.0",
        target_version="1.0.1",
        restricted_operations=[RestrictedOperationType.RENAME_ARGUMENT],
        evidence_references={"observed_code": "MISSING_REQUIRED_FIELD"},
        transform_spec={"rename": {"user_id": "assignee_id"}}
    )
    assert proposal.tool == "assign_ticket"
    assert proposal.restricted_operations[0] == RestrictedOperationType.RENAME_ARGUMENT

    # Test PatchDecision
    decision = PatchDecision(
        decision_id="dec-001",
        proposal_id="prop-001",
        accepted=True,
        status="accepted",
        reasons=["Deterministic rename verified against validation error"]
    )
    assert decision.accepted is True
    assert decision.status == "accepted"


def test_example_events_satisfy_schema(events_schema):
    """Verify all example fixtures satisfy contracts/events.schema.json and parse into RunEvent."""
    examples_lists = [
        get_normal_run_example(),
        get_tool_failure_example(),
        get_patch_rejection_example(),
        get_terminal_status_example()
    ]

    for event_list in examples_lists:
        assert isinstance(event_list, list)
        for ev in event_list:
            # Validate JSON Schema
            jsonschema.validate(instance=ev, schema=events_schema)
            # Validate Pydantic model
            parsed = RunEvent(**ev)
            assert parsed.run_id == ev["run_id"]
            assert parsed.sequence == ev["sequence"]
            assert parsed.type == ev["type"]


@pytest.mark.asyncio
async def test_business_requests_match_documented_endpoints():
    """Verify business client sends requests to documented endpoints using MockTransport."""
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        if request.url.path == "/users":
            return httpx.Response(200, json=[{"id": "123", "name": "Maya"}])
        elif request.url.path == "/tickets/assign":
            return httpx.Response(200, json={"ticket_id": 81, "status": "assigned", "assignee_id": "123"})
        elif request.url.path == "/tickets/81":
            return httpx.Response(200, json={"id": 81, "status": "assigned", "assignee_id": "123"})
        return httpx.Response(404, json={"error": "Not Found"})

    transport = httpx.MockTransport(handler)

    async with MockBusinessApiClient(base_url="http://mock.test", transport=transport) as client:
        # 1. find_user
        res_user = await client.find_user("Maya")
        assert res_user.is_success is True
        assert res_user.outcome["users"][0]["id"] == "123"
        assert captured_requests[-1].url.path == "/users"
        assert captured_requests[-1].url.query == b"name=Maya"

        # 2. assign_ticket
        res_assign = await client.assign_ticket(ticket_id=81, user_id="123", idempotency_key="idemp-key-99")
        assert res_assign.is_success is True
        assert captured_requests[-1].url.path == "/tickets/assign"
        assert captured_requests[-1].headers["Idempotency-Key"] == "idemp-key-99"
        sent_body = json.loads(captured_requests[-1].content)
        assert sent_body == {"ticket_id": 81, "user_id": "123"}

        # 3. get_ticket
        res_ticket = await client.get_ticket(81)
        assert res_ticket.is_success is True
        assert res_ticket.outcome["assignee_id"] == "123"
        assert captured_requests[-1].url.path == "/tickets/81"


@pytest.mark.asyncio
async def test_structured_api_errors_survive_normalization():
    """Verify structured validation errors from downstream are preserved in SanitizedFailure."""
    def handler(request: httpx.Request):
        return httpx.Response(
            status_code=422,
            json={
                "error": "ValidationFailed",
                "code": "SCHEMA_MISMATCH",
                "message": "Field 'user_id' renamed to 'assignee_id'",
                "details": {
                    "expected_schema": {"ticket_id": "integer", "assignee_id": "string"},
                    "received_payload": {"ticket_id": 81, "user_id": "123"}
                },
                "hint": "Migrated in v2: use assignee_id"
            }
        )

    transport = httpx.MockTransport(handler)

    async with MockBusinessApiClient(base_url="http://mock.test", transport=transport) as client:
        with pytest.raises(MockApiClientError) as exc_info:
            await client.assign_ticket(ticket_id=81, user_id="123")

        failure = exc_info.value.failure
        assert failure.is_drift is True
        assert failure.http_status == 422
        assert failure.error_code == "SCHEMA_MISMATCH"
        assert failure.message == "Field 'user_id' renamed to 'assignee_id'"
        assert failure.normalized_errors["expected_schema"]["assignee_id"] == "string"
        assert failure.migration_hint == "Migrated in v2: use assignee_id"
        assert failure.tool_name == "assign_ticket"
        assert failure.endpoint == "/tickets/assign"


@pytest.mark.asyncio
async def test_secrets_are_absent_from_failures_and_representations():
    """Verify tokens, authorization headers and secret credentials are redacted."""
    sensitive_token = "secret-super-auth-token-xyz123"

    def handler(request: httpx.Request):
        return httpx.Response(
            status_code=401,
            json={
                "error": "Unauthorized",
                "code": "INVALID_TOKEN",
                "message": "Provided token is invalid",
                "details": {
                    "received_token": sensitive_token,
                    "authorization": f"Bearer {sensitive_token}",
                    "api_key": "raw-key-abc"
                }
            }
        )

    transport = httpx.MockTransport(handler)

    async with MockBusinessApiClient(
        base_url="http://mock.test",
        transport=transport,
        credential_resolver=lambda ref: sensitive_token
    ) as client:
        with pytest.raises(MockApiClientError) as exc_info:
            await client.assign_ticket(81, "123", credential_ref="secops_auth")

        failure = exc_info.value.failure
        # Assert raw details and normalized errors are redacted
        assert sensitive_token not in str(failure.raw_details)
        assert sensitive_token not in str(failure.normalized_errors)
        assert failure.raw_details["details"]["received_token"] == "[REDACTED]"
        assert failure.raw_details["details"]["api_key"] == "[REDACTED]"

        # Assert representation of exception is redacted
        repr_str = repr(exc_info.value)
        assert sensitive_token not in repr_str
        assert "raw-key-abc" not in repr_str


def test_business_client_cannot_access_admin_controls():
    """Verify business client exposes NO admin methods (strict security boundary)."""
    client = MockBusinessApiClient(base_url="http://mock.test")

    # Business methods exist
    assert hasattr(client, "find_user")
    assert hasattr(client, "assign_ticket")
    assert hasattr(client, "get_ticket")

    # Admin methods must NOT exist on business client
    assert not hasattr(client, "reset")
    assert not hasattr(client, "admin_reset")
    assert not hasattr(client, "mutation")
    assert not hasattr(client, "set_mutation")
    assert not hasattr(client, "get_state")
    assert not hasattr(client, "admin_state")

    # Separate admin client exists for harness/admin operations
    admin_client = MockAdminApiClient(base_url="http://mock.test")
    assert hasattr(admin_client, "reset")
    assert hasattr(admin_client, "set_mutation")
    assert hasattr(admin_client, "get_state")


@pytest.mark.asyncio
async def test_client_cleanup_and_orchestrator_startup():
    """Verify client async cleanup and orchestrator non-blocking startup."""
    # Test client lifecycle cleanup
    client = MockBusinessApiClient(base_url="http://mock.test")
    assert client._owns_client is True
    await client.aclose()

    # Test orchestrator startup and health check without live mock API
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as test_client:
        res = await test_client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["service"] == "orchestrator"
        assert data["client_initialized"] is True
