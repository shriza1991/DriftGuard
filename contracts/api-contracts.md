# DriftGuard Contract Specification

This document freezes the API and Event contracts between **Mock API**, **Orchestrator**, **Frontend**, and **Evaluation Harness**.

---

## 1. Mock API Contract (`http://localhost:8001`)

### 1.1 Business Endpoints

#### `GET /users`
Search users by query parameters.
- **Query Params**: `name` (string, e.g. `Maya`)
- **Success Response (200 OK)**:
```json
[
  {
    "id": "123",
    "name": "Maya",
    "email": "maya@example.com",
    "department": "Engineering"
  }
]
```

#### `POST /tickets/assign`
Assign an open ticket to a user.
- **Headers**:
  - `Content-Type: application/json`
  - `Idempotency-Key` (optional string): Client-provided UUID to prevent duplicate assignments.
  - `Authorization` (required only under `required_header` or `combined` mutation scenario).
- **V1 Request Body**:
```json
{
  "ticket_id": 81,
  "user_id": "123"
}
```
- **V2 Combined Future Request Body** (for testing complex schema drift):
```json
{
  "ticket_id": 81,
  "target": {
    "id": 123
  }
}
```
- **Success Response (200 OK)**:
```json
{
  "ticket_id": 81,
  "status": "assigned",
  "assignee_id": "123",
  "updated_at": "2026-10-14T12:00:00Z",
  "idempotent_replayed": false
}
```
- **Validation Failure Response (422 Unprocessable Entity)**:
```json
{
  "error": "ValidationFailed",
  "code": "SCHEMA_MISMATCH",
  "message": "Expected field 'target' object with nested integer 'id', received 'user_id' string",
  "details": {
    "expected_schema": {
      "ticket_id": "integer",
      "target": { "id": "integer" }
    },
    "received_payload": {
      "ticket_id": 81,
      "user_id": "123"
    }
  },
  "hint": "POST body migrated in v2: user_id is now nested under target.id as an integer."
}
```

#### `GET /tickets/{ticket_id}`
Independent read verification endpoint.
- **Success Response (200 OK)**:
```json
{
  "id": 81,
  "title": "System access drift alert",
  "status": "assigned",
  "assignee_id": "123",
  "history": [
    {
      "action": "assigned",
      "assignee_id": "123",
      "timestamp": "2026-10-14T12:00:00Z",
      "idempotency_key": "idemp-001"
    }
  ]
}
```

---

### 1.2 Admin Endpoints (Mock Only - Inaccessible to Healer Diagnosis)

#### `POST /admin/reset`
Resets the mock API to initial fixtures (ticket 81 unassigned, user Maya="123", active mutation=None).
- **Success Response (200 OK)**:
```json
{
  "status": "reset_successful",
  "timestamp": "2026-10-14T12:00:00Z"
}
```

#### `POST /admin/mutation`
Configures active drift mutation scenario on Mock API.
- **Request Body**:
```json
{
  "scenario": "rename | type_change | nesting | required_header | combined | missing_business_value | unavailable_credential | ambiguous_mapping",
  "enabled": true
}
```
- **Response (200 OK)**:
```json
{
  "active_scenario": "rename",
  "enabled": true,
  "description": "Field 'user_id' renamed to 'assignee_id'"
}
```

#### `GET /admin/state`
Returns internal mock state (users, tickets, mutation configuration).
> **CRITICAL RULE**: The orchestrator's healing diagnosis engine must **NEVER** query `/admin/state`. It is reserved strictly for evaluation runners, admin verification, and test assertions.

---

## 2. Orchestrator Contract (`http://localhost:8000`)

### 2.1 Workflow & Run Endpoints

#### `POST /runs`
Triggers an agent workflow run.
- **Request Body**:
```json
{
  "mode": "normal | healing",
  "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."
}
```
- **Response (202 Accepted)**:
```json
{
  "run_id": "run-a1b2c3d4",
  "status": "running",
  "mode": "normal",
  "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
  "created_at": "2026-10-14T12:00:00Z"
}
```

#### `GET /runs/{run_id}`
Retrieves summary status of a run.
- **Response (200 OK)**:
```json
{
  "run_id": "run-a1b2c3d4",
  "status": "completed | failed | running",
  "mode": "normal",
  "task": "Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee.",
  "created_at": "2026-10-14T12:00:00Z",
  "completed_at": "2026-10-14T12:00:02Z",
  "duration_ms": 210,
  "events_count": 8,
  "outcome": {
    "user_id": "123",
    "ticket_id": 81,
    "assigned": true,
    "verified_assignee": "123"
  }
}
```

#### `GET /runs/{run_id}/events`
Server-Sent Events (SSE) streaming endpoint.
- Content-Type: `text/event-stream`
- Replays all prior events in the run in sequence, followed by live events until `run_completed` or `run_failed`.

### 2.2 Registry & Patches

#### `GET /registry`
Returns the active tool registry entries.
- **Response (200 OK)**:
```json
{
  "tools": [
    {
      "name": "find_user",
      "version": "1.0.0",
      "fingerprint": "fp_find_user_v1",
      "description": "Finds user by name",
      "endpoint": "/users",
      "method": "GET",
      "argument_schema": {
        "type": "object",
        "properties": { "name": { "type": "string" } },
        "required": ["name"]
      }
    },
    {
      "name": "assign_ticket",
      "version": "1.0.0",
      "fingerprint": "fp_assign_ticket_v1",
      "description": "Assigns a ticket to a user",
      "endpoint": "/tickets/assign",
      "method": "POST",
      "argument_schema": {
        "type": "object",
        "properties": {
          "ticket_id": { "type": "integer" },
          "user_id": { "type": "string" }
        },
        "required": ["ticket_id", "user_id"]
      }
    },
    {
      "name": "get_ticket",
      "version": "1.0.0",
      "fingerprint": "fp_get_ticket_v1",
      "description": "Retrieves ticket details and assignee",
      "endpoint": "/tickets/{ticket_id}",
      "method": "GET",
      "argument_schema": {
        "type": "object",
        "properties": { "ticket_id": { "type": "integer" } },
        "required": ["ticket_id"]
      }
    }
  ]
}
```

#### `GET /patches`
Returns active and proposed patches in the registry. (Returns empty list in v1 scaffolding).

#### `POST /demo/reset`
Resets orchestrator run store and triggers mock API reset.

#### `POST /demo/mutation`
Disabled in current prototype; returns 501 / Pending Member 1 notice.

#### `GET /metrics`
Returns honest, non-fabricated metrics.
```json
{
  "total_runs": 1,
  "normal_runs": 1,
  "normal_success_count": 1,
  "healing_trials": 0,
  "verified_recoveries": 0,
  "unsafe_trials": 0,
  "average_recovery_time_ms": null,
  "token_usage": "unavailable",
  "status": "normal_scaffolding_active"
}
```
