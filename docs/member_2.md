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

### Task 3: Normal Tool Execution (`orchestrator/execution/`)
- [x] **Shared Registry & Client Injection**:
  - Injected `VersionedToolRegistry` and `MockBusinessApiClient` into workflows and FastAPI endpoints (`/runs`, `/registry`) without per-run resets.
  - Committed registry definitions persist across successive runs.
- [x] **Defensive Snapshot & Staged Candidate Support**:
  - Single defensive snapshot captured per invocation, ensuring consistent schema, mappings, version, and SHA-256 fingerprint throughout execution.
  - Supports explicit candidate snapshot execution without activating or altering registry state.
- [x] **Safe Registry-Driven Executor (`orchestrator/execution/tool_executor.py`)**:
  - Deep-copies input arguments for caller immutability.
  - Pre-HTTP JSON Schema Draft 7 argument validation guaranteeing **zero** network requests on schema rejection.
  - Resolves credentials via `CredentialResolver` prior to network dispatch (**zero** network requests on missing credentials).
  - Recursively scrubs secrets (`auth`, `token`, `secret`, `key`, `password`, `credential`) from all outcomes, exceptions, and event payloads.
- [x] **Pure Deterministic Mapping Engine (`orchestrator/execution/mapping.py`)**:
  - Evaluates `path_params`, `query_params`, `headers`, and `body` without `eval` or dynamic code execution.
  - Strict type preservation for arguments (e.g. integer `81` remains integer `81` without string coercion).
  - Explicit rejection of unsupported mapping keys.
- [x] **Logical Execution Identity & Idempotency**:
  - Carries `run_id`, `tool_call_id`, and stable `idempotency_key` on `OperationIdentity` and `ExecutionContext`.
  - Replays of the same logical operation preserve `Idempotency-Key`; distinct operations receive distinct UUIDs. Never conflates `run_id` with write idempotency.
- [x] **Normalized Outcome & Categorized Failure Semantics**:
  - Categorizes failures cleanly: `VALIDATION_FAILURE`, `CREDENTIAL_FAILURE`, `TRANSPORT_FAILURE`, `HTTP_REJECTION`, `RESPONSE_PARSING_FAILURE`.
  - Tokenized JSONPath response extraction (`$[0].id`, `$.ticket_id`).
  - Flags `uncertain_write = True` on transport or response parsing failures following a mutation write, preventing dangerous automatic retries.
  - Preserves structured HTTP evidence without prematurely classifying as drift (classification deferred to Task 5).
- [x] **Deterministic 3-Tool Workflow Verification (`orchestrator/execution/workflow.py`)**:
  - Sequence: `find_user` &rarr; `assign_ticket` &rarr; `get_ticket`.
  - Ambiguity guards: halts immediately on 0 or >1 user directory matches before mutating ticket state.
  - Independent read verification: compares actual `assignee_id` against intended user ID. Never declares success on HTTP 200 alone.
- [x] **Targeted Test Suite (`tests/healing/test_tool_execution.py`)**:
  - 13 comprehensive tests covering zero-HTTP schema rejection, argument immutability, mapping verification, candidate isolation, type preservation, credential fail-fast, idempotency preservation, response parsing, write uncertainty, verification mismatch rejection, ambiguity handling, and shared registry persistence.

---

- [x] **Task 4: Genuine Fixed-Registry Baseline Agent (`orchestrator/agents/`)**:
  - `orchestrator/agents/catalogue.py`: Canonical immutable frozen v1 baseline catalogue (`get_frozen_v1_catalogue()`) and LangChain tool schemas (`get_langchain_tool_specs()`), completely isolated from active mutable registry commits.
  - `orchestrator/agents/model_provider.py`: Lazy model provider initialization with clear `ModelConfigurationError` when API keys are missing, preserving non-blocking orchestrator startup.
  - `orchestrator/agents/baseline.py`: Genuine LangChain tool-calling baseline agent (`BaselineAgent`). Model selects tools and arguments; tool execution executes through `ToolExecutor` and injected `MockBusinessApiClient`.
  - Finite Execution Bounds: Enforces configurable `max_turns` (5), `max_tool_calls` (10), and deadline `timeout_seconds` (30s).
  - Write Operation Identity: Stable `Idempotency-Key` computation mapped across identical write intents.
  - Rejection of Unknown / Invalid Calls: Rejects uncatalogued tools or schema violations without downstream HTTP calls.
  - Independent Read Verification: Extracts target ticket ID and user from task inputs and genuine directory evidence, and queries `GET /tickets/81` to verify `actual_assignee == resolved_user_id`. Never trusts conversational model "done" claims alone.
  - Truthful Token Usage: Aggregates real provider tokens without double-counting; missing usage marked as `"unavailable"`, diagnosis tokens strictly `0`.
  - API Route Integration: `POST /runs` accepts `execution_type="model_driven_baseline"`, emitting contract-compliant SSE events and returning `RunSummaryResponse`.
  - Targeted Test Suite (`tests/healing/test_baseline_agent.py`): 13 comprehensive unit tests validating paired tool call IDs, catalogue immutability across registry commits, zero-HTTP invalid call rejection, independent read verification, false-claim rejection, drift failure without patching, write idempotency, uncertain write handling, finite limits, truthful usage tracking, and configuration error handling.

---

- [x] **Task 5: Failure Interception & Semantic Classification (`orchestrator/healing/`)**:
  - `orchestrator/models/observation.py`: Immutable, sanitized `FailureObservation` capturing tool name, registry version, canonical SHA-256 fingerprint, execution identities (`run_id`, `tool_call_id`, `idempotency_key`), `ExecutionPhase`, method, endpoint, HTTP status, original argument snapshot, request/response evidence, structured validation details, evidence paths, timing, and `uncertain_write` flag. Guaranteed zero credentials, administrative mutation states, or scenario answer leaks.
  - `orchestrator/models/classification.py`: Strongly typed `ClassificationResult` and `SemanticFailureCategory` (`SCHEMA_DRIFT`, `INVALID_INPUT`, `BUSINESS_RULE_REJECTION`, `AUTHENTICATION_AUTHORIZATION`, `TRANSIENT_SERVICE_FAILURE`, `AMBIGUOUS`). Preserves existing execution-layer `FailureCategory` while modeling semantic classification independently.
  - `orchestrator/models/classification.py` (`RetrySafety`): Models retry safety independently from diagnosis eligibility. Encapsulates `safe_to_retry`, plain-language `reason`, `uncertain_write`, `idempotent_method`, and `requires_input_adaptation`. Uncertain writes are strictly non-retryable regardless of diagnosis eligibility.
  - `orchestrator/healing/classifier.py` (`classify_failure`): Pure deterministic classifier inspecting observed runtime evidence:
    - Pre-execution schema failures classified as `INVALID_INPUT` (zero downstream drift).
    - Structured downstream validation errors (`expected_schema`, `missing`, `unexpected`, `migration_hint`) identify `SCHEMA_DRIFT` candidates.
    - Bare HTTP 400/422 status codes without structured details safely degrade to `AMBIGUOUS`.
    - Documented business error codes (`TICKET_CLOSED`, `TICKET_NOT_FOUND`) classified as `BUSINESS_RULE_REJECTION`.
    - Standard 401/403 classified as `AUTHENTICATION_AUTHORIZATION`; explicit new-header requirements identify `SCHEMA_DRIFT` candidates while clearly affirming credentials cannot be fabricated.
    - Known missing resources (404 on `/tickets/999`) classified as `BUSINESS_RULE_REJECTION`; 404 on endpoints degraded to `AMBIGUOUS`.
    - Transport drops, 429 rate limits, and 5xx errors classified as `TRANSIENT_SERVICE_FAILURE`.
    - Post-write response parsing failures retain write uncertainty and forbid blind retries.
    - Contradictory, malformed, or missing evidence degrades safely to `AMBIGUOUS`.
  - Shared Executor Boundary (`orchestrator/execution/tool_executor.py`): All failure paths in `ToolExecutor` construct a sanitized `FailureObservation`, classify it via `classify_failure`, and attach `failure.observation` and `failure.classification` to `ExecutionResult`. Both deterministic workflows and baseline agent paths receive uniform classifications.
  - Event Integration (`orchestrator/execution/workflow.py`, `orchestrator/agents/baseline.py`): Attached sanitized classification metadata to `tool_call_failed` event data without altering the public event schema or inventing diagnosis activity.
  - Targeted Test Suite (`tests/healing/test_failure_classification.py`): 13 comprehensive unit tests validating bare 400/422 ambiguity, explicit drift candidate detection, business rule rejections, rename non-correspondence invariants, auth header drift, missing resources, rate limits and transport failures, post-write parsing failure uncertainty, contradictory evidence degradation, protection of uncertain writes over diagnosis eligibility, identical classification across execution paths, absolute secret redaction, dynamic ticket verification, and write timeout uncertainty.
  - Full Regression Verification: All 68 backend tests (`tests/mock_api`, `tests/evaluation`, `tests/healing`) passing cleanly.

---

- [x] **Task 6: Deterministic Repair Proposals (`orchestrator/healing/planner.py`)**:
  - `orchestrator/models/proposal.py`: Strongly typed `PlanningStatus` (`proposed`, `insufficient_evidence`, `unsupported`, `not_eligible`), `PlannedOperation` (with operation type, source path, target path, conversion policy, credential reference, value provenance, and evidence path), and `PlanningResult`.
  - `orchestrator/healing/planner.py` (`plan_repair`): Deterministic planner operating strictly on observable runtime evidence without LLM calls, mock admin state, or evaluation scenario answers.
  - Eligibility & Identity Gating: Requires `classification.diagnosis_eligible == True` (`category == 'schema_drift'`) and matching tool snapshot identity (`name`, `version`, canonical SHA-256 `fingerprint`). Stale or mismatched evidence safely returns `not_eligible`.
  - Evidence Grammar Normalization:
    - Normalizes explicit source-to-target migration mappings, structured expected types, and newly required headers.
    - Free-text keyword similarity alone cannot authorize proposals.
    - Missing fields alone without correspondence evidence return `insufficient_evidence` (never assumes rename correspondence).
    - Newly required business values with no caller source return `unsupported` (inventing data is strictly forbidden).
    - Unapproved required headers return `unsupported` (credentials cannot be fabricated or permissions bypassed).
  - Restricted Safe Operations: Formulates `RENAME_ARGUMENT`, `NEST_ARGUMENT`, `CAST_TYPE`, and `SET_HEADER` operations while keeping source argument paths, wire request paths, and schema paths distinct. Original caller inputs (`ticket_id: 81`, `user_id: "123"`) remain strictly immutable.
  - Candidate Preview Generation: Generates defensively copied `ToolDefinition` with updated `request_mapping` and `headers` labeled `proposed_unvalidated`. Does not write to registry.
  - Deterministic Proposal Fingerprint: SHA-256 fingerprint over canonical plan elements excluding timestamps, random IDs, or secret credentials.
  - Targeted Test Suite (`tests/healing/test_repair_proposals.py`): 15 comprehensive unit tests covering explicit renames, nested paths, type casts, approved vs. unapproved headers, combined compositions, missing fields without correspondence, missing business values, destination collisions, stale snapshot gating, distinct path layers, immutability of inputs/registry, deterministic ordering/fingerprinting, zero secrets leakage, frozen observation immutability, and unvalidated `patch_proposed` event emission.
  - Full Regression Verification: All 83 backend tests (`tests/mock_api`, `tests/evaluation`, `tests/healing`) passing cleanly.

---

## 4. Next Tasks

- [x] **Task 7: Safety Validation & Invariants (`orchestrator/healing/validator.py`)**:
  - Independent Deterministic Validation: Pure validation engine (`validate_repair_proposal`) independently verifying proposal integrity, recomputing proposal fingerprints, and verifying that every evidence path resolves into the failure observation.
  - Zero Trust for Planner Previews: Derives the candidate contract independently from base snapshot and validated operations, rejecting tampered or diverging previews.
  - Value Provenance & Conversion Allowlist:
    - Sourced strictly from caller arguments; zero invented business values or hallucinated defaults.
    - Lossless string-to-integer allowlist: strictly rejects leading-zero strings (`"0123"`), decimal truncation (`"12.34"`), boolean-as-int (`True`), and nonnumeric strings.
    - Defensive deepcopy ensures original caller arguments remain strictly immutable.
  - Header & Credential Safety: Maps headers only through approved configuration, validates against injected local credential resolver, and strictly forbids raw secrets from entering candidates, decisions, fingerprints, or events.
  - Adapted-Retry Safety Enforcement:
    - Uncertain writes remain strictly blocked (`uncertain_write=True`).
    - Pre-execution rejection confirmed via HTTP status (400, 401, 403, 422).
    - Repeat POST write blocked on HTTP 200 response drift (prevents duplicate mutations).
  - Accepted Output: Defensively isolated candidate `ToolDefinition` labeled `status: "validated_not_committed"`, transformed arguments copy, evidence summary, and retry authorization. Validation is point-in-time; Task 8 rechecks version/lock before execution.
  - Event Envelopes: Emits `patch_validated` and `patch_rejected` adhering to `contracts/events.schema.json`.
  - Targeted Test Suite (`tests/healing/test_safety_validator.py`): 20 comprehensive unit tests covering valid proposals, tampered fingerprints, forged evidence, diverging previews, stale versions, destination collisions, leading zeros, decimal truncation, bool-as-int, nonnumeric strings, missing credentials, secret leakage, uncertain writes, response drift repeat write prevention, inputs/registry immutability, and event envelopes.
  - Full Regression Verification: All 103 backend tests passing cleanly.

- [x] **Task 8: Bounded Recovery and Registry Commit (`orchestrator/healing/recovery.py`, `orchestrator/agents/healing.py`)**:
  - Task 7 Blocker Fixes:
    - Pre-execution rejection enforcement: HTTP 400/401/403/422 status alone strictly blocks recovery unless explicit trusted endpoint guarantees or execution details confirm pre-mutation rejection.
    - Semantic evidence support: Dot-notated/nested fields and expected types are resolved directly against observation structures.
    - Evidenced numeric semantics: String-to-integer conversion requires verified numeric semantics plus lossless representation.
    - Coherent schema and mapping updates: Model-visible arguments, required fields, and executor wire mappings updated synchronously.
  - Recovery Controller Architecture (`RecoveryController`):
    - Full dependency injection: registry, executor, planner, validator, verifier, event broadcaster, credentials resolver, and execution guarantees.
    - Strict eligibility gating: Non-diagnosis eligible failures, uncertain writes, and unverified status-only rejections strictly blocked.
    - Monotonic version locking: Re-entrant per-tool lock acquired within remaining timeout budget; validates active version and fingerprint before execution.
    - Loop termination: Tracks attempted proposal fingerprints per operation identity; zero infinite retry loops.
    - Bounded execution: Exactly ONE repaired HTTP call executed with candidate snapshot and transformed arguments using stable idempotency keys.
    - Independent verification: Executes `get_ticket` read check asserting both requested ticket ID and intended assignee using identity equivalence (`str(actual) == str(expected)`). Verification is read-only and never invokes recursive healing.
    - Atomic version-checked commit: Calls `commit_candidate` with expected base version only AFTER verification succeeds; staged candidates discarded on failure or timeout.
    - Truthful outcome separation: If repaired write succeeds but verification/commit fails, records separate business vs registry outcomes; never claims write rollback.
  - Genuine Healing Agent Integration (`HealingAgent`):
    - Supplies active registry definitions to LangChain ChatModel with dynamic tool bindings.
    - Intercepts supported runtime tool failures through the recovery controller.
    - Pairs recovered outcomes with original model `tool_call_id`.
    - Automatically refreshes model bindings from newly committed registry contracts before next turn.
    - Subsequent runs share the application registry and execute committed contracts with zero diagnosis tokens.
  - Contract-Compatible Event Flow:
    - Emits `diagnosis_started`, `patch_proposed`, `patch_validated`, `retry_started`, `recovery_verified`, `registry_committed`, and terminal events strictly matching `contracts/events.schema.json`.
    - `recovery_verified` emitted strictly after actual read verification; `registry_committed` emitted strictly after atomic registry activation.
  - Targeted Test Suite (`tests/healing/test_recovery.py`): 14 comprehensive unit tests verifying rename, nesting, type conversion, approved header, status-only rejection blocking, uncertain write prevention, single retry bound, verification mismatch handling, staged candidate cleanup, loop prevention, stale recovery skips, model binding refresh, frozen baseline preservation, and truthful contract-compliant events.
  - Full Regression Verification: All 119 backend tests passing cleanly.

- [ ] **Task 9: LLM-Assisted Diagnosis (`orchestrator/healing/llm_diagnosis.py`)**:
  - Integrate LLM fallback for ambiguous failures where deterministic heuristic returns insufficient evidence.
  - Authorize proposed patches strictly through deterministic `SafetyValidator`.

- [ ] **Task 10: Persistent Audit & Aggregate Metrics (`orchestrator/audit/`, `orchestrator/metrics/`)**:
  - Persistent SQLite storage for audit entries and benchmark metrics.
  - Token tracking and adaptation rate calculations.

---

## 5. Acceptance Criteria
1. Baseline mode fails deterministically when drift mutation is enabled.
2. Healing mode autonomously recovers from rename, type_change, and nesting within 1 retry.
3. Second run reuses hot-committed patch immediately with zero diagnostic overhead.
4. Negative drift scenarios trigger safe refusal without inventing values.
