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

## 5. Model-Driven Baseline Agent Integration (Member 2 Task 4)

### 5.1 Architecture & Invariants
- **Genuine Tool Calling**: Driven by LangChain tool-calling agents (`google_genai` or `openai`).
- **Frozen Catalogue Isolation**: Derives tool specifications and candidate snapshots exclusively from `orchestrator/agents/catalogue.py` (`get_frozen_v1_catalogue()`). Commits or proposals in `VersionedToolRegistry` **never** alter baseline schemas or behavior.
- **Execution Limits**: Bound by configurable limits: `max_turns` (default: 5), `max_tool_calls` (default: 10), and overall deadline `timeout_seconds` (default: 30.0s).
- **Operation Identity**: Writes (`POST /tickets/assign`) compute a stable idempotency key mapped to `tool_name` + canonical sorted JSON arguments, preventing duplicate write side-effects within a task run.
- **Truthful Token Tracking**: Aggregates input, output, and total tokens from model metadata without double counting. If unavailable from the provider, explicitly sets `"unavailable"` (never synthesizes token counts). Baseline diagnosis tokens are strictly `0`.
- **Independent Verification Over Model Claims**: Completion is independently verified by reading downstream state via `get_ticket(81)` and matching `actual_assignee == resolved_user_id`. Model claims of "done" without matching downstream state are marked as `verification_failed`.

### 5.2 Invocation & Run Request Contract (For Member 1 & Evaluation)
Triggering a baseline model run:
```http
POST /runs HTTP/1.1
Content-Type: application/json

{
  "mode": "baseline",
  "execution_type": "model_driven_baseline",
  "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
  "limits": {
    "max_turns": 5,
    "max_tool_calls": 10,
    "timeout_seconds": 30.0
  }
}
```

Response payload (`RunSummaryResponse`):
```json
{
  "run_id": "run-48afbc29-7981-420a",
  "status": "completed",
  "mode": "normal",
  "execution_type": "model_driven_baseline",
  "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
  "created_at": "2026-10-14T12:00:00Z",
  "completed_at": "2026-10-14T12:00:02Z",
  "duration_ms": 1420.5,
  "outcome": {
    "ticket_id": 81,
    "target_user_name": "Maya",
    "resolved_user_id": "123",
    "verified_assignee_id": "123",
    "verified": true,
    "execution_type": "model_driven_baseline",
    "status": "assigned",
    "uncertain_write": false
  },
  "error": null,
  "model_usage": {
    "input_tokens": 320,
    "output_tokens": 75,
    "total_tokens": 395,
    "diagnosis_tokens": 0,
    "model_name": "gemini-2.5-flash",
    "provider": "google_genai"
  },
  "termination_reason": "completed_and_verified"
}
```

---

## 6. Failure Interception & Semantic Classification (Member 2 Task 5)

### 6.1 `FailureObservation` Schema
Constructed defensively at the shared `ToolExecutor` boundary upon any tool failure:
```python
class FailureObservation(BaseModel):
    tool_name: str
    registry_version: str
    fingerprint: str
    run_id: Optional[str]
    tool_call_id: Optional[str]
    idempotency_key: Optional[str]
    operation_identity: OperationIdentity
    execution_phase: ExecutionPhase  # PRE_VALIDATION, CREDENTIAL_RESOLUTION, REQUEST_MAPPING, HTTP_DISPATCH, RESPONSE_PARSING, READ_VERIFICATION
    method: str
    endpoint: str
    http_status: Optional[int]
    original_arguments: Dict[str, Any]       # Immutable caller snapshot
    request_evidence: Optional[Dict[str, Any]]
    response_evidence: Optional[Dict[str, Any]]
    validation_details: Optional[Dict[str, Any]]
    evidence_paths: List[str]
    timing: TimingInfo
    write_attempted: bool
    uncertain_write: bool                    # Preserves uncertain-write protection
    raw_error_message: Optional[str]         # Recursively redacted
    timestamp: str
```

### 6.2 Semantic Categories (`SemanticFailureCategory`)
| Category | Definition | Default Diagnosis Eligibility |
|---|---|---|
| `schema_drift` | Structured downstream schema mismatch evidence (`expected_schema`, `missing`, `unexpected`, `migration_hint`, newly required header). Identifies candidate for repair. | `True` |
| `invalid_input` | Pre-dispatch argument schema rejection by local JSON Schema Draft 7 validator. Represents bad caller input, not downstream drift. | `False` |
| `business_rule_rejection` | Downstream business logic rejection (`TICKET_CLOSED`, `TICKET_NOT_FOUND`, documented 404 for nonexistent resource). | `False` |
| `authentication_authorization` | Standard HTTP 401/403 credential or permission rejection without header drift evidence. | `False` |
| `transient_service_failure` | HTTP 429 rate limit, 500/502/503/504 server error, or network transport failure/socket timeout. | `False` |
| `ambiguous` | Bare HTTP 400/422 status codes without structured details, unrecognized status codes, or contradictory/malformed evidence. | `False` |

### 6.3 Retry Safety & Invariants (`RetrySafety`)
- **Uncertain Write Invariant**: If `uncertain_write == True` (e.g. transport disconnect or response parsing failure on a write), `safe_to_retry` is strictly `False`. This protection holds **even if** `diagnosis_eligible == True`.
- **Pre-Execution Invariant**: If rejected before HTTP dispatch (`PRE_VALIDATION`, `REQUEST_MAPPING`), `safe_to_retry` is `False` with `requires_input_adaptation == True`.
- **Idempotency Keys**: Supplying an `Idempotency-Key` does **not** prove server-side deduplication; writes with uncertain server outcome are never retried blindly.

---

## 7. Deterministic Repair Proposals (Member 2 Task 6)

### 7.1 Proposal Models & Status (`PlanningStatus`)
```python
class PlanningStatus(str, Enum):
    PROPOSED = "proposed"                       # Evidence is complete; coherent plan formulated
    INSUFFICIENT_EVIDENCE = "insufficient_evidence" # Missing correspondence or schema details
    UNSUPPORTED = "unsupported"                 # Unapproved header, invented business value, or collision
    NOT_ELIGIBLE = "not_eligible"               # Ineligible failure category or stale snapshot
```

### 7.2 Path Semantics & Distinct Layers
DriftGuard strictly separates three distinct layers of paths:
1. **Source Argument Path**: The caller's original business parameter (`user_id`, `ticket_id`). Original caller arguments remain **strictly immutable**.
2. **Wire Request Path**: The HTTP request wire structure (`body["assignee_id"]`, `body["target"]["id"]`, `headers["Authorization"]`). Updated via `request_mapping`.
3. **Argument Schema Path**: The local input validation contract in `argument_schema.properties`.

### 7.3 Supported Evidence Grammar
The planner normalizes only documented, machine-readable evidence:
- **Explicit Field Migration**:
  - `details.migration_mapping = {"user_id": "assignee_id"}`
  - Structured machine-readable hint: `"Field 'user_id' is deprecated. Expected 'assignee_id'."` or `"user_id is now nested under target.id"`.
- **Expected Schema Types**:
  - `details.expected_schema = {"ticket_id": "integer", "target": {"id": "integer"}}`
  - Declared conversion policy: `"string_to_integer"`
- **Required Headers**:
  - `details.required_headers = ["Authorization"]` or `["X-Org-Token"]`
  - Must map to a pre-approved credential reference name (`approved_header_credentials = {"Authorization": "secops_auth"}`).

### 7.4 Task 7 Validation Requirements
Proposals generated in Task 6 are declarative candidates labeled `status: "proposed_unvalidated"`. Task 7 must statically verify:
1. `verify_caller_argument_immutability`: Caller arguments (`ticket_id: 81`, `user_id: "123"`) must not be mutated or dropped.
2. `verify_no_invented_business_values`: The patch must never invent artificial IDs, priorities, or flags.
3. `verify_lossless_type_conversion`: Type conversions must be safe and lossless (e.g. numeric string `"123"` to `123`).
4. `verify_approved_credential_resolution`: Headers must map to approved credential references without fabricating secrets.
5. `verify_idempotent_retry_safety`: Uncertain writes (`uncertain_write == True`) must never be retried.

---

## 8. Contract Gaps & Coordination Handoffs

### 8.1 For Member 1 (Mock API & Mutations) — Proposal Evidence & Refusal Examples
The deterministic planner enforces strict evidence requirements:
1. **Missing Field Alone Refusal (`insufficient_evidence`)**:
   - If mock returns `{"error": {"details": {"missing": ["assignee_id"]}}}`, DriftGuard **refuses** to guess that `assignee_id` replaces `user_id`.
   - **Requirement**: Provide explicit correspondence: `details.migration_mapping = {"user_id": "assignee_id"}` or `migration_hint = "Use assignee_id instead of user_id"`.
2. **Unapproved Required Header Refusal (`unsupported`)**:
   - If mock returns `401` with `required_headers = ["X-Custom-Secret"]` and that header is not in approved orchestrator configuration, DriftGuard **refuses**. Credentials cannot be fabricated.
3. **Newly Required Business Parameter Refusal (`unsupported`)**:
   - If mock returns `missing: ["priority_tier"]`, DriftGuard **refuses**. The agent cannot invent ungrounded business values.

### 8.2 For Member 3 (Frontend & SSE Events) — Representative Event Payloads

#### Representative `tool_call_failed` Event Payload with Classification:
```json
{
  "run_id": "run-48afbc29-7981-420a",
  "sequence": 4,
  "timestamp": "2026-10-14T12:00:01.250Z",
  "mode": "normal",
  "type": "tool_call_failed",
  "tool": "assign_ticket",
  "data": {
    "status_code": 422,
    "error_code": "schema_mismatch",
    "message": "Field 'user_id' is deprecated. Expected 'assignee_id'.",
    "uncertain_write": false,
    "classification": {
      "category": "schema_drift",
      "diagnosis_eligible": true,
      "safe_to_retry": false,
      "retry_reason": "Downstream server rejected the request schema. Repeating identical payload will deterministically fail; input adaptation is required."
    }
  }
}
```

#### Representative `patch_proposed` Event Payload (Unvalidated Candidate):
```json
{
  "run_id": "run-48afbc29-7981-420a",
  "sequence": 5,
  "timestamp": "2026-10-14T12:00:01.350Z",
  "mode": "healing",
  "type": "patch_proposed",
  "tool": "assign_ticket",
  "data": {
    "proposal_id": "prop-b4f2c8d19a02",
    "tool": "assign_ticket",
    "base_version": "1.0.0",
    "target_version": "1.1.0",
    "proposal_fingerprint": "b4f2c8d19a02ec71",
    "status": "unvalidated_proposal",
    "validation_required": true,
    "operations": [
      {
        "operation_type": "set_header",
        "source_path": null,
        "target_path": "Authorization",
        "conversion_policy": null,
        "credential_reference": "secops_auth"
      },
      {
        "operation_type": "nest_argument",
        "source_path": "user_id",
        "target_path": "target.id",
        "conversion_policy": null,
        "credential_reference": null
      },
      {
        "operation_type": "cast_type",
        "source_path": "user_id",
        "target_path": "target.id",
        "conversion_policy": "string_to_integer",
        "credential_reference": null
      }
    ]
  }
}
```

---

## 9. Server-Sent Events (SSE) Contract & Public Examples (For Member 3)

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


