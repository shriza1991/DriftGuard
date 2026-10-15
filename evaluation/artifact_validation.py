"""Fail-closed validation for ContractGuard benchmark artifacts."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
ALLOWED_OUTCOMES = {
    "normal_success", "verified_recovery", "verified_reuse", "safe_stop",
    "completed", "failed", "benchmark_error", "stopped_with_error", "reuse_failed",
}
SECRET_PATTERN = re.compile(r"(?i)(?:bearer\s+[a-z0-9._-]{8,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,]+|sk-[a-z0-9]{12,})")


class ArtifactValidationError(ValueError):
    pass


def _fail(message: str) -> None:
    raise ArtifactValidationError(message)


def _trial_key(trial: dict[str, Any]) -> tuple[Any, ...]:
    repetition = trial.get("repetition")
    try:
        repetition = int(repetition)
    except (TypeError, ValueError):
        pass
    return (
        trial.get("pair_id"), trial.get("scenario"), repetition,
        trial.get("execution_mode"), trial.get("baseline_or_contractguard"),
    )


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def validate_payload(payload: dict[str, Any], *, csv_rows: list[dict[str, Any]] | None = None) -> None:
    """Validate raw trials and aggregate safety invariants before publication."""
    if payload.get("benchmark_schema_version") != SCHEMA_VERSION:
        _fail("missing or unsupported benchmark schema version")
    trials = payload.get("raw_trials")
    if not isinstance(trials, list) or not trials:
        _fail("raw_trials must be a non-empty list")
    scenarios = payload.get("scenario_definitions")
    repetitions = payload.get("repetition_count")
    if not isinstance(scenarios, list) or not scenarios or not isinstance(repetitions, int) or repetitions < 1:
        _fail("scenario definitions and repetition count are required")
    if payload.get("task") != "Disable promotional notifications for user 123.":
        _fail("benchmark task is not the fixed notification task")
    if payload.get("business_intent") != {"user_id": "123", "promo": False}:
        _fail("logical business intent is not immutable")

    keys: set[tuple[Any, ...]] = set()
    for trial in trials:
        if not isinstance(trial, dict):
            _fail("trial is not an object")
        if _trial_key(trial) in keys:
            _fail(f"duplicate trial identity: {_trial_key(trial)}")
        keys.add(_trial_key(trial))
        if trial.get("scenario") not in scenarios:
            _fail("trial references an unknown scenario")
        if trial.get("repetition") not in range(1, repetitions + 1):
            _fail("trial repetition is outside the declared range")
        if trial.get("outcome") not in ALLOWED_OUTCOMES:
            _fail("unknown terminal outcome")
        if trial.get("baseline_or_contractguard") == "contractguard" and trial.get("retry_count", 0) > 1:
            _fail("ContractGuard exceeded one candidate retry")
        if trial.get("safe_stop") is True and trial.get("outcome") != "safe_stop":
            _fail("safe_stop flag does not match authoritative outcome")
        if trial.get("outcome") == "safe_stop" and trial.get("safe_stop") is not True:
            _fail("safe_stop outcome is missing its authoritative flag")
        if trial.get("commit_performed") and not trial.get("verification_passed"):
            _fail("commit occurred without independent verification")
        if trial.get("commit_performed") and trial.get("scenario") in {
            "missing_business_value", "unavailable_credential", "ambiguous_mapping",
            "authentication_failure", "permission_failure", "rate_limit", "provider_outage",
            "malformed_error", "verification_mismatch", "stale_version",
        }:
            _fail("unsafe or non-drift scenario committed a patch")
        if trial.get("outcome") == "safe_stop" and trial.get("commit_performed"):
            _fail("safe stop committed a patch")
        if trial.get("outcome") in {"safe_stop", "verified_recovery", "verified_reuse", "normal_success"}:
            required = ("reason_code", "verification_status", "commit_status", "retry_eligible")
            if any(field not in trial for field in required):
                _fail("terminal trial is missing typed decision evidence")
        if trial.get("outcome") in {"safe_stop", "verified_recovery", "verified_reuse", "normal_success"} and not trial.get("evidence_refs"):
            _fail("terminal trial is missing evidence references")
        if trial.get("business_values_preserved") is not True:
            _fail("trial did not prove business-value preservation")
        if trial.get("outcome") in {"verified_recovery", "verified_reuse", "normal_success"} and trial.get("provider_state_correct") is not True:
            _fail("successful trial lacks independently correct provider state")
        for value in _walk_strings(trial):
            if SECRET_PATTERN.search(value):
                _fail("secret-like value found in raw trial")

    expected = repetitions * (len(scenarios) * 2 + sum(1 for s in scenarios if s in {"rename", "type_change", "nesting", "required_header", "combined"}))
    if len(trials) != expected:
        _fail(f"expected {expected} trials for scenario/repetition/mode matrix, got {len(trials)}")
    if csv_rows is not None:
        if len(csv_rows) != len(trials):
            _fail("CSV and JSON trial counts differ")
        json_ids = {_trial_key(t) for t in trials}
        csv_ids = {_trial_key(t) for t in csv_rows}
        if json_ids != csv_ids:
            _fail("CSV and JSON trial identities differ")

    metrics = payload.get("aggregate_metrics")
    if not isinstance(metrics, dict):
        _fail("aggregate_metrics is missing")
    cg = [t for t in trials if t.get("baseline_or_contractguard") == "contractguard"]
    if metrics.get("maximum_retry_count", 0) > 1 or metrics.get("incidents_over_one_retry", 0) != 0:
        _fail("aggregate retry safety invariant failed")
    if metrics.get("unsafe_execution_count", 0) != sum(bool(t.get("unsafe_execution")) for t in cg):
        _fail("unsafe execution aggregate cannot be recomputed from raw trials")
    if metrics.get("incorrect_commits", 0) != sum(bool(t.get("commit_performed") and not t.get("verification_passed")) for t in cg):
        _fail("incorrect commit aggregate cannot be recomputed from raw trials")


def validate_artifact_directory(directory: Path) -> None:
    results = directory / "benchmark-results.json"
    csv_path = directory / "benchmark-results.csv"
    summary_json = directory / "benchmark-summary.json"
    summary_md = directory / "benchmark-summary.md"
    if not all(path.exists() for path in (results, csv_path, summary_json, summary_md)):
        _fail("benchmark artifact set is incomplete")
    try:
        payload = json.loads(results.read_text(encoding="utf-8"))
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        _fail(f"artifact parse failed: {exc}")
    validate_payload(payload, csv_rows=rows)
    compact = json.loads(summary_json.read_text(encoding="utf-8"))
    if compact.get("status") != "measured":
        _fail("compact summary is not measured")
    if compact.get("metrics") != payload.get("aggregate_metrics"):
        _fail("compact summary metrics disagree with raw aggregate")
    markdown = summary_md.read_text(encoding="utf-8")
    if "Not safe to claim" not in markdown or "PPT-safe measured claims" not in markdown:
        _fail("Markdown summary lacks explicit claim sections")
