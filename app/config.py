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

    # T4L Daily Briefing — personal NFL podcast feature.
    # The podcast pipeline lives in app/podcast and app/delivery. It pulls
    # the same raw NFL feed the editorial cycle uses, re-clusters league-
    # wide, generates a two-persona script (color + analyst), and renders
    # audio via a *direct* Gemini multi-speaker TTS call (NOT the team-beat
    # Cloud Run worker). MVP is personal-only; the Spotify delivery uses
    # the Save-to-Spotify CLI installed on the VPS.
    podcast_default_language: str = "en-US"
    podcast_target_word_count: int = 4200       # ~25 min runtime
    podcast_min_word_count: int = 700           # ~5 min adaptive floor
    podcast_lookback_hours: int = 24
    podcast_audio_temp_dir: Path = Path("/tmp/t4l_podcast")
    # Direct Gemini TTS integration (separate from the team-beat Cloud Run
    # tts_batch_service). The model + voice settings here are unrelated to
    # `tts_model_name` / `tts_voice_name`, which belong to team-beat. Voice
    # names (e.g. "Puck", "Charon") come from Gemini's prebuilt voice list.
    podcast_gemini_tts_model: str = "gemini-3.1-flash-tts-preview"
    # Two grounded former-athlete podcaster voices. Both male, both
    # carry chest-resonance authority. Marcus (color/host) drives
    # narrative; the analyst (Ray) drives technical/metrics breakdown.
    # The Gemini prebuilt voice catalogue ships ~30 voices — common
    # picks for low/grounded male: Zubenelgenubi, umbriel, Charon,
    # Orus, Algenib, Algieba, Sadachbia, Achird.
    podcast_gemini_voice_color: str = "Zubenelgenubi"
    podcast_gemini_voice_analyst: str = "umbriel"
    # Natural-language register hint prepended to every style prompt.
    # Gemini TTS doesn't expose a pitch dial, so we steer register via
    # prompt direction. Tune the strength via env if voices are still
    # too high — e.g. "deep baritone, chest voice, lower register".
    podcast_voice_register_hint: str = (
        "Both voices sit in a LOW baritone register. Pitch is deep, "
        "grounded in the chest cavity — not throat, not nasal. Think "
        "veteran sports-talk radio: warm, low resonance, no upward "
        "lift on emphasis (emphasis comes from VOLUME, not PITCH)."
    )
    podcast_gemini_poll_interval_seconds: float = 10.0
    podcast_gemini_timeout_seconds: float = 1800.0  # 30 min ceiling
    # Gemini's TTS preview API occasionally returns 500/503 INTERNAL
    # errors that are transient. Retry with exponential backoff so a
    # single hiccup doesn't kill an episode that's 14 chunks in.
    podcast_gemini_max_retries: int = 4
    podcast_gemini_retry_base_seconds: float = 4.0
    # Fallback path: if Gemini multi-speaker is flaky, render a single voice
    # with inline character tags. Off by default; flip to True via env if
    # the multi-speaker mode misbehaves.
    podcast_force_single_voice: bool = False

    # Background music + sting. Two pipelines depending on configuration:
    #
    # 1. SINGLE-SONG MODE (podcast_intro_song_mode=True, default):
    #    `podcast_intro_music_path` points to a single song that carries
    #    the brand vocal at its head. The pipeline is:
    #      0..vocal_intro_seconds   → music solo (brand vocal heard)
    #      vocal..vocal+headlines   → music pitched/slowed, voice mixed
    #      vocal+headlines..+sting  → music returns to normal pitch
    #      last fade_out_seconds    → fade to silence
    #    `podcast_sting_music_path` is ignored in this mode.
    #
    # 2. TWO-FILE MODE (podcast_intro_song_mode=False):
    #    Legacy: separate intro_music (bed) + sting_music (transition).
    podcast_intro_music_path: Path | None = None
    podcast_sting_music_path: Path | None = None
    podcast_intro_song_mode: bool = True
    # Single-song mode parameters
    podcast_song_vocal_intro_seconds: float = 7.0  # length of the brand-vocal head
    # Smooth transition (seconds) between full volume and bed level —
    # used at BOTH the intro→bed and bed→sting boundaries so the music
    # never has a hard volume cut.
    podcast_song_transition_seconds: float = 1.0
    podcast_song_sting_seconds: float = 25.0  # post-headlines solo at full volume
    podcast_song_fade_out_seconds: float = 3.0
    # Two-file mode parameters (legacy)
    podcast_intro_solo_seconds: float = 4.0
    podcast_intro_tail_seconds: float = 1.5
    # Bed volume under voice — at -26 dB the music sits clearly behind
    # the two hosts so intelligibility wins, while still being audibly
    # present. Tune via .env if voices are still fighting the music.
    podcast_music_bed_volume_db: float = -26.0
    podcast_sting_max_seconds: float = 30.0
    podcast_sting_fade_out_seconds: float = 2.0

    # Brand-line standalone padding: silence inserted right after the
    # branded intro line so it lands on its own before the headlines.
    podcast_brand_line_pause_seconds: float = 0.9

    # Audio post-processing — EBU R128 loudness target for podcasts
    # is -16 LUFS integrated, with a true-peak ceiling at -1 dBTP.
    # `loudnorm` (ffmpeg) implements R128 in a single pass; we follow
    # with a `alimiter` for safety. A high-pass at 80 Hz removes
    # rumble. Disable by setting podcast_postprocess_enabled=False.
    podcast_postprocess_enabled: bool = True
    podcast_postprocess_target_lufs: float = -16.0
    podcast_postprocess_true_peak_db: float = -1.0
    podcast_postprocess_loudness_range_lu: float = 11.0
    podcast_postprocess_highpass_hz: int = 80

    # Long-form TTS quality control via chunking. Gemini multi-speaker
    # output quality degrades on very long single generations; we cut
    # the body into chunks at speaker boundaries and render each chunk
    # as its own Gemini call. Empirically ~1100 chars per chunk keeps
    # pacing tight and consistent throughout a 25-min episode.
    podcast_tts_chunk_max_chars: int = 1100
    # Path to the ffmpeg / ffprobe binaries. Most VPS installs have
    # them on PATH; override if you keep them somewhere odd.
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    # Agent models (resolved by `agent_model("podcast_…")`).
    openai_model_podcast_cluster_ranker_agent: str = "gpt-5.4-mini"
    openai_model_podcast_cold_open_writer_agent: str = "gpt-5.4-mini"
    openai_model_podcast_dialogue_writer_agent: str = "gpt-5.4"
    openai_model_podcast_director_pass_agent: str = "gpt-5.4-mini"
    openai_model_podcast_episode_metadata_agent: str = "gpt-5.4-mini"

    # Delivery (Save-to-Spotify CLI on the VPS).
    spotify_token_path: Path = Path.home() / ".config" / "save-to-spotify" / "token.json"
    save_to_spotify_cli_path: str = "save-to-spotify"
    save_to_spotify_show_id: str | None = None

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
            "podcast_cluster_ranker_agent": self.openai_model_podcast_cluster_ranker_agent,
            "podcast_cold_open_writer_agent": self.openai_model_podcast_cold_open_writer_agent,
            "podcast_dialogue_writer_agent": self.openai_model_podcast_dialogue_writer_agent,
            "podcast_director_pass_agent": self.openai_model_podcast_director_pass_agent,
            "podcast_episode_metadata_agent": self.openai_model_podcast_episode_metadata_agent,
        }

    def agent_model(self, agent_name: str) -> str:
        try:
            return self.agent_models()[agent_name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent name: {agent_name}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
