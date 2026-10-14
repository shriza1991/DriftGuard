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
  status: 'running' | 'completed' | 'failed';
  mode: 'normal' | 'healing';
  task: string;
  created_at: string;
  completed_at?: string;
  duration_ms?: number;
  outcome?: {
    ticket_id: number;
    target_user_name: string;
    resolved_user_id: string;
    verified_assignee_id: string;
    verified: boolean;
    status: string;
  };
  error?: string;
}
