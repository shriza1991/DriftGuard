# Member 3 Phase 0 audit — 14 October 2026

Scope: repository inspection only. No product implementation or backend files changed. Findings below distinguish source-level implementation from a demonstrated live frontend.

## Repository and instructions

- Initial Git state: `main...origin/main`, clean working tree. No branch change, commit, push, or deploy.
- Root layout: `contracts/` (schema and examples), `docs/` (plans and integration notes), `evaluation/` (scenario catalog and benchmark stub), `frontend/` (one React/Vite page), `mock_api/` (business and admin API), `orchestrator/` (routes, agents, execution, healing, registry, audit, metrics), `submission/`, and `tests/{evaluation,healing,mock_api}`. Also present: `README.md`, `AGENTS.md`, `.env.example`, `pyproject.toml`, `requirements.txt`, and `venv/`.
- Read root `AGENTS.md`; a hidden-inclusive search found no nested `AGENTS.md` under `frontend/`, `docs/`, or elsewhere. Inspected agent-related directories: `orchestrator/agents/` contains Python agent implementations; no repository `.agents/` or `.codex/` directory is present. Directory existence was checked independently of file searches.
- Configuration inspection used `.env.example` variable **names** only. Relevant frontend name is `VITE_ORCHESTRATOR_URL`; `VITE_MOCK_API_URL` is declared but unused. No `.env` values were read or reproduced.

## Sources inspected

- Planning and startup: `README.md`, `docs/architecture.md`, `docs/demo-script.md`, `docs/member_1.md`, `docs/member_2.md`, `docs/member_3.md`, relevant sections of `docs/INTEGRATION.md` and `docs/DECISIONS.md`, `submission/evidence_checklist.md`, `.env.example`, `.gitignore`, `pyproject.toml`.
- Contracts: `contracts/api-contracts.md`, `contracts/events.schema.json`, all `contracts/examples/*.json` request, response, and event examples.
- Frontend: `frontend/package.json`, lockfile header and dependency entries, `vite.config.ts`, `tsconfig.json`, `index.html`, every `frontend/src/**/*.ts` and `*.tsx` file, and `frontend/src/index.css`. There is no route library, fixture module, frontend test file, or frontend lint configuration. The app is a single page.
- Orchestrator: `api/routes.py`, `app.py`, `config.py`, `models/__init__.py`, `execution/{workflow,events}.py`, `registry/{models,default_tools,service}.py`, `healing/{stubs,recovery}.py`, `agents/{baseline,healing}.py`, `audit/{models,store}.py`, `metrics/{run_metrics,tracker}.py`; inspected API and event generation points in other agent/execution files by search.
- Mock and evaluation: `mock_api/{app,business/models,business/router,mutations/manager,mutations/scenarios,fixtures/initial_data}.py`, `evaluation/{runner,reporting,scenarios}.py`.
- Tests: inventory of `tests/`; read relevant route, reset, metrics, mock API, and event-schema tests in `tests/healing/{test_stubs,test_audit_and_metrics,test_versioned_registry,test_foundation,test_recovery,test_safety_validator}.py`, `tests/mock_api/test_mock_api.py`, and `tests/evaluation/test_scenarios.py` (some via targeted searches/sections). Their presence is evidence of coverage intent, not of this audit's successful execution.

## Current architecture and plan fit

React 18 + TypeScript + Vite 5 are configured. `App.tsx` owns all page state: mode, editable task, one current run, one event array, registry entries, aggregate metrics, and busy flags. `services/api.ts` calls absolute `VITE_ORCHESTRATOR_URL` or `http://127.0.0.1:8000`; Vite also defines a `/api` development proxy, but the client never uses it. No frontend router or persistence layer exists. `Controls`, `Timeline`, `ContractDiffView`, `RegistryView`, `MetricsView`, and `Header` are presentation components.

The Maya/81/123 default task matches the target workflow. `POST /runs` executes one workflow synchronously and returns its final summary with one `run_id`; `App` then opens SSE and receives historical replay. The source supports a normal deterministic run, and separate backend paths exist for `execution_type: "model_driven_baseline"` and `"model_driven_healing"`. The UI sends only `{mode, task}`. Selecting `healing` therefore enters the deterministic workflow's healing-stub path on failure, not the model-driven healing agent. There is no comparison orchestration or pair of run identifiers. The written 202/running contract in `contracts/api-contracts.md` conflicts with the implemented 200/final-summary route.

The main page has controls, a single timeline, metrics cards, a registry card grid, and a contract-diff placeholder. It has no baseline-versus-healing screen, audit view, version history, or live diff. There is no login, tenancy, or infrastructure expansion.

## Actual orchestrator endpoint contract

| Endpoint | Implemented request and response | Frontend relevance / discrepancy |
| --- | --- | --- |
| `POST /runs` | Body `mode` (string, default `normal`), `task` (default canonical prompt), optional `execution_type`; HTTP **200** after awaited execution. `RunSummaryResponse`: `run_id`, `status`, `mode`, `task`, `created_at`, nullable `completed_at`, `duration_ms`, `outcome`, `error`, `model_usage`, `termination_reason`, `execution_type`. One run ID only. | `mode=baseline` or `execution_type=model_driven_baseline` selects frozen baseline; `execution_type=model_driven_healing` selects healing agent; `mode=healing` alone uses deterministic workflow. No comparison endpoint/ID pair. Contract document says 202/running and includes `events_count`, which response model does not. |
| `GET /runs/{run_id}` | Same summary model from memory; SQLite fallback after memory loss; 404 if absent. Persisted fallback fills `outcome` from verified outcome but omits persisted `model_usage`. | Frontend has no wrapper or status refresh. `RunSummary` TS type omits backend optional fields and `interrupted`. |
| `GET /runs/{run_id}/events` | `text/event-stream`; each event is `data: {RunEvent JSON}\n\n`, plus `: ping` comments every 2 s while waiting. Envelope: `run_id`, per-run `sequence` starting at 1, UTC `timestamp`, `mode`, `type`, nullable `tool`, object `data`. | Default messages, **not named SSE events**. Events are published in workflow order and replayed from in-memory list or SQLite if in-memory list empty. No SSE `id:` or `Last-Event-ID` handling, cursor, or server deduplication. Frontend deduplicates `(run_id, sequence)` only, closes on error, and does not reconnect or refetch summary. Replay/listener registration can duplicate events at a live boundary; frontend dedupe handles those received. A persisted completed run after reset may replay then remain open because terminal detection checks only in-memory run state. Unknown IDs can leave a heartbeat stream open. |
| `GET /registry` | `{tools: ToolDefinition[], version: "v1", updated_at}`. Each active tool includes name, version, fingerprint, description, endpoint, method, headers, argument schema, request/response mappings, credential reference, status. | Only active definitions exposed; staged candidates and history are internal. Top-level `version` is constant `v1`, not an aggregate contract revision. UI shows active tool cards only, loaded on mount/reset, not after commit. |
| `GET /patches?limit=50&offset=0` | `{status:"pending_member_2", notice, active_patches, repair_history, limit, offset, count}`. `active_patches` comes from an empty `HealingEngineStub` list; `repair_history` is paginated SQLite `PersistedRepairAttempt` records with proposal, candidate definition, validation, recovery, verification, and commit fields. | A committed registry change is visible through `/registry` and audit history, **not reliably through `active_patches`**. Staged candidates are not returned despite route docstring. `fetchPatches()` exists but is unused and typed `any`. Fixed `pending_member_2` status can coexist with real audit history. |
| `POST /demo/reset` | No body. Returns `{status:"reset_completed", orchestrator:"state_cleared", mock_api:<reset result>, audit_store:"preserved"}`. Clears workflow manager's in-memory runs/listeners/events/metrics and its registry; forwards mock `/admin/reset`; SQLite history remains. | Route reports `reset_completed` even if mock reset was unavailable. `GET /registry` and run execution use `app.state.registry`, which this reset does **not** replace; committed contracts can therefore remain active. UI clears its visible run and reloads registry/metrics. Persistent metrics and run history remain, so reset does not clear comparison history or counters served from SQLite. |
| `POST /demo/mutation` | No request model; always HTTP **501** `{status:"not_implemented", message}`. | Mock API has `/admin/mutation` with eight scenario branches and reset behavior, but orchestrator demo route does not forward it. UI scenario select is disabled. |
| `GET /metrics?limit=50&offset=0&mode=...` | `SystemMetrics`: `total_runs`, `normal_runs`, `normal_success_count`, `healing_trials`, `verified_recoveries`, `unsafe_trials`, nullable `average_recovery_time_ms`, `token_usage`, `status`, `note`, `limit`, `offset`, `total_runs_count`, `run_metrics[]`. `RunMetrics` has run/mode/execution/status, duration, turns, original/repaired dispatch counts, attempts/rejections/recoveries/commits, three millisecond timing fields, agent/diagnosis/session token breakdowns, audit status. | Recovery timing is ms from observed failure to independent verification; commit latency and failed recovery duration are separate. Token breakdown values are integers or `"unavailable"`, with `complete`/`incomplete`/`unavailable` status. Top-level `token_usage` is only `"measured"` or `"unavailable"`, not a token count. `unsafe_trials` is hard-coded 0; safe refusals are not an authoritative top-level metric. Aggregate queries cap scans at 1,000 rows; `total_runs` can undercount larger histories. UI uses only six aggregate fields, shows missing metrics as zero, and labels `unsafe_trials` as if authoritative. |

Event types in shared schema and frontend union: `run_started`, `tool_call_started`, `tool_call_failed`, `diagnosis_started`, `patch_proposed`, `patch_validated`, `patch_rejected`, `retry_started`, `recovery_verified`, `registry_committed`, `run_completed`, `run_failed`. The actual UI renders generic JSON for all; CSS highlights only a subset. Contract examples are fixtures, not evidence that all events occur in a live demo. Some model-driven agent/recovery code emits the healing lifecycle, but the current UI does not invoke that execution type.

## Feature matrix

`VERIFIED LIVE` requires observed browser-to-service behavior during this audit. No feature met that bar because frontend dependencies and the local Python runtime were unavailable.

| Feature | Status | Evidence / limitation |
| --- | --- | --- |
| React/TS/Vite single-page shell and responsive cards | IMPLEMENTED BUT UNVERIFIED | Components, stylesheet, and Vite configuration exist; build and browser run blocked. |
| Editable canonical task, normal run trigger, disabled active-run button | IMPLEMENTED BUT UNVERIFIED | `Controls.tsx` and `App.tsx` call `POST /runs`; no live UI execution observed. |
| One-run SSE timeline with replay dedupe | IMPLEMENTED BUT UNVERIFIED | `EventSource.onmessage` and `(run_id,sequence)` dedupe match default SSE route; no reconnect, event ordering guard, or final status refetch. |
| Registry active tool cards | IMPLEMENTED BUT UNVERIFIED | `GET /registry` integration exists; versions/fingerprints shown, but no history, mappings, audit, or post-commit refresh. |
| Aggregate metrics cards | IMPLEMENTED BUT UNVERIFIED | Calls `/metrics`, but does not use per-run data; default-zero display can hide unavailable/failed fetch. |
| Reset control | IMPLEMENTED BUT UNVERIFIED | Calls `/demo/reset`; no success/error feedback; server reset/registry mismatch remains. |
| Drift mutation controls | BLOCKED | UI disabled; orchestrator `/demo/mutation` is 501 despite mock admin scenarios. |
| Baseline versus healing comparison with two run timelines/outcomes | MISSING | Only `currentRun` and one event array; one ID returned per `POST /runs`. |
| Live contract diff, candidate versus committed labels | FIXTURE ONLY | `ContractDiffView.tsx` contains hard-coded rename and verification preview; it never calls `/patches` or uses events. |
| Registry versions and audit evidence/decisions/verification view | MISSING | Backend SQLite repair records exist, but no frontend view or navigation. |
| Safe refusal badges and authoritative adaptation/refusal metrics | MISSING | Generic event JSON only; no reason classification UI; `unsafe_trials` currently hard-coded 0 and no safe-refusal count. |
| Service connectivity indicators and action feedback | FIXTURE ONLY | Header dots/port pills are static green; run/reset errors go to console. |
| Frontend tests, lint command, dedicated type-check command | MISSING | `package.json` has only `dev`, `build`, `preview`; build includes `tsc`. |

## Validation performed

| Command | Actual result |
| --- | --- |
| `git status --short --branch`, file inventories, source reads/searches | Initial tree clean on `main`; final tracked changes limited to this report. |
| `npm ci --offline` in `frontend/` | Failed `ENOTCACHED` for `yallist`; no dependencies installed. |
| `npm ci --no-audit --no-fund` | Failed `ENOTEMPTY` while clearing partial `node_modules` on Windows. |
| `npm install --no-audit --no-fund` | Failed `ENOTFOUND registry.npmjs.org`; its lockfile edits were restored to the original content and line endings. Ignored partial `node_modules` may remain. |
| `npm run build` | Failed: `tsc` not recognized. Type-check and Vite build did not run. |
| `npm exec --offline -- tsc --noEmit -p tsconfig.json` | Failed `ENOTCACHED` because `tsc` is absent. |
| Lint / frontend tests | Not run: neither script nor frontend test files/configuration exist. |
| Selected Python tests via `venv/Scripts/python.exe` and `python -m pytest` | Produced `Failed to find real location of C:\Python313\python.exe` and no test results before interruption. The local venv points at that unavailable base path. This audit does not claim a passing backend suite. |

No local services were started and no live run, SSE exchange, UI screenshot, or endpoint response was produced in this audit.

## Questions for Member 2 / shared contract decisions

1. What is the supported comparison protocol: two explicit `POST /runs` calls with externally coordinated mock reset/scenario selection, or a backend comparison command returning a pair of IDs? How will both runs be linked and preserved for the UI?
2. Which public request selects the genuine healing agent? Today `mode: "healing"` selects deterministic execution; must the UI send `execution_type: "model_driven_healing"`? Likewise, should baseline send `execution_type: "model_driven_baseline"`? Confirm statuses and missing-model behavior.
3. Please reconcile `POST /runs` contract documentation (202/running) with implemented 200/final summary. If synchronous by design, is SSE intended as replay for short runs? Should long model runs be backgrounded?
4. Define `/demo/mutation` request/response and orchestrator forwarding, including scenario, enabled/evidence level, safe sequencing with reset, and errors. This currently blocks demo controls.
5. Fix/clarify reset ownership: should `POST /demo/reset` replace `app.state.registry`, and how should it signal mock reset failure? Confirm whether historical SQLite metrics/runs should remain visible after reset and how UI filters a demo session.
6. Specify SSE resume semantics: `id`/`Last-Event-ID` or query cursor, terminal replay after reset/restart, behavior for unknown run IDs, and duplicate/live-boundary ordering. Confirm whether default `data:` messages remain the contract.
7. Define a public patch projection that distinguishes proposed, validated/staged, discarded, and committed versions and exposes sanitized changed fields, evidence, decisions, and verification. Is `/patches` repair history authoritative for this, and when will its fixed pending status change?
8. Confirm authoritative metric definitions and availability: adaptation denominator, safe refusal count, unsafe trial count, session token totals with incomplete usage, recovery timing, and pagination/aggregate behavior beyond 1,000 records. Current `unsafe_trials=0` is not evidence of safety.
9. Coordinate changes to `contracts/api-contracts.md` and `contracts/events.schema.json` across Members 1–3 before changing shared request or event shapes, per root `AGENTS.md`.

## Recommended next changes, in order

1. Resolve the run/mode/comparison and mutation/reset API contract with Member 2 and Member 1. Update shared contracts with the agreed shapes before frontend wiring.
2. Align frontend TypeScript API types with actual nullable summaries, per-run metrics, patch/audit records, and explicit unavailable states. Add error and connectivity feedback so the UI does not imply success from defaults or static green dots.
3. Build comparison state around two run IDs, independent event histories, outcome and timing views, and safe run/reset/mutation sequencing.
4. Harden SSE lifecycle: ordered merge/dedupe, terminal status lookup, cleanup on unmount/new run, and agreed replay/reconnect behavior.
5. Replace hard-coded diff preview with live candidate/committed and audit records; expose changed fields, evidence, validation decision, independent verification, and version/fingerprint history.
6. Render measured adaptation, recovery, token, success, and refusal metrics only when the backend marks them authoritative; preserve unavailable/incomplete states. Add targeted frontend validation once dependency access is restored.

Phase 1 can start with frontend preparation that relies on stable existing shapes, but a trustworthy live comparison and dashboard cannot be completed safely until the endpoint and reset/SSE/metric questions above are resolved. This audit stops before Phase 1.

---

## Phase 1 — fixture dashboard (14 October 2026)

Phase 0 was reviewed before implementation. Its source findings still match the route and event code. On entry to Phase 1, `frontend/package.json` and `frontend/package-lock.json` already had uncommitted dependency changes (including Vite 8), and `docs/member3-progress.md` was untracked. Those existing changes were preserved; this phase did not change dependency declarations, backend code, or shared contracts.

### Frontend changes

- `frontend/src/App.tsx`: three-tab shell, explicit Sample/Live source switch, separate baseline and healing state, sample playback, reset, backend error handling, and cleanup of timers/SSE subscriptions. Sample is the default source. Live mode retains the existing single-run API route; live comparison and mutation remain disabled.
- `frontend/src/components/DashboardViews.tsx`: Demo controls, side-by-side run cards, ordered event timeline with expandable redacted JSON, request comparison, independent verification summary, Registry & Audit view, and Benchmark view. The sample label appears in the sticky header and in every tab's data area.
- `frontend/src/fixtures/scenarios.ts`: documented frontend-only sample shapes for normal success, rename recovery, combined nesting/type/header recovery, and missing-business-value safe refusal. The selector lists all eight agreed mutation names; unsupported sample choices show an unavailable notice and disable execution. Samples are restricted to the canonical Maya/81 task so edited input cannot produce misleading sample results.
- `frontend/src/state/runState.ts`: per-run `(run_id, sequence)` sorting/deduplication, terminal outcome and candidate state derivation, event colour categories, and defensive credential redaction for displayed JSON.
- `frontend/src/services/api.ts`: existing live SSE integration retained, with explicit stream/parse error callback. `frontend/src/index.css`: responsive dark dashboard styling. No new runtime dependency was added.
- `frontend/tests/fixture-state.test.ts`: focused state tests for all four samples, run separation, ordering, nonterminal tool failure, validation versus commit, normal success, safe refusal, deduplication, and credential redaction.

### Frontend-only sample payload contract

The samples use the **shared `RunEvent` envelope** (`run_id`, per-run `sequence`, ISO `timestamp`, `mode`, `type`, nullable `tool`, object `data`) and the 12 event type names from `contracts/events.schema.json`. These are explanatory frontend fixtures, not captured backend responses. The sample adds a human-readable `data.summary` field; the schema permits extra `data` keys but does not require or promise this field. The UI falls back to event type/tool for real events without it.

Each sample has independent `sample-<scenario>-baseline` and `sample-<scenario>-healing` IDs. `run_completed.data.outcome` in the fixture includes `ticket_id`, `resolved_user_id`, `verified_assignee_id`, `verified: true`, and `verification_method`. That shape is a frontend illustration built from the canonical workflow and is **not** declared as the authoritative live response. The original request is `{ticket_id:81,user_id:"123"}`. Rename shows `{ticket_id:81,assignee_id:"123"}` only after a proposal; combined shows `{ticket_id:81,target:{id:123}}` plus a *header-name/reference description*. No credential value is present. Safe refusal has no candidate request and no `run_completed` or `registry_committed` event.

UI state is evidence based: `tool_call_failed` leaves a run running until a terminal event; `patch_validated` means validated but uncommitted; only `registry_committed` marks the sample registry committed; `run_failed` with fixture `safe_refusal:true` is a refusal, separate from completion; and `run_completed` without `outcome.verified === true` displays completion with verification unavailable. The fixture uses explicit labels and never invents benchmark rates, times, token counts, or credential values. The sample registry fingerprint is shown as unavailable.

### Verification and limits

| Check | Result |
| --- | --- |
| `npm run build` | **Passed**: TypeScript and Vite production build. Vite 8 / React plugin emitted nonfatal deprecation recommendations tied to the pre-existing dependency versions. |
| `node --experimental-strip-types --test tests/fixture-state.test.ts` | **Passed 6/6** focused tests covering all four fixtures, ordering, terminal and unavailable states, candidate transitions, deduplication, and redaction. |
| Frontend lint | No lint script or lint config exists in `frontend/package.json`; no lint result claimed. |
| `npm run dev -- --host 127.0.0.1` | Failed with `EPERM` while Vite tried to remove `node_modules/.vite/deps` in the OneDrive checkout. Production build still passed. |
| `npm run preview -- --host 127.0.0.1` | Preview process started and announced `http://127.0.0.1:4173/`. An HTTP request from the shell was blocked by socket permissions. |
| Browser visual inspection | Attempted with the in-app browser at both `127.0.0.1:4173` and `localhost:4173`; both timed out. A direct local file URL was then rejected by browser security policy as a disallowed protocol. **Rendered visual inspection and interactive browser exercise are unavailable in this environment.** Source and state tests cover the logic; layout appearance remains unverified. |

The sample loading state appears while event playback is active; empty states appear before a run, when registry data is absent, and when no benchmark sample has been run; error notices cover unsupported samples and live API/SSE failures. These states were inspected in source, not exercised in a browser. The existing Python/backend tests were not rerun in Phase 1.

### Remaining backend dependencies

The Phase 0 questions remain open: an agreed paired comparison protocol and run identifiers; explicit model-driven baseline/healing selection; the real `/demo/mutation` request/response; reset semantics for `app.state.registry` and SQLite history; SSE replay/reconnection and terminal behavior; an authoritative patch projection with candidate/committed evidence; and measured adaptation, safe-refusal, timing, and token fields. Until those contracts are coordinated, live comparison, live mutation, live contract diff, live audit, and benchmark claims remain unavailable. Sample outcomes must not be reported as backend verification.

**Phase 1 status: PASSED — FIXTURE DASHBOARD**, with visual inspection blocked by the local browser connection boundary. Phase 2 has not started.
