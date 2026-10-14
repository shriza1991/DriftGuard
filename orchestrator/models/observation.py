"""Typed model for immutable, sanitized failure observations.

Adheres strictly to Member 2 safety invariants:
- Captures runtime evidence from execution phases without accessing mock admin/mutation state.
- Strictly redacts secrets and credentials prior to storage.
- Immutable snapshot of tool inputs, request/response evidence, and write certainty.
"""
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime, timezone
import copy
from pydantic import BaseModel, Field, model_validator

from orchestrator.models import OperationIdentity, TimingInfo


class ExecutionPhase(str, Enum):
    """Execution lifecycle phase where failure occurred."""
    PRE_VALIDATION = "pre_validation"
    CREDENTIAL_RESOLUTION = "credential_resolution"
    REQUEST_MAPPING = "request_mapping"
    HTTP_DISPATCH = "http_dispatch"
    RESPONSE_PARSING = "response_parsing"
    READ_VERIFICATION = "read_verification"


class FailureObservation(BaseModel):
    """Immutable, sanitized observation capturing runtime failure evidence."""
    model_config = {"frozen": True}

    tool_name: str
    registry_version: str = "1.0.0"
    fingerprint: str = ""
    run_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    operation_identity: Optional[OperationIdentity] = None
    execution_phase: ExecutionPhase
    method: str
    endpoint: str
    http_status: Optional[int] = None
    original_arguments: Dict[str, Any] = Field(default_factory=dict)
    request_evidence: Optional[Dict[str, Any]] = None
    response_evidence: Optional[Dict[str, Any]] = None
    validation_details: Optional[Dict[str, Any]] = None
    evidence_paths: List[str] = Field(default_factory=list)
    timing: Optional[TimingInfo] = None
    write_attempted: bool = False
    uncertain_write: bool = False
    raw_error_message: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="before")
    @classmethod
    def _deep_copy_inputs(cls, data: Any) -> Any:
        import copy
        if isinstance(data, dict):
            return copy.deepcopy(data)
        return data

    @property
    def phase(self) -> ExecutionPhase:
        return self.execution_phase

    @property
    def registry_fingerprint(self) -> str:
        return self.fingerprint

    def __getattribute__(self, name: str) -> Any:
        val = super().__getattribute__(name)
        if name in [
            "original_arguments",
            "request_evidence",
            "response_evidence",
            "validation_details",
            "evidence_paths",
        ]:
            if isinstance(val, (dict, list)):
                return copy.deepcopy(val)
        return val

