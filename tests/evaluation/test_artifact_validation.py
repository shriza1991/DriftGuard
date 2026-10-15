import copy
from functools import lru_cache

import pytest

from evaluation.artifact_validation import ArtifactValidationError, validate_payload


@lru_cache(maxsize=1)
def _payload_template():
    from evaluation.hardening_benchmark import Harness, SCENARIOS, _run_baseline, _run_contractguard
    import asyncio

    async def build():
        harness = Harness()
        trials = []
        for scenario in SCENARIOS:
            trials.append(await _run_baseline(harness, scenario, 1))
            cold, warm = await _run_contractguard(harness, scenario, 1)
            trials.extend([cold] + ([warm] if warm else []))
        from dataclasses import asdict
        from evaluation.hardening_benchmark import aggregate, BUSINESS_ARGUMENTS, TASK
        return {
            "benchmark_schema_version": "1.0", "raw_trials": [asdict(t) for t in trials],
            "scenario_definitions": list(SCENARIOS), "repetition_count": 1,
            "task": TASK, "business_intent": BUSINESS_ARGUMENTS,
            "aggregate_metrics": aggregate(trials),
        }
    return asyncio.run(build())


def _payload():
    return copy.deepcopy(_payload_template())


@pytest.mark.parametrize("change", [
    lambda t: t.update(safe_stop=True),
    lambda t: t.update(retry_count=2),
    lambda t: t.update(commit_performed=True, verification_passed=False),
])
def test_invalid_safety_metrics_fail_closed(change):
    payload = _payload()
    target = next(t for t in payload["raw_trials"] if t["baseline_or_contractguard"] == "contractguard")
    change(target)
    with pytest.raises(ArtifactValidationError):
        validate_payload(payload)


def test_duplicate_trial_identity_is_rejected():
    payload = _payload()
    payload["raw_trials"].append(copy.deepcopy(payload["raw_trials"][0]))
    with pytest.raises(ArtifactValidationError, match="duplicate trial identity"):
        validate_payload(payload)
