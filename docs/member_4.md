# Member 4 Ownership & Task Specification

## 1. Domain & Responsibilities
- **Domain**: Hackathon Submission, Slide Deck (~8 Slides), Pitch Script (30s elevator + 3min presentation), Screenshot Curation, Evidence Checklist.
- **Directory Scope**:
  - `submission/`
  - `docs/`
- **Suggested Branch**: `docs/submission-deck`

---

## 2. Dependencies
- **Inbound**: Consumes demo UI from Member 3, evaluation metrics from Member 1, and architecture diagrams from Member 2.
- **Outbound**: Hackathon submission assets for judges and pitch presentation.

---

## 3. Next Tasks Checklist

- [ ] **Task 1: Slide Deck Presentation (8 Slides)**:
  - Slide 1: Problem & Value Proposition (The silent killer of production agents: API drift).
  - Slide 2: Real-World Drift Example (Maya ticket assignment breaking on renaming/nesting).
  - Slide 3: DriftGuard Architecture & Security Boundary (Zero out-of-band cheating).
  - Slide 4: Autonomous Diagnosis & Safe Repair Planning (Model proposes, deterministic validator authorizes).
  - Slide 5: Strict Safety & Refusal Guarantees (Refusing to invent values/credentials).
  - Slide 6: Actual System Screenshots (Live UI, Event Timeline, Hot-Committed Registry).
  - Slide 7: Measured Comparison Results (Recovery latency, trials, honest token stats).
  - Slide 8: Limitations, Roadmap & Future Work (Multi-tenancy, OpenAPI 3.1 streaming).

- [ ] **Task 2: 30-Second Elevator Pitch Script**:
  - Crisp, punchy hook explaining why agent brittle-tool syndrome costs millions and how DriftGuard fixes it at runtime.

- [ ] **Task 3: Screenshot Collection & Curation**:
  - High-res screenshots of dashboard normal workflow, pending mutation states, and tool registry.

- [ ] **Task 4: Evidence & Verifiable Audit Checklist**:
  - Document reproducible terminal commands and test steps for judges.

---

## 4. Acceptance Criteria
1. Slide deck matches Genesis Hackathon PS01 evaluation rubrics.
2. Pitch script can be delivered comfortably within 30 seconds.
3. Screenshots depict real, non-fabricated system states.
