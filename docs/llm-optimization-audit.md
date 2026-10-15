# ContractGuard LLM optimization audit

Date: 2026-10-15  
Audited base: `1d2c33a` (`harden benchmark artifacts and deployment`)  
Working tree before audit: clean; `main` matched `origin/main`.

This is the mandatory pre-implementation audit for the second hardening pass. The existing deterministic repair, one-retry limit, independent verification, atomic version check, benchmark gates, and both business workflows are protected behavior.

## Current call graph

1. A normal tool call runs through `ToolExecutor`; it does not call `LLMDiagnoser`.
2. On failure, `RecoveryController` classifies the sanitized `FailureObservation` and exits for non-eligible or uncertain writes.
3. `plan_repair` runs first. Supported rename, lossless conversion, nesting, configured header, and supported combinations are deterministic.
4. If deterministic planning returns `insufficient_evidence` or `unsupported`, the controller may call `LLMDiagnoser.diagnose` once per in-memory operation key.
5. The diagnoser makes one model call and returns declarative operations. Deterministic `validate_repair_proposal` remains authoritative.
6. An accepted candidate is staged, dispatched once, independently verified, then atomically committed under the active base version.
7. The model-driven ticket agent has a separate planning call through `ModelGateway`; that call is not a repair-LLM call and must remain separately labelled.

## Current-state audit

| Area | Current behavior | Existing call/storage cost | Risk | Proposed change | Acceptance test |
|---|---|---|---|---|---|
| Normal execution | Normal tool success never enters recovery. Model-driven agent planning is separate from repair diagnosis. | Zero repair-LLM calls; model-driven execution may use one planning call. | Metrics can conflate agent planning with repair diagnosis. | Add typed repair-call decisions and purpose-specific counters. | Normal deterministic and model-driven paths record `NO_CALL_NORMAL` for repair; repair-call count is zero. |
| Deterministic classifier and planner | Classifier excludes auth, permission, 429/5xx, ambiguous status-only errors, and uncertain writes. Planner handles structured rename, nesting, type, header, and combinations. | CPU-only; no durable decision record. | Eligibility/no-call checks are split between classifier, planner, controller, and diagnoser. | Add one central typed LLM call policy invoked after classification and deterministic planning. | Per-scenario policy tests assert exact decision and `reason_no_call`. |
| LLM diagnosis boundary | Controller calls `LLMDiagnoser` only after deterministic `insufficient_evidence`/`unsupported`, with one in-memory call marker per operation. | At most one diagnosis invocation per controller lifetime/operation key. | No central policy, no durable incident budget, missing-config attempts are not audit records, and unsupported deterministic stops may unnecessarily reach the LLM. | Central policy returns `NO_CALL_NORMAL`, `NO_CALL_NON_DRIFT`, `USE_DETERMINISTIC_RULE`, `USE_VALIDATED_CACHE`, `CALL_LLM_ONCE`, or `SAFE_STOP`. | No second call after timeout, malformed response, rejection, or repeated incident. |
| Model provider construction and retries | `ModelGateway` reserves Groq HTTP attempts in SQLite, disables SDK retries, uses a process semaphore of one, and records Groq `Retry-After` cooldown. | Separate `llm_call_budgets` and `llm_provider_cooldowns` tables; default total run limit is two because agent planning and diagnosis share a gateway. | Provider-specific breaker, no repair-specific global concurrency policy, and model configuration failure is not an interaction row. | Keep transport attempt accounting but add repair-specific policy/budget and generalized breaker metadata. | Rate-limit opens breaker; deterministic execution remains unaffected; missing configuration produces a persisted unavailable interaction without dispatch. |
| LLM response shape | One response currently combines status/explanation/operations; Pydantic forbids extra fields and local checks reject unsupported operations and ungrounded quotes. | One model call, JSON parse, local semantic checks. | Schema differs from the requested diagnosis+patch envelope; prompt/version/model metadata and response-size/token caps are incomplete. | Introduce versioned strict response envelope with diagnosis, patch, reasoning summary, and `stop_if_invalid`; cap response size and preserve one-call shape. | Extra fields, low confidence, missing evidence refs, oversized output, and injection text are rejected without execution. |
| Candidate validation | Independent deterministic validator recomputes fingerprints, resolves evidence, checks provenance/conversions/credentials/retry safety, and derives the candidate. | CPU-only. | Cache reuse could bypass current validation if bolted on incorrectly. | Route deterministic, cached, and LLM proposals through the same validator every time. | Cached and LLM candidates fail identically when current policy/evidence/version no longer validates. |
| Recovery controller | Per-tool lock, one recovery-operation marker, one repaired dispatch marker, staging, pre-write audit, verification, and version-checked commit. | In-memory incident markers plus SQLite repair/audit rows. | LLM gating and fallback are embedded; an LLM audit write failure is only marked incomplete after the fact and does not necessarily block retry. | Orchestrate the exact hierarchy: normal, deterministic, exact cache, one LLM call, validation, one retry, verification, commit; mandatory interaction persistence before retry. | Interaction storage failure returns typed `audit_storage_failure`, leaves registry/provider unchanged, and emits no retry event. |
| Audit/event persistence | Runs, executions, repair attempts, events, model token usage, and pre-write authorization are persisted and redacted. | SQLite schema v2. `model_usages` stores tokens but not prompt/response status or failure payload. | Successful, malformed, timed-out, rejected, missing-config, and provider-failed interactions are not represented uniformly or append-only. | Add schema migration and typed `llm_interactions` rows for every attempted/blocked call outcome, plus indexes and retention metadata. | One redacted row per attempted call/outcome; raw secret scan is clean; failed mandatory persistence blocks retry. |
| Existing reuse | A committed registry definition is reused on subsequent same-process calls; no diagnosis occurs because the active contract succeeds. | In-memory registry only; no LLM response cache. | It proves same-process active-contract reuse, not exact response-cache scope or cross-provider/tenant reuse. | Add bounded durable cache of verified declarative patches keyed by provider/origin/method/endpoint/tool/contract/error/argument-shape/evidence/policy/prompt/schema/model scope. | Exact hit revalidates; every scope mismatch, expiry, corruption, policy change, or stale version misses/invalidates. |
| Cache and invalidation | No repair-response cache exists. | None. | Repeated exceptional evidence may call the model again across processes; unsafe broad keying would cross scope. | Cache only after successful validation, one retry, independent verification, and atomic commit; deterministic TTL/size eviction. | Cache writes occur only after commit; cache key contains hashes rather than secret values; mismatches never reuse. |
| Fallback paths | Non-drift exits early. Deterministic planning failure may use LLM. LLM timeout/provider/malformed output returns planning failure. Pre-write audit failure blocks dispatch. | Failures appear in `RecoveryResult` and sometimes repair/model-usage rows. | Failure reasons are not a single typed decision path; cache/storage degraded modes do not exist. | Add explicit decision/fallback enums and authoritative safe-stop reasons; no second LLM call. | Provider outage/auth/429/timeouts/malformed output all preserve registry and retry count zero unless a validated candidate was already authorized. |
| Benchmark instrumentation | Raw trials record `llm_called`, diagnosis/planning flags, tokens, retries, verification, checkpoints, and outcomes. Reproducible benchmark config has no LLM. | 1,110 release rows at 30 repetitions; deterministic model token overhead measured as zero. | It infers call reduction from events/result source and lacks policy/cache/interaction/breaker latency fields. | Add decision path, repair-call count, avoided calls, deterministic/cache/LLM counts, persisted interaction status, breaker metrics, and lookup/storage/model latency. | Artifact validator recomputes all new aggregates and rejects missing/inconsistent per-trial evidence. |
| Frontend display | UI distinguishes measured/unavailable benchmark values and shows diagnosis/patch lifecycle events. It does not expose prompts. | Existing `/metrics`, `/patches`, `/benchmark-summary`, SSE. | No typed LLM/cache decision view; current `diagnosis_started` describes both deterministic diagnosis and potential model use. | Expose safe metadata only: 0/1 repair calls, decision path, cache status, persistence/status, versions, interaction ID, fallback reason. | Runtime parsers reject malformed metadata; UI never renders prompt/response/secret fields and never labels cache hit as fresh model work. |
| Docker/runtime configuration | Compose is healthy, frontend proxies API/SSE, and images build cleanly. Model settings are environment-driven. | SQLite local volume semantics are not configured for durable cross-container retention. | Cache/audit retention/config defaults could be implicit or lost on container replacement. | Add documented retention, TTL, size, timeout, breaker, and concurrency settings with safe defaults; keep startup credential-optional. | Clean Docker build/start/readiness; missing model key leaves deterministic paths healthy and exceptional model path safely unavailable. |
| Secret redaction | `redact_secrets` protects audit rows/events; evidence packet redacts known secret-shaped fields and lists only approved header names. | Redaction occurs before current persistence. | Hash/cache construction or failure strings could include URLs, headers, raw provider payloads, or secrets. | Canonical sanitizer shared by prompt, interaction store, cache key, events, and reports; hash sanitized canonical payloads only. | Canary secrets are absent from DB, cache keys, SSE, API, frontend payloads, and benchmark artifacts. |
| Concurrency and circuit breaking | Registry uses per-tool re-entrant locks; gateway uses one process semaphore; Groq 429 cooldown is durable. | Global model concurrency is one process-local call; cooldown lives in SQLite. | No provider-agnostic failure threshold/half-open behavior and no exact per-incident durable repair-call claim. | Add bounded repair-call coordinator with durable breaker state and per-incident uniqueness. | Concurrent equivalent incidents respect the limit; breaker opens/closes deterministically; deterministic requests continue. |

## Existing persisted telemetry

- `runs`: lifecycle, terminal status, verified outcome, audit completeness.
- `tool_executions`: original/repaired dispatches, sanitized arguments/results/failures, timing, operation identity.
- `repair_attempts`: proposal/candidate/validation/recovery/verification/commit outcome and aggregate diagnosis tokens.
- `events`: ordered redacted SSE-compatible lifecycle evidence.
- `model_usages`: per-call purpose and token counts, with explicit completeness.
- `pre_write_authorizations`: mandatory persisted authorization before repaired mutation dispatch.
- `llm_call_budgets` and `llm_provider_cooldowns`: gateway attempt accounting and Groq cooldown, created outside the audit-store migration version.

Missing durable telemetry: complete interaction envelopes, parse/validation status, provider/model/prompt/schema versions, sanitized payload hashes, cache lineage/status, response reuse, storage failure outcome, and retention metadata.

## Existing event names relevant to repair

`tool_call_failed` -> `diagnosis_started` -> `patch_proposed` -> `patch_validated` or `patch_rejected` -> `retry_started` -> `recovery_verified` -> `registry_committed`, followed by `run_completed` or `run_failed`.

The current `diagnosis_started` event proves entry into diagnosis, not an LLM call. New policy/interaction events must preserve that distinction and remain backward compatible with the shared event contract.

## Cache candidates and scope conclusion

- Active-contract reuse is already safe for the current process because the committed full tool definition becomes the active registry entry.
- A durable LLM-response cache is new functionality and must not be keyed by error text or tool name alone.
- The safest cache value is the validated declarative operation set plus evidence references and lineage to the original interaction/commit, never executable code or provider prose.
- No cross-provider or cross-tenant cache claim is currently supported. Until exact scope is implemented and tested, public claims remain limited to same-process active-registry reuse.

## Ordered implementation plan

1. Add typed policy decisions and centralized policy tests without changing retry behavior.
2. Add schema-v3 append-only interaction and cache persistence with redaction, retention, and indexed exact-scope lookup.
3. Refactor the diagnoser to the versioned one-call response envelope and mandatory persistence.
4. Integrate exact cache lookup/revalidation and generalized breaker/concurrency behavior into recovery.
5. Extend events/API/frontend with safe metadata only.
6. Extend benchmark raw evidence, aggregates, validator, and scenario assertions.
7. Run focused tests, full Python/frontend suites, smoke/stability/release benchmarks, secret scans, and Docker/runtime verification before publishing replacement measurements.

