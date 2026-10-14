import json
from pathlib import Path
import jsonschema
import pytest
import aiosqlite
import httpx
from datetime import datetime, timezone, timedelta

from evaluation.scenarios import SCENARIO_CATALOG
from evaluation.reporting import EvaluationReporter
from evaluation.runner import BenchmarkRunRequest, BenchmarkRunner


def test_scenario_catalog_integrity():
    assert len(SCENARIO_CATALOG) == 8
    scenario_ids = [s.id for s in SCENARIO_CATALOG]
    expected_ids = [
        "rename",
        "type_change",
        "nesting",
        "required_header",
        "combined",
        "missing_business_value",
        "unavailable_credential",
        "ambiguous_mapping"
    ]
    assert scenario_ids == expected_ids

    # Check that safety scenarios are classified as safe_refusal
    safety_negatives = [s for s in SCENARIO_CATALOG if s.category == "safety_negative"]
    assert len(safety_negatives) == 3
    for s in safety_negatives:
        assert s.expected_outcome == "safe_refusal"


def test_events_schema_validation():
    schema_path = Path("contracts/events.schema.json")
    assert schema_path.exists()
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    example_path = Path("contracts/examples/run_events_normal.json")
    assert example_path.exists()
    with open(example_path, "r", encoding="utf-8") as f:
        events = json.load(f)

    assert isinstance(events, list)
    assert len(events) >= 5

    for event in events:
        jsonschema.validate(instance=event, schema=schema)


def test_evaluation_reporter_no_fabricated_data():
    summary = EvaluationReporter.generate_summary([])
    assert summary["status"] == "no_data"
    assert summary["token_usage"] == "unavailable"


def test_evaluation_reporter_separates_recovery_from_safe_refusal_denominators():
    trials = [
        {
            "expected_outcome": "heal_and_recover",
            "verified_recovery": True,
            "recovery_time_ms": 100.0,
            "total_tokens": 20,
        },
        {
            "expected_outcome": "heal_and_recover",
            "verified_recovery": False,
            "recovery_time_ms": None,
            "total_tokens": None,
        },
        {
            "expected_outcome": "safe_refusal",
            "safe_refusal": True,
            "total_tokens": 10,
        },
    ]
    summary = EvaluationReporter.generate_summary(trials)
    assert summary["recovery_rate_percent"] == 50.0
    assert summary["safe_refusal_rate_percent"] == 100.0
    assert summary["median_recovery_latency_ms"] == 100.0
    assert summary["p95_recovery_latency_ms"] is None
    assert summary["token_usage"] == "partially_available"


def test_benchmark_request_rejects_unknown_scenarios_and_evidence_levels():
    with pytest.raises(ValueError):
        BenchmarkRunRequest(scenarios=["not-real"])
    with pytest.raises(ValueError):
        BenchmarkRunRequest(evidence_levels=["secret_admin_answer"])


@pytest.mark.asyncio
async def test_benchmark_runner_executes_paired_trials_and_persists_results(tmp_path):
    state = {
        "scenario": None,
        "assignee_id": None,
        "history": [],
        "run_counter": 0,
        "run_modes": {},
    }

    def mock_api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/mutation" and request.method == "POST":
            payload = json.loads(request.content)
            state["scenario"] = payload["scenario"]
            return httpx.Response(200, json={"active_scenario": payload["scenario"], "enabled": True})
        if request.url.path == "/admin/state" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "users_count": 2,
                    "tickets": {
                        "81": {
                            "id": 81,
                            "title": "Test",
                            "status": "assigned" if state["assignee_id"] else "open",
                            "assignee_id": state["assignee_id"],
                            "history": list(state["history"]),
                        }
                    },
                    "active_mutation": {"scenario": state["scenario"], "enabled": True},
                    "idempotency_records_count": 0,
                },
            )
        return httpx.Response(404)

    def orchestrator_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/demo/reset" and request.method == "POST":
            state["assignee_id"] = None
            state["history"] = []
            return httpx.Response(
                200,
                json={"status": "reset_completed", "orchestrator": "state_cleared", "mock_api": "reset_successful"},
            )
        if request.url.path == "/registry" and request.method == "GET":
            return httpx.Response(200, json={"tools": [{"name": "assign_ticket", "version": "1.0.0"}]})
        if request.url.path == "/runs" and request.method == "POST":
            payload = json.loads(request.content)
            state["run_counter"] += 1
            run_id = f"run-{state['run_counter']}"
            mode = payload["mode"]
            state["run_modes"][run_id] = mode
            is_unsafe = state["scenario"] == "ambiguous_mapping"
            if mode == "healing" and not is_unsafe:
                state["assignee_id"] = "123"
                state["history"] = [{"action": "assigned", "assignee_id": "123"}]
                status_value = "completed"
                error = None
            else:
                status_value = "failed"
                error = "safe refusal" if is_unsafe else "schema mismatch"
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "status": status_value,
                    "mode": mode,
                    "task": "task",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_ms": 12.5,
                    "outcome": None,
                    "error": error,
                    "model_usage": {"total_tokens": 25, "diagnosis_tokens": 0},
                    "termination_reason": "completed_and_verified" if status_value == "completed" else "refused",
                    "execution_type": payload["execution_type"],
                },
            )
        if request.url.path.startswith("/runs/") and request.url.path.endswith("/events"):
            started = datetime.now(timezone.utc)
            finished = started + timedelta(milliseconds=50)
            body = (
                "data: " + json.dumps({"type": "tool_call_failed", "timestamp": started.isoformat()}) + "\n\n"
                "data: " + json.dumps({"type": "recovery_verified", "timestamp": finished.isoformat()}) + "\n\n"
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(orchestrator_handler), base_url="http://orchestrator"
    ) as orchestrator_client, httpx.AsyncClient(
        transport=httpx.MockTransport(mock_api_handler), base_url="http://mock"
    ) as mock_client:
        database_path = tmp_path / "evaluation.sqlite3"
        runner = BenchmarkRunner(
            database_path=str(database_path),
            orchestrator_client=orchestrator_client,
            mock_admin_client=mock_client,
        )
        result = await runner.run_benchmarks(
            BenchmarkRunRequest(
                scenarios=["rename", "ambiguous_mapping"],
                evidence_levels=["explicit_hint"],
                trials_per_scenario=1,
            )
        )

    assert result.status == "completed"
    assert result.execution_completed is True
    assert result.all_expected_outcomes_met is True
    assert result.evaluated_trials == 4
    assert result.comparison_pairs == 2
    assert result.driftguard_recovery_rate == 100.0
    assert result.safe_refusal_rate == 100.0
    assert result.baseline_success_rate == 0.0
    assert result.median_recovery_time_ms == 50.0
    assert result.token_usage_available_trials == 4

    async with aiosqlite.connect(database_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM evaluation_trials")
        row = await cursor.fetchone()
    assert row[0] == 4


@pytest.mark.asyncio
async def test_benchmark_runner_polls_async_runs_to_terminal_state(tmp_path):
    state = {"assignee_id": None, "history": [], "mode": None, "polls": 0}

    def mock_api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/mutation":
            return httpx.Response(200, json={"active_scenario": "rename", "enabled": True})
        if request.url.path == "/admin/state":
            return httpx.Response(
                200,
                json={
                    "tickets": {
                        "81": {
                            "id": 81,
                            "status": "assigned" if state["assignee_id"] else "open",
                            "assignee_id": state["assignee_id"],
                            "history": list(state["history"]),
                        }
                    }
                },
            )
        return httpx.Response(404)

    def orchestrator_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/demo/reset":
            state.update(assignee_id=None, history=[], polls=0)
            return httpx.Response(200, json={"mock_api": "reset_successful"})
        if request.url.path == "/registry":
            return httpx.Response(
                200, json={"tools": [{"name": "assign_ticket", "version": "1.0.0"}]}
            )
        if request.url.path == "/runs" and request.method == "POST":
            state["mode"] = json.loads(request.content)["mode"]
            return httpx.Response(200, json={"run_id": "run-async", "status": "running"})
        if request.url.path == "/runs/run-async" and request.method == "GET":
            state["polls"] += 1
            if state["mode"] == "healing":
                state["assignee_id"] = "123"
                state["history"] = [{"action": "assigned", "assignee_id": "123"}]
                return httpx.Response(200, json={"run_id": "run-async", "status": "completed"})
            return httpx.Response(200, json={"run_id": "run-async", "status": "failed"})
        if request.url.path == "/runs/run-async/events":
            return httpx.Response(200, text="")
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(orchestrator_handler), base_url="http://orchestrator"
    ) as orchestrator_client, httpx.AsyncClient(
        transport=httpx.MockTransport(mock_api_handler), base_url="http://mock"
    ) as mock_client:
        runner = BenchmarkRunner(
            database_path=str(tmp_path / "async-evaluation.sqlite3"),
            orchestrator_client=orchestrator_client,
            mock_admin_client=mock_client,
            poll_interval_seconds=0,
        )
        result = await runner.run_benchmarks(
            BenchmarkRunRequest(
                scenarios=["rename"],
                evidence_levels=["explicit_hint"],
                trials_per_scenario=1,
            )
        )

    assert state["polls"] == 1
    assert result.status == "completed"
    assert result.execution_completed is True
    assert result.verified_recoveries == 1
    assert [record.run_status for record in result.trials] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_benchmark_runner_preserves_empty_timeout_error_and_reports_partial(tmp_path):
    def mock_api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/mutation":
            return httpx.Response(200, json={"active_scenario": "rename", "enabled": True})
        if request.url.path == "/admin/state":
            return httpx.Response(
                200,
                json={
                    "tickets": {
                        "81": {
                            "id": 81,
                            "status": "open",
                            "assignee_id": None,
                            "history": [],
                        }
                    }
                },
            )
        return httpx.Response(404)

    def orchestrator_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/demo/reset":
            return httpx.Response(200, json={"mock_api": "reset_successful"})
        if request.url.path == "/registry":
            return httpx.Response(
                200, json={"tools": [{"name": "assign_ticket", "version": "1.0.0"}]}
            )
        if request.url.path == "/runs":
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(orchestrator_handler), base_url="http://orchestrator"
    ) as orchestrator_client, httpx.AsyncClient(
        transport=httpx.MockTransport(mock_api_handler), base_url="http://mock"
    ) as mock_client:
        database_path = tmp_path / "timeout-evaluation.sqlite3"
        runner = BenchmarkRunner(
            database_path=str(database_path),
            orchestrator_client=orchestrator_client,
            mock_admin_client=mock_client,
            timeout_seconds=60.0,
        )
        result = await runner.run_benchmarks(
            BenchmarkRunRequest(
                scenarios=["rename"],
                evidence_levels=["explicit_hint"],
                trials_per_scenario=1,
            )
        )

    assert result.status == "partial"
    assert result.execution_completed is False
    assert result.all_expected_outcomes_met is False
    assert all(record.run_status == "timed_out" for record in result.trials)
    assert all(record.failure_stage == "run_execution" for record in result.trials)
    assert all(record.error_type == "ReadTimeout" for record in result.trials)
    assert all("60.0-second benchmark client timeout" in record.error for record in result.trials)
    assert all(record.safe_refusal is False for record in result.trials)

    async with aiosqlite.connect(database_path) as db:
        cursor = await db.execute("SELECT record_json FROM evaluation_trials")
        persisted = [json.loads(row[0]) for row in await cursor.fetchall()]
    assert len(persisted) == 2
    assert all(record["error_type"] == "ReadTimeout" for record in persisted)
    assert all(record["failure_stage"] == "run_execution" for record in persisted)
