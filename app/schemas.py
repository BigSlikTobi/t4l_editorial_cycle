from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# --- Inbound from feed ---


class EntityMatch(BaseModel):
    entity_type: str
    entity_id: str
    matched_name: str


class RawArticle(BaseModel):
    id: str
    url: str
    title: str
    source_name: str
    category: str | None = None
    facts_count: int = 0
    entities: list[EntityMatch] = Field(default_factory=list)


# --- Article content lookup ---


class StoredArticleRecord(BaseModel):
    url: str
    header: str | None = None
    content: str
    description: str | None = None
    quotes: list[str] = Field(default_factory=list)


class ArticleContentLookupToolResponse(BaseModel):
    requested_url: str
    found: bool
    article: StoredArticleRecord | None = None


# --- Article Data Agent output ---


class ArticleDigestAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    key_facts: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    content_status: Literal["full", "thin", "missing"] = "full"


class ArticleDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_id: str
    url: str
    title: str
    source_name: str
    summary: str
    key_facts: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    content_status: Literal["full", "thin", "missing"] = "full"
    team_mentions: list[str] = Field(default_factory=list)


# --- Story Cluster Agent output ---


class StoryClusterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster_headline: str
    synthesis: str
    news_value_score: float = Field(ge=0.0, le=1.0)
    is_new: bool
    story_fingerprint: str
    source_digests: list[ArticleDigest] = Field(default_factory=list)
    team_codes: list[str] = Field(default_factory=list)


# --- Editorial Cycle Orchestrator output ---


class PlayerMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str  # maps to public.players.player_id (GSIS format, e.g. "00-0026158")
    name: str


class StoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    cluster_headline: str
    story_fingerprint: str
    action: Literal["publish", "update", "skip"]
    news_value_score: float = Field(ge=0.0, le=1.0)
    reasoning: str
    source_digests: list[ArticleDigest] = Field(default_factory=list)
    team_codes: list[str] = Field(default_factory=list)
    player_mentions: list[PlayerMention] = Field(default_factory=list)
    existing_article_id: int | None = None


class CyclePublishPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stories: list[StoryEntry] = Field(default_factory=list)
    skipped_stories: list[StoryEntry] = Field(default_factory=list)
    reasoning: str
    prevented_duplicates: int = Field(default=0, ge=0)


# --- Article Writer Agent output (maps to content.team_article) ---


class PublishableArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team: str
    language: str = "en-US"
    headline: str
    sub_headline: str
    introduction: str
    content: str
    x_post: str
    bullet_points: str
    story_fingerprint: str
    author: str | None = None
    mentioned_players: list[str] = Field(default_factory=list)
    image: str | None = None
    tts_file: str | None = None


class PersonaSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_id: Literal["analyst", "insider", "columnist"]
    reasoning: str


# --- Editorial state (cross-cycle memory) ---


class PublishedStoryRecord(BaseModel):
    id: int | None = None
    story_fingerprint: str
    published_at: datetime
    last_updated_at: datetime
    supabase_article_id: int
    cycle_id: str
    cluster_headline: str = ""
    source_urls: list[str] = Field(default_factory=list)


# --- Cycle result ---


class CycleResult(BaseModel):
    cycle_id: str
    generated_at: datetime
    plan: CyclePublishPlan
    articles_written: int = 0
    articles_updated: int = 0
    prevented_duplicates: int = 0
    warnings: list[str] = Field(default_factory=list)
