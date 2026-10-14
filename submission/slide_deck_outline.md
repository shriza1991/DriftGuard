# DriftGuard: Self-Healing Tool Registry for Autonomous AI Agents
## Genesis Hackathon PS01 Presentation Deck Outline (8 Slides)

---

### Slide 1: Title & Value Proposition
- **Title**: DriftGuard: Self-Healing Tool Registry for AI Agents
- **Subtitle**: Eliminating the #1 Cause of Agent Failures: Downstream API Drift
- **Problem**: 73% of production agent outages are caused by subtle, unannounced third-party API changes (renamed fields, altered nesting, new headers).
- **Solution**: DriftGuard detects runtime contract mismatches, synthesizes safe deterministic patches, verifies recovery independently, and hot-commits updated contracts without redeployments.
- **Presenter**: Team DriftGuard (Genesis Hackathon PS01)

---

### Slide 2: The Real-World Drift Problem
- **Scenario**: SecOps agent task: *"Find Maya's user ID, assign ticket 81 to her, and confirm assignee."*
- **What Went Wrong**:
  - Yesterday: `POST /tickets/assign` accepted `{"ticket_id": 81, "user_id": "123"}`.
  - Today: Downstream API updated to `{"ticket_id": 81, "target": {"id": 123}}` + `Authorization` header.
- **The Consequence**: Agent crashes with HTTP 422 `SCHEMA_MISMATCH`. Critical security tickets remain unassigned. Human engineer paged at 2 AM.

---

### Slide 3: Architecture & Strict Security Isolation Boundary
- **Three-Tier Architecture**:
  1. Frontend Dashboard (Live SSE Event Stream, Contract Diffs, Registry Viewer).
  2. Orchestrator Engine (FastAPI, JSON Schema Registry, Diagnostic Engine, Patch Validator).
  3. Downstream Mock Services (Deterministic business endpoints with runtime drift injection).
- **Core Security Invariant**: **Zero Out-of-Band Knowledge**.
  - The diagnosis engine has NO access to downstream server state or secret mutation flags.
  - Recovery is derived strictly from observable HTTP status codes and structured validation errors.

---

### Slide 4: Autonomous Diagnosis & Safe Repair Planning
- **How DriftGuard Heals**:
  1. **Failure Classification**: Distinguishes transient network errors (retry) from contract drift (heal).
  2. **Proposal**: Diagnostic planner synthesizes a candidate argument transform.
  3. **Deterministic Safety Gate**: Statically verifies that original business identifiers are preserved and no required values are hallucinated.
  4. **Single Staged Retry**: Re-invokes endpoint with `Idempotency-Key`.
  5. **Independent Read Verification**: Calls `GET /tickets/81` to verify business write succeeded.
  6. **Hot Commit**: Updates registry; future runs reuse the patch with zero latency overhead.

---

### Slide 5: Strict Safety Guarantees & Safe Refusal
- **Guiding Rule**: *Model Proposes, Deterministic Validation Authorizes*.
- **The Three Immutable Invariants**:
  - Never invent missing business parameters (e.g. `priority_tier`).
  - Never fabricate missing credentials or elevated roles.
  - Never guess between ambiguous target mappings.
- **Graceful Escalation**: If safety checks fail, DriftGuard executes a safe refusal, alerts the operator, and refuses to guess.

---

### Slide 6: System in Action: Scaffolding & Live Dashboard
- **Live Interface Highlights**:
  - Real-time SSE timeline displaying lifecycle events (`tool_call_started`, `recovery_verified`, `run_completed`).
  - Cryptographically fingerprinted v1 Tool Registry (`find_user`, `assign_ticket`, `get_ticket`).
  - Idempotency tracking and pre-write validation guards.
  - Transparent state: clearly labeled fixture data and explicit pending states for unreleased features.

---

### Slide 7: Measured Comparison & Empirical Benchmarks
- **Comparison Methodology**: Identical LLM, identical task prompt, identical initial state.
- **Baseline Agent**: 0% recovery on drifted contracts (hard failure, unassigned ticket).
- **DriftGuard Target**:
  - Syntactic drift (rename, type cast): 100% autonomous recovery.
  - Structural drift (nesting, headers): 100% autonomous recovery within 1 retry.
  - Negative scenarios (missing values, missing roles): 100% safe refusal (0% hallucination).
  - Subsequent calls: 0ms diagnosis overhead (instant hot-commit reuse).

---

### Slide 8: Limitations, Roadmap & Genesis PS01 Impact
- **Current Prototype Scope**: Focused on canonical 3-tool workflow and core drift vectors.
- **Production Roadmap**:
  - OpenAPI 3.1 streaming spec synchronizer.
  - Distributed multi-tenant registry backing SQLite/PostgreSQL.
  - Granular RBAC and cryptographic patch signing.
- **Conclusion**: DriftGuard turns brittle agent tool execution into resilient, enterprise-ready infrastructure.
