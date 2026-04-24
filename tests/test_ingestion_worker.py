from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.clients import (
    ContentResult,
    JobFailedError,
    KnowledgeResult,
    NewsItem,
    ResolvedEntity,
    Topic,
)
from app.config import Settings
from app.ingestion import worker as worker_module
from app.ingestion.store import PendingArticle


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    monkeypatch.setenv(
        "NEWS_EXTRACTION_SUBMIT_URL", "https://cf/news-submit"
    )
    monkeypatch.setenv("NEWS_EXTRACTION_POLL_URL", "https://cf/news-poll")
    monkeypatch.setenv(
        "URL_CONTENT_EXTRACTION_SUBMIT_URL", "https://cf/url-submit"
    )
    monkeypatch.setenv("URL_CONTENT_EXTRACTION_POLL_URL", "https://cf/url-poll")
    monkeypatch.setenv(
        "KNOWLEDGE_EXTRACTION_SUBMIT_URL", "https://cf/kn-submit"
    )
    monkeypatch.setenv("KNOWLEDGE_EXTRACTION_POLL_URL", "https://cf/kn-poll")
    monkeypatch.setenv("EXTRACTION_FUNCTION_AUTH_TOKEN", "test-fn-token")
    for key in (
        "OPENAI_MODEL_ARTICLE_DATA_AGENT",
        "OPENAI_MODEL_STORY_CLUSTER_AGENT",
        "OPENAI_MODEL_EDITORIAL_ORCHESTRATOR_AGENT",
        "OPENAI_MODEL_ARTICLE_WRITER_AGENT",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings(_env_file=None)


class _FakeStore:
    def __init__(self) -> None:
        self.inserted: list[list[NewsItem]] = []
        self.content_updates: list[tuple[str, ContentResult]] = []
        self.knowledge_updates: list[tuple[str, KnowledgeResult]] = []
        self.failures: list[tuple[str, dict[str, Any]]] = []
        self.pending_by_status: dict[str, list[PendingArticle]] = {}
        self.watermarks: dict[str, datetime] = {}
        self.watermark_writes: list[dict[str, datetime]] = []
        self.closed = False

    async def insert_discovered(self, items: list[NewsItem]) -> int:
        self.inserted.append(items)
        return len(items)

    async def read_watermarks(self) -> dict[str, datetime]:
        return dict(self.watermarks)

    async def upsert_watermarks(self, watermarks: dict[str, datetime]) -> None:
        self.watermark_writes.append(dict(watermarks))
        self.watermarks.update(watermarks)

    async def list_pending(
        self, *, status: str, limit: int = 100
    ) -> list[PendingArticle]:
        return self.pending_by_status.get(status, [])

    async def update_content(self, article_id: str, result: ContentResult) -> None:
        self.content_updates.append((article_id, result))

    async def update_knowledge(self, article_id: str, result: KnowledgeResult) -> None:
        self.knowledge_updates.append((article_id, result))

    async def mark_failed(self, article_id: str, error: dict[str, Any]) -> None:
        self.failures.append((article_id, error))

    async def close(self) -> None:
        self.closed = True


class _FakeNewsClient:
    def __init__(self, items: list[NewsItem]) -> None:
        self._items = items
        self.closed = False

    async def extract(self, **kwargs: Any) -> list[NewsItem]:
        self.last_kwargs = kwargs
        return self._items

    async def close(self) -> None:
        self.closed = True


class _FakeContentClient:
    def __init__(self, by_url: dict[str, ContentResult]) -> None:
        self._by_url = by_url
        self.closed = False

    async def extract(self, urls: list[str], **kwargs: Any) -> dict[str, ContentResult]:
        self.last_urls = urls
        return {u: self._by_url[u] for u in urls if u in self._by_url}

    async def close(self) -> None:
        self.closed = True


class _FakeKnowledgeClient:
    def __init__(self, by_id: dict[str, KnowledgeResult | Exception]) -> None:
        self._by_id = by_id
        self.closed = False

    async def extract(
        self, *, article_id: str, text: str, title: str | None, url: str | None
    ) -> KnowledgeResult:
        value = self._by_id[article_id]
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


class TestRunIngestionCycle:
    async def test_full_pipeline_happy_path(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        news_items = [
            NewsItem(
                url="https://ex.com/a",
                title="A",
                description=None,
                publication_date=datetime(2026, 4, 23, tzinfo=UTC),
                source_name="ESPN",
                publisher=None,
            ),
        ]
        content_by_url = {
            "https://ex.com/a": ContentResult(
                url="https://ex.com/a",
                title="A",
                content="body text",
                paragraphs=["body text"],
                error=None,
            ),
        }
        knowledge_by_id = {
            "art-1": KnowledgeResult(
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
            ),
        }

        store = _FakeStore()
        store.pending_by_status["discovered"] = [
            PendingArticle(
                id="art-1", url="https://ex.com/a", title="A", content=None
            )
        ]
        store.pending_by_status["content_ok"] = [
            PendingArticle(
                id="art-1",
                url="https://ex.com/a",
                title="A",
                content="body text",
            )
        ]
        news_client = _FakeNewsClient(news_items)
        content_client = _FakeContentClient(content_by_url)
        knowledge_client = _FakeKnowledgeClient(knowledge_by_id)

        monkeypatch.setattr(worker_module, "RawArticleStore", lambda **_: store)
        monkeypatch.setattr(
            worker_module, "NewsExtractionClient", lambda **_: news_client
        )
        monkeypatch.setattr(
            worker_module, "UrlContentClient", lambda **_: content_client
        )
        monkeypatch.setattr(
            worker_module, "KnowledgeExtractionClient", lambda **_: knowledge_client
        )

        summary = await worker_module.run_ingestion_cycle(settings)

        assert summary.discovered == 1
        assert summary.content_updated == 1
        assert summary.content_failed == 0
        assert summary.knowledge_updated == 1
        assert summary.knowledge_failed == 0
        assert len(store.content_updates) == 1
        assert len(store.knowledge_updates) == 1
        assert store.closed is True
        assert news_client.closed is True
        assert content_client.closed is True
        assert knowledge_client.closed is True

    async def test_watermarks_advance_and_drive_since(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Watermark flow: initial run uses 6h fallback; subsequent run
        uses min(watermarks) - 15min rewind; only forward-moving sources
        get written."""
        existing_espn_mark = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
        existing_cbs_mark = datetime(2026, 4, 23, 11, 0, tzinfo=UTC)

        store = _FakeStore()
        store.watermarks = {
            "ESPN": existing_espn_mark,
            "CBSSports": existing_cbs_mark,
        }

        news_items = [
            NewsItem(  # advances ESPN
                url="https://espn.com/a",
                title=None,
                description=None,
                publication_date=datetime(2026, 4, 23, 12, 30, tzinfo=UTC),
                source_name="ESPN",
                publisher=None,
            ),
            NewsItem(  # older than existing ESPN mark → should NOT write
                url="https://espn.com/b",
                title=None,
                description=None,
                publication_date=datetime(2026, 4, 23, 9, 0, tzinfo=UTC),
                source_name="ESPN",
                publisher=None,
            ),
            NewsItem(  # new source entirely → should write
                url="https://nfl.com/c",
                title=None,
                description=None,
                publication_date=datetime(2026, 4, 23, 13, 0, tzinfo=UTC),
                source_name="NFL",
                publisher=None,
            ),
            NewsItem(  # missing pub_date → ignored for watermark
                url="https://cbs.com/d",
                title=None,
                description=None,
                publication_date=None,
                source_name="CBSSports",
                publisher=None,
            ),
        ]
        news_client = _FakeNewsClient(news_items)

        monkeypatch.setattr(worker_module, "RawArticleStore", lambda **_: store)
        monkeypatch.setattr(
            worker_module, "NewsExtractionClient", lambda **_: news_client
        )
        monkeypatch.setattr(
            worker_module, "UrlContentClient", lambda **_: _FakeContentClient({})
        )
        monkeypatch.setattr(
            worker_module,
            "KnowledgeExtractionClient",
            lambda **_: _FakeKnowledgeClient({}),
        )

        await worker_module.run_ingestion_cycle(settings)

        # `since` = min(ESPN=10:00, CBSSports=11:00) - 15min = 09:45
        assert news_client.last_kwargs["since"] == existing_espn_mark - timedelta(
            minutes=15
        )

        assert len(store.watermark_writes) == 1
        written = store.watermark_writes[0]
        # ESPN advanced to 12:30; new source NFL appeared.
        assert written["ESPN"] == datetime(2026, 4, 23, 12, 30, tzinfo=UTC)
        assert written["NFL"] == datetime(2026, 4, 23, 13, 0, tzinfo=UTC)
        # CBSSports was never advanced (no item with a pub_date for it).
        assert "CBSSports" not in written

    async def test_first_run_uses_6h_fallback(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()  # no watermarks
        news_client = _FakeNewsClient([])

        monkeypatch.setattr(worker_module, "RawArticleStore", lambda **_: store)
        monkeypatch.setattr(
            worker_module, "NewsExtractionClient", lambda **_: news_client
        )
        monkeypatch.setattr(
            worker_module, "UrlContentClient", lambda **_: _FakeContentClient({})
        )
        monkeypatch.setattr(
            worker_module,
            "KnowledgeExtractionClient",
            lambda **_: _FakeKnowledgeClient({}),
        )

        before = datetime.now(UTC)
        await worker_module.run_ingestion_cycle(settings)
        after = datetime.now(UTC)

        since = news_client.last_kwargs["since"]
        assert before - timedelta(hours=6, seconds=1) <= since <= after - timedelta(
            hours=5, minutes=59
        )

    async def test_content_missing_from_batch_marks_failed(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        store.pending_by_status["discovered"] = [
            PendingArticle(id="x", url="https://ex.com/x", title=None, content=None),
        ]
        news_client = _FakeNewsClient([])
        content_client = _FakeContentClient({})  # empty response
        knowledge_client = _FakeKnowledgeClient({})

        monkeypatch.setattr(worker_module, "RawArticleStore", lambda **_: store)
        monkeypatch.setattr(
            worker_module, "NewsExtractionClient", lambda **_: news_client
        )
        monkeypatch.setattr(
            worker_module, "UrlContentClient", lambda **_: content_client
        )
        monkeypatch.setattr(
            worker_module, "KnowledgeExtractionClient", lambda **_: knowledge_client
        )

        summary = await worker_module.run_ingestion_cycle(settings)
        assert summary.content_failed == 1
        assert store.failures[0][1]["stage"] == "content"

    async def test_knowledge_failure_marks_row_failed(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        store.pending_by_status["content_ok"] = [
            PendingArticle(id="y", url="u", title="t", content="body")
        ]
        monkeypatch.setattr(worker_module, "RawArticleStore", lambda **_: store)
        monkeypatch.setattr(
            worker_module, "NewsExtractionClient", lambda **_: _FakeNewsClient([])
        )
        monkeypatch.setattr(
            worker_module, "UrlContentClient", lambda **_: _FakeContentClient({})
        )
        monkeypatch.setattr(
            worker_module,
            "KnowledgeExtractionClient",
            lambda **_: _FakeKnowledgeClient({"y": JobFailedError("LLM down")}),
        )

        summary = await worker_module.run_ingestion_cycle(settings)
        assert summary.knowledge_failed == 1
        assert store.failures[-1][1]["stage"] == "knowledge"

    async def test_missing_config_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-key")
        for var in (
            "NEWS_EXTRACTION_SUBMIT_URL",
            "NEWS_EXTRACTION_POLL_URL",
            "URL_CONTENT_EXTRACTION_SUBMIT_URL",
            "URL_CONTENT_EXTRACTION_POLL_URL",
            "KNOWLEDGE_EXTRACTION_SUBMIT_URL",
            "KNOWLEDGE_EXTRACTION_POLL_URL",
            "EXTRACTION_FUNCTION_AUTH_TOKEN",
            "OPENAI_MODEL_ARTICLE_DATA_AGENT",
            "OPENAI_MODEL_STORY_CLUSTER_AGENT",
            "OPENAI_MODEL_EDITORIAL_ORCHESTRATOR_AGENT",
            "OPENAI_MODEL_ARTICLE_WRITER_AGENT",
        ):
            monkeypatch.delenv(var, raising=False)
        s = Settings(_env_file=None)
        with pytest.raises(RuntimeError, match="NEWS_EXTRACTION_SUBMIT_URL"):
            await worker_module.run_ingestion_cycle(s)
