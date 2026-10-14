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
    execution_completed: bool = False
    expected_outcome_met: bool = False
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
    error_type: Optional[str] = None
    failure_stage: Optional[str] = None
    tool_call_attempts: int = 0
    retry_attempts: int = 0
    failure_evidence: Optional[Dict[str, Any]] = None
    proposed_patch_operations: List[Dict[str, Any]] = Field(default_factory=list)
    patch_validation_status: Optional[str] = None
    recovery_verified_event: bool = False
    committed_registry_version: Optional[str] = None
    event_collection_error: Optional[str] = None
    created_at: str


class BenchmarkResult(BaseModel):
    status: str
    message: str
    execution_completed: bool
    all_expected_outcomes_met: bool
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


class BenchmarkStageError(RuntimeError):
    """Preserves the precise benchmark stage and original exception type."""

    def __init__(self, stage: str, cause: Exception):
        self.stage = stage
        self.cause = cause
        detail = str(cause).strip() or "no detail supplied"
        super().__init__(f"{type(cause).__name__} during {stage}: {detail}")


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
        try:
            reset = await orchestrator.post("/demo/reset")
            reset.raise_for_status()
            reset_payload = reset.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BenchmarkStageError("reset", exc) from exc
        if reset_payload.get("mock_api") != "reset_successful":
            raise BenchmarkStageError(
                "reset",
                RuntimeError(f"Mock reset failed: {reset_payload.get('mock_api')}"),
            )

        try:
            mutation = await mock_admin.post(
                "/admin/mutation",
                json={"scenario": scenario_id, "enabled": True, "evidence_level": evidence_level},
            )
            mutation.raise_for_status()
        except httpx.HTTPError as exc:
            raise BenchmarkStageError("mutation_activation", exc) from exc

        try:
            state = await mock_admin.get("/admin/state")
            state.raise_for_status()
            state_payload = state.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BenchmarkStageError("initial_state_verification", exc) from exc
        tickets = state_payload.get("tickets", {})
        ticket = tickets.get("81") or tickets.get(81) or {}
        if ticket.get("assignee_id") is not None or ticket.get("history"):
            raise BenchmarkStageError(
                "initial_state_verification",
                RuntimeError("Mock state was not pristine before trial"),
            )

        try:
            registry = await orchestrator.get("/registry")
            registry.raise_for_status()
            assign_tool = next(
                (tool for tool in registry.json().get("tools", []) if tool.get("name") == "assign_ticket"),
                None,
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise BenchmarkStageError("registry_verification", exc) from exc
        if not assign_tool or assign_tool.get("version") != "1.0.0":
            raise BenchmarkStageError(
                "registry_verification",
                RuntimeError("Active assign_ticket registry was not reset to version 1.0.0"),
            )

    @staticmethod
    def _token_value(model_usage: Any, field: str) -> Optional[int]:
        if not isinstance(model_usage, dict):
            return None
        value = model_usage.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def _collect_event_evidence(
        self, orchestrator: httpx.AsyncClient, run_id: Optional[str]
    ) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {
            "recovery_time_ms": None,
            "tool_call_attempts": 0,
            "retry_attempts": 0,
            "failure_evidence": None,
            "proposed_patch_operations": [],
            "patch_validation_status": None,
            "recovery_verified_event": False,
            "committed_registry_version": None,
            "event_collection_error": None,
        }
        if not run_id:
            return evidence
        try:
            response = await orchestrator.get(f"/runs/{run_id}/events")
            response.raise_for_status()
            events: List[Dict[str, Any]] = []
            for line in response.text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            evidence["tool_call_attempts"] = sum(
                event.get("type") == "tool_call_started" for event in events
            )
            evidence["retry_attempts"] = sum(
                event.get("type") == "retry_started" for event in events
            )
            failed = next((e for e in events if e.get("type") == "tool_call_failed"), None)
            proposed = next((e for e in events if e.get("type") == "patch_proposed"), None)
            validated = next(
                (e for e in events if e.get("type") in {"patch_validated", "patch_rejected"}),
                None,
            )
            verified = next((e for e in events if e.get("type") == "recovery_verified"), None)
            committed = next((e for e in events if e.get("type") == "registry_committed"), None)
            evidence["failure_evidence"] = failed.get("data") if failed else None
            evidence["proposed_patch_operations"] = (
                proposed.get("data", {}).get("operations", []) if proposed else []
            )
            evidence["patch_validation_status"] = validated.get("type") if validated else None
            evidence["recovery_verified_event"] = verified is not None
            evidence["committed_registry_version"] = (
                committed.get("data", {}).get("committed_version") if committed else None
            )
            start = next((e for e in events if e.get("type") == "tool_call_failed"), None)
            end = next((e for e in events if e.get("type") == "recovery_verified"), None)
            if start and end:
                start_at = datetime.fromisoformat(start["timestamp"].replace("Z", "+00:00"))
                end_at = datetime.fromisoformat(end["timestamp"].replace("Z", "+00:00"))
                evidence["recovery_time_ms"] = round(
                    (end_at - start_at).total_seconds() * 1000, 2
                )
        except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as exc:
            detail = str(exc).strip() or "no detail supplied"
            evidence["event_collection_error"] = f"{type(exc).__name__}: {detail}"
        return evidence

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
        error_type: Optional[str] = None
        failure_stage: Optional[str] = None
        caught_exception: Optional[Exception] = None

        try:
            await self._reset_and_activate(orchestrator, mock_admin, scenario_id, evidence_level)
            execution_type = "model_driven_baseline" if mode == "baseline" else "model_driven_healing"
            try:
                response = await orchestrator.post(
                    "/runs",
                    json={"mode": mode, "execution_type": execution_type, "task": request.task},
                )
                response.raise_for_status()
                run_payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise BenchmarkStageError("run_execution", exc) from exc
        except BenchmarkStageError as exc:
            caught_exception = exc.cause
            failure_stage = exc.stage
            error_type = type(exc.cause).__name__
            detail = str(exc.cause).strip()
            if isinstance(exc.cause, httpx.TimeoutException) and not detail:
                detail = f"request exceeded the {self.timeout_seconds:.1f}-second benchmark client timeout"
            elif not detail:
                detail = "no detail supplied"
            error = f"{error_type} during {failure_stage}: {detail}"

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        state_payload: Dict[str, Any] = {}
        try:
            state_response = await mock_admin.get("/admin/state")
            state_response.raise_for_status()
            state_payload = state_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if error is None:
                failure_stage = "final_state_verification"
                error_type = type(exc).__name__
                detail = str(exc).strip() or "no detail supplied"
                error = f"{error_type} during final_state_verification: {detail}"

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
        event_evidence = await self._collect_event_evidence(
            orchestrator, run_payload.get("run_id")
        )
        usage = run_payload.get("model_usage")
        run_status = run_payload.get("status")
        if not run_status:
            if isinstance(caught_exception, httpx.TimeoutException):
                run_status = "timed_out"
            elif failure_stage == "run_execution":
                run_status = "run_request_failed"
            elif failure_stage:
                run_status = "setup_failed"
            else:
                run_status = "unknown"
        execution_completed = bool(
            run_payload.get("run_id") and run_status in {"completed", "failed"}
        )
        expected_outcome_met = bool(
            mode == "healing" and (verified or safe_refusal)
        )
        return TrialRecord(
            trial_id=f"trial-{uuid.uuid4().hex[:12]}",
            comparison_id=comparison_id,
            scenario_id=scenario_id,
            evidence_level=evidence_level,
            trial_index=trial_index,
            mode=mode,
            expected_outcome=scenario.expected_outcome,
            run_id=run_payload.get("run_id"),
            run_status=run_status,
            execution_completed=execution_completed,
            expected_outcome_met=expected_outcome_met,
            verified_recovery=verified,
            safe_refusal=safe_refusal,
            final_assignee_id=None if final_assignee is None else str(final_assignee),
            intended_effect_count=intended_effect_count,
            duration_ms=run_payload.get("duration_ms") if isinstance(run_payload.get("duration_ms"), (int, float)) else duration_ms,
            recovery_time_ms=event_evidence["recovery_time_ms"],
            total_tokens=self._token_value(usage, "total_tokens"),
            diagnosis_tokens=self._token_value(usage, "diagnosis_tokens"),
            termination_reason=run_payload.get("termination_reason"),
            error=error or run_payload.get("error"),
            error_type=error_type,
            failure_stage=failure_stage,
            tool_call_attempts=event_evidence["tool_call_attempts"],
            retry_attempts=event_evidence["retry_attempts"],
            failure_evidence=event_evidence["failure_evidence"],
            proposed_patch_operations=event_evidence["proposed_patch_operations"],
            patch_validation_status=event_evidence["patch_validation_status"],
            recovery_verified_event=event_evidence["recovery_verified_event"],
            committed_registry_version=event_evidence["committed_registry_version"],
            event_collection_error=event_evidence["event_collection_error"],
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
        execution_completed = bool(trials) and all(record.execution_completed for record in trials)
        all_expected_outcomes_met = bool(healing) and all(
            record.expected_outcome_met for record in healing
        )
        incomplete_trials = sum(not record.execution_completed for record in trials)
        unmet_healing_outcomes = sum(not record.expected_outcome_met for record in healing)
        if incomplete_trials:
            status = "partial"
            message = (
                f"Benchmark ended with {incomplete_trials} incomplete trial(s); "
                "inspect failure_stage, error_type, and error."
            )
        elif not all_expected_outcomes_met:
            status = "completed_with_failures"
            message = (
                f"All trial executions reached a terminal state, but {unmet_healing_outcomes} "
                "healing outcome(s) did not meet expectations."
            )
        else:
            status = "completed"
            message = "All requested trials completed and all healing outcomes met expectations."
        p95 = None
        if len(recovery_times) >= 20:
            p95 = round(statistics.quantiles(recovery_times, n=100, method="inclusive")[94], 2)

        return BenchmarkResult(
            status=status,
            message=message,
            execution_completed=execution_completed,
            all_expected_outcomes_met=all_expected_outcomes_met,
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
