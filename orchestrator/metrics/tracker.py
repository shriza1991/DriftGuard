"""Metrics tracking and truthful aggregation for DriftGuard."""
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

from orchestrator.metrics.run_metrics import (
    RunMetrics,
    ModelUsageBreakdown,
    compute_usage_breakdown,
    combine_session_usage,
)


class SystemMetrics(BaseModel):
    """System-wide metrics and run history conforming to contracts."""
    total_runs: int = 0
    normal_runs: int = 0
    normal_success_count: int = 0
    healing_trials: int = 0
    verified_recoveries: int = 0
    unsafe_trials: int = 0
    average_recovery_time_ms: Optional[float] = None
    token_usage: str = "unavailable"
    status: str = "normal_scaffolding_active"
    note: str = "Only real runs are counted; no synthetic benchmarks or fabricated recovery metrics are reported."
    
    # Additive fields for Member 3 dashboard / pagination
    limit: Optional[int] = None
    offset: Optional[int] = None
    total_runs_count: Optional[int] = None
    run_metrics: List[RunMetrics] = Field(default_factory=list)


class MetricsTracker:
    """Computes truthful system and per-run metrics without fabricating numbers."""

    def __init__(self, audit_store: Optional[Any] = None):
        self.audit_store = audit_store
        self._in_memory_metrics = SystemMetrics(status="normal_scaffolding_active")

    def record_run(self, mode: str, success: bool, duration_ms: float):
        """In-memory legacy tracker hook for fast synchronous updates."""
        self._in_memory_metrics.total_runs += 1
        if mode == "normal":
            self._in_memory_metrics.normal_runs += 1
            if success:
                self._in_memory_metrics.normal_success_count += 1
        elif mode == "healing":
            self._in_memory_metrics.healing_trials += 1
            if success:
                self._in_memory_metrics.verified_recoveries += 1

    async def compute_run_metrics(self, run_id: str, store: Optional[Any] = None) -> Optional[RunMetrics]:
        """Assembles truthful RunMetrics for a single run from persistent audit tables."""
        audit_store = store or self.audit_store
        if not audit_store:
            return None

        run = await audit_store.get_run(run_id)
        if not run:
            return None

        tool_execs = await audit_store.get_run_tool_executions(run_id)
        repairs = await audit_store.get_run_repair_attempts(run_id)
        usages = await audit_store.get_run_model_usages(run_id)

        # Tool execution counts
        dispatched = [e for e in tool_execs if not e.failure or e.failure.get("http_status", 0) != 0 or e.failure.get("uncertain_write")]
        tool_dispatch_count = len(dispatched)
        original_dispatch_count = sum(1 for e in dispatched if not e.is_repaired)
        repaired_dispatch_count = sum(1 for e in dispatched if e.is_repaired)

        # Repair attempt counts and timing
        repair_attempts_count = len(repairs)
        repair_rejections_count = sum(
            1 for r in repairs
            if r.validation_status == "rejected" or r.recovery_status == "validation_rejected"
        )
        verified_recoveries_count = sum(1 for r in repairs if r.verification_succeeded)
        registry_commits_count = sum(1 for r in repairs if r.registry_committed)

        # Recovery durations: separate verified recovery from failed recovery
        recovery_duration_ms: Optional[float] = None
        commit_latency_ms: Optional[float] = None
        failed_recovery_duration_ms: Optional[float] = None

        verified_durations = [r.recovery_duration_ms for r in repairs if r.verification_succeeded and r.recovery_duration_ms is not None]
        if verified_durations:
            recovery_duration_ms = round(sum(verified_durations), 2)

        commit_latencies = [r.commit_latency_ms for r in repairs if r.commit_latency_ms is not None]
        if commit_latencies:
            commit_latency_ms = round(sum(commit_latencies), 2)

        failed_durations = [r.failed_recovery_duration_ms for r in repairs if not r.verification_succeeded and r.failed_recovery_duration_ms is not None]
        if failed_durations:
            failed_recovery_duration_ms = round(sum(failed_durations), 2)

        # Usages breakdown
        agent_usages = [u for u in usages if u.purpose == "agent_turn"]
        diag_usages = [u for u in usages if u.purpose == "diagnosis"]

        agent_breakdown = compute_usage_breakdown(agent_usages)
        diag_breakdown = compute_usage_breakdown(diag_usages) if diag_usages else ModelUsageBreakdown(input_tokens=0, output_tokens=0, total_tokens=0, is_complete=True, status="complete")
        session_breakdown = combine_session_usage(agent_breakdown, diag_breakdown)

        # Turns count
        total_turns = 1
        if run.verified_outcome and isinstance(run.verified_outcome, dict):
            total_turns = run.verified_outcome.get("turns_executed", 1)
        elif agent_usages:
            total_turns = len(agent_usages)

        return RunMetrics(
            run_id=run.run_id,
            mode=run.mode,
            execution_type=run.execution_type,
            status=run.status,
            duration_ms=run.duration_ms,
            total_turns=total_turns,
            tool_dispatch_count=tool_dispatch_count,
            original_dispatch_count=original_dispatch_count,
            repaired_dispatch_count=repaired_dispatch_count,
            repair_attempts_count=repair_attempts_count,
            repair_rejections_count=repair_rejections_count,
            verified_recoveries_count=verified_recoveries_count,
            registry_commits_count=registry_commits_count,
            recovery_duration_ms=recovery_duration_ms,
            commit_latency_ms=commit_latency_ms,
            failed_recovery_duration_ms=failed_recovery_duration_ms,
            agent_token_usage=agent_breakdown,
            diagnosis_token_usage=diag_breakdown,
            session_token_usage=session_breakdown,
            audit_status=run.audit_status,
        )

    async def get_metrics(
        self,
        store: Optional[Any] = None,
        limit: int = 50,
        offset: int = 0,
        mode: Optional[str] = None,
    ) -> SystemMetrics:
        """Assembles truthful system metrics from SQLite audit store when available."""
        audit_store = store or self.audit_store
        if not audit_store:
            return self._in_memory_metrics

        runs, total_count = await audit_store.list_runs(limit=limit, offset=offset, mode=mode)
        all_runs, _ = await audit_store.list_runs(limit=1000, offset=0)

        normal_runs = sum(1 for r in all_runs if r.mode == "normal")
        normal_success = sum(1 for r in all_runs if r.mode == "normal" and r.status == "completed")
        healing_trials = sum(1 for r in all_runs if r.mode == "healing")

        # Fetch all repair attempts across runs to compute verified recoveries and average recovery time
        all_repairs = await audit_store.get_all_repair_attempts(limit=1000, offset=0)
        verified_reps = [r for r in all_repairs if r.verification_succeeded]
        verified_recoveries_count = len(verified_reps)

        avg_recovery_ms: Optional[float] = None
        valid_rec_times = [r.recovery_duration_ms for r in verified_reps if r.recovery_duration_ms is not None]
        if valid_rec_times:
            avg_recovery_ms = round(sum(valid_rec_times) / len(valid_rec_times), 2)

        # Build per-run metrics for the paginated slice
        run_metrics_list: List[RunMetrics] = []
        for r in runs:
            rm = await self.compute_run_metrics(r.run_id, store=audit_store)
            if rm:
                run_metrics_list.append(rm)

        has_usages = any(
            rm.agent_token_usage.status != "unavailable" or rm.diagnosis_token_usage.total_tokens != 0 for rm in run_metrics_list
        )
        token_usage_str = "measured" if has_usages else "unavailable"

        return SystemMetrics(
            total_runs=len(all_runs),
            normal_runs=normal_runs,
            normal_success_count=normal_success,
            healing_trials=healing_trials,
            verified_recoveries=verified_recoveries_count,
            unsafe_trials=0,
            average_recovery_time_ms=avg_recovery_ms,
            token_usage=token_usage_str,
            status="normal_scaffolding_active",
            note="Only real runs are counted; no synthetic benchmarks or fabricated recovery metrics are reported.",
            limit=limit,
            offset=offset,
            total_runs_count=total_count,
            run_metrics=run_metrics_list,
        )

    def reset(self):
        """Resets in-memory metrics state (does not drop SQLite database)."""
        self._in_memory_metrics = SystemMetrics(status="normal_scaffolding_active")


metrics_tracker = MetricsTracker()
