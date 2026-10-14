import test from 'node:test';
import assert from 'node:assert/strict';
import { activeVersionMatches, currentRunPatch, operationSummary, parsePatchPage, patchOperations, requestPair, reuseEvidence } from '../src/state/patchState.ts';
import { safeDisplay } from '../src/state/runState.ts';
import { DriftGuardApiClient } from '../src/services/api.ts';
import type { RepairAttempt, RunEvent, ToolRegistryEntry } from '../src/types/index.ts';

const active = (version: string): ToolRegistryEntry => ({
  name: 'assign_ticket', version, fingerprint: version === '1.1.0' ? 'candidate-fp' : 'base-fp',
  description: 'Assignment', endpoint: '/tickets/assign', method: 'POST', headers: {},
  argument_schema: { type: 'object', properties: {}, required: [] }, request_mapping: {}, response_mapping: {},
  credential_reference: null, status: 'active',
});
const operation = (operation_type: string, source_path: string | null, target_path: string | null, conversion_policy: string | null = null) => ({
  operation_type, source_path, target_path, conversion_policy, credential_reference: operation_type === 'set_header' ? 'vault-ref' : null,
  value_provenance: operation_type === 'set_header' ? 'approved_credential_reference:vault-ref' : 'caller_arguments',
  evidence_path: 'details.expected_schema', evidence_summary: operation_type === 'set_header' ? "Authorization required via credential reference 'vault-ref'" : 'Observed schema mismatch',
});
const patch = (changes: Partial<RepairAttempt> = {}): RepairAttempt => ({
  run_id: 'run-heal', tool_name: 'assign_ticket', base_version: '1.0.0', base_fingerprint: 'base-fp',
  proposal_fingerprint: 'proposal-fp', candidate_id: 'candidate-1', candidate_version: null,
  candidate_fingerprint: 'candidate-fp', diagnosis_source: 'deterministic',
  proposal: { transform_spec: { operations: [operation('nest_argument', 'user_id', 'target.id'), operation('cast_type', 'target.id', 'target.id', 'lossless_string_to_integer'), operation('set_header', null, 'Authorization')] } },
  candidate_definition: active('1.1.0'), validation_status: 'accepted',
  validation_details: { authorized_for_recovery: true, transformed_arguments: { ticket_id: 81, target: { id: 123 } }, checks: [{ check_name: 'immutable_values', passed: true }] },
  recovery_status: 'recovered', business_effect_occurred: true, verification_succeeded: true,
  registry_committed: true, committed_version: '1.1.0', termination_reason: null, created_at: '2026-10-14T01:00:00Z', ...changes,
});
const event = (run_id: string, sequence: number, type: RunEvent['type'], data: Record<string, unknown> = {}): RunEvent => ({
  run_id, sequence, type, timestamp: '2026-10-14T00:00:00Z', mode: 'healing', tool: 'assign_ticket', data,
});

test('combined diff uses recorded caller arguments and validated transformed arguments', () => {
  const accepted = patch();
  const pair = requestPair(accepted, [event('run-heal', 1, 'tool_call_started', { arguments: { ticket_id: 81, user_id: '123' }, tool_version: '1.0.0', fingerprint: 'base-fp' })]);
  assert.deepEqual(pair.original, { ticket_id: 81, user_id: '123' });
  assert.deepEqual(pair.candidate, { ticket_id: 81, target: { id: 123 } });
  assert.deepEqual(patchOperations(accepted).map(operationSummary), [
    'user_id moved to target.id.', 'String identifier converted to integer.', 'Authorization loaded from configured credentials.',
  ]);
  const display = safeDisplay(accepted.proposal) as Record<string, unknown>;
  assert.ok(!JSON.stringify(display).includes('vault-ref'));
});

test('validated candidate and rejected candidate never become active without a commit', () => {
  const validated = patch({ registry_committed: false, committed_version: null, recovery_status: 'execution_failed', verification_succeeded: false });
  const rejected = patch({ validation_status: 'rejected', validation_details: { rejection_reasons: ['Unsupported conversion'] }, candidate_definition: null,
    registry_committed: false, committed_version: null, recovery_status: 'validation_rejected', verification_succeeded: false });
  assert.equal(activeVersionMatches(validated, [active('1.0.0')]), false);
  assert.equal(activeVersionMatches(rejected, [active('1.0.0')]), false);
  assert.deepEqual(rejected.validation_details?.rejection_reasons, ['Unsupported conversion']);
  assert.equal(activeVersionMatches(patch(), [active('1.1.0')]), true);
  assert.equal(activeVersionMatches(patch(), [active('1.0.0')]), false);
});

test('current run inspector does not inherit an older run patch', () => {
  const previous = patch({ run_id: 'run-old' });
  assert.equal(currentRunPatch([previous], 'run-new'), null);
  assert.equal(currentRunPatch([previous, patch()], 'run-heal')?.run_id, 'run-heal');
  assert.equal(requestPair(previous, []).original, null);
});

test('original request is paired to the matching proposal and preceding base-version call', () => {
  const events = [
    event('run-heal', 1, 'tool_call_started', { arguments: { ticket_id: 10, user_id: 'x' }, tool_version: '1.0.0', fingerprint: 'base-fp' }),
    event('run-heal', 2, 'patch_proposed', { proposal_fingerprint: 'other' }),
    event('run-heal', 3, 'tool_call_started', { arguments: { ticket_id: 81, user_id: '123' }, tool_version: '1.0.0', fingerprint: 'base-fp' }),
    event('run-heal', 4, 'patch_proposed', { proposal_fingerprint: 'proposal-fp' }),
  ];
  assert.deepEqual(requestPair(patch(), events).original, { ticket_id: 81, user_id: '123' });
  assert.equal(requestPair(patch(), events.slice(0, 3)).original, null);
});

test('reuse requires a matching prior commit, active version, and current tool-call evidence', () => {
  const prior = patch();
  const current = [event('run-second', 1, 'tool_call_started', { tool_version: '1.1.0', fingerprint: 'candidate-fp' })];
  assert.deepEqual(reuseEvidence('run-second', current, [prior], [active('1.1.0')]), {
    used: { tool: 'assign_ticket', version: '1.1.0', fingerprint: 'candidate-fp', prior_run_id: 'run-heal' }, diagnosisOccurred: false,
  });
  assert.equal(reuseEvidence('run-second', current, [prior], [active('1.0.0')]), null);
  assert.equal(reuseEvidence('run-second', [event('run-second', 1, 'diagnosis_started')], [prior], [active('1.1.0')]), null);
});

test('empty, partial, and malformed patch responses are distinguished', () => {
  assert.deepEqual(parsePatchPage({ status: 'available', repair_history: [], limit: 100, offset: 0, count: 0 }).repair_history, []);
  const page = parsePatchPage({ status: 'available', repair_history: [patch(), { run_id: 'incomplete' }], limit: 100, offset: 0, count: 2 });
  assert.equal(page.repair_history.length, 1);
  assert.equal(page.invalid_count, 1);
  assert.throws(() => parsePatchPage({ status: 'available', repair_history: {}, limit: 100, offset: 0, count: 0 }));
  assert.throws(() => parsePatchPage({ status: 'available', repair_history: [], limit: 100, offset: 0, count: 1 }));
});

test('registry client rejects incomplete tool definitions', async () => {
  const request = async () => ({ ok: true, status: 200, json: async () => ({ version: 'v1', tools: [{ name: 'assign_ticket' }] }) } as Response);
  await assert.rejects(new DriftGuardApiClient('/api', request).fetchRegistry(), /invalid active registry/);
});
