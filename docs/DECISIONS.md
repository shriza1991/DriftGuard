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
