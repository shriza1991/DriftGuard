"""Healing subpackage for failure interception, classification, autonomous repair planning, and safety validation."""
from orchestrator.healing.classifier import classify_failure
from orchestrator.healing.planner import plan_repair, emit_patch_proposed_event
from orchestrator.healing.validator import validate_repair_proposal, emit_patch_validation_event

__all__ = [
    "classify_failure",
    "plan_repair",
    "emit_patch_proposed_event",
    "validate_repair_proposal",
    "emit_patch_validation_event",
]

