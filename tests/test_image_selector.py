from __future__ import annotations

from typing import Any

import pytest

from app.schemas import (
    ArticleDigest,
    PlayerMention,
    PublishableArticle,
    StoryEntry,
)
from app.writer.image_selector import (
    HeadshotBudget,
    ImageResult,
    ImageSelector,
    team_logo_ref,
)


# --- Fakes --------------------------------------------------------------


class FakeImageClient:
    def __init__(
        self,
        url: str | None = None,
        raise_exc: Exception | None = None,
        source: str = "",
        author: str = "",
        original_url: str | None = None,
    ):
        self.url = url
        self.raise_exc = raise_exc
        self.source = source
        self.author = author
        self.original_url = original_url
        self.calls: list[dict] = []

    async def select_image(self, **kwargs: Any):
        from app.writer.image_clients import ImageCandidate

        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        if self.url is None:
            return None
        return ImageCandidate(
            image_url=self.url,
            original_url=self.original_url or self.url,
            source=self.source,
            author=self.author,
        )


class FakeGeminiClient:
    def __init__(self, urls: list[str] | None = None):
        # one entry consumed per generate_image call
        self.urls = list(urls or [])
        self.prompts: list[str] = []

    async def generate_image(self, prompt: str) -> str | None:
        self.prompts.append(prompt)
        return self.urls.pop(0) if self.urls else None


class FakeValidator:
    def __init__(
        self,
        match_results: list[tuple[bool, str]] | None = None,
        text_results: list[tuple[bool, str]] | None = None,
    ):
        self.match_results = list(match_results or [])
        self.text_results = list(text_results or [])
        self.match_calls: list[tuple[str, str, str]] = []
        self.text_calls: list[str] = []

    async def does_image_match(
        self, url: str, headline: str, intro: str, **kwargs: Any
    ) -> tuple[bool, str]:
        self.match_calls.append((url, headline, intro))
        self.last_kwargs = kwargs
        return self.match_results.pop(0) if self.match_results else (False, "no stub")

    async def image_contains_text(self, url: str) -> tuple[bool, str]:
        self.text_calls.append(url)
        return self.text_results.pop(0) if self.text_results else (True, "no stub")


class FakeHTTPResponse:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body

    @property
    def text(self) -> str:
        return str(self._body)

    def json(self) -> Any:
        return self._body


class FakeHTTPClient:
    def __init__(self, response: FakeHTTPResponse | Exception):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, params: dict | None = None, **kwargs: Any):
        self.calls.append((url, params or {}))
        if isinstance(self.response, Exception):
            raise self.response
        # Attach minimal fields needed for image download path too
        resp = self.response
        if not hasattr(resp, "headers"):
            resp.headers = {"content-type": "image/jpeg"}
            resp.content = b"fake-bytes"
        return resp

    async def aclose(self) -> None:
        pass


class FakeUploader:
    def __init__(self):
        self.uploads: list[tuple[bytes, str, str]] = []
        self.metadata: list[dict] = []

    async def upload(self, data: bytes, content_type: str, path: str) -> str:
        self.uploads.append((data, content_type, path))
        return f"https://supabase.test/storage/v1/object/public/images/{path}"

    async def record_metadata(self, **kwargs: Any) -> int | None:
        self.metadata.append(kwargs)
        return len(self.metadata)


# --- Fixtures -----------------------------------------------------------


def _story(
    team_codes: list[str] | None = None,
    players: list[PlayerMention] | None = None,
) -> StoryEntry:
    digest = ArticleDigest(
        story_id="s1",
        url="http://a",
        title="T",
        source_name="X",
        summary="s",
        key_facts=["f"],
        confidence=0.5,
        content_status="full",
    )
    return StoryEntry(
        rank=1,
        cluster_headline="hl",
        story_fingerprint="fp1",
        action="publish",
        news_value_score=0.8,
        reasoning="r",
        source_digests=[digest],
        team_codes=team_codes or [],
        player_mentions=players or [],
    )


def _article(headline: str = "Chiefs trade for WR") -> PublishableArticle:
    return PublishableArticle(
        team="KC",
        headline=headline,
        sub_headline="sub",
        introduction="intro",
        content="body",
        x_post="x",
        bullet_points="b",
        story_fingerprint="fp1",
    )


def _make_selector(
    *,
    image_client=None,
    gemini_client=None,
    wikimedia_client=None,
    validator=None,
    http_response=FakeHTTPResponse(200, []),
    uploader: FakeUploader | None = None,
    gemini_model_name: str = "gemini-test",
) -> ImageSelector:
    selector = ImageSelector.__new__(ImageSelector)
    selector._supabase_base = "http://supabase.test"
    selector._supabase_key = "key"
    selector._image_client = image_client
    selector._gemini_client = gemini_client
    selector._wikimedia_client = wikimedia_client
    selector._validator = validator or FakeValidator()
    selector._uploader = uploader
    selector._gemini_model_name = gemini_model_name
    selector._http = FakeHTTPClient(http_response)
    return selector


class FakeWikimediaClient:
    def __init__(
        self,
        url: str | None = None,
        raise_exc: Exception | None = None,
    ):
        self.url = url
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    async def search_image(self, query: str):
        from app.writer.image_clients import ImageCandidate

        self.calls.append(query)
        if self.raise_exc:
            raise self.raise_exc
        if self.url is None:
            return None
        return ImageCandidate(
            image_url=self.url,
            original_url=f"https://commons.wikimedia.org/wiki/File:{self.url.split('/')[-1]}",
            source="wikimedia_commons",
            author="Photographer X",
        )


# --- Tier 1 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier1_accepts_when_validator_approves_and_uploads():
    uploader = FakeUploader()
    # HTTP response is reused as the download response too (mock adds headers/content).
    selector = _make_selector(
        image_client=FakeImageClient(
            url="http://img.example.com/1.jpg",
            source="example.com",
            author="J. Photographer",
            original_url="http://origin.example.com/page",
        ),
        validator=FakeValidator(match_results=[(True, "looks right")]),
        uploader=uploader,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"]), cycle_id="cyc-1"
    )
    assert result.tier == "image_search"
    assert result.url.startswith("https://supabase.test/storage/v1/object/public/images/")
    assert "cyc-1" in result.url
    assert "image_search" in result.url
    # provenance comes from the cloud function's returned fields
    assert len(uploader.metadata) == 1
    assert uploader.metadata[0]["original_url"] == "http://origin.example.com/page"
    assert uploader.metadata[0]["source"] == "example.com"
    assert uploader.metadata[0]["author"] == "J. Photographer"


@pytest.mark.asyncio
async def test_tier1_falls_through_when_validator_rejects():
    # No gemini, no headshot → should end at team logo (tier 4)
    selector = _make_selector(
        image_client=FakeImageClient(url="http://img/1.jpg"),
        validator=FakeValidator(match_results=[(False, "not football")]),
        http_response=FakeHTTPResponse(200, []),  # no headshot row
    )
    result = await selector.select(_article(), _story(team_codes=["KC"]))
    assert result.tier == "team_logo"
    assert result.url == team_logo_ref("KC")


@pytest.mark.asyncio
async def test_tier1_skipped_when_no_client():
    selector = _make_selector(
        image_client=None,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(_article(), _story(team_codes=["KC"]))
    assert result.tier == "team_logo"  # falls straight to logo


# --- Tier 1 Wikimedia fallback -----------------------------------------


@pytest.mark.asyncio
async def test_wikimedia_fallback_used_when_google_returns_none():
    uploader = FakeUploader()
    wiki = FakeWikimediaClient(url="http://upload.wikimedia.org/foo.jpg")
    selector = _make_selector(
        image_client=FakeImageClient(url=None),  # google empty
        wikimedia_client=wiki,
        validator=FakeValidator(match_results=[(True, "sideline photo")]),
        uploader=uploader,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"]), cycle_id="cyc-w"
    )
    assert result.tier == "image_search"
    assert "wikimedia" in result.notes
    assert wiki.calls == ["KC NFL football"]
    assert uploader.metadata[0]["source"] == "wikimedia_commons"


@pytest.mark.asyncio
async def test_wikimedia_fallback_used_when_google_rejected():
    uploader = FakeUploader()
    wiki = FakeWikimediaClient(url="http://upload.wikimedia.org/bar.jpg")
    selector = _make_selector(
        image_client=FakeImageClient(url="http://g/x.jpg"),
        wikimedia_client=wiki,
        # first match-call (google) rejects, second (wikimedia) accepts
        validator=FakeValidator(
            match_results=[(False, "wrong sport"), (True, "practice scene")]
        ),
        uploader=uploader,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"]), cycle_id="cyc-w2"
    )
    assert result.tier == "image_search"
    assert "wikimedia" in result.notes


@pytest.mark.asyncio
async def test_wikimedia_prefers_player_name_when_available():
    wiki = FakeWikimediaClient(url=None)
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        wikimedia_client=wiki,
        http_response=FakeHTTPResponse(200, []),
    )
    await selector.select(
        _article(),
        _story(
            team_codes=["KC"],
            # two players → no dominant → tier 1 runs, wikimedia uses first player
            players=[
                PlayerMention(id="00-001", name="Patrick Mahomes"),
                PlayerMention(id="00-002", name="Travis Kelce"),
            ],
        ),
    )
    assert wiki.calls == ["Patrick Mahomes"]


@pytest.mark.asyncio
async def test_validator_receives_expected_team_context():
    validator = FakeValidator(match_results=[(True, "ok")])
    selector = _make_selector(
        image_client=FakeImageClient(url="http://g/x.jpg"),
        wikimedia_client=None,
        validator=validator,
        uploader=FakeUploader(),
        http_response=FakeHTTPResponse(200, []),
    )
    await selector.select(_article(), _story(team_codes=["LV"]), cycle_id="c")
    assert validator.last_kwargs["expected_team_code"] == "LV"
    assert validator.last_kwargs["expected_team_name"] == "Las Vegas Raiders"


@pytest.mark.asyncio
async def test_both_web_sources_reject_falls_through_to_logo():
    selector = _make_selector(
        image_client=FakeImageClient(url="http://g/x.jpg"),
        wikimedia_client=FakeWikimediaClient(url="http://w/y.jpg"),
        validator=FakeValidator(
            match_results=[(False, "google bad"), (False, "wiki bad")]
        ),
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(_article(), _story(team_codes=["KC"]))
    assert result.tier == "team_logo"


# --- Tier 2 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier2_uses_headshot_when_single_player():
    selector = _make_selector(
        image_client=FakeImageClient(url=None),  # tier 1 returns nothing
        http_response=FakeHTTPResponse(
            200, [{"headshot": "http://headshot.jpg", "display_name": "A Player"}]
        ),
    )
    result = await selector.select(
        _article(),
        _story(team_codes=["KC"], players=[PlayerMention(id="00-001", name="A Player")]),
    )
    assert result.tier == "player_headshot"
    assert result.url == "http://headshot.jpg"


@pytest.mark.asyncio
async def test_tier2_skipped_when_ambiguous_players_and_no_headline_match():
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(headline="Two stars swap places"),
        _story(
            team_codes=["KC"],
            players=[
                PlayerMention(id="00-001", name="Alpha One"),
                PlayerMention(id="00-002", name="Bravo Two"),
            ],
        ),
    )
    assert result.tier == "team_logo"  # no dominant player → skip to logo


@pytest.mark.asyncio
async def test_tier2_picks_headline_match_when_multiple_players():
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        http_response=FakeHTTPResponse(
            200, [{"headshot": "http://hs.jpg", "display_name": "Bravo Two"}]
        ),
    )
    result = await selector.select(
        _article(headline="Chiefs rally behind Bravo"),
        _story(
            team_codes=["KC"],
            players=[
                PlayerMention(id="00-001", name="Alpha One"),
                PlayerMention(id="00-002", name="Bravo Two"),
            ],
        ),
    )
    assert result.tier == "player_headshot"
    assert result.url == "http://hs.jpg"


@pytest.mark.asyncio
async def test_tier2_skipped_when_no_headshot_in_table():
    # single player, but players row has no headshot → fall through
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        http_response=FakeHTTPResponse(200, [{"headshot": None, "display_name": "X"}]),
    )
    result = await selector.select(
        _article(),
        _story(team_codes=["KC"], players=[PlayerMention(id="00-001", name="A")]),
    )
    assert result.tier == "team_logo"


# --- Tier 3 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tier3_accepts_and_uploads_bytes():
    uploader = FakeUploader()
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        gemini_client=FakeGeminiClient(urls=["data:image/png;base64,AAAA"]),
        validator=FakeValidator(
            text_results=[(False, "clean")],
            match_results=[(True, "stadium shot")],
        ),
        uploader=uploader,
        gemini_model_name="gemini-3.1-flash-image-preview",
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"]), cycle_id="cyc-2"
    )
    assert result.tier == "ai_generated"
    assert result.url.startswith("https://supabase.test/storage/v1/object/public/images/")
    assert "ai_generated" in result.url
    # Uploaded decoded bytes (AAAA decodes to 3 bytes)
    assert len(uploader.uploads) == 1
    data, content_type, path = uploader.uploads[0]
    assert content_type == "image/png"
    assert data == b"\x00\x00\x00"
    assert path.endswith(".png")
    # Provenance
    assert uploader.metadata[0]["source"] == "gemini"
    assert uploader.metadata[0]["author"] == "gemini-3.1-flash-image-preview"
    # original_url is now unique per image — includes the storage path suffix
    assert uploader.metadata[0]["original_url"].startswith(
        "gemini://gemini-3.1-flash-image-preview/"
    )
    assert uploader.metadata[0]["original_url"].endswith(".png")


@pytest.mark.asyncio
async def test_tier3_regenerates_once_on_text_then_accepts():
    uploader = FakeUploader()
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        gemini_client=FakeGeminiClient(
            urls=["data:image/png;base64,AAAA", "data:image/png;base64,BBBB"]
        ),
        validator=FakeValidator(
            text_results=[(True, "has 'KC' text"), (False, "clean")],
            match_results=[(True, "ok")],
        ),
        uploader=uploader,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"]), cycle_id="cyc-3"
    )
    assert result.tier == "ai_generated"
    # The SECOND generation (bytes from "BBBB") is the one uploaded
    assert len(uploader.uploads) == 1
    assert uploader.uploads[0][0] == b"\x04\x10\x41"  # base64("BBBB")


@pytest.mark.asyncio
async def test_tier3_rejects_when_text_after_retry():
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        gemini_client=FakeGeminiClient(urls=["data:a", "data:b"]),
        validator=FakeValidator(
            text_results=[(True, "text"), (True, "still text")],
        ),
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(_article(), _story(team_codes=["KC"]))
    assert result.tier == "team_logo"  # fell through to logo


# --- Tier 4 / 5 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tier4_logo_reference_format():
    selector = _make_selector(
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(_article(), _story(team_codes=["KC"]))
    assert result.tier == "team_logo"
    assert result.url == "asset://team_logo/KC"


@pytest.mark.asyncio
async def test_generic_nfl_fallback_when_no_team():
    """No team + all other tiers exhausted → generic NFL asset, never null."""
    selector = _make_selector(
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(_article(), _story(team_codes=[]))
    assert result.tier == "generic_nfl"
    assert result.url == "asset://generic/nfl"


def test_logo_ref_helper():
    assert team_logo_ref("NYJ") == "asset://team_logo/NYJ"


# --- Budget --------------------------------------------------------------


@pytest.mark.asyncio
async def test_headshot_budget_enforces_capacity():
    budget = HeadshotBudget(capacity=2)
    assert await budget.try_consume() is True
    assert await budget.try_consume() is True
    assert await budget.try_consume() is False
    assert budget.used == 2


def test_headshot_budget_for_cycle_floor():
    # 5 articles × 0.5 = 2.5 → floor 2
    assert HeadshotBudget.for_cycle(5).capacity == 2
    # 4 articles × 0.5 = 2
    assert HeadshotBudget.for_cycle(4).capacity == 2
    # 1 article → guarantee at least 1 (floor(0.5)=0 would strand it)
    assert HeadshotBudget.for_cycle(1).capacity == 1
    # 0 → 0
    assert HeadshotBudget.for_cycle(0).capacity == 0


@pytest.mark.asyncio
async def test_tier2_falls_through_when_budget_exhausted():
    uploader = FakeUploader()
    budget = HeadshotBudget(capacity=0)  # no headshots allowed this cycle
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        gemini_client=None,
        uploader=uploader,
        http_response=FakeHTTPResponse(
            200, [{"headshot": "http://hs.jpg", "display_name": "A"}]
        ),
    )
    result = await selector.select(
        _article(),
        _story(team_codes=["KC"], players=[PlayerMention(id="00-001", name="A")]),
        cycle_id="cyc-budget",
        headshot_budget=budget,
    )
    # No headshot slot, no gemini → falls to logo
    assert result.tier == "team_logo"
    assert budget.used == 0  # budget never burned


@pytest.mark.asyncio
async def test_tier2_consumes_budget_on_success():
    budget = HeadshotBudget(capacity=1)
    selector = _make_selector(
        image_client=FakeImageClient(url=None),
        http_response=FakeHTTPResponse(
            200, [{"headshot": "http://hs.jpg", "display_name": "A"}]
        ),
    )
    result = await selector.select(
        _article(),
        _story(team_codes=["KC"], players=[PlayerMention(id="00-001", name="A")]),
        cycle_id="cyc-budget",
        headshot_budget=budget,
    )
    assert result.tier == "player_headshot"
    assert budget.used == 1


@pytest.mark.asyncio
async def test_tier1_skipped_when_dominant_player_present():
    """Budget has room + dominant player exists → tier 1 pre-skipped."""
    budget = HeadshotBudget(capacity=5)
    image_client = FakeImageClient(url="http://img.example.com/x.jpg")
    selector = _make_selector(
        image_client=image_client,
        validator=FakeValidator(match_results=[(True, "ok")]),
        http_response=FakeHTTPResponse(
            200, [{"headshot": "http://hs.jpg", "display_name": "A"}]
        ),
    )
    result = await selector.select(
        _article(),
        _story(team_codes=["KC"], players=[PlayerMention(id="00-001", name="A")]),
        cycle_id="cyc",
        headshot_budget=budget,
    )
    # Tier 1 was SKIPPED (image_client never called)
    assert len(image_client.calls) == 0
    assert result.tier == "player_headshot"


@pytest.mark.asyncio
async def test_tier1_runs_when_no_dominant_player():
    """No dominant player → tier 1 runs normally."""
    budget = HeadshotBudget(capacity=5)
    uploader = FakeUploader()
    image_client = FakeImageClient(
        url="http://img.example.com/x.jpg",
        source="example.com",
        original_url="http://origin/",
    )
    selector = _make_selector(
        image_client=image_client,
        validator=FakeValidator(match_results=[(True, "ok")]),
        uploader=uploader,
        http_response=FakeHTTPResponse(200, []),
    )
    result = await selector.select(
        _article(), _story(team_codes=["KC"], players=[]),
        cycle_id="cyc", headshot_budget=budget,
    )
    assert len(image_client.calls) == 1
    assert result.tier == "image_search"
