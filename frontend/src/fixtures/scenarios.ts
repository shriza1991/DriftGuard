import type { RunEvent, RunEventType, ToolRegistryEntry } from '../types';

export const DEFAULT_TASK = "Find Maya’s user ID, assign ticket 81 to her, and confirm the updated assignee.";

export const MUTATIONS = [
  { id: 'none', label: 'None · normal success' },
  { id: 'rename', label: 'Rename · user_id → assignee_id' },
  { id: 'type_change', label: 'Type change · string → integer' },
  { id: 'nesting', label: 'Nesting · target.id' },
  { id: 'required_header', label: 'Required header' },
  { id: 'combined', label: 'Combined · nesting, type, header' },
  { id: 'missing_business_value', label: 'Missing business value · safe refusal' },
  { id: 'unavailable_credential', label: 'Unavailable credential · safe refusal' },
  { id: 'ambiguous_mapping', label: 'Ambiguous mapping · safe refusal' },
] as const;

export type MutationId = typeof MUTATIONS[number]['id'];
export type RunSide = 'baseline' | 'healing';

type EventStep = { type: RunEventType; tool?: string; summary: string; data?: Record<string, unknown> };

export interface FixtureScenario {
  title: string;
  description: string;
  originalRequest: Record<string, unknown>;
  candidateRequest: Record<string, unknown> | null;
  changes: string[];
  baseline: EventStep[];
  healing: EventStep[];
}

const outcome = {
  ticket_id: 81,
  resolved_user_id: '123',
  verified_assignee_id: '123',
  verified: true,
  verification_method: 'GET /tickets/81',
};

const start = (mode: RunSide): EventStep => ({
  type: 'run_started',
  summary: `${mode === 'baseline' ? 'Baseline' : 'DriftGuard'} workflow started.`,
  data: { task: DEFAULT_TASK },
});
const find: EventStep = { type: 'tool_call_started', tool: 'find_user', summary: 'Looking up Maya.', data: { arguments: { name: 'Maya' } } };
const assign: EventStep = { type: 'tool_call_started', tool: 'assign_ticket', summary: 'Assigning ticket 81 to user 123.', data: { arguments: { ticket_id: 81, user_id: '123' } } };
const read: EventStep = { type: 'tool_call_started', tool: 'get_ticket', summary: 'Reading ticket 81 to verify the assignee.', data: { arguments: { ticket_id: 81 } } };
const complete: EventStep = { type: 'run_completed', summary: 'Task completed after independent assignee verification.', data: { outcome } };
const verified: EventStep = { type: 'recovery_verified', tool: 'assign_ticket', summary: 'Independent read confirms assignee 123 on ticket 81.', data: { verification_method: 'GET /tickets/81', verified_assignee_id: '123' } };
const committed: EventStep = { type: 'registry_committed', tool: 'assign_ticket', summary: 'Verified candidate committed as registry version 1.1.0.', data: { base_version: '1.0.0', committed_version: '1.1.0' } };

const failure = (message: string, code: string): EventStep => ({
  type: 'tool_call_failed', tool: 'assign_ticket', summary: message,
  data: { status_code: 422, error: { code, message } },
});
const diagnosis: EventStep = { type: 'diagnosis_started', tool: 'assign_ticket', summary: 'Diagnosing the observed validation response.', data: { observed_status: 422 } };
const retry: EventStep = { type: 'retry_started', tool: 'assign_ticket', summary: 'Trying the validated request once.', data: { attempt: 1 } };

export const FIXTURES: Partial<Record<MutationId, FixtureScenario>> = {
  none: {
    title: 'Normal success', description: 'Both paths complete without an API change.',
    originalRequest: { ticket_id: 81, user_id: '123' }, candidateRequest: null, changes: [],
    baseline: [start('baseline'), find, assign, read, complete],
    healing: [start('healing'), find, assign, read, complete],
  },
  rename: {
    title: 'Field rename recovery', description: 'The baseline stops at a renamed field; DriftGuard validates and verifies an adapted request.',
    originalRequest: { ticket_id: 81, user_id: '123' },
    candidateRequest: { ticket_id: 81, assignee_id: '123' },
    changes: ['user_id moved to assignee_id.'],
    baseline: [start('baseline'), find, assign, failure("The API now requires assignee_id instead of user_id.", 'MISSING_REQUIRED_FIELD'),
      { type: 'run_failed', summary: 'Baseline stopped after the schema mismatch.', data: { reason: 'Schema mismatch' } }],
    healing: [start('healing'), find, assign, failure("The API now requires assignee_id instead of user_id.", 'MISSING_REQUIRED_FIELD'),
      diagnosis,
      { type: 'patch_proposed', tool: 'assign_ticket', summary: 'Candidate maps user_id to assignee_id.', data: { candidate_version: '1.1.0' } },
      { type: 'patch_validated', tool: 'assign_ticket', summary: 'Candidate passed deterministic validation; it is not committed yet.', data: { status: 'validated_not_committed' } },
      retry, read, verified, committed, complete],
  },
  combined: {
    title: 'Combined drift recovery', description: 'A nested integer identifier and an authorization header are required together.',
    originalRequest: { ticket_id: 81, user_id: '123' },
    candidateRequest: { ticket_id: 81, target: { id: 123 }, headers: ['Authorization (configured credentials)'] },
    changes: ['user_id moved to target.id.', 'String identifier converted to integer.', 'Authorization loaded from configured credentials.'],
    baseline: [start('baseline'), find, assign, failure('The request no longer matches the downstream contract.', 'COMBINED_SCHEMA_MISMATCH'),
      { type: 'run_failed', summary: 'Baseline stopped at the downstream rejection.', data: { reason: 'Combined contract drift' } }],
    healing: [start('healing'), find, assign, failure('The request no longer matches the downstream contract.', 'COMBINED_SCHEMA_MISMATCH'),
      diagnosis,
      { type: 'patch_proposed', tool: 'assign_ticket', summary: 'Candidate changes nesting, identifier type, and header reference.', data: { candidate_version: '1.1.0' } },
      { type: 'patch_validated', tool: 'assign_ticket', summary: 'Candidate passed deterministic validation; it is not committed yet.', data: { status: 'validated_not_committed' } },
      retry, read, verified, committed, complete],
  },
  missing_business_value: {
    title: 'Safe refusal', description: 'The API demands a business value absent from the caller’s intent.',
    originalRequest: { ticket_id: 81, user_id: '123' }, candidateRequest: null,
    changes: ['priority_tier is unavailable; no value is invented.'],
    baseline: [start('baseline'), find, assign, failure('The API requires priority_tier, which the task did not provide.', 'MISSING_BUSINESS_VALUE'),
      { type: 'run_failed', summary: 'Baseline stopped at the required-value error.', data: { reason: 'Missing business value' } }],
    healing: [start('healing'), find, assign, failure('The API requires priority_tier, which the task did not provide.', 'MISSING_BUSINESS_VALUE'),
      diagnosis,
      { type: 'patch_rejected', tool: 'assign_ticket', summary: 'No safe candidate: priority_tier cannot be invented.', data: { rejection_reasons: ['Business value absent from caller intent'] } },
      { type: 'run_failed', summary: 'Safe refusal. Ticket assignment was not verified.', data: { safe_refusal: true, reason: 'Missing business value' } }],
  },
};

export function fixtureEvents(scenario: MutationId, side: RunSide): RunEvent[] {
  const steps = FIXTURES[scenario]?.[side] ?? [];
  return steps.map((step, index) => ({
    run_id: `sample-${scenario}-${side}`,
    sequence: index + 1,
    timestamp: new Date(Date.UTC(2026, 9, 14, 12, 0, 0, index * 180)).toISOString(),
    mode: side === 'baseline' ? 'normal' : 'healing',
    type: step.type,
    tool: step.tool ?? null,
    data: { summary: step.summary, ...step.data },
  }));
}

export const SAMPLE_REGISTRY: ToolRegistryEntry[] = [
  { name: 'find_user', version: '1.0.0', fingerprint: '', description: 'Resolve a user by name.', endpoint: '/users', method: 'GET', headers: {}, argument_schema: { type: 'object', properties: { name: { type: 'string' } }, required: ['name'] }, request_mapping: {}, response_mapping: {}, credential_reference: null, status: 'active' },
  { name: 'assign_ticket', version: '1.0.0', fingerprint: '', description: 'Assign ticket 81 to a resolved user.', endpoint: '/tickets/assign', method: 'POST', headers: {}, argument_schema: { type: 'object', properties: { ticket_id: { type: 'integer' }, user_id: { type: 'string' } }, required: ['ticket_id', 'user_id'] }, request_mapping: {}, response_mapping: {}, credential_reference: null, status: 'active' },
  { name: 'get_ticket', version: '1.0.0', fingerprint: '', description: 'Independently read the ticket assignee.', endpoint: '/tickets/{ticket_id}', method: 'GET', headers: {}, argument_schema: { type: 'object', properties: { ticket_id: { type: 'integer' } }, required: ['ticket_id'] }, request_mapping: {}, response_mapping: {}, credential_reference: null, status: 'active' },
];
