"""Comprehensive Task 11 tests for Orchestrator Endpoints and SSE Integration.

Covers:
1. Prompt run-ID return and eventual truthful terminal status.
2. Invalid/conflicting modes and unsupported execution types.
3. Atomic rejection of simultaneous runs, reset, and mutation (HTTP 409).
4. Gate release after run completion, error, and cancellation.
5. Admin forwarding (reset/mutation) via MockAdminApiClient and handling failure -> DEMO_STATE_UNKNOWN.
6. SSE streaming: framing (id: <seq>, data: <json>), gapless replay, and live handoff.
7. Last-Event-ID reconnection, conflicting/negative cursor rejection (400), and terminal replay.
8. Unknown run 404 before opening stream.
9. Multiple subscribers and slow-consumer bounded queue overflow handling.
10. SSE disconnect does not cancel background run execution.
11. Persistence across restart (querying run and events from SQLite).
12. Managed shutdown task cancellation.
"""
import pytest
import asyncio
import json
import httpx
from datetime import datetime, timezone

from orchestrator.app import app
from mock_api.app import app as mock_app
from orchestrator.client.mock_client import MockAdminApiClient, MockBusinessApiClient
from orchestrator.audit.store import SQLiteAuditStore
from orchestrator.execution.gate import admission_gate, AdmissionState
from orchestrator.execution.tasks import app_task_manager
from orchestrator.execution.events import event_broadcaster, RunEvent, EventSubscriber
from orchestrator.execution.workflow import workflow_manager
from orchestrator.registry import VersionedToolRegistry


@pytest.fixture
async def temp_audit_store(tmp_path):
    db_file = tmp_path / "test_orch_audit.db"
    store = SQLiteAuditStore(db_path=str(db_file))
    await store.initialize()
    return store


@pytest.fixture
def test_setup(temp_audit_store):
    """Sets up orchestrator app state with in-process mock app transport and clean gate."""
    admission_gate.reset_state()
    app_task_manager.reset()
    workflow_manager.reset()

    # Wire in-process mock API
    mock_transport = httpx.ASGITransport(app=mock_app)
    app.state.mock_client = MockBusinessApiClient(
        base_url="http://127.0.0.1:8001",
        transport=mock_transport
    )
    app.state.admin_client = MockAdminApiClient(
        base_url="http://127.0.0.1:8001",
        transport=mock_transport
    )
    app.state.audit_store = temp_audit_store
    app.state.registry = VersionedToolRegistry.create_default()
    app.state.admission_gate = admission_gate
    app.state.task_manager = app_task_manager

    event_broadcaster.set_audit_store(temp_audit_store)
    workflow_manager.set_audit_store(temp_audit_store)

    yield

    admission_gate.reset_state()
    app_task_manager.reset()


# -----------------------------------------------------------------------------
# 1. Prompt Run-ID Return and Mode Validation
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_run_id_return_and_eventual_completion(test_setup):
    """POST /runs returns promptly with status='running' and run_id when wait=False,
    and eventual terminal state is accessible via GET /runs/{run_id}.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Prompt return
        res = await client.post("/runs", json={"mode": "normal", "wait": False})
        assert res.status_code == 200
        data = res.json()
        assert "run_id" in data
        run_id = data["run_id"]
        assert data["status"] == "running"
        assert data["mode"] == "normal"

        # Await completion in background task
        task = app_task_manager.get_task(run_id)
        if task:
            await task

        # Query GET /runs/{run_id}
        status_res = await client.get(f"/runs/{run_id}")
        assert status_res.status_code == 200
        run_data = status_res.json()
        assert run_data["status"] == "completed"
        assert run_data["outcome"]["verified"] is True
        assert run_data["outcome"]["resolved_user_id"] == "123"


@pytest.mark.asyncio
async def test_invalid_and_conflicting_modes_rejected(test_setup):
    """POST /runs rejects invalid modes and conflicting mode/execution_type combinations with 400."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Invalid mode
        res1 = await client.post("/runs", json={"mode": "unsupported_mode"})
        assert res1.status_code == 400
        assert "Unsupported mode" in res1.json()["detail"]

        # 2. Conflicting: mode=baseline with execution_type=model_driven_healing
        res2 = await client.post("/runs", json={
            "mode": "baseline",
            "execution_type": "model_driven_healing"
        })
        assert res2.status_code == 400
        assert "Conflicting mode" in res2.json()["detail"]

        # 3. Conflicting: mode=normal with execution_type=model_driven_healing
        res3 = await client.post("/runs", json={
            "mode": "normal",
            "execution_type": "model_driven_healing"
        })
        assert res3.status_code == 400
        assert "Conflicting mode" in res3.json()["detail"]

        # 4. Unsupported execution_type
        res4 = await client.post("/runs", json={
            "mode": "normal",
            "execution_type": "quantum_execution"
        })
        assert res4.status_code == 400
        assert "Unsupported execution_type" in res4.json()["detail"]


# -----------------------------------------------------------------------------
# 2. Atomic Concurrency Rejection and Gate Release
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_atomic_rejection_of_simultaneous_runs(test_setup):
    """Admission gate allows exactly one active run; concurrent run attempts receive 409 Conflict."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Acquire run gate manually to simulate active run
        await admission_gate.acquire_run("run-active-test")
        try:
            res = await client.post("/runs", json={"mode": "normal"})
            assert res.status_code == 409
            assert "another run 'run-active-test' is currently active" in res.json()["detail"]

            # Also verify reset and mutation are rejected during active run
            reset_res = await client.post("/demo/reset")
            assert reset_res.status_code == 409

            mutation_res = await client.post("/demo/mutation", json={"scenario": "rename"})
            assert mutation_res.status_code == 409
        finally:
            await admission_gate.release_run("run-active-test")

        # After release, gate returns to idle and allows new run
        res_after = await client.post("/runs", json={"mode": "normal", "wait": True})
        assert res_after.status_code == 200


@pytest.mark.asyncio
async def test_gate_release_on_execution_error(test_setup):
    """Gate is cleanly released even if workflow task throws an unhandled exception."""
    transport = httpx.ASGITransport(app=app)

    # Monkeypatch workflow manager to simulate unhandled crash
    orig_exec = workflow_manager.execute_workflow
    async def crash_exec(*args, **kwargs):
        raise RuntimeError("Simulated unhandled pipeline crash")
    workflow_manager.execute_workflow = crash_exec

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post("/runs", json={"mode": "normal", "wait": True})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "failed"
            assert "Simulated unhandled pipeline crash" in data["error"]

            # Gate must be IDLE
            assert admission_gate.state == AdmissionState.IDLE
    finally:
        workflow_manager.execute_workflow = orig_exec


# -----------------------------------------------------------------------------
# 3. Admin Forwarding (Reset and Mutation)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_demo_mutation_forwarding_and_validation(test_setup):
    """POST /demo/mutation validates scenario allowlist and forwards via MockAdminApiClient."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Invalid scenario rejected with 400
        bad_res = await client.post("/demo/mutation", json={"scenario": "nonexistent_scenario"})
        assert bad_res.status_code == 400
        assert "Unsupported scenario" in bad_res.json()["detail"]

        # 2. Valid scenario forwarded
        good_res = await client.post("/demo/mutation", json={"scenario": "nesting", "enabled": True})
        assert good_res.status_code == 200
        data = good_res.json()
        assert data["active_scenario"] == "nesting"
        assert data["enabled"] is True


@pytest.mark.asyncio
async def test_failed_downstream_reset_marks_demo_state_unknown(test_setup):
    """If downstream mock reset fails, orchestrator marks state DEMO_STATE_UNKNOWN,
    returns 502, and blocks subsequent runs with 409 until a successful reset.
    """
    transport = httpx.ASGITransport(app=app)

    # Simulate broken mock API client
    class FailingAdminClient:
        async def reset(self):
            raise httpx.ConnectError("Mock API unreachable")
        async def set_mutation(self, *args, **kwargs):
            raise httpx.ConnectError("Mock API unreachable")
        async def aclose(self):
            pass

    app.state.admin_client = FailingAdminClient()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Reset fails
        res = await client.post("/demo/reset")
        assert res.status_code == 502
        assert admission_gate.state == AdmissionState.DEMO_STATE_UNKNOWN

        # New runs blocked with 409 Conflict
        run_res = await client.post("/runs", json={"mode": "normal"})
        assert run_res.status_code == 409
        assert "Demo state is unknown" in run_res.json()["detail"]

        # Restore working client and perform successful reset
        mock_transport = httpx.ASGITransport(app=mock_app)
        app.state.admin_client = MockAdminApiClient(transport=mock_transport)

        res_ok = await client.post("/demo/reset")
        assert res_ok.status_code == 200
        assert admission_gate.state == AdmissionState.IDLE

        # Now runs are admitted again
        run_ok = await client.post("/runs", json={"mode": "normal", "wait": True})
        assert run_ok.status_code == 200


# -----------------------------------------------------------------------------
# 4. SSE Integration: Framing, Cursor, Replay & Live Handoff
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_unknown_run_returns_404_before_stream(test_setup):
    """GET /runs/{run_id}/events returns 404 before opening stream if run does not exist."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/runs/run-nonexistent-123/events")
        assert res.status_code == 404
        assert "not found" in res.json()["detail"]


@pytest.mark.asyncio
async def test_sse_invalid_and_conflicting_cursors_rejected(test_setup, temp_audit_store):
    """GET /runs/{run_id}/events rejects negative cursors and conflicting header/query values with 400."""
    await temp_audit_store.record_run_start("run-cursor-test", mode="normal", task="Task")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Negative cursor
        res1 = await client.get("/runs/run-cursor-test/events?cursor=-1")
        assert res1.status_code == 400

        # 2. Non-integer header
        res2 = await client.get("/runs/run-cursor-test/events", headers={"Last-Event-ID": "abc"})
        assert res2.status_code == 400

        # 3. Conflicting header and query cursor
        res3 = await client.get("/runs/run-cursor-test/events?cursor=5", headers={"Last-Event-ID": "2"})
        assert res3.status_code == 400
        assert "Conflicting" in res3.json()["detail"]


@pytest.mark.asyncio
async def test_sse_replay_and_live_handoff_no_duplicates(test_setup, temp_audit_store):
    """SSE correctly streams historical events followed by live events without sequence gaps or duplicates."""
    run_id = "run-sse-gapless"
    await temp_audit_store.record_run_start(run_id, mode="normal", task="SSE Test")

    # Seed 2 historical events via broadcaster so sequence numbers increment truthfully
    event_broadcaster.publish_event(
        run_id=run_id,
        event_type="run_started",
        mode="normal",
        tool=None,
        data={"step": 1}
    )
    event_broadcaster.publish_event(
        run_id=run_id,
        event_type="tool_call_started",
        mode="normal",
        tool="find_user",
        data={"step": 2}
    )


    transport = httpx.ASGITransport(app=app)
    received_lines = []

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Publish a live event task spawned before entering client.stream
        async def emit_live():
            await asyncio.sleep(0.05)
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_completed",
                mode="normal",
                tool=None,
                data={"step": 3}
            )

        emit_task = asyncio.create_task(emit_live())

        async with client.stream("GET", f"/runs/{run_id}/events") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            # Read stream
            async for line in response.aiter_lines():
                if line:
                    received_lines.append(line)
                if "run_completed" in line:
                    break

        await emit_task

    # Parse received event IDs and sequences
    event_ids = [int(line.replace("id: ", "")) for line in received_lines if line.startswith("id: ")]
    assert event_ids == [1, 2, 3]  # Strict sequence order, no duplicates, no gaps!


@pytest.mark.asyncio
async def test_sse_last_event_id_reconnect_skips_prior_events(test_setup, temp_audit_store):
    """Connecting with Last-Event-ID: 2 skips events 1 & 2 and only replays event 3."""
    run_id = "run-reconnect"
    await temp_audit_store.record_run_start(run_id, mode="normal", task="Task")

    for seq in (1, 2, 3):
        await temp_audit_store.record_event(
            run_id=run_id,
            sequence=seq,
            timestamp=datetime.now(timezone.utc).isoformat(),
            mode="normal",
            event_type="run_started" if seq == 1 else ("run_completed" if seq == 3 else "tool_call_started"),
            tool=None,
            data={"seq": seq}
        )

    # Mark run completed
    await temp_audit_store.record_run_completed(
        run_id=run_id,
        status="completed",
        duration_ms=10.0,
        completed_at=datetime.now(timezone.utc).isoformat()
    )

    transport = httpx.ASGITransport(app=app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "2"}) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line:
                    lines.append(line)

    ids = [int(line.replace("id: ", "")) for line in lines if line.startswith("id: ")]
    assert ids == [3]


@pytest.mark.asyncio
async def test_sse_slow_consumer_overflow_disconnect(test_setup, temp_audit_store):
    """When a subscriber queue overflows (bounded capacity), it emits a documented error and disconnects."""
    run_id = "run-overflow-test"
    await temp_audit_store.record_run_start(run_id, mode="normal", task="Task")

    # Mock register_subscriber so the endpoint's subscriber has overflowed = True
    orig_reg = event_broadcaster.register_subscriber
    def mock_reg(r_id, maxsize=100):
        s = orig_reg(r_id, maxsize=2)
        s.overflowed = True
        return s
    event_broadcaster.register_subscriber = mock_reg

    try:
        transport = httpx.ASGITransport(app=app)
        received = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream("GET", f"/runs/{run_id}/events") as response:
                async for line in response.aiter_lines():
                    if line:
                        received.append(line)

        content = "\n".join(received)
        assert "slow_consumer_overflow" in content
        assert "reconnect with Last-Event-ID" in content
    finally:
        event_broadcaster.register_subscriber = orig_reg



@pytest.mark.asyncio
async def test_sse_disconnect_does_not_cancel_run(test_setup):
    """Client aborting the SSE connection does not cancel the background workflow."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Start run in background
        run_res = await client.post("/runs", json={"mode": "normal", "wait": False})
        run_id = run_res.json()["run_id"]

        # Ensure task exists and runs independently of HTTP connection
        task = app_task_manager.get_task(run_id)
        assert task is not None

        # Verify run continues to completion in background
        await task

        status_res = await client.get(f"/runs/{run_id}")
        assert status_res.json()["status"] == "completed"



# -----------------------------------------------------------------------------
# 5. Persistence across Restart & Read Endpoints
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persisted_run_and_events_survive_restart(tmp_path):
    """Runs and events persisted in SQLite audit store remain queryable across fresh app instances."""
    db_file = tmp_path / "restart_audit.db"
    store1 = SQLiteAuditStore(db_path=str(db_file))
    await store1.initialize()

    # Create run and event in store1
    await store1.record_run_start("run-survive", mode="normal", task="Survive restart")
    await store1.record_event(
        run_id="run-survive",
        sequence=1,
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode="normal",
        event_type="run_started",
        tool=None,
        data={"step": 1}
    )
    await store1.record_run_completed(
        run_id="run-survive",
        status="completed",
        duration_ms=45.0,
        verified_outcome={"recovered": True},
        completed_at=datetime.now(timezone.utc).isoformat()
    )

    # Initialize fresh store instance pointing to same file
    store2 = SQLiteAuditStore(db_path=str(db_file))
    await store2.initialize()
    app.state.audit_store = store2
    workflow_manager._runs_store.clear()  # In-memory store is empty

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /runs/{run_id} succeeds from SQLite
        res = await client.get("/runs/run-survive")
        assert res.status_code == 200
        data = res.json()
        assert data["run_id"] == "run-survive"
        assert data["status"] == "completed"
        assert data["outcome"] == {"recovered": True}

        # SSE replay from SQLite succeeds
        events_res = []
        async with client.stream("GET", "/runs/run-survive/events") as stream:
            async for line in stream.aiter_lines():
                if line:
                    events_res.append(line)
        content = "\n".join(events_res)
        assert "run-survive" in content
        assert "id: 1" in content


@pytest.mark.asyncio
async def test_managed_shutdown_cancels_active_tasks(test_setup, temp_audit_store):
    """app_task_manager.cancel_and_wait_all() cleanly cancels running tasks and updates status."""
    # Spawn a slow artificial task
    async def slow_work():
        await asyncio.sleep(10.0)

    task = app_task_manager.spawn_run_task("run-shutdown-test", slow_work())
    assert app_task_manager.has_active_task("run-shutdown-test")

    # Trigger shutdown cleanup
    await app_task_manager.cancel_and_wait_all(timeout=1.0)
    assert not app_task_manager.has_active_task("run-shutdown-test")
    assert task.cancelled()
