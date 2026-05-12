from __future__ import annotations

import httpx
import pytest

from app.adapters import PodcastEpisodeWriter


def _writer_with_handler(handler) -> PodcastEpisodeWriter:
    w = PodcastEpisodeWriter(base_url="https://db", service_role_key="k")
    w._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer k",
            "apikey": "k",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    return w


@pytest.mark.asyncio
class TestPodcastEpisodeWriter:
    async def test_upsert_pending_returns_id(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            captured["body"] = request.read()
            return httpx.Response(201, json=[{"id": 42}])

        w = _writer_with_handler(handler)
        episode_id = await w.upsert_pending(run_date="2026-05-09", language="en-US")
        await w.close()

        assert episode_id == 42
        assert captured["path"].endswith("/podcast_episodes")
        assert captured["params"]["on_conflict"] == "run_date,language"

    async def test_mark_rendering_patches_status(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PATCH"
            captured["params"] = dict(request.url.params)
            captured["body"] = request.read()
            return httpx.Response(204)

        w = _writer_with_handler(handler)
        await w.mark_rendering(42, story_count=5, word_count=4000)
        await w.close()

        assert captured["params"]["id"] == "eq.42"
        body = captured["body"].decode()
        assert '"status":"rendering"' in body or '"status": "rendering"' in body
        assert "4000" in body

    async def test_mark_rendered_sets_audio_path(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read().decode()
            return httpx.Response(204)

        w = _writer_with_handler(handler)
        await w.mark_rendered(
            42,
            audio_local_path="/tmp/x.wav",
            duration_seconds=1500,
            story_count=6,
            word_count=4100,
        )
        await w.close()

        assert "/tmp/x.wav" in captured["body"]
        assert "1500" in captured["body"]
        assert "rendered" in captured["body"]

    async def test_mark_delivered_sets_spotify_id(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read().decode()
            return httpx.Response(204)

        w = _writer_with_handler(handler)
        await w.mark_delivered(42, spotify_episode_id="spot-abc")
        await w.close()

        assert "spot-abc" in captured["body"]
        assert "delivered" in captured["body"]
        assert "delivered_at" in captured["body"]

    async def test_mark_failed_truncates_long_messages(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.read().decode()
            return httpx.Response(204)

        w = _writer_with_handler(handler)
        long_msg = "x" * 5000
        await w.mark_failed(42, error_message=long_msg)
        await w.close()

        # error_message capped at 1000 chars.
        assert captured["body"].count("x") <= 1000

    async def test_get_returns_record(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 42,
                        "run_date": "2026-05-09",
                        "language": "en-US",
                        "story_count": 5,
                        "word_count": 4000,
                        "status": "rendered",
                        "audio_local_path": "/tmp/a.wav",
                        "duration_seconds": 1500,
                        "delivered_at": None,
                        "spotify_episode_id": None,
                        "error_message": None,
                        "created_at": "2026-05-09T04:00:00+00:00",
                        "updated_at": "2026-05-09T04:05:00+00:00",
                    }
                ],
            )

        w = _writer_with_handler(handler)
        record = await w.get(42)
        await w.close()

        assert record.id == 42
        assert record.status == "rendered"
        assert record.audio_local_path == "/tmp/a.wav"

    async def test_latest_id_returns_none_when_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        w = _writer_with_handler(handler)
        result = await w.latest_id(language="en-US", status="rendered")
        await w.close()

        assert result is None

    async def test_latest_id_includes_status_filter(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=[{"id": 99}])

        w = _writer_with_handler(handler)
        result = await w.latest_id(language="de-DE", status="rendered")
        await w.close()

        assert result == 99
        assert captured["params"]["status"] == "eq.rendered"
        assert captured["params"]["language"] == "eq.de-DE"
