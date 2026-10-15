# ContractGuard hardening: agent start here

This document is the entry point for any coding agent working on the DriftGuard repository after commit `1675ccc` (`benchmark changes`). The goal is to improve the existing solution without replacing its architecture, weakening its safety controls, or breaking the original ticket-assignment demonstration.

## Required reading order

Read these files completely before editing code:

1. `AGENTS.md`
2. `README.md`
3. `docs/audit-report.md`
4. `docs/benchmark-methodology.md`
5. `docs/00_AGENT_START_HERE.md`
6. `docs/REPOSITORY_HARDENING_PLAN.md`
7. `docs/BENCHMARK_ACCEPTANCE_CRITERIA.md`
8. `docs/IMPLEMENTATION_RUNBOOK.md`

Then inspect the implementation and tests named by the hardening plan. Treat source code and executable tests as stronger evidence than prose. If documentation disagrees with code, record the disagreement and fix the documentation only after verifying the intended behavior.

## Product and repository identity

- **DriftGuard** is the repository and demo application name.
- **ContractGuard** is the bounded contract-repair capability implemented and benchmarked by the repository.
- This prototype does **not** claim universal self-healing, guaranteed correctness, production durability, or autonomous credential discovery.

## Two workflows must coexist

| Workflow | Purpose | Must remain working |
|---|---|---|
| Ticket assignment: find Maya, assign ticket 81, verify assignee 123 | Original interactive demo and most legacy integration coverage | Yes |
| Notification preference: set `user_id="123"`, `promo=false` | Deterministic safety benchmark and presentation evidence | Yes |

Do not convert the whole application to the notification task. Do not remove or silently alter the ticket workflow. Notification tools may remain benchmark-only unless a deliberate, tested product decision makes them part of the live application.

## Non-negotiable invariants

1. Diagnosis receives only ordinary runtime evidence, the active contract, caller intent, and authorised configuration. It never reads mock admin state, scenario identifiers, hidden ground truth, or provider source code.
2. A model or deterministic planner may propose; deterministic code authorises.
3. A repair can transform representation, never business intent.
4. No generated code, expression, template, or provider text is executed.
5. Secrets are resolved server-side and never enter model prompts, browser payloads, raw benchmark rows, or audit logs.
6. A mutating incident gets at most one eligible candidate retry.
7. HTTP success alone never authorises a commit. The intended provider state must be independently verified.
8. Schema and HTTP adapter changes activate together under version checking.
9. Rejection, ambiguity, missing evidence, verification failure, or stale version leaves the active registry unchanged.
10. A safe stop is a typed, authoritative terminal outcome. It must never be inferred merely because a run failed or verification was false.
11. Benchmark fields must be measured or derived from recorded evidence. Do not hardcode a safety result to `true` or a token count to zero without traceable proof.
12. Generated benchmark files are evidence, not source. Do not tune implementation or tests merely to preserve attractive headline numbers.

## Current verified baseline at `1675ccc`

The following was independently checked on 2026-10-15:

- Frontend tests: **24 passed**.
- Frontend TypeScript/Vite production build: **passed**.
- Fresh one-repetition deterministic benchmark: **completed with 37 raw trials**.
- Full Python run with proxy variables disabled and development dependencies: **205 passed, 3 failed**.
  - One legacy combined-mutation integration test failed.
  - Two Groq construction tests failed when the `groq` optional dependency was not installed; `requirements.txt` and `pyproject.toml` currently describe dependencies differently.
- Docker was not available in the inspection environment, so no Docker build claim is verified.
- The repository does not contain a canonical `reports/` result set at this commit.

These checks are a starting point, not release evidence. Re-run them in the implementation environment.

## Definition of done

Work is complete only when all of the following are true:

- the hardening plan's P0 items are resolved;
- all existing and new Python tests pass from a clean environment;
- frontend tests, typecheck, and build pass;
- Docker images and Compose startup are verified, or a real environmental blocker is documented without claiming success;
- the full 30-repetition benchmark finishes and passes artifact validation;
- raw artifacts, aggregates, and Markdown agree;
- no safe stop is inferred from generic failure;
- no benchmark safety field is hardcoded without evidence;
- no secret appears in reports or logs;
- the original ticket demo and notification benchmark both pass;
- only validated, reproducible, independently verified values are marked PPT-safe.

