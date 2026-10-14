# DriftGuard - Agent Collaboration Guidelines & Team Charter (AGENTS.md)

## 1. Project Mission & Hackathon Scope
**DriftGuard** is an autonomous self-healing tool registry prototype for AI agents, built for the **Genesis Hackathon PS01 selection**. 

When downstream third-party APIs drift (e.g., parameter renaming, nesting changes, header requirements, or schema updates), agents break. DriftGuard diagnoses API schema mismatches, formulates a deterministic safe patch, verifies recovery against the downstream endpoint, and hot-commits the updated tool contract to the registry without requiring developer redeployments.

### Target Workflow
> "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
Tools involved:
1. `find_user`: Search for Maya (`GET /users?name=Maya`) -> Returns user ID `"123"`.
2. `assign_ticket`: Assign ticket 81 to Maya (`POST /tickets/assign`, v1 body: `{"ticket_id": 81, "user_id": "123"}`).
3. `get_ticket`: Independent verification read (`GET /tickets/81`) -> Asserts `assignee_id == "123"`.

---

## 2. Team Ownership & Code Boundaries

| Member | Domain & Ownership | Directory Tree | Suggested Branch |
|---|---|---|---|
| **Member 1** | Mock API, Runtime Mutations, Evaluation & Benchmarking | `mock_api/`<br>`evaluation/`<br>`tests/mock_api/`<br>`tests/evaluation/` | `feat/mock-api-evaluation` |
| **Member 2** | Orchestrator, Agent Registry, Tool Execution, Healing Pipeline, Audit, Shared Config | `orchestrator/`<br>`tests/healing/`<br>`contracts/` (shared) | `feat/agent-healing` |
| **Member 3** | Frontend Dashboard, SSE Events Viewer, Registry/Audit UI, Comparison Views | `frontend/` | `feat/demo-dashboard` |
| **Member 4** | Submission Deck, Demo Pitch, Screenshot Curation, Evidence Verification | `submission/`<br>`docs/` | `docs/submission-deck` |

---

## 3. Strict Safety & Architectural Invariants

1. **No Out-of-Band Knowledge in Diagnosis**:
   - The diagnosis engine and repair planner must **never** inspect `/admin/state`, `/admin/mutation`, or hidden mock source code.
   - All repair proposals must be derived strictly from observed runtime evidence (HTTP status codes, response bodies, validation errors, and caller intent).
2. **Model Proposes, Deterministic Validation Authorizes**:
   - The LLM or repair planner may suggest an argument translation or contract patch.
   - A deterministic validator must statically verify the patch against safety constraints before any live execution.
3. **Immutability of Business Intent**:
   - Original business parameters provided by the agent (e.g., `ticket_id: 81`, `user_id: "123"`) must remain immutable.
   - The healer adapts format, keys, nesting, or headers; it must never invent artificial IDs, falsify credentials, or perform lossy conversions.
4. **Idempotency & Safe Retries**:
   - Writes (`POST /tickets/assign`) must supply an `Idempotency-Key` header.
   - Never repeat an uncertain write without verifying current state via `GET /tickets/{ticket_id}`.
5. **No Hallucinated Benchmarks or Fabricated Recovery**:
   - Never report successful healing, benchmark results, or token counts from placeholders.
   - Unfinished features must return explicit typed stubs with `status: "pending_implementation"`.

---

## 4. Contract Change Protocol
- Shared contracts live in `contracts/api-contracts.md` and `contracts/events.schema.json`.
- Any breaking change to request/response schemas or SSE event structures requires approval and coordination across Members 1, 2, and 3.

---

## 5. Branch & Versioning Policy
- Do not commit directly to `main` without review.
- Keep commits focused on member-assigned scopes.
- Do not push or deploy without explicit manual verification.
