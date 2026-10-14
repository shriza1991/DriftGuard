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

---

## Phase 2 — orchestrator connection (14 October 2026)

### Recheck before implementation

Phase 1 fixture files, tests, build scripts, root `AGENTS.md`, `docs/member_3`, shared contracts, and current `orchestrator/api/routes.py`, `orchestrator/execution/events.py`, agent outcomes, and mock mutation route were reread. The entry tree was clean on `main` and one commit ahead of `origin/main`; no backend or contract file was edited. The Phase 1 fixture dashboard still built and its six state tests passed.

**The backend changed after the Phase 0/1 audit.** Its current routes, rather than the stale `contracts/api-contracts.md` examples, establish these Phase 2 facts:

| Route | Current behavior used by frontend |
| --- | --- |
| `POST /runs` | Body has `mode`, `task`, `execution_type`, `wait:false`. It validates mode/type combinations, gates concurrent operations, starts an app-managed background task, and returns HTTP 200 with one `run_id` and usually `status:"running"`. `mode:normal` + `execution_type:deterministic`, `mode:baseline` + `model_driven_baseline`, and `mode:healing` + `model_driven_healing` are explicit frontend selections. No paired comparison operation or IDs. |
| `GET /runs/{run_id}` | Authoritative summary with `running/completed/failed/interrupted`, nullable outcome/error/timing/usage, `termination_reason`, `execution_type`, and `audit_status`; SQLite fallback may omit model usage. |
| `GET /runs/{run_id}/events` | Default `data:` messages with `id: <sequence>`, plus named `event:error` for audit/overflow. Supports `Last-Event-ID` or `cursor` query, rejects conflicting values, replays durable events after a cursor, then live events. Initial EventSource URL omits a query cursor so native reconnect can send `Last-Event-ID` without a conflict. A manual full replay reopens without a cursor and deduplicates by `(run_id, sequence)`. |
| `POST /demo/reset` | Requires idle admission; downstream mock reset must succeed before in-memory state and `app.state.registry` reset. Returns 502 and marks state unknown if downstream reset fails. SQLite audit/metrics history remains. |
| `POST /demo/mutation` | Implemented forwarding with `{scenario,enabled,evidence_level:"ordinary"}`. Accepts the allowlisted scenarios (including `none`); frontend requires the returned scenario/enabled values to match before enabling runs. Errors leave the scenario unconfirmed until a reset or successful apply. |

`contracts/api-contracts.md` still describes a 202 run start and a 501 mutation route, and does not specify current SSE resume and reset behavior. Member 2 should coordinate updating this shared contract. No frontend model credential or credential-bearing Vite variable was added. Development uses the existing `/api` Vite proxy to `127.0.0.1:8000`; `VITE_ORCHESTRATOR_URL` can override the public orchestrator address when proxying is unavailable.

### Frontend changes

- `frontend/src/services/api.ts` is one typed client for run creation/status, SSE URL, reset, mutation, registry, and metrics. Network/HTTP/response-shape errors are explicit. The client never falls back to fixtures.
- `frontend/src/services/eventStream.ts` parses default SSE messages defensively, checks run identity and `id`/sequence, deduplicates replay, asks for a full replay after a sequence gap or closed connection, handles native reconnect state, and closes stale streams. Named server error messages trigger replay. The client waits for a contiguous terminal sequence before treating the event stream as complete. Full replay is capped at two client attempts; an incomplete stream is labeled as such.
- `frontend/src/App.tsx` uses an immediate command gate, user-triggered `POST /runs`, the returned run ID, per-lane events, GET status polling, and a final GET summary. A terminal GET with missing terminal SSE gets a brief replay grace period, then closes with an explicit incomplete-timeline notice. Source switching and unmount close obsolete streams; late responses are ignored by generation. Reset and mutation actions are gated and confirmed against response fields.
- The Demo tab has live normal, baseline, and healing actions. Normal is labeled deterministic. Live mutation selection is enabled, but run buttons require a confirmed reset or mutation apply. The live comparison button remains disabled because the backend does not provide a fair paired operation. Registry/Audit displays received live repair events; Benchmark does not present independent live runs as a controlled comparison. Live outcome labels and verification values come from GET `/runs/{run_id}`, with unsupported values shown as unavailable. Fixtures retain the persistent sample label.
- `frontend/src/state/commandGate.ts` supplies synchronous lock semantics; `runState.ts` adds authoritative summary classification and credential-safe display handling. No dependencies were added.

### Verification

| Check | Result |
| --- | --- |
| `npm run build` | Passed TypeScript and Vite production build. Existing Vite/React plugin deprecation notices are nonfatal. |
| `node --experimental-transform-types --test tests/fixture-state.test.ts tests/live-stream.test.ts` | Passed 13/13 tests: fixtures, candidate/commit states, API request paths/bodies, malformed events, replay dedupe/gaps, transport reconnect, stale-stream closure, authoritative summary classification, and immediate double-action gate. |
| `git diff --check` | Passed; Git displayed only checkout line-ending conversion warnings. |
| `Invoke-WebRequest http://127.0.0.1:8000/health -TimeoutSec 3` | Failed: local socket access forbidden by environment permissions. No orchestrator HTTP response was obtained. |
| Python import probe | `python --version` reported 3.13.1; import probe printed a broken `C:\Python313\python.exe` location warning despite importing installed packages. It did not establish a usable live service. |
| Frontend lint | No lint script/config is defined in `frontend/package.json`; no lint result is claimed. |
| Browser/live E2E | No live normal or rename run, real SSE timeline, final GET, interactive rapid-click, or rendered browser inspection could be observed because local HTTP sockets are blocked. Phase 1 browser attempts at preview localhost also timed out. Unit tests verify client/state behavior only. |

### Remaining Member 2 dependencies and questions

1. Add a controlled comparison operation or protocol that resets the mock and active registry to equivalent initial state for each lane, preserves both run IDs/outcomes across reset, and states how history is linked. The frontend intentionally cannot claim a fair live comparison from independent calls.
2. Update `contracts/api-contracts.md` and sample payloads for the current async 200 run start, `execution_type`, mutation request/response, reset 502 behavior, and SSE IDs/replay/error events. Coordinate shared changes per root `AGENTS.md`.
3. Confirm whether `termination_reason` or another authoritative field will classify safe refusals. The current frontend only labels a live safe refusal when an explicit `safe_refusal` marker is present; a generic failed run is shown as failed.
4. Define public sanitized original/candidate request snapshots and patch/audit semantics for the live contract diff, including authoritative candidate-versus-committed state. The event timeline now shows live evidence; the current API does not guarantee the request pair needed for the diff card.
5. Confirm aggregate benchmark fields for adaptation denominator, safe refusals, token completeness, and history-scoped metrics. The UI does not infer missing measurements from samples or a hard-coded zero.

**Phase 2 status: IMPLEMENTED — LIVE VERIFICATION BLOCKED.** The client and UI are wired to current route code, but no browser-to-orchestrator workflow could be observed in this environment. Phase 3 has not started.

---

## Phase 3 — contract diff, registry, and audit views (14 October 2026)

### Gate and current contract

Phase 2 remains **IMPLEMENTED — LIVE VERIFICATION BLOCKED**. This phase implemented views independently and does not claim that a browser, frontend proxy, or backend run was verified. Current `orchestrator/api/routes.py`, `orchestrator/audit/models.py`, `orchestrator/audit/store.py`, `orchestrator/healing/recovery.py`, planner/validator models, tool-call events, and `contracts/api-contracts.md` were inspected again. The shared markdown contract still describes `/patches` as scaffolding, while the route now returns `status:"available"`, paginated `repair_history`, and `active_patches` filtered from records with `registry_committed:true`.

`GET /registry` returns only active tool definitions, version/fingerprint/schema/mappings, with a top-level constant `version:"v1"`. It does not expose staged candidates. `GET /patches?limit&offset` returns persisted `PersistedRepairAttempt` records ordered newest first. Each has run/tool/base/proposal identity, proposal operations, optional candidate definition, validator status/details, recovery status, verification flag, commit flag/version, and termination reason. The backend persists `candidate_version` as `res.committed_version`, so it can be null for a valid uncommitted candidate; the UI uses the recorded candidate definition version when present. The route does not include original caller arguments or full verification detail. For the **current** run, `tool_call_started.data.arguments` supplies the original arguments and `validation_details.transformed_arguments` supplies candidate arguments. Historical records without current run events show the original as unavailable. A successful business verification is available as an audit boolean; method/ticket/assignee detail is shown only when the matching `recovery_verified` event is available.

### Frontend changes

- `frontend/src/services/api.ts` now requests and validates `/registry` and `/patches`. Top-level invalid patch responses fail clearly; incomplete individual repair records are omitted with a warning count. The first page loads 100 records, with a “Load older repair attempts” action for more history. Registry malformed tool definitions are rejected rather than displayed as empty state.
- `frontend/src/state/patchState.ts` projects structured proposal operations, request pairs, lifecycle state, current-run identity, active-version matching, and second-run reuse evidence. Original arguments are matched to the proposal fingerprint and a preceding tool call with the base version/fingerprint; ambiguous calls remain unavailable. Operations are rendered from recorded `transform_spec.operations`, never from the selected scenario. The combined test record renders `{"ticket_id":81,"user_id":"123"}` against recorded validator output `{"ticket_id":81,"target":{"id":123}}`, plus nest/cast/Authorization summaries.
- `frontend/src/components/PatchInspector.tsx` shows Stage → Retry → Verify → Commit; run/tool/version and fingerprint identity; changed paths, conversion policy, per-operation evidence; acceptance/rejection reasons; retry outcome; independent verification; original/candidate requests and schemas when recorded; and sanitized expandable JSON. An audit commit is marked historical if the current `/registry` version/fingerprint no longer matches it after reset. Header credential references/values are hidden in summaries and JSON.
- `frontend/src/components/DashboardViews.tsx` connects the current run’s Demo diff to its matching patch, displays active registry definitions with expandable sanitized JSON, lists rejected and accepted repair history, and lets the user inspect a historical record explicitly. A selected old record cannot become the new run’s default inspector. A reuse panel requires a prior committed record, matching current active tool version/fingerprint, and a second run’s actual `tool_call_started` event. It reports whether `diagnosis_started` was received; it makes no cache-hit claim.
- `frontend/src/App.tsx` refreshes registry, patch history, and metrics after terminal operations and reset. The selected mutation remains applied across another run, so a second task can be issued without resetting a healed registry. No backend or shared contract files changed.

### Verification and limitations

| Case | Evidence / result |
| --- | --- |
| Accepted patch, successful verification and commit | Pure projection test confirms recorded operations, exact original/candidate pair, and active version match only against matching `/registry` version/fingerprint. UI code renders the four lifecycle steps. No live accepted patch observed. |
| Rejected patch, validated but uncommitted, failed retry | Tests keep `registry_committed:false`, preserve rejection reason, and leave the active tool at its original version. UI shows these as candidate/rejected, never as active commits. No live retry observed. |
| Combined transformation | Test verifies `user_id → target.id`, lossless string-to-integer summary, and Authorization from configured credentials, using structured operation records. No live combined run observed. |
| Empty/invalid responses | Tests distinguish empty history from malformed top-level response and omit an incomplete row with a warning; invalid registry tools are rejected. |
| Second-task reuse | Test requires prior committed patch, matching active registry, and the second run’s tool-call version/fingerprint. Actual no-reset second run unavailable, so reuse remains unverified live. |
| `npm run build` | Passed TypeScript and Vite production build; only pre-existing Vite/plugin deprecation notices. |
| `node --experimental-transform-types --test tests/fixture-state.test.ts tests/live-stream.test.ts tests/patch-state.test.ts` | Passed **20/20** tests, including ambiguous same-tool request pairing. |
| `git diff --check` | Passed; line-ending conversion warnings only. |
| `Invoke-WebRequest http://127.0.0.1:8000/registry -TimeoutSec 3` | Failed with socket-access permission error; no live registry, patch response, browser rendering, or second-task reuse could be checked. |
| Lint | No lint script/config exists; no lint result claimed. |

### Backend questions for Member 2

1. Update `contracts/api-contracts.md` for current `/registry` and `/patches` shapes and pagination, including the difference between historical `active_patches` and the **currently active** `/registry` definitions.
2. Expose sanitized original caller arguments (or a run-scoped tool execution projection) for historical repair records, plus independent verification details, if the UI must inspect an older run without reopening its event stream.
3. Persist a genuine staged candidate version separately from `committed_version`; current `candidate_version` is null until commit even when `candidate_definition` exists.
4. Provide a live environment or permitted local HTTP path for frontend-to-orchestrator verification. The existing socket restriction still blocks Phase 2 and Phase 3 end-to-end evidence.

**Phase 3 status: PARTIAL.** The frontend view implementation, build, and state/client tests pass. Live registry, patch lifecycle, and second-task reuse remain unverified because local HTTP is inaccessible. Phase 4 has not started.

---

## Phase 4 — authoritative metrics, safeguards, and presentation (14 October 2026)

### Gate and contract evidence

Phase 2 is still **IMPLEMENTED — LIVE VERIFICATION BLOCKED** and Phase 3 remains **PARTIAL**. No live end-to-end evidence was obtained in Phase 4. Rechecked `orchestrator/api/routes.py`, `orchestrator/metrics/{tracker,run_metrics}.py`, `evaluation/{runner,reporting,scenarios}.py`, `contracts/api-contracts.md`, `docs/{INTEGRATION,DECISIONS,member_3}.md`, and the current frontend source/tests. The markdown metrics contract remains a minimal example and does not describe the route's pagination or per-run fields.

Actual `GET /metrics` returns operational counters, nullable `average_recovery_time_ms`, `limit`, `offset`, `total_runs_count`, and a page of `run_metrics`. Run metrics include measured recovery/commit/failure durations and agent, diagnosis, and session token breakdowns with completeness status. The backend mean is calculated from verified repair records with non-null durations. Its timing sample count is not returned. Aggregate run and repair scans each cap at 1,000, and their scopes differ. `unsafe_trials` is hard-coded zero. The endpoint does **not** expose evaluated recoverable-trial denominator, safe refusals, failed recoveries, benchmark median/p95, or aggregate token comparisons. `evaluation/runner.py` computes those for a controlled evaluation, but no orchestrator route publishes its result. The UI therefore does not derive an adaptation rate from incompatible operational counts or treat `unsafe_trials:0` as a measured safety result.

### Frontend changes

- `frontend/src/types/index.ts`, `state/metricsState.ts`, and `services/api.ts` model and validate the actual metrics response. Invalid responses fail visibly; missing or incomplete token usage is unavailable. A complete measured zero remains zero.
- `components/MetricsView.tsx` separates unavailable controlled benchmark measures, operational counters, and selected-run measurements. It shows the backend mean in milliseconds with the timed sample count marked unavailable. The numerator/denominator for adaptation are both unavailable until a controlled benchmark result is published. Median, p95, aggregate verified successes, safe refusals, and failed recoveries remain unavailable. Current-run diagnosis/session tokens and backend recovery duration use only the matching run ID. Baseline and ordinary-success token observations name their own runs and do not become a paired comparison or cross-run total.
- `App.tsx` tracks metrics loading and errors separately, discards stale metrics on failed refresh, and refreshes registry, patches, and metrics after completed runs and reset. A final `GET /runs/{id}` failure now keeps the command lock and retries status rather than releasing controls. A transport drop alone does not unlock a run. Source switching is disabled and guarded during commands. An ambiguous run-start network/server failure keeps controls locked because no run identifier is available for reconciliation; an explicit 4xx rejection releases them.
- Reset uses the real endpoint, clears the selected-run view after confirmed downstream reset, and refetches persisted data. The UI explains that backend audit/metrics history remains. Completed **sample** comparisons are kept as clearly labelled illustrations after Reset view; none become benchmark measurements. Scenario intent labels distinguish recoverable drift from deliberately unsafe changes without claiming an observed outcome.
- Demo run IDs are shortened with full values in expandable details. Keyboard focus styles, metric-card contrast, and responsive grid breakpoints were added for narrow and laptop-sized layouts. Status colours retain text labels.

### Verification

| Check | Result |
| --- | --- |
| `npm run build` | Passed TypeScript and Vite production build; only existing Vite/plugin deprecation notices. |
| `node --experimental-transform-types --test tests/fixture-state.test.ts tests/live-stream.test.ts tests/patch-state.test.ts tests/metrics-state.test.ts` | Passed **23/23**. New tests check response shape, matching-run selection, nullable timing, incomplete usage, and complete zero. Existing tests cover fixture terminal outcomes, safe refusal, event dedup/replay, switching, and synchronous command gating. |
| `git diff --check` | Passed; Git reported only line-ending conversion warnings. |
| Dev server | `npm run dev -- --host 127.0.0.1` failed with `EPERM` while Vite removed `node_modules/.vite/deps`. No files were deleted manually. |
| Browser/layout inspection | Production preview started at `127.0.0.1:4173`, but browser navigation timed out and `Invoke-WebRequest` reported socket access forbidden. Laptop/narrow CSS breakpoints were inspected statically; rendered layouts could not be confirmed. |
| Live metrics, reset, and busy controls | Not exercised over HTTP because local sockets are blocked. State logic and parser are implemented, but real backend behaviour remains unverified. |
| Lint | No lint script or config exists in `frontend/package.json`; no lint pass is claimed. |

### Remaining backend dependencies for Member 2

1. Publish a controlled evaluation result with explicit recoverable-trial numerator/denominator, unsafe-trial/safe-refusal and failed-recovery counts, matching filters, timing sample count, median and p95 only when meaningful, and complete baseline/healing/ordinary-success token scopes. Keep operational `/metrics` counters distinct from benchmark trials.
2. Provide a recoverable command identity or status lookup for an ambiguous `POST /runs` network/server failure. The frontend cannot safely unlock controls when the request may have been accepted but returned no run ID.
3. Document the actual `/metrics` pagination, cap, units, nullable/`unavailable` fields, and run token completeness in `contracts/api-contracts.md` after coordination. Clarify whether the operational mean can be accompanied by its measured duration sample count.
4. Provide a permitted local/browser HTTP path for real reset, metrics, SSE, and layout verification. A controlled paired comparison endpoint/protocol remains absent, so the live comparison gate is still blocked.

**Phase 4 status: IMPLEMENTED — LIVE AND VISUAL VERIFICATION BLOCKED.** Production build and focused state tests pass. Metrics that the backend does not publish remain unavailable, and no fixture outcome is presented as live or measured benchmark evidence. Phase 5 has not started.
