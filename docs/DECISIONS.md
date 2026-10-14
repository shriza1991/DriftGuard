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

## ADR-022: Independent Deterministic Validation and Candidate Derivation
- **Context**: Relying on candidate previews supplied by planners or external components introduces security vulnerabilities if the preview was tampered with, miscompiled, or contains unbacked contract changes.
- **Decision**: The safety validator (`orchestrator/healing/validator.py`) never trusts supplied candidate previews. It independently derives the candidate definition from the base snapshot and validated operations, then strictly compares the derived candidate with the preview (if provided), rejecting any discrepancies. The validator also recomputes the proposal fingerprint and checks that every evidence path resolves into observable evidence.
- **Rationale**: Guarantees zero unbacked mutations and maintains verifiable integrity between observed evidence, declarative plan, and executable candidate contracts.

## ADR-023: Strict Conversion Allowlist and Write Uncertainty Protection in Safety Validation
- **Context**: Automatic type conversions (e.g. string to int) can silently truncate data (e.g. converting `"0123"` to `123` drops leading zeros, or `"12.34"` drops decimals). Furthermore, stateful writes that experience network drops or succeed downstream (HTTP 200) with response drift risk duplicate mutations if blindly retried.
- **Decision**:
  1. Implement a strict, non-lossy conversion allowlist: string-to-integer conversion requires exact lexical invertibility (`str(int(val)) == val`), explicitly rejecting leading-zero strings (`"0123"`), decimals (`"12.34"`), bool-as-int (`True`), and non-numeric strings.
  2. Enforce strict write uncertainty protection: any observation with `uncertain_write=True` or an HTTP POST write returning 200/201 (response drift) is strictly denied retry authorization (`authorized_for_recovery = False`).
- **Rationale**: Eliminates data corruption and duplicate side-effects on downstream services, ensuring safe recovery execution under Task 8.

## ADR-024: Bounded Recovery Lifecycle and Independent Intent Verification
- **Context**: Autonomous recovery execution must never enter infinite repair loops, overwrite newer registry versions, execute speculative unverified retries, or fabricate recovery claims.
- **Decision**:
  1. Enforce a single repair proposal and at most ONE repaired HTTP execution per failed invocation.
  2. Guard same-tool recovery under a re-entrant asynchronous tool lock (`AsyncRLock`) and re-verify version and fingerprint freshness prior to staging. Stale observations abort speculative retries (`RecoveryStatus.STALE_SKIPPED`).
  3. Track attempted proposal fingerprints per operation identity (`attempt_key = f"{op_id.key}:{proposal_fp}"`) to permanently terminate repair loops.
  4. Perform independent read verification (`get_ticket`) prior to registry commit, checking requested ticket and intended assignee using identity equivalence (`str(actual) == str(expected)`).
  5. Discard staged candidates on any execution failure, timeout, or cancellation.
  6. Record separate business vs registry outcomes: if a write succeeds downstream but verification or commit fails, record `business_effect_occurred=True` without claiming rollback or re-executing.
- **Rationale**: Adheres strictly to Genesis Hackathon safety invariants, preventing duplicate mutations and race conditions during concurrent execution.

## ADR-025: Strict Pre-Execution Rejection Guarantees for Stateful Writes
- **Context**: HTTP 400, 401, 403, and 422 status codes indicate rejection, but alone do not guarantee that the downstream server rejected the request *before* executing mutating side-effects.
- **Decision**: Reject automated recovery retries for stateful HTTP writes (`POST`, `PUT`, `PATCH`, `DELETE`) with 400/401/403/422 status unless:
  1. The execution guarantees explicitly specify `pre_execution_rejection: True`, or
  2. The failure observation includes `validation_details["pre_execution_rejection"] = True`.
- **Rationale**: Guarantees zero unverified writes on partially mutating endpoints, maintaining rock-solid retry safety.

## ADR-026: LLM-Assisted Fallback Diagnosis with Untrusted API Evidence Isolation
- **Context**: While machine-readable errors (e.g. `migration_mapping` objects) can be planned deterministically, downstream APIs frequently communicate contract changes via natural language error messages (e.g., `"Field user_id was renamed to assignee_id in v2 API"`). Unassisted regex heuristic parsing is brittle, but unchecked LLM generation risks prompt injection, hallucinated values, and credential theft.
- **Decision**:
  1. Deterministic planning is executed first; the LLM diagnoser is invoked **strictly** as a fallback when deterministic planning returns `insufficient_evidence` or `unsupported` on eligible `SCHEMA_DRIFT` failures.
  2. The LLM is never invoked for non-eligible failures, uncertain writes, unverified status-only rejections, or exhausted operations.
  3. API error text is treated strictly as untrusted data. Prompts clearly delimit quoted API error content from system instructions, explicitly prohibiting instruction following within errors.
  4. The model has zero tool access, zero network browsing capability, and zero access to mock admin states or benchmark answers.
  5. The evidence packet is bounded to 4000 characters with all credentials/secrets recursively redacted. If essential evidence exceeds the budget, diagnosis refuses rather than silently truncating.
- **Rationale**: Enables robust interpretation of natural language drift evidence while preventing prompt-injection exploits and preserving strict security boundaries.

## ADR-027: Strict Natural Language Evidence Grammar and Independent Validator Authorization
- **Context**: If an LLM proposes argument renaming, nesting, or type conversion based on natural language error text, trusting the model's self-reported mapping or confidence introduces hallucinations (e.g. guessing that missing `assignee_id` corresponds to `user_id` without explicit correspondence evidence).
- **Decision**:
  1. The LLM must output structured JSON matching the established proposal format (`rename_argument`, `nest_argument`, `cast_type`, `set_header`) accompanied by verbatim `evidence_quote` and `evidence_path`.
  2. Local diagnoser validation strictly rejects unknown fields, malformed paths, path traversal or `/admin` endpoints, and non-lossless conversions.
  3. Model confidence or semantic similarity is **never** accepted as evidence.
  4. The deterministic safety validator independently verifies the quoted natural language text against the original observation:
     - Rename operations require explicit correspondence phrasing connecting source and target (e.g. `renamed to`, `deprecated ... use`, `use ... instead of`, `replaced by`, `->`). A missing-field error alone (e.g. `Missing required parameter: assignee_id`) strictly cannot authorize a rename.
     - Nesting operations require explicit nesting markers (e.g. `nested under`, `moved under`, `sub-object`, `expected object`).
     - Type conversions require explicit expected type evidence and must satisfy the lossless lexical invertibility allowlist.
  5. At most ONE LLM diagnosis call and at most ONE repaired HTTP dispatch are permitted per logical operation.
- **Rationale**: Enforces AGENTS.md Invariant 2: "Model proposes, deterministic validation authorizes."

---

## Technical Clarifications & Invariant Reminders

> [!IMPORTANT]
> **Observation vs. Proof**:
> 1. `expected_schema` alone in an error response does **not** establish semantic field correspondence (e.g. that `assignee_id` corresponds to `user_id`).
> 2. An HTTP `401 Unauthorized` or `422 Unprocessable Content` status alone does **not** prove contract drift (it may indicate genuine authorization failure or bad caller intent).
> 3. HTTP 400/401/403/422 status codes alone **never** authorize write retries without an explicit trusted pre-execution rejection guarantee.
> 4. Proposed repair candidates remain strictly `proposed_unvalidated` until validated by Task 7 and staged/committed by Task 8.
> 5. Validated candidates are marked `status: "validated_not_committed"`. Validation is a point-in-time check; Task 8 re-verifies version freshness under the tool lock before recovery dispatch.
> 6. LLM proposals have `source: "llm_assisted"` and are authorized through the identical deterministic safety validator as deterministic proposals. Neither source confers automatic authorization.




