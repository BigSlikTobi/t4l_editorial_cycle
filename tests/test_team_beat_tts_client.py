"""Tests for the three-stage TTS batch orchestrator.

Each test wires the underlying AsyncJobClient with an httpx.MockTransport
that simulates a sequence of submit→poll cycles against the real Cloud
Run contract (action-discriminated payload, `result.action` discriminator,
Gemini-state polling).
"""

from __future__ import annotations

import json

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
        job_poll_interval_seconds=0.0,
        job_timeout_seconds=overrides.get("job_timeout_seconds", 5.0),
        status_poll_interval_seconds=0.0,
        status_timeout_seconds=overrides.get("status_timeout_seconds", 5.0),
    )
    client._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _items() -> list[TTSItem]:
    return [
        TTSItem(id="NYJ-2026-05-02T04:00Z", text="...nyj script...", title="NYJ AM"),
        TTSItem(id="CHI-2026-05-02T04:00Z", text="...chi script...", title="CHI AM"),
    ]


class TestSynthesizeHappyPath:
    async def test_full_lifecycle(self) -> None:
        """create → status (one running, one terminal SUCCEEDED) → process."""
        captured_submits: list[dict] = []
        # Each submit gets a unique job_id so the script below can assert
        # ordering without coupling to call counts.
        next_job = {"n": 0}
        # Sequence of poll responses per job_id (FIFO).
        poll_responses_by_job: dict[str, list[dict]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                body = json.loads(request.read())
                captured_submits.append(body)
                next_job["n"] += 1
                job_id = f"job-{next_job['n']}"
                action = body["action"]
                if action == "create":
                    poll_responses_by_job[job_id] = [{
                        "status": "succeeded",
                        "job_id": job_id,
                        "result": {
                            "action": "create",
                            "batch_id": "batches/abc123",
                            "status": "JOB_STATE_PENDING",
                        },
                    }]
                elif action == "status":
                    # First status call returns RUNNING, second SUCCEEDED.
                    state = "JOB_STATE_RUNNING" if next_job["n"] == 2 else GEMINI_SUCCESS_STATE
                    poll_responses_by_job[job_id] = [{
                        "status": "succeeded",
                        "job_id": job_id,
                        "result": {
                            "action": "status",
                            "batch_id": "batches/abc123",
                            "status": state,
                        },
                    }]
                else:  # process
                    poll_responses_by_job[job_id] = [{
                        "status": "succeeded",
                        "job_id": job_id,
                        "result": {
                            "action": "process",
                            "results": [
                                {"id": "NYJ-2026-05-02T04:00Z", "public_url": "https://cdn/nyj.mp3"},
                                {"id": "CHI-2026-05-02T04:00Z", "public_url": "https://cdn/chi.mp3"},
                            ],
                            "failures": [],
                        },
                    }]
                return httpx.Response(202, json={"job_id": job_id})
            # /poll
            body = json.loads(request.read())
            job_id = body["job_id"]
            queue = poll_responses_by_job.get(job_id, [])
            if not queue:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json=queue.pop(0))

        async with _build_client(handler) as client:
            outcome = await client.synthesize(_items(), path_prefix_suffix="2026-05-02_AM")

        assert outcome.batch_id == "batches/abc123"
        assert outcome.url_for("NYJ-2026-05-02T04:00Z") == "https://cdn/nyj.mp3"
        assert outcome.url_for("CHI-2026-05-02T04:00Z") == "https://cdn/chi.mp3"

        # Action sequence: 1 create + N status + 1 process. We don't pin N
        # since the polling loop count is implementation detail, but the
        # first must be create and the last must be process.
        actions = [s["action"] for s in captured_submits]
        assert actions[0] == "create"
        assert actions[-1] == "process"
        assert actions.count("create") == 1
        assert actions.count("process") == 1
        assert actions.count("status") >= 1

    async def test_create_payload_carries_model_voice_and_items(self) -> None:
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured.append(json.loads(request.read()))
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": "j",
                "result": {"action": "create", "batch_id": "b", "status": GEMINI_SUCCESS_STATE},
            })

        async with _build_client(handler) as client:
            # Run only the _create stage so the test is focused.
            await client._create(_items())

        assert captured[0]["action"] == "create"
        assert captured[0]["model_name"] == "gemini-3.1-flash-tts-preview"
        assert captured[0]["voice_name"] == "Kore"
        assert len(captured[0]["items"]) == 2
        assert captured[0]["items"][0] == {
            "id": "NYJ-2026-05-02T04:00Z",
            "text": "...nyj script...",
            "title": "NYJ AM",
        }

    async def test_process_payload_uses_nested_storage_block(self) -> None:
        """The Cloud Run submit handler expects `storage: {bucket, path_prefix}`
        nested under the action payload. The worker flattens it before
        persisting; our client must send the public-API shape."""
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
            await client._process("batches/abc", "gemini-tts-batch/2026-05-02_AM")

        assert captured[0]["action"] == "process"
        assert captured[0]["storage"] == {
            "bucket": "team-beat-audio",
            "path_prefix": "gemini-tts-batch/2026-05-02_AM",
        }


class TestSynthesizeFailures:
    async def test_synthesize_rejects_empty_items(self) -> None:
        async with _build_client(lambda r: httpx.Response(500)) as client:
            with pytest.raises(ValueError, match="at least one TTSItem"):
                await client.synthesize([])

    async def test_gemini_terminal_failed_raises_tts_batch_error(self) -> None:
        next_job = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                next_job["n"] += 1
                body = json.loads(request.read())
                action = body["action"]
                return httpx.Response(202, json={"job_id": f"j{next_job['n']}-{action}"})
            # poll: route by job_id
            body = json.loads(request.read())
            job_id = body["job_id"]
            if "create" in job_id:
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "create", "batch_id": "batches/x", "status": "JOB_STATE_PENDING"},
                })
            # status returns FAILED
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": job_id,
                "result": {"action": "status", "batch_id": "batches/x", "status": "JOB_STATE_FAILED"},
            })

        async with _build_client(handler) as client:
            with pytest.raises(TTSBatchError) as exc:
                await client.synthesize(_items())

        assert exc.value.batch_id == "batches/x"
        assert exc.value.state == "JOB_STATE_FAILED"

    async def test_status_loop_timeout_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                body = json.loads(request.read())
                return httpx.Response(202, json={"job_id": f"j-{body['action']}"})
            body = json.loads(request.read())
            job_id = body["job_id"]
            if job_id == "j-create":
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "create", "batch_id": "batches/x", "status": "JOB_STATE_PENDING"},
                })
            # status: stuck in RUNNING forever
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": job_id,
                "result": {"action": "status", "batch_id": "batches/x", "status": "JOB_STATE_RUNNING"},
            })

        async with _build_client(handler, status_timeout_seconds=0.01) as client:
            with pytest.raises(TTSBatchError, match="did not reach terminal state"):
                await client.synthesize(_items())


class TestProcessManifestMapping:
    async def test_failed_items_surface_as_url_none(self) -> None:
        next_job = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                next_job["n"] += 1
                body = json.loads(request.read())
                return httpx.Response(202, json={"job_id": f"j{next_job['n']}-{body['action']}"})
            body = json.loads(request.read())
            job_id = body["job_id"]
            if "create" in job_id:
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "create", "batch_id": "batches/x", "status": GEMINI_SUCCESS_STATE},
                })
            if "status" in job_id:
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "status", "batch_id": "batches/x", "status": GEMINI_SUCCESS_STATE},
                })
            # process: NYJ succeeds, CHI fails
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": job_id,
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
            outcome = await client.synthesize(_items())

        assert outcome.url_for("NYJ-2026-05-02T04:00Z") == "https://cdn/nyj.mp3"
        assert outcome.url_for("CHI-2026-05-02T04:00Z") is None
        chi_result = next(i for i in outcome.items if i.item_id == "CHI-2026-05-02T04:00Z")
        assert chi_result.error == "Missing response payload"

    async def test_missing_from_manifest_recorded_as_error(self) -> None:
        """Service bug or partial response: id we requested isn't in the
        manifest at all. We record it as missing rather than silently dropping."""
        next_job = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                next_job["n"] += 1
                body = json.loads(request.read())
                return httpx.Response(202, json={"job_id": f"j{next_job['n']}-{body['action']}"})
            body = json.loads(request.read())
            job_id = body["job_id"]
            if "create" in job_id:
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "create", "batch_id": "b", "status": GEMINI_SUCCESS_STATE},
                })
            if "status" in job_id:
                return httpx.Response(200, json={
                    "status": "succeeded", "job_id": job_id,
                    "result": {"action": "status", "batch_id": "b", "status": GEMINI_SUCCESS_STATE},
                })
            return httpx.Response(200, json={
                "status": "succeeded", "job_id": job_id,
                "result": {"action": "process", "results": [], "failures": []},
            })

        async with _build_client(handler) as client:
            outcome = await client.synthesize(_items())

        for item in outcome.items:
            assert item.public_url is None
            assert item.error == "missing_from_manifest"
