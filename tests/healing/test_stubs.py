import pytest
from httpx import AsyncClient, ASGITransport
from orchestrator.app import app as orch_app
from mock_api.app import app as mock_app
from orchestrator.execution.tool_caller import http_tool_caller


@pytest.mark.asyncio
async def test_orchestrator_registry_and_metrics():
    transport = ASGITransport(app=orch_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Check registry
        reg_res = await client.get("/registry")
        assert reg_res.status_code == 200
        reg = reg_res.json()
        assert len(reg["tools"]) == 3
        tool_names = [t["name"] for t in reg["tools"]]
        assert "find_user" in tool_names
        assert "assign_ticket" in tool_names
        assert "get_ticket" in tool_names

        # Check patches endpoint returns explicit pending state
        patches_res = await client.get("/patches")
        assert patches_res.status_code == 200
        assert patches_res.json()["status"] == "pending_member_2"

        # Check metrics endpoint returns honest counts
        metrics_res = await client.get("/metrics")
        assert metrics_res.status_code == 200
        metrics = metrics_res.json()
        assert metrics["token_usage"] == "unavailable"
        assert metrics["status"] == "normal_scaffolding_active"


@pytest.mark.asyncio
async def test_orchestrator_mutation_forwarding():
    from orchestrator.client.mock_client import MockAdminApiClient
    orch_app.state.admin_client = MockAdminApiClient(transport=ASGITransport(app=mock_app))
    transport = ASGITransport(app=orch_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/demo/mutation", json={"scenario": "rename", "enabled": True})
        assert res.status_code == 200
        data = res.json()
        assert data["active_scenario"] == "rename"
        assert data["enabled"] is True


@pytest.mark.asyncio
async def test_normal_workflow_execution_end_to_end():
    # Setup mock app transport into tool caller
    mock_transport = ASGITransport(app=mock_app)
    mock_client = AsyncClient(transport=mock_transport, base_url="http://127.0.0.1:8001")
    
    # Reset mock API state
    await mock_client.post("/admin/reset")

    # Patch http_tool_caller base_url & client for test
    from orchestrator.execution import tool_caller
    orig_call = tool_caller.http_tool_caller.call_tool

    async def mock_call_tool(tool, arguments, idempotency_key=None):
        method = tool.method.upper()
        url = f"http://127.0.0.1:8001{tool.endpoint}"
        headers = dict(tool.headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        
        if method == "GET":
            if "{" in url:
                for k, v in arguments.items():
                    url = url.replace(f"{{{k}}}", str(v))
                res = await mock_client.get(url, headers=headers)
            else:
                res = await mock_client.get(url, params=arguments, headers=headers)
        elif method == "POST":
            res = await mock_client.post(url, json=arguments, headers=headers)
        
        if res.status_code >= 400:
            raise tool_caller.ToolExecutionError("Mock error", res.status_code, res.json())
        return res.json()

    tool_caller.http_tool_caller.call_tool = mock_call_tool

    try:
        orch_transport = ASGITransport(app=orch_app)
        async with AsyncClient(transport=orch_transport, base_url="http://test") as client:
            # Trigger run
            run_res = await client.post("/runs", json={"mode": "normal", "wait": True})
            assert run_res.status_code == 200
            run = run_res.json()
            assert run["status"] == "completed"
            assert run["outcome"]["target_user_name"] == "Maya"
            assert run["outcome"]["resolved_user_id"] == "123"
            assert run["outcome"]["verified_assignee_id"] == "123"
            assert run["outcome"]["verified"] is True

            # Verify metrics updated
            metrics_res = await client.get("/metrics")
            m = metrics_res.json()
            assert m["total_runs"] >= 1
            assert m["normal_success_count"] >= 1
    finally:
        tool_caller.http_tool_caller.call_tool = orig_call
        await mock_client.aclose()
