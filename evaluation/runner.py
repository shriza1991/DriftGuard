"""Measured baseline-versus-healing benchmark runner.

The runner talks only to public orchestrator endpoints and the isolated mock
admin surface. Expected outcomes come from the evaluation catalogue and never
enter the healing path.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import uuid
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import httpx
from pydantic import BaseModel, Field, field_validator

from evaluation.scenarios import SCENARIO_CATALOG


DEFAULT_TASK = "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
SCENARIOS_BY_ID = {scenario.id: scenario for scenario in SCENARIO_CATALOG}


class BenchmarkRunRequest(BaseModel):
    scenarios: List[str] = Field(
        default_factory=lambda: ["rename", "type_change", "nesting", "required_header", "combined"]
    )
    evidence_levels: List[str] = Field(default_factory=lambda: ["explicit_hint"])
    trials_per_scenario: int = Field(default=3, ge=1, le=100)
    task: str = DEFAULT_TASK

    @field_validator("scenarios")
    @classmethod
    def validate_scenarios(cls, values: List[str]) -> List[str]:
        unknown = sorted(set(values) - set(SCENARIOS_BY_ID))
        if unknown:
            raise ValueError(f"Unknown evaluation scenarios: {', '.join(unknown)}")
        if not values:
            raise ValueError("At least one scenario is required")
        return values

    @field_validator("evidence_levels")
    @classmethod
    def validate_evidence_levels(cls, values: List[str]) -> List[str]:
        allowed = {"ordinary", "explicit_hint"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"Unknown evidence levels: {', '.join(unknown)}")
        if not values:
            raise ValueError("At least one evidence level is required")
        return values


class TrialRecord(BaseModel):
    trial_id: str
    comparison_id: str
    scenario_id: str
    evidence_level: str
    trial_index: int
    mode: str
    expected_outcome: str
    run_id: Optional[str] = None
    run_status: str
    verified_recovery: bool = False
    safe_refusal: bool = False
    final_assignee_id: Optional[str] = None
    intended_effect_count: int = 0
    duration_ms: Optional[float] = None
    recovery_time_ms: Optional[float] = None
    total_tokens: Optional[int] = None
    diagnosis_tokens: Optional[int] = None
    termination_reason: Optional[str] = None
    error: Optional[str] = None
    created_at: str


class BenchmarkResult(BaseModel):
    status: str
    message: str
    evaluated_trials: int
    comparison_pairs: int
    recoverable_healing_trials: int
    verified_recoveries: int
    unsafe_healing_trials: int
    safe_refusals: int
    baseline_verified_successes: int
    baseline_success_rate: Optional[float]
    driftguard_recovery_rate: Optional[float]
    safe_refusal_rate: Optional[float]
    mean_recovery_time_ms: Optional[float]
    median_recovery_time_ms: Optional[float]
    p95_recovery_time_ms: Optional[float]
    scenarios_tested: List[str]
    evidence_levels_tested: List[str]
    token_usage_available_trials: int
    database_path: str
    trials: List[TrialRecord]


class BenchmarkRunner:
    """Runs fair paired trials through public service contracts."""

    def __init__(
        self,
        orchestrator_url: str = "http://127.0.0.1:8000",
        mock_api_url: str = "http://127.0.0.1:8001",
        database_path: Optional[str] = None,
        orchestrator_client: Optional[httpx.AsyncClient] = None,
        mock_admin_client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.orchestrator_url = orchestrator_url.rstrip("/")
        self.mock_api_url = mock_api_url.rstrip("/")
        self.database_path = database_path or os.getenv(
            "DRIFTGUARD_EVALUATION_DB_PATH", "driftguard.sqlite3"
        )
        self._orchestrator_client = orchestrator_client
        self._mock_admin_client = mock_admin_client
        self.timeout_seconds = timeout_seconds

    async def _initialize_database(self) -> None:
        path = Path(self.database_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS evaluation_trials (
                    trial_id TEXT PRIMARY KEY,
                    comparison_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    evidence_level TEXT NOT NULL,
                    trial_index INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    expected_outcome TEXT NOT NULL,
                    run_id TEXT,
                    run_status TEXT NOT NULL,
                    verified_recovery INTEGER NOT NULL,
                    safe_refusal INTEGER NOT NULL,
                    final_assignee_id TEXT,
                    intended_effect_count INTEGER NOT NULL,
                    duration_ms REAL,
                    recovery_time_ms REAL,
                    total_tokens INTEGER,
                    diagnosis_tokens INTEGER,
                    termination_reason TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def _persist_trial(self, record: TrialRecord) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO evaluation_trials VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.trial_id,
                    record.comparison_id,
                    record.scenario_id,
                    record.evidence_level,
                    record.trial_index,
                    record.mode,
                    record.expected_outcome,
                    record.run_id,
                    record.run_status,
                    int(record.verified_recovery),
                    int(record.safe_refusal),
                    record.final_assignee_id,
                    record.intended_effect_count,
                    record.duration_ms,
                    record.recovery_time_ms,
                    record.total_tokens,
                    record.diagnosis_tokens,
                    record.termination_reason,
                    record.error,
                    record.created_at,
                    record.model_dump_json(),
                ),
            )
            await db.commit()

    async def _reset_and_activate(
        self,
        orchestrator: httpx.AsyncClient,
        mock_admin: httpx.AsyncClient,
        scenario_id: str,
        evidence_level: str,
    ) -> None:
        reset = await orchestrator.post("/demo/reset")
        reset.raise_for_status()
        reset_payload = reset.json()
        if reset_payload.get("mock_api") != "reset_successful":
            raise RuntimeError(f"Mock reset failed: {reset_payload.get('mock_api')}")

        mutation = await mock_admin.post(
            "/admin/mutation",
            json={"scenario": scenario_id, "enabled": True, "evidence_level": evidence_level},
        )
        mutation.raise_for_status()

        state = await mock_admin.get("/admin/state")
        state.raise_for_status()
        state_payload = state.json()
        tickets = state_payload.get("tickets", {})
        ticket = tickets.get("81") or tickets.get(81) or {}
        if ticket.get("assignee_id") is not None or ticket.get("history"):
            raise RuntimeError("Mock state was not pristine before trial")

        registry = await orchestrator.get("/registry")
        registry.raise_for_status()
        assign_tool = next(
            (tool for tool in registry.json().get("tools", []) if tool.get("name") == "assign_ticket"),
            None,
        )
        if not assign_tool or assign_tool.get("version") != "1.0.0":
            raise RuntimeError("Active assign_ticket registry was not reset to version 1.0.0")

    @staticmethod
    def _token_value(model_usage: Any, field: str) -> Optional[int]:
        if not isinstance(model_usage, dict):
            return None
        value = model_usage.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def _recovery_time_from_events(
        self, orchestrator: httpx.AsyncClient, run_id: Optional[str]
    ) -> Optional[float]:
        if not run_id:
            return None
        try:
            response = await orchestrator.get(f"/runs/{run_id}/events")
            response.raise_for_status()
            events: List[Dict[str, Any]] = []
            for line in response.text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            start = next((e for e in events if e.get("type") == "tool_call_failed"), None)
            end = next((e for e in events if e.get("type") == "recovery_verified"), None)
            if not start or not end:
                return None
            start_at = datetime.fromisoformat(start["timestamp"].replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(end["timestamp"].replace("Z", "+00:00"))
            return round((end_at - start_at).total_seconds() * 1000, 2)
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError):
            return None

    async def _run_one(
        self,
        orchestrator: httpx.AsyncClient,
        mock_admin: httpx.AsyncClient,
        request: BenchmarkRunRequest,
        scenario_id: str,
        evidence_level: str,
        trial_index: int,
        mode: str,
        comparison_id: str,
    ) -> TrialRecord:
        scenario = SCENARIOS_BY_ID[scenario_id]
        created_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        run_payload: Dict[str, Any] = {}
        error: Optional[str] = None

        try:
            await self._reset_and_activate(orchestrator, mock_admin, scenario_id, evidence_level)
            execution_type = "model_driven_baseline" if mode == "baseline" else "model_driven_healing"
            response = await orchestrator.post(
                "/runs",
                json={"mode": mode, "execution_type": execution_type, "task": request.task},
            )
            response.raise_for_status()
            run_payload = response.json()
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            error = str(exc)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        state_payload: Dict[str, Any] = {}
        try:
            state_response = await mock_admin.get("/admin/state")
            state_response.raise_for_status()
            state_payload = state_response.json()
        except httpx.HTTPError as exc:
            error = error or f"Independent state verification failed: {exc}"

        tickets = state_payload.get("tickets", {})
        ticket = tickets.get("81") or tickets.get(81) or {}
        final_assignee = ticket.get("assignee_id")
        history = ticket.get("history") or []
        intended_effect_count = sum(
            1 for item in history
            if str(item.get("assignee_id")) == "123" and item.get("action") == "assigned"
        )
        verified = (
            scenario.expected_outcome == "heal_and_recover"
            and run_payload.get("status") == "completed"
            and str(final_assignee) == "123"
            and intended_effect_count == 1
        )
        safe_refusal = (
            scenario.expected_outcome == "safe_refusal"
            and bool(run_payload.get("run_id"))
            and run_payload.get("status") == "failed"
            and final_assignee is None
            and intended_effect_count == 0
        )
        recovery_time = (
            await self._recovery_time_from_events(orchestrator, run_payload.get("run_id"))
            if mode == "healing"
            else None
        )
        usage = run_payload.get("model_usage")
        return TrialRecord(
            trial_id=f"trial-{uuid.uuid4().hex[:12]}",
            comparison_id=comparison_id,
            scenario_id=scenario_id,
            evidence_level=evidence_level,
            trial_index=trial_index,
            mode=mode,
            expected_outcome=scenario.expected_outcome,
            run_id=run_payload.get("run_id"),
            run_status=run_payload.get("status", "setup_failed" if error else "unknown"),
            verified_recovery=verified,
            safe_refusal=safe_refusal,
            final_assignee_id=None if final_assignee is None else str(final_assignee),
            intended_effect_count=intended_effect_count,
            duration_ms=run_payload.get("duration_ms") if isinstance(run_payload.get("duration_ms"), (int, float)) else duration_ms,
            recovery_time_ms=recovery_time,
            total_tokens=self._token_value(usage, "total_tokens"),
            diagnosis_tokens=self._token_value(usage, "diagnosis_tokens"),
            termination_reason=run_payload.get("termination_reason"),
            error=error or run_payload.get("error"),
            created_at=created_at,
        )

    async def run_benchmarks(self, req: BenchmarkRunRequest) -> BenchmarkResult:
        await self._initialize_database()
        own_orchestrator = self._orchestrator_client is None
        own_mock = self._mock_admin_client is None
        orchestrator = self._orchestrator_client or httpx.AsyncClient(
            base_url=self.orchestrator_url, timeout=self.timeout_seconds
        )
        mock_admin = self._mock_admin_client or httpx.AsyncClient(
            base_url=self.mock_api_url, timeout=self.timeout_seconds
        )
        trials: List[TrialRecord] = []
        try:
            for scenario_id in req.scenarios:
                for evidence_level in req.evidence_levels:
                    for index in range(1, req.trials_per_scenario + 1):
                        comparison_id = f"pair-{uuid.uuid4().hex[:12]}"
                        for mode in ("baseline", "healing"):
                            record = await self._run_one(
                                orchestrator, mock_admin, req, scenario_id,
                                evidence_level, index, mode, comparison_id,
                            )
                            trials.append(record)
                            await self._persist_trial(record)
        finally:
            if own_orchestrator:
                await orchestrator.aclose()
            if own_mock:
                await mock_admin.aclose()

        healing = [record for record in trials if record.mode == "healing"]
        baseline = [record for record in trials if record.mode == "baseline"]
        recoverable = [record for record in healing if record.expected_outcome == "heal_and_recover"]
        unsafe = [record for record in healing if record.expected_outcome == "safe_refusal"]
        verified_count = sum(record.verified_recovery for record in recoverable)
        safe_refusal_count = sum(record.safe_refusal for record in unsafe)
        baseline_recoverable = [
            record for record in baseline if record.expected_outcome == "heal_and_recover"
        ]
        baseline_verified = sum(record.verified_recovery for record in baseline_recoverable)
        recovery_times = [
            record.recovery_time_ms for record in recoverable
            if record.verified_recovery and record.recovery_time_ms is not None
        ]
        failed_setups = sum(record.run_status == "setup_failed" for record in trials)
        status = "completed" if failed_setups == 0 else "partial"
        p95 = None
        if len(recovery_times) >= 20:
            p95 = round(statistics.quantiles(recovery_times, n=100, method="inclusive")[94], 2)

        return BenchmarkResult(
            status=status,
            message=(
                "All requested measured trials completed."
                if status == "completed"
                else f"Benchmark completed with {failed_setups} setup failures; inspect trial errors."
            ),
            evaluated_trials=len(trials),
            comparison_pairs=len({record.comparison_id for record in trials}),
            recoverable_healing_trials=len(recoverable),
            verified_recoveries=verified_count,
            unsafe_healing_trials=len(unsafe),
            safe_refusals=safe_refusal_count,
            baseline_verified_successes=baseline_verified,
            baseline_success_rate=(
                round(100 * baseline_verified / len(baseline_recoverable), 2)
                if baseline_recoverable else None
            ),
            driftguard_recovery_rate=(
                round(100 * verified_count / len(recoverable), 2) if recoverable else None
            ),
            safe_refusal_rate=(
                round(100 * safe_refusal_count / len(unsafe), 2) if unsafe else None
            ),
            mean_recovery_time_ms=(round(statistics.mean(recovery_times), 2) if recovery_times else None),
            median_recovery_time_ms=(round(statistics.median(recovery_times), 2) if recovery_times else None),
            p95_recovery_time_ms=p95,
            scenarios_tested=req.scenarios,
            evidence_levels_tested=req.evidence_levels,
            token_usage_available_trials=sum(record.total_tokens is not None for record in trials),
            database_path=str(Path(self.database_path).resolve()),
            trials=trials,
        )


benchmark_runner = BenchmarkRunner()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Run measured DriftGuard comparison trials")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["rename", "type_change", "nesting", "required_header", "combined"],
        choices=sorted(SCENARIOS_BY_ID),
    )
    parser.add_argument(
        "--evidence-levels",
        nargs="+",
        default=["explicit_hint"],
        choices=["ordinary", "explicit_hint"],
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--orchestrator-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mock-api-url", default="http://127.0.0.1:8001")
    parser.add_argument("--database", default=None)
    args = parser.parse_args()

    runner = BenchmarkRunner(
        orchestrator_url=args.orchestrator_url,
        mock_api_url=args.mock_api_url,
        database_path=args.database,
    )
    result = await runner.run_benchmarks(
        BenchmarkRunRequest(
            scenarios=args.scenarios,
            evidence_levels=args.evidence_levels,
            trials_per_scenario=args.trials,
        )
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(_main())
