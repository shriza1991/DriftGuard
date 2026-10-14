from pydantic_settings import BaseSettings, SettingsConfigDict


class MockApiSettings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8001
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_prefix="MOCK_API_",
        env_file=".env",
        extra="ignore"
    )


settings = MockApiSettings()
