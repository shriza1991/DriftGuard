# DriftGuard Screenshot & Visual Evidence Guide

This guide specifies the exact screenshots required for the hackathon submission deck and README evidence.

## Required Screenshots

1. **Dashboard Overview (Default State)**:
   - URL: `http://localhost:5173`
   - Content: Show the Header badges (Mock API connected, Orchestrator connected), Control panel with "Normal Baseline" selected, empty event timeline with placeholder, and Tool Registry with 3 v1 tools displayed.

2. **Normal Workflow Execution (Completed)**:
   - Action: Click "Execute Workflow" under Normal Baseline mode.
   - Content: Full SSE Timeline showing 5 events in sequence (`run_started`, `tool_call_started (find_user)`, `tool_call_started (assign_ticket)`, `tool_call_started (get_ticket)`, `run_completed`). Outcome summary showing `ticket_id: 81`, `assignee_id: "123"`, `verified: true`.

3. **Tool Registry Inspection Card**:
   - Content: Focus view of the `assign_ticket` tool card showing the JSON argument schema, endpoint `/tickets/assign`, method `POST`, and cryptographic fingerprint (`fp_assign_ticket_v1`).

4. **Transparent Pending Features Card**:
   - Content: Control panel showing disabled Mutation selector with badge "Pending Member 1 Implementation", and Metrics card showing honest counts (`Total Runs: 1`, `Normal Success: 1`, `Healing Trials: 0`, `Token Usage: unavailable`).
