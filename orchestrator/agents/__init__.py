"""Agents subpackage exposing BaselineAgent, model providers, and frozen catalogue."""
from orchestrator.agents.catalogue import get_frozen_v1_catalogue, get_langchain_tool_specs
from orchestrator.agents.model_provider import create_chat_model, ModelConfigurationError
from orchestrator.agents.baseline import BaselineAgent
from orchestrator.agents.healing import HealingAgent

__all__ = [
    "get_frozen_v1_catalogue",
    "get_langchain_tool_specs",
    "create_chat_model",
    "ModelConfigurationError",
    "BaselineAgent",
    "HealingAgent",
]
