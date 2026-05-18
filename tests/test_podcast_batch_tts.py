from __future__ import annotations

import base64
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.podcast.batch_tts import (
    GeminiBatchTTSClient,
    GeminiBatchTTSTimeout,
    build_batch_request,
    extract_audio_bytes_from_batch_response,
)
from app.podcast.render import render_to_audio_with_batch_fallback
from app.podcast.schemas import MultiSpeakerTTSPayload, PodcastScript, RenderResult, ScriptLine


def _payload() -> MultiSpeakerTTSPayload:
    return MultiSpeakerTTSPayload(
        language="de-DE",
        lines=[("Marcus", "Hallo Robin."), ("Robin", "Hallo Marcus.")],
        voice_map={"Marcus": "Kore", "Robin": "Charon"},
        title="Test",
        style_prompt="Deutsch sprechen.",
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        gemini_api_key="gk-test",
        podcast_audio_temp_dir=tmp_path,
        podcast_gemini_batch_enabled=True,
        podcast_gemini_batch_timeout_seconds=1,
    )


def test_batch_poll_defaults_wait_120_minutes() -> None:
    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
    )

    assert settings.podcast_gemini_batch_poll_interval_seconds == 600
    assert settings.podcast_gemini_batch_timeout_seconds == 7200


def test_build_batch_request_contains_audio_generation_config() -> None:
    request = build_batch_request("chunk-001", _payload())
    assert request["key"] == "chunk-001"
    generation_config = request["request"]["generation_config"]
    assert generation_config["responseModalities"] == ["AUDIO"]
    assert "speechConfig" in generation_config
    text = request["request"]["contents"][0]["parts"][0]["text"]
    assert "Marcus: Hallo Robin." in text


def test_extract_audio_bytes_from_batch_response_accepts_inline_data() -> None:
    audio = b"\x00\x01\x02"
    row = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": base64.b64encode(audio).decode("ascii"),
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    assert extract_audio_bytes_from_batch_response(row) == audio


@pytest.mark.asyncio
async def test_batch_timeout_falls_back_to_sync_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = PodcastScript(
        language="de-DE",
        run_date=date(2026, 5, 13),
        body=[ScriptLine(speaker="color", text="Tape on, let's go.")],
    )

    async def fake_batch(*args, **kwargs):
        raise GeminiBatchTTSTimeout("too slow")

    async def fake_sync(*args, **kwargs):
        audio = tmp_path / "sync.wav"
        audio.write_bytes(b"not-a-real-wav")
        return RenderResult(str(audio), 1, "audio/wav")

    monkeypatch.setattr("app.podcast.render._render_to_audio_batch", fake_batch)
    monkeypatch.setattr("app.podcast.render.render_to_audio", fake_sync)

    status_path = tmp_path / "tts_status.json"
    result = await render_to_audio_with_batch_fallback(
        script,
        run_date=script.run_date,
        settings=_settings(tmp_path),
        status_path=status_path,
    )

    assert result.audio_path.endswith("sync.wav")
    status = status_path.read_text(encoding="utf-8")
    assert '"mode": "sync"' in status
    assert "too slow" in status


@pytest.mark.asyncio
async def test_batch_timeout_waits_ten_minutes_before_each_of_twelve_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.podcast.batch_tts as batch_tts

    sleeps: list[float] = []
    get_calls = 0

    class FakeLoop:
        now = 0.0

        def time(self) -> float:
            return self.now

    fake_loop = FakeLoop()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_loop.now += seconds

    async def fake_to_thread(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    class FakeFiles:
        def upload(self, **kwargs):
            return SimpleNamespace(name="files/request-jsonl")

    class FakeBatches:
        def create(self, **kwargs):
            return SimpleNamespace(name="batches/podcast", state="JOB_STATE_PENDING")

        def get(self, **kwargs):
            nonlocal get_calls
            get_calls += 1
            return SimpleNamespace(name="batches/podcast", state="JOB_STATE_PENDING")

    fake_client = SimpleNamespace(files=FakeFiles(), batches=FakeBatches())
    fake_types = SimpleNamespace(UploadFileConfig=lambda **kwargs: kwargs)

    monkeypatch.setattr(batch_tts, "_gemini_client", lambda api_key: fake_client)
    monkeypatch.setattr(batch_tts, "_gemini_types", lambda: fake_types)
    monkeypatch.setattr(batch_tts, "build_batch_request", lambda key, payload: {"key": key})
    monkeypatch.setattr(batch_tts.asyncio, "get_running_loop", lambda: fake_loop)
    monkeypatch.setattr(batch_tts.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(batch_tts.asyncio, "to_thread", fake_to_thread)

    client = GeminiBatchTTSClient(
        api_key="gk-test",
        model="gemini-3.1-flash-tts-preview",
        poll_interval_seconds=600,
        timeout_seconds=7200,
    )

    with pytest.raises(GeminiBatchTTSTimeout):
        await client.render_payloads(
            payloads=[_payload()],
            output_paths=[tmp_path / "chunk.wav"],
            workdir=tmp_path,
            display_name="podcast",
        )

    assert sleeps == [600] * 12
    assert get_calls == 12


@pytest.mark.asyncio
async def test_batch_status_check_errors_do_not_escalate_before_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.podcast.batch_tts as batch_tts

    sleeps: list[float] = []
    get_calls = 0

    class FakeLoop:
        now = 0.0

        def time(self) -> float:
            return self.now

    fake_loop = FakeLoop()

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_loop.now += seconds

    async def fake_to_thread(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    class FakeFiles:
        def upload(self, **kwargs):
            return SimpleNamespace(name="files/request-jsonl")

    class FakeBatches:
        def create(self, **kwargs):
            return SimpleNamespace(name="batches/podcast", state="JOB_STATE_PENDING")

        def get(self, **kwargs):
            nonlocal get_calls
            get_calls += 1
            raise RuntimeError("temporary status API failure")

    fake_client = SimpleNamespace(files=FakeFiles(), batches=FakeBatches())
    fake_types = SimpleNamespace(UploadFileConfig=lambda **kwargs: kwargs)

    monkeypatch.setattr(batch_tts, "_gemini_client", lambda api_key: fake_client)
    monkeypatch.setattr(batch_tts, "_gemini_types", lambda: fake_types)
    monkeypatch.setattr(batch_tts, "build_batch_request", lambda key, payload: {"key": key})
    monkeypatch.setattr(batch_tts.asyncio, "get_running_loop", lambda: fake_loop)
    monkeypatch.setattr(batch_tts.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(batch_tts.asyncio, "to_thread", fake_to_thread)

    client = GeminiBatchTTSClient(
        api_key="gk-test",
        model="gemini-3.1-flash-tts-preview",
        poll_interval_seconds=600,
        timeout_seconds=7200,
    )

    with pytest.raises(GeminiBatchTTSTimeout):
        await client.render_payloads(
            payloads=[_payload()],
            output_paths=[tmp_path / "chunk.wav"],
            workdir=tmp_path,
            display_name="podcast",
        )

    assert sleeps == [600] * 12
    assert get_calls == 12
