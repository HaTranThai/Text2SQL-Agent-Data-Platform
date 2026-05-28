from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+psycopg://fintextsql:fintextsql@localhost:5432/fintextsql",
        validation_alias="DATABASE_URL",
    )
    llm_base_url: str = Field(default="http://localhost:20128/v1", validation_alias="LLM_BASE_URL")
    llm_api_key: str = Field(default="", validation_alias="LLM_API_KEY")
    llm_model: str = Field(default="local-model", validation_alias="LLM_MODEL")
    llm_timeout_seconds: int = Field(default=60, validation_alias="LLM_TIMEOUT_SECONDS")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="CORS_ORIGINS",
    )
    max_sql_rows: int = Field(default=5000, validation_alias="MAX_SQL_ROWS")
    tavily_api_key: str = Field(default="", validation_alias="TAVILY_API_KEY")
    tavily_timeout_seconds: int = Field(default=15, validation_alias="TAVILY_TIMEOUT_SECONDS")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
