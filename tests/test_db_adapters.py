from __future__ import annotations

import httpx

from app.adapters import ArticleLookupFromDb, RawArticleDbReader


def _reader_with_handler(handler) -> RawArticleDbReader:
    reader = RawArticleDbReader(base_url="https://db", service_role_key="k")
    reader._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer k", "apikey": "k"},
    )
    return reader


def _lookup_with_handler(handler) -> ArticleLookupFromDb:
    a = ArticleLookupFromDb(base_url="https://db", service_role_key="k")
    a._client = httpx.AsyncClient(
        base_url="https://db",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer k", "apikey": "k"},
    )
    return a


class TestRawArticleDbReader:
    async def test_fetch_raw_articles_joins_entities(self) -> None:
        calls: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.path, dict(request.url.params)))
            if request.url.path.endswith("/raw_articles"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "a1",
                            "url": "https://ex.com/a",
                            "title": "A",
                            "source_name": "ESPN",
                            "category": "nfl",
                            "fetched_at": "2026-04-23T12:00:00+00:00",
                            "publication_date": "2026-04-23T10:00:00+00:00",
                        },
                        {
                            "id": "a2",
                            "url": "https://ex.com/b",
                            "title": "B",
                            "source_name": "CBS",
                            "category": None,
                            "fetched_at": "2026-04-23T11:00:00+00:00",
                            "publication_date": None,
                        },
                    ],
                )
            # article_entities
            return httpx.Response(
                200,
                json=[
                    {
                        "article_id": "a1",
                        "entity_type": "player",
                        "entity_id": "00-0026158",
                        "matched_name": "Josh Allen",
                    },
                    {
                        "article_id": "a1",
                        "entity_type": "team",
                        "entity_id": "BUF",
                        "matched_name": "Buffalo Bills",
                    },
                ],
            )

        reader = _reader_with_handler(handler)
        articles = await reader.fetch_raw_articles(lookback_hours=6)
        await reader.close()

        assert len(articles) == 2
        a1 = next(a for a in articles if a.id == "a1")
        assert a1.url == "https://ex.com/a"
        assert a1.source_name == "ESPN"
        assert {e.entity_id for e in a1.entities} == {"00-0026158", "BUF"}
        assert a1.facts_count == 2

        a2 = next(a for a in articles if a.id == "a2")
        assert a2.entities == []
        assert a2.facts_count == 0

        # First call filters on status + cutoff
        articles_params = calls[0][1]
        assert articles_params["status"] == "eq.knowledge_ok"
        assert articles_params["fetched_at"].startswith("gte.")
        # Second call scopes to collected article ids
        ent_params = calls[1][1]
        assert "a1" in ent_params["article_id"]
        assert "a2" in ent_params["article_id"]

    async def test_fetch_raw_articles_empty_skips_entity_query(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=[])

        reader = _reader_with_handler(handler)
        articles = await reader.fetch_raw_articles(lookback_hours=2)
        await reader.close()

        assert articles == []
        assert calls == ["/rest/v1/raw_articles"]


class TestArticleLookupFromDb:
    async def test_lookup_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/raw_articles")
            assert dict(request.url.params)["url"] == "eq.https://ex.com/a"
            return httpx.Response(
                200,
                json=[{"url": "https://ex.com/a", "title": "A", "content": "body"}],
            )

        lookup = _lookup_with_handler(handler)
        resp = await lookup.lookup_article("https://ex.com/a")
        await lookup.close()

        assert resp.found is True
        assert resp.article is not None
        assert resp.article.content == "body"
        assert resp.article.header == "A"

    async def test_lookup_not_found_returns_found_false(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        lookup = _lookup_with_handler(handler)
        resp = await lookup.lookup_article("https://ex.com/missing")
        await lookup.close()

        assert resp.found is False
        assert resp.article is None

    async def test_lookup_row_without_content_is_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=[{"url": "https://ex.com/a", "title": "A", "content": None}]
            )

        lookup = _lookup_with_handler(handler)
        resp = await lookup.lookup_article("https://ex.com/a")
        await lookup.close()

        assert resp.found is False
        assert resp.article is None
