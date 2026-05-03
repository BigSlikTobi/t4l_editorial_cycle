"""Tests for the produce/harvest split TTS client.

Two surfaces to cover:

  * PRODUCE — `create_and_wait()`. Short async-job poll, falls back to
    Gemini-API listing when the worker stalls. Does not wait for the
    Gemini batch to finish.

  * HARVEST — `process_batch()` + `check_batch_state()`. The harvest
    cycle (`scripts/tts_recover.py`) gates `process_batch` on
    `check_batch_state` so it only runs the worker's process action
    when Gemini reports JOB_STATE_SUCCEEDED.

Each test wires the underlying AsyncJobClient with an httpx.MockTransport
that simulates the Cloud Run service. Gemini API calls are stubbed via
monkeypatching `_gemini_client` to return a fake namespace object.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.clients.base import SupabaseJobsConfig
from app.team_beat.schemas import TTSItem
from app.team_beat.tts_client import (
    GEMINI_SUCCESS_STATE,
    TTSBatchClient,
    TTSBatchError,
)


def _build_client(handler, **overrides) -> TTSBatchClient:
    client = TTSBatchClient(
        submit_url="https://svc/submit",
        poll_url="https://svc/poll",
        supabase=SupabaseJobsConfig(url="https://db"),
        auth_token="ttok",
        model_name=overrides.get("model_name", "gemini-3.1-flash-tts-preview"),
        voice_name=overrides.get("voice_name", "Kore"),
        storage_bucket=overrides.get("storage_bucket", "team-beat-audio"),
        storage_path_prefix=overrides.get("storage_path_prefix", "gemini-tts-batch"),
        gemini_api_key=overrides.get("gemini_api_key", "gem-test-key"),
        job_poll_interval_seconds=0.0,
        create_short_timeout_seconds=overrides.get("create_short_timeout_seconds", 5.0),
        process_timeout_seconds=overrides.get("process_timeout_seconds", 5.0),
    )
    client._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _items() -> list[TTSItem]:
    return [
        TTSItem(id="NYJ-2026-05-02T04:00Z", text="...nyj script...", title="NYJ AM"),
        TTSItem(id="CHI-2026-05-02T04:00Z", text="...chi script...", title="CHI AM"),
    ]


# --------------------------------------------------------------- PRODUCE


class TestCreateAndWaitHappyPath:
    """Worker reports back within the short timeout: return batch_id, done."""

    async def test_happy_path_returns_worker_batch_id(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured.append(json.loads(request.read()))
                return httpx.Response(202, json={"job_id": "j-create"})
            # Single poll → succeeded with batch_id.
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j-create",
                "result": {
                    "action": "create",
                    "batch_id": "batches/happy",
                    "status": "JOB_STATE_PENDING",
                },
            })

        async with _build_client(handler) as client:
            bid = await client.create_and_wait(_items())

        assert bid == "batches/happy"
        # Confirm the create payload shape.
        assert captured[0]["action"] == "create"
        assert captured[0]["model_name"] == "gemini-3.1-flash-tts-preview"
        assert captured[0]["voice_name"] == "Kore"
        assert len(captured[0]["items"]) == 2

    async def test_does_not_wait_for_status_succeeded(self) -> None:
        """Returns as soon as create lands — even when Gemini state is
        still PENDING. The harvest cycle handles eventual completion."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j",
                "result": {"action": "create", "batch_id": "b/x", "status": "JOB_STATE_PENDING"},
            })
        async with _build_client(handler) as client:
            bid = await client.create_and_wait(_items())
        assert bid == "b/x"


class TestCreateAndWaitStallRecovery:
    """Worker stalls past the short timeout. Recovery via Gemini API listing."""

    async def test_recovers_via_gemini_list_when_poll_times_out(
        self, monkeypatch
    ) -> None:
        # Async-job /poll always returns running → forces JobTimeoutError.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j-stall"})
            return httpx.Response(200, json={"status": "running", "job_id": "j-stall"})

        # Stub the Gemini SDK: list returns one fresh batch matching our
        # submit window.
        recovered_batch = SimpleNamespace(
            create_time=datetime.now(UTC),
            name="batches/recovered-from-gemini",
        )
        # Older batch that should be skipped (outside window).
        older_batch = SimpleNamespace(
            create_time=datetime.now(UTC) - timedelta(hours=1),
            name="batches/too-old",
        )
        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([recovered_batch, older_batch])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )

        async with _build_client(
            handler, create_short_timeout_seconds=0.05
        ) as client:
            bid = await client.create_and_wait(_items())

        assert bid == "batches/recovered-from-gemini"
        fake_client.batches.list.assert_called_once()

    async def test_invokes_on_worker_stall_callback_with_job_id(
        self, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j-stall-cb"})
            return httpx.Response(200, json={"status": "running", "job_id": "j-stall-cb"})

        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([
            SimpleNamespace(create_time=datetime.now(UTC), name="batches/x"),
        ])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )

        callback_calls: list[tuple[str, str]] = []

        async def on_stall(job_id: str, reason: str) -> None:
            callback_calls.append((job_id, reason))

        async with _build_client(
            handler, create_short_timeout_seconds=0.05
        ) as client:
            await client.create_and_wait(_items(), on_worker_stall=on_stall)

        assert len(callback_calls) == 1
        job_id, reason = callback_calls[0]
        assert job_id == "j-stall-cb"
        assert "create poll timeout" in reason

    async def test_callback_failure_does_not_block_recovery(
        self, monkeypatch
    ) -> None:
        """Cancellation is best-effort; if the PATCH fails we still need
        to return the recovered batch_id so the brief lands."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={"status": "running", "job_id": "j"})

        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([
            SimpleNamespace(create_time=datetime.now(UTC), name="batches/recovered"),
        ])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )

        async def on_stall(job_id: str, reason: str) -> None:
            raise RuntimeError("PATCH supabase failed")

        async with _build_client(
            handler, create_short_timeout_seconds=0.05
        ) as client:
            bid = await client.create_and_wait(_items(), on_worker_stall=on_stall)

        assert bid == "batches/recovered"

    async def test_raises_when_no_recent_gemini_batch_in_window(
        self, monkeypatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={"status": "running", "job_id": "j"})

        # Gemini list returns only batches older than the submit window.
        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([
            SimpleNamespace(
                create_time=datetime.now(UTC) - timedelta(hours=2),
                name="batches/way-older",
            ),
        ])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )

        async with _build_client(
            handler, create_short_timeout_seconds=0.05
        ) as client:
            with pytest.raises(TTSBatchError, match="no Gemini batch was created"):
                await client.create_and_wait(_items())


class TestCreateAndWaitInputValidation:
    async def test_rejects_empty_items(self) -> None:
        async with _build_client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(ValueError, match="at least one TTSItem"):
                await client.create_and_wait([])


# --------------------------------------------------------------- HARVEST


class TestCheckBatchState:
    async def test_returns_state_string_from_gemini_sdk(self, monkeypatch) -> None:
        fake_client = MagicMock()
        fake_client.batches.get.return_value = SimpleNamespace(
            state=SimpleNamespace(name="JOB_STATE_SUCCEEDED"),
        )
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )
        async with _build_client(lambda r: httpx.Response(500)) as client:
            state = await client.check_batch_state("batches/abc")
        assert state == "JOB_STATE_SUCCEEDED"
        fake_client.batches.get.assert_called_once_with(name="batches/abc")

    async def test_raises_tts_batch_error_on_sdk_exception(
        self, monkeypatch
    ) -> None:
        fake_client = MagicMock()
        fake_client.batches.get.side_effect = RuntimeError("404 not found")
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )
        async with _build_client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(TTSBatchError, match="state read failed"):
                await client.check_batch_state("batches/x")

    async def test_raises_when_api_key_missing(self) -> None:
        client = TTSBatchClient(
            submit_url="https://svc/submit",
            poll_url="https://svc/poll",
            supabase=SupabaseJobsConfig(url="https://db"),
            auth_token="ttok",
            model_name="m",
            voice_name="v",
            storage_bucket="b",
            gemini_api_key=None,
        )
        try:
            with pytest.raises(TTSBatchError, match="Gemini API key not configured"):
                await client.check_batch_state("batches/x")
        finally:
            await client.close()


class TestProcessBatch:
    async def test_process_payload_uses_nested_storage_block(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured.append(json.loads(request.read()))
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j",
                "result": {"action": "process", "results": [], "failures": []},
            })

        async with _build_client(handler) as client:
            await client.process_batch("batches/abc", ["NYJ-…"], path_prefix_suffix="2026-05-02_AM")

        assert captured[0]["action"] == "process"
        assert captured[0]["storage"] == {
            "bucket": "team-beat-audio",
            "path_prefix": "gemini-tts-batch/2026-05-02_AM",
        }

    async def test_failed_items_surface_as_url_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j",
                "result": {
                    "action": "process",
                    "results": [
                        {"id": "NYJ-2026-05-02T04:00Z", "public_url": "https://cdn/nyj.mp3"},
                    ],
                    "failures": [
                        {"id": "CHI-2026-05-02T04:00Z", "error": "Missing response payload"},
                    ],
                },
            })

        async with _build_client(handler) as client:
            outcome = await client.process_batch(
                "batches/x",
                ["NYJ-2026-05-02T04:00Z", "CHI-2026-05-02T04:00Z"],
            )

        assert outcome.url_for("NYJ-2026-05-02T04:00Z") == "https://cdn/nyj.mp3"
        assert outcome.url_for("CHI-2026-05-02T04:00Z") is None

    async def test_missing_from_manifest_recorded_as_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j",
                "result": {"action": "process", "results": [], "failures": []},
            })
        async with _build_client(handler) as client:
            outcome = await client.process_batch(
                "batches/x", ["NYJ-2026-05-02T04:00Z"],
            )
        only = outcome.items[0]
        assert only.public_url is None
        assert only.error == "missing_from_manifest"


class TestDiscoverRecentBatchId:
    async def test_picks_newest_in_window(self, monkeypatch) -> None:
        now = datetime.now(UTC)
        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([
            SimpleNamespace(create_time=now, name="batches/newest"),
            SimpleNamespace(create_time=now - timedelta(seconds=60), name="batches/middle"),
            SimpleNamespace(create_time=now - timedelta(hours=2), name="batches/way-old"),
        ])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )
        async with _build_client(lambda r: httpx.Response(500)) as client:
            recovered = await client.discover_recent_batch_id(
                submit_time=now - timedelta(seconds=120),
            )
        assert recovered == "batches/newest"

    async def test_returns_none_when_window_empty(self, monkeypatch) -> None:
        fake_client = MagicMock()
        fake_client.batches.list.return_value = iter([
            SimpleNamespace(
                create_time=datetime.now(UTC) - timedelta(hours=2),
                name="batches/old",
            ),
        ])
        monkeypatch.setattr(
            "app.team_beat.tts_client._gemini_client",
            lambda key: fake_client,
        )
        async with _build_client(lambda r: httpx.Response(500)) as client:
            assert await client.discover_recent_batch_id(
                submit_time=datetime.now(UTC),
            ) is None

    async def test_returns_none_when_api_key_missing(self) -> None:
        client = TTSBatchClient(
            submit_url="https://svc/submit",
            poll_url="https://svc/poll",
            supabase=SupabaseJobsConfig(url="https://db"),
            auth_token="ttok",
            model_name="m",
            voice_name="v",
            storage_bucket="b",
            gemini_api_key=None,
        )
        try:
            assert await client.discover_recent_batch_id(
                submit_time=datetime.now(UTC),
            ) is None
        finally:
            await client.close()
