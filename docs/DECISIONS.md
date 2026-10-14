# DriftGuard Architectural Decision Records (ADRs)

## ADR-001: Separation of Diagnosis from Mock Admin State
- **Context**: To test drift recovery, the test harness can inspect `/admin/state` to know exactly what drift scenario was injected.
- **Decision**: The Orchestrator's healing diagnosis engine is strictly prohibited from querying `/admin/state` or `/admin/mutation`.
- **Rationale**: A real-world agent registry has zero access to internal downstream server code or hidden flags. Autonomous recovery is only scientifically valid if derived purely from observable HTTP responses and validation payloads.

## ADR-002: Staged Patches and Single Verified Retry
- **Context**: If a patch proposal is attempted, should the agent retry indefinitely?
- **Decision**: DriftGuard permits exactly ONE staged retry attempt per drift failure. If the retried call fails, the candidate patch is discarded, and the run fails safely.
- **Rationale**: Prevents infinite loops, quota exhaustion, and state corruption on downstream systems.

## ADR-003: Mandatory Idempotency Keys on Mutation Calls
- **Context**: Retrying network requests against non-idempotent endpoints can cause duplicate writes (e.g. duplicate tickets, conflicting assignments).
- **Decision**: All `POST /tickets/assign` calls must supply an `Idempotency-Key` header. Downstream mock checks and caches prior responses.
- **Rationale**: Safe retries require verifiable idempotency.

## ADR-004: Independent Read Verification Before Hot Commit
- **Context**: Even if an endpoint returns HTTP 200, how does DriftGuard ensure the business write succeeded?
- **Decision**: Before committing a patch to the registry, the orchestrator invokes the read endpoint (`GET /tickets/81`) to verify the assignee ID matches expectations.
- **Rationale**: Guards against false-positive 200 responses where data was not written or was silently dropped.

## ADR-005: Honest Metric Tracking (Zero Synthetic Data)
- **Context**: In hackathon prototypes, it is tempting to hardcode 99.8% recovery rates or synthetic token charts.
- **Decision**: All metrics endpoints and UI cards report strictly measured runtime values (`healing_trials: 0`, `token_usage: "unavailable"` in scaffolding). Unimplemented features must return explicit typed stubs.
- **Rationale**: Adheres to the Genesis Hackathon PS01 integrity bar and transparent engineering.

## ADR-006: Dedicated Pure-Business Client Segregation
- **Context**: Having admin methods (`reset`, `mutation`, `state`) on the downstream client risks accidental use by orchestrator agents or healers.
- **Decision**: `MockBusinessApiClient` implements solely business endpoints (`find_user`, `assign_ticket`, `get_ticket`, `execute_tool`). Admin methods are segregated into a separate `MockAdminApiClient` used strictly by test harnesses and manual reset endpoints.
- **Rationale**: Enforces the architectural isolation boundary at compile/type level.

## ADR-007: Preservation of Structured Validation Evidence in SanitizedFailure
- **Context**: Standard HTTP clients discard response JSON bodies when raising exceptions.
- **Decision**: `MockApiClientError` wraps `SanitizedFailure`, which preserves structured downstream validation evidence (`expected_schema`, `received_payload`, `code`, `hint`), while recursively scrubbing sensitive credentials (`auth`, `token`, `secret`, `password`, `key`).
- **Rationale**: Autonomous healing depends on diagnostic evidence while maintaining strict data security.

## ADR-008: Non-Blocking Lifespan and Dependency Injection
- **Context**: In production, downstream services may not be available at orchestrator startup.
- **Decision**: The orchestrator lifespan initializes client connection pools with finite timeouts and does not execute network or LLM calls during startup. Custom `httpx.AsyncBaseTransport` can be injected for offline deterministic testing.
- **Rationale**: Ensures fast, deterministic container and test startup with zero network flakiness.

## ADR-009: In-Memory Versioned Registry Lifecycle and Atomic Commit
- **Context**: When hot-committing contract patches, concurrent runs or stale proposals could cause race conditions or corrupt the active registry.
- **Decision**: The `VersionedToolRegistry` manages immutable snapshots via defensive deep copies, preserves full historical snapshots in chronological order, stages candidates under unique IDs without mutating active definitions, and executes atomic version-checked commits.
- **Rationale**: Prevents partial mutations, race condition overwrites, and state corruption.

## ADR-010: Canonical Contract Fingerprinting
- **Context**: Contract versions and candidate IDs change across staging/commit lifecycles, and dictionary key order can vary across platforms.
- **Decision**: Contract fingerprints are computed using SHA-256 over canonical contract content with `json.dumps(..., sort_keys=True)`. Lifecycle metadata (`version`, `status`, `candidate_id`) and actual secret credentials are strictly excluded.
- **Rationale**: Ensures fingerprint stability across non-semantic variations (e.g. key order) while remaining strictly sensitive to schema, endpoint, or mapping changes.

## ADR-011: Re-Entrant Per-Tool Locking (`AsyncRLock`)
- **Context**: When future recovery code acquires a tool lock to execute staged retries and then calls `commit_candidate`, a standard `asyncio.Lock` causes a deadlock.
- **Decision**: The registry uses `AsyncRLock` keyed to `asyncio.current_task()`. Calling nested registry operations from the task already holding the lock succeeds re-entrantly. Different tools use independent locks and run concurrently without contention.
- **Rationale**: Guarantees concurrency safety while preventing task self-deadlocks.

## ADR-012: Pre-HTTP JSON Schema Validation
- **Context**: If an agent produces arguments that fail the tool's declared JSON Schema, should an HTTP request still be sent to the downstream service?
- **Decision**: `ToolExecutor` strictly validates caller arguments against the captured snapshot's `argument_schema` before ANY HTTP call. Invalid arguments immediately produce a `VALIDATION_FAILURE` with zero network requests.
- **Rationale**: Eliminates unnecessary network load, prevents downstream error pollution, and protects against malformed writes.

## ADR-013: Deterministic Safe Request/Response Mapping without Eval
- **Context**: Mapping arguments into HTTP parameters and extracting responses into outcomes could be implemented using dynamic evaluation (e.g. Python `eval()` or JMESPath expression execution).
- **Decision**: Mappings are executed through a pure recursive dictionary/list tree traversal with exact type preservation (integers stay integers) and tokenized JSONPath extraction (`$[0].id`, `$.ticket_id`). Executable code evaluation is strictly forbidden.
- **Rationale**: Prevents remote code execution (RCE) vulnerabilities and guarantees deterministic, reproducible argument translation.

## ADR-014: Independent Assignee Read Verification Over HTTP 200 Trust
- **Context**: A downstream write endpoint (`POST /tickets/assign`) might return HTTP 200 OK without actually updating state (e.g. silent discard, async queue failure, or no-op assignment).
- **Decision**: The normal workflow and future recovery workflows never declare success based on HTTP 200 alone. Step 3 (`get_ticket`) reads ticket state and verifies `actual_assignee_id == intended_user_id` and `actual_ticket_id == 81`.
- **Rationale**: Guards against false-positive success reporting and verifies true business intent achievement.

## ADR-015: Write Uncertainty Flagging Without Retries on Post-Write Response Failure
- **Context**: If a downstream write returns an unparseable response (e.g. non-JSON proxy error) or suffers a transport disconnect after sending the request, the state of the write is unknown.
- **Decision**: The failure is flagged with `uncertain_write = True`, and the executor strictly performs ZERO automatic retries.
- **Rationale**: Blind retries against stateful mutating endpoints risk duplicating operations or corrupting data. Safe recovery requires explicit read verification prior to any retried write.

## ADR-016: Immutable Frozen v1 Baseline Catalogue
- **Context**: The healing registry dynamically stages and hot-commits updated tool definitions (e.g. v1.1.0, v2.0.0). If the baseline agent reads from the mutable registry, its behavior and failure benchmarks would change whenever healing commits a patch.
- **Decision**: The baseline agent operates strictly against a frozen canonical v1 catalogue (`orchestrator/agents/catalogue.py`), derived directly from the canonical v1 specifications. Commits or candidate stagings in `VersionedToolRegistry` never mutate baseline schemas, tool specifications, or execution snapshots.
- **Rationale**: Scientific baseline comparison requires an immutable reference point. A baseline that mutates across runs cannot benchmark unhealed failure rates.

## ADR-017: Independent Verification Over Model Done Claims and Truthful Usage
- **Context**: LLMs can hallucinate success, emitting conversational messages like "Ticket 81 has been successfully assigned to Maya" even when the downstream API rejected the assignment or was never called.
- **Decision**: The baseline agent never accepts model claims as proof of completion. It derives business intent from trusted inputs and genuine tool call evidence, then independently queries `GET /tickets/81` to verify `actual_assignee == resolved_user_id`. A final "done" response without matching downstream state is strictly flagged as a `verification_failed` failure. Real provider token counts are aggregated without double-counting; missing usage is recorded as `"unavailable"`, and baseline diagnosis tokens are strictly 0.
- **Rationale**: Prevents false-positive success reporting and guarantees truthful, auditable evaluation metrics.

## ADR-018: Strict Separation of Execution-Layer Categories from Semantic Classification
- **Context**: The execution runtime already tracks `FailureCategory` (`VALIDATION_FAILURE`, `HTTP_REJECTION`, `TRANSPORT_FAILURE`, etc.) for network and transport handling. Conflating this with semantic classification (schema drift vs. invalid caller input vs. business rejections) would lead to loss of technical execution context.
- **Decision**: Introduce a dedicated, strongly typed `SemanticFailureCategory` (`schema_drift`, `invalid_input`, `business_rule_rejection`, `authentication_authorization`, `transient_service_failure`, `ambiguous`) within `ClassificationResult`. Both layers are preserved on `SanitizedFailure`: `failure.failure_category` represents the execution-level transport mechanism, while `failure.classification` represents the semantic reasoning.
- **Rationale**: Clean separation of concerns allows the execution engine to handle transport invariants while the healing pipeline reasons about semantic contract evolution.

## ADR-019: Decoupling of Retry Safety from Diagnosis Eligibility
- **Context**: If an error is classified as schema drift, it may be eligible for repair diagnosis. However, if the error occurred during or after a stateful write (such as a POST write where the network disconnected or response parsing failed), blind retries risk duplicate writes or corrupted state.
- **Decision**: Retry safety is modeled independently from diagnosis eligibility via `RetrySafety`. Diagnosis eligibility indicates whether a candidate patch can be derived from the evidence; `RetrySafety.safe_to_retry` evaluates whether repeating the request is safe. Uncertain writes are strictly marked `safe_to_retry: false` regardless of diagnosis eligibility, and idempotency keys alone do not authorize unverified writes.
- **Rationale**: Eliminates dangerous race conditions and write corruption on downstream systems.

## ADR-020: Deterministic Evidence-Backed Repair Planning and Proposal Isolation
- **Context**: When API schema drift occurs, arbitrary heuristics or ungrounded generative proposals could invent parameters, corrupt business intent, or introduce unauthorized changes into the tool registry.
- **Decision**: Repair proposal generation (`orchestrator/healing/planner.py`) is deterministic, evidence-backed, and strictly declarative. It operates solely on observable runtime evidence (`FailureObservation`, `ClassificationResult`) and read-only approved credential reference configurations. Out-of-band knowledge (admin state, mock code, mutation names) is strictly forbidden. Proposals generate isolated candidate previews with status `proposed_unvalidated` and perform zero registry commits or staging.
- **Rationale**: Keeps proposal generation auditable, reproducible, and verifiable before Task 7 static safety validation and Task 8 recovery execution.

## ADR-021: Strict Separation of Caller Argument Identity, Wire Request Paths, and Schema Paths
- **Context**: Tools interface between agent callers and downstream HTTP APIs. Conflating caller arguments with HTTP wire payload structures leads to brittle mutations where the caller's interface is unnecessarily broken or corrupted.
- **Decision**: Clearly distinguish three path categories:
  1. **Caller Argument Paths**: Immutable business parameters provided by the agent (e.g. `user_id: "123"`).
  2. **Wire Request Paths**: Downstream HTTP request payload structure (e.g. `request_mapping.body["target"]["id"]`).
  3. **Argument Schema Paths**: JSON schema definitions declaring acceptable caller parameter names and types.
  Drift adaptations primarily modify `request_mapping` to bridge unchanged caller arguments to downstream wire changes. Argument schemas are updated only when contract types or caller parameters genuinely change.
- **Rationale**: Preserves the immutability of business intent (AGENTS.md Invariant 3) while accommodating complex wire structural drifts like nested objects or renamed fields.

---

## Technical Clarifications & Invariant Reminders

> [!IMPORTANT]
> **Observation vs. Proof**:
> 1. `expected_schema` alone in an error response does **not** establish semantic field correspondence (e.g. that `assignee_id` corresponds to `user_id`).
> 2. An HTTP `401 Unauthorized` or `422 Unprocessable Content` status alone does **not** prove contract drift (it may indicate genuine authorization failure or bad caller intent).
> 3. Proposed repair candidates remain strictly `proposed_unvalidated` until validated by Task 7 and staged/committed by Task 8.


