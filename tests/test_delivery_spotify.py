from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.delivery.spotify import (
    SaveToSpotifyDelivery,
    _build_argv,
    _parse_episode_id,
    _spotify_lang_code,
)


def _settings(tmp_path: Path) -> Settings:
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        openai_api_key="sk-test",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="sk",
        spotify_token_path=token,
        save_to_spotify_cli_path="save-to-spotify",
    )


class TestSpotifyLangCode:
    def test_strips_region(self) -> None:
        assert _spotify_lang_code("en-US") == "en"
        assert _spotify_lang_code("de-DE") == "de"

    def test_passthrough_simple(self) -> None:
        assert _spotify_lang_code("en") == "en"

    def test_none(self) -> None:
        assert _spotify_lang_code(None) is None
        assert _spotify_lang_code("") is None


class TestBuildArgv:
    def test_minimal_argv_positional_file(self, tmp_path: Path) -> None:
        argv = _build_argv(
            cli_path="save-to-spotify",
            audio_path="/tmp/x.wav",
            title="t",
            summary="d",
            show_id=None,
            language=None,
        )
        # File is positional, right after `upload`.
        assert argv[:3] == ["save-to-spotify", "upload", "/tmp/x.wav"]
        assert "--title" in argv and "--summary" in argv
        assert "--json" in argv  # structured output for scripting
        assert "--audio" not in argv  # NOT a flag in the real CLI
        assert "--description" not in argv  # field is --summary
        assert "--token-path" not in argv  # token is XDG-discovered
        assert "--show-id" not in argv
        assert "--language" not in argv
        assert "--image" not in argv

    def test_with_show_id(self, tmp_path: Path) -> None:
        argv = _build_argv(
            cli_path="cli",
            audio_path="/tmp/x.wav",
            title="t",
            summary="d",
            show_id="show-42",
            language=None,
        )
        assert "--show-id" in argv
        assert argv[argv.index("--show-id") + 1] == "show-42"

    def test_with_language_strips_region(self, tmp_path: Path) -> None:
        argv = _build_argv(
            cli_path="cli",
            audio_path="/tmp/x.wav",
            title="t",
            summary="d",
            show_id=None,
            language="de-DE",
        )
        assert "--language" in argv
        assert argv[argv.index("--language") + 1] == "de"

    def test_with_image_path(self, tmp_path: Path) -> None:
        argv = _build_argv(
            cli_path="cli",
            audio_path="/tmp/x.wav",
            title="t",
            summary="d",
            show_id=None,
            language=None,
            image_path="/tmp/cover.png",
        )
        assert "--image" in argv
        assert argv[argv.index("--image") + 1] == "/tmp/cover.png"


class TestParseEpisodeId:
    def test_json_episode_id(self) -> None:
        assert _parse_episode_id('{"episode_id": "spot-abc"}') == "spot-abc"

    def test_json_episode_uri(self) -> None:
        assert (
            _parse_episode_id('{"episode_uri": "spotify:episode:abc123"}')
            == "spotify:episode:abc123"
        )

    def test_json_id_key(self) -> None:
        assert _parse_episode_id('{"id": "spot-xyz"}') == "spot-xyz"

    def test_nested_episode_object(self) -> None:
        assert (
            _parse_episode_id('{"episode": {"id": "spot-nested"}}')
            == "spot-nested"
        )

    def test_spotify_uri_format(self) -> None:
        # Spotify URIs use colons; the regex char class includes them.
        assert (
            _parse_episode_id("episode_id: spotify:episode:abc123")
            == "spotify:episode:abc123"
        )

    def test_text_fallback(self) -> None:
        assert _parse_episode_id("Uploaded! episode_id: spot-789") == "spot-789"

    def test_none_when_unparseable(self) -> None:
        assert _parse_episode_id("hello world") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_episode_id("") is None


@pytest.mark.asyncio
class TestSaveToSpotifyDelivery:
    async def test_dry_run_does_not_invoke(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        delivery = SaveToSpotifyDelivery(settings)

        with patch(
            "app.delivery.spotify.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as mock_exec:
            result = await delivery.dispatch(
                audio_path=str(tmp_path / "x.wav"),
                title="t",
                summary="d",
                dry_run=True,
            )
            mock_exec.assert_not_called()

        assert result.success is True
        assert result.invocation and "DRY RUN" in result.invocation

    async def test_missing_audio_returns_failure(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        delivery = SaveToSpotifyDelivery(settings)

        result = await delivery.dispatch(
            audio_path=str(tmp_path / "missing.wav"),
            title="t",
            summary="d",
            dry_run=False,
        )

        assert result.success is False
        assert "audio file not found" in (result.error_message or "")

    async def test_missing_token_returns_friendly_error(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        # Remove the token after settings construction.
        settings.spotify_token_path.unlink()
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"\x00\x00")

        delivery = SaveToSpotifyDelivery(settings)
        result = await delivery.dispatch(
            audio_path=str(audio),
            title="t",
            summary="d",
            dry_run=False,
        )
        assert result.success is False
        assert "Spotify token not found" in (result.error_message or "")
        assert "save-to-spotify auth login" in (result.error_message or "")

    async def test_missing_image_returns_failure(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"\x00\x00")

        delivery = SaveToSpotifyDelivery(settings)
        result = await delivery.dispatch(
            audio_path=str(audio),
            title="t",
            summary="d",
            image_path=str(tmp_path / "missing.png"),
            dry_run=False,
        )

        assert result.success is False
        assert "image file not found" in (result.error_message or "")

    async def test_subprocess_success(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"\x00\x00")

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b'{"episode_id": "abc-1"}', b"")
        )

        async def fake_exec(*args: Any, **kwargs: Any) -> Any:
            return proc

        delivery = SaveToSpotifyDelivery(settings)
        with patch(
            "app.delivery.spotify.asyncio.create_subprocess_exec",
            new=fake_exec,
        ):
            result = await delivery.dispatch(
                audio_path=str(audio),
                title="t",
                summary="d",
                dry_run=False,
                language="en-US",
            )

        assert result.success is True
        assert result.spotify_episode_id == "abc-1"

    async def test_subprocess_failure_captures_stderr(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"\x00\x00")

        proc = MagicMock()
        proc.returncode = 7
        proc.communicate = AsyncMock(
            return_value=(b"", b"Spotify API rejected upload: rate limit"),
        )

        async def fake_exec(*args: Any, **kwargs: Any) -> Any:
            return proc

        delivery = SaveToSpotifyDelivery(settings)
        with patch(
            "app.delivery.spotify.asyncio.create_subprocess_exec",
            new=fake_exec,
        ):
            result = await delivery.dispatch(
                audio_path=str(audio),
                title="t",
                summary="d",
                dry_run=False,
            )

        assert result.success is False
        assert "exited 7" in (result.error_message or "")
        assert "rate limit" in (result.error_message or "")

    async def test_cli_not_found_error(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        audio = tmp_path / "x.wav"
        audio.write_bytes(b"\x00\x00")

        async def fake_exec(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("save-to-spotify")

        delivery = SaveToSpotifyDelivery(settings)
        with patch(
            "app.delivery.spotify.asyncio.create_subprocess_exec",
            new=fake_exec,
        ):
            result = await delivery.dispatch(
                audio_path=str(audio),
                title="t",
                summary="d",
                dry_run=False,
            )

        assert result.success is False
        assert "CLI not found" in (result.error_message or "")
        assert "install" in (result.error_message or "").lower()
