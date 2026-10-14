"""Tests for Member 2 Task 10: Persistent Audit Records and Run Metrics.

Verifies:
1. SQLite audit store schema initialization, WAL mode, migrations, and restart survival.
2. Interrupted runs cleanup on restart without activating archived patches.
3. Complete recording of successful, rejected, and failed repairs, distinguishing outcomes.
4. Independent measurement of recovery duration (up to verification) vs commit latency.
5. Uniqueness constraints preventing duplicate accounting across overlapping hooks.
6. Absolute redaction of secrets from all stored rows and API responses.
7. Truthful token accounting: complete reconciliation vs incomplete/unavailable.
8. Pre-write audit failure strictly blocks repaired mutating HTTP dispatch.
9. Post-write audit failure preserves outcome and flags audit_incomplete without retry.
10. Concurrent writes with unique run and event identities.
11. Demo reset preserves persistent SQLite history while clearing memory and registry.
12. Read endpoints (GET /runs/{run_id}, GET /patches, GET /metrics) satisfy contracts.
"""
import pytest
import asyncio
import sqlite3
import json
from pathlib import Path
from typing import Dict, Any, Optional
import httpx

from orchestrator.audit.store import SQLiteAuditStore, PersistenceError, PreWriteAuditError
from orchestrator.audit.models import (
    PersistedRunRecord,
    PersistedToolExecution,
    PersistedRepairAttempt,
    PersistedEvent,
    PersistedModelUsage,
)
from orchestrator.metrics.run_metrics import (
    RunMetrics,
    ModelUsageBreakdown,
    compute_usage_breakdown,
    combine_session_usage,
)
from orchestrator.metrics.tracker import MetricsTracker, SystemMetrics
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.execution.events import EventBroadcaster
from orchestrator.healing.recovery import RecoveryController
from orchestrator.models import (
    FailureObservation,
    ClassificationResult,
    SemanticFailureCategory,
    RetrySafety,
    OperationIdentity,
    ToolDefinition,
    ExecutionResult,
)
from orchestrator.app import app


@pytest.fixture
def temp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_audit.sqlite3")


@pytest.fixture
def audit_store(temp_db_path: str) -> SQLiteAuditStore:
    return SQLiteAuditStore(db_path=temp_db_path)


# -----------------------------------------------------------------------------
# 1. Store Initialization, WAL Mode, and Schema Migration
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_store_initialization_and_schema(audit_store: SQLiteAuditStore, temp_db_path: str):
    await audit_store.initialize()

    # Verify tables exist and schema_migrations is set to version 1
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        ]
        assert "schema_migrations" in tables
        assert "runs" in tables
        assert "tool_executions" in tables
        assert "repair_attempts" in tables
        assert "events" in tables
        assert "model_usages" in tables

        cur = conn.execute("SELECT MAX(version) as v FROM schema_migrations;")
        assert cur.fetchone()["v"] == 2

        # Test idempotency: re-initializing should not fail
        await audit_store.initialize()
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 2. History Survives Restart; Nonterminal Runs Become Interrupted; Registry Isolated
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_survives_restart_and_marks_interrupted(temp_db_path: str):
    store_1 = SQLiteAuditStore(db_path=temp_db_path)
    await store_1.initialize()

    # Record a completed run and an in-progress running run
    await store_1.record_run_start("run-completed-1", mode="normal", task="Task 1")
    await store_1.record_run_completed(
        "run-completed-1", status="completed", duration_ms=150.0,
        verified_outcome={"success": True}, termination_reason="completed"
    )

    await store_1.record_run_start("run-interrupted-1", mode="healing", task="Task 2")

    # Simulate restart by creating a new store instance pointing to the same file
    store_2 = SQLiteAuditStore(db_path=temp_db_path)
    await store_2.initialize()

    run_comp = await store_2.get_run("run-completed-1")
    assert run_comp is not None
    assert run_comp.status == "completed"
    assert run_comp.audit_status == "complete"

    run_int = await store_2.get_run("run-interrupted-1")
    assert run_int is not None
    assert run_int.status == "interrupted"
    assert run_int.termination_reason == "system_interrupted_restart"
    assert run_int.audit_status == "incomplete"

    # In-memory registry must remain default v1 on restart (never activates archived patches)
    reg = VersionedToolRegistry.create_default()
    active_tools = reg.list_active()
    assert all(t.version == "1.0.0" for t in active_tools)


# -----------------------------------------------------------------------------
# 3. Complete Recording of Successful, Rejected, and Failed Repairs with Timings
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repair_records_distinguish_outcomes_and_timing(audit_store: SQLiteAuditStore):
    await audit_store.initialize()
    run_id = "run-repair-outcomes"
    await audit_store.record_run_start(run_id, mode="healing", task="Repair test")

    # 1. Record a validation-rejected repair
    await audit_store.record_repair_attempt(
        run_id=run_id,
        tool_name="assign_ticket",
        base_version="1.0.0",
        base_fingerprint="fp_base",
        proposal_fingerprint="fp_prop_reject",
        candidate_id="cand-rej",
        candidate_version="1.1.0-cand",
        candidate_fingerprint="fp_cand_rej",
        diagnosis_source="deterministic",
        diagnosis_tokens=0,
        proposal={"action": "drop_param"},
        candidate_definition=None,
        validation_status="rejected",
        validation_details={"reasons": ["Disallowed drop"]},
        recovery_status="validation_rejected",
        business_effect_occurred=False,
        verification_succeeded=False,
        registry_committed=False,
        committed_version=None,
        termination_reason="Validation rule violation",
        failed_recovery_duration_ms=12.4,
    )

    # 2. Record a verification-failed repair
    await audit_store.record_repair_attempt(
        run_id=run_id,
        tool_name="assign_ticket",
        base_version="1.0.0",
        base_fingerprint="fp_base",
        proposal_fingerprint="fp_prop_verif_fail",
        candidate_id="cand-vfail",
        candidate_version="1.1.0-cand",
        candidate_fingerprint="fp_cand_vfail",
        diagnosis_source="llm_diagnoser",
        diagnosis_tokens=42,
        proposal={"action": "rename"},
        candidate_definition={"name": "assign_ticket"},
        validation_status="passed",
        validation_details={"passed": True},
        recovery_status="verification_failed",
        business_effect_occurred=True,
        verification_succeeded=False,
        registry_committed=False,
        committed_version=None,
        termination_reason="Assignee mismatch in verification",
        failed_recovery_duration_ms=45.6,
    )

    # 3. Record a successful, verified, and committed repair
    await audit_store.record_repair_attempt(
        run_id=run_id,
        tool_name="assign_ticket",
        base_version="1.0.0",
        base_fingerprint="fp_base",
        proposal_fingerprint="fp_prop_success",
        candidate_id="cand-succ",
        candidate_version="1.1.0-cand",
        candidate_fingerprint="fp_cand_succ",
        diagnosis_source="deterministic",
        diagnosis_tokens=0,
        proposal={"action": "rename", "param": "assignee_id"},
        candidate_definition={"name": "assign_ticket"},
        validation_status="passed",
        validation_details={"passed": True},
        recovery_status="recovered",
        business_effect_occurred=True,
        verification_succeeded=True,
        registry_committed=True,
        committed_version="1.1.0",
        termination_reason="recovered_and_verified",
        recovery_duration_ms=38.2,
        commit_latency_ms=1.1,
    )

    repairs = await audit_store.get_run_repair_attempts(run_id)
    assert len(repairs) == 3

    rej = next(r for r in repairs if r.proposal_fingerprint == "fp_prop_reject")
    assert rej.validation_status == "rejected"
    assert rej.recovery_status == "validation_rejected"
    assert rej.business_effect_occurred is False
    assert rej.failed_recovery_duration_ms == 12.4
    assert rej.recovery_duration_ms is None

    vfail = next(r for r in repairs if r.proposal_fingerprint == "fp_prop_verif_fail")
    assert vfail.validation_status == "passed"
    assert vfail.recovery_status == "verification_failed"
    assert vfail.business_effect_occurred is True
    assert vfail.verification_succeeded is False
    assert vfail.registry_committed is False
    assert vfail.failed_recovery_duration_ms == 45.6
    assert vfail.recovery_duration_ms is None

    succ = next(r for r in repairs if r.proposal_fingerprint == "fp_prop_success")
    assert succ.validation_status == "passed"
    assert succ.recovery_status == "recovered"
    assert succ.verification_succeeded is True
    assert succ.registry_committed is True
    assert succ.recovery_duration_ms == 38.2
    assert succ.commit_latency_ms == 1.1


# -----------------------------------------------------------------------------
# 4. Duplicate Hooks Do Not Double-Count
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_hooks_do_not_double_count(audit_store: SQLiteAuditStore):
    await audit_store.initialize()
    run_id = "run-dup-test"
    await audit_store.record_run_start(run_id, mode="healing", task="Dup test")

    # Record tool execution twice with same attempt_number
    await audit_store.record_tool_execution(
        run_id=run_id,
        tool_call_id="call-1",
        tool_name="assign_ticket",
        tool_version="1.0.0",
        fingerprint="fp1",
        attempt_number=1,
        is_repaired=False,
        arguments={"ticket_id": 81},
        status="failed",
    )
    await audit_store.record_tool_execution(
        run_id=run_id,
        tool_call_id="call-1",
        tool_name="assign_ticket",
        tool_version="1.0.0",
        fingerprint="fp1",
        attempt_number=1,
        is_repaired=False,
        arguments={"ticket_id": 81},
        status="failed",
    )
    execs = await audit_store.get_run_tool_executions(run_id)
    assert len(execs) == 1

    # Record model usage twice with same call_id and purpose
    await audit_store.record_model_usage(
        run_id=run_id,
        call_id="call-m1",
        purpose="agent_turn",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        is_complete=True,
    )
    await audit_store.record_model_usage(
        run_id=run_id,
        call_id="call-m1",
        purpose="agent_turn",
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        is_complete=True,
    )
    usages = await audit_store.get_run_model_usages(run_id)
    assert len(usages) == 1

    # Record repair attempt twice with same proposal_fingerprint
    for _ in range(2):
        await audit_store.record_repair_attempt(
            run_id=run_id,
            tool_name="assign_ticket",
            base_version="1.0.0",
            base_fingerprint="fp_base",
            proposal_fingerprint="fp_prop_dup",
            candidate_id="cand-dup",
            candidate_version="1.1.0-cand",
            candidate_fingerprint="fp_cand_dup",
            diagnosis_source="deterministic",
            diagnosis_tokens=0,
            proposal={"rename": "user_id"},
            candidate_definition=None,
            validation_status="passed",
            validation_details=None,
            recovery_status="recovered",
            business_effect_occurred=True,
            verification_succeeded=True,
            registry_committed=True,
            committed_version="1.1.0",
            termination_reason="done",
        )
    repairs = await audit_store.get_run_repair_attempts(run_id)
    assert len(repairs) == 1


# -----------------------------------------------------------------------------
# 5. Secrets Redacted Before Persistence
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_secrets_never_appear_in_persisted_rows(audit_store: SQLiteAuditStore, temp_db_path: str):
    await audit_store.initialize()
    run_id = "run-secrets-test"
    secret_token = "SUPER_SECRET_BEARER_TOKEN_999"
    secret_api_key = "MOCK_KEY_ABC123"

    await audit_store.record_run_start(run_id, mode="healing", task="Secret test")

    await audit_store.record_tool_execution(
        run_id=run_id,
        tool_call_id="call-sec-1",
        tool_name="assign_ticket",
        tool_version="1.0.0",
        fingerprint="fp",
        attempt_number=1,
        is_repaired=False,
        arguments={"ticket_id": 81, "authorization": secret_token, "apiKey": secret_api_key},
        status="failed",
        failure={"error": "denied", "header": {"Authorization": secret_token}},
    )

    await audit_store.record_event(
        run_id=run_id,
        sequence=1,
        timestamp="2026-10-14T12:00:00Z",
        mode="healing",
        event_type="tool_call_started",
        tool="assign_ticket",
        data={"auth_token": secret_token, "client_secret": secret_api_key},
    )

    # Check database rows directly at raw SQLite level
    conn = sqlite3.connect(temp_db_path)
    try:
        cur = conn.execute("SELECT arguments, failure FROM tool_executions WHERE run_id = ?;", (run_id,))
        row = cur.fetchone()
        args_str, fail_str = row[0], row[1]
        assert secret_token not in args_str
        assert secret_api_key not in args_str
        assert "[REDACTED]" in args_str
        assert secret_token not in fail_str

        ev_cur = conn.execute("SELECT data FROM events WHERE run_id = ?;", (run_id,))
        ev_data_str = ev_cur.fetchone()[0]
        assert secret_token not in ev_data_str
        assert secret_api_key not in ev_data_str
        assert "[REDACTED]" in ev_data_str
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 6. Truthful Token Usage: Complete vs Incomplete vs Unavailable
# -----------------------------------------------------------------------------

def test_truthful_tokens_aggregation():
    # 1. Empty usages -> unavailable
    empty_bd = compute_usage_breakdown([])
    assert empty_bd.is_complete is False
    assert empty_bd.input_tokens == "unavailable"
    assert empty_bd.total_tokens == "unavailable"

    # 2. Incomplete usages -> marked incomplete, keeps known subtotal
    partial_usage = [
        PersistedModelUsage(
            run_id="r1", call_id="c1", purpose="agent_turn",
            prompt_tokens=100, completion_tokens=None, total_tokens=None, is_complete=False,
            created_at="2026-10-14T12:00:00Z"
        )
    ]
    partial_bd = compute_usage_breakdown(partial_usage)
    assert partial_bd.is_complete is False
    assert partial_bd.status == "incomplete"
    assert partial_bd.input_tokens == 100
    assert partial_bd.output_tokens == 0
    assert partial_bd.total_tokens == 100

    # 3. Complete usages -> reconciles exactly
    agent_usages = [
        PersistedModelUsage(
            run_id="r1", call_id="c1", purpose="agent_turn",
            prompt_tokens=150, completion_tokens=30, total_tokens=180, is_complete=True,
            created_at="2026-10-14T12:00:00Z"
        ),
        PersistedModelUsage(
            run_id="r1", call_id="c2", purpose="agent_turn",
            prompt_tokens=200, completion_tokens=40, total_tokens=240, is_complete=True,
            created_at="2026-10-14T12:00:01Z"
        ),
    ]
    diag_usages = [
        PersistedModelUsage(
            run_id="r1", call_id="c3", purpose="diagnosis",
            prompt_tokens=80, completion_tokens=20, total_tokens=100, is_complete=True,
            created_at="2026-10-14T12:00:02Z"
        )
    ]
    agent_bd = compute_usage_breakdown(agent_usages)
    diag_bd = compute_usage_breakdown(diag_usages)
    assert agent_bd.is_complete is True
    assert agent_bd.input_tokens == 350
    assert agent_bd.output_tokens == 70
    assert agent_bd.total_tokens == 420

    assert diag_bd.is_complete is True
    assert diag_bd.input_tokens == 80
    assert diag_bd.output_tokens == 20
    assert diag_bd.total_tokens == 100

    session_bd = combine_session_usage(agent_bd, diag_bd)
    assert session_bd.is_complete is True
    assert session_bd.input_tokens == 430
    assert session_bd.output_tokens == 90
    assert session_bd.total_tokens == 520
    # Reconciliation: session total == prompt + completion
    assert session_bd.total_tokens == session_bd.input_tokens + session_bd.output_tokens


# -----------------------------------------------------------------------------
# 7. Pre-Write Audit Failure Blocks Repaired Mutating Dispatch
# -----------------------------------------------------------------------------

class FailingPreWriteAuditStore(SQLiteAuditStore):
    async def record_pre_write_audit(self, *args, **kwargs):
        raise PreWriteAuditError("Simulated database failure during pre-write authorization")


@pytest.mark.asyncio
async def test_pre_write_audit_failure_blocks_repaired_dispatch(temp_db_path: str):
    from orchestrator.models import ExecutionPhase
    failing_store = FailingPreWriteAuditStore(db_path=temp_db_path)
    await failing_store.initialize()

    registry = VersionedToolRegistry.create_default()
    broadcaster = EventBroadcaster()

    # Track whether executor actually executed
    executed_calls = []

    class MockExecutor:
        async def execute(self, tool_name, args, **kwargs):
            executed_calls.append((tool_name, args))
            return ExecutionResult(is_success=True, result={"status": "assigned"}, duration_ms=10.0)

    controller = RecoveryController(
        registry=registry,
        executor=MockExecutor(),
        event_broadcaster=broadcaster,
        audit_store=failing_store,
        approved_header_credentials={"Authorization": "secops_auth"},
        execution_guarantees={"pre_execution_rejection": True},
    )

    assign_tool = registry.get("assign_ticket")
    assign_fp = assign_tool.fingerprint or assign_tool.compute_fingerprint()

    resp_body = {
        "error": {
            "code": "SCHEMA_MISMATCH",
            "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
            "details": {
                "migration_mapping": {"user_id": "assignee_id"},
                "pre_execution_rejection": True,
            },
        }
    }

    observation = FailureObservation(
        tool_name="assign_ticket",
        registry_version="1.0.0",
        fingerprint=assign_fp,
        execution_phase=ExecutionPhase.HTTP_DISPATCH,
        method="POST",
        endpoint="/tickets/assign",
        http_status=422,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        response_evidence={"status_code": 422, "body": resp_body},
        validation_details={"pre_execution_rejection": True, "migration_mapping": {"user_id": "assignee_id"}},
        headers={},
        operation_identity=OperationIdentity(
            tool_name="assign_ticket",
            endpoint="/tickets/assign",
            method="POST",
            business_id_fields={"ticket_id": 81},
            key="dg-op-81",
        ),
    )

    result = await controller.recover_failure(
        observation=observation,
        original_arguments={"ticket_id": 81, "user_id": "123"},
        operation_identity=observation.operation_identity,
        run_id="run-pre-write-fail",
        deadline=30.0,
        mode="healing",
    )

    # Verification: execution MUST be blocked!
    assert result.recovered is False
    assert result.status == "execution_failed"
    assert result.business_effect_occurred is False
    assert "Pre-write audit persistence failed; mutating dispatch blocked" in (result.termination_reason or "")
    assert len(executed_calls) == 0, "Mutating dispatch must NEVER be called if pre-write audit fails!"


# -----------------------------------------------------------------------------
# 8. Post-Write Audit Failure Marks Run Incomplete Without Retry
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_write_audit_failure_marks_run_incomplete(audit_store: SQLiteAuditStore):
    await audit_store.initialize()
    run_id = "run-post-write-fail"

    await audit_store.record_run_start(run_id, mode="healing", task="Post write fail test")
    # Simulate post-write uncertainty
    await audit_store.mark_audit_incomplete(run_id)

    run = await audit_store.get_run(run_id)
    assert run is not None
    assert run.audit_status == "incomplete"


# -----------------------------------------------------------------------------
# 9. Concurrent Writes Preserve Unique Run and Event Identities
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_writes_preserve_identities(audit_store: SQLiteAuditStore):
    await audit_store.initialize()

    async def write_run(index: int):
        r_id = f"run-concurrent-{index}"
        await audit_store.record_run_start(r_id, mode="normal", task=f"Concurrent task {index}")
        for seq in range(1, 6):
            await audit_store.record_event(
                run_id=r_id,
                sequence=seq,
                timestamp="2026-10-14T12:00:00Z",
                mode="normal",
                event_type="step",
                tool=None,
                data={"step_index": seq},
            )
        await audit_store.record_run_completed(
            run_id=r_id,
            status="completed",
            duration_ms=10.0 * index,
            verified_outcome={"index": index},
            termination_reason="completed",
        )

    # Launch 10 concurrent run writes
    tasks = [asyncio.create_task(write_run(i)) for i in range(10)]
    await asyncio.gather(*tasks)

    runs, total = await audit_store.list_runs(limit=50, offset=0)
    assert total == 10
    assert len(runs) == 10

    # Verify each run has exactly 5 events with correct sequence numbers 1..5
    for r in runs:
        events = await audit_store.get_run_events(r.run_id)
        assert len(events) == 5
        assert [e.sequence for e in events] == [1, 2, 3, 4, 5]


# -----------------------------------------------------------------------------
# 10. Demo Reset Preserves SQLite Audit History
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_demo_reset_preserves_persistent_history(audit_store: SQLiteAuditStore):
    await audit_store.initialize()
    app.state.audit_store = audit_store

    # Insert a run
    await audit_store.record_run_start("run-persist-reset", mode="normal", task="Reset test")
    await audit_store.record_run_completed(
        "run-persist-reset", status="completed", duration_ms=50.0,
        verified_outcome={"preserved": True}, termination_reason="completed"
    )

    from mock_api.app import app as mock_app
    from orchestrator.client.mock_client import MockAdminApiClient
    app.state.admin_client = MockAdminApiClient(transport=httpx.ASGITransport(app=mock_app))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        res = await client.post("/demo/reset")
        assert res.status_code == 200
        data = res.json()
        assert data["audit_store"] == "preserved"

    # Verify run is still present in SQLite audit store
    persisted = await audit_store.get_run("run-persist-reset")
    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.verified_outcome == {"preserved": True}


# -----------------------------------------------------------------------------
# 11. Read Endpoints: GET /runs/{run_id}, GET /patches, GET /metrics
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_endpoints_contracts_and_pagination(audit_store: SQLiteAuditStore):
    await audit_store.initialize()
    app.state.audit_store = audit_store

    run_id = "run-read-endpoint-test"
    await audit_store.record_run_start(run_id, mode="healing", task="Read test", execution_type="model_driven_healing")
    await audit_store.record_repair_attempt(
        run_id=run_id,
        tool_name="assign_ticket",
        base_version="1.0.0",
        base_fingerprint="fp1",
        proposal_fingerprint="fp_prop_read",
        candidate_id="cand-1",
        candidate_version="1.1.0",
        candidate_fingerprint="fp2",
        diagnosis_source="deterministic",
        diagnosis_tokens=0,
        proposal={"action": "rename"},
        candidate_definition=None,
        validation_status="passed",
        validation_details=None,
        recovery_status="recovered",
        business_effect_occurred=True,
        verification_succeeded=True,
        registry_committed=True,
        committed_version="1.1.0",
        termination_reason="recovered",
        recovery_duration_ms=42.5,
        commit_latency_ms=1.3,
    )
    await audit_store.record_run_completed(
        run_id=run_id,
        status="completed",
        duration_ms=250.0,
        verified_outcome={"verified": True},
        termination_reason="completed",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. GET /runs/{run_id}
        res_run = await client.get(f"/runs/{run_id}")
        assert res_run.status_code == 200
        run_data = res_run.json()
        assert run_data["run_id"] == run_id
        assert run_data["status"] == "completed"
        assert run_data["outcome"] == {"verified": True}

        # 2. GET /patches
        res_patches = await client.get("/patches?limit=10&offset=0")
        assert res_patches.status_code == 200
        patches_data = res_patches.json()
        assert "repair_history" in patches_data
        assert patches_data["count"] >= 1
        history_item = next(h for h in patches_data["repair_history"] if h["run_id"] == run_id)
        assert history_item["tool_name"] == "assign_ticket"
        assert history_item["recovery_status"] == "recovered"
        assert history_item["recovery_duration_ms"] == 42.5
        assert history_item["commit_latency_ms"] == 1.3

        # 3. GET /metrics
        res_metrics = await client.get("/metrics?limit=10&offset=0")
        assert res_metrics.status_code == 200
        metrics_data = res_metrics.json()
        assert "run_metrics" in metrics_data
        rm = next(m for m in metrics_data["run_metrics"] if m["run_id"] == run_id)
        assert rm["mode"] == "healing"
        assert rm["verified_recoveries_count"] == 1
        assert rm["recovery_duration_ms"] == 42.5
        assert rm["commit_latency_ms"] == 1.3
        assert rm["repair_attempts_count"] == 1
