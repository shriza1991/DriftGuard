"""Model provider integration for DriftGuard baseline agent.

Supports single configured model provider through environment variables:
- google_genai (Gemini 2.5 Flash / 1.5 Flash via langchain_google_genai)
- openai (GPT-4o / GPT-4o-mini via langchain_openai)

Adheres strictly to Member 2 safety invariants:
- Startup and health checks remain non-blocking without requiring model credentials.
- Lazy model initialization: credentials validated only when model execution is requested.
- Missing credentials raise explicit ModelConfigurationError (zero fabricated model responses).
- Supports full dependency injection for offline/deterministic testing.
"""
import os
from typing import Optional, Any
from langchain_core.language_models.chat_models import BaseChatModel

from orchestrator.config import settings


class ModelConfigurationError(Exception):
    """Raised when model provider credentials or packages are missing or misconfigured."""
    pass


def create_chat_model(
    override_model: Optional[BaseChatModel] = None,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
) -> BaseChatModel:
    """Creates and returns the configured LangChain chat model.
    
    Args:
        override_model: Pre-instantiated ChatModel (e.g. for testing). If supplied, used directly.
        provider: Provider identifier ('google_genai' / 'gemini' or 'openai'). Defaults to settings.model_provider.
        model_name: Model name identifier. Defaults to settings.model_name.
        api_key: Provider API key. Defaults to settings.model_api_key or standard env vars.
    """
    if override_model is not None:
        return override_model

    target_provider = (provider or settings.model_provider).lower()
    target_model = model_name or settings.model_name

    if target_provider in ["google_genai", "gemini"]:
        key = (
            api_key
            or settings.model_api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not key:
            raise ModelConfigurationError(
                "Missing API key for Google GenAI model provider. "
                "Set GEMINI_API_KEY or ORCHESTRATOR_MODEL_API_KEY in environment."
            )
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise ModelConfigurationError(
                f"langchain_google_genai is not installed: {str(exc)}"
            )
        return ChatGoogleGenerativeAI(
            model=target_model,
            google_api_key=key,
            temperature=0.0,
        )

    elif target_provider == "openai":
        key = (
            api_key
            or settings.model_api_key
            or os.environ.get("OPENAI_API_KEY")
        )
        if not key:
            raise ModelConfigurationError(
                "Missing API key for OpenAI model provider. "
                "Set OPENAI_API_KEY or ORCHESTRATOR_MODEL_API_KEY in environment."
            )
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise ModelConfigurationError(
                f"langchain_openai is not installed: {str(exc)}"
            )
        return ChatOpenAI(
            model=target_model,
            api_key=key,
            temperature=0.0,
        )

    elif target_provider == "mock":
        raise ModelConfigurationError(
            "Model provider is set to 'mock' and no override_model was injected. "
            "Configure a real provider (e.g. google_genai or openai) with API credentials, "
            "or inject an override_model for test execution."
        )

    else:
        raise ModelConfigurationError(
            f"Unsupported model provider '{target_provider}'. "
            "Supported providers are 'google_genai' and 'openai'."
        )
