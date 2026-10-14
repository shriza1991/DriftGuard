# DriftGuard Integration Guide

This document specifies the integration contracts and communication channels between all four components.

## 1. Port Allocation & Endpoints
| Component | Local Host / Port | Responsibilities |
|---|---|---|
| **Mock API** | `http://127.0.0.1:8001` | Simulated ticketing & user directory service; drift mutation injection |
| **Orchestrator** | `http://127.0.0.1:8000` | Tool registry, execution engine, SSE stream, healing pipeline, metrics |
| **Frontend** | `http://127.0.0.1:5173` | React/Vite dashboard, live timeline, comparison panels |

---

## 2. Communication Protocols

### 2.1 Frontend to Orchestrator
- **Workflow Run**: `POST http://127.0.0.1:8000/runs` with `{"mode": "normal", "task": "..."}`
- **SSE Stream**: `GET http://127.0.0.1:8000/runs/{run_id}/events`
- **Tool Registry**: `GET http://127.0.0.1:8000/registry`
- **Metrics**: `GET http://127.0.0.1:8000/metrics`
- **Reset**: `POST http://127.0.0.1:8000/demo/reset`

### 2.2 Orchestrator to Mock API
- **User Search**: `GET http://127.0.0.1:8001/users?name=Maya`
- **Ticket Assignment**: `POST http://127.0.0.1:8001/tickets/assign`
  - Body: `{"ticket_id": 81, "user_id": "123"}`
  - Header: `Idempotency-Key: driftguard-<uuid>`
- **Independent Read Verification**: `GET http://127.0.0.1:8001/tickets/81`

### 2.3 Evaluation Runner to Mock API (Admin Only)
- **Reset Fixtures**: `POST http://127.0.0.1:8001/admin/reset`
- **Inject Mutation**: `POST http://127.0.0.1:8001/admin/mutation`
- **Assert Ground Truth**: `GET http://127.0.0.1:8001/admin/state`
*(Note: Strictly forbidden for Orchestrator diagnosis engine!)*

---

## 3. Server-Sent Events (SSE) Contract
Each event emitted across `/runs/{run_id}/events` conforms to `contracts/events.schema.json`:
```json
{
  "run_id": "run-f19b22a0",
  "sequence": 1,
  "timestamp": "2026-10-14T12:00:00.100Z",
  "mode": "normal",
  "type": "run_started",
  "tool": null,
  "data": {
    "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
  }
}
```
All secrets in `data` are automatically redacted using keyword inspection before broadcasting.
