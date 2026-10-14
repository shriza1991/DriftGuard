# Member 3 Ownership & Task Specification

## 1. Domain & Responsibilities
- **Domain**: Frontend Dashboard, UI Components, Real-Time SSE Event Viewer, Comparison Panels, Registry/Audit Views.
- **Directory Scope**:
  - `frontend/`
- **Suggested Branch**: `feat/demo-dashboard`

---

## 2. Dependencies
- **Inbound**: Consumes Orchestrator API (`http://127.0.0.1:8000`) and SSE stream (`/runs/{id}/events`).
- **Outbound**: Provides visual UI and screenshots for Member 4 (Deck) and live hackathon demo.

---

## 3. Next Tasks Checklist

- [ ] **Task 1: Complete Real-Time SSE Integration**:
  - Connect `EventSource` to `/runs/{run_id}/events` upon workflow execution.
  - Dynamically render event cards for all 12 lifecycle types (`run_started`, `tool_call_started`, `diagnosis_started`, `patch_proposed`, `recovery_verified`, etc.).

- [ ] **Task 2: Side-by-Side Comparison View**:
  - Provide comparative visual timeline: Baseline Run (Breakage) vs. DriftGuard Run (Autonomous Recovery).
  - Highlight difference in recovery time and outcome.

- [ ] **Task 3: Readable Contract Diffs**:
  - Render JSON diffs comparing Original Registry Schema vs. Healed Schema (e.g. `user_id` highlighted in red, `assignee_id` highlighted in green).

- [ ] **Task 4: Safety & Refusal States**:
  - Clearly display refusal badges for Scenarios 6, 7, and 8 ("Refusal to invent missing priority_tier").
  - Prevent conflicting runs with loading/disabled states during active execution.

- [ ] **Task 5: Reset & Clean State Controls**:
  - Hook Reset button to `POST /demo/reset`.
  - Provide feedback toast and refresh active tool registry and metrics.

---

## 4. Acceptance Criteria
1. UI renders cleanly with zero errors on modern browsers.
2. Live SSE events animate into the timeline without freezing or dropping events.
3. Disabled mutation controls clearly communicate pending backend implementation.
4. Production build (`npm run build`) compiles with zero TypeScript errors.
