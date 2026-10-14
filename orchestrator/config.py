from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class OrchestratorSettings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8000
    mock_api_url: str = "http://127.0.0.1:8001"
    # Accept the legacy MODEL_* names used by existing local .env files while
    # preferring the documented ORCHESTRATOR_MODEL_* names when both are set.
    model_provider: str = Field(default="mock", validation_alias=AliasChoices("ORCHESTRATOR_MODEL_PROVIDER", "MODEL_PROVIDER"))
    model_name: str = Field(default="gemini-2.5-flash", validation_alias=AliasChoices("ORCHESTRATOR_MODEL_NAME", "MODEL_NAME"))
    model_api_key: Optional[str] = Field(default=None, repr=False, validation_alias=AliasChoices("ORCHESTRATOR_MODEL_API_KEY", "MODEL_API_KEY"))
    sqlite_db_path: str = "driftguard.sqlite3"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    assignment_token: Optional[str] = Field(default=None, repr=False)

    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        env_file=".env",
        extra="ignore"
    )


settings = OrchestratorSettings()
