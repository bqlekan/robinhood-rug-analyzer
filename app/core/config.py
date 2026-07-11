from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Robinhood Rug Analyzer"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    etherscan_api_key: str = ""
    bscscan_api_key: str = ""
    polygonscan_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
