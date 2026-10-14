# DriftGuard Evidence & Verification Checklist

Use this checklist to reproduce and verify all hackathon claims.

## 1. Automated Verification Checks
- [ ] Run backend unit and integration test suite:
  ```bash
  pytest -v
  ```
- [ ] Run frontend TypeScript type-check and production build:
  ```bash
  cd frontend && npm run build
  ```

## 2. API Contract Invariants
- [ ] Verify Mock API user lookup:
  ```bash
  curl http://127.0.0.1:8001/users?name=Maya
  # Expected: [{"id":"123","name":"Maya",...}]
  ```
- [ ] Verify Mock API assignment:
  ```bash
  curl -X POST http://127.0.0.1:8001/tickets/assign -H "Content-Type: application/json" -d "{\"ticket_id\":81,\"user_id\":\"123\"}"
  # Expected: {"ticket_id":81,"status":"assigned","assignee_id":"123",...}
  ```
- [ ] Verify independent read verification:
  ```bash
  curl http://127.0.0.1:8001/tickets/81
  # Expected: {"id":81,"status":"assigned","assignee_id":"123",...}
  ```
- [ ] Verify idempotency key replay:
  ```bash
  curl -X POST http://127.0.0.1:8001/tickets/assign -H "Content-Type: application/json" -H "Idempotency-Key: test-1" -d "{\"ticket_id\":81,\"user_id\":\"123\"}"
  curl -X POST http://127.0.0.1:8001/tickets/assign -H "Content-Type: application/json" -H "Idempotency-Key: test-1" -d "{\"ticket_id\":81,\"user_id\":\"123\"}"
  # Expected: idempotent_replayed: true on second call, ticket history has only 1 entry.
  ```

## 3. Orchestrator Endpoints
- [ ] Verify tool registry retrieval:
  ```bash
  curl http://127.0.0.1:8000/registry
  ```
- [ ] Verify metrics endpoint:
  ```bash
  curl http://127.0.0.1:8000/metrics
  # Expected: real counts, token_usage: "unavailable", no fake numbers
  ```
- [ ] Verify demo reset:
  ```bash
  curl -X POST http://127.0.0.1:8000/demo/reset
  ```
