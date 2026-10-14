import test from 'node:test';
import assert from 'node:assert/strict';
import { FIXTURES, fixtureEvents } from '../src/fixtures/scenarios.ts';
import { candidateState, eventSummary, mergeEvents, runOutcome, safeDisplay } from '../src/state/runState.ts';

test('backend events have readable evidence-based summaries without raw JSON', () => {
  const base = { run_id: 'run-1', sequence: 1, timestamp: '2026-10-14T00:00:00Z', mode: 'healing' as const };
  assert.equal(eventSummary({ ...base, type: 'tool_call_started', tool: 'assign_ticket', data: { arguments: { ticket_id: 81, user_id: '123' } } }), 'Assigning ticket 81 using the active tool definition.');
  assert.equal(eventSummary({ ...base, type: 'patch_validated', tool: 'assign_ticket', data: {} }), 'Candidate passed deterministic validation; it is not committed yet.');
  assert.equal(eventSummary({ ...base, type: 'patch_rejected', tool: 'assign_ticket', data: { rejection_reasons: ['Missing configured credential'] } }), 'Missing configured credential');
  assert.equal(eventSummary({ ...base, type: 'run_failed', tool: null, data: { reason: 'Could not safely repair' } }), 'Could not safely repair');
});

test('all four documented fixture scenarios have separate ordered run identities', () => {
  for (const scenario of ['none', 'rename', 'combined', 'missing_business_value'] as const) {
    assert.ok(FIXTURES[scenario]);
    const baseline = fixtureEvents(scenario, 'baseline');
    const healing = fixtureEvents(scenario, 'healing');
    assert.notEqual(baseline[0].run_id, healing[0].run_id);
    assert.deepEqual(baseline.map((event) => event.sequence), baseline.map((_, index) => index + 1));
    assert.deepEqual(healing.map((event) => event.sequence), healing.map((_, index) => index + 1));
    assert.equal(baseline[0].type, 'run_started');
    assert.equal(healing[0].type, 'run_started');
  }
});

test('a tool failure can be followed by recovery; validation does not commit', () => {
  for (const scenario of ['rename', 'combined'] as const) {
    const events = fixtureEvents(scenario, 'healing');
    const failed = events.findIndex((event) => event.type === 'tool_call_failed');
    const validated = events.findIndex((event) => event.type === 'patch_validated');
    const verified = events.findIndex((event) => event.type === 'recovery_verified');
    const committed = events.findIndex((event) => event.type === 'registry_committed');
    assert.ok(failed > 0 && failed < validated && validated < verified && verified < committed);
    assert.equal(runOutcome(events.slice(0, failed + 1)), 'running');
    assert.equal(candidateState(events.slice(0, validated + 1)), 'validated');
    assert.equal(candidateState(events.slice(0, committed)), 'validated');
    assert.equal(candidateState(events.slice(0, committed + 1)), 'committed');
    assert.equal(runOutcome(events), 'verified');
    assert.equal(runOutcome(fixtureEvents(scenario, 'baseline')), 'failed');
  }
});

test('normal success needs no patch; refusal is distinct from completion', () => {
  const normal = fixtureEvents('none', 'healing');
  assert.equal(runOutcome(normal), 'verified');
  assert.equal(candidateState(normal), 'none');
  const refusal = fixtureEvents('missing_business_value', 'healing');
  assert.equal(runOutcome(refusal), 'safe_refusal');
  assert.equal(candidateState(refusal), 'rejected');
  assert.ok(!refusal.some((event) => event.type === 'registry_committed' || event.type === 'run_completed'));
});

test('empty and unverified completion states never claim verified success', () => {
  assert.equal(runOutcome([]), 'idle');
  const events = fixtureEvents('none', 'baseline');
  assert.equal(runOutcome(events.slice(0, -1)), 'running');
  const terminal = { ...events[events.length - 1], data: { outcome: { ticket_id: 81 } } };
  assert.equal(runOutcome([...events.slice(0, -1), terminal]), 'completed_unverified');
});

test('replay merge sorts and deduplicates by run and sequence', () => {
  const events = fixtureEvents('rename', 'healing');
  const merged = mergeEvents([events[2], events[0]], [events[1], events[2]]);
  assert.deepEqual(merged.map((event) => event.sequence), [1, 2, 3]);
});

test('display projection removes credential values', () => {
  assert.deepEqual(safeDisplay({ Authorization: 'secret', nested: { credential_reference: 'vault-key' }, total_tokens: 7 }), {
    Authorization: '[redacted]', nested: { credential_reference: '[redacted]' }, total_tokens: 7,
  });
});
