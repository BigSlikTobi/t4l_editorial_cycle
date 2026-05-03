from __future__ import annotations

import pytest
import httpx

from app.clients.base import (
    AsyncJobClient,
    JobFailedError,
    JobTimeoutError,
    SupabaseJobsConfig,
)


def _client_with_transport(transport: httpx.MockTransport, **kwargs) -> AsyncJobClient:
    c = AsyncJobClient(
        submit_url="https://svc/submit",
        poll_url="https://svc/poll",
        supabase=SupabaseJobsConfig(url="https://db"),
        auth_token=kwargs.pop("auth_token", None),
        poll_interval_seconds=kwargs.pop("poll_interval_seconds", 0.0),
        timeout_seconds=kwargs.pop("timeout_seconds", 5.0),
    )
    c._client = httpx.AsyncClient(transport=transport)
    return c


class TestAsyncJobClient:
    async def test_run_happy_path(self) -> None:
        calls = {"submit": 0, "poll": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            if request.url.path.endswith("/submit"):
                calls["submit"] += 1
                assert '"url":"https://db"' in body
                assert '"jobs_table":"extraction_jobs"' in body
                assert '"key"' not in body
                return httpx.Response(202, json={"status": "queued", "job_id": "abc"})
            calls["poll"] += 1
            if calls["poll"] == 1:
                return httpx.Response(200, json={"status": "running", "job_id": "abc"})
            return httpx.Response(
                200, json={"status": "succeeded", "job_id": "abc", "result": {"ok": True}}
            )

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            result = await client.run({"options": {"since": "2026-04-23T00:00:00+00:00"}})

        assert result == {"ok": True}
        assert calls == {"submit": 1, "poll": 2}

    async def test_submit_passes_supabase_block(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured["body"] = request.read()
                return httpx.Response(202, json={"job_id": "j1"})
            return httpx.Response(
                200, json={"status": "succeeded", "job_id": "j1", "result": {}}
            )

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            await client.run({"options": {}})

        import json as _json

        body = _json.loads(captured["body"])
        assert body["supabase"] == {
            "url": "https://db",
            "jobs_table": "extraction_jobs",
        }
        assert "key" not in body["supabase"]

    async def test_poll_body_omits_service_key(self) -> None:
        """Poll requests must not carry the Supabase service-role key either."""
        poll_bodies: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j1"})
            poll_bodies.append(request.read())
            return httpx.Response(
                200, json={"status": "succeeded", "job_id": "j1", "result": {}}
            )

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            await client.run({"options": {}})

        import json as _json

        assert poll_bodies, "expected at least one poll call"
        for raw in poll_bodies:
            body = _json.loads(raw)
            assert body["supabase"] == {
                "url": "https://db",
                "jobs_table": "extraction_jobs",
            }
            assert "key" not in body["supabase"]

    async def test_auth_token_sent_as_bearer_on_submit_and_poll(self) -> None:
        """Shared-secret bearer token must be attached to every submit+poll call."""
        seen_auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200, json={"status": "succeeded", "job_id": "j", "result": {}}
            )

        async with _client_with_transport(
            httpx.MockTransport(handler), auth_token="shh-secret"
        ) as client:
            await client.run({"options": {}})

        assert seen_auth, "expected at least one request"
        assert all(h == "Bearer shh-secret" for h in seen_auth)

    async def test_no_auth_header_when_token_unset(self) -> None:
        seen_auth: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("authorization"))
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200, json={"status": "succeeded", "job_id": "j", "result": {}}
            )

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            await client.run({"options": {}})

        assert all(h is None for h in seen_auth)

    async def test_run_raises_on_failed_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "job_id": "j",
                    "error": {"message": "boom"},
                },
            )

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            with pytest.raises(JobFailedError) as exc:
                await client.run({})

        assert exc.value.error == {"message": "boom"}

    async def test_run_raises_on_submit_http_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server err")

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            with pytest.raises(JobFailedError, match="Submit failed"):
                await client.run({})

    async def test_run_raises_on_poll_404_terminal(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(404, text="not found")

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            with pytest.raises(JobFailedError, match="not found"):
                await client.run({})

    async def test_run_raises_on_unknown_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={"status": "mystery", "job_id": "j"})

        async with _client_with_transport(httpx.MockTransport(handler)) as client:
            with pytest.raises(JobFailedError, match="unknown status"):
                await client.run({})

    async def test_run_raises_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={"status": "running", "job_id": "j"})

        async with _client_with_transport(
            httpx.MockTransport(handler),
            poll_interval_seconds=0.0,
            timeout_seconds=0.01,
        ) as client:
            with pytest.raises(JobTimeoutError):
                await client.run({})

    async def test_per_call_timeout_override_takes_precedence(self) -> None:
        """Per-call timeout_seconds wins over the instance default.

        Lets callers (e.g. the TTS client) use a single AsyncJobClient
        for stages with very different latency profiles — Gemini batch
        creation can take many minutes, status checks are sub-second."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(200, json={"status": "running", "job_id": "j"})

        async with _client_with_transport(
            httpx.MockTransport(handler),
            poll_interval_seconds=0.0,
            timeout_seconds=60.0,  # generous instance default
        ) as client:
            # Per-call override of 0.01s wins → fast timeout despite the
            # generous instance default.
            with pytest.raises(JobTimeoutError, match="0.01s"):
                await client.run({}, timeout_seconds=0.01)
