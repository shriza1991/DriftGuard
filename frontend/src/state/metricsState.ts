import type { ModelUsageBreakdown, RunMetrics, SystemMetrics } from '../types';

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 0;
const measure = (value: unknown): value is number | null => value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0);

function usage(value: unknown): value is ModelUsageBreakdown {
  return record(value) && ['complete', 'incomplete', 'unavailable'].includes(String(value.status)) &&
    typeof value.is_complete === 'boolean' &&
    ['input_tokens', 'output_tokens', 'total_tokens'].every((key) => value[key] === 'unavailable' || count(value[key]));
}

function runMetric(value: unknown): value is RunMetrics {
  return record(value) && typeof value.run_id === 'string' && value.run_id.length > 0 &&
    typeof value.mode === 'string' && typeof value.status === 'string' &&
    (value.execution_type === null || typeof value.execution_type === 'string') &&
    ['duration_ms', 'recovery_duration_ms', 'commit_latency_ms', 'failed_recovery_duration_ms'].every((key) => measure(value[key])) &&
    ['total_turns', 'tool_dispatch_count', 'original_dispatch_count', 'repaired_dispatch_count', 'repair_attempts_count', 'repair_rejections_count', 'verified_recoveries_count', 'registry_commits_count'].every((key) => count(value[key])) &&
    usage(value.agent_token_usage) && usage(value.diagnosis_token_usage) && usage(value.session_token_usage) &&
    typeof value.audit_status === 'string';
}

export function parseSystemMetrics(value: unknown): SystemMetrics {
  if (!record(value) ||
    !['total_runs', 'normal_runs', 'normal_success_count', 'healing_trials', 'verified_recoveries', 'unsafe_trials'].every((key) => count(value[key])) ||
    !measure(value.average_recovery_time_ms) ||
    typeof value.token_usage !== 'string' || typeof value.status !== 'string' || typeof value.note !== 'string' ||
    !(value.limit === null || count(value.limit)) || !(value.offset === null || count(value.offset)) ||
    !(value.total_runs_count === null || count(value.total_runs_count)) ||
    !Array.isArray(value.run_metrics) || !value.run_metrics.every(runMetric)) {
    throw new Error('Backend returned an invalid metrics response.');
  }
  return value as unknown as SystemMetrics;
}

export const measuredTokens = (usageValue: ModelUsageBreakdown | undefined): number | null =>
  usageValue?.status === 'complete' && usageValue.is_complete && count(usageValue.total_tokens)
    ? usageValue.total_tokens : null;

export const currentRunMetric = (metrics: SystemMetrics | null, runId: string | null): RunMetrics | null =>
  metrics?.run_metrics.find((entry) => entry.run_id === runId) ?? null;
