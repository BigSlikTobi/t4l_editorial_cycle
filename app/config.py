from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Env vars are always strings; referencing a missing GitHub secret resolves
# to "" rather than being absent. Treat empty / whitespace-only values as
# "not set" so optional fields stay None instead of failing URL parsing.
_EMPTY_TO_NONE_URL_FIELDS = (
    "news_extraction_submit_url",
    "news_extraction_poll_url",
    "url_content_extraction_submit_url",
    "url_content_extraction_poll_url",
    "knowledge_extraction_submit_url",
    "knowledge_extraction_poll_url",
    "image_selection_url",
)
_EMPTY_TO_NONE_SECRET_FIELDS = (
    "google_custom_search_key",
    "gemini_api_key",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr
    supabase_url: AnyHttpUrl
    supabase_service_role_key: SecretStr

    # Extraction cloud functions (Google Cloud Functions from
    # tackle_4_loss_intelligence). Each service deploys submit + poll as
    # separate HTTP functions, so we configure both URLs per service.
    # Required to run the ingestion worker.
    news_extraction_submit_url: AnyHttpUrl | None = None
    news_extraction_poll_url: AnyHttpUrl | None = None
    url_content_extraction_submit_url: AnyHttpUrl | None = None
    url_content_extraction_poll_url: AnyHttpUrl | None = None
    knowledge_extraction_submit_url: AnyHttpUrl | None = None
    knowledge_extraction_poll_url: AnyHttpUrl | None = None
    extraction_jobs_table: str = "extraction_jobs"
    extraction_poll_interval_seconds: float = 2.0
    extraction_timeout_seconds: float = 300.0
    ingestion_max_articles_per_run: int = 200

    top_n: int = 5
    lookback_hours: int = 2
    news_timeout_seconds: float = 15.0

    openai_model_article_data_agent: str = "gpt-5.4-nano"
    openai_model_story_cluster_agent: str = "gpt-5.4-mini"
    openai_model_editorial_orchestrator_agent: str = "gpt-5.4"
    openai_model_article_writer_agent: str = "gpt-5.4-mini"
    openai_model_persona_selector_agent: str = "gpt-5.4-mini"

    openai_temperature: float | None = None
    openai_max_tokens: int | None = None

    # Image selection cascade
    image_selection_url: AnyHttpUrl | None = None
    google_custom_search_key: SecretStr | None = None
    google_custom_search_engine_id: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_image_model: str = "gemini-3.1-flash-image-preview"
    openai_model_vision_validator: str = "gpt-5.4-mini"
    image_selection_timeout_seconds: float = 30.0

    @field_validator(*_EMPTY_TO_NONE_URL_FIELDS, mode="before")
    @classmethod
    def _blank_url_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(*_EMPTY_TO_NONE_SECRET_FIELDS, mode="before")
    @classmethod
    def _blank_secret_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def agent_models(self) -> dict[str, str]:
        return {
            "article_data_agent": self.openai_model_article_data_agent,
            "story_cluster_agent": self.openai_model_story_cluster_agent,
            "editorial_orchestrator_agent": self.openai_model_editorial_orchestrator_agent,
            "article_writer_agent": self.openai_model_article_writer_agent,
            "persona_selector_agent": self.openai_model_persona_selector_agent,
        }

    def agent_model(self, agent_name: str) -> str:
        try:
            return self.agent_models()[agent_name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent name: {agent_name}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
