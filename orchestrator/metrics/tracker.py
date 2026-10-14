from typing import Optional, Dict, Any
from pydantic import BaseModel


class SystemMetrics(BaseModel):
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


class MetricsTracker:
    def __init__(self):
        self._metrics = SystemMetrics()

    def record_run(self, mode: str, success: bool, duration_ms: float):
        self._metrics.total_runs += 1
        if mode == "normal":
            self._metrics.normal_runs += 1
            if success:
                self._metrics.normal_success_count += 1
        elif mode == "healing":
            self._metrics.healing_trials += 1
            # In scaffolding, healing is pending, so recoveries remain 0

    def get_metrics(self) -> SystemMetrics:
        return self._metrics

    def reset(self):
        self._metrics = SystemMetrics()


metrics_tracker = MetricsTracker()
