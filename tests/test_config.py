from __future__ import annotations

import pytest
from app.config import Settings


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_NEWS_FEED_URL", "https://test.supabase.co/functions/v1/feed")
    monkeypatch.setenv("SUPABASE_ARTICLE_LOOKUP_URL", "https://test.supabase.co/functions/v1/lookup")
    monkeypatch.delenv("SUPABASE_FUNCTION_AUTH_TOKEN", raising=False)
    # Clear any model overrides from real .env
    for key in (
        "OPENAI_MODEL_ARTICLE_DATA_AGENT",
        "OPENAI_MODEL_STORY_CLUSTER_AGENT",
        "OPENAI_MODEL_EDITORIAL_ORCHESTRATOR_AGENT",
        "OPENAI_MODEL_ARTICLE_WRITER_AGENT",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=None)


class TestSettings:
    def test_loads_from_env(self, fake_settings: Settings) -> None:
        assert fake_settings.openai_api_key.get_secret_value() == "sk-test"
        assert fake_settings.top_n == 5
        assert fake_settings.lookback_hours == 2

    def test_agent_model_returns_correct_model(self, fake_settings: Settings) -> None:
        assert fake_settings.agent_model("article_data_agent") == "gpt-5-nano-2025-08-07"
        assert fake_settings.agent_model("editorial_orchestrator_agent") == "gpt-5.2-2025-12-11"

    def test_agent_model_raises_on_unknown(self, fake_settings: Settings) -> None:
        with pytest.raises(ValueError, match="Unknown agent name"):
            fake_settings.agent_model("nonexistent_agent")

    def test_agent_models_contains_all_agents(self, fake_settings: Settings) -> None:
        models = fake_settings.agent_models()
        assert set(models.keys()) == {
            "article_data_agent",
            "story_cluster_agent",
            "editorial_orchestrator_agent",
            "article_writer_agent",
            "persona_selector_agent",
        }

    def test_validates_url_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
        monkeypatch.setenv("SUPABASE_NEWS_FEED_URL", "https://test.supabase.co/functions/v1/same")
        monkeypatch.setenv("SUPABASE_ARTICLE_LOOKUP_URL", "https://test.supabase.co/functions/v1/same")
        # SUPABASE_FUNCTION_AUTH_TOKEN intentionally omitted — falls back to service role key
        with pytest.raises(Exception, match="must differ"):
            Settings(_env_file=None)

    def test_resolved_function_auth_token_fallback(self, fake_settings: Settings) -> None:
        assert fake_settings.supabase_function_auth_token is None
        assert fake_settings.resolved_function_auth_token() == "test-key"

    def test_resolved_function_auth_token_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
        monkeypatch.setenv("SUPABASE_NEWS_FEED_URL", "https://test.supabase.co/functions/v1/feed")
        monkeypatch.setenv("SUPABASE_ARTICLE_LOOKUP_URL", "https://test.supabase.co/functions/v1/lookup")
        monkeypatch.setenv("SUPABASE_FUNCTION_AUTH_TOKEN", "explicit-token")
        s = Settings(_env_file=None)
        assert s.resolved_function_auth_token() == "explicit-token"
