import uuid
import json
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from orchestrator.config import settings
from orchestrator.registry.models import RegistryResponse
from orchestrator.registry import VersionedToolRegistry
from orchestrator.execution.workflow import workflow_manager
from orchestrator.execution.events import event_broadcaster, RunEvent, EventSubscriber
from orchestrator.execution.gate import admission_gate, AppAdmissionGate, AdmissionState
from orchestrator.execution.tasks import app_task_manager, AppTaskManager
from orchestrator.client.mock_client import MockAdminApiClient
from orchestrator.healing.stubs import healing_engine
from orchestrator.metrics.tracker import metrics_tracker, SystemMetrics


router = APIRouter()


ALLOWED_SCENARIOS = {
    "none",
    "rename",
    "type_change",
    "nesting",
    "required_header",
    "combined",
    "missing_business_value",
    "unavailable_credential",
    "ambiguous_mapping",
}

ALLOWED_EVIDENCE_LEVELS = {"ordinary", "minimal", "rich"}

VALID_MODES = {"normal", "healing", "baseline"}
VALID_EXECUTION_TYPES = {None, "deterministic", "model_driven_baseline", "model_driven_healing"}


class CreateRunRequest(BaseModel):
    mode: str = Field(default="normal", description="'normal', 'healing', or 'baseline'")
    task: str = Field(
        default="Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
        description="Target user task"
    )
    execution_type: Optional[str] = Field(
        default=None,
        description="'model_driven_baseline', 'model_driven_healing', or 'deterministic'"
    )
    wait: Optional[bool] = Field(
        default=False,
        description="If True, awaits run completion before returning. If False, returns promptly with status='running'."
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


class AdminMutationRequest(BaseModel):
    scenario: str = Field(..., description="Drift scenario to enable on downstream mock API")
    enabled: bool = Field(default=True, description="Enable or disable the scenario")
    evidence_level: str = Field(default="ordinary", description="Evidence level: ordinary, minimal, rich")


@router.post("/runs", response_model=RunSummaryResponse, status_code=status.HTTP_200_OK)
async def create_run(request: CreateRunRequest, req: Request):
    """Triggers an agent workflow run.
    - Validates mode and execution_type combinations. Conflicting/unsupported values fail immediately with 400.
    - Coordinates admission atomically through AppAdmissionGate. Rejects concurrent runs/demo ops with 409.
    - Allocates and persists the run start in SQLite audit store immediately.
    - Executes the workflow inside an app-managed background task (no orphaned tasks).
    - Returns prompt run-ID if wait=False (default), or awaits completion if wait=True.
    - Handles model configuration/execution failures truthfully, finalizing terminal state once.
    """
    # 1. Validation
    if request.mode not in VALID_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported mode '{request.mode}'. Supported modes: {sorted(list(VALID_MODES))}"
        )

    if request.execution_type not in VALID_EXECUTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported execution_type '{request.execution_type}'. Supported: {sorted([t for t in VALID_EXECUTION_TYPES if t is not None])}"
        )

    # Validate mode and execution_type compatibility
    if request.mode == "baseline" and request.execution_type in ("deterministic", "model_driven_healing"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conflicting mode '{request.mode}' and execution_type '{request.execution_type}'"
        )
    if request.mode == "healing" and request.execution_type == "model_driven_baseline":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conflicting mode '{request.mode}' and execution_type '{request.execution_type}'"
        )
    if request.mode == "normal" and request.execution_type == "model_driven_healing":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conflicting mode '{request.mode}' and execution_type '{request.execution_type}'"
        )

    effective_exec_type = request.execution_type
    if effective_exec_type is None:
        effective_exec_type = "model_driven_baseline" if request.mode == "baseline" else "deterministic"

    # 2. Gate Admission
    gate: AppAdmissionGate = getattr(req.app.state, "admission_gate", None) or admission_gate
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    await gate.acquire_run(run_id)

    # 3. Initial Run Record & Persistence
    now_iso = datetime.now(timezone.utc).isoformat()
    initial_record = {
        "run_id": run_id,
        "status": "running",
        "mode": request.mode,
        "task": request.task,
        "created_at": now_iso,
        "completed_at": None,
        "duration_ms": None,
        "outcome": None,
        "error": None,
        "model_usage": None,
        "termination_reason": None,
        "execution_type": effective_exec_type,
    }
    workflow_manager._runs_store[run_id] = initial_record

    audit_store = getattr(req.app.state, "audit_store", None)
    if audit_store:
        try:
            await audit_store.record_run_start(
                run_id=run_id,
                mode=request.mode,
                task=request.task,
                execution_type=effective_exec_type,
                created_at=now_iso,
            )
        except Exception:
            pass

    # 4. Managed Background Task Definition
    app_registry = getattr(req.app.state, "registry", None)
    app_client = getattr(req.app.state, "mock_client", None)

    async def _execute_workflow_task():
        try:
            if effective_exec_type == "model_driven_baseline":
                from orchestrator.agents.baseline import BaselineAgent
                agent = BaselineAgent(client=app_client, audit_store=audit_store)
                result = await agent.execute_workflow(
                    run_id=run_id,
                    task=request.task,
                    client=app_client,
                )
                workflow_manager._runs_store[run_id] = result
            elif effective_exec_type == "model_driven_healing":
                from orchestrator.agents.healing import HealingAgent
                agent = HealingAgent(registry=app_registry, client=app_client, audit_store=audit_store)
                result = await agent.execute_workflow(
                    run_id=run_id,
                    task=request.task,
                    client=app_client,
                )
                workflow_manager._runs_store[run_id] = result
            else:
                if audit_store:
                    workflow_manager.set_audit_store(audit_store)
                result = await workflow_manager.execute_workflow(
                    run_id=run_id,
                    mode=request.mode,
                    task=request.task,
                    registry=app_registry,
                    client=app_client,
                )
                workflow_manager._runs_store[run_id] = result
        except asyncio.CancelledError:
            completed_iso = datetime.now(timezone.utc).isoformat()
            cancel_msg = "Run cancelled during server shutdown"
            cancelled_record = {
                "run_id": run_id,
                "status": "interrupted",
                "mode": request.mode,
                "task": request.task,
                "created_at": now_iso,
                "completed_at": completed_iso,
                "duration_ms": 0.0,
                "outcome": None,
                "error": cancel_msg,
                "termination_reason": "shutdown_cancelled",
                "execution_type": effective_exec_type,
            }
            workflow_manager._runs_store[run_id] = cancelled_record
            if audit_store:
                try:
                    await audit_store.record_run_completed(
                        run_id=run_id,
                        status="interrupted",
                        duration_ms=0.0,
                        error=cancel_msg,
                        termination_reason="shutdown_cancelled",
                        completed_at=completed_iso,
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            completed_iso = datetime.now(timezone.utc).isoformat()
            err_msg = f"Unexpected execution failure: {str(exc)}"
            failed_record = {
                "run_id": run_id,
                "status": "failed",
                "mode": request.mode,
                "task": request.task,
                "created_at": now_iso,
                "completed_at": completed_iso,
                "duration_ms": 0.0,
                "outcome": None,
                "error": err_msg,
                "termination_reason": "unhandled_exception",
                "execution_type": effective_exec_type,
            }
            workflow_manager._runs_store[run_id] = failed_record
            event_broadcaster.publish_event(
                run_id=run_id,
                event_type="run_failed",
                mode=request.mode,
                tool=None,
                data={"reason": err_msg},
            )
            if audit_store:
                try:
                    await audit_store.record_run_completed(
                        run_id=run_id,
                        status="failed",
                        duration_ms=0.0,
                        error=err_msg,
                        termination_reason="unhandled_exception",
                        completed_at=completed_iso,
                    )
                except Exception:
                    pass
        finally:
            await gate.release_run(run_id)

    # 5. Spawn Task in AppTaskManager
    task_manager: AppTaskManager = getattr(req.app.state, "task_manager", None) or app_task_manager
    run_task = task_manager.spawn_run_task(run_id, _execute_workflow_task())

    if request.wait:
        await run_task
        final_record = workflow_manager.get_run(run_id) or initial_record
        return RunSummaryResponse(**final_record)
    else:
        return RunSummaryResponse(**initial_record)


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
async def get_run_events_stream(
    run_id: str,
    request: Request,
    last_event_id: Optional[str] = None,
    cursor: Optional[int] = None
):
    """Server-Sent Events (SSE) stream for real-time and historical run events.
    - Derives event IDs from run-local sequence (id: <seq>).
    - Supports Last-Event-ID header and cursor query parameters; rejects conflicting/negative values with 400.
    - Replays events after the cursor from durable SQLite audit store / memory, transitioning to live queue without gaps.
    - Yields ': ping\\n\\n' keepalive comments during idle periods.
    - Returns 404 before opening stream if run_id does not exist.
    - Bounded queues: detects slow consumer overflow and disconnects cleanly with Last-Event-ID recovery.
    - Client disconnect does not cancel the background run or duplicate writes.
    """
    audit_store = getattr(request.app.state, "audit_store", None)

    # 1. 404 Check: verify run existence BEFORE opening the event stream
    run_info = workflow_manager.get_run(run_id)
    if not run_info and audit_store:
        persisted_run = await audit_store.get_run(run_id)
        if persisted_run:
            run_info = {"status": persisted_run.status}

    if not run_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found"
        )

    # 2. Cursor extraction and validation
    header_raw = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    header_val: Optional[int] = None
    if header_raw is not None and header_raw != "":
        try:
            header_val = int(header_raw)
            if header_val < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Last-Event-ID header; must be non-negative integer"
            )

    query_val: Optional[int] = None
    if cursor is not None:
        if cursor < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cursor query parameter; must be non-negative integer"
            )
        query_val = cursor
    elif last_event_id is not None and last_event_id != "":
        try:
            q_int = int(last_event_id)
            if q_int < 0:
                raise ValueError()
            query_val = q_int
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid last_event_id query parameter; must be non-negative integer"
            )

    if header_val is not None and query_val is not None and header_val != query_val:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conflicting Last-Event-ID header ({header_val}) and query cursor ({query_val})"
        )

    effective_cursor = header_val if header_val is not None else (query_val if query_val is not None else 0)

    # 3. Stream Generator
    async def event_generator():
        # Register bounded subscriber BEFORE fetching historical events to guarantee zero gaps
        subscriber: EventSubscriber = event_broadcaster.register_subscriber(run_id)
        last_sent_seq = effective_cursor

        try:
            # Fetch authoritative historical events
            historical_events: List[RunEvent] = []
            if audit_store:
                try:
                    db_events = await audit_store.get_run_events(run_id)
                    historical_events = [
                        RunEvent(
                            run_id=pe.run_id,
                            sequence=pe.sequence,
                            timestamp=pe.timestamp,
                            mode=pe.mode,
                            type=pe.event_type,
                            tool=pe.tool,
                            data=pe.data,
                        )
                        for pe in db_events
                    ]
                except Exception:
                    historical_events = event_broadcaster.get_events(run_id)
            else:
                historical_events = event_broadcaster.get_events(run_id)

            # Replay historical events after the cursor
            for ev in sorted(historical_events, key=lambda x: x.sequence):
                if ev.sequence > effective_cursor:
                    yield f"id: {ev.sequence}\ndata: {ev.model_dump_json()}\n\n"
                    last_sent_seq = max(last_sent_seq, ev.sequence)

            # Check if run already completed
            current_status = None
            mem_run = workflow_manager.get_run(run_id)
            if mem_run:
                current_status = mem_run.get("status")
            elif audit_store:
                prun = await audit_store.get_run(run_id)
                if prun:
                    current_status = prun.status

            if current_status in ("completed", "failed", "interrupted"):
                # Drain any remaining events in subscriber queue
                while not subscriber.queue.empty():
                    ev = subscriber.queue.get_nowait()
                    if ev.sequence > last_sent_seq:
                        yield f"id: {ev.sequence}\ndata: {ev.model_dump_json()}\n\n"
                        last_sent_seq = max(last_sent_seq, ev.sequence)
                return

            # Live event loop
            while True:
                if await request.is_disconnected():
                    break

                if subscriber.overflowed:
                    # Slow consumer queue overflow: disconnect with documented recovery guidance
                    overflow_payload = json.dumps({
                        "error": "slow_consumer_overflow",
                        "message": "Subscriber queue overflowed; reconnect with Last-Event-ID",
                        "last_event_id": last_sent_seq
                    })
                    yield f"id: {last_sent_seq}\nevent: error\ndata: {overflow_payload}\n\n"
                    break

                try:
                    event: RunEvent = await asyncio.wait_for(subscriber.queue.get(), timeout=2.0)
                    if subscriber.overflowed:
                        overflow_payload = json.dumps({
                            "error": "slow_consumer_overflow",
                            "message": "Subscriber queue overflowed; reconnect with Last-Event-ID",
                            "last_event_id": last_sent_seq
                        })
                        yield f"id: {last_sent_seq}\nevent: error\ndata: {overflow_payload}\n\n"
                        break

                    # Deduplicate between historical replay and live queue
                    if event.sequence > last_sent_seq:
                        yield f"id: {event.sequence}\ndata: {event.model_dump_json()}\n\n"
                        last_sent_seq = event.sequence
                        if event.type in ("run_completed", "run_failed", "run_interrupted"):
                            break
                except asyncio.TimeoutError:
                    # Keepalive idle heartbeat comment (not parsed as an event)
                    yield ": ping\n\n"

        finally:
            event_broadcaster.unregister_subscriber(run_id, subscriber)

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
    """Returns all registered active tool entries, schemas, methods and fingerprints.
    Displays only active runtime definitions, never archived candidate entries.
    """
    registry = getattr(req.app.state, "registry", None) or workflow_manager.get_registry()
    return RegistryResponse(
        tools=registry.list_active(),
        version="v1",
        updated_at=datetime.now(timezone.utc).isoformat()
    )


@router.get("/patches")
async def get_patches(req: Request, limit: int = 50, offset: int = 0):
    """Returns active and staged patches, alongside sanitized persistent repair history.
    Uses bounded pagination.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
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
async def demo_reset(req: Request):
    """Resets both orchestrator runtime in-memory state and downstream mock API fixtures.
    - Requires idle admission state; rejects if a run or mutation is active (409).
    - Forwards reset exclusively via MockAdminApiClient.
    - Runtime registry and reset changes execute ONLY after downstream reset succeeds.
    - On downstream failure, marks demo state unknown and returns 502, blocking new runs.
    - Preserves persistent SQLite audit history and metrics.
    """
    gate: AppAdmissionGate = getattr(req.app.state, "admission_gate", None) or admission_gate
    await gate.acquire_reset()

    admin_client: MockAdminApiClient = getattr(req.app.state, "admin_client", None)
    if not admin_client:
        admin_client = MockAdminApiClient(base_url=settings.mock_api_url)

    mock_status = "unavailable"
    mock_details = None
    reset_failed = False
    fail_reason = ""

    try:
        mock_details = await admin_client.reset()
        mock_status = "reset_successful"
    except Exception as e:
        reset_failed = True
        fail_reason = str(e)
        mock_status = f"failed_to_reach_mock: {fail_reason}"

    if reset_failed:
        await gate.mark_reset_failed(fail_reason)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Downstream mock API reset failed; demo state marked unknown. Reason: {fail_reason}"
        )

    # Downstream reset succeeded: reset orchestrator runtime state
    workflow_manager.reset()
    req.app.state.registry = VersionedToolRegistry.create_default()

    await gate.mark_reset_success()

    return {
        "status": "reset_completed",
        "orchestrator": "state_cleared",
        "mock_api": mock_status,
        "mock_api_details": mock_details,
        "audit_store": "preserved"
    }


@router.post("/demo/mutation")
async def demo_mutation(mutation_req: AdminMutationRequest, req: Request):
    """Configures drift mutation scenario on Mock API (forwarded via MockAdminApiClient).
    - Enforces scenario allowlist and evidence level validation.
    - Coordinates admission atomically through AppAdmissionGate.
    - Sanitizes errors and keeps local orchestrator state consistent.
    """
    if mutation_req.scenario not in ALLOWED_SCENARIOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported scenario '{mutation_req.scenario}'. Allowed: {sorted(list(ALLOWED_SCENARIOS))}"
        )
    if mutation_req.evidence_level not in ALLOWED_EVIDENCE_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported evidence_level '{mutation_req.evidence_level}'. Allowed: {sorted(list(ALLOWED_EVIDENCE_LEVELS))}"
        )

    gate: AppAdmissionGate = getattr(req.app.state, "admission_gate", None) or admission_gate
    await gate.acquire_mutation()

    admin_client: MockAdminApiClient = getattr(req.app.state, "admin_client", None)
    if not admin_client:
        admin_client = MockAdminApiClient(base_url=settings.mock_api_url)

    try:
        res = await admin_client.set_mutation(
            scenario=mutation_req.scenario,
            enabled=mutation_req.enabled,
            evidence_level=mutation_req.evidence_level,
        )
        return res
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Downstream mutation configuration failed: {str(e)}"
        )
    finally:
        await gate.release_mutation()


@router.get("/metrics", response_model=SystemMetrics)
async def get_metrics(
    req: Request,
    limit: int = 50,
    offset: int = 0,
    mode: Optional[str] = None
):
    """Returns real measured system metrics without fabricated numbers, including per-run metrics.
    Uses bounded pagination.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    audit_store = getattr(req.app.state, "audit_store", None)
    return await metrics_tracker.get_metrics(store=audit_store, limit=limit, offset=offset, mode=mode)
