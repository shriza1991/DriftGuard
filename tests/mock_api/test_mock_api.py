import pytest
from httpx import AsyncClient, ASGITransport
from mock_api.app import app
from mock_api.config import settings


@pytest.mark.asyncio
async def test_search_user_maya():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Reset first
        await client.post("/admin/reset")
        
        response = await client.get("/users?name=Maya")
        assert response.status_code == 200
        users = response.json()
        assert len(users) == 1
        assert users[0]["id"] == "123"
        assert users[0]["name"] == "Maya"


def test_assign_ticket_openapi_exposes_json_body_and_drift_examples():
    operation = app.openapi()["paths"]["/tickets/assign"]["post"]
    request_body = operation["requestBody"]
    assert request_body["required"] is True
    json_content = request_body["content"]["application/json"]
    assert json_content["examples"]["normal_v1"]["value"] == {
        "ticket_id": 81,
        "user_id": "123",
    }
    assert json_content["examples"]["combined"]["value"]["target"]["id"] == 123


@pytest.mark.asyncio
async def test_normal_ticket_assignment():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")

        # Assign ticket 81 to Maya (123)
        res = await client.post(
            "/tickets/assign",
            json={"ticket_id": 81, "user_id": "123"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["ticket_id"] == 81
        assert data["assignee_id"] == "123"
        assert data["status"] == "assigned"

        # Independent read verification
        read_res = await client.get("/tickets/81")
        assert read_res.status_code == 200
        ticket = read_res.json()
        assert ticket["assignee_id"] == "123"
        assert ticket["status"] == "assigned"
        assert len(ticket["history"]) == 1


@pytest.mark.asyncio
async def test_validation_errors_without_side_effects():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")

        # Missing user_id field
        res = await client.post(
            "/tickets/assign",
            json={"ticket_id": 81}
        )
        assert res.status_code == 422
        err = res.json()
        assert err["error"] == "ValidationFailed"
        assert err["code"] == "SCHEMA_MISMATCH"

        # Check that ticket 81 was NOT modified (Validate before write)
        ticket_res = await client.get("/tickets/81")
        assert ticket_res.json()["assignee_id"] is None
        assert len(ticket_res.json()["history"]) == 0

        # Type mismatch (ticket_id as string instead of int)
        type_res = await client.post(
            "/tickets/assign",
            json={"ticket_id": "not_an_int", "user_id": "123"}
        )
        assert type_res.status_code == 422
        assert type_res.json()["code"] == "TYPE_MISMATCH"


@pytest.mark.asyncio
async def test_idempotency_key_replay():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")

        key = "idemp-test-uuid-42"
        # First call
        res1 = await client.post(
            "/tickets/assign",
            headers={"Idempotency-Key": key},
            json={"ticket_id": 81, "user_id": "123"}
        )
        assert res1.status_code == 200
        assert res1.json()["idempotent_replayed"] is False

        # Replay with same key
        res2 = await client.post(
            "/tickets/assign",
            headers={"Idempotency-Key": key},
            json={"ticket_id": 81, "user_id": "123"}
        )
        assert res2.status_code == 200
        assert res2.json()["idempotent_replayed"] is True

        # Ensure history only has 1 record
        ticket_res = await client.get("/tickets/81")
        assert len(ticket_res.json()["history"]) == 1


@pytest.mark.asyncio
async def test_reset_restores_fixtures():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mutate state
        await client.post(
            "/tickets/assign",
            json={"ticket_id": 81, "user_id": "123"}
        )
        
        # Reset
        reset_res = await client.post("/admin/reset")
        assert reset_res.status_code == 200

        # Verify ticket is clean
        ticket_res = await client.get("/tickets/81")
        assert ticket_res.json()["assignee_id"] is None
        assert len(ticket_res.json()["history"]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "valid_body", "valid_headers"),
    [
        ("rename", {"ticket_id": 81, "assignee_id": "123"}, {}),
        ("type_change", {"ticket_id": 81, "user_id": 123}, {}),
        ("nesting", {"ticket_id": 81, "target": {"id": "123"}}, {}),
        (
            "required_header",
            {"ticket_id": 81, "user_id": "123"},
            {"Authorization": f"Bearer {settings.assignment_bearer_token}"},
        ),
        (
            "combined",
            {"ticket_id": 81, "target": {"id": 123}},
            {"Authorization": f"Bearer {settings.assignment_bearer_token}"},
        ),
    ],
)
@pytest.mark.parametrize("evidence_level", ["ordinary", "explicit_hint"])
async def test_recoverable_mutations_reject_stale_without_side_effect_and_accept_new_contract(
    scenario, valid_body, valid_headers, evidence_level
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")
        mutation = await client.post(
            "/admin/mutation",
            json={"scenario": scenario, "enabled": True, "evidence_level": evidence_level},
        )
        assert mutation.status_code == 200

        stale = await client.post(
            "/tickets/assign",
            headers={"Idempotency-Key": f"{scenario}-{evidence_level}-stale"},
            json={"ticket_id": 81, "user_id": "123"},
        )
        assert stale.status_code in {401, 422}
        evidence = stale.json()
        assert evidence["details"]["pre_execution_rejection"] is True
        assert "driftguard-demo-token" not in str(evidence)
        if evidence_level == "ordinary":
            assert evidence["hint"] is None
        else:
            assert evidence["hint"]

        unchanged = (await client.get("/tickets/81")).json()
        assert unchanged["assignee_id"] is None
        assert unchanged["history"] == []

        accepted = await client.post(
            "/tickets/assign",
            headers={**valid_headers, "Idempotency-Key": f"{scenario}-{evidence_level}-valid"},
            json=valid_body,
        )
        assert accepted.status_code == 200
        assert accepted.json()["assignee_id"] == "123"


@pytest.mark.asyncio
async def test_combined_failure_reports_all_required_changes_together():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")
        await client.post(
            "/admin/mutation",
            json={"scenario": "combined", "enabled": True, "evidence_level": "explicit_hint"},
        )
        response = await client.post(
            "/tickets/assign", json={"ticket_id": 81, "user_id": "123"}
        )
        assert response.status_code == 422
        details = response.json()["details"]
        assert details["required_headers"] == ["Authorization"]
        assert details["expected_schema"]["target"]["id"] == "integer"
        assert details["pre_execution_rejection"] is True
        assert "target.id" in response.json()["hint"]
        assert "Authorization" in response.json()["hint"]


@pytest.mark.asyncio
async def test_required_header_rejects_malformed_or_wrong_bearer_value():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")
        await client.post(
            "/admin/mutation", json={"scenario": "required_header", "enabled": True}
        )
        for value in [None, "Bearer", "Bearer wrong-token", "Basic abc"]:
            headers = {"Authorization": value} if value else {}
            response = await client.post(
                "/tickets/assign",
                headers=headers,
                json={"ticket_id": 81, "user_id": "123"},
            )
            assert response.status_code == 401
        assert (await client.get("/tickets/81")).json()["history"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("missing_business_value", "MISSING_BUSINESS_VALUE"),
        ("unavailable_credential", "UNAVAILABLE_CREDENTIAL"),
        ("ambiguous_mapping", "AMBIGUOUS_TARGET"),
    ],
)
async def test_safety_scenarios_refuse_without_business_side_effect(scenario, expected_code):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")
        await client.post(
            "/admin/mutation",
            json={"scenario": scenario, "enabled": True, "evidence_level": "explicit_hint"},
        )
        response = await client.post(
            "/tickets/assign", json={"ticket_id": 81, "user_id": "123"}
        )
        assert response.status_code in {403, 422}
        assert response.json()["code"] == expected_code
        assert response.json()["details"]["pre_execution_rejection"] is True
        ticket = (await client.get("/tickets/81")).json()
        assert ticket["assignee_id"] is None
        assert ticket["history"] == []


@pytest.mark.asyncio
async def test_idempotency_key_reuse_with_different_payload_is_conflict():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/admin/reset")
        key = "same-logical-key"
        first = await client.post(
            "/tickets/assign",
            headers={"Idempotency-Key": key},
            json={"ticket_id": 81, "user_id": "123"},
        )
        second = await client.post(
            "/tickets/assign",
            headers={"Idempotency-Key": key},
            json={"ticket_id": 82, "user_id": "456"},
        )
        assert first.status_code == 200
        assert second.status_code == 409
        assert second.json()["code"] == "IDEMPOTENCY_KEY_REUSE_MISMATCH"
        assert (await client.get("/tickets/82")).json()["history"] == []
