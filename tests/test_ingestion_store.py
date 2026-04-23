from __future__ import annotations

import json as _json
from datetime import UTC, datetime

import httpx

from app.clients import ContentResult, KnowledgeResult, NewsItem, ResolvedEntity, Topic
from app.ingestion.store import RawArticleStore


def _store_with_handler(handler) -> RawArticleStore:
    store = RawArticleStore(base_url="https://db", service_role_key="k")
    store._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer k", "apikey": "k"},
    )
    return store


class TestRawArticleStore:
    async def test_insert_discovered_posts_with_ignore_duplicates(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["prefer"] = request.headers.get("prefer")
            captured["body"] = _json.loads(request.read())
            # postgrest returns only newly inserted rows when ignore-duplicates
            return httpx.Response(
                201,
                json=[{"id": "1", "url": "https://ex.com/a"}],
            )

        store = _store_with_handler(handler)
        inserted = await store.insert_discovered(
            [
                NewsItem(
                    url="https://ex.com/a",
                    title="A",
                    description=None,
                    publication_date=datetime(2026, 4, 23, tzinfo=UTC),
                    source_name="ESPN",
                    publisher=None,
                ),
                NewsItem(
                    url="https://ex.com/b",
                    title=None,
                    description=None,
                    publication_date=None,
                    source_name=None,
                    publisher=None,
                ),
            ]
        )
        await store.close()

        assert inserted == 1
        assert "ignore-duplicates" in captured["prefer"]
        assert len(captured["body"]) == 2
        assert captured["body"][0]["status"] == "discovered"
        assert captured["body"][0]["publication_date"] == "2026-04-23T00:00:00+00:00"
        assert captured["body"][1]["publication_date"] is None

    async def test_insert_discovered_no_items_skips_http(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be called")

        store = _store_with_handler(handler)
        n = await store.insert_discovered([])
        await store.close()
        assert n == 0

    async def test_list_known_urls_filters_by_since(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json=[{"url": "https://ex.com/a"}, {"url": "https://ex.com/b"}],
            )

        store = _store_with_handler(handler)
        urls = await store.list_known_urls(datetime(2026, 4, 23, tzinfo=UTC))
        await store.close()

        assert urls == {"https://ex.com/a", "https://ex.com/b"}
        assert captured["params"]["fetched_at"].startswith("gte.2026-04-23")

    async def test_update_content_patches_fields(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["params"] = dict(request.url.params)
            captured["body"] = _json.loads(request.read())
            return httpx.Response(204)

        store = _store_with_handler(handler)
        await store.update_content(
            "article-1",
            ContentResult(
                url="u",
                title="Updated Title",
                content="body",
                paragraphs=["body"],
                error=None,
            ),
        )
        await store.close()

        assert captured["method"] == "PATCH"
        assert captured["params"]["id"] == "eq.article-1"
        assert captured["body"]["status"] == "content_ok"
        assert captured["body"]["content"] == "body"
        assert captured["body"]["title"] == "Updated Title"

    async def test_update_knowledge_writes_entities_topics_and_patches(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(204 if request.method == "PATCH" else 201, json=[])

        store = _store_with_handler(handler)
        await store.update_knowledge(
            "article-1",
            KnowledgeResult(
                topics=[Topic(topic="trade", confidence=0.9, rank=1)],
                entities=[
                    ResolvedEntity(
                        entity_type="player",
                        entity_id="00-0026158",
                        mention_text="Josh Allen",
                        matched_name="Josh Allen",
                        confidence=0.98,
                        team_abbr="BUF",
                        position="QB",
                    )
                ],
                unresolved_entities=[],
            ),
        )
        await store.close()

        paths = [path for _, path in calls]
        methods = [m for m, _ in calls]
        assert any("article_entities" in p for p in paths)
        assert any("article_topics" in p for p in paths)
        assert methods.count("PATCH") == 1

    async def test_update_knowledge_skips_empty_buckets(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return httpx.Response(204)

        store = _store_with_handler(handler)
        await store.update_knowledge("a1", KnowledgeResult())
        await store.close()

        # Only the PATCH to raw_articles — no entity/topic posts.
        assert len(calls) == 1
        assert calls[0][0] == "PATCH"
