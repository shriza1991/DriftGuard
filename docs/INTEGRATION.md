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

## 4. Normal Tool Execution Architecture (Member 2 Task 3)

### 4.1 ToolExecutor API & Lifecycle
The `ToolExecutor` provides deterministic, registry-driven tool execution without hardcoded payload assembly:

```python
from orchestrator.execution.tool_executor import ToolExecutor
from orchestrator.models import ExecutionContext

executor = ToolExecutor(
    registry=app.state.registry,
    client=app.state.mock_client
)

context = ExecutionContext(
    run_id="run-12345",
    tool_call_id="call-find-abc",
    idempotency_key="dg-idemp-uuid-42"
)

result = await executor.execute(
    tool_name_or_def="assign_ticket",
    arguments={"ticket_id": 81, "user_id": "123"},
    context=context,
    candidate_snapshot=None  # Optional staged candidate definition
)
```

### 4.2 Executor Inputs and Results
- **Inputs**:
  - `tool_name_or_def`: Name string or pre-supplied `ToolDefinition` snapshot.
  - `arguments`: Dict of input arguments (deep-copied; caller inputs are strictly immutable).
  - `context`: `ExecutionContext` carrying `run_id`, `tool_call_id`, `idempotency_key`, and metadata.
  - `candidate_snapshot`: Optional candidate definition to test-execute in isolation without activating in the registry.
- **Output (`ExecutionResult`)**:
  - `is_success`: bool indicating overall execution success.
  - `outcome`: Dict of extracted response values according to declared `response_mapping`.
  - `http_status`: HTTP status code (or `0` for pre-network validation/credential errors).
  - `failure`: `SanitizedFailure` if execution failed, with `failure_category` and secret scrubbing.
  - `registry_version`: Monotonic version of tool used.
  - `fingerprint`: Deterministic SHA-256 canonical contract fingerprint.
  - `timing`: `TimingInfo` with wall-clock UTC start/complete and duration in milliseconds.
  - `operation_identity`: `OperationIdentity` tying tool name, endpoint, method, call ID, run ID, and idempotency key.

### 4.3 Supported Mapping Syntax
- **No Executable Code**: Mappings are purely declarative; dynamic `eval`, `exec`, or code execution is strictly prohibited.
- **Top-Level Keys Supported**: `path_params`, `query_params`, `headers`, `body`. Any other top-level key is rejected with `UnsupportedMappingError`.
- **Type Preservation**:
  - Single placeholder `{field}` preserves the native argument type (e.g. integer `81` remains integer `81`, not converted to `"81"`).
  - Composite string `{prefix}_{field}` performs standard string interpolation.
  - Nested dictionary and list trees are traversed recursively with full type preservation.
- **Response Extraction**:
  - Dot and bracket JSONPath notation without eval: `$[0].id`, `$.ticket_id`, `$.target.id`, `$.history`.
  - Optional dictionary keys resolve safely to `None` if omitted by downstream responses.

### 4.4 Logical Execution Identity & Idempotency Key Semantics
- **Operation Identity**: `OperationIdentity` tracks `tool_name`, `endpoint`, `method`, `tool_call_id`, `run_id`, and `idempotency_key`.
- **Idempotency on Writes**: All mutation writes (`POST`, `PUT`, `PATCH`, `DELETE`) require a stable idempotency key sent via `Idempotency-Key` header.
- **Stable Logical Identity**: Explicit replays of the same logical operation reuse the exact same `idempotency_key`. Distinct operations receive freshly generated UUIDs (`dg-idemp-...`).
- **Separation from Workflow ID**: The workflow `run_id` is never used as an idempotency key to prevent cross-tool key collision.

### 4.5 Failure Categories & Write Uncertainty
- **Failure Categories (`FailureCategory`)**:
  - `VALIDATION_FAILURE`: Local argument validation failed against argument JSON Schema prior to network execution (**zero** HTTP calls).
  - `CREDENTIAL_FAILURE`: Required credential reference missing from resolver prior to network execution (**zero** HTTP calls).
  - `TRANSPORT_FAILURE`: Downstream connection refused, network timeout, or socket drop.
  - `HTTP_REJECTION`: Downstream HTTP response with status code >= 400.
  - `RESPONSE_PARSING_FAILURE`: HTTP 2xx response that cannot be parsed as JSON or violates required response mapping.
- **Write Uncertainty (`uncertain_write = True`)**:
  - When a write (`POST /tickets/assign`) experiences a transport failure or post-write response parsing failure, `uncertain_write` is flagged as `True`.
  - In adherence to ADR-002, the executor **never automatically retries** an uncertain write. Verification reads must be performed before any future recovery retry.

### 4.6 Verification & Ambiguity Semantics
- **User Ambiguity Guard**: The workflow resolves Maya via `find_user`. If zero users or multiple users are returned, the workflow halts immediately with an explicit failure before mutating ticket assignment state.
- **Independent Read Verification**: Step 3 (`get_ticket`) reads ticket 81 and asserts `actual_assignee_id == intended_user_id` and `actual_ticket_id == 81`.
- **Non-Reliance on HTTP 200**: Returning HTTP 200 on `assign_ticket` alone never qualifies as success. If independent read verification shows an assignee mismatch, the workflow fails with an explicit verification mismatch error.

---

## 5. Contract Gaps & Coordination Handoffs

### 5.1 For Member 1 (Mock API & Mutations)
1. **Idempotency Key Header**: Confirmed that `POST /tickets/assign` accepts and checks `Idempotency-Key` header. Any unassigned endpoints should specify whether idempotency headers are supported.
2. **User Search Query**: `GET /users?name=Maya` currently returns a JSON list. Note that returning an empty list `[]` triggers safe `USER_NOT_FOUND` refusal, while returning multiple users triggers `AMBIGUOUS_USER_RESOLUTION` refusal.
3. **Missing Auth Header Status**: Confirm whether future mutation scenarios return `401 Unauthorized` or `422 Unprocessable Content` when `Authorization` is omitted. Both are handled safely by `MockBusinessApiClient`.

### 5.2 For Member 3 (Frontend & SSE Events)
1. **Deterministic Execution Tag**: All normal baseline workflow runs emit `data.execution_type = "deterministic_normal_workflow"` in `run_started`.
2. **Tool Call Metadata**: `tool_call_started` events now include `tool_call_id`, `tool_version`, and canonical `fingerprint`.
3. **Tool Call Failure Metadata**: `tool_call_failed` events include `status_code`, `error_code`, `message`, and `uncertain_write` flag.
4. **Read Verification Output**: `run_completed` and `run_failed` include structured `outcome` showing `target_user_name`, `resolved_user_id`, `verified_assignee_id`, and boolean `verified`.

---

## 6. Server-Sent Events (SSE) Contract & Public Examples (For Member 3)

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

