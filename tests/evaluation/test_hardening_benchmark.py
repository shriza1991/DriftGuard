import json

import pytest

from evaluation.hardening_benchmark import RECOVERABLE, run


@pytest.mark.asyncio
async def test_hardening_benchmark_writes_traceable_safe_artifacts(tmp_path):
    payload = await run(repetitions=1, seed=20261015, output_dir=tmp_path)
    metrics = payload["aggregate_metrics"]

    assert payload["task"] == "Disable promotional notifications for user 123."
    assert payload["business_intent"] == {"user_id": "123", "promo": False}
    assert metrics["recoverable_case_adaptation_rate_percent"] == 100.0
    assert metrics["safe_stop_rate_percent"] == 100.0
    assert metrics["maximum_retry_count"] == 1
    assert metrics["incidents_over_one_retry"] == 0
    assert metrics["unsafe_execution_count"] == 0
    assert metrics["incorrect_commits"] == 0
    assert metrics["failed_verification_commits"] == 0
    assert metrics["stale_version_overwrites"] == 0

    raw = json.loads((tmp_path / "benchmark-results.json").read_text(encoding="utf-8"))
    assert raw["raw_trials"]
    assert all(t["business_values_preserved"] for t in raw["raw_trials"])
    assert all(t["retry_count"] <= 1 for t in raw["raw_trials"] if t["baseline_or_contractguard"] == "contractguard")
    assert {t["scenario"] for t in raw["raw_trials"] if t["outcome"] == "verified_reuse"} == set(RECOVERABLE)
    assert (tmp_path / "benchmark-results.csv").exists()
    assert (tmp_path / "benchmark-summary.md").exists()
    assert (tmp_path / "benchmark-summary.json").exists()
