from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.clients import gemini_tts as gtts


def _make_pcm(seconds: float) -> bytes:
    """Generate `seconds` of silent 16-bit mono PCM @ 24 kHz."""
    n_samples = int(seconds * 24_000)
    return b"\x00\x00" * n_samples


def _fake_response_with_pcm(pcm: bytes) -> Any:
    """Build a Gemini-shaped response object with one audio part."""
    inline = MagicMock()
    inline.data = pcm
    part = MagicMock()
    part.inline_data = inline
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    response = MagicMock()
    response.candidates = [candidate]
    return response


class TestExtractPcmBytes:
    def test_extracts_single_part(self) -> None:
        response = _fake_response_with_pcm(b"\x01\x02\x03\x04")
        assert gtts._extract_pcm_bytes(response) == b"\x01\x02\x03\x04"

    def test_concatenates_multiple_parts(self) -> None:
        inline_a = MagicMock(); inline_a.data = b"\xaa"
        inline_b = MagicMock(); inline_b.data = b"\xbb"
        part_a = MagicMock(); part_a.inline_data = inline_a
        part_b = MagicMock(); part_b.inline_data = inline_b
        content = MagicMock(); content.parts = [part_a, part_b]
        candidate = MagicMock(); candidate.content = content
        response = MagicMock(); response.candidates = [candidate]
        assert gtts._extract_pcm_bytes(response) == b"\xaa\xbb"

    def test_no_candidates_raises(self) -> None:
        response = MagicMock(); response.candidates = []
        with pytest.raises(gtts.GeminiTTSEmptyResponse):
            gtts._extract_pcm_bytes(response)

    def test_no_audio_parts_raises(self) -> None:
        content = MagicMock(); content.parts = []
        candidate = MagicMock(); candidate.content = content
        response = MagicMock(); response.candidates = [candidate]
        with pytest.raises(gtts.GeminiTTSEmptyResponse):
            gtts._extract_pcm_bytes(response)


class TestWriteWav:
    def test_writes_valid_wav(self, tmp_path: Path) -> None:
        pcm = _make_pcm(0.5)  # half a second
        out = tmp_path / "test.wav"
        duration = gtts._write_wav(out, pcm)
        assert duration == 0  # rounds down; 0.5s -> 0
        assert out.exists()
        with wave.open(str(out), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == 24_000
            assert w.getsampwidth() == 2

    def test_one_second_duration(self, tmp_path: Path) -> None:
        pcm = _make_pcm(1.0)
        out = tmp_path / "one.wav"
        duration = gtts._write_wav(out, pcm)
        assert duration == 1


class TestFormatTranscript:
    def test_multi_speaker_format(self) -> None:
        lines = [("color", "Welcome in."), ("analyst", "Numbers ready.")]
        rendered = gtts._format_transcript(lines)
        assert rendered.splitlines() == ["color: Welcome in.", "analyst: Numbers ready."]

    def test_single_voice_uses_brackets(self) -> None:
        lines = [("color", "A"), ("analyst", "B")]
        rendered = gtts._format_transcript_single_voice(lines)
        assert "[COLOR] A" in rendered
        assert "[ANALYST] B" in rendered

    def test_skips_empty_text(self) -> None:
        lines = [("color", ""), ("analyst", "B")]
        assert gtts._format_transcript(lines).splitlines() == ["analyst: B"]

    def test_style_prompt_prepended(self) -> None:
        rendered = gtts._format_transcript(
            [("color", "Hi.")],
            style_prompt="Two hosts. color is warm. analyst is dry.",
        )
        # Style prompt comes first, blank line, then transcript.
        lines = rendered.splitlines()
        assert lines[0].startswith("Two hosts.")
        assert lines[1] == ""
        assert lines[2] == "color: Hi."

    def test_style_prompt_optional(self) -> None:
        # Same lines without style prompt → no leading paragraph.
        rendered = gtts._format_transcript([("color", "Hi.")])
        assert rendered == "color: Hi."

    def test_single_voice_style_prompt(self) -> None:
        rendered = gtts._format_transcript_single_voice(
            [("color", "Hi.")],
            style_prompt="Read warmly.",
        )
        assert rendered.splitlines()[0] == "Read warmly."
        assert "[COLOR] Hi." in rendered


class TestBuildSpeechConfig:
    def test_two_speakers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        types_mod = MagicMock()
        config = gtts._build_speech_config(types_mod, {"color": "Puck", "analyst": "Charon"})
        # Two SpeakerVoiceConfig invocations.
        assert types_mod.SpeakerVoiceConfig.call_count == 2

    def test_more_than_two_speakers_raises(self) -> None:
        types_mod = MagicMock()
        with pytest.raises(gtts.GeminiTTSError):
            gtts._build_speech_config(types_mod, {"a": "v1", "b": "v2", "c": "v3"})


class TestGeminiTTSClientRender:
    @pytest.mark.asyncio
    async def test_render_multi_speaker_writes_wav(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock the SDK: client.models.generate_content returns a fake response.
        fake_response = _fake_response_with_pcm(_make_pcm(2.0))
        fake_models = MagicMock()
        fake_models.generate_content = MagicMock(return_value=fake_response)
        fake_client_obj = MagicMock()
        fake_client_obj.models = fake_models

        monkeypatch.setattr(gtts, "_gemini_client", lambda api_key: fake_client_obj)
        # types_mod also needs to be mockable; the client only uses it to
        # build config objects, so plain MagicMock is fine.
        monkeypatch.setattr(gtts, "_gemini_types", MagicMock)

        client = gtts.GeminiTTSClient(api_key="k", model="gemini-test", timeout_seconds=10.0)
        out = tmp_path / "ep.wav"
        result = await client.render_multi_speaker(
            transcript_lines=[("color", "Hello."), ("analyst", "Yes.")],
            voice_map={"color": "Puck", "analyst": "Charon"},
            output_path=out,
        )
        assert result.audio_path == out
        assert result.duration_seconds == 2
        assert out.exists()

    @pytest.mark.asyncio
    async def test_empty_transcript_raises(self, tmp_path: Path) -> None:
        client = gtts.GeminiTTSClient(api_key="k", model="gemini-test", timeout_seconds=10.0)
        with pytest.raises(gtts.GeminiTTSError):
            await client.render_multi_speaker(
                transcript_lines=[],
                voice_map={"color": "Puck"},
                output_path=tmp_path / "x.wav",
            )

    @pytest.mark.asyncio
    async def test_timeout_wraps_to_typed_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def slow_thread(_call: Any) -> Any:
            await asyncio.sleep(10)
            return None

        monkeypatch.setattr(gtts, "_gemini_client", lambda api_key: MagicMock())
        monkeypatch.setattr(gtts, "_gemini_types", MagicMock)
        monkeypatch.setattr(gtts.asyncio, "to_thread", slow_thread)

        # max_retries=1 keeps this test snappy — we just want to verify
        # the timeout wraps into GeminiTTSTimeout, not exercise retries.
        client = gtts.GeminiTTSClient(
            api_key="k", model="m", timeout_seconds=0.05, max_retries=1,
        )
        with pytest.raises(gtts.GeminiTTSTimeout):
            await client.render_multi_speaker(
                transcript_lines=[("color", "Hi")],
                voice_map={"color": "Puck"},
                output_path=tmp_path / "x.wav",
            )

    @pytest.mark.asyncio
    async def test_sdk_error_wrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_models = MagicMock()
        fake_models.generate_content = MagicMock(side_effect=RuntimeError("boom"))
        fake_client_obj = MagicMock()
        fake_client_obj.models = fake_models

        monkeypatch.setattr(gtts, "_gemini_client", lambda api_key: fake_client_obj)
        monkeypatch.setattr(gtts, "_gemini_types", MagicMock)

        # RuntimeError is a non-retryable error (not a Gemini server
        # error), so the loop should raise on the first attempt.
        client = gtts.GeminiTTSClient(
            api_key="k", model="m", timeout_seconds=10.0, max_retries=1,
        )
        with pytest.raises(gtts.GeminiTTSError):
            await client.render_multi_speaker(
                transcript_lines=[("color", "Hi")],
                voice_map={"color": "Puck"},
                output_path=tmp_path / "x.wav",
            )

    @pytest.mark.asyncio
    async def test_retries_on_transient_server_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Server errors retry and a later success closes the call."""
        from google.genai import errors as genai_errors

        fake_response = _fake_response_with_pcm(_make_pcm(0.5))
        # First call: 500 ServerError. Second call: success.
        call_count = {"n": 0}

        def flaky_call(*args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise genai_errors.ServerError(500, {"error": {"message": "x"}}, MagicMock())
            return fake_response

        fake_models = MagicMock()
        fake_models.generate_content = flaky_call
        fake_client_obj = MagicMock()
        fake_client_obj.models = fake_models

        monkeypatch.setattr(gtts, "_gemini_client", lambda api_key: fake_client_obj)
        monkeypatch.setattr(gtts, "_gemini_types", MagicMock)

        client = gtts.GeminiTTSClient(
            api_key="k",
            model="m",
            timeout_seconds=10.0,
            max_retries=3,
            retry_base_seconds=0.01,  # near-zero backoff for fast tests
        )
        out = tmp_path / "ok.wav"
        result = await client.render_multi_speaker(
            transcript_lines=[("color", "Hi")],
            voice_map={"color": "Puck"},
            output_path=out,
        )
        assert call_count["n"] == 2
        assert result.audio_path == out
