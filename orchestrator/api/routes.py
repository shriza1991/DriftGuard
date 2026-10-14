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
    execution_type: Optional[str] = Field(
        default=None,
        description="'model_driven_baseline' or 'deterministic'"
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
    model_usage: Optional[Dict[str, Any]] = None
    termination_reason: Optional[str] = None
    execution_type: Optional[str] = None


@router.post("/runs", response_model=RunSummaryResponse, status_code=status.HTTP_200_OK)
async def create_run(request: CreateRunRequest, req: Request):
    """Triggers an agent workflow run.
    - If execution_type == 'model_driven_baseline' or mode == 'baseline':
      Executes genuine LangChain tool-calling baseline agent against frozen v1 contracts.
    - If execution_type == 'model_driven_healing':
      Executes LLM-driven autonomous healing agent.
    - Otherwise:
      Executes deterministic normal workflow.
    """
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    app_registry = getattr(req.app.state, "registry", None)
    app_client = getattr(req.app.state, "mock_client", None)
    app_audit = getattr(req.app.state, "audit_store", None)

    if request.execution_type == "model_driven_baseline" or request.mode == "baseline":
        from orchestrator.agents.baseline import BaselineAgent
        agent = BaselineAgent(client=app_client, audit_store=app_audit)
        result = await agent.execute_workflow(
            run_id=run_id,
            task=request.task,
            client=app_client,
        )
        workflow_manager._runs_store[run_id] = result
    elif request.execution_type == "model_driven_healing":
        from orchestrator.agents.healing import HealingAgent
        agent = HealingAgent(registry=app_registry, client=app_client, audit_store=app_audit)
        result = await agent.execute_workflow(
            run_id=run_id,
            task=request.task,
            client=app_client,
        )
        workflow_manager._runs_store[run_id] = result
    else:
        if app_audit:
            workflow_manager.set_audit_store(app_audit)
        result = await workflow_manager.execute_workflow(
            run_id=run_id,
            mode=request.mode,
            task=request.task,
            registry=app_registry,
            client=app_client,
        )
    return RunSummaryResponse(**result)


@router.get("/runs/{run_id}", response_model=RunSummaryResponse)
async def get_run_status(run_id: str, req: Request):
    """Retrieves status and outcome of an execution run (in-memory or persistent SQLite audit)."""
    run = workflow_manager.get_run(run_id)
    if run:
        return RunSummaryResponse(**run)

    audit_store = getattr(req.app.state, "audit_store", None)
    if audit_store:
        persisted = await audit_store.get_run(run_id)
        if persisted:
            return RunSummaryResponse(
                run_id=persisted.run_id,
                status=persisted.status,
                mode=persisted.mode,
                task=persisted.task,
                created_at=persisted.created_at,
                completed_at=persisted.completed_at,
                duration_ms=persisted.duration_ms,
                outcome=persisted.verified_outcome,
                error=persisted.error,
                termination_reason=persisted.termination_reason,
                execution_type=persisted.execution_type,
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Run '{run_id}' not found"
    )


@router.get("/runs/{run_id}/events")
async def get_run_events_stream(run_id: str, request: Request):
    """Server-Sent Events (SSE) stream for real-time and historical run events."""
    existing_events = event_broadcaster.get_events(run_id)
    if not existing_events:
        audit_store = getattr(request.app.state, "audit_store", None)
        if audit_store:
            persisted_events = await audit_store.get_run_events(run_id)
            existing_events = [
                RunEvent(
                    run_id=pe.run_id,
                    sequence=pe.sequence,
                    timestamp=pe.timestamp,
                    mode=pe.mode,
                    type=pe.event_type,
                    tool=pe.tool,
                    data=pe.data,
                )
                for pe in persisted_events
            ]

    async def event_generator():
        listener_queue = event_broadcaster.register_listener(run_id)
        try:
            # Replay historical events first
            for ev in existing_events:
                yield f"data: {ev.model_dump_json()}\n\n"

            # Check if run has already finished
            run_data = workflow_manager.get_run(run_id)
            if run_data and run_data.get("status") in ["completed", "failed", "interrupted"]:
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
async def get_registry(req: Request):
    """Returns all registered active tool entries, schemas, methods and fingerprints."""
    registry = getattr(req.app.state, "registry", None) or workflow_manager.get_registry()
    from datetime import datetime, timezone
    return RegistryResponse(
        tools=registry.list_active(),
        version="v1",
        updated_at=datetime.now(timezone.utc).isoformat()
    )


@router.get("/patches")
async def get_patches(req: Request, limit: int = 50, offset: int = 0):
    """Returns active and staged patches, alongside sanitized persistent repair history."""
    audit_store = getattr(req.app.state, "audit_store", None)
    repair_history: List[Dict[str, Any]] = []
    if audit_store:
        records = await audit_store.get_all_repair_attempts(limit=limit, offset=offset)
        repair_history = [r.model_dump() for r in records]

    patches = healing_engine.get_patches()
    return {
        "status": "pending_member_2",
        "notice": "Autonomous patch history retrieved from SQLite audit store." if repair_history else "Autonomous patch registry is pending Member 2 implementation.",
        "active_patches": patches,
        "repair_history": repair_history,
        "limit": limit,
        "offset": offset,
        "count": len(repair_history),
    }


@router.post("/demo/reset")
async def demo_reset():
    """Resets both orchestrator runtime in-memory state and downstream mock API fixtures.
    Preserves persistent SQLite audit history and metrics without dropping tables.
    """
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
        "mock_api": mock_status,
        "audit_store": "preserved"
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
async def get_metrics(
    req: Request,
    limit: int = 50,
    offset: int = 0,
    mode: Optional[str] = None
):
    """Returns real measured system metrics without fabricated numbers, including per-run metrics."""
    audit_store = getattr(req.app.state, "audit_store", None)
    return await metrics_tracker.get_metrics(store=audit_store, limit=limit, offset=offset, mode=mode)
