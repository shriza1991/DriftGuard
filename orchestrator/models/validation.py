"""Typed models for deterministic safety validation.

Adheres strictly to Member 2 Task 7 safety invariants:
- Pure independent deterministic validation and preparation of a safe candidate.
- Zero LLM calls, zero HTTP requests, zero credential acquisition.
- Zero registry staging or commits (Task 8 owns staged patching & commit).
- Strict verification of proposal integrity, evidence references, and fingerprint freshness.
- Strict verification of value provenance, lossless numeric conversions, and no invented values.
- Strict protection of uncertain writes and pre-execution rejection guarantees.
- Candidate definitions marked 'validated_not_committed'.
"""
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from orchestrator.models import ToolDefinition


class ValidationStatus(str, Enum):
    """Outcome status of deterministic safety validation."""
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SafetyCheckRecord(BaseModel):
    """Record of an individual static safety invariant check."""
    check_name: str
    passed: bool
    reason: Optional[str] = None
    evidence_reference: Optional[str] = None


class ValidationResult(BaseModel):
    """Complete typed outcome of deterministic safety validation."""
    status: ValidationStatus
    tool_name: str
    base_version: str
    base_fingerprint: str
    proposal_fingerprint: str
    candidate_fingerprint: Optional[str] = None
    candidate: Optional[ToolDefinition] = None
    transformed_arguments: Optional[Dict[str, Any]] = None
    authorized_for_recovery: bool = False
    checks: List[SafetyCheckRecord] = Field(default_factory=list)
    rejection_reasons: List[str] = Field(default_factory=list)
    evidence_summary: Dict[str, Any] = Field(default_factory=dict)
    validated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_accepted(self) -> bool:
        return self.status == ValidationStatus.ACCEPTED

    @property
    def is_rejected(self) -> bool:
        return self.status == ValidationStatus.REJECTED
