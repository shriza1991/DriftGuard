# Member 2 Ownership & Task Specification

## 1. Domain & Responsibilities
- **Domain**: Orchestrator, Agent Registry, Tool Execution, Autonomous Healing Pipeline, Audit, Shared Backend Config.
- **Directory Scope**:
  - `orchestrator/{api,agents,registry,execution,healing,audit,metrics,models,client,fixtures}`
  - `tests/healing`
  - Integration owner and backend configuration lead
- **Suggested Branch**: `feat/agent-healing`

---

## 2. Dependencies & Contract Status
- **Inbound**: Consumes Mock API endpoints from Member 1 (`/users`, `/tickets/assign`, `/tickets/{id}`).
- **Outbound**: Provides SSE events stream (`/runs/{id}/events`), run triggers, tool registry (`/registry`), and metrics (`/metrics`) to Member 3 (Frontend).
- **Contract Gaps & Open Questions for Member 1**:
  1. *Validation Error Format Under Nested Errors*: For `nesting` and `combined` scenarios, ensure the `details.expected_schema` reflects the full subtree so the diagnostic engine can extract parameter targets without ambiguity.
  2. *Status Code Consistency for Missing Required Header*: Confirm whether missing `Authorization` returns `401 Unauthorized` or `422 Unprocessable Content`. The business client handles both as drift candidates.
  3. *Credential Reference Resolution*: Confirm whether `credential_reference` in registry entries maps to environment variable names or vault keys. Currently resolved via injected `CredentialResolver`.
  4. *Schema Error Field Correspondence*: Note that `expected_schema` alone in an error response does not establish semantic field correspondence (e.g. that `assignee_id` corresponds to `user_id`); classification and diagnosis heuristics will be responsible for resolving candidate correspondences.

---

## 3. Completed Tasks

### Task 1: Interfaces & Foundation
- [x] **Typed Internal Models (`orchestrator/models/`)**:
  - `ToolDefinition`: Full representation of tool contracts with argument schemas, endpoints, methods, and SHA-256 fingerprinting.
  - `ExecutionResult`: Outcome, HTTP status, normalized results/errors, timing info, and operation identity.
  - `SanitizedFailure`: Normalized failures preserving structured validation evidence (`code`, `message`, `expected_schema`, `migration_hint`), with recursive secret redaction.
  - `RepairProposal`: Structure for tool name, base version, restricted operations (`rename_argument`, `cast_type`, `nest_argument`, `set_header`), and evidence references.
  - `PatchDecision`: Static authorization status (`accepted`, `rejected`, `pending_validation`), reasons, and safety violations.
  - `RunStatus` & `RunEvent`: Typed event model strictly conforming to `contracts/events.schema.json`.
- [x] **Injectable Async Mock API Client (`orchestrator/client/mock_client.py`)**:
  - Pure business interface (`find_user`, `assign_ticket`, `get_ticket`, `execute_tool`).
  - Strict segregation: exposes **NO** admin endpoints (`reset`, `mutation`, `state` strictly omitted).
  - Dependency injection for `httpx.AsyncBaseTransport`, `httpx.AsyncClient`, and `CredentialResolver`.
  - Normalized failures into `SanitizedFailure` with recursive secret redaction.
  - Explicit async lifecycle and cleanup (`aclose()`, `__aenter__`, `__aexit__`).
- [x] **Orchestrator Lifespan & Foundation (`orchestrator/app.py`)**:
  - FastAPI lifespan context manager managing client and registry lifecycle.
  - Non-blocking startup without requiring a live mock API or model provider.
- [x] **Valid Event Examples & Fixtures (`contracts/examples/`, `orchestrator/fixtures/`)**:
  - `run_events_normal.json`, `run_events_tool_failure.json`, `run_events_patch_rejection.json`, `run_events_terminal_status.json`.
  - Static fixtures loader in `orchestrator/fixtures/event_examples.py`.

### Task 2: Versioned Tool Registry (`orchestrator/registry/service.py`)
- [x] **In-Memory Versioned Storage**:
  - Stores canonical v1 tool definitions (`find_user`, `assign_ticket`, `get_ticket`).
  - Active and historical snapshots preserved in chronological order.
- [x] **Immutable Defensive Snapshots**:
  - All lookups (`get`, `list_active`, `get_version`, `get_history`, `get_candidate`) return deep copies.
  - Caller mutations on returned objects or passed candidate definitions cannot alter registry state.
- [x] **Candidate Staging & Validation**:
  - Requires valid existing tool and matching base version.
  - Validates JSON Schema structure and rejects identity (name), endpoint, or method changes.
  - Internally derives target monotonic version (`1.0.0` &rarr; `1.1.0`) and canonical fingerprint.
  - Generates unique candidate ID (`cand-<12-hex>`).
  - Leaves active state and history completely untouched.
- [x] **Candidate Discard**:
  - Marks staged candidate as discarded without altering active definitions or history.
- [x] **Atomic Version-Checked Commit**:
  - Atomically verifies current active tool version against expected base version and candidate's base version.
  - Rejects stale, discarded, missing, or already committed candidates.
  - Archives previous active definition to history and activates candidate definition with target version.
  - Leaves all state unchanged on failure.
- [x] **Per-Tool Re-Entrant Async Locking (`AsyncRLock`)**:
  - Avoids deadlocks when future recovery code holds the tool lock and invokes `commit_candidate`.
  - Different tools use independent locks and run concurrently without contention.
- [x] **Canonical Fingerprinting**:
  - Computes deterministic SHA-256 fingerprint over canonical contract content with `sort_keys=True`.
  - Excludes lifecycle metadata (`version`, `status`, `candidate_id`) and actual secrets.
- [x] **Public `GET /registry` Endpoint**:
  - Exposes sanitized active entries only, leaking no secrets, internal candidate objects, or historical mutable lists.
- [x] **Targeted Test Suite (`tests/healing/test_versioned_registry.py`)**:
  - 11 comprehensive test cases validating entries, defensive copies, stage/discard isolation, monotonic commits, stale proposal rejection, deterministic fingerprints, concurrent commits, re-entrant locking, and endpoint sanitation.

---

## 4. Next Tasks

- [ ] **Task 3: Normal Tool Execution (`orchestrator/execution/`)**:
  - Wire normal workflow execution to resolve tools from `VersionedToolRegistry` and dispatch calls via `MockBusinessApiClient`.
  - Ensure end-to-end execution of `find_user` &rarr; `assign_ticket` &rarr; `get_ticket` passes deterministically.
  - Maintain event sequence emission with timing and identity metadata.

- [ ] **Task 4: Failure Classification & Diagnostic Engine (`orchestrator/healing/`)**:
  - Distinguish transient failures (5xx, 429) from contract drift (400, 401, 422).
  - Extract failure signals from structured HTTP response bodies and status codes.
  - Apply deterministic heuristic rules first (e.g. rename matching, type coercion).
  - Fallback to LLM-assisted repair planning with strict prompt constraints.

- [ ] **Task 5: Safety Validation & Invariants**:
  - Verify proposed patch does not invent new business arguments (e.g. `priority_tier`).
  - Verify no lossy conversions or unapproved domain redirects.
  - Keep caller arguments immutable.

- [ ] **Task 6: Staged Patch & Verified Execution**:
  - Stage the proposed patch in memory with per-tool lock.
  - Execute exactly ONE retry call with idempotency key.
  - If retry succeeds, perform independent read verification via `GET /tickets/81`.
  - Discard candidate patch if retry or read verification fails.

- [ ] **Task 7: Registry Hot-Commit & Second-Call Reuse**:
  - Hot-commit verified patch into active tool registry.
  - Verify that subsequent calls to `assign_ticket` in future runs automatically reuse the updated contract without reparsing or re-diagnosing.

- [ ] **Task 8: Audit & Token Tracking**:
  - Log every diagnosis step and patch commit into `orchestrator/audit/logger.py`.
  - Track real model tokens used during repair planning.

---

## 5. Acceptance Criteria
1. Baseline mode fails deterministically when drift mutation is enabled.
2. Healing mode autonomously recovers from rename, type_change, and nesting within 1 retry.
3. Second run reuses hot-committed patch immediately with zero diagnostic overhead.
4. Negative drift scenarios trigger safe refusal without inventing values.
