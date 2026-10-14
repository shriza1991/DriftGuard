"""Canonical immutable v1 baseline tool catalogue.

Adheres to Member 2 safety invariants:
- Independent of mutable VersionedToolRegistry state.
- Derived directly from default v1 specifications without duplicating definitions.
- Always exposes frozen 1.0.0 contracts regardless of active registry commits.
- Generates LangChain-compatible tool specifications for model binding.
"""
from typing import Dict, Any, List, Optional
import copy

from orchestrator.models import ToolDefinition
from orchestrator.registry.default_tools import create_default_registry


def get_frozen_v1_catalogue() -> Dict[str, ToolDefinition]:
    """Returns defensive deep-copies of the canonical frozen v1 tool definitions.
    
    Guaranteed to remain at version 1.0.0 with canonical SHA256 fingerprints,
    completely isolated from active registry commits or candidate staging.
    """
    raw_v1 = create_default_registry()
    catalogue: Dict[str, ToolDefinition] = {}
    for name, tool in raw_v1.items():
        tool_copy = copy.deepcopy(tool)
        if not tool_copy.fingerprint:
            tool_copy.fingerprint = tool_copy.compute_fingerprint()
        catalogue[name] = tool_copy
    return catalogue


def get_langchain_tool_specs(catalogue: Optional[Dict[str, ToolDefinition]] = None) -> List[Dict[str, Any]]:
    """Converts the frozen v1 catalogue into standard tool definitions for LangChain models."""
    active_catalogue = catalogue or get_frozen_v1_catalogue()
    specs: List[Dict[str, Any]] = []
    for tool in active_catalogue.values():
        specs.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.argument_schema.model_dump(),
            }
        })
    return specs
