from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from orchestrator.api.routes import router as orchestrator_router
from orchestrator.config import settings
from orchestrator.client.mock_client import MockBusinessApiClient, MockAdminApiClient
from orchestrator.registry import VersionedToolRegistry
from orchestrator.audit import SQLiteAuditStore
from orchestrator.execution.events import event_broadcaster
from orchestrator.metrics.tracker import metrics_tracker
from orchestrator.execution.workflow import workflow_manager
from orchestrator.execution.gate import admission_gate
from orchestrator.execution.tasks import app_task_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Orchestrator lifespan manager.
    Initializes shared clients, audit store, admission gate, and tool registry on app state with finite timeouts.
    Does NOT require a live mock API or model provider call to complete startup.
    Ensures safe async cleanup and managed task cancellation on shutdown.
    """
    if not hasattr(app.state, "registry") or app.state.registry is None:
        app.state.registry = VersionedToolRegistry.create_default()
    if not hasattr(app.state, "mock_client") or app.state.mock_client is None:
        app.state.mock_client = MockBusinessApiClient(base_url=settings.mock_api_url, credential_resolver=lambda ref: settings.assignment_token if ref == "secops_auth" else None)
    if not hasattr(app.state, "admin_client") or app.state.admin_client is None:
        app.state.admin_client = MockAdminApiClient(base_url=settings.mock_api_url)
    if not hasattr(app.state, "audit_store") or app.state.audit_store is None:
        app.state.audit_store = SQLiteAuditStore(db_path=settings.sqlite_db_path)
    if not hasattr(app.state, "admission_gate") or app.state.admission_gate is None:
        app.state.admission_gate = admission_gate
    if not hasattr(app.state, "task_manager") or app.state.task_manager is None:
        app.state.task_manager = app_task_manager
    
    await app.state.audit_store.initialize()
    event_broadcaster.set_audit_store(app.state.audit_store)
    metrics_tracker.audit_store = app.state.audit_store
    workflow_manager.set_audit_store(app.state.audit_store)

    yield

    # Managed shutdown sequence: cancel and await background tasks within finite budget
    await app.state.task_manager.cancel_and_wait_all(timeout=5.0)
    await event_broadcaster.flush()

    # Safely close clients
    if hasattr(app.state, "mock_client") and app.state.mock_client:
        await app.state.mock_client.aclose()
    if hasattr(app.state, "admin_client") and app.state.admin_client:
        await app.state.admin_client.aclose()


app = FastAPI(
    title="DriftGuard Orchestrator Service",
    description="Autonomous Tool Registry and Agent Workflow Engine (Member 2 Scope).",
    version="1.0.0",
    lifespan=lifespan
)

# Initialize registry, client, and audit store on app state for direct ASGI test clients
app.state.registry = VersionedToolRegistry.create_default()
app.state.mock_client = MockBusinessApiClient(base_url=settings.mock_api_url, credential_resolver=lambda ref: settings.assignment_token if ref == "secops_auth" else None)
app.state.admin_client = MockAdminApiClient(base_url=settings.mock_api_url)
app.state.audit_store = SQLiteAuditStore(db_path=settings.sqlite_db_path)
app.state.admission_gate = admission_gate
app.state.task_manager = app_task_manager

event_broadcaster.set_audit_store(app.state.audit_store)
metrics_tracker.audit_store = app.state.audit_store
workflow_manager.set_audit_store(app.state.audit_store)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(orchestrator_router)


@app.get("/health")
async def health_check():
    """Health check endpoint.
    Operates without requiring live mock API or model provider connectivity.
    """
    return {
        "status": "healthy",
        "service": "orchestrator",
        "mock_api_url": settings.mock_api_url,
        "model_provider": settings.model_provider,
        "version": "1.0.0",
        "registry_initialized": hasattr(app.state, "registry") and app.state.registry is not None,
        "client_initialized": hasattr(app.state, "mock_client") and app.state.mock_client is not None
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("orchestrator.app:app", host=settings.host, port=settings.port, reload=True)
