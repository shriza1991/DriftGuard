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

---

## 3. Completed Work: Task 1 — Interfaces & Foundation

- [x] **Typed Internal Models (`orchestrator/models/`)**:
  - `ToolDefinition`: Full representation of tool contracts with argument schemas, endpoints, methods, and SHA-256 fingerprinting.
  - `ExecutionResult`: Outcome, HTTP status, normalized results/errors, timing info, and operation identity.
  - `SanitizedFailure`: Normalized failures preserving structured validation evidence (`code`, `message`, `expected_schema`, `migration_hint`), with recursive secret redaction.
  - `RepairProposal`: Structure for tool name, base version, restricted operations (`rename_argument`, `cast_type`, `nest_argument`, `set_header`), and evidence references.
  - `PatchDecision`: Static authorization status (`accepted`, `rejected`, `pending_validation`), reasons, and safety violations.
  - `RunStatus` & `RunEvent`: Typed event model strictly conforming to `contracts/events.schema.json`.
  - All models re-exported via `orchestrator/registry/models.py` and `orchestrator/execution/events.py` for backward compatibility.

- [x] **Injectable Async Mock API Client (`orchestrator/client/mock_client.py`)**:
  - `MockBusinessApiClient`:
    - Pure business interface (`find_user`, `assign_ticket`, `get_ticket`, `execute_tool`).
    - Exposes **NO** admin endpoints (`reset`, `mutation`, `state` strictly omitted).
    - Dependency injection for `httpx.AsyncBaseTransport`, `httpx.AsyncClient`, and `CredentialResolver`.
    - Finite timeouts (`httpx.Timeout(10.0)`).
    - Normalized failures into `SanitizedFailure` preserving structured validation evidence.
    - Recursive secret redaction from logs, failure dictionaries, and `__repr__`.
    - Explicit async lifecycle and cleanup (`aclose()`, `__aenter__`, `__aexit__`).
    - Zero automatic retries or repair logic inside the client.
  - `MockAdminApiClient`: Completely segregated client for admin/reset operations.

- [x] **Orchestrator Lifespan & Foundation (`orchestrator/app.py`)**:
  - FastAPI lifespan context manager managing client lifecycle.
  - Non-blocking startup without requiring a live mock API or model provider.
  - Health endpoint (`/health`) reporting service status and client initialization.

- [x] **Valid Event Examples & Fixtures (`contracts/examples/`, `orchestrator/fixtures/`)**:
  - `run_events_normal.json`: Normal successful workflow.
  - `run_events_tool_failure.json`: Downstream schema mismatch failure.
  - `run_events_patch_rejection.json`: Invariant violation (refusal to invent parameters).
  - `run_events_terminal_status.json`: Terminal success and failure summaries.
  - Static fixtures loader in `orchestrator/fixtures/event_examples.py`.

- [x] **Verification Checks (`tests/healing/test_foundation.py`)**:
  - 18/18 passing tests using `httpx.MockTransport` and local fixtures.
  - Zero live network or LLM dependency.

---

## 4. Next Tasks (Task 2 & Beyond)

- [ ] **Task 2: Failure Classification & Diagnostic Engine (`orchestrator/healing/`)**:
  - Distinguish transient failures (5xx, 429) from contract drift (400, 401, 422).
  - Extract failure signals from structured HTTP response bodies and status codes.
  - Apply deterministic heuristic rules first (e.g. rename matching, type coercion).
  - Fallback to LLM-assisted repair planning with strict prompt constraints.

- [ ] **Task 3: Safety Validation & Invariants**:
  - Verify proposed patch does not invent new business arguments (e.g. `priority_tier`).
  - Verify no lossy conversions or unapproved domain redirects.
  - Keep caller arguments immutable.

- [ ] **Task 4: Staged Patch & Verified Execution**:
  - Stage the proposed patch in memory with per-tool lock.
  - Execute exactly ONE retry call with idempotency key.
  - If retry succeeds, perform independent read verification via `GET /tickets/81`.
  - Discard candidate patch if retry or read verification fails.

- [ ] **Task 5: Registry Hot-Commit & Second-Call Reuse**:
  - Hot-commit verified patch into active tool registry.
  - Verify that subsequent calls to `assign_ticket` in future runs automatically reuse the updated contract without reparsing or re-diagnosing.

- [ ] **Task 6: Audit & Token Tracking**:
  - Log every diagnosis step and patch commit into `orchestrator/audit/logger.py`.
  - Track real model tokens used during repair planning.

---

## 5. Acceptance Criteria
1. Baseline mode fails deterministically when drift mutation is enabled.
2. Healing mode autonomously recovers from rename, type_change, and nesting within 1 retry.
3. Second run reuses hot-committed patch immediately with zero diagnostic overhead.
4. Negative drift scenarios trigger safe refusal without inventing values.
