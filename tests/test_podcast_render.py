from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.clients.gemini_tts import GeminiRenderOutcome
from app.config import Settings
from app.podcast.render import (
    _build_continuation_style_prompt,
    _build_style_prompt,
    render_to_audio,
    script_to_payload,
    script_to_segment_payloads,
)
from app.podcast.schemas import PodcastScript, ScriptLine


def _settings(tmp_path: Path, *, force_single: bool = False) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        gemini_api_key="gk-test",
        podcast_audio_temp_dir=tmp_path,
        podcast_force_single_voice=force_single,
    )


def _script() -> PodcastScript:
    return PodcastScript(
        language="en-US",
        run_date=date(2026, 5, 9),
        cold_open=[ScriptLine(speaker="color", text="Big news.")],
        body=[
            ScriptLine(speaker="color", text="Hello.", prosody_hints=["warm"]),
            ScriptLine(speaker="analyst", text="EPA was 0.42.", prosody_hints=["deadpan", "pause"]),
            ScriptLine(speaker="narrator", text="Coerced to color."),
        ],
        outro=[ScriptLine(speaker="analyst", text="Bye.")],
    )


class TestScriptToPayload:
    def test_flattens_all_sections(self, tmp_path: Path) -> None:
        payload = script_to_payload(_script(), settings=_settings(tmp_path), title="t")
        # 5 lines total (cold open + body 3 + outro).
        assert len(payload.lines) == 5

    def test_narrator_coerced_to_color(self, tmp_path: Path) -> None:
        payload = script_to_payload(_script(), settings=_settings(tmp_path), title="t")
        # Find the narrator line. Internal "narrator" ID should be
        # coerced to the color host's Gemini speaker tag ("Marcus").
        narrator_text = "Coerced to color."
        match = [s for (s, t) in payload.lines if t.endswith(narrator_text)]
        assert match == ["Marcus"]

    def test_prosody_hints_inlined(self, tmp_path: Path) -> None:
        payload = script_to_payload(_script(), settings=_settings(tmp_path), title="t")
        # Hello line has hint "warm" prepended.
        hello_line = [t for (s, t) in payload.lines if "Hello." in t][0]
        assert hello_line.startswith("(warm)")

    def test_voice_map_uses_settings(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        payload = script_to_payload(_script(), settings=s, title="t")
        # Keys are first names from the persona bylines so Gemini's
        # multi-speaker mode matches reliably.
        assert payload.voice_map["Marcus"] == s.podcast_gemini_voice_color
        assert payload.voice_map["Robin"] == s.podcast_gemini_voice_analyst

    def test_drops_empty_text(self, tmp_path: Path) -> None:
        script = PodcastScript(
            language="en-US",
            run_date=date(2026, 5, 9),
            body=[
                ScriptLine(speaker="color", text="   "),
                ScriptLine(speaker="analyst", text="Real."),
            ],
        )
        payload = script_to_payload(script, settings=_settings(tmp_path), title="t")
        assert len(payload.lines) == 1

    def test_style_prompt_attached(self, tmp_path: Path) -> None:
        payload = script_to_payload(_script(), settings=_settings(tmp_path), title="t")
        assert payload.style_prompt is not None
        # Header + per-speaker briefs both present.
        assert "TTS the following" in payload.style_prompt
        assert "Marcus Hale" in payload.style_prompt
        assert "Robin Donnelly" in payload.style_prompt


class TestScriptToSegmentPayloads:
    def test_splits_cold_open_from_body_outro(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        cold, body = script_to_segment_payloads(_script(), settings=s, title="t")
        assert cold is not None
        # Cold open had 1 line.
        assert len(cold.lines) == 1
        # Body+outro had 4 lines (3 body including narrator coerced + 1 outro).
        assert len(body.lines) == 4
        # Style prompt is the same on both segments.
        assert cold.style_prompt == body.style_prompt

    def test_no_cold_open_returns_none(self, tmp_path: Path) -> None:
        script = PodcastScript(
            language="en-US",
            run_date=date(2026, 5, 9),
            cold_open=[],
            body=[ScriptLine(speaker="color", text="A.")],
            outro=[],
        )
        cold, body = script_to_segment_payloads(
            script, settings=_settings(tmp_path), title="t"
        )
        assert cold is None
        assert len(body.lines) == 1


@pytest.mark.asyncio
class TestRenderWithMusic:
    async def test_multi_segment_path_when_intro_music_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        intro = tmp_path / "intro.wav"
        intro.write_bytes(b"\x00")
        s = Settings(  # type: ignore[arg-type]
            _env_file=None,
            openai_api_key="sk-test",
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="sk",
            gemini_api_key="gk-test",
            podcast_audio_temp_dir=tmp_path,
            podcast_intro_music_path=intro,
        )

        # Mock the client to record Gemini calls.
        client = AsyncMock()
        client.render_multi_speaker = AsyncMock(
            return_value=GeminiRenderOutcome(
                audio_path=tmp_path / "x.wav", duration_seconds=10
            )
        )
        client.render_single_voice = AsyncMock()

        # Mock compose_episode to record what it received.
        compose_calls: list[dict] = []

        async def fake_compose(**kwargs: Any) -> int:
            compose_calls.append(kwargs)
            return 1500

        monkeypatch.setattr(
            "app.podcast.render.compose_episode", fake_compose
        )

        # Mock the new ffmpeg helpers so the test stays unit-scoped.
        async def fake_concat(paths, *, output_path, music):
            # Touch the output so downstream callers don't trip on
            # missing files (defensive only — compose is also mocked).
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"\x00")

        async def fake_silence(output_path, *, duration_seconds, music):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"\x00")

        monkeypatch.setattr("app.podcast.render._concat_voice_chunks", fake_concat)
        monkeypatch.setattr("app.podcast.render._silence_wav", fake_silence)

        result = await render_to_audio(
            _script(),
            run_date=date(2026, 5, 9),
            settings=s,
            client=client,
            title="Test",
        )
        # At least 2 Gemini calls — cold open + body. With a multi-line
        # cold open the brand-line splits adds a third; this test's
        # `_script()` has only one cold-open line, so 2 is the minimum.
        assert client.render_multi_speaker.await_count >= 2
        # compose_episode was called.
        assert len(compose_calls) == 1
        kwargs = compose_calls[0]
        assert kwargs["cold_open_voice_path"] is not None
        assert kwargs["body_voice_path"] is not None
        # Result reflects compose duration.
        assert result.duration_seconds == 1500

    async def test_no_music_uses_single_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No music paths in settings — single render path.
        s = _settings(tmp_path)

        client = AsyncMock()
        client.render_multi_speaker = AsyncMock(
            return_value=GeminiRenderOutcome(
                audio_path=tmp_path / "x.wav", duration_seconds=10
            )
        )
        client.render_single_voice = AsyncMock()

        compose_called = False

        async def fake_compose(**kwargs: Any) -> int:
            nonlocal compose_called
            compose_called = True
            return 0

        monkeypatch.setattr(
            "app.podcast.render.compose_episode", fake_compose
        )

        result = await render_to_audio(
            _script(),
            run_date=date(2026, 5, 9),
            settings=s,
            client=client,
            title="Test",
        )
        # One Gemini call (whole script in one).
        assert client.render_multi_speaker.await_count == 1
        # compose_episode NOT called.
        assert compose_called is False
        assert result.duration_seconds == 10


class TestBuildContinuationStylePrompt:
    def test_en_forbids_restart_framing(self) -> None:
        p = _build_continuation_style_prompt("en-US")
        # Must explicitly forbid re-introducing the show.
        assert "do NOT introduce" in p
        assert "welcome back" in p
        # Should NOT contain the persona Audio Profile / Scene markers
        # that trigger Gemini to "start the show".
        assert "Audio Profile" not in p
        assert "Scene" not in p
        # Should still pin the speaker mapping.
        assert "Marcus Hale" in p and "Robin Donnelly" in p

    def test_de_forbids_restart_framing(self) -> None:
        p = _build_continuation_style_prompt("de-DE")
        assert "stell dich NICHT vor" in p
        assert "willkommen zurück" in p
        # German-specific markers: accent setup is now in the prompt.
        assert "Berliner" in p and "AMERIKANISCH" in p
        assert "Audio Profile" not in p


class TestBuildStylePrompt:
    def test_en_mentions_both_speakers(self) -> None:
        prompt = _build_style_prompt("en-US")
        assert "Marcus Hale" in prompt
        assert "Robin Donnelly" in prompt
        # Performance-direction language present.
        assert "(laughs)" in prompt or "(sighs)" in prompt
        # Explicit "do not read parens aloud" guard.
        assert "DO NOT" in prompt or "NICHT" in prompt

    def test_de_mentions_both_speakers(self) -> None:
        prompt = _build_style_prompt("de-DE")
        assert "Marcus Hale" in prompt
        assert "Robin Donnelly" in prompt
        # German-specific token.
        assert "Podcast" in prompt
        assert "NICHT" in prompt


@pytest.mark.asyncio
class TestRenderToAudio:
    async def test_calls_multi_speaker(self, tmp_path: Path) -> None:
        s = _settings(tmp_path, force_single=False)
        payload = script_to_payload(_script(), settings=s, title="t")

        client = AsyncMock()
        client.render_multi_speaker = AsyncMock(
            return_value=GeminiRenderOutcome(
                audio_path=tmp_path / "2026-05-09_en-US.wav",
                duration_seconds=42,
            )
        )
        client.render_single_voice = AsyncMock()

        result = await render_to_audio(
            payload, run_date=date(2026, 5, 9), settings=s, client=client
        )
        client.render_multi_speaker.assert_awaited_once()
        client.render_single_voice.assert_not_called()
        assert result.duration_seconds == 42
        assert result.mime_type == "audio/wav"

    async def test_force_single_voice_path(self, tmp_path: Path) -> None:
        s = _settings(tmp_path, force_single=True)
        payload = script_to_payload(_script(), settings=s, title="t")

        client = AsyncMock()
        client.render_single_voice = AsyncMock(
            return_value=GeminiRenderOutcome(
                audio_path=tmp_path / "x.wav",
                duration_seconds=10,
            )
        )
        client.render_multi_speaker = AsyncMock()

        result = await render_to_audio(
            payload, run_date=date(2026, 5, 9), settings=s, client=client
        )
        client.render_single_voice.assert_awaited_once()
        client.render_multi_speaker.assert_not_called()
        assert result.duration_seconds == 10

    async def test_missing_api_key_raises(self, tmp_path: Path) -> None:
        s = Settings(  # type: ignore[arg-type]
            _env_file=None,
            openai_api_key="sk-test",
            supabase_url="https://x.supabase.co",
            supabase_service_role_key="sk",
            gemini_api_key=None,
            podcast_audio_temp_dir=tmp_path,
        )
        payload = script_to_payload(_script(), settings=s, title="t")
        with pytest.raises(RuntimeError, match="gemini_api_key"):
            await render_to_audio(payload, run_date=date(2026, 5, 9), settings=s)
