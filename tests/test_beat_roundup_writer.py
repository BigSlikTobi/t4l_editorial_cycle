"""Tests for BeatRoundupWriter and BeatCycleStateStore."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.adapters import (
    BeatCycleStateStore,
    BeatRoundupWriter,
    ExternalServiceError,
)
from app.team_beat.schemas import BeatCycleResult, BeatOutcome, BeatRoundup


def _writer(handler) -> BeatRoundupWriter:
    w = BeatRoundupWriter(base_url="https://db", service_role_key="k")
    w._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer k",
            "apikey": "k",
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
    )
    return w


def _state_store(handler) -> BeatCycleStateStore:
    s = BeatCycleStateStore(base_url="https://db", service_role_key="k")
    s._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer k",
            "apikey": "k",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    return s


def _roundup(
    audio_url: str | None = "https://cdn/x.mp3",
    tts_batch_id: str | None = "batches/abc123",
) -> BeatRoundup:
    return BeatRoundup(
        team_code="NYJ",
        cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
        cycle_slot="AM",
        persona_name="Theo Briggs",
        en_body="EN body",
        de_body="DE body",
        radio_script="Style: ruhig...\n\n[pause] Heute aus East Rutherford...",
        audio_url=audio_url,
        tts_batch_id=tts_batch_id,
    )


class TestBeatRoundupWriter:
    async def test_upsert_uses_on_conflict_target(self) -> None:
        captured: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append((str(request.url), json.loads(request.read())))
            return httpx.Response(201, json=[{"id": 42}])

        writer = _writer(handler)
        roundup_id = await writer.upsert(_roundup())
        await writer.close()

        assert roundup_id == 42
        url, body = captured[0]
        assert "/rest/v1/team_roundup" in url
        # Upsert by composite unique key declared in migration 008
        assert "on_conflict=team_code%2Ccycle_ts" in url or "on_conflict=team_code,cycle_ts" in url
        assert body["team_code"] == "NYJ"
        assert body["cycle_slot"] == "AM"
        assert body["audio_url"] == "https://cdn/x.mp3"
        assert body["tts_batch_id"] == "batches/abc123"
        assert body["cycle_ts"].startswith("2026-05-02T04:00")

    async def test_upsert_handles_null_audio_url(self) -> None:
        # TTS failure path: brief is still written; audio_url is null so
        # the row is recoverable for a later re-render.
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            return httpx.Response(201, json=[{"id": 7}])

        writer = _writer(handler)
        await writer.upsert(_roundup(audio_url=None))
        await writer.close()

        assert bodies[0]["audio_url"] is None
        # batch_id is preserved on TTS failure → tts_recover.py can use it.
        assert bodies[0]["tts_batch_id"] == "batches/abc123"

    async def test_upsert_handles_null_audio_and_null_batch_id(self) -> None:
        # Worst case (e.g. JobTimeoutError on create) — neither audio nor
        # batch_id is available. Row still writes, NULLs land in DB.
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            return httpx.Response(201, json=[{"id": 8}])

        writer = _writer(handler)
        await writer.upsert(_roundup(audio_url=None, tts_batch_id=None))
        await writer.close()

        assert bodies[0]["audio_url"] is None
        assert bodies[0]["tts_batch_id"] is None

    async def test_upsert_raises_on_db_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        writer = _writer(handler)
        with pytest.raises(ExternalServiceError, match="team_roundup upsert failed"):
            await writer.upsert(_roundup())
        await writer.close()


class TestBeatCycleStateStore:
    async def test_records_no_news(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            return httpx.Response(201, json=[{"id": 1}])

        store = _state_store(handler)
        result = BeatCycleResult(
            team_code="NYJ",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            outcome=BeatOutcome.NO_NEWS,
            reason="Quiet 12h window.",
            article_count=2,
        )
        state_id = await store.record(result)
        await store.close()

        assert state_id == 1
        assert bodies[0]["outcome"] == "no_news"
        assert bodies[0]["reason"] == "Quiet 12h window."
        assert bodies[0]["article_count"] == 2
        assert bodies[0]["roundup_id"] is None

    async def test_records_filed_with_roundup_id(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            return httpx.Response(201, json=[{"id": 9}])

        store = _state_store(handler)
        result = BeatCycleResult(
            team_code="CHI",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            outcome=BeatOutcome.FILED,
            roundup_id=42,
            article_count=11,
        )
        state_id = await store.record(result)
        await store.close()

        assert state_id == 9
        assert bodies[0]["outcome"] == "filed"
        assert bodies[0]["roundup_id"] == 42

    async def test_records_error_outcome(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.read()))
            return httpx.Response(201, json=[{"id": 3}])

        store = _state_store(handler)
        result = BeatCycleResult(
            team_code="NYJ",
            cycle_ts=datetime(2026, 5, 2, 4, 0, tzinfo=UTC),
            cycle_slot="AM",
            outcome=BeatOutcome.ERROR,
            reason="TTS batch reached JOB_STATE_FAILED.",
        )
        await store.record(result)
        await store.close()

        assert bodies[0]["outcome"] == "error"
        assert "TTS batch" in bodies[0]["reason"]
