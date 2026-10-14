# ContractGuard benchmark methodology

## Fixed business task

Every trial uses the same intent: **“Disable promotional notifications for user 123.”** The evaluator asserts that `user_id="123"` and `promo=false` are preserved. It never substitutes a different user, operation, or business value.

## Scenario generation

`evaluation.hardening_benchmark` runs a fixed, seeded order of scenarios. Recoverable drift covers field rename, lossless string-to-integer conversion of the user ID, request nesting, configured header insertion, and a supported combination. Safety cases cover missing business context, unavailable credentials, ambiguity, authentication, permission, rate limiting, provider outage, adversarial error content, verification mismatch, and a stale registry version. A no-drift request proves that repair is not entered on the normal path.

The mock provider owns mutation configuration and ground truth. The healing path receives only the active registry contract, the original caller arguments, and the ordinary HTTP response. It has no admin client and never reads `/admin/state` or `/admin/mutation`.

## Execution modes

- **Cold registry / rules-first:** starts from v1, observes one failure, creates a bounded declarative patch, validates it deterministically, performs at most one candidate retry, independently reads provider state, then commits the entire tool definition.
- **Warm verified registry:** reuses the committed schema and HTTP adapter on the next matching call. It performs no diagnosis or patch planning.
- **Baseline agent:** uses the frozen stale v1 contract with the same provider, intent, and two-attempt budget. It never receives ContractGuard repair logic.
- **LLM fallback:** not configured in the reproducible local benchmark. Its metrics remain unclaimed.

## Held-out and adversarial evidence

The evaluator configures scenarios by identifier, but the classifier, planner, and validator do not receive that identifier. They operate on structured public failures. Malformed/adversarial text deliberately lacks sufficient correspondence evidence and must stop safely.

## Independent evaluation

The evaluator reads `/admin/state` only after execution to count provider effects. Product verification uses the ordinary read-only `GET /notifications/preferences/{user_id}` endpoint and compares it with the original caller intent. HTTP success alone is never counted as verified correctness.

## Metrics

- **Recoverable-case adaptation rate:** verified and committed recoveries / recoverable cold trials.
- **All-breaking-case resolution rate:** verified recoveries plus authoritative safe stops / all non-normal cold trials.
- **Safe-stop rate:** safe stops / unsafe, non-drift, adversarial, verification-mismatch, and stale-version trials.
- **Verified task correctness:** trials whose intended provider state was independently confirmed / verified successful trials.
- **MTTR:** elapsed local monotonic time from the first stale-contract dispatch through verified recovery. Safe-stop latency is reported separately.
- **Retry efficiency:** repaired candidate retries only; baseline repetitions are separately labeled.
- **Registry safety:** incorrect or unverified commits, stale overwrites, failed-verification commits, and active changes during ordinary candidate evaluation.
- **Reuse benefit:** cold versus warm latency plus diagnosis and planning call reductions.
- **Token overhead:** measured provider-reported tokens. The rules-first suite makes no model calls, so its model token overhead is measured as zero. LLM fallback tokens are unavailable, not estimated.

A safe stop is never counted as adaptation success. A failed verification cannot authorize a commit.

## Reproducibility controls

The default run uses 30 repetitions per deterministic scenario and seed `20261015`. Outputs include the commit hash, UTC timestamp, interpreter/platform, scenario list, every raw trial, aggregates, failures, and limitations. The order is deterministically shuffled per repetition. Run:

```powershell
python -m evaluation.hardening_benchmark --repetitions 30 --seed 20261015 --output reports
```

## Known limitations

- Latency is in-process ASGI latency, not production network latency.
- Registry activation is in-memory and does not survive an orchestrator restart.
- The deterministic baseline is bounded to two attempts and is not an open-ended general-purpose model agent.
- LLM fallback is not configured or measured by this suite.
- The benchmark demonstrates a bounded allowlist of repair operations; it does not claim universal self-healing or guaranteed correctness.
