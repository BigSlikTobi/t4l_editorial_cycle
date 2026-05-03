from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AnyHttpUrl, SecretStr
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
    # Shared bearer token that authenticates submit+poll calls against the
    # extraction cloud functions. The token must match what each function
    # verifies in its own runtime env — not a Supabase/OpenAI credential.
    extraction_function_auth_token: SecretStr | None = None
    ingestion_max_articles_per_run: int = 200
    ingestion_knowledge_max_concurrency: int = 4

    top_n: int = 5
    lookback_hours: int = 2
    news_timeout_seconds: float = 15.0

    openai_model_article_data_agent: str = "gpt-5.4-nano"
    openai_model_story_cluster_agent: str = "gpt-5.4-mini"
    openai_model_editorial_orchestrator_agent: str = "gpt-5.4"
    openai_model_article_writer_agent: str = "gpt-5.4-mini"
    openai_model_persona_selector_agent: str = "gpt-5.4-mini"
    openai_model_article_quality_gate_agent: str = "gpt-5.4-mini"
    openai_model_editorial_memory_agent: str = "gpt-5.4-mini"
    openai_model_team_beat_reporter_agent: str = "gpt-5.4-mini"
    openai_model_radio_script_agent: str = "gpt-5.4-mini"
    editorial_memory_dir: Path = Path("editorial_memory")

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

    # Team Beat — gemini_tts_batch_service (sibling Cloud Run deployment).
    # Submit + poll URLs front the three-stage create→status→process lifecycle
    # documented in tackle_4_loss_intelligence/.../gemini_tts_batch_service/README.md.
    # The bearer token authenticates submit+poll calls; it is *not* a Gemini
    # or Supabase credential. Storage bucket is caller-chosen and lives in our
    # Supabase project.
    tts_batch_submit_url: AnyHttpUrl | None = None
    tts_batch_poll_url: AnyHttpUrl | None = None
    tts_batch_function_auth_token: SecretStr | None = None
    tts_model_name: str = "gemini-3.1-flash-tts-preview"
    tts_voice_name: str = "Kore"
    tts_storage_bucket: str = "team-beat-audio"
    tts_storage_path_prefix: str = "gemini-tts-batch"
    # Status-poll cadence between create and process: how often we ask the
    # batch service to re-read the upstream Gemini batch state. The batch
    # service's own /poll cycle is governed by extraction_poll_interval_seconds.
    tts_status_poll_interval_seconds: float = 30.0
    tts_status_poll_timeout_seconds: float = 1800.0  # 30 min
    # Per-stage timeouts for the underlying AsyncJobClient submit→poll cycle.
    # Process involves downloading + uploading every MP3, so allow some headroom.
    # Status action is fast (one Gemini state read).
    tts_create_timeout_seconds: float = 1800.0   # 30 min  (legacy field, kept for tests)
    tts_status_action_timeout_seconds: float = 60.0  # per status check
    tts_process_timeout_seconds: float = 900.0   # 15 min
    # Short poll for the happy-path create cycle. The TTS service worker
    # *should* return the Gemini batch_id within 1-2 min of submit. If the
    # worker stalls past this, we fall back to listing Gemini batches via
    # the Gemini API directly (see TTSBatchClient.discover_recent_batch_id).
    # Either way the produce cycle exits in under ~3 min and the audio is
    # harvested by team-beat-harvest.yml on its own cron.
    tts_create_short_timeout_seconds: float = 120.0   # 2 min

    def agent_models(self) -> dict[str, str]:
        return {
            "article_data_agent": self.openai_model_article_data_agent,
            "story_cluster_agent": self.openai_model_story_cluster_agent,
            "editorial_orchestrator_agent": self.openai_model_editorial_orchestrator_agent,
            "article_writer_agent": self.openai_model_article_writer_agent,
            "persona_selector_agent": self.openai_model_persona_selector_agent,
            "article_quality_gate_agent": self.openai_model_article_quality_gate_agent,
            "editorial_memory_agent": self.openai_model_editorial_memory_agent,
            "team_beat_reporter_agent": self.openai_model_team_beat_reporter_agent,
            "radio_script_agent": self.openai_model_radio_script_agent,
        }

    def agent_model(self, agent_name: str) -> str:
        try:
            return self.agent_models()[agent_name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent name: {agent_name}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
