export type RunEventType =
  | 'run_started'
  | 'tool_call_started'
  | 'tool_call_failed'
  | 'diagnosis_started'
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
