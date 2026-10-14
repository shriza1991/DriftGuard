"""App-managed background task manager for DriftGuard orchestrator.

Tracks active workflow run tasks, ensures exception logging, and provides
finite-budget cancellation/awaiting on lifespan shutdown to prevent orphaned tasks.
"""
import asyncio
import logging
from typing import Dict, Optional, Any

logger = logging.getLogger("orchestrator.tasks")


class AppTaskManager:
    """Manages execution and shutdown of background run tasks."""

    def __init__(self):
        self._active_tasks: Dict[str, asyncio.Task] = {}

    def spawn_run_task(self, run_id: str, coro) -> asyncio.Task:
        """Spawns and tracks an app-managed background task for a run."""
        task = asyncio.create_task(coro, name=f"run-worker-{run_id}")
        self._active_tasks[run_id] = task

        def _cleanup(t: asyncio.Task):
            self._active_tasks.pop(run_id, None)
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    logger.error(f"Run task for '{run_id}' failed with unhandled exception: {exc}", exc_info=exc)

        task.add_done_callback(_cleanup)
        return task

    def get_task(self, run_id: str) -> Optional[asyncio.Task]:
        """Retrieves active task for run_id if currently running."""
        return self._active_tasks.get(run_id)

    def has_active_task(self, run_id: str) -> bool:
        """Checks if run_id has an active, non-completed task."""
        t = self._active_tasks.get(run_id)
        return t is not None and not t.done()

    async def cancel_and_wait_all(self, timeout: float = 5.0) -> None:
        """Cancels and awaits all active tasks within a finite budget on shutdown."""
        tasks = [t for t in self._active_tasks.values() if not t.done()]
        if not tasks:
            return

        for t in tasks:
            t.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"Timed out after {timeout}s waiting for {len(tasks)} tasks to cancel during shutdown.")
        finally:
            self._active_tasks.clear()

    def reset(self) -> None:
        """Synchronously clears tracking (for test teardowns)."""
        for t in self._active_tasks.values():
            if not t.done():
                t.cancel()
        self._active_tasks.clear()


app_task_manager = AppTaskManager()
