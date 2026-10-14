import test from 'node:test';
import assert from 'node:assert/strict';
import { currentRunMetric, measuredTokens, parseSystemMetrics } from '../src/state/metricsState.ts';

const complete = { input_tokens: 7, output_tokens: 3, total_tokens: 10, is_complete: true, status: 'complete' };
const unavailable = { input_tokens: 'unavailable', output_tokens: 'unavailable', total_tokens: 'unavailable', is_complete: false, status: 'unavailable' };
const metric = {
  run_id: 'healing-one', mode: 'healing', execution_type: 'model_driven_healing', status: 'completed', duration_ms: 120,
  total_turns: 2, tool_dispatch_count: 2, original_dispatch_count: 1, repaired_dispatch_count: 1,
  repair_attempts_count: 1, repair_rejections_count: 0, verified_recoveries_count: 1, registry_commits_count: 1,
  recovery_duration_ms: 42, commit_latency_ms: 2, failed_recovery_duration_ms: null,
  agent_token_usage: complete, diagnosis_token_usage: complete, session_token_usage: complete, audit_status: 'complete',
};
const response = {
  total_runs: 2, normal_runs: 1, normal_success_count: 1, healing_trials: 1, verified_recoveries: 1,
  unsafe_trials: 0, average_recovery_time_ms: 42, token_usage: 'measured', status: 'normal_scaffolding_active',
  note: 'Only real runs are counted.', limit: 50, offset: 0, total_runs_count: 2, run_metrics: [metric],
};

test('accepts actual paginated metrics and selects only the matching run', () => {
  const parsed = parseSystemMetrics(response);
  assert.equal(parsed.average_recovery_time_ms, 42);
  assert.equal(currentRunMetric(parsed, 'healing-one')?.recovery_duration_ms, 42);
  assert.equal(currentRunMetric(parsed, 'another-run'), null);
  assert.equal(measuredTokens(parsed.run_metrics[0].diagnosis_token_usage), 10);
});

test('missing measurements remain unavailable, while a complete measured zero is allowed', () => {
  const parsed = parseSystemMetrics({ ...response, average_recovery_time_ms: null, run_metrics: [{ ...metric, recovery_duration_ms: null, diagnosis_token_usage: unavailable }] });
  assert.equal(parsed.average_recovery_time_ms, null);
  assert.equal(measuredTokens(parsed.run_metrics[0].diagnosis_token_usage), null);
  assert.equal(measuredTokens({ input_tokens: 0, output_tokens: 0, total_tokens: 0, is_complete: true, status: 'complete' }), 0);
  assert.equal(measuredTokens({ input_tokens: 7, output_tokens: 3, total_tokens: 10, is_complete: false, status: 'incomplete' }), null);
});

test('rejects incomplete or inconsistent response shapes instead of showing zeroes', () => {
  assert.throws(() => parseSystemMetrics({ ...response, run_metrics: null }), /invalid metrics/);
  assert.throws(() => parseSystemMetrics({ ...response, average_recovery_time_ms: undefined }), /invalid metrics/);
  assert.throws(() => parseSystemMetrics({ ...response, run_metrics: [{ ...metric, session_token_usage: null }] }), /invalid metrics/);
});
