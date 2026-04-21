from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: SecretStr
    supabase_url: AnyHttpUrl
    supabase_service_role_key: SecretStr
    supabase_news_feed_url: AnyHttpUrl
    supabase_article_lookup_url: AnyHttpUrl
    supabase_function_auth_token: SecretStr | None = None

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

    @model_validator(mode="after")
    def validate_supabase_urls(self) -> "Settings":
        if str(self.supabase_article_lookup_url) == str(self.supabase_news_feed_url):
            raise ValueError("SUPABASE_ARTICLE_LOOKUP_URL must differ from SUPABASE_NEWS_FEED_URL")
        return self

    def resolved_function_auth_token(self) -> str:
        """Return the edge-function auth token, falling back to the service role key."""
        if self.supabase_function_auth_token is not None:
            return self.supabase_function_auth_token.get_secret_value()
        return self.supabase_service_role_key.get_secret_value()

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
