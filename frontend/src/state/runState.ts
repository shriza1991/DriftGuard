import type { RunEvent } from '../types';

export type RunOutcome = 'idle' | 'running' | 'verified' | 'completed_unverified' | 'failed' | 'safe_refusal';
export type CandidateState = 'none' | 'proposed' | 'validated' | 'rejected' | 'committed';

export function mergeEvents(existing: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const byIdentity = new Map<string, RunEvent>();
  for (const event of [...existing, ...incoming]) {
    byIdentity.set(`${event.run_id}:${event.sequence}`, event);
  }
  return [...byIdentity.values()].sort((a, b) =>
    a.run_id === b.run_id ? a.sequence - b.sequence : a.run_id.localeCompare(b.run_id)
  );
}

export function runOutcome(events: RunEvent[]): RunOutcome {
  if (events.length === 0) return 'idle';
  const ordered = mergeEvents([], events);
  const terminal = [...ordered].reverse().find((event) => event.type === 'run_completed' || event.type === 'run_failed');
  if (!terminal) return 'running';
  if (terminal.type === 'run_failed') {
    return terminal.data.safe_refusal === true ? 'safe_refusal' : 'failed';
  }
  const outcome = terminal.data.outcome;
  return outcome && typeof outcome === 'object' && outcome.verified === true
    ? 'verified'
    : 'completed_unverified';
}

export function candidateState(events: RunEvent[]): CandidateState {
  let state: CandidateState = 'none';
  for (const event of mergeEvents([], events)) {
    if (event.type === 'patch_proposed') state = 'proposed';
    if (event.type === 'patch_validated') state = 'validated';
    if (event.type === 'patch_rejected') state = 'rejected';
    if (event.type === 'registry_committed') state = 'committed';
  }
  return state;
}

export function eventTone(type: RunEvent['type']): 'blue' | 'amber' | 'green' | 'red' {
  if (type === 'tool_call_failed' || type === 'patch_rejected' || type === 'run_failed') return 'red';
  if (type === 'diagnosis_started' || type === 'patch_proposed' || type === 'patch_validated') return 'amber';
  if (type === 'recovery_verified' || type === 'registry_committed' || type === 'run_completed') return 'green';
  return 'blue';
}

export function safeDisplay(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeDisplay);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => {
      const normalized = key.toLowerCase();
      const isUsage = normalized.includes('token') && (normalized.endsWith('_tokens') || normalized === 'token_usage');
      const sensitive = /auth|secret|password|credential|api.key|token/i.test(key) && !isUsage;
      return [key, sensitive ? '[redacted]' : safeDisplay(entry)];
    }));
  }
  return value;
}
