"""Typed data models for SQLite audit storage."""
from typing import Dict, Any, Optional, List, Union
from pydantic import BaseModel, Field
from datetime import datetime, timezone


class PersistedRunRecord(BaseModel):
    """Complete summary and status of a workflow run."""
    run_id: str
    mode: str
    execution_type: Optional[str] = None
    task: str
    status: str  # 'running', 'completed', 'failed', 'interrupted'
    termination_reason: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[float] = None
    verified_outcome: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    audit_status: str = "complete"  # 'complete' or 'incomplete'


class PersistedToolExecution(BaseModel):
    """Execution details for an individual tool invocation attempt."""
    run_id: str
    tool_call_id: str
    tool_name: str
    tool_version: str
    fingerprint: str
    attempt_number: int = 1
    is_repaired: bool = False
    arguments: Dict[str, Any] = Field(default_factory=dict)
    status: str  # 'success', 'failed', 'uncertain'
    result: Optional[Dict[str, Any]] = None
    failure: Optional[Dict[str, Any]] = None
    timing: Optional[Dict[str, Any]] = None
    operation_identity: Optional[str] = None


class PersistedRepairAttempt(BaseModel):
    """Complete record of an autonomous repair proposal, validation, and recovery attempt."""
    run_id: str
    tool_name: str
    base_version: str
    base_fingerprint: str
    proposal_fingerprint: str
    candidate_id: Optional[str] = None
    candidate_version: Optional[str] = None
    candidate_fingerprint: Optional[str] = None
    diagnosis_source: str = "deterministic"  # 'deterministic' or 'llm_assisted'
    diagnosis_tokens: int = 0
    proposal: Dict[str, Any] = Field(default_factory=dict)
    candidate_definition: Optional[Dict[str, Any]] = None
    validation_status: str  # 'accepted', 'rejected', 'pending'
    validation_details: Optional[Dict[str, Any]] = None
    recovery_status: str  # 'recovered', 'validation_rejected', 'planning_failed', 'execution_failed', 'verification_failed', 'commit_failed', etc.
    business_effect_occurred: bool = False
    verification_succeeded: bool = False
    registry_committed: bool = False
    committed_version: Optional[str] = None
    termination_reason: Optional[str] = None
    recovery_duration_ms: Optional[float] = None  # from first drift failure to verified read
    commit_latency_ms: Optional[float] = None     # separate measurement for atomic commit
    failed_recovery_duration_ms: Optional[float] = None  # measured on failure/refusal
    created_at: str


class PersistedEvent(BaseModel):
    """Audit event record matching the agreed contract envelope."""
    run_id: str
    sequence: int
    timestamp: str
    mode: str
    event_type: str
    tool: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class PersistedModelUsage(BaseModel):
    """Model token consumption record per call and purpose."""
    run_id: str
    call_id: str
    purpose: str  # 'agent_turn' or 'diagnosis'
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    is_complete: bool = True
    created_at: str


class PersistedLLMInteraction(BaseModel):
    """Redacted, durable record for one attempted repair-model interaction."""
    interaction_id: str
    run_id: str
    incident_id: str
    trial_id: Optional[str] = None
    created_at: str
    duration_ms: float
    provider: str
    model_name: str
    prompt_template_version: str
    response_schema_version: str
    cache_key: Optional[str] = None
    cache_status: str = "bypassed"
    sanitized_prompt_hash: str
    sanitized_prompt_payload: Optional[Dict[str, Any]] = None
    sanitized_response_payload: Optional[Dict[str, Any]] = None
    failure_payload: Optional[Dict[str, Any]] = None
    response_hash: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_unavailable_reason: Optional[str] = None
    timed_out: bool = False
    provider_error: Optional[str] = None
    parse_status: str
    validation_status: str
    deterministic_decision: str
    reused_later: bool = False
    retention_until: Optional[str] = None
    deleted_at: Optional[str] = None


class PersistedRepairCacheEntry(BaseModel):
    """Exact-scope validated declarative repair cached only after verified commit."""
    cache_key: str
    scope_hashes: Dict[str, str]
    tool_name: str
    base_version: str
    base_fingerprint: str
    policy_version: str
    prompt_version: str
    response_schema_version: str
    model_configuration_hash: str
    operations: List[Dict[str, Any]]
    evidence_references: Dict[str, Any]
    proposal_fingerprint: str
    original_interaction_id: str
    committed_version: str
    committed_fingerprint: str
    created_at: str
    expires_at: str
    last_accessed_at: Optional[str] = None
    hit_count: int = 0
    valid: bool = True
    invalidation_reason: Optional[str] = None
