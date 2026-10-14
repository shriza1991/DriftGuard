"""Legacy provider metadata scaffold; not used by model-driven run execution.

Model-driven runs construct BaselineAgent or HealingAgent and obtain a real model
through create_chat_model. The explicit deterministic execution type is separate.
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
