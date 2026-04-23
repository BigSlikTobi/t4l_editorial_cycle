"""HTTP clients for the three extraction cloud functions.

Each service exposes a submit/poll async contract. `AsyncJobClient` in
`base.py` implements the submit → poll loop once; the three concrete
clients (news, url_content, knowledge) wrap it with typed payloads.

The ingestion worker (app/ingestion/worker.py) orchestrates all three.
"""
from app.clients.base import (
    AsyncJobClient,
    JobFailedError,
    JobTimeoutError,
    SupabaseJobsConfig,
)
from app.clients.knowledge_extraction import (
    KnowledgeExtractionClient,
    KnowledgeResult,
    ResolvedEntity,
    Topic,
)
from app.clients.news_extraction import NewsExtractionClient, NewsItem
from app.clients.url_content import ContentResult, UrlContentClient

__all__ = [
    "AsyncJobClient",
    "ContentResult",
    "JobFailedError",
    "JobTimeoutError",
    "KnowledgeExtractionClient",
    "KnowledgeResult",
    "NewsExtractionClient",
    "NewsItem",
    "ResolvedEntity",
    "SupabaseJobsConfig",
    "Topic",
    "UrlContentClient",
]
