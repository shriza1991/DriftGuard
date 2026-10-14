# Member 2 Ownership & Task Specification

## 1. Domain & Responsibilities
- **Domain**: Orchestrator, Agent Registry, Tool Execution, Autonomous Healing Pipeline, Audit, Shared Backend Config.
- **Directory Scope**:
  - `orchestrator/{api,agents,registry,execution,healing,audit,metrics}`
  - `tests/healing`
  - Integration owner and backend configuration lead
- **Suggested Branch**: `feat/agent-healing`

---

## 2. Dependencies
- **Inbound**: Consumes Mock API endpoints from Member 1.
- **Outbound**: Provides SSE events stream, run triggers, tool registry, and metrics to Member 3 (Frontend).

---

## 3. Next Tasks Checklist

- [ ] **Task 1: Failure Classification & Diagnostic Engine (`orchestrator/healing/`)**:
  - Distinguish transient failures (5xx, 429) from contract drift (400, 401, 422).
  - Extract failure signals from structured HTTP response bodies and status codes.
  - Apply deterministic heuristic rules first (e.g. rename matching, type coercion).
  - Fallback to LLM-assisted repair planning with strict prompt constraints.

- [ ] **Task 2: Safety Validation & Invariants**:
  - Verify proposed patch does not invent new business arguments (e.g. `priority_tier`).
  - Verify no lossy conversions or unapproved domain redirects.
  - Keep caller arguments immutable.

- [ ] **Task 3: Staged Patch & Verified Execution**:
  - Stage the proposed patch in memory with per-tool lock.
  - Execute exactly ONE retry call with idempotency key.
  - If retry succeeds, perform independent read verification via `GET /tickets/81`.
  - Discard candidate patch if retry or read verification fails.

- [ ] **Task 4: Registry Hot-Commit & Second-Call Reuse**:
  - Hot-commit verified patch into active tool registry.
  - Verify that subsequent calls to `assign_ticket` in future runs automatically reuse the updated contract without reparsing or re-diagnosing.

- [ ] **Task 5: Audit & Token Tracking**:
  - Log every diagnosis step and patch commit into `orchestrator/audit/logger.py`.
  - Track real model tokens used during repair planning.

---

## 4. Acceptance Criteria
1. Baseline mode fails deterministically when drift mutation is enabled.
2. Healing mode autonomously recovers from rename, type_change, and nesting within 1 retry.
3. Second run reuses hot-committed patch immediately with zero diagnostic overhead.
4. Negative drift scenarios trigger safe refusal without inventing values.
