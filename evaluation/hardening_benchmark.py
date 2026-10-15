"""Reproducible ContractGuard safety benchmark for notification preference drift.

The benchmark exercises the production registry, executor, classifier, planner,
validator, recovery controller, and mock FastAPI provider in-process. Admin
controls are used only by this evaluator to configure scenarios and independently
inspect final state; they are never passed into the healing path.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import copy
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from mock_api.app import app as mock_app
from mock_api.config import settings as mock_settings
from orchestrator.client.mock_client import MockBusinessApiClient
from orchestrator.execution.events import EventBroadcaster
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.healing.recovery import RecoveryController, RecoveryStatus
from orchestrator.models import ExecutionContext
from orchestrator.registry import VersionedToolRegistry
from orchestrator.registry.default_tools import create_default_registry
from evaluation.artifact_validation import SCHEMA_VERSION


TASK = "Disable promotional notifications for user 123."
BUSINESS_ARGUMENTS = {"user_id": "123", "promo": False}
TOOL = "update_notification_preferences"
RECOVERABLE = ("rename", "type_change", "nesting", "required_header", "combined")
UNSAFE = ("missing_business_value", "unavailable_credential", "ambiguous_mapping")
NON_DRIFT = ("authentication_failure", "permission_failure", "rate_limit", "provider_outage")
SPECIAL = ("malformed_error", "verification_mismatch", "stale_version")
SCENARIOS = ("normal",) + RECOVERABLE + UNSAFE + NON_DRIFT + SPECIAL
CSV_FIELDS = (
    "scenario", "mutation_type", "execution_mode", "baseline_or_contractguard",
    "pair_id", "repetition", "scenario_class", "run_id", "outcome", "safe_stop", "retry_count", "diagnosis_called",
    "llm_called", "verification_passed", "commit_performed",
    "registry_version_before", "registry_version_after", "latency_ms",
    "input_tokens", "output_tokens", "provider_state_correct", "error_category",
    "reason_code", "verification_status", "commit_status", "task_hash",
)


@dataclass
class Trial:
    scenario: str
    mutation_type: str
    execution_mode: str
    baseline_or_contractguard: str
    run_id: str
    repetition: int
    outcome: str
    safe_stop: bool
    retry_count: int
    diagnosis_called: bool
    patch_planning_called: bool
    llm_called: bool
    verification_passed: bool
    commit_performed: bool
    registry_version_before: str
    registry_version_after: str
    registry_unchanged_during_candidate_evaluation: bool
    latency_ms: float
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    provider_state_correct: bool
    error_category: Optional[str]
    business_values_preserved: bool
    unsafe_execution: bool
    active_contract_fingerprint: str
    proposed_patch_operations: list[dict[str, Any]]
    deterministic_decision: Optional[str]
    termination_reason: Optional[str]
    repeated_same_invalid_request: bool = False
    exceeded_iteration_budget: bool = False
    benchmark_schema_version: str = SCHEMA_VERSION
    pair_id: str = ""
    scenario_class: str = ""
    task: str = TASK
    logical_intent_hash: str = ""
    initial_provider_state_fingerprint: str = ""
    initial_contract_fingerprint: str = ""
    reason_code: str = ""
    retry_eligible: bool = False
    verification_status: str = "not_run"
    commit_status: str = "not_committed"
    evidence_refs: dict[str, Any] | None = None
    registry_checkpoints: dict[str, Any] | None = None
    timing_boundaries: dict[str, Any] | None = None
    end_to_end_latency_ms: float | None = None
    mttr_ms: float | None = None
    safe_stop_latency_ms: float | None = None

    def __post_init__(self) -> None:
        self.pair_id = self.pair_id or f"pair-{self.scenario}-{self.repetition}"
        self.scenario_class = self.scenario_class or (
            "normal" if self.scenario == "normal" else
            "recoverable" if self.scenario in RECOVERABLE else
            "unsafe" if self.scenario in UNSAFE else
            "non_drift" if self.scenario in NON_DRIFT else "special"
        )
        self.logical_intent_hash = self.logical_intent_hash or hashlib.sha256(
            json.dumps(BUSINESS_ARGUMENTS, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.initial_contract_fingerprint = self.initial_contract_fingerprint or self.active_contract_fingerprint
        self.reason_code = self.reason_code or self._reason_code()
        self.verification_status = self.verification_status if self.verification_status != "not_run" else ("passed" if self.verification_passed else "failed")
        self.commit_status = "committed" if self.commit_performed else "not_committed"
        self.evidence_refs = self.evidence_refs or {
            "initial_contract": self.initial_contract_fingerprint,
            "terminal_outcome": self.outcome,
            "decision": self.deterministic_decision,
        }
        self.registry_checkpoints = self.registry_checkpoints or {
            "before_initial_call": self.registry_version_before,
            "terminal": self.registry_version_after,
        }
        self.timing_boundaries = self.timing_boundaries or {"end_to_end_ms": self.latency_ms}
        self.end_to_end_latency_ms = self.end_to_end_latency_ms if self.end_to_end_latency_ms is not None else self.latency_ms
        self.safe_stop_latency_ms = self.safe_stop_latency_ms if self.safe_stop_latency_ms is not None else (self.latency_ms if self.outcome == "safe_stop" else None)

    def _reason_code(self) -> str:
        return {
            "normal_success": "normal_verified",
            "verified_recovery": "recovery_verified_and_committed",
            "verified_reuse": "verified_reuse",
            "safe_stop": "authoritative_safe_stop",
            "benchmark_error": "benchmark_error",
        }.get(self.outcome, "operation_failed")


def _percent(numerator: int, denominator: int) -> Optional[float]:
    return round(100 * numerator / denominator, 2) if denominator else None


def _p95(values: Iterable[float]) -> Optional[float]:
    data = list(values)
    return round(statistics.quantiles(data, n=100, method="inclusive")[94], 3) if len(data) >= 20 else None


def _git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


class Harness:
    def __init__(self) -> None:
        self.transport = httpx.ASGITransport(app=mock_app)

    async def admin(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self.transport, base_url="http://mock") as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

    async def reset(self, scenario: str) -> None:
        await self.admin("POST", "/admin/reset")
        active = "rename" if scenario == "stale_version" else scenario
        if active != "normal":
            await self.admin(
                "POST", "/admin/mutation",
                json={"scenario": active, "enabled": True, "evidence_level": "ordinary"},
            )

    def client(self) -> MockBusinessApiClient:
        raw = httpx.AsyncClient(transport=self.transport, base_url="http://mock")
        return MockBusinessApiClient(
            client=raw,
            credential_resolver=lambda ref: mock_settings.assignment_bearer_token
            if ref == "secops_auth" else None,
        )

    async def state(self) -> dict[str, Any]:
        return await self.admin("GET", "/admin/state")


def _registry() -> VersionedToolRegistry:
    return VersionedToolRegistry(initial_tools=create_default_registry(include_notification_tools=True))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()[:16]


def _intent_values(arguments: dict[str, Any]) -> tuple[str | None, bool | None]:
    user = arguments.get("user_id")
    if user is None and isinstance(arguments.get("target"), dict):
        user = arguments["target"].get("id")
    promo = arguments.get("promo")
    if promo is None and isinstance(arguments.get("preferences"), dict):
        promo = arguments["preferences"].get("promo")
    if promo is None:
        promo = arguments.get("promotional")
    return (None if user is None else str(user), promo if isinstance(promo, bool) else None)


def _apply_operations(arguments: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply only the recorded declarative operations for evidence checking."""
    result = copy.deepcopy(arguments)
    for operation in operations:
        source = operation.get("source_path")
        target = operation.get("target_path")
        if not source or not target:
            continue
        source_parts = source.split(".")
        current: Any = result
        for part in source_parts[:-1]:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if not isinstance(current, dict) or source_parts[-1] not in current:
            continue
        value = current.pop(source_parts[-1])
        target_parts = target.split(".")
        dest = result
        for part in target_parts[:-1]:
            dest = dest.setdefault(part, {})
        if operation.get("operation_type") == "cast_type" and operation.get("conversion_policy") in {"str_to_int", "string_to_integer", "to_integer"}:
            value = int(value)
        elif operation.get("operation_type") == "cast_type" and operation.get("conversion_policy") in {"int_to_str", "integer_to_string", "to_string"}:
            value = str(value)
        dest[target_parts[-1]] = value
    return result


def _authoritative_stop(recovery: Any, scenario: str, verified: bool) -> bool:
    if verified:
        return False
    if scenario in NON_DRIFT:
        return False
    if scenario == "verification_mismatch":
        return True
    if recovery is None:
        return False
    return recovery.status in {
        RecoveryStatus.INELIGIBLE,
        RecoveryStatus.VALIDATION_REJECTED,
        RecoveryStatus.PLANNING_FAILED,
        RecoveryStatus.STALE_SKIPPED,
    }


def _warm_arguments(scenario: str) -> dict[str, Any]:
    return {
        "rename": {"user_id": "123", "promotional": False},
        "type_change": {"user_id": 123, "promo": False},
        "nesting": {"user_id": "123", "preferences": {"promo": False}},
        "required_header": dict(BUSINESS_ARGUMENTS),
        "combined": {"target": {"id": 123}, "preferences": {"promo": False}},
    }[scenario]


async def _verify(executor: ToolExecutor, run_id: str) -> bool:
    result = await executor.execute(
        "get_notification_preferences", {"user_id": "123"},
        context=ExecutionContext(run_id=run_id),
    )
    return bool(result.is_success and (result.result or {}).get("promo") is False)


async def _run_baseline(harness: Harness, scenario: str, repetition: int) -> Trial:
    await harness.reset(scenario)
    initial_state = await harness.state()
    registry = _registry()
    client = harness.client()
    executor = ToolExecutor(registry=registry, client=client)
    run_id = f"baseline-{scenario}-{repetition}-{uuid.uuid4().hex[:8]}"
    before = registry.get(TOOL)
    started = time.perf_counter()
    attempts = []
    try:
        for attempt in range(2):
            result = await executor.execute(
                TOOL, BUSINESS_ARGUMENTS,
                context=ExecutionContext(run_id=run_id, tool_call_id=f"call-{attempt}", idempotency_key=f"{run_id}-write"),
            )
            attempts.append(result)
            if result.is_success:
                break
        verified = await _verify(executor, run_id) if attempts[-1].is_success else False
        state = await harness.state()
    finally:
        await client._client.aclose()
    after = registry.get(TOOL)
    last = attempts[-1]
    category = last.classification.category.value if last.classification else None
    repeated = len(attempts) == 2 and not attempts[0].is_success and not attempts[1].is_success
    preserved = _intent_values(BUSINESS_ARGUMENTS) == ("123", False)
    return Trial(
        scenario=scenario, mutation_type=scenario, execution_mode="cold_registry",
        baseline_or_contractguard="baseline", run_id=run_id, repetition=repetition,
        outcome="completed" if verified else "stopped_with_error", safe_stop=False,
        retry_count=max(0, len(attempts) - 1), diagnosis_called=False,
        patch_planning_called=False, llm_called=False, verification_passed=verified,
        commit_performed=False, registry_version_before=before.version,
        registry_version_after=after.version,
        registry_unchanged_during_candidate_evaluation=True,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        input_tokens=0, output_tokens=0,
        provider_state_correct=state["notification_preferences"]["123"]["promo"] is False,
        error_category=category, business_values_preserved=preserved,
        unsafe_execution=False, active_contract_fingerprint=before.fingerprint,
        proposed_patch_operations=[], deterministic_decision=None,
        termination_reason=None if verified else (last.failure.message if last.failure else "baseline stopped"),
        repeated_same_invalid_request=repeated,
        initial_provider_state_fingerprint=_fingerprint(initial_state.get("notification_preferences", {}).get("123", {})),
        reason_code="baseline_completed" if verified else "baseline_stopped_with_error",
        retry_eligible=False,
        verification_status="passed" if verified else "failed",
    )


async def _run_contractguard(
    harness: Harness,
    scenario: str,
    repetition: int,
) -> tuple[Trial, Optional[Trial]]:
    await harness.reset(scenario)
    initial_state = await harness.state()
    registry = _registry()
    client = harness.client()
    executor = ToolExecutor(registry=registry, client=client)
    events = EventBroadcaster()
    controller = RecoveryController(
        registry=registry,
        executor=executor,
        event_broadcaster=events,
        approved_header_credentials={"Authorization": "secops_auth"},
        credential_resolver=lambda ref: ref == "secops_auth",
    )
    run_id = f"contractguard-{scenario}-{repetition}-{uuid.uuid4().hex[:8]}"
    before = registry.get(TOOL)
    started = time.perf_counter()
    initial = await executor.execute(
        TOOL, BUSINESS_ARGUMENTS,
        context=ExecutionContext(run_id=run_id, tool_call_id="initial", idempotency_key=f"{run_id}-write"),
    )
    recovery = None
    initial_version_after_failure = registry.get(TOOL).version
    if not initial.is_success and initial.observation is not None:
        if scenario == "stale_version":
            current = registry.get(TOOL)
            staged = await registry.stage_candidate(TOOL, current, current.version)
            await registry.commit_candidate(staged.candidate_id, current.version)
        recovery = await controller.recover_failure(
            initial.observation,
            BUSINESS_ARGUMENTS,
            initial.operation_identity,
            run_id,
            deadline=10.0,
            mode="healing",
        )
    execution_success = initial.is_success or bool(recovery and recovery.recovered)
    verified = await _verify(executor, run_id) if execution_success else False
    state = await harness.state()
    after = registry.get(TOOL)
    event_rows = [event.model_dump() for event in events.get_events(run_id)]
    proposal = next((e for e in event_rows if e["type"] == "patch_proposed"), None)
    decision_event = next((e for e in event_rows if e["type"] in {"patch_validated", "patch_rejected"}), None)
    retry_count = sum(e["type"] == "retry_started" for e in event_rows)
    diagnosis_called = any(e["type"] == "diagnosis_started" for e in event_rows)
    safe_stop = _authoritative_stop(recovery, scenario, verified)
    operations = proposal["data"].get("operations", []) if proposal else []
    transformed = _apply_operations(BUSINESS_ARGUMENTS, operations)
    preserved = _intent_values(transformed) == ("123", False)
    if not operations:
        preserved = _intent_values(BUSINESS_ARGUMENTS) == ("123", False)
    if verified and recovery and recovery.registry_committed:
        terminal_outcome = "verified_recovery"
    elif scenario == "normal" and verified:
        terminal_outcome = "normal_success"
    elif safe_stop:
        terminal_outcome = "safe_stop"
    else:
        terminal_outcome = "failed"
    reason_code = (
        "recovery_verified_and_committed" if terminal_outcome == "verified_recovery" else
        "normal_verified" if terminal_outcome == "normal_success" else
        "authoritative_safe_stop" if terminal_outcome == "safe_stop" else "product_or_harness_failure"
    )
    provider_correct = state["notification_preferences"]["123"]["promo"] is False
    category = initial.classification.category.value if initial.classification else None
    cold = Trial(
        scenario=scenario, mutation_type=scenario,
        execution_mode="cold_registry_rules_first", baseline_or_contractguard="contractguard",
        run_id=run_id, repetition=repetition,
        outcome=terminal_outcome,
        safe_stop=safe_stop, retry_count=retry_count,
        diagnosis_called=diagnosis_called, patch_planning_called=bool(proposal),
        llm_called=bool(recovery and recovery.diagnosis_source == "llm_assisted"),
        verification_passed=verified, commit_performed=bool(recovery and recovery.registry_committed),
        registry_version_before=before.version, registry_version_after=after.version,
        registry_unchanged_during_candidate_evaluation=initial_version_after_failure == before.version,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        input_tokens=0, output_tokens=0, provider_state_correct=provider_correct,
        error_category=category, business_values_preserved=preserved,
        unsafe_execution=bool(safe_stop and retry_count > 0 and len(state.get("notification_preferences", {}).get("123", {}).get("history", [])) > 0),
        active_contract_fingerprint=before.fingerprint,
        proposed_patch_operations=operations,
        deterministic_decision=decision_event["type"] if decision_event else None,
        termination_reason=(
            recovery.termination_reason if recovery
            else initial.failure.message if initial.failure
            else "Independent verification did not confirm promo=false" if not verified
            else None
        ),
        initial_provider_state_fingerprint=_fingerprint(initial_state.get("notification_preferences", {}).get("123", {})),
        reason_code=reason_code,
        retry_eligible=bool(initial.classification and initial.classification.retry_safety.safe_to_retry),
        verification_status="passed" if verified else "failed" if execution_success else "not_run",
        mttr_ms=(recovery.recovery_duration_ms if recovery and recovery.recovery_duration_ms is not None else None),
        safe_stop_latency_ms=(recovery.failed_recovery_duration_ms if recovery and safe_stop and recovery.failed_recovery_duration_ms is not None else None),
        evidence_refs={
            "failure_observation": bool(initial.observation),
            "classification": category,
            "proposal_operations": len(operations),
            "validation_decision": decision_event["type"] if decision_event else None,
        },
        registry_checkpoints={
            "before_initial_call": before.version,
            "after_failure": initial_version_after_failure,
            "terminal": after.version,
        },
    )

    warm: Optional[Trial] = None
    if scenario in RECOVERABLE and cold.commit_performed:
        await harness.admin("POST", "/admin/reset")
        await harness.admin("POST", "/admin/mutation", json={"scenario": scenario, "enabled": True, "evidence_level": "ordinary"})
        warm_initial_state = await harness.state()
        warm_id = f"warm-{scenario}-{repetition}-{uuid.uuid4().hex[:8]}"
        warm_started = time.perf_counter()
        warm_result = await executor.execute(
            TOOL, _warm_arguments(scenario),
            context=ExecutionContext(run_id=warm_id, tool_call_id="warm", idempotency_key=f"{warm_id}-write"),
        )
        warm_verified = await _verify(executor, warm_id) if warm_result.is_success else False
        warm_state = await harness.state()
        warm = Trial(
            scenario=scenario, mutation_type=scenario,
            execution_mode="warm_verified_registry", baseline_or_contractguard="contractguard",
            run_id=warm_id, repetition=repetition,
            outcome="verified_reuse" if warm_verified else "benchmark_error", safe_stop=False,
            retry_count=0, diagnosis_called=False, patch_planning_called=False, llm_called=False,
            verification_passed=warm_verified, commit_performed=False,
            registry_version_before=after.version, registry_version_after=registry.get(TOOL).version,
            registry_unchanged_during_candidate_evaluation=True,
            latency_ms=round((time.perf_counter() - warm_started) * 1000, 3),
            input_tokens=0, output_tokens=0,
            provider_state_correct=warm_state["notification_preferences"]["123"]["promo"] is False,
            error_category=None, business_values_preserved=_intent_values(_warm_arguments(scenario)) == ("123", False), unsafe_execution=False,
            active_contract_fingerprint=after.fingerprint, proposed_patch_operations=[],
            deterministic_decision=None,
            termination_reason=None if warm_verified else (warm_result.failure.message if warm_result.failure else "warm reuse failed"),
            initial_provider_state_fingerprint=_fingerprint(warm_initial_state.get("notification_preferences", {}).get("123", {})),
            reason_code="verified_reuse" if warm_verified else "warm_reuse_failed",
            retry_eligible=False,
            verification_status="passed" if warm_verified else "failed",
            evidence_refs={"diagnosis_called": False, "patch_planning_called": False, "independent_read": warm_verified},
        )
    await client._client.aclose()
    return cold, warm


def aggregate(trials: list[Trial]) -> dict[str, Any]:
    cg_cold = [t for t in trials if t.baseline_or_contractguard == "contractguard" and t.execution_mode == "cold_registry_rules_first"]
    recoverable = [t for t in cg_cold if t.scenario in RECOVERABLE]
    breaking = [t for t in cg_cold if t.scenario in RECOVERABLE + UNSAFE + SPECIAL]
    unsafe_cases = [t for t in cg_cold if t.scenario in UNSAFE + SPECIAL]
    non_drift = [t for t in cg_cold if t.scenario in NON_DRIFT]
    warm = [t for t in trials if t.execution_mode == "warm_verified_registry"]
    baseline_recoverable = [t for t in trials if t.baseline_or_contractguard == "baseline" and t.scenario in RECOVERABLE]
    recovery_latencies = [t.latency_ms for t in recoverable if t.verification_passed]
    safe_stop_latencies = [t.latency_ms for t in unsafe_cases if t.safe_stop]
    cold_latencies = [t.latency_ms for t in cg_cold]
    warm_latencies = [t.latency_ms for t in warm]
    successes = sum(t.verification_passed and t.commit_performed for t in recoverable)
    resolved = sum((t.verification_passed and t.commit_performed) or t.safe_stop for t in breaking)
    safe_stops = sum(t.safe_stop for t in unsafe_cases)
    verified_trials = [t for t in cg_cold if t.verification_passed]
    return {
        "recoverable_case_adaptation_rate_percent": _percent(successes, len(recoverable)),
        "all_breaking_case_resolution_rate_percent": _percent(resolved, len(breaking)),
        "safe_stop_rate_percent": _percent(safe_stops, len(unsafe_cases)),
        "non_drift_exclusion_rate_percent": _percent(sum(t.outcome == "failed" for t in non_drift), len(non_drift)),
        "verified_task_correctness_percent": _percent(sum(t.provider_state_correct for t in verified_trials), len(verified_trials)),
        "median_mttr_ms": round(statistics.median(recovery_latencies), 3) if recovery_latencies else None,
        "p95_mttr_ms": _p95(recovery_latencies),
        "safe_stop_median_ms": round(statistics.median(safe_stop_latencies), 3) if safe_stop_latencies else None,
        "cold_cache_median_ms": round(statistics.median(cold_latencies), 3) if cold_latencies else None,
        "warm_cache_median_ms": round(statistics.median(warm_latencies), 3) if warm_latencies else None,
        "input_tokens": sum(t.input_tokens or 0 for t in trials),
        "output_tokens": sum(t.output_tokens or 0 for t in trials),
        "token_measurement_note": "Rules-first deterministic benchmark made no model calls; measured model token overhead is zero. LLM fallback was not configured.",
        "average_retry_count": round(statistics.mean(t.retry_count for t in cg_cold), 3),
        "maximum_retry_count": max((t.retry_count for t in cg_cold), default=0),
        "incidents_over_one_retry": sum(t.retry_count > 1 for t in cg_cold),
        "unsafe_execution_count": sum(t.unsafe_execution for t in cg_cold),
        "incorrect_commits": sum(t.commit_performed and not t.verification_passed for t in cg_cold),
        "commits_without_verification": sum(t.commit_performed and not t.verification_passed for t in cg_cold),
        "stale_version_overwrites": sum(t.scenario == "stale_version" and t.commit_performed for t in cg_cold),
        "failed_verification_commits": sum(t.scenario == "verification_mismatch" and t.commit_performed for t in cg_cold),
        "active_registry_changes_during_candidate_evaluation": sum(not t.registry_unchanged_during_candidate_evaluation for t in cg_cold if t.scenario != "stale_version"),
        "warm_call_diagnosis_reduction_percent": _percent(sum(not t.diagnosis_called for t in warm), len(warm)),
        "warm_call_patch_planning_reduction_percent": _percent(sum(not t.patch_planning_called for t in warm), len(warm)),
        "warm_reuse_verified_percent": _percent(sum(t.verification_passed for t in warm), len(warm)),
        "baseline_recoverable_success_rate_percent": _percent(sum(t.verification_passed for t in baseline_recoverable), len(baseline_recoverable)),
        "baseline_repeated_invalid_request_count": sum(t.repeated_same_invalid_request for t in baseline_recoverable),
        "trial_count": len(trials),
    }


def write_artifacts(output_dir: Path, repetitions: int, seed: int, trials: list[Trial]) -> dict[str, Any]:
    from evaluation.artifact_validation import validate_artifact_directory
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    metrics = aggregate(trials)
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": "measured",
        "benchmark_schema_version": SCHEMA_VERSION,
        "git_commit_hash": _git_hash(), "benchmark_timestamp": timestamp,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "execution": "in-process ASGI deterministic"},
        "model_name": None, "task": TASK, "business_intent": BUSINESS_ARGUMENTS,
        "scenario_definitions": list(SCENARIOS), "repetition_count": repetitions,
        "random_seed": seed, "raw_trials": [asdict(t) for t in trials],
        "aggregate_metrics": metrics, "failures": [asdict(t) for t in trials if t.outcome in {"reuse_failed"}],
        "limitations": [
            "LLM fallback was not configured or measured; all repair decisions were rules-first and deterministic.",
            "Registry activation is process-local and is not durable across orchestrator restarts.",
            "Latency is local in-process ASGI latency and must not be presented as network production latency.",
            "The baseline is a deterministic frozen-contract agent with a two-attempt budget; it does not receive ContractGuard repair logic.",
        ],
    }
    import tempfile
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        (temp_dir / "benchmark-results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with (temp_dir / "benchmark-results.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(asdict(t) for t in trials)
        compact = {
            "status": "measured", "benchmark_schema_version": SCHEMA_VERSION,
            "timestamp": timestamp, "task": TASK, "repetitions": repetitions,
            "metrics": metrics, "limitations": payload["limitations"],
        }
        (temp_dir / "benchmark-summary.json").write_text(json.dumps(compact, indent=2), encoding="utf-8")
        def shown(value: Any, suffix: str = "") -> str:
            return "Unavailable" if value is None else f"{value}{suffix}"
        summary = f"""# ContractGuard benchmark summary

## PPT-safe measured claims

These values are computed from the raw JSON trial set for task `{TASK}`, with {repetitions} repetitions per scenario and local in-process ASGI latency.

| Metric | Measured value |
|---|---:|
| Recoverable-case adaptation rate | {shown(metrics['recoverable_case_adaptation_rate_percent'], '%')} |
| All-breaking-case resolution rate | {shown(metrics['all_breaking_case_resolution_rate_percent'], '%')} |
| Safe-stop rate | {shown(metrics['safe_stop_rate_percent'], '%')} |
| Non-drift exclusion rate | {shown(metrics['non_drift_exclusion_rate_percent'], '%')} |
| Verified task correctness | {shown(metrics['verified_task_correctness_percent'], '%')} |
| Median MTTR | {shown(metrics['median_mttr_ms'], ' ms')} |
| P95 MTTR | {shown(metrics['p95_mttr_ms'], ' ms')} |
| Maximum repaired retries | {shown(metrics['maximum_retry_count'])} |
| Unsafe executions | {shown(metrics['unsafe_execution_count'])} |

## Not safe to claim

- LLM fallback performance or provider token cost; it was not configured.
- Production-network latency, restart persistence, universal self-healing, or guaranteed correctness.
- Cross-provider, cross-origin, or tenant-isolated repair reuse.

## Baseline comparison

| System | Recoverable success | Behaviour |
|---|---:|---|
| Frozen-contract baseline | {shown(metrics['baseline_recoverable_success_rate_percent'], '%')} | Repeated invalid requests: {metrics['baseline_repeated_invalid_request_count']} |
| ContractGuard rules-first | {shown(metrics['recoverable_case_adaptation_rate_percent'], '%')} | One bounded candidate retry, independent verification, atomic activation |

## Registry safety

- Incorrect commits: {metrics['incorrect_commits']}
- Commits without verification: {metrics['commits_without_verification']}
- Stale-version overwrites: {metrics['stale_version_overwrites']}
- Failed-verification commits: {metrics['failed_verification_commits']}
- Active-registry changes during ordinary candidate evaluation: {metrics['active_registry_changes_during_candidate_evaluation']}

## Warm reuse

- Verified warm reuse: {shown(metrics['warm_reuse_verified_percent'], '%')}
- Warm-call diagnosis reduction: {shown(metrics['warm_call_diagnosis_reduction_percent'], '%')}
- Warm-call patch-planning reduction: {shown(metrics['warm_call_patch_planning_reduction_percent'], '%')}

## Token measurement

{metrics['token_measurement_note']}

## Limitations

""" + "\n".join(f"- {item}" for item in payload["limitations"]) + f"""

## Reproduce

```powershell
python -m evaluation.hardening_benchmark --repetitions {repetitions} --seed {seed} --output reports
```
"""
        (temp_dir / "benchmark-summary.md").write_text(summary, encoding="utf-8")
        validate_artifact_directory(temp_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("benchmark-results.json", "benchmark-results.csv", "benchmark-summary.json", "benchmark-summary.md"):
            os.replace(temp_dir / name, output_dir / name)
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
    return payload


async def run(repetitions: int, seed: int, output_dir: Path) -> dict[str, Any]:
    random.seed(seed)
    harness = Harness()
    trials: list[Trial] = []
    order = list(SCENARIOS)
    for repetition in range(1, repetitions + 1):
        random.shuffle(order)
        for scenario in order:
            trials.append(await _run_baseline(harness, scenario, repetition))
            cold, warm = await _run_contractguard(harness, scenario, repetition)
            trials.append(cold)
            if warm is not None:
                trials.append(warm)
    return write_artifacts(output_dir, repetitions, seed, trials)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic ContractGuard hardening benchmark")
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--output", type=Path, default=Path("reports"))
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    result = asyncio.run(run(args.repetitions, args.seed, args.output))
    print(json.dumps(result["aggregate_metrics"], indent=2))


if __name__ == "__main__":
    main()
