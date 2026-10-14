# DriftGuard / ContractGuard

**Autonomous Self-Healing Tool Registry for AI Agents**  
*Genesis Hackathon PS01 Selection Prototype*

**Naming:** DriftGuard is the repository and demo application name. **ContractGuard** is the bounded contract-repair engine demonstrated and benchmarked here. Reports use ContractGuard for the product capability and DriftGuard when referring to the repository/application.

---

## 1. Problem Statement
Production AI agents break when downstream third-party APIs change without notice. Common issues such as renamed parameters (`user_id` &rarr; `assignee_id`), altered nesting (`{"target": {"id": ...}}`), or newly mandated headers cause sudden HTTP 400/422 errors. The agent fails mid-workflow, leading to stalled business processes and requiring developer intervention to rewrite code or prompt definitions.

**DriftGuard** acts as an adaptive, resilient tool registry layer. When a downstream endpoint drifts, DriftGuard:
1. Intercepts runtime failure evidence (status code, JSON validation details, migration hints).
2. Synthesizes a safe contract patch without accessing internal server state.
3. Enforces deterministic safety gates (no invented values, no lossy conversions).
4. Executes a single verified retry with an `Idempotency-Key`.
5. Independently confirms write execution (e.g. `GET /tickets/81`).
6. Hot-commits the verified patch to the active registry so subsequent agent calls succeed immediately.

---

## 2. Prototype Scope & Canonical Workflow

### Target Workflow
> *"Find Maya's user ID, assign ticket 81 to her, and confirm the updated assignee."*

### Tools Involved
1. `find_user`: Search for Maya (`GET /users?name=Maya`) &rarr; Resolves ID `"123"`.
2. `assign_ticket`: Assign ticket 81 to Maya (`POST /tickets/assign`) &rarr; V1 body: `{"ticket_id": 81, "user_id": "123"}`.
3. `get_ticket`: Independent verification read (`GET /tickets/81`) &rarr; Verifies `assignee_id == "123"`.

### Explicit Exclusions (Out of Scope for Hackathon Prototype)
- Multi-tenant enterprise RBAC / user authentication UI
- Complex multi-cloud infrastructure deployment
- Extended business tools outside `find_user`, `assign_ticket`, `get_ticket`

---

## 3. System Architecture & Port Mapping

```
[ Frontend Dashboard ] (React/Vite :5173)
         │  ▲
         │  │ SSE Event Stream (/runs/{id}/events)
         ▼  │
[ Orchestrator Service ] (FastAPI :8000)
    ├── Tool Registry (v1 frozen definitions)
    ├── Execution & SSE Event Broadcaster
    ├── Autonomous Healing Engine (Milestone 2 Pending)
    └── Metrics Tracker & Audit Trail
         │
         │ HTTP Tool Calls (find_user, assign_ticket, get_ticket)
         │ (STRICT BOUNDARY: No access to /admin endpoints!)
         ▼
[ Mock API Service ] (FastAPI :8001)
    ├── Business Endpoints (/users, /tickets/assign, /tickets/{id})
    └── Admin Endpoints (/admin/reset, /admin/mutation, /admin/state)
```

| Service | Port | Endpoint URL | Description |
|---|---|---|---|
| **Mock API** | `8001` | `http://127.0.0.1:8001` | Deterministic ticketing & user directory; configurable drift mutations |
| **Orchestrator** | `8000` | `http://127.0.0.1:8000` | Tool registry, execution engine, SSE stream, metrics, and healing stubs |
| **Frontend** | `5173` | `http://127.0.0.1:5173` | Modern dark-mode React dashboard with live timeline and registry views |

---

## 4. Ownership & Team Structure

| Member | Domain & Ownership | Key Directories | Suggested Branch |
|---|---|---|---|
| **Member 1** | Mock API, Runtime Mutations, Evaluation & Fair Benchmarking | `mock_api/`, `evaluation/`, `tests/mock_api/`, `tests/evaluation/` | `feat/mock-api-evaluation` |
| **Member 2** | Orchestrator, Registry, Tool Caller, Healing Pipeline, Audit, Shared Config | `orchestrator/`, `tests/healing/`, `contracts/` | `feat/agent-healing` |
| **Member 3** | Frontend Dashboard, Live SSE Viewer, Registry/Audit UI, Comparison Views | `frontend/` | `feat/demo-dashboard` |
| **Member 4** | Submission Deck, 30s Pitch Script, Screenshot Curation, Evidence Checklist | `submission/`, `docs/` | `docs/submission-deck` |

---

## 5. Repository Structure Map

```
DriftGuard/
├── .gitignore
├── .env.example
├── README.md
├── AGENTS.md
├── pyproject.toml
├── requirements.txt
├── contracts/
│   ├── api-contracts.md
│   ├── events.schema.json
│   └── examples/
│       ├── run_events_normal.json
│       ├── assign_v1_request.json
│       ├── assign_v1_response.json
│       └── assign_v2_future_request.json
├── docs/
│   ├── architecture.md
│   ├── INTEGRATION.md
│   ├── DECISIONS.md
│   ├── demo-script.md
│   ├── member_1.md
│   ├── member_2.md
│   ├── member_3.md
│   └── member_4.md
├── mock_api/
│   ├── app.py
│   ├── config.py
│   ├── business/
│   │   ├── models.py
│   │   └── router.py
│   ├── mutations/
│   │   ├── scenarios.py
│   │   └── manager.py
│   └── fixtures/
│       └── initial_data.py
├── orchestrator/
│   ├── app.py
│   ├── config.py
│   ├── api/
│   │   └── routes.py
│   ├── registry/
│   │   ├── models.py
│   │   └── default_tools.py
│   ├── execution/
│   │   ├── workflow.py
│   │   ├── tool_caller.py
│   │   └── events.py
│   ├── agents/
│   │   └── stub_agent.py
│   ├── healing/
│   │   └── stubs.py
│   ├── audit/
│   │   └── logger.py
│   └── metrics/
│       └── tracker.py
├── evaluation/
│   ├── scenarios.py
│   ├── runner.py
│   └── reporting.py
├── tests/
│   ├── mock_api/
│   │   └── test_mock_api.py
│   ├── evaluation/
│   │   └── test_scenarios.py
│   └── healing/
│       └── test_stubs.py
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── App.tsx
│       ├── main.tsx
│       ├── index.css
│       ├── types/
│       ├── components/
│       └── services/
└── submission/
    ├── slide_deck_outline.md
    ├── pitch_script_30s.md
    ├── screenshot_guide.md
    └── evidence_checklist.md
```

---

## 6. Implemented vs. Pending Features

| Feature Area | Status | Implementation Details |
|---|---|---|
| **Deterministic v1 Mock** | **Implemented** | Maya (`"123"`), Ticket `81`, pre-write validation, idempotency cache, independent read verification (`/tickets/81`), reset endpoint. |
| **Tool Registry (v1)** | **Implemented** | 3 tools (`find_user`, `assign_ticket`, `get_ticket`) with JSON argument schemas, endpoints, methods, and SHA-256 fingerprints. |
| **Normal Workflow Execution** | **Implemented** | End-to-end execution of Find Maya &rarr; Assign 81 &rarr; Verify Assignee. |
| **Event Stream (SSE)** | **Implemented** | Real-time broadcasting of 12 lifecycle event types with schema validation and secret redaction. |
| **Dashboard Shell** | **Implemented** | React/TypeScript/Vite dark mode interface, live timeline, metrics tracking, registry viewer, and transparent pending states. |
| **Automated Tests** | **Implemented** | 11/11 passing tests covering mock API, validation without side effects, idempotency, event schema, and reset. |
| **Runtime Mutation Engine** | **Implemented** | Recoverable, refusal, non-drift, adversarial, verification-mismatch, and stale-version scenarios. |
| **Autonomous Healer** | **Implemented (bounded prototype)** | Failure classifier, evidence planner, optional LLM fallback, deterministic validator, one retry, independent verification, and version-checked in-memory commit. |
| **Benchmark Runner** | **Implemented** | Seeded baseline-versus-ContractGuard runner with raw JSON/CSV and measured Markdown/JSON summaries. |
| **Slide Deck & Pitch** | *Pending (M3 - Member 4)* | Complete outline and script documented in `submission/`. |

---

## 7. Setup & Local Startup Instructions

### Prerequisites
- Python 3.11+
- Node.js v18+ & npm

### Setup Environment
```bash
# Clone and enter repo
cd DriftGuard

# Copy environment config
cp .env.example .env

# Install Python dependencies
pip install -r requirements.txt

# Install Frontend dependencies
cd frontend && npm install && cd ..
```

### Running the Services

Open three terminal windows:

**Terminal 1: Start Mock API Service (Port 8001)**
```bash
python -m uvicorn mock_api.app:app --host 127.0.0.1 --port 8001 --reload
```

**Terminal 2: Start Orchestrator Service (Port 8000)**
```bash
python -m uvicorn orchestrator.app:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 3: Start Frontend Dashboard (Port 5173)**
```bash
cd frontend
npm run dev
```

Open `http://localhost:5173` in your browser.

### Reproducible safety benchmark

The benchmark uses the fixed task **“Disable promotional notifications for user 123.”** with immutable `promo=false` intent. It runs 30 repetitions per deterministic scenario by default and never imports presentation values:

```bash
python -m evaluation.hardening_benchmark --repetitions 30 --seed 20261015 --output reports
```

Outputs are written to `reports/benchmark-results.json`, `reports/benchmark-results.csv`, `reports/benchmark-summary.md`, and `reports/benchmark-summary.json`. LLM fallback is not configured by this command; model-dependent results remain explicitly unclaimed.

### Validation commands

```bash
python -m pytest -q --basetemp=.pytest-tmp
cd frontend
npm test
npm run build
```

Docker Compose is provided for build verification and local startup:

```bash
docker compose build
docker compose up
```

The active registry is intentionally process-local in this prototype. Verified repairs are reused by later matching calls in the same process but are not restored after restart.

---

## 8. Development Milestones

1. **Milestone 1 (Current)**:
   - Shared foundation, frozen contracts, deterministic v1 mock, normal workflow execution, live SSE dashboard, and automated test suite.
2. **Milestone 2**:
   - Member 1 completes runtime mutation scenarios.
   - Member 2 completes failure classification, diagnosis, safety gate, single retry, and hot-commit.
   - Member 3 integrates contract diffs and refusal badges.
3. **Milestone 3**:
   - Member 1 runs comparative benchmark evaluations.
   - Member 4 finalizes presentation deck and recorded pitch.
