"""Typed models for deterministic repair proposal planning.

Adheres strictly to Member 2 safety invariants:
- Pure evidence-backed declarative proposal generation.
- Zero LLM diagnosis or heuristic hallucination.
- Strict preservation of caller business parameters.
- Separation of planning from static validation (Task 7) and recovery execution (Task 8).
"""
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from orchestrator.models import ToolDefinition, RestrictedOperationType, RepairProposal
from orchestrator.models.classification import RetrySafety


class PlanningStatus(str, Enum):
    """Outcome status of deterministic repair planning."""
    PROPOSED = "proposed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED = "unsupported"
    NOT_ELIGIBLE = "not_eligible"


class PlannedOperation(BaseModel):
    """Declarative restricted operation proposed to adapt a tool contract."""
    operation_type: RestrictedOperationType
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    conversion_policy: Optional[str] = None
    credential_reference: Optional[str] = None
    value_provenance: str
    evidence_path: str
    evidence_summary: str


class PlanningResult(BaseModel):
    """Complete typed outcome of deterministic repair planning."""
    status: PlanningStatus
    tool_name: str
    base_version: str
    base_fingerprint: str
    proposal: Optional[RepairProposal] = None
    operations: List[PlannedOperation] = Field(default_factory=list)
    candidate_preview: Optional[ToolDefinition] = None
    proposal_fingerprint: str = ""
    reasons: List[str] = Field(default_factory=list)
    missing_requirements: List[str] = Field(default_factory=list)
    required_validation_checks: List[str] = Field(default_factory=list)
    evidence_references: Dict[str, Any] = Field(default_factory=dict)
    is_eligible: bool = True
    retry_safety: Optional[RetrySafety] = None
    source: str = "deterministic"
    diagnosis_tokens: Optional[Dict[str, int]] = None
    explanation: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def outcome(self) -> PlanningStatus:
        return self.status
