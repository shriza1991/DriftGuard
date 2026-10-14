# Member 2 repository review

Base: `b5ce6e13309c4a2004f873c6eefb59376a64ac12` on `main`.

## Verified findings and corrections

| Tasks | Finding | Correction |
| --- | --- | --- |
| 1 and 3 | Runtime credential resolver always returned None; credential placeholders in custom headers could be sent literally. | Local configured assignment credential reference; approved placeholder substitution. |
| 2 | Registry staging did not validate full JSON Schema; constraints could be dropped by model parsing. | Schema validation and preservation of extra JSON Schema constraints. |
| 3 and 4 | Agent tasks silently defaulted to Maya/ticket 81; unresolved or incorrect assignment intent could write. | Explicit supported task syntax and pre-write matching against directory evidence. |
| 4 | Audited baseline used undefined `turns_executed`; both model agents read nonexistent result duration fields. | Correct turn identifier and TimingInfo access. |
| 5 and 7 | Healing installed an implicit global pre-execution guarantee; language evidence used keyword co-occurrence. | No implicit guarantee; directional correspondence validation against original evidence. |
| 6 and 7 | Rename/nesting changed only HTTP mappings. | Coherent model-visible schema paths, required fields, argument transformation and mappings. |
| 8 | Final healing success accepted any assigned user; complete recovery/model calls were not deadline bounded. | Verify requested ticket and resolved assignee; bounded model, dispatch, verifier and recovery stages. |
| 8 | Recovery exhaustion identity omitted run/write identity; failed recovery could leave the model trying again. | Scoped operation identities, one-attempt tracking and terminal recovery behavior. |
| 9 | Lazy-created models did not initialize fallback diagnosis; usage metadata could be fabricated as zero. | Initialize bounded fallback; strict output models and incomplete-usage handling. |
| 10 | Token metadata was redacted; failed repair dispatches and early refusal outcomes were missing; completion could clear audit-incomplete state. | Preserve usage metadata, persist outcomes and maintain incomplete state. |
| 10 and 11 | Event writes were untracked and happened after live publication; internal authorization events collided with public sequences. | Serialized durable publication, flush/replay synchronization and a separate authorization table. |
| 11 | Mutation evidence levels did not match Member 1; default healing selected deterministic execution; patch endpoint remained a stub. | Actual evidence enum, genuine healing default and persistent patch history response. |
| 11 | Failed mutation could return the gate to idle; cancelled waiting HTTP requests could cancel managed runs. | Quarantine uncertain admin state; shield managed runs and release admission safely. |
| Setup | Editable installation failed on multiple top-level package discovery. | Explicit backend package discovery and provider installation extras. |

## Validation

Run `python -m pytest -q`. The review adds 14 regression cases, including the actual Member 1 ASGI API for all five positive mutation scenarios and subsequent repaired-contract reuse. It exercises a public combined run, SQLite history and SSE reconnect with a scripted model. Fixtures were updated where their old assertions contradicted the corrected contract or trusted-intent requirements; safety assertions were retained or strengthened.

No live Gemini/OpenAI call was made. These checks do not establish live-provider latency, adaptation-rate benchmarks or frontend visual quality. Member 1 owns benchmark aggregation and Member 3 owns dashboard implementation.

## Applying

Confirm your checkout is the base above and preserve local work. From the repository root:

```bash
git apply --check /path/to/DriftGuard_Member2_Review_b5ce6e1.patch
git apply /path/to/DriftGuard_Member2_Review_b5ce6e1.patch
python -m pytest -q
```

For a real-provider rehearsal install the selected adapter extra and configure its existing provider/name/key settings plus the approved assignment token. The current agent task grammar requires explicit `Find <name>` and `ticket <number>`; response nesting recovery remains stretch scope. Registry state remains in-memory after restart; persistent audit is not automatic replay authorization.
