# DriftGuard Integration Guide

This document specifies the integration contracts and communication channels between all components.

## 1. Port Allocation & Endpoints
| Component | Local Host / Port | Responsibilities |
|---|---|---|
| **Mock API** | `http://127.0.0.1:8001` | Simulated ticketing & user directory service; drift mutation injection |
| **Orchestrator** | `http://127.0.0.1:8000` | Tool registry, execution engine, SSE stream, healing pipeline, metrics |
| **Frontend** | `http://127.0.0.1:5173` | React/Vite dashboard, live timeline, comparison panels |

---

## 2. Downstream Client Architecture (Member 2 Foundation)

### 2.1 Importing & Injecting the Client
The orchestrator provides an injectable async client in `orchestrator.client`:

```python
from orchestrator.client import MockBusinessApiClient, MockApiClientError

# Injected client with custom transport for isolated testing:
client = MockBusinessApiClient(
    base_url="http://127.0.0.1:8001",
    timeout=10.0,
    transport=custom_transport,
    credential_resolver=lambda ref: "vault-token" if ref == "secops_auth" else None
)
```

### 2.2 Client Ownership & Lifecycle Cleanup
Clients can be managed either via context manager or explicit lifespan:

```python
# Context Manager pattern:
async with MockBusinessApiClient(base_url="http://127.0.0.1:8001") as client:
    result = await client.find_user(name="Maya")

# FastAPI Lifespan pattern:
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.registry = VersionedToolRegistry.create_default()
    app.state.mock_client = MockBusinessApiClient(base_url=settings.mock_api_url)
    yield
    await app.state.mock_client.aclose()
```

### 2.3 Strict Security Segregation
- `MockBusinessApiClient`: Exposes **ONLY** business methods (`find_user`, `assign_ticket`, `get_ticket`, `execute_tool`). Admin methods (`reset`, `mutation`, `state`) are strictly absent.
- `MockAdminApiClient`: Dedicated exclusively to admin operations for testing and reset forwarding. Never imported by the healing diagnosis engine.

---

## 3. Versioned Tool Registry Integration (Member 2 Registry Lifecycle)

### 3.1 Registry Initialization & Injection
The `VersionedToolRegistry` is initialized on `app.state.registry` via `create_default()` containing the three canonical tools: `find_user`, `assign_ticket`, and `get_ticket`.

```python
from orchestrator.registry import VersionedToolRegistry

registry = VersionedToolRegistry.create_default()
```

### 3.2 Active & Historical Lookups
All read operations return defensive deep copies to protect internal state:

```python
# Active tool lookup
tool = registry.get("assign_ticket")

# Listing all active tools
tools = registry.list_active()

# Historical lookup
old_version = registry.get_version("assign_ticket", "1.0.0")
history = registry.get_history("assign_ticket")
```

### 3.3 Candidate Staging, Discard & Atomic Commit
```python
# 1. Stage candidate (validates schema, rejects endpoint/method/name identity changes, derives target version)
candidate = await registry.stage_candidate(
    tool_name="assign_ticket",
    candidate_definition=candidate_def,
    base_version="1.0.0"  # Optional; defaults to active version
)
# Returns StagedCandidate(candidate_id="cand-...", target_version="1.1.0", status="staged")

# 2. If recovery fails verification: discard candidate
registry.discard_candidate(candidate.candidate_id)

# 3. If recovery succeeds verification: atomic version-checked commit
# Requires matching expected base version to prevent race conditions
committed_tool = await registry.commit_candidate(
    candidate_id=candidate.candidate_id,
    expected_base_version="1.0.0"
)
```

### 3.4 Per-Tool Lock Ownership Semantics
- Each tool has an independent `AsyncRLock` accessed via `registry.tool_lock(tool_name)`.
- Locking `assign_ticket` does not block lookups or modifications on `find_user` or `get_ticket`.
- Locks are task-reentrant: future recovery workflows can hold the tool lock across diagnosis, retry, and verification, and safely invoke `commit_candidate` without deadlocking.

---

## 4. Communication Protocols

### 4.1 Frontend to Orchestrator
- **Workflow Run**: `POST http://127.0.0.1:8000/runs` with `{"mode": "normal | healing", "task": "..."}`
- **SSE Stream**: `GET http://127.0.0.1:8000/runs/{run_id}/events`
- **Tool Registry**: `GET http://127.0.0.1:8000/registry`
  - Response: `RegistryResponse` exposing sanitized active tools only (defensive snapshots with no secret values, leaked history, or internal staged candidate objects).
- **Metrics**: `GET http://127.0.0.1:8000/metrics`
- **Reset**: `POST http://127.0.0.1:8000/demo/reset`

### 4.2 Orchestrator to Mock API
- **User Search**: `GET http://127.0.0.1:8001/users?name=Maya`
- **Ticket Assignment**: `POST http://127.0.0.1:8001/tickets/assign`
  - Body: `{"ticket_id": 81, "user_id": "123"}`
  - Header: `Idempotency-Key: driftguard-<uuid>`
- **Independent Read Verification**: `GET http://127.0.0.1:8001/tickets/81`

### 4.3 Evaluation Runner to Mock API (Admin Only)
- **Reset Fixtures**: `POST http://127.0.0.1:8001/admin/reset`
- **Inject Mutation**: `POST http://127.0.0.1:8001/admin/mutation`
- **Assert Ground Truth**: `GET http://127.0.0.1:8001/admin/state`
*(Note: Strictly forbidden for Orchestrator diagnosis engine!)*

---

## 5. Server-Sent Events (SSE) Contract & Public Examples (For Member 3)

Each event emitted across `/runs/{run_id}/events` strictly conforms to `contracts/events.schema.json`.
All secrets in `data` are recursively redacted with `[REDACTED]` prior to emission.

### Public Event Examples Catalog:
- **Normal Baseline Run**: [`contracts/examples/run_events_normal.json`](file:///contracts/examples/run_events_normal.json)
  - `run_started` &rarr; `tool_call_started (find_user)` &rarr; `tool_call_started (assign_ticket)` &rarr; `tool_call_started (get_ticket)` &rarr; `run_completed`.
- **Downstream Tool Failure**: [`contracts/examples/run_events_tool_failure.json`](file:///contracts/examples/run_events_tool_failure.json)
  - `run_started` &rarr; `tool_call_started` &rarr; `tool_call_failed` (422 `SCHEMA_MISMATCH`) &rarr; `run_failed`.
- **Patch Rejection (Safety Refusal)**: [`contracts/examples/run_events_patch_rejection.json`](file:///contracts/examples/run_events_patch_rejection.json)
  - `tool_call_failed` &rarr; `diagnosis_started` &rarr; `patch_proposed` &rarr; `patch_rejected` (invariant violation) &rarr; `run_failed`.
- **Terminal Status Summaries**: [`contracts/examples/run_events_terminal_status.json`](file:///contracts/examples/run_events_terminal_status.json)
  - Demonstrates terminal payloads for both completed and failed runs with duration and outcome metrics.
