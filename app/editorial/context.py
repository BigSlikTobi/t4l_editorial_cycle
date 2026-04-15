from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.schemas import ArticleDigest, PublishedStoryRecord, RawArticle


@dataclass(slots=True)
class CycleRunContext:
    cycle_id: str
    generated_at: datetime
    lookback_hours: int
    top_n: int
    raw_articles: list[RawArticle] = field(default_factory=list)
    published_state: list[PublishedStoryRecord] = field(default_factory=list)
    article_digests: list[ArticleDigest] = field(default_factory=list)
    deduplicated_count: int = 0
    prevented_duplicates: int = 0
    warnings: list[str] = field(default_factory=list)
