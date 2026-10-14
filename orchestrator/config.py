from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional


class OrchestratorSettings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8000
    mock_api_url: str = "http://127.0.0.1:8001"
    model_provider: str = "mock"
    model_name: str = "gemini-2.5-flash"
    model_api_key: Optional[str] = None
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
