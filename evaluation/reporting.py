"""Honest aggregation for persisted or in-memory evaluation trials."""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class MetricReport(BaseModel):
    evaluated_trials: int
    recoverable_trials: int
    verified_recoveries: int
    unsafe_trials: int
    safe_refusals: int
    recovery_rate_percent: Optional[float]
    safe_refusal_rate_percent: Optional[float]
    mean_recovery_latency_ms: Optional[float]
    median_recovery_latency_ms: Optional[float]
    p95_recovery_latency_ms: Optional[float]
    token_usage_available_trials: int
    token_usage: str


class EvaluationReporter:
    @staticmethod
    def generate_summary(trials: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trials:
            return {
                "status": "no_data",
                "message": "No benchmark evaluation trials have been run yet.",
                "evaluated_trials": 0,
                "recoverable_trials": 0,
                "verified_recoveries": 0,
                "unsafe_trials": 0,
                "safe_refusals": 0,
                "recovery_rate_percent": None,
                "safe_refusal_rate_percent": None,
                "mean_recovery_latency_ms": None,
                "median_recovery_latency_ms": None,
                "p95_recovery_latency_ms": None,
                "token_usage_available_trials": 0,
                "token_usage": "unavailable",
            }

        recoverable = [t for t in trials if t.get("expected_outcome") == "heal_and_recover"]
        unsafe = [t for t in trials if t.get("expected_outcome") == "safe_refusal"]
        recoveries = sum(bool(t.get("verified_recovery")) for t in recoverable)
        refusals = sum(bool(t.get("safe_refusal")) for t in unsafe)
        latencies = [
            float(t["recovery_time_ms"])
            for t in recoverable
            if t.get("verified_recovery") and isinstance(t.get("recovery_time_ms"), (int, float))
        ]
        p95 = None
        if len(latencies) >= 20:
            p95 = round(statistics.quantiles(latencies, n=100, method="inclusive")[94], 2)

        return MetricReport(
            evaluated_trials=len(trials),
            recoverable_trials=len(recoverable),
            verified_recoveries=recoveries,
            unsafe_trials=len(unsafe),
            safe_refusals=refusals,
            recovery_rate_percent=(round(100 * recoveries / len(recoverable), 2) if recoverable else None),
            safe_refusal_rate_percent=(round(100 * refusals / len(unsafe), 2) if unsafe else None),
            mean_recovery_latency_ms=(round(statistics.mean(latencies), 2) if latencies else None),
            median_recovery_latency_ms=(round(statistics.median(latencies), 2) if latencies else None),
            p95_recovery_latency_ms=p95,
            token_usage_available_trials=sum(
                isinstance(t.get("total_tokens"), int) and not isinstance(t.get("total_tokens"), bool)
                for t in trials
            ),
            token_usage=(
                "available"
                if all(isinstance(t.get("total_tokens"), int) for t in trials)
                else "partially_available"
                if any(isinstance(t.get("total_tokens"), int) for t in trials)
                else "unavailable"
            ),
        ).model_dump()
