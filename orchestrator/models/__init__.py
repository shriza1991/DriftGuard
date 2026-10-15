"""Orchestrator typed models package.
Exposes ToolDefinition, ExecutionResult, SanitizedFailure, RepairProposal,
PatchDecision, RunStatus, and RunEvent.
"""
from typing import Optional, Dict, Any, List, Literal
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict
import hashlib
import json
from datetime import datetime, timezone


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


RunEventType = Literal[
    "run_started",
    "tool_call_started",
    "tool_call_failed",
    "diagnosis_started",
    "llm_policy_decision",
    "cache_hit_validated",
    "patch_proposed",
    "patch_validated",
    "patch_rejected",
    "retry_started",
    "recovery_verified",
    "registry_committed",
    "run_completed",
    "run_failed"
]


class RunEvent(BaseModel):
    """Event matching contracts/events.schema.json exactly."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    sequence: int = Field(ge=1)
    timestamp: str
    mode: Literal["normal", "healing"]
    type: RunEventType
    tool: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class ToolArgumentSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "object"
    properties: Dict[str, Any]
    required: List[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    """Tool definition specification adhering to frozen v1 contracts."""
    name: str
    version: str = "1.0.0"
    fingerprint: str = ""
    description: str
    endpoint: str
    method: str = "GET"
    headers: Dict[str, str] = Field(default_factory=dict)
    argument_schema: ToolArgumentSchema
    request_mapping: Dict[str, Any] = Field(default_factory=dict)
    response_mapping: Dict[str, Any] = Field(default_factory=dict)
    credential_reference: Optional[str] = None
    status: str = "active"

    def compute_fingerprint(self) -> str:
        """Compute deterministic SHA256 fingerprint for canonical contract content with stable key ordering.
        Excludes lifecycle metadata such as version, status, and candidate IDs.
        """
        payload = {
            "name": self.name,
            "endpoint": self.endpoint,
            "method": self.method.upper(),
            "argument_schema": self.argument_schema.model_dump(),
            "request_mapping": self.request_mapping,
            "response_mapping": self.response_mapping,
            "headers": self.headers,
            "credential_reference": self.credential_reference
        }
        raw = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# Alias for backwards-compatibility
ToolRegistryEntry = ToolDefinition


class RegistryResponse(BaseModel):
    tools: List[ToolDefinition]
    version: str = "v1"
    updated_at: str


class FailureCategory(str, Enum):
    VALIDATION_FAILURE = "validation_failure"
    CREDENTIAL_FAILURE = "credential_failure"
    TRANSPORT_FAILURE = "transport_failure"
    HTTP_REJECTION = "http_rejection"
    RESPONSE_PARSING_FAILURE = "response_parsing_failure"


class ExecutionContext(BaseModel):
    """Contextual metadata and execution identity for a logical tool call or workflow."""
    run_id: str
    tool_call_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    candidate_snapshot: Optional[ToolDefinition] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OperationIdentity(BaseModel):
    tool_name: str
    endpoint: str
    method: str
    idempotency_key: Optional[str] = None
    tool_call_id: Optional[str] = None
    run_id: Optional[str] = None

    @property
    def key(self) -> str:
        identity = self.idempotency_key or self.tool_call_id or "unidentified"
        return f"{self.run_id or 'unscoped'}:{identity}:{self.tool_name}:{self.method.upper()}:{self.endpoint}"


class TimingInfo(BaseModel):
    started_at: str
    completed_at: str
    duration_ms: float


from orchestrator.models.observation import ExecutionPhase, FailureObservation
from orchestrator.models.classification import SemanticFailureCategory, RetrySafety, ClassificationResult


class SanitizedFailure(BaseModel):
    """Normalized and secret-redacted failure representation preserving structured validation evidence."""
    is_drift: bool = False
    error_code: str
    message: str
    http_status: int
    normalized_errors: Dict[str, Any] = Field(default_factory=dict)
    raw_details: Dict[str, Any] = Field(default_factory=dict)
    migration_hint: Optional[str] = None
    tool_name: str
    endpoint: str
    timestamp: str
    registry_version: str = "1.0.0"
    failure_category: Optional[FailureCategory] = None
    uncertain_write: bool = False
    observation: Optional[FailureObservation] = None
    classification: Optional[ClassificationResult] = None


class ExecutionResult(BaseModel):
    """Normalized result of executing a tool call."""
    is_success: bool
    outcome: Optional[Dict[str, Any]] = None
    http_status: int
    normalized_result: Optional[Dict[str, Any]] = None
    raw_response: Optional[Any] = None
    normalized_errors: Optional[Dict[str, Any]] = None
    failure: Optional[SanitizedFailure] = None
    registry_version: str = "1.0.0"
    fingerprint: str = ""
    timing: TimingInfo
    operation_identity: OperationIdentity
    observation: Optional[FailureObservation] = None
    classification: Optional[ClassificationResult] = None

    @property
    def failure_observation(self) -> Optional[FailureObservation]:
        return self.observation

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self.outcome or self.normalized_result


class RestrictedOperationType(str, Enum):
    RENAME_ARGUMENT = "rename_argument"
    CAST_TYPE = "cast_type"
    NEST_ARGUMENT = "nest_argument"
    SET_HEADER = "set_header"


class RepairProposal(BaseModel):
    """Typed repair proposal containing restricted safe operations and evidence references.
    Structure only; no patch application logic.
    """
    proposal_id: str
    tool: str
    base_version: str
    target_version: str
    restricted_operations: List[RestrictedOperationType] = Field(default_factory=list)
    evidence_references: Dict[str, Any] = Field(default_factory=dict)
    transform_spec: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PatchDecision(BaseModel):
    """Static authorization decision regarding a RepairProposal."""
    decision_id: str
    proposal_id: str
    accepted: bool
    status: Literal["accepted", "rejected", "pending_validation"] = "pending_validation"
    reasons: List[str] = Field(default_factory=list)
    safety_violations: List[str] = Field(default_factory=list)
    decided_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


from orchestrator.models.proposal import PlanningStatus, PlannedOperation, PlanningResult
from orchestrator.models.validation import ValidationStatus, SafetyCheckRecord, ValidationResult
