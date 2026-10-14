import type { RunEvent, RunSummary } from '../types';

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

export function summaryOutcome(summary: RunSummary | null, events: RunEvent[]): RunOutcome {
  if (!summary) return events.length ? 'running' : 'idle';
  if (summary.status === 'running') return 'running';
  if (summary.status === 'failed' || summary.status === 'interrupted') {
    return summary.termination_reason === 'safe_refusal' || summary.outcome?.safe_refusal === true ? 'safe_refusal' : 'failed';
  }
  return summary.outcome?.verified === true || summary.outcome?.downstream_verified === true
    ? 'verified' : 'completed_unverified';
}

export function hasEventGap(events: RunEvent[]): boolean {
  const ordered = mergeEvents([], events);
  return ordered.some((event, index) => event.sequence !== index + 1);
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
  if (typeof value === 'string') return value.replace(/\bBearer\s+\S+/gi, 'Bearer [redacted]').replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[redacted]').replace(/credential reference ['"][^'"]+['"]/gi, 'credential reference [configured]');
  if (Array.isArray(value)) return value.map(safeDisplay);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => {
      const normalized = key.toLowerCase();
      const isUsage = normalized.includes('token') && (normalized.endsWith('_tokens') || normalized === 'token_usage');
      const sensitive = /auth|secret|password|credential|api.key|token/i.test(key) && !isUsage ||
        normalized === 'value_provenance' && typeof entry === 'string' && entry.includes('credential_reference');
      return [key, sensitive ? '[redacted]' : safeDisplay(entry)];
    }));
  }
  return value;
}

export function safeText(value: unknown): string {
  return String(safeDisplay(value));
}
