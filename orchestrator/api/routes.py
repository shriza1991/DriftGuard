import uuid
import json
import asyncio
from typing import Optional, Dict, Any, List
import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, status
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from orchestrator.config import settings
from orchestrator.registry.models import RegistryResponse, ToolRegistryEntry
from orchestrator.execution.workflow import workflow_manager
from orchestrator.execution.events import event_broadcaster, RunEvent
from orchestrator.healing.stubs import healing_engine, ProposedPatch
from orchestrator.metrics.tracker import metrics_tracker, SystemMetrics


router = APIRouter()


class CreateRunRequest(BaseModel):
    mode: str = Field(default="normal", description="'normal' or 'healing'")
    task: str = Field(
        default="Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
        description="Target user task"
    )


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    mode: str
    task: str
    created_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    outcome: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@router.post("/runs", response_model=RunSummaryResponse, status_code=status.HTTP_200_OK)
async def create_run(request: CreateRunRequest):
    """Triggers an agent workflow run.
    In normal mode: executes Find Maya -> Assign 81 -> Verify via get_ticket.
    In healing mode: executes until failure, where healing is explicitly reported as pending.
    """
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    result = await workflow_manager.execute_workflow(
        run_id=run_id,
        mode=request.mode,
        task=request.task
    )
    return RunSummaryResponse(**result)


@router.get("/runs/{run_id}", response_model=RunSummaryResponse)
async def get_run_status(run_id: str):
    """Retrieves status and outcome of an execution run."""
    run = workflow_manager.get_run(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found"
        )
    return RunSummaryResponse(**run)


@router.get("/runs/{run_id}/events")
async def get_run_events_stream(run_id: str, request: Request):
    """Server-Sent Events (SSE) stream for real-time and historical run events."""
    existing_events = event_broadcaster.get_events(run_id)

    async def event_generator():
        listener_queue = event_broadcaster.register_listener(run_id)
        try:
            # Replay historical events first
            for ev in existing_events:
                yield f"data: {ev.model_dump_json()}\n\n"

            # Check if run has already finished
            run_data = workflow_manager.get_run(run_id)
            if run_data and run_data.get("status") in ["completed", "failed"]:
                return

            # Listen for new events
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event: RunEvent = await asyncio.wait_for(listener_queue.get(), timeout=2.0)
                    yield f"data: {event.model_dump_json()}\n\n"
                    if event.type in ["run_completed", "run_failed"]:
                        break
                except asyncio.TimeoutError:
                    # Keepalive heartbeat
                    yield ": ping\n\n"
        finally:
            event_broadcaster.unregister_listener(run_id, listener_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/registry", response_model=RegistryResponse)
async def get_registry():
    """Returns all registered active tool entries, schemas, methods and fingerprints."""
    registry = workflow_manager.get_registry()
    from datetime import datetime, timezone
    return RegistryResponse(
        tools=registry.list_active(),
        version="v1",
        updated_at=datetime.now(timezone.utc).isoformat()
    )


@router.get("/patches")
async def get_patches():
    """Returns active and staged patches (empty in v1 scaffolding)."""
    patches = healing_engine.get_patches()
    return {
        "status": "pending_member_2",
        "notice": "Autonomous patch registry is pending Member 2 implementation.",
        "active_patches": patches
    }


@router.post("/demo/reset")
async def demo_reset():
    """Resets both orchestrator run state and downstream mock API fixtures."""
    workflow_manager.reset()

    # Forward reset to Mock API
    mock_url = f"{settings.mock_api_url.rstrip('/')}/admin/reset"
    mock_status = "unavailable"
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            res = await client.post(mock_url)
            if res.status_code == 200:
                mock_status = "reset_successful"
        except Exception as e:
            mock_status = f"failed_to_reach_mock: {str(e)}"

    return {
        "status": "reset_completed",
        "orchestrator": "state_cleared",
        "mock_api": mock_status
    }


@router.post("/demo/mutation")
async def demo_mutation():
    """Mutation controls are explicitly disabled until Member 1 runtime mutations are integrated."""
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "status": "not_implemented",
            "message": "Mutation controls are disabled in this milestone. Runtime drift mutation execution is pending Member 1 & 2 integration."
        }
    )


@router.get("/metrics", response_model=SystemMetrics)
async def get_metrics():
    """Returns real measured system metrics without fabricated numbers."""
    return metrics_tracker.get_metrics()
