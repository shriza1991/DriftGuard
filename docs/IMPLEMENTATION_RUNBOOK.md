# ContractGuard implementation runbook

## 1. Protect the current state

From the repository root:

```bash
git status --short
git rev-parse HEAD
git log -1 --oneline
```

Work on a new branch from the intended current commit. Do not reset, rewrite, or discard unrelated user changes. Do not push, merge, rename the remote repository, or deploy without explicit instruction.

Create a short pre-change note containing:

- base commit;
- dirty files that already existed;
- environment versions;
- commands available in the environment;
- blockers such as missing Docker.

## 2. Establish the executable baseline

Use the repository's chosen clean-install method. Reconcile `requirements.txt`, `pyproject.toml`, and `uv.lock` before claiming reproducibility.

Suggested checks after dependency reconciliation:

```bash
python -m pytest -q --basetemp=.pytest-tmp
cd frontend
npm ci
npm test
npm run typecheck
npm run build
cd ..
```

If ambient proxy variables affect localhost `httpx` construction, fix test/runtime isolation in code or documented configuration; do not rely on manually clearing secrets or global user settings.

Record every failure before editing. Classify it as product regression, test defect, dependency/configuration defect, or environment blocker.

## 3. Implement in narrow phases

### Phase A: benchmark oracle

1. Add typed terminal outcomes.
2. Propagate reason codes and terminal evidence.
3. Replace inferred/hardcoded fields with evidence-derived values.
4. Correct timing boundaries.
5. Add artifact schema and validator.
6. Add negative tests that prove false positives are rejected.

Run focused tests after each step, then the full Python suite.

### Phase B: regression and dependency repair

1. Fix the legacy combined-mutation regression without removing the ticket workflow.
2. Align dependency declarations and clean-install instructions.
3. Preserve optional model providers while ensuring the documented full-test environment contains required test dependencies.

### Phase C: container integration

1. Make clean-checkout images independent of generated reports.
2. Add production frontend API/SSE proxy configuration.
3. Add health checks and `.dockerignore` files.
4. Verify unavailable benchmark behavior before reports exist.
5. Verify measured behavior only with validated reports.

### Phase D: documentation and cleanup

1. Reconcile README, AGENTS, architecture, integration, and submission claims.
2. Remove committed smoke-output directories and ignore regenerated temporary output.
3. Keep the historical audit but label its source commit and status.
4. Document exact limitations and naming.

## 4. Test requirements

Add tests for at least these cases:

- normal request never enters repair;
- every recoverable scenario produces the expected declarative operation set;
- every unsafe scenario produces a specific authoritative stop reason;
- auth, permission, rate limit, and outage never enter repair;
- malformed provider text cannot create an operation;
- business intent remains unchanged while wire representation changes;
- no more than one candidate retry;
- verification mismatch never commits;
- stale candidate never overwrites;
- candidate staging does not mutate active registry;
- schema and adapter activate together;
- warm reuse performs no diagnosis or planning;
- invalid raw trial fails artifact validation;
- aggregate formulas exclude benchmark errors and generic failures;
- frontend rejects malformed or incomplete benchmark summaries;
- both original ticket and notification workflows remain functional.

Do not write tests that merely assert `100%`. Test the numerator, denominator, membership, exclusions, and raw evidence.

## 5. Benchmark sequence

Do not begin with the full benchmark.

1. Run unit and integration tests.
2. Run a one-repetition smoke benchmark into a temporary ignored directory.
3. Run the artifact validator and inspect representative raw rows.
4. Run a small repeated benchmark to check stability and cleanup.
5. Only when all gates pass, run the 30-repetition release benchmark into `reports/`.
6. Re-run validation and secret scan on the final directory.

Recommended release command, unless the implemented CLI deliberately changes:

```bash
python -m evaluation.hardening_benchmark \
  --repetitions 30 \
  --seed 20261015 \
  --output reports
```

The command must return non-zero if the run or artifact validation is invalid.

## 6. Docker verification

When Docker is available:

```bash
docker compose build --no-cache
docker compose up -d
docker compose ps
```

Verify from the browser-facing container path:

- frontend loads;
- `/api` reaches the orchestrator;
- SSE remains streaming through the proxy;
- orchestrator reaches mock API by service name;
- health checks become healthy;
- benchmark cards show unavailable when no validated report exists;
- validated reports appear as measured;
- reset, normal ticket demo, recoverable demo, and safe-stop demo work.

Then stop the stack using the normal non-destructive Compose command. Do not delete user volumes unless explicitly requested.

## 7. Final review and handoff

Produce a final engineering report with:

- base and final commit IDs;
- files changed and why;
- audit findings resolved/deferred;
- tests and exact commands;
- benchmark command and artifact validation result;
- scenario/trial counts;
- measured results with sample sizes;
- baseline definition;
- registry safety counts;
- limitations;
- PPT-safe claims;
- not-safe-to-claim list;
- blockers that remain.

Never say “all tests pass,” “Docker works,” or “benchmark validated” unless the corresponding command completed successfully in the current final state.

