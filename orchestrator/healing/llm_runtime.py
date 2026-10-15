"""Provider-agnostic concurrency and circuit-breaker controls for repair LLM calls."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from orchestrator.config import settings


class RepairLLMCircuitOpen(RuntimeError):
    pass


class RepairLLMRuntime:
    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        self.max_concurrency = max(1, max_concurrency or settings.repair_llm_global_concurrency)
        self.failure_threshold = max(
            1, failure_threshold or settings.repair_llm_breaker_failure_threshold
        )
        self.cooldown_seconds = max(
            0.1, cooldown_seconds or settings.repair_llm_breaker_cooldown_seconds
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._opened_until = 0.0
        self.open_count = 0

    def is_open(self) -> bool:
        return time.monotonic() < self._opened_until

    async def _record_success(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0

    async def _record_failure(self, *, force_open: bool = False) -> None:
        async with self._state_lock:
            self._consecutive_failures += 1
            if force_open or self._consecutive_failures >= self.failure_threshold:
                was_open = time.monotonic() < self._opened_until
                self._opened_until = time.monotonic() + self.cooldown_seconds
                if not was_open:
                    self.open_count += 1

    async def invoke(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        timeout_seconds: float,
    ) -> Any:
        if self.is_open():
            raise RepairLLMCircuitOpen("Repair-model circuit breaker is open")
        async with self._semaphore:
            if self.is_open():
                raise RepairLLMCircuitOpen("Repair-model circuit breaker is open")
            try:
                result = await asyncio.wait_for(call(), timeout=timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc).lower()
                force_open = "429" in message or "rate limit" in message or "rate_limit" in message
                await self._record_failure(force_open=force_open)
                raise
            await self._record_success()
            return result

    async def reset(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0
            self.open_count = 0


repair_llm_runtime = RepairLLMRuntime()
