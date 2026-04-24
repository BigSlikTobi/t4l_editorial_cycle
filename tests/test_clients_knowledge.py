from __future__ import annotations

import json as _json

import httpx

from app.clients.base import SupabaseJobsConfig
from app.clients.knowledge_extraction import KnowledgeExtractionClient


def _make_client(handler) -> KnowledgeExtractionClient:
    client = KnowledgeExtractionClient(
        submit_url="https://svc/submit",
        poll_url="https://svc/poll",
        supabase=SupabaseJobsConfig(url="https://db"),
        openai_model="gpt-x",
        poll_interval_seconds=0.0,
        timeout_seconds=5.0,
    )
    client._job._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class TestKnowledgeExtractionClient:
    async def test_payload_omits_openai_api_key(self) -> None:
        """OpenAI API key must be read from the cloud function's runtime env,
        never shipped inside the request body."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured["submit"] = _json.loads(request.read())
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "job_id": "j",
                    "result": {"topics": [], "entities": []},
                },
            )

        client = _make_client(handler)
        await client.extract(article_id="a1", text="some text", title="t", url="u")
        await client.close()

        raw = _json.dumps(captured["submit"])
        assert "api_key" not in raw
        assert "api-key" not in raw
        llm = captured["submit"]["llm"]
        assert llm == {"provider": "openai", "model": "gpt-x"}

    async def test_payload_omits_supabase_service_key(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured["submit"] = _json.loads(request.read())
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "job_id": "j",
                    "result": {"topics": [], "entities": []},
                },
            )

        client = _make_client(handler)
        await client.extract(article_id="a1", text="t")
        await client.close()

        assert "key" not in captured["submit"]["supabase"]
