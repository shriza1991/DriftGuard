export type RunEventType =
  | 'run_started'
  | 'tool_call_started'
  | 'tool_call_failed'
  | 'diagnosis_started'
  | 'llm_policy_decision'
  | 'cache_hit_validated'
  | 'patch_proposed'
  | 'patch_validated'
  | 'patch_rejected'
  | 'retry_started'
  | 'recovery_verified'
  | 'registry_committed'
  | 'run_completed'
  | 'run_failed';

export interface RunEvent {
  run_id: string;
  sequence: number;
  timestamp: string;
  mode: 'normal' | 'healing';
  type: RunEventType;
  tool: string | null;
  data: Record<string, any>;
}

export interface ToolArgumentSchema {
  type: string;
  properties: Record<string, any>;
  required?: string[];
}

export interface ToolRegistryEntry {
  name: string;
  version: string;
  fingerprint: string;
  description: string;
  endpoint: string;
  method: string;
  headers: Record<string, string>;
  argument_schema: ToolArgumentSchema;
  request_mapping: Record<string, any>;
  response_mapping: Record<string, any>;
  credential_reference: string | null;
  status: string;
}

export interface SystemMetrics {
  total_runs: number;
  normal_runs: number;
  normal_success_count: number;
  healing_trials: number;
  verified_recoveries: number;
  unsafe_trials: number;
  average_recovery_time_ms: number | null;
  token_usage: string;
  status: string;
  note: string;
  limit: number | null;
  offset: number | null;
  total_runs_count: number | null;
  run_metrics: RunMetrics[];
}

export interface BenchmarkSummary {
  status: 'measured' | 'unavailable';
  timestamp?: string;
  task?: string;
  repetitions?: number;
  message?: string;
  metrics: null | {
    recoverable_case_adaptation_rate_percent: number | null;
    all_breaking_case_resolution_rate_percent: number | null;
    safe_stop_rate_percent: number | null;
    verified_task_correctness_percent: number | null;
    median_mttr_ms: number | null;
    p95_mttr_ms: number | null;
    input_tokens: number;
    output_tokens: number;
    unsafe_execution_count: number;
    maximum_retry_count: number;
    warm_call_diagnosis_reduction_percent: number | null;
    baseline_recoverable_success_rate_percent: number | null;
  };
  limitations?: string[];
}

export interface ModelUsageBreakdown {
  input_tokens: number | 'unavailable';
  output_tokens: number | 'unavailable';
  total_tokens: number | 'unavailable';
  is_complete: boolean;
  status: 'complete' | 'incomplete' | 'unavailable';
}

export interface RunMetrics {
  run_id: string;
  mode: string;
  execution_type: string | null;
  status: string;
  duration_ms: number | null;
  total_turns: number;
  tool_dispatch_count: number;
  original_dispatch_count: number;
  repaired_dispatch_count: number;
  repair_attempts_count: number;
  repair_rejections_count: number;
  verified_recoveries_count: number;
  registry_commits_count: number;
  recovery_duration_ms: number | null;
  commit_latency_ms: number | null;
  failed_recovery_duration_ms: number | null;
  agent_token_usage: ModelUsageBreakdown;
  diagnosis_token_usage: ModelUsageBreakdown;
  session_token_usage: ModelUsageBreakdown;
  audit_status: string;
}

export interface RunSummary {
  run_id: string;
  status: 'running' | 'completed' | 'failed' | 'interrupted';
  mode: 'normal' | 'healing' | 'baseline';
  task: string;
  created_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  outcome: Record<string, unknown> | null;
  error: string | null;
  model_usage: Record<string, unknown> | null;
  llm_calls?: { outbound_attempts: number; limit: number; logical_invocations: number; successful_responses: number; failed_attempts: number; http_429_responses: number; tool_api_calls: number; token_usage: unknown } | null;
  termination_reason: string | null;
  execution_type: string | null;
  audit_status: string;
}

export interface CreateRunRequest {
  mode: 'normal' | 'baseline' | 'healing';
  task: string;
  execution_type: 'deterministic' | 'model_driven_baseline' | 'model_driven_healing';
  wait: false;
}

export interface MutationResponse {
  active_scenario: string;
  enabled: boolean;
  evidence_level: string;
  description?: string;
}

export type EvidenceLevel = 'ordinary' | 'explicit_hint';
export type HealingExecution = 'model_driven_healing' | 'deterministic';

export interface ResetResponse {
  status: 'reset_completed';
  orchestrator: string;
  mock_api: string;
  audit_store: string;
}

export interface PatchOperation {
  operation_type: 'rename_argument' | 'cast_type' | 'nest_argument' | 'set_header';
  source_path: string | null;
  target_path: string | null;
  conversion_policy: string | null;
  credential_reference: string | null;
  value_provenance: string;
  evidence_path: string;
  evidence_summary: string;
}

export interface RepairAttempt {
  run_id: string;
  tool_name: string;
  base_version: string;
  base_fingerprint: string;
  proposal_fingerprint: string;
  candidate_id: string | null;
  candidate_version: string | null;
  candidate_fingerprint: string | null;
  diagnosis_source: string;
  proposal: Record<string, unknown>;
  candidate_definition: ToolRegistryEntry | null;
  validation_status: 'accepted' | 'rejected' | 'pending' | string;
  validation_details: Record<string, unknown> | null;
  recovery_status: string;
  business_effect_occurred: boolean;
  verification_succeeded: boolean;
  registry_committed: boolean;
  committed_version: string | null;
  termination_reason: string | null;
  created_at: string;
}

export interface PatchPage {
  status: string;
  notice: string;
  repair_history: RepairAttempt[];
  limit: number;
  offset: number;
  count: number;
  invalid_count: number;
}
