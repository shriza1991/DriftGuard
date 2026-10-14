import pytest
from httpx import AsyncClient, ASGITransport
from mock_api.app import app


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
