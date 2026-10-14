# DriftGuard Demo Script (Hackathon PS01 Presentation)

## Demo Narrative Arc
- **Target Workflow**: "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
- **Current Milestone**: Foundation & Normal Workflow Scaffolding.

---

## Step-by-Step Live Walkthrough

### 1. System Health & Architecture Overview (0:00 - 0:45)
- Open the DriftGuard Dashboard at `http://localhost:5173`.
- Point out the active service badges:
  - `Mock API Service: Connected (127.0.0.1:8001)`
  - `Orchestrator Engine: Connected (127.0.0.1:8000)`
- Explain the frozen v1 registry view:
  - Tools: `find_user`, `assign_ticket`, `get_ticket` with schemas and cryptographic fingerprints.

### 2. Trigger Normal Workflow Execution (0:45 - 1:30)
- In the Control Panel, ensure Mode is set to **Normal Baseline**.
- Click **"Execute Workflow"**.
- Watch the live SSE Event Stream populate in real time:
  1. `run_started`: Task initialized with input prompt.
  2. `tool_call_started (find_user)`: Query `name="Maya"` -> Resolved `id="123"`.
  3. `tool_call_started (assign_ticket)`: Payload `{"ticket_id": 81, "user_id": "123"}` -> HTTP 200 Assigned.
  4. `tool_call_started (get_ticket)`: Independent verification `GET /tickets/81` -> Confirmed `assignee_id="123"`.
  5. `run_completed`: Summary card showing 100% verified normal execution.

### 3. Transparently Highlight Pending Autonomous Healing (1:30 - 2:15)
- Toggle the mode selector to **Autonomous Healing (Pending)**.
- Note the disabled mutation controls:
  - Tooltip: *"Pending Member 1 implementation (Milestone 2)"*.
- Note the contract diff view:
  - *"No active patches. Normal v1 contracts in effect."*
- Note the metrics card:
  - Total Runs: `1`, Normal Success: `1`, Healing Trials: `0`, Token Usage: `unavailable`.
- Explain the upcoming Milestones:
  - Milestone 2: Failure classification, LLM-assisted repair planning, and single verified retry hot-commits.
  - Milestone 3: 8-scenario benchmark evaluation and safe refusal proofs.
