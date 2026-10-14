"""Evaluation metrics and reporting utilities (Member 1 Scope).

Honest reporting metrics:
- Verified autonomous recoveries / evaluated trials
- Separate tracking of unsafe trials / safe refusals
- Measured recovery time from first observed drift failure to verified completion
- Actual token usage or 'unavailable' (never invent claims)
"""
from typing import Dict, Any, List, Optional
from pydantic import BaseModel


class MetricReport(BaseModel):
    scenario_id: str
    evaluated_trials: int
    verified_recoveries: int
    safe_refusals: int
    unsafe_trials: int
    recovery_rate_percent: float
    avg_recovery_latency_ms: Optional[float]
    token_usage: str = "unavailable"


class EvaluationReporter:
    @staticmethod
    def generate_summary(trials: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trials:
            return {
                "status": "no_data",
                "message": "No benchmark evaluation trials have been run yet.",
                "total_trials": 0,
                "verified_recoveries": 0,
                "unsafe_trials": 0,
                "token_usage": "unavailable"
            }
        
        total = len(trials)
        recoveries = sum(1 for t in trials if t.get("verified_recovery"))
        unsafe = sum(1 for t in trials if t.get("unsafe"))
        
        return {
            "total_trials": total,
            "verified_recoveries": recoveries,
            "unsafe_trials": unsafe,
            "recovery_rate_percent": round((recoveries / total) * 100, 2) if total > 0 else 0.0,
            "token_usage": "unavailable"
        }
