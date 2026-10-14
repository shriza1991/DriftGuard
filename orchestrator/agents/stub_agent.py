"""Model provider and Agent bindings (Member 2 Scope).

Configurable single provider supporting:
- mock (deterministic fallback for testing)
- gemini (Google GenAI)
- openai (OpenAI GPT-4o / compatible)

In this initial foundation, tool calling executes deterministically via the registry.
"""
from typing import Dict, Any, List, Optional
from orchestrator.config import settings


class AgentProvider:
    def __init__(self):
        self.provider = settings.model_provider
        self.model_name = settings.model_name

    def get_info(self) -> Dict[str, str]:
        return {
            "provider": self.provider,
            "model_name": self.model_name,
            "status": "configured"
        }


agent_provider = AgentProvider()
