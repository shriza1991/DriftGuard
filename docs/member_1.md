# Member 1 Ownership & Task Specification

## 1. Domain & Responsibilities
- **Domain**: Mock API, Runtime Drift Mutations, Evaluation Scenarios & Benchmark Runner.
- **Directory Scope**:
  - `mock_api/{business,mutations,fixtures}`
  - `evaluation/{scenarios,runner.py,reporting.py}`
  - `tests/{mock_api,evaluation}`
- **Suggested Branch**: `feat/mock-api-evaluation`

---

## 2. Dependencies
- **Inbound**: Contracts defined in `contracts/api-contracts.md` and `contracts/events.schema.json`.
- **Outbound**: Supplies downstream API responses to Member 2 (Orchestrator) and benchmark results to Member 3 (Frontend) & Member 4 (Deck).

---

## 3. Next Tasks Checklist

- [ ] **Task 1: Complete Runtime Mutation Handlers (`mock_api/mutations/`)**:
  - Implement full runtime mutation interception in `POST /tickets/assign` for all 8 scenarios:
    1. `rename`: Expect `assignee_id` instead of `user_id`.
    2. `type_change`: Expect integer `123` instead of string `"123"`.
    3. `nesting`: Expect nested `target: { id: "123" }`.
    4. `required_header`: Enforce `Authorization: Bearer <token>`.
    5. `combined`: Enforce nested integer `target: { id: 123 }` and `Authorization` header.
    6. `missing_business_value`: Demand unmentioned `priority_tier` field.
    7. `unavailable_credential`: Require missing role `secops-admin`.
    8. `ambiguous_mapping`: Present ambiguous fields (`assignee_user_id` vs `escalation_user_id`).
  
- [ ] **Task 2: Evidence Level Toggle (`ordinary` vs `explicit_hint`)**:
  - Ensure ordinary errors emit standard 422 JSON schemas.
  - Ensure explicit hint mode injects deprecation/migration hints in headers or body.

- [ ] **Task 3: Implement Fair Benchmark Runner (`evaluation/runner.py`)**:
  - Build automated comparative trial execution:
    - Baseline Agent (fixed v1 schema, normal retry) vs. DriftGuard (diagnosis + patch + retry).
  - Ensure identical model, identical prompt task, identical reset initial state.
  - Never loop infinitely; cap retries at 1.

- [ ] **Task 4: Independent Outcome Checks**:
  - Verify state after each trial via `GET /tickets/81`.
  - Check that ticket history records genuine assignment.

---

## 4. Acceptance Criteria
1. All 8 mutation scenarios can be enabled via `POST /admin/mutation`.
2. Negative scenarios (6, 7, 8) trigger refusal without state alteration.
3. Tests in `tests/mock_api/` and `tests/evaluation/` pass with 100% determinism.
