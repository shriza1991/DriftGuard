# Groq inference budget and per-run evidence — 15 October 2026

## Root cause and scope

The previous `BaselineAgent` and `HealingAgent` loops called `bound_model.ainvoke(messages)` after every tool result, including successful `find_user`, `assign_ticket`, and `get_ticket` results and a final-response turn. `HealingAgent` could also invoke `LLMDiagnoser` after a failed tool call. `ChatGroq` inherited LangChain's default `max_retries=2`, so a single logical invocation could send more than one HTTP request after a provider error. These are code-level causes. **No before-change outbound attempt count was measured for one run**, so this report makes no “15 to 2” savings claim and does not derive per-run counts from Groq's 24-hour dashboard.

The frontend already acquires `CommandGate` synchronously before `POST /runs`. The backend's admission gate rejects a second run while one is active. SSE opens and reconnects only through `GET /runs/{id}/events`; it does not create a run. The live double-submission check below observed HTTP 409 for an immediate second `POST /runs`.

## Implementation

- Live Groq runs use one structured `submit_workflow_plan` response. The model selects the three allowed business tools and their arguments. Assignment must refer to the observed `find_user.user_id` result; a literal or invented assignee is rejected. The validator checks task intent, active tool names and schemas, required fields, result references, order, and lossless integer conversion. `PlanReplayModel` feeds validated steps to the existing baseline/healing execution loops without another inference request. Final text and success state come from independently verified backend results.
- The existing recovery controller still tries deterministic evidence-backed repair first. If that cannot produce an eligible proposal, `LLMDiagnoser` may make one restricted, sanitized JSON proposal request through the same run gateway. There is no proposal-correction loop. Stage → retry → verify → commit and rejection history remain in the existing controller.
- `ModelGateway` is the live Groq entry point. Its `BudgetedGroqTransport` reserves each `/chat/completions` HTTP attempt in SQLite **before** dispatch. `BEGIN IMMEDIATE` makes the two-attempt limit atomic across concurrent callers and process reconstruction with the same `run_id`. It counts responses, failures, HTTP 429, planning and repair attempts separately from logical invocations and downstream tool HTTP dispatches. `ChatGroq(max_retries=0)` and `httpx.AsyncHTTPTransport(retries=0)` remove hidden automatic retries; no provider fallback is attempted. A process-wide semaphore limits concurrent inference to one. Maximum output is 700 tokens per request.
- A 429 is recorded as a provider failure. When `Retry-After` is present, its delay is stored as a provider cooldown (capped at one hour), so a subsequent attempted request during that period is blocked before transport. The gateway does not immediately retry. Two requests can still encounter request or token quotas.
- `GET /runs/{id}` returns `llm_calls` for active and persisted model-driven runs. Terminal events and persisted outcomes include the same snapshot. The live Demo run card displays **LLM API attempts: X / 2** from that backend field; absent data displays **Unavailable**. Provider token usage is numeric only when reported completely; otherwise the gateway marks it unavailable. Deterministic execution has no Groq attempt claim.

This planning change applies to both model-driven baseline and healing. The matched baseline/healing pair below keeps equivalent initial state and model planning architecture; any lower call count cannot be attributed solely to healing.

## Live integrated measurements

Each row below comes from a single isolated backend run, `GET /runs/{id}` and its persisted event stream, against mock API port 18101 and orchestrator port 18100. The SQLite budget table counts actual HTTP transport attempts. No 24-hour dashboard total was used.

| Scenario and run | Logical invocations | Outbound LLM HTTP attempts | Planning / repair attempts | Tool API calls | Outcome | Provider token usage |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Normal no mutation, `run-37350b2b` | 1 | **1 / 2** | 1 / 0 | 4 | Completed; independent ticket verification confirmed assignee `123` | 566 input + 364 output |
| Rename with explicit hint, `run-3f70d0c5` | 1 | **1 / 2** | 1 / 0 | 6 | Failure → deterministic diagnosis → candidate → retry → business verification → registry commit; assignee `123` | 566 input + 263 output |
| No-reset registry reuse, `run-f9f280a8` | 1 | **1 / 2** | 1 / 0 | 4 | Completed; the new plan supplied `assignee_id`, with no diagnosis event | 568 input + 359 output |
| Rename with ordinary evidence, `run-af0ff1af` | 2 | **2 / 2** | 1 / 1 | 3 | Model-assisted proposal was rejected by deterministic validation; no commit or claimed business success | 1,555 input + 600 output |
| Matched rename baseline, `run-7d20c62e` | 1 | **1 / 2** | 1 / 0 | Recorded in run | Failed on drift; immediate duplicate `POST /runs` returned 409 | Available in persisted metrics; not copied into this table |
| Matched rename healing after equivalent reset, `run-96f509c5` | 1 | **1 / 2** | 1 / 0 | Recorded in run | Completed; independent verification confirmed assignee `123` | Available in persisted metrics; not copied into this table |

For `run-af0ff1af`, the recorded rejection said the cited `raw_error_message` did not semantically support the proposed `user_id` → `assignee_id` rename. This is a bounded safe failure, **not** a successful model-assisted repair. The deterministic explicit-hint repair used zero extra diagnosis inference requests. In the reuse run, the model received the committed schema on its next run and chose `assignee_id`.

After restarting the isolated orchestrator with the same SQLite file, `GET /runs/{id}` still returned attempt counts 1, 1, 1, and 2 for the first four rows. Opening the baseline SSE replay twice did not change its attempt count. An immediate second `POST /runs` during `run-7d20c62e` returned HTTP 409; this verifies the backend admission check. The frontend's synchronous `CommandGate` and SSE-only reconnection path were inspected, but an actual rapid browser double-click was not performed.

## Mocked transport and regression checks

- A mocked 429 response counted one failed outbound attempt and one HTTP 429, then `Retry-After: 60` blocked the next request before transport. Token usage displayed unavailable. No real quota was consumed for this test.
- Three concurrent reservation calls against one run allowed exactly two and rejected the third. Reconstructing `RunCallBudget` with the same `run_id` and SQLite file retained the count.
- A mocked planning response drove dependent `find_user` → `assign_ticket` → `get_ticket` steps with one logical model request. A literal `999` assignee or invalid structured plan failed validation without a corrective model request. A mocked gateway allowed one planning and one repair HTTP request, then blocked a third. `ChatGroq.max_retries` was verified as 0.
- The relevant baseline, diagnosis, gateway and provider tests passed **40/40**; Member 2 recovery and registry regression tests passed **14/14**; the full Python suite passed **207/207** with 17 existing FastAPI deprecation warnings. `python -m compileall` and frontend `npm run build` passed. No frontend lint or test script exists in `frontend/package.json`.
- After a final SQLite connection-lifetime cleanup, the focused call-budget suite passed again **7/7**. The full-suite result above predates that cleanup.

## Files changed

`orchestrator/agents/model_gateway.py`, `orchestrator/agents/workflow_plan.py`, `orchestrator/agents/model_provider.py`, `orchestrator/agents/baseline.py`, `orchestrator/agents/healing.py`, `orchestrator/healing/llm_diagnosis.py`, `orchestrator/execution/tool_executor.py`, `orchestrator/api/routes.py`, `frontend/src/types/index.ts`, `frontend/src/components/DashboardViews.tsx`, and `tests/healing/test_model_call_budget.py`.

## Remaining limitations

- The ordinary-evidence model-assisted repair was safely rejected; no ordinary-evidence success is claimed. The validator's semantic rejection should be investigated separately if this scenario is required for the demo.
- The strict plan validator currently supports the prototype task form `Find <name> ... ticket <number>` and its three allowed tools. Unsupported tasks and invalid plans fail explicitly. The final independent `get_ticket` check remains a separate downstream API call; it is included in `tool_api_calls`, not LLM attempts.
- The browser visual check was stopped by the computer-use tool because it could not determine the active Chrome URL with enough confidence to enforce its policy. The dashboard TypeScript/build passed, but rendered call-count placement and rapid UI double-click remain unverified.
- In-process provider concurrency is one. The SQLite attempt reservation applies across reconstructed budgets and workers sharing the same database. The provider cooldown is also SQLite-backed. It does not protect a deployment that points workers at separate database files; that infrastructure is outside the current local prototype.

**Observed healing-run limit:** explicit-hint healing `run-3f70d0c5` **1/2**, ordinary-evidence healing `run-af0ff1af` **2/2**, matched explicit-hint healing `run-96f509c5` **1/2**. The two-attempt ceiling **passed** for every tested healing run. Only the explicit-hint runs achieved verified recovery.
