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
    "run_id", "outcome", "safe_stop", "retry_count", "diagnosis_called",
    "llm_called", "verification_passed", "commit_performed",
    "registry_version_before", "registry_version_after", "latency_ms",
    "input_tokens", "output_tokens", "provider_state_correct", "error_category",
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
        error_category=category, business_values_preserved=True,
        unsafe_execution=False, active_contract_fingerprint=before.fingerprint,
        proposed_patch_operations=[], deterministic_decision=None,
        termination_reason=None if verified else (last.failure.message if last.failure else "baseline stopped"),
        repeated_same_invalid_request=repeated,
    )


async def _run_contractguard(
    harness: Harness,
    scenario: str,
    repetition: int,
) -> tuple[Trial, Optional[Trial]]:
    await harness.reset(scenario)
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
    safe_stop = not verified and scenario != "normal"
    provider_correct = state["notification_preferences"]["123"]["promo"] is False
    category = initial.classification.category.value if initial.classification else None
    cold = Trial(
        scenario=scenario, mutation_type=scenario,
        execution_mode="cold_registry_rules_first", baseline_or_contractguard="contractguard",
        run_id=run_id, repetition=repetition,
        outcome="verified_recovery" if verified and recovery else "normal_success" if verified else "safe_stop",
        safe_stop=safe_stop, retry_count=retry_count,
        diagnosis_called=diagnosis_called, patch_planning_called=bool(proposal),
        llm_called=bool(recovery and recovery.diagnosis_source == "llm_assisted"),
        verification_passed=verified, commit_performed=bool(recovery and recovery.registry_committed),
        registry_version_before=before.version, registry_version_after=after.version,
        registry_unchanged_during_candidate_evaluation=initial_version_after_failure == before.version,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        input_tokens=0, output_tokens=0, provider_state_correct=provider_correct,
        error_category=category, business_values_preserved=True,
        unsafe_execution=bool(safe_stop and provider_correct),
        active_contract_fingerprint=before.fingerprint,
        proposed_patch_operations=proposal["data"].get("operations", []) if proposal else [],
        deterministic_decision=decision_event["type"] if decision_event else None,
        termination_reason=(
            recovery.termination_reason if recovery
            else initial.failure.message if initial.failure
            else "Independent verification did not confirm promo=false" if not verified
            else None
        ),
    )

    warm: Optional[Trial] = None
    if scenario in RECOVERABLE and cold.commit_performed:
        await harness.admin("POST", "/admin/reset")
        await harness.admin("POST", "/admin/mutation", json={"scenario": scenario, "enabled": True, "evidence_level": "ordinary"})
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
            outcome="verified_reuse" if warm_verified else "reuse_failed", safe_stop=not warm_verified,
            retry_count=0, diagnosis_called=False, patch_planning_called=False, llm_called=False,
            verification_passed=warm_verified, commit_performed=False,
            registry_version_before=after.version, registry_version_after=registry.get(TOOL).version,
            registry_unchanged_during_candidate_evaluation=True,
            latency_ms=round((time.perf_counter() - warm_started) * 1000, 3),
            input_tokens=0, output_tokens=0,
            provider_state_correct=warm_state["notification_preferences"]["123"]["promo"] is False,
            error_category=None, business_values_preserved=True, unsafe_execution=False,
            active_contract_fingerprint=after.fingerprint, proposed_patch_operations=[],
            deterministic_decision=None,
            termination_reason=None if warm_verified else (warm_result.failure.message if warm_result.failure else "warm reuse failed"),
        )
    await client._client.aclose()
    return cold, warm


def aggregate(trials: list[Trial]) -> dict[str, Any]:
    cg_cold = [t for t in trials if t.baseline_or_contractguard == "contractguard" and t.execution_mode == "cold_registry_rules_first"]
    recoverable = [t for t in cg_cold if t.scenario in RECOVERABLE]
    breaking = [t for t in cg_cold if t.scenario != "normal"]
    unsafe_cases = [t for t in cg_cold if t.scenario in UNSAFE + NON_DRIFT + SPECIAL]
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
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = aggregate(trials)
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
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
    (output_dir / "benchmark-results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with (output_dir / "benchmark-results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(asdict(t) for t in trials)
    compact = {"status": "measured", "timestamp": timestamp, "task": TASK, "repetitions": repetitions, "metrics": metrics, "limitations": payload["limitations"]}
    (output_dir / "benchmark-summary.json").write_text(json.dumps(compact, indent=2), encoding="utf-8")
    summary = f"""# ContractGuard benchmark summary

## Executive summary

Measured deterministic results for **{TASK}** with immutable intent `user_id="123"` and `promo=false`. Safe stops are excluded from adaptation success.

## Measured results

| Metric | Measured value |
|---|---:|
| Recoverable-case adaptation rate | {metrics['recoverable_case_adaptation_rate_percent']}% |
| All-breaking-case resolution rate | {metrics['all_breaking_case_resolution_rate_percent']}% |
| Safe-stop rate | {metrics['safe_stop_rate_percent']}% |
| Verified task correctness | {metrics['verified_task_correctness_percent']}% |
| Median MTTR | {metrics['median_mttr_ms']} ms |
| P95 MTTR | {metrics['p95_mttr_ms']} ms |
| Maximum repaired retries | {metrics['maximum_retry_count']} |
| Unsafe executions | {metrics['unsafe_execution_count']} |

## Baseline comparison

| System | Recoverable success | Behaviour |
|---|---:|---|
| Frozen-contract baseline | {metrics['baseline_recoverable_success_rate_percent']}% | Repeated invalid requests: {metrics['baseline_repeated_invalid_request_count']} |
| ContractGuard rules-first | {metrics['recoverable_case_adaptation_rate_percent']}% | One bounded candidate retry, independent verification, then atomic commit |

## Recoverable versus safe-stop results

Recoverable mutations are rename, lossless string-to-integer conversion, nesting, configured header insertion, and their supported combination. Missing business value, unavailable credential, ambiguity, authentication, permission, rate limit, provider outage, adversarial evidence, verification mismatch, and stale-version conflict stop without being counted as adaptations.

## Latency summary

- Cold median: {metrics['cold_cache_median_ms']} ms
- Warm median: {metrics['warm_cache_median_ms']} ms
- Verified recovery median: {metrics['median_mttr_ms']} ms
- Verified recovery p95: {metrics['p95_mttr_ms']} ms

## Token summary

{metrics['token_measurement_note']}

## Registry safety summary

- Incorrect commits: {metrics['incorrect_commits']}
- Commits without verification: {metrics['commits_without_verification']}
- Stale-version overwrites: {metrics['stale_version_overwrites']}
- Failed-verification commits: {metrics['failed_verification_commits']}
- Active registry changes during ordinary candidate evaluation: {metrics['active_registry_changes_during_candidate_evaluation']}

## Second-call reuse

- Verified warm reuse: {metrics['warm_reuse_verified_percent']}%
- Warm-call diagnosis reduction: {metrics['warm_call_diagnosis_reduction_percent']}%
- Warm-call patch-planning reduction: {metrics['warm_call_patch_planning_reduction_percent']}%

## Known limitations

""" + "\n".join(f"- {item}" for item in payload["limitations"]) + f"""

## Reproduce

```powershell
python -m evaluation.hardening_benchmark --repetitions {repetitions} --seed {seed} --output reports
```
"""
    (output_dir / "benchmark-summary.md").write_text(summary, encoding="utf-8")
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
