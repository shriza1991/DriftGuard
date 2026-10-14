import type { PatchOperation, PatchPage, RepairAttempt, RunEvent, ToolRegistryEntry } from '../types';
import { mergeEvents } from './runState.ts';

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const string = (value: unknown): value is string => typeof value === 'string';
const nullableString = (value: unknown) => value === null || string(value);

export function parsePatchPage(value: unknown): PatchPage {
  if (!object(value) || !Array.isArray(value.repair_history) || !string(value.status) ||
      !Number.isInteger(value.limit) || (value.limit as number) < 1 || !Number.isInteger(value.offset) ||
      (value.offset as number) < 0 || !Number.isInteger(value.count) || value.count !== value.repair_history.length)
    throw new Error('Backend returned an invalid patch history response.');
  const repair_history: RepairAttempt[] = [];
  let invalid_count = 0;
  for (const row of value.repair_history) {
    if (!object(row) || !string(row.run_id) || !string(row.tool_name) || !string(row.base_version) ||
        !string(row.base_fingerprint) || !string(row.proposal_fingerprint) ||
        !nullableString(row.candidate_id) || !nullableString(row.candidate_version) ||
        !nullableString(row.candidate_fingerprint) || !string(row.diagnosis_source) ||
        !object(row.proposal) || !(row.candidate_definition === null ||
          object(row.candidate_definition) && string(row.candidate_definition.name) &&
          string(row.candidate_definition.version) && object(row.candidate_definition.argument_schema)) ||
        !string(row.validation_status) || !(row.validation_details === null || object(row.validation_details)) ||
        !string(row.recovery_status) || typeof row.business_effect_occurred !== 'boolean' ||
        typeof row.verification_succeeded !== 'boolean' || typeof row.registry_committed !== 'boolean' ||
        !nullableString(row.committed_version) || !nullableString(row.termination_reason) || !string(row.created_at)) {
      invalid_count += 1;
      continue;
    }
    repair_history.push(row as unknown as RepairAttempt);
  }
  return {
    status: value.status, notice: string(value.notice) ? value.notice : '',
    repair_history, limit: value.limit as number, offset: value.offset as number,
    count: value.count as number, invalid_count,
  };
}

export function patchOperations(patch: RepairAttempt): PatchOperation[] {
  const transform = patch.proposal.transform_spec;
  const entries = object(transform) && Array.isArray(transform.operations) ? transform.operations : [];
  return entries.filter((entry): entry is PatchOperation =>
    object(entry) && ['rename_argument', 'cast_type', 'nest_argument', 'set_header'].includes(String(entry.operation_type)) &&
    nullableString(entry.source_path) && nullableString(entry.target_path) && nullableString(entry.conversion_policy) &&
    nullableString(entry.credential_reference) && string(entry.value_provenance) && string(entry.evidence_path) && string(entry.evidence_summary));
}

export function operationSummary(operation: PatchOperation): string {
  const source = operation.source_path || 'field';
  const target = operation.target_path || 'field';
  if (operation.operation_type === 'rename_argument' || operation.operation_type === 'nest_argument') return `${source} moved to ${target}.`;
  if (operation.operation_type === 'cast_type') return operation.conversion_policy?.includes('int')
    ? 'String identifier converted to integer.' : `${source} converted using ${operation.conversion_policy || 'recorded policy'}.`;
  if (operation.operation_type === 'set_header') return `${target} loaded from configured credentials.`;
  return 'Unsupported operation.';
}

export function patchKey(patch: RepairAttempt): string {
  return `${patch.run_id}:${patch.tool_name}:${patch.proposal_fingerprint}`;
}

export function currentRunPatch(patches: RepairAttempt[], runId: string | null): RepairAttempt | null {
  if (!runId) return null;
  return patches.filter((patch) => patch.run_id === runId).sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ?? null;
}

export function requestPair(patch: RepairAttempt, events: RunEvent[]) {
  const ordered = mergeEvents([], events).filter((event) => event.run_id === patch.run_id);
  const proposal = ordered.find((event) => event.type === 'patch_proposed' && event.tool === patch.tool_name &&
    event.data.proposal_fingerprint === patch.proposal_fingerprint);
  const candidates = ordered.filter((event) => event.tool === patch.tool_name && event.type === 'tool_call_started' &&
    object(event.data.arguments) && event.data.tool_version === patch.base_version &&
    event.data.fingerprint === patch.base_fingerprint && (!proposal || event.sequence < proposal.sequence));
  const started = proposal ? candidates[candidates.length - 1] : candidates.length === 1 ? candidates[0] : null;
  const details = patch.validation_details;
  return {
    original: started && object(started.data.arguments) ? started.data.arguments : null,
    candidate: details && object(details.transformed_arguments) ? details.transformed_arguments : null,
  };
}

export function activeVersionMatches(patch: RepairAttempt, tools: ToolRegistryEntry[]): boolean {
  const tool = tools.find((item) => item.name === patch.tool_name);
  return !!tool && patch.registry_committed && patch.committed_version === tool.version &&
    (!patch.candidate_fingerprint || patch.candidate_fingerprint === tool.fingerprint);
}

export function reuseEvidence(runId: string | null, events: RunEvent[], patches: RepairAttempt[], tools: ToolRegistryEntry[]) {
  if (!runId) return null;
  const ordered = mergeEvents([], events).filter((event) => event.run_id === runId);
  const used = ordered.filter((event) => event.type === 'tool_call_started' && string(event.data.tool_version));
  const matches = used.flatMap((event) => {
    const tool = tools.find((item) => item.name === event.tool);
    const prior = patches.find((patch) => patch.run_id !== runId && patch.tool_name === event.tool &&
      patch.registry_committed && patch.committed_version === event.data.tool_version &&
      activeVersionMatches(patch, tools));
    return tool && prior && event.data.fingerprint === tool.fingerprint
      ? [{ tool: tool.name, version: tool.version, fingerprint: tool.fingerprint, prior_run_id: prior.run_id }]
      : [];
  });
  if (!matches.length) return null;
  return { used: matches[0], diagnosisOccurred: ordered.some((event) => event.type === 'diagnosis_started') };
}
