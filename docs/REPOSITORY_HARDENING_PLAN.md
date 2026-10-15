# DriftGuard repository hardening plan

## Scope

This plan is based on repository commit `1675ccc`. It is an incremental hardening plan, not a rewrite. Preserve the existing FastAPI services, registry, executor, healing pipeline, React frontend, SQLite audit design, and both business workflows unless a change is required for correctness.

## What is already present

- Original ticket-assignment demo with independent read verification.
- Notification-preference mock endpoints and benchmark-only registry tools.
- Recoverable scenarios for rename, lossless type conversion, nesting, configured header, and combined drift.
- Unsafe, non-drift, adversarial, verification-mismatch, and stale-version scenarios.
- Rules-first recovery through the real executor, classifier, planner, validator, recovery controller, and versioned registry.
- Cold, warm-reuse, and frozen-contract baseline benchmark modes.
- JSON, CSV, Markdown, and compact frontend benchmark artifacts.
- Frontend benchmark summary cards with unavailable-state handling.
- Dockerfiles and Compose configuration.

Do not duplicate these features. Inspect and strengthen them.

## P0: correctness and evidence validity

### P0.1 Replace inferred safe stops with authoritative outcomes

Current weakness: `evaluation/hardening_benchmark.py` effectively treats every non-normal, non-verified ContractGuard trial as `safe_stop`. This makes the safe-stop and all-breaking resolution metrics capable of reporting success even when the system merely crashed, timed out, or failed for an unrelated reason.

Required change:

- Add or reuse a typed terminal recovery outcome containing at least:
  - `outcome`: `normal_success | verified_recovery | verified_reuse | safe_stop | failed | benchmark_error`
  - `safe_stop`: boolean derived from the typed outcome
  - `reason_code`
  - `error_category`
  - `retry_eligible`
  - `retry_count`
  - `verification_status`
  - `commit_status`
- Propagate it through recovery, events/audit as appropriate, benchmark raw trials, and frontend summaries.
- Count only explicit `safe_stop` outcomes in the safe-stop numerator.
- Treat unexpected exceptions, incomplete evidence, harness failures, and generic failures as benchmark failures, not safe stops.

Acceptance: deliberately inject a harness error and a generic product failure; neither may increase the safe-stop rate or all-breaking resolution rate.

### P0.2 Replace hardcoded benchmark evidence fields

Current weakness: fields such as `business_values_preserved`, registry isolation values in some modes, and zero token values are assigned directly rather than derived from evidence.

Required change:

- Capture original logical intent and actual outbound candidate request separately.
- Verify that logical `user_id="123"` and `promo=false` are preserved after every transformation.
- Snapshot active version and fingerprint at defined checkpoints: before initial call, after failure, after staging, after candidate retry, after verification, and after terminal action.
- Derive isolation and commit metrics from those snapshots.
- Record actual model-call telemetry per stage. A deterministic stage may report measured zero only when call instrumentation proves no model call occurred. Otherwise use `null`/`unavailable`.
- Add before/after canonical contract projections and verify expected schema plus adapter deltas activate together.

Acceptance: mutate a benchmark field or omit a checkpoint in a test fixture; artifact validation must fail rather than silently report success.

### P0.3 Correct timing semantics

Current weakness: the benchmark timer starts before the initial stale request while the documentation and slide language describe MTTR from first observed contract failure to verified recovery or authoritative stop.

Required change:

- Record separate monotonic timestamps for:
  - workflow start;
  - first contract failure observed;
  - diagnosis start/end;
  - candidate retry start/end;
  - verification completion;
  - commit or authoritative stop.
- Report:
  - end-to-end workflow latency;
  - recovery MTTR from first observed contract failure to verified recovery;
  - safe-stop decision latency from first observed failure to typed safe stop;
  - warm verified-call latency.
- Never label end-to-end latency as MTTR.

Acceptance: raw timestamps recompute to the published aggregate within documented rounding tolerance.

### P0.4 Make the benchmark self-validating

Add an artifact validator that runs before files are published and exits non-zero on any violation:

- retry count above one for ContractGuard;
- commit without successful independent verification;
- commit in an unsafe, non-drift, adversarial, mismatch, or stale-version scenario;
- safe stop counted as adaptation;
- active registry change on rejection;
- missing raw evidence for a published aggregate;
- unknown or incomplete outcome;
- inconsistent JSON, CSV, compact summary, or Markdown;
- secret-like value in an artifact;
- missing scenario/repetition pair;
- duplicate trial identity;
- baseline and ContractGuard pair with non-equivalent initial state or task budget.

Write report artifacts atomically through a temporary directory and rename only after validation passes. A failed benchmark must not leave a `status: measured` summary.

### P0.5 Restore a green regression baseline

At `1675ccc`, an independent run produced 205 passing and 3 failing Python tests. Resolve causes; do not delete, skip, or weaken the tests.

- Diagnose the failing combined-mutation public integration test and preserve the intended ticket workflow behavior.
- Unify dependency declarations. `requirements.txt` installs Groq while `pyproject.toml` keeps it optional; define one documented clean-install path for the complete test suite.
- Ensure test execution is not dependent on ambient proxy variables for localhost clients. Prefer explicit `trust_env=False` where appropriate or a documented isolated test setting.
- Add tests for all P0 benchmark-oracle changes.

Acceptance: a clean checkout can run the documented Python command with zero failures.

## P1: deployment, reproducibility, and scope safety

### P1.1 Fix container build and runtime integration

Known risks:

- Root `Dockerfile` copies `reports/`, but that directory is absent at `1675ccc`; a clean build can fail before benchmarks are generated.
- The production frontend defaults to `/api`, but the Nginx image has no checked-in reverse-proxy configuration equivalent to the Vite development proxy.
- Compose uses startup ordering but no readiness health checks.

Required change:

- Make images build from a clean checkout with no generated reports.
- Serve a clear unavailable benchmark response until validated reports are mounted or generated.
- Add Nginx `/api` and SSE proxying to the orchestrator, or use a tested alternative configuration.
- Add service health checks and readiness-aware dependencies where supported.
- Add `.dockerignore` files and avoid copying temporary databases, environments, smoke results, or secrets.
- Verify `docker compose build`, service health, frontend API calls, SSE streaming, and benchmark-summary unavailable/measured states.

### P1.2 Separate source, smoke output, and canonical evidence

- Remove committed `.benchmark-smoke*` directories from the maintained source tree after preserving any needed debugging information in prose.
- Ignore temporary benchmark, pytest, virtual-environment, and local database directories.
- Define `reports/` as generated canonical evidence with a documented publication policy.
- Do not commit one-repetition smoke results as final evidence.
- Include command, clean/dirty git state, dependency lock hash, platform, Python version, and benchmark schema version in final results.

### P1.3 Prove repair reuse scope or narrow the claim

The current registry is primarily keyed by tool name/version. The benchmark recreates registries per scenario and proves same-process second-call reuse, but it does not prove isolation across provider, origin, tenant, endpoint, method, and contract version.

Choose one evidence-backed path:

1. implement immutable scope metadata and exact-match reuse tests; or
2. explicitly limit the prototype claim to same-process, same-tool, same-provider benchmark reuse.

Do not claim provider-scoped or tenant-scoped reuse in the PPT until it is implemented and tested.

### P1.4 Strengthen baseline fairness

- Document whether the baseline is the existing model-driven agent or a deterministic frozen-contract executor loop.
- Use exactly the same task, starting provider state, mutation, timeout, attempt budget, and verification oracle for each paired comparison.
- Record pair IDs and initial-state fingerprints.
- Categorise baseline outcomes: completed, repeated invalid request, stopped with error, wrong provider state, timeout, or budget exhausted.
- Do not describe the deterministic two-attempt baseline as representative of every standard agent.

## P2: clarity and maintainability

### P2.1 Reconcile documentation

Update stale claims in `README.md`, `AGENTS.md`, architecture/integration docs, and submission files. Examples at `1675ccc` include “Milestone 2 Pending,” “11/11 tests,” a three-tool-only repository description, and outdated directory trees.

Mark `docs/audit-report.md` clearly as the pre-implementation audit of `be5f034`, or append a dated status column without erasing the original evidence.

### P2.2 Validate frontend benchmark data at runtime

The UI must not trust a TypeScript cast alone. Parse and validate the compact summary, schema version, `status`, repetition count, and required metric types. Invalid or partial reports must render **Unavailable**, never zero or measured.

### P2.3 Add CI-equivalent commands

Provide one local validation entry point that runs:

- Python formatting/lint/type checks if adopted;
- full Python tests;
- benchmark unit/oracle tests, not the 30-run suite on every edit;
- frontend tests, typecheck, and build;
- Docker build when available;
- artifact schema and secret validation.

Keep the full 30-repetition benchmark as a release/evidence gate.

## Explicit non-goals

- Rewriting the system in another framework.
- Replacing deterministic rules with a larger model.
- Adding arbitrary code generation or execution.
- Adding multi-tenant claims without scope enforcement.
- Persisting and auto-reactivating repaired contracts after restart without a separate trust design.
- Optimising numbers by removing hard scenarios, weakening the baseline, or shortening measured intervals.

