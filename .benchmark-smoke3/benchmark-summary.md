# ContractGuard benchmark summary

## Executive summary

Measured deterministic results for **Disable promotional notifications for user 123.** with immutable intent `user_id="123"` and `promo=false`. Safe stops are excluded from adaptation success.

## Measured results

| Metric | Measured value |
|---|---:|
| Recoverable-case adaptation rate | 100.0% |
| All-breaking-case resolution rate | 100.0% |
| Safe-stop rate | 100.0% |
| Verified task correctness | 100.0% |
| Median MTTR | 62.114 ms |
| P95 MTTR | None ms |
| Maximum repaired retries | 1 |
| Unsafe executions | 0 |

## Baseline comparison

| System | Recoverable success | Behaviour |
|---|---:|---|
| Frozen-contract baseline | 0.0% | Repeated invalid requests: 5 |
| ContractGuard rules-first | 100.0% | One bounded candidate retry, independent verification, then atomic commit |

## Recoverable versus safe-stop results

Recoverable mutations are rename, lossless string-to-integer conversion, nesting, configured header insertion, and their supported combination. Missing business value, unavailable credential, ambiguity, authentication, permission, rate limit, provider outage, adversarial evidence, verification mismatch, and stale-version conflict stop without being counted as adaptations.

## Latency summary

- Cold median: 14.475 ms
- Warm median: 19.218 ms
- Verified recovery median: 62.114 ms
- Verified recovery p95: None ms

## Token summary

Rules-first deterministic benchmark made no model calls; measured model token overhead is zero. LLM fallback was not configured.

## Registry safety summary

- Incorrect commits: 0
- Commits without verification: 0
- Stale-version overwrites: 0
- Failed-verification commits: 0
- Active registry changes during ordinary candidate evaluation: 0

## Second-call reuse

- Verified warm reuse: 100.0%
- Warm-call diagnosis reduction: 100.0%
- Warm-call patch-planning reduction: 100.0%

## Known limitations

- LLM fallback was not configured or measured; all repair decisions were rules-first and deterministic.
- Registry activation is process-local and is not durable across orchestrator restarts.
- Latency is local in-process ASGI latency and must not be presented as network production latency.
- The baseline is a deterministic frozen-contract agent with a two-attempt budget; it does not receive ContractGuard repair logic.

## Reproduce

```powershell
python -m evaluation.hardening_benchmark --repetitions 1 --seed 20261015 --output reports
```
