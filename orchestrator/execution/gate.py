"""App-level admission gate for DriftGuard orchestrator.

Coordinates run admission, reset, and mutation operations atomically.
Guarantees:
- Single active run execution in shared-state prototype.
- Rejection of conflicting commands with clear HTTP 409 responses.
- Prevention of check-then-act race conditions via asyncio.Lock.
- Safe release on all error, terminal, and cancellation paths.
- Blocking of new runs when downstream state is marked unknown.
"""
from enum import Enum
import asyncio
from typing import Optional, Dict, Any
from fastapi import HTTPException, status


class AdmissionState(str, Enum):
    IDLE = "idle"
    RUN_ACTIVE = "run_active"
    RESET_ACTIVE = "reset_active"
    MUTATION_ACTIVE = "mutation_active"
    DEMO_STATE_UNKNOWN = "demo_state_unknown"


class AppAdmissionGate:
    """Atomic admission coordinator for orchestrator operations."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._state: AdmissionState = AdmissionState.IDLE
        self._active_run_id: Optional[str] = None
        self._unknown_reason: Optional[str] = None

    @property
    def state(self) -> AdmissionState:
        return self._state

    @property
    def active_run_id(self) -> Optional[str]:
        return self._active_run_id

    async def acquire_run(self, run_id: str) -> None:
        """Atomically checks and admits a new run execution."""
        async with self._lock:
            if self._state == AdmissionState.DEMO_STATE_UNKNOWN:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Demo state is unknown due to prior downstream reset failure: {self._unknown_reason}. "
                        "Please run POST /demo/reset to restore consistent state before starting new runs."
                    )
                )
            if self._state == AdmissionState.RUN_ACTIVE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot start run '{run_id}': another run '{self._active_run_id}' is currently active."
                )
            if self._state in (AdmissionState.RESET_ACTIVE, AdmissionState.MUTATION_ACTIVE):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot start run '{run_id}': demo operation '{self._state.value}' is currently in progress."
                )
            self._state = AdmissionState.RUN_ACTIVE
            self._active_run_id = run_id

    async def release_run(self, run_id: str) -> None:
        """Releases the active run gate, transitioning back to IDLE if matching."""
        async with self._lock:
            if self._active_run_id == run_id:
                self._state = AdmissionState.IDLE
                self._active_run_id = None

    async def acquire_reset(self) -> None:
        """Atomically checks and admits a demo reset operation."""
        async with self._lock:
            if self._state == AdmissionState.RUN_ACTIVE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot execute demo reset while run '{self._active_run_id}' is active."
                )
            if self._state in (AdmissionState.RESET_ACTIVE, AdmissionState.MUTATION_ACTIVE):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot execute demo reset: another operation '{self._state.value}' is currently in progress."
                )
            self._state = AdmissionState.RESET_ACTIVE

    async def mark_reset_success(self) -> None:
        """Transitions to IDLE state after downstream reset successfully finishes."""
        async with self._lock:
            self._state = AdmissionState.IDLE
            self._active_run_id = None
            self._unknown_reason = None

    async def mark_reset_failed(self, reason: str) -> None:
        """Marks the demo state as unknown when downstream reset fails or is uncertain."""
        async with self._lock:
            self._state = AdmissionState.DEMO_STATE_UNKNOWN
            self._unknown_reason = reason
            self._active_run_id = None

    async def acquire_mutation(self) -> None:
        """Atomically checks and admits a demo mutation operation."""
        async with self._lock:
            if self._state == AdmissionState.DEMO_STATE_UNKNOWN:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Cannot configure mutation: demo state is unknown due to prior downstream reset failure: {self._unknown_reason}. "
                        "Please run POST /demo/reset first."
                    )
                )
            if self._state == AdmissionState.RUN_ACTIVE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot configure mutation while run '{self._active_run_id}' is active."
                )
            if self._state != AdmissionState.IDLE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot configure mutation while operation is active ({self._state.value})."
                )
            self._state = AdmissionState.MUTATION_ACTIVE

    async def release_mutation(self) -> None:
        """Releases the mutation gate back to IDLE."""
        async with self._lock:
            if self._state == AdmissionState.MUTATION_ACTIVE:
                self._state = AdmissionState.IDLE

    def reset_state(self) -> None:
        """Synchronously resets the gate state (for test fixtures)."""
        self._state = AdmissionState.IDLE
        self._active_run_id = None
        self._unknown_reason = None

    def get_status(self) -> Dict[str, Any]:
        """Returns the current admission gate status."""
        return {
            "state": self._state.value,
            "active_run_id": self._active_run_id,
            "unknown_reason": self._unknown_reason,
        }


admission_gate = AppAdmissionGate()
