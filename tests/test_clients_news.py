from __future__ import annotations

import json as _json
from datetime import UTC, datetime

import httpx

from app.clients.base import SupabaseJobsConfig
from app.clients.news_extraction import NewsExtractionClient, NewsItem


def _make_client(handler) -> NewsExtractionClient:
    client = NewsExtractionClient(
        submit_url="https://svc/submit",
        poll_url="https://svc/poll",
        supabase=SupabaseJobsConfig(url="https://db"),
        poll_interval_seconds=0.0,
        timeout_seconds=5.0,
    )
    client._job._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class TestNewsExtractionClient:
    async def test_extract_returns_parsed_items(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "job_id": "j",
                    "result": {
                        "items": [
                            {
                                "url": "https://ex.com/a",
                                "title": "A",
                                "publication_date": "2026-04-23T10:00:00Z",
                                "source_name": "ESPN",
                                "publisher": "ESPN Inc",
                                "description": "d",
                            },
                            {
                                "url": "https://ex.com/b",
                                "title": "B",
                                "publication_date": None,
                                "source_name": "CBS",
                            },
                        ],
                        "sources_processed": 2,
                        "items_extracted": 2,
                        "items_filtered": 0,
                    },
                },
            )

        client = _make_client(handler)
        items = await client.extract(since=datetime(2026, 4, 23, tzinfo=UTC))
        await client.close()

        assert len(items) == 2
        assert items[0].url == "https://ex.com/a"
        assert items[0].publication_date == datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        assert items[1].publication_date is None

    async def test_extract_sends_options_payload(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/submit"):
                captured["body"] = _json.loads(request.read())
                return httpx.Response(202, json={"job_id": "j"})
            return httpx.Response(
                200,
                json={"status": "succeeded", "job_id": "j", "result": {"items": []}},
            )

        client = _make_client(handler)
        await client.extract(
            since=datetime(2026, 4, 23, 10, 0, tzinfo=UTC),
            source_filter="ESPN",
            max_articles=50,
        )
        await client.close()

        options = captured["body"]["options"]
        assert options["since"] == "2026-04-23T10:00:00+00:00"
        assert options["source_filter"] == "ESPN"
        assert options["max_articles"] == 50
        assert "max_workers" not in options


class TestNewsItem:
    def test_from_payload_parses_zulu_datetime(self) -> None:
        item = NewsItem.from_payload(
            {"url": "u", "publication_date": "2026-04-23T00:00:00Z"}
        )
        assert item.publication_date == datetime(2026, 4, 23, tzinfo=UTC)

    def test_from_payload_tolerates_garbage_date(self) -> None:
        item = NewsItem.from_payload({"url": "u", "publication_date": "not-a-date"})
        assert item.publication_date is None
