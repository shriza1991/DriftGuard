# Member 3 local demonstration guide

**Status (14 October 2026):** Deterministic recovery and a real Gemini model-driven normal/rename-healing/reuse path have been verified against isolated local services. A controlled paired comparison still lacks a backend contract, and provider quota can block further live runs. Keep these limits visible to judges.

## Start the services

From the repository root, use three terminals:

```powershell
python -m uvicorn mock_api.app:app --host 127.0.0.1 --port 8001
python -m uvicorn orchestrator.app:app --host 127.0.0.1 --port 8000
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open `http://127.0.0.1:5173/`. Vite proxies `/api` to `http://127.0.0.1:8000` through `frontend/vite.config.ts`. The frontend uses `/api` by default; `VITE_ORCHESTRATOR_URL` may point at a permitted backend base URL when proxying is not used. Never place model or assignment credentials in `VITE_` variables or frontend files. Keep secrets server-side.

The orchestrator must have an authorized assignment credential available to its configured reference for the combined header mutation. The relevant server-side configuration names are `ORCHESTRATOR_ASSIGNMENT_TOKEN` and the mock API's `MOCK_API_ASSIGNMENT_BEARER_TOKEN`; the values must agree for this local demo. Configure them outside the repository and keep values out of screenshots/logs. A real model-driven path requires `ORCHESTRATOR_MODEL_PROVIDER=gemini`, a supported `ORCHESTRATOR_MODEL_NAME`, and the server-side `ORCHESTRATOR_MODEL_API_KEY` (the backend also accepts legacy `MODEL_NAME` and `MODEL_API_KEY`). Install the declared Gemini extra and restart the orchestrator after changing configuration. The local Gemini run succeeded before a later quota/rate-limit response; confirm availability immediately before presenting. No frontend credential is required.

## Exact live steps

1. Select **Live**. Confirm the persistent **Live backend · real API data only** label. Confirm the registry and metrics load; a connection error means stop and troubleshoot before presenting outcomes.
2. Click **Reset backend**. Confirm the message says the backend and mock reset succeeded. Reset restores downstream business state and the active registry; it clears the selected run, while persisted run/audit/metrics history remains. A completed baseline is retained in this browser session as a labelled historical card. Reset is disabled during an active command.
3. For ordinary success, choose **None**, leave the prefilled Maya/ticket 81 task, choose **Ordinary response evidence**, click **Apply mutation**, then **Run normal**. The actual final run should resolve Maya as user `123`, assign ticket `81`, and independently read back assignee `123`. Read the run ID and final verification on the Demo tab; inspect `GET /runs/{run_id}` or `GET /tickets/81` if independent confirmation is needed.
4. For the recoverable combined mutation, click **Reset backend**. Choose **Combined · nesting, type, header**, **Explicit migration hint**, and **Deterministic · no model**. Click **Apply mutation** and wait for backend confirmation. Click **Run deterministic healing**. The real event timeline should show `tool_call_failed`, `diagnosis_started`, `patch_proposed`, `patch_validated`, `retry_started`, `recovery_verified`, `registry_committed`, and `run_completed`. The original/candidate card should show `{"ticket_id":81,"user_id":"123"}` → `{"ticket_id":81,"target":{"id":123}}`. Verify the final observed assignee is `123`.
5. Open **Registry & Audit**. Confirm the active `assign_ticket` version is `1.1.0`, and inspect the matching run's structured operations, evidence, validation checks, retry, independent verification and commit. Authorization must appear only as a configured credential reference or redacted summary.
6. Without resetting or changing mutation, return to **Demo** and run the same task again with **Run deterministic healing**. The second run should complete using the active `assign_ticket` 1.1.0 fingerprint. In **Registry & Audit**, confirm the reuse panel cites that tool-call version and reports no `diagnosis_started` event. This is observed registry reuse, not a frontend cache claim.
7. For the unsafe demonstration, click **Reset backend**, select **Missing business value**, **Explicit migration hint**, and deterministic healing. Apply and run. In the observed backend, the run failed safely without patch/retry/commit, and ticket 81 remained unassigned. **Do not call this an authoritative safe refusal:** the current final run lacks a `safe_refusal` marker. The audit shows `planning_failed` and the reason; the Demo result must read failed.

The **Run baseline** button uses the backend's model-driven baseline. Gemini completed a normal baseline in an isolated run; a later rename baseline observed the schema failure, then hit its former 30-second deadline. The default agent budget is now 90 seconds, but that exact baseline case has not been rerun after the change. The **Compare both** button is disabled in Live because the backend returns one run per command and has no paired-equivalence operation. A baseline card preserved across reset is a historical independent run, not a controlled comparison. Sample mode illustrates all scenarios but keeps **Sample data — backend not connected** visible; it must never be presented as live evidence.

## What to show on a demonstration laptop

- Keep the browser near 1366×768. Demo cards, recovery timeline, request diff and final verification are readable in the dark layout; scroll to show the complete timeline. At 390 px width the panels stack without observed clipping.
- Show the run ID, live label, failure and diagnosis, the candidate request, backend validation, retry, independent verification and commit. Raw JSON is supplementary.
- Then show **Registry & Audit** for active version, changed fields, credential reference, evidence and second-run reuse. Show **Benchmark** only to explain the available operational measures and clearly unavailable controlled benchmark fields. Do not quote sample results or missing token values as measured numbers.
- Browser screenshots from live checks were captured in the Phase 5 chat tool output. If Member 4 needs durable image files for slides, capture the running **Live** dashboard directly and include the run ID/source label in each crop. Label every Sample screenshot explicitly.

## Connection troubleshooting

| Symptom | Check |
| --- | --- |
| Registry/metrics connection error | Confirm both backend processes are listening and `GET http://127.0.0.1:8000/health` responds; check Vite `/api` proxy target and browser network panel. Do not switch silently to Sample to report a live result. |
| Browser `fetch` or CORS failure | Use the documented Vite proxy at `127.0.0.1:5173`, or configure an agreed backend CORS origin. `VITE_ORCHESTRATOR_URL` must be a backend base URL, never a secret. |
| SSE shows reconnecting/disconnected | Check `GET /runs/{run_id}/events` in browser network panel and the backend process. The frontend replays/deduplicates event sequences where supported and polls `GET /runs/{run_id}` for final status. A transport drop alone does not mark a business failure or unlock an active run. If replay remains incomplete, the timeline warns that events may be missing. |
| Controls remain locked after failed `POST /runs` | The request may have been accepted without returning a run ID. Confirm backend idle state outside the UI before reloading; a backend command identity/status lookup is still needed for automatic reconciliation. |
| Combined candidate rejected for credential | Configure the server-side assignment credential reference. The UI cannot provide or display its value. Do not weaken the validator or invent Authorization data. |
| Model-driven baseline fails before tools | Check `/health` provider, the server-side Gemini adapter/key and model name, then restart. A sanitized quota/rate-limit error means wait for quota recovery; the agent will not fall back to a static success. |

## Known limits

The backend does not publish a controlled paired comparison or evaluation result; adaptation rate, safe-refusal count, benchmark median/p95 and certain token scopes remain **Unavailable**. Ordinary evidence did not contain enough machine-readable migration detail for deterministic repair in the tested rename/combined runs; the exact deterministic demo therefore uses **Explicit migration hint**. The unsafe path stopped without a patch but lacks an explicit terminal safe-refusal classification. Live reconnection under an actual network interruption, a validated-but-uncommitted candidate, and a failed retry were not observed end to end; state tests cover the frontend presentation logic for those cases.
