"""Typed model for deterministic semantic failure classification and retry safety.

Adheres strictly to Member 2 safety invariants:
- Semantic categories separate from execution-layer FailureCategory.
- Independent modeling of retry safety and diagnosis eligibility.
- Zero reliance on hidden mutation state or scenario names.
"""
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


class SemanticFailureCategory(str, Enum):
    """Semantic category distinguishing drift, business rejection, auth, invalid input, transient, and ambiguous."""
    SCHEMA_DRIFT = "schema_drift"
    INVALID_INPUT = "invalid_input"
    BUSINESS_RULE_REJECTION = "business_rule_rejection"
    AUTHENTICATION_AUTHORIZATION = "authentication_authorization"
    TRANSIENT_SERVICE_FAILURE = "transient_service_failure"
    AMBIGUOUS = "ambiguous"


class RetrySafety(BaseModel):
    """Models whether an operation is safe to retry and under what constraints."""
    safe_to_retry: bool
    reason: str
    uncertain_write: bool = False
    idempotent_method: bool = False
    requires_input_adaptation: bool = False

    @property
    def retry_reason(self) -> str:
        return self.reason


class ClassificationResult(BaseModel):
    """Typed deterministic classification result with evidence references and retry safety."""
    category: SemanticFailureCategory
    evidence_references: Dict[str, Any] = Field(default_factory=dict)
    reason: str
    diagnosis_eligible: bool
    retry_safety: RetrySafety
    uncertainty_reason: Optional[str] = None

    @property
    def semantic_category(self) -> SemanticFailureCategory:
        return self.category

