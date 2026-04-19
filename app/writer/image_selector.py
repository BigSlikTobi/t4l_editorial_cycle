"""Image selection cascade for writer output.

Tier order (agreed with product):
  1. image_selection cloud function + vision validator
  2. Player headshot from public.players (dominant player only)
  3. Gemini AI generation (stadium / tactical, no text) + validator + OCR check
  4. Team logo (Flutter asset reference: asset://team_logo/{TEAM_CODE})
  5. None

Principle: wrong image > bad quality > no image. Every interesting-but-
unverified source (tiers 1 and 3) is gated by the vision validator. Boring-
but-safe sources (tiers 2 and 4) skip validation because they cannot be
"wrong" — they are always the named player or the named team.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from app.adapters import ImageUploader
from app.schemas import PlayerMention, PublishableArticle, StoryEntry
from app.team_codes import normalize_team_codes, team_colors, team_full_name
from app.writer.image_clients import (
    GeminiImageClient,
    ImageCandidate,
    ImageSelectionClient,
    WikimediaCommonsClient,
)
from app.writer.image_validator import ImageValidator

logger = logging.getLogger(__name__)

Tier = Literal["image_search", "player_headshot", "ai_generated", "team_logo", "generic_nfl", "none"]

_LOGO_SCHEME = "asset://team_logo/"
_GENERIC_NFL_ASSET = "asset://generic/nfl"


@dataclass(frozen=True)
class ImageResult:
    url: str | None
    tier: Tier
    notes: str


def team_logo_ref(team_code: str) -> str:
    return f"{_LOGO_SCHEME}{team_code}"


class HeadshotBudget:
    """Per-cycle cap on how many articles may use tier 2 (player headshot).

    Stops repetitive "another mugshot" feeds by forcing surplus articles to
    fall through to tier 3 (AI-generated) or tier 4 (team logo) instead.
    Async-safe — selectors run in parallel and share one budget instance.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, capacity)
        self._used = 0
        self._lock = asyncio.Lock()

    @classmethod
    def for_cycle(cls, publishable_count: int, ratio: float = 0.5) -> "HeadshotBudget":
        """Round DOWN so the cap never exceeds the stated ratio.

        Edge case: for publishable_count == 1, floor(0.5) == 0 would mean
        a single-article cycle could never use a headshot. Guarantee at
        least 1 slot when there's any article at all.
        """
        cap = max(1, math.floor(publishable_count * ratio)) if publishable_count else 0
        return cls(cap)

    async def try_consume(self) -> bool:
        async with self._lock:
            if self._used >= self._capacity:
                return False
            self._used += 1
            return True

    @property
    def used(self) -> int:
        return self._used

    @property
    def capacity(self) -> int:
        return self._capacity


class ImageSelector:
    def __init__(
        self,
        *,
        supabase_url: str,
        supabase_service_role_key: str,
        image_client: ImageSelectionClient | None,
        gemini_client: GeminiImageClient | None,
        validator: ImageValidator,
        uploader: ImageUploader | None = None,
        wikimedia_client: WikimediaCommonsClient | None = None,
        gemini_model_name: str = "gemini",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._supabase_base = supabase_url.rstrip("/")
        self._supabase_key = supabase_service_role_key
        self._image_client = image_client
        self._gemini_client = gemini_client
        self._wikimedia_client = wikimedia_client
        self._validator = validator
        self._uploader = uploader
        self._gemini_model_name = gemini_model_name
        # User-Agent is required by Wikimedia's upload.wikimedia.org CDN
        # (403 without it) and is good etiquette for any external download.
        self._http = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {supabase_service_role_key}",
                "apikey": supabase_service_role_key,
                "User-Agent": "t4l-editorial-cycle/0.1 (contact: businesstobiaslatta@gmail.com)",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def select(
        self,
        article: PublishableArticle,
        story: StoryEntry,
        *,
        cycle_id: str | None = None,
        headshot_budget: "HeadshotBudget | None" = None,
    ) -> ImageResult:
        """Run the cascade. Each tier's failure is logged; never raises."""
        normalized_teams = normalize_team_codes(story.team_codes)
        primary_team = normalized_teams[0] if normalized_teams else None
        dominant_player = self._pick_dominant_player(article, story)

        # Tier 1 pre-check: if we have a dominant player AND the budget still
        # has room, a headshot is likely going to win anyway — skip the
        # expensive image_search round-trip.
        skip_tier1 = (
            dominant_player is not None
            and (headshot_budget is None or headshot_budget.capacity > headshot_budget.used)
        )

        # Tier 1: image_selection + validator → upload to bucket
        if not skip_tier1:
            result = await self._try_image_search(
                article, story, cycle_id=cycle_id, primary_team=primary_team
            )
            if result.url:
                return result
        else:
            logger.info(
                "Skipping tier 1 for %s (dominant player %s likely wins tier 2)",
                article.headline[:60], dominant_player.id if dominant_player else "?",
            )

        # Tier 2: player headshot — gated by per-cycle budget
        result = await self._try_player_headshot(
            article, story, dominant_player=dominant_player, budget=headshot_budget
        )
        if result.url:
            return result

        # Tier 3: AI generation (no text, validator + OCR) → upload to bucket
        result = await self._try_ai_generation(
            article, primary_team, cycle_id=cycle_id, story=story
        )
        if result.url:
            return result

        # Tier 4: team logo reference (FE-resolved asset)
        if primary_team:
            return ImageResult(
                url=team_logo_ref(primary_team),
                tier="team_logo",
                notes=f"fallback logo for {primary_team}",
            )

        # Tier 4b: generic NFL asset — league-wide stories with no team still
        # get SOMETHING rather than nothing. FE resolves asset://generic/nfl.
        return ImageResult(
            url=_GENERIC_NFL_ASSET,
            tier="generic_nfl",
            notes="no team — generic NFL asset",
        )

    # ------------------------------------------------------------------
    # Tier 1 — image_selection + validator
    # ------------------------------------------------------------------

    async def _try_image_search(
        self,
        article: PublishableArticle,
        story: StoryEntry,
        *,
        cycle_id: str | None,
        primary_team: str | None = None,
    ) -> ImageResult:
        """Try each configured web source in order; first validator-approved
        candidate wins. Sources: (a) Google CC via image_selection function,
        (b) Wikimedia Commons. Both gated by the same vision validator."""
        notes: list[str] = []

        # (a) Google CC search via the deployed cloud function
        if self._image_client is not None:
            candidate = await self._fetch_from_google(article, story)
            result = await self._accept_candidate(
                candidate, article, story,
                cycle_id=cycle_id, source_label="google", primary_team=primary_team,
            )
            if result.url:
                return result
            notes.append(f"google: {result.notes}")

        # (b) Wikimedia Commons fallback — broader supply of CC-licensed NFL
        # photos (stadiums, coaches, historical players).
        if self._wikimedia_client is not None:
            candidate = await self._fetch_from_wikimedia(article, story)
            result = await self._accept_candidate(
                candidate, article, story,
                cycle_id=cycle_id, source_label="wikimedia", primary_team=primary_team,
            )
            if result.url:
                return result
            notes.append(f"wikimedia: {result.notes}")

        if not notes:
            return ImageResult(None, "image_search", "no source configured")
        return ImageResult(None, "image_search", "; ".join(notes))

    async def _fetch_from_google(
        self, article: PublishableArticle, story: StoryEntry
    ) -> ImageCandidate | None:
        if self._image_client is None:
            return None
        required_terms = self._build_required_terms(article, story)
        article_text = f"{article.headline}\n\n{article.introduction}\n\n{article.content}"
        try:
            return await self._image_client.select_image(
                article_text=article_text,
                required_terms=required_terms,
                num_images=1,
            )
        except Exception as exc:
            logger.warning("Google image_selection failed: %s", exc)
            return None

    async def _fetch_from_wikimedia(
        self, article: PublishableArticle, story: StoryEntry
    ) -> ImageCandidate | None:
        if self._wikimedia_client is None:
            return None
        query = self._build_wikimedia_query(article, story)
        if not query:
            return None
        try:
            return await self._wikimedia_client.search_image(query)
        except Exception as exc:
            logger.warning("Wikimedia search failed for %r: %s", query, exc)
            return None

    async def _accept_candidate(
        self,
        candidate: ImageCandidate | None,
        article: PublishableArticle,
        story: StoryEntry,
        *,
        cycle_id: str | None,
        source_label: str,
        primary_team: str | None = None,
    ) -> ImageResult:
        if candidate is None:
            return ImageResult(None, "image_search", "no candidate")
        matches, reason = await self._validator.does_image_match(
            candidate.image_url, article.headline, article.introduction,
            expected_team_code=primary_team,
            expected_team_name=team_full_name(primary_team),
        )
        if not matches:
            logger.info(
                "image_search (%s) validator rejected: %s", source_label, reason
            )
            return ImageResult(None, "image_search", f"rejected: {reason}")
        hosted_url = await self._download_and_host(
            candidate.image_url,
            path=self._storage_path(
                cycle_id, story, tier="image_search", ext_hint=candidate.image_url
            ),
            provenance_source=candidate.source or self._domain(candidate.original_url),
            provenance_original_url=candidate.original_url,
            provenance_author=candidate.author,
        )
        if hosted_url is None:
            return ImageResult(None, "image_search", "upload failed")
        return ImageResult(
            url=hosted_url, tier="image_search", notes=f"{source_label}: {reason}"
        )

    def _build_wikimedia_query(
        self, article: PublishableArticle, story: StoryEntry
    ) -> str:
        """Commons full-text search. Prefer player name (best hit rate);
        otherwise team code + NFL. Empty string if neither available."""
        # Player names alone get best hit rate on Commons — the "NFL" suffix
        # narrows too aggressively because most player photo files aren't
        # tagged or titled with the league name.
        if story.player_mentions:
            return story.player_mentions[0].name
        teams = normalize_team_codes(story.team_codes)
        if teams:
            return f"{teams[0]} NFL football"
        return ""

    def _build_required_terms(
        self,
        article: PublishableArticle,
        story: StoryEntry,
    ) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()

        def _add(t: str) -> None:
            low = t.lower()
            if low and low not in seen:
                seen.add(low)
                terms.append(t)

        if story.player_mentions:
            _add(story.player_mentions[0].name)
        for team in normalize_team_codes(story.team_codes):
            _add(team)
        return terms[:3]  # keep concise — looser matching = more candidates

    # ------------------------------------------------------------------
    # Tier 2 — player headshot
    # ------------------------------------------------------------------

    async def _try_player_headshot(
        self,
        article: PublishableArticle,
        story: StoryEntry,
        *,
        dominant_player: PlayerMention | None = None,
        budget: "HeadshotBudget | None" = None,
    ) -> ImageResult:
        player = dominant_player or self._pick_dominant_player(article, story)
        if player is None:
            return ImageResult(None, "player_headshot", "no dominant player")

        try:
            resp = await self._http.get(
                f"{self._supabase_base}/rest/v1/players",
                params={
                    "player_id": f"eq.{player.id}",
                    "select": "headshot,display_name",
                    "limit": 1,
                },
            )
        except Exception as exc:
            return ImageResult(None, "player_headshot", f"lookup error: {exc}")

        if resp.status_code >= 400:
            return ImageResult(
                None, "player_headshot",
                f"lookup HTTP {resp.status_code}: {resp.text[:100]}",
            )

        rows = resp.json()
        if not rows or not rows[0].get("headshot"):
            return ImageResult(None, "player_headshot", f"no headshot for {player.id}")

        # Consume the per-cycle headshot slot LAST — only after we confirmed
        # a valid headshot exists. Otherwise a dominant-player miss would
        # burn a slot for nothing.
        if budget is not None and not await budget.try_consume():
            return ImageResult(
                None, "player_headshot",
                f"headshot budget exhausted ({budget.capacity} used); falling through",
            )

        return ImageResult(
            url=rows[0]["headshot"],
            tier="player_headshot",
            notes=f"{rows[0].get('display_name', player.name)} headshot",
        )

    def _pick_dominant_player(
        self,
        article: PublishableArticle,
        story: StoryEntry,
    ) -> PlayerMention | None:
        """Dominant player = only one player in mentions, OR a player whose
        name appears in the headline. No guessing between two stars."""
        mentions = story.player_mentions
        if not mentions:
            return None
        if len(mentions) == 1:
            return mentions[0]

        headline_tokens = {t.lower().strip(".,!?;:'\"") for t in article.headline.split()}
        headline_matches = [
            m for m in mentions if self._name_tokens_hit(m.name, headline_tokens)
        ]
        if len(headline_matches) == 1:
            return headline_matches[0]
        return None

    @staticmethod
    def _name_tokens_hit(full_name: str, headline_tokens: set[str]) -> bool:
        """True if any significant (3+ char) token of the name appears in the headline."""
        for part in full_name.split():
            token = part.lower().strip(".,!?;:'\"")
            if len(token) >= 3 and token in headline_tokens:
                return True
        return False

    # ------------------------------------------------------------------
    # Tier 3 — AI generation
    # ------------------------------------------------------------------

    async def _try_ai_generation(
        self,
        article: PublishableArticle,
        primary_team: str | None,
        *,
        cycle_id: str | None,
        story: StoryEntry,
    ) -> ImageResult:
        if self._gemini_client is None:
            return ImageResult(None, "ai_generated", "client not configured")

        prompt = self._build_generation_prompt(article, primary_team)

        for attempt in (1, 2):
            try:
                data_url = await self._gemini_client.generate_image(prompt)
            except Exception as exc:
                return ImageResult(None, "ai_generated", f"gemini error: {exc}")

            if not data_url:
                return ImageResult(None, "ai_generated", "gemini returned no image")

            # Must be text-free
            has_text, text_notes = await self._validator.image_contains_text(data_url)
            if has_text:
                if attempt == 1:
                    logger.info("AI image contained text (%s) — regenerating once", text_notes)
                    prompt = prompt + "\n\nREMINDER: absolutely no letters, numbers, watermarks, or any text of any kind."
                    continue
                logger.info("AI image still contained text after retry: %s", text_notes)
                return ImageResult(None, "ai_generated", f"contained text after retry: {text_notes}")

            # Must match the article topically
            matches, reason = await self._validator.does_image_match(
                data_url, article.headline, article.introduction,
                expected_team_code=primary_team,
                expected_team_name=team_full_name(primary_team),
            )
            if not matches:
                logger.info(
                    "AI image match-validator rejected: %s", reason
                )
                return ImageResult(None, "ai_generated", f"validator rejected: {reason}")

            # Decode + upload
            decoded = self._decode_data_url(data_url)
            if decoded is None:
                return ImageResult(None, "ai_generated", "malformed data URL")
            content_type, data_bytes = decoded
            ext = content_type.split("/")[-1] if "/" in content_type else "png"
            path = self._storage_path(
                cycle_id, story, tier="ai_generated", ext_hint=f".{ext}"
            )
            hosted_url = await self._host_bytes(
                data_bytes,
                content_type=content_type,
                path=path,
                provenance_source="gemini",
                # Unique per image — the storage path is deterministic per
                # (cycle, story fingerprint, tier) so reruns upsert correctly.
                provenance_original_url=f"gemini://{self._gemini_model_name}/{path}",
                provenance_author=self._gemini_model_name,
            )
            if hosted_url is None:
                return ImageResult(None, "ai_generated", "upload failed")
            return ImageResult(url=hosted_url, tier="ai_generated", notes=reason)

        return ImageResult(None, "ai_generated", "exhausted retries")

    # ------------------------------------------------------------------
    # Upload helpers (shared by tiers 1 & 3)
    # ------------------------------------------------------------------

    async def _download_and_host(
        self,
        external_url: str,
        *,
        path: str,
        provenance_source: str,
        provenance_original_url: str,
        provenance_author: str,
    ) -> str | None:
        """Download an external image, re-host it in our bucket, record provenance."""
        if self._uploader is None:
            # Uploader not configured — return external URL as-is (degraded mode).
            return external_url
        try:
            resp = await self._http.get(external_url)
            if resp.status_code >= 400:
                logger.warning("Download failed for %s: HTTP %s", external_url, resp.status_code)
                return None
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            data = resp.content
        except Exception as exc:
            logger.warning("Download error for %s: %s", external_url, exc)
            return None

        return await self._host_bytes(
            data,
            content_type=content_type,
            path=path,
            provenance_source=provenance_source,
            provenance_original_url=provenance_original_url,
            provenance_author=provenance_author,
        )

    async def _host_bytes(
        self,
        data: bytes,
        *,
        content_type: str,
        path: str,
        provenance_source: str,
        provenance_original_url: str,
        provenance_author: str,
    ) -> str | None:
        if self._uploader is None:
            return None
        try:
            public_url = await self._uploader.upload(data, content_type, path)
        except Exception as exc:
            logger.warning("Upload failed for path %s: %s", path, exc)
            return None
        try:
            await self._uploader.record_metadata(
                image_url=public_url,
                original_url=provenance_original_url,
                source=provenance_source,
                author=provenance_author,
            )
        except Exception as exc:
            # Metadata failure is non-fatal — we still have the hosted image.
            logger.warning("article_images metadata insert failed: %s", exc)
        return public_url

    @staticmethod
    def _storage_path(
        cycle_id: str | None,
        story: StoryEntry,
        *,
        tier: str,
        ext_hint: str,
    ) -> str:
        ext = ImageSelector._extension(ext_hint)
        cid = cycle_id or "no-cycle"
        return f"public/editorial/{cid}/{story.story_fingerprint}-{tier}.{ext}"

    @staticmethod
    def _extension(hint: str) -> str:
        # Accept either a URL ("...x.jpg?abc") or a bare ".png" hint.
        hint = hint.lower()
        for ext in ("jpg", "jpeg", "png", "webp", "gif"):
            if f".{ext}" in hint:
                return "jpg" if ext == "jpeg" else ext
        return "jpg"

    @staticmethod
    def _domain(url: str) -> str:
        try:
            host = urlparse(url).hostname or ""
            return host.removeprefix("www.")
        except Exception:
            return ""

    @staticmethod
    def _decode_data_url(data_url: str) -> tuple[str, bytes] | None:
        if not data_url.startswith("data:"):
            return None
        try:
            header, b64 = data_url.split(",", 1)
            # header format: data:<mime>;base64
            mime = header[5:].split(";", 1)[0] or "image/png"
            return mime, base64.b64decode(b64)
        except Exception:
            return None

    @staticmethod
    def _build_generation_prompt(
        article: PublishableArticle,
        primary_team: str | None,
    ) -> str:
        """Generate a journalistic, story-specific image prompt.

        Goal is a news-wire photograph that a picture editor would actually
        run next to this article — NOT a mood piece. Gemini is asked to depict
        the real subject of the story (the moment, the setting, the activity),
        in a plain documentary style, with only text and logo rendering
        disallowed (those are hard technical failures, not stylistic).
        """
        # Inject the actual team name + uniform color palette. "KC" or even
        # "NE" is opaque to Gemini; "New England Patriots, navy blue, silver,
        # and red" actually steers the palette of the generated scene.
        if primary_team:
            full = team_full_name(primary_team) or primary_team
            colors = team_colors(primary_team)
            color_clause = (
                f" Uniform palette: {colors}. Depict the players in these "
                f"colors — this is the central visual cue for team identity "
                f"(since wordmarks and helmet logos are forbidden below)."
                if colors else ""
            )
            team_phrase = (
                f"The story is about the {full}.{color_clause} "
            )
        else:
            team_phrase = "The story is NFL/football-related (no specific team). "
        return f"""You are generating a wire-service NFL game-action news photograph to run alongside the following article. Think AP / Getty / Reuters sports desk — a live-game or practice photograph a picture editor would file. NOT a movie still, NOT a mood piece, NOT an empty stadium.

HEADLINE: {article.headline}
INTRO: {article.introduction}

{team_phrase}

STRONG BIAS TOWARD IN-GAME ACTION. Default to showing players actively PLAYING football:
- A ball-carrier running, stiff-arming, or being tackled.
- A wide receiver catching or contesting a pass in mid-air.
- A quarterback in the pocket releasing a throw, or being sacked.
- Linemen in a trench battle, hands engaged, pads colliding.
- A defender wrapping up a runner in open field.
- Special teams: a punt returner cutting upfield, a kicker in follow-through.
- Pregame / sideline alternative (use only when game action doesn't fit the story): coach mid-play-call on the sideline, team huddle, player in helmet on the bench in focus.

Choose the scene that best fits THIS story. If it's a trade/transaction/injury/contract piece where on-field action is still appropriate (most are — show the player doing what they're known for), use action. If it's strictly front-office or legal, a sideline/huddle/locker-room scene is fine.

GUIDELINES (photojournalism, not cinema):
- Natural stadium/daylight/field lighting. NO cinematic color grading, NO teal-and-orange, NO moody silhouettes, NO golden-hour worship, NO heavy bokeh for drama.
- Sideline or end-zone photographer perspective — eye-level with the action. Motion blur on a moving limb or the ball is good; it reads as real. Composition can be slightly imperfect.
- Real-looking people in real contact or real motion. Multiple players in frame is encouraged (offense vs defense, receiver vs DB). Faces are fine, including in focus.
- VARIETY IS CRITICAL. Do NOT default to an empty stadium, a person standing alone with their back turned, a silhouette at dusk, or a moody locker room. Those are AI-slop fingerprints we are explicitly rejecting. Show actual football being played.

HARD CONSTRAINTS (rejection triggers):
- NO readable WORDS, letters, team names, player names on jerseys, captions, signage wordmarks, sponsor logos, press-backdrop wordmarks, or watermarks. Treat any would-be word area (sponsor patches, nameplate above a jersey number, end-zone team name) as if it must be plain / unlabeled / occluded.
- NUMBERS ARE ALLOWED in their natural sports-photo places: jersey numbers on players, yard-line numbers on the field, down markers. These are expected and do not count as "text."
- NO team logos, helmet wordmarks, or nickname-logos — team identity only through uniform color. (The league's shield logo and generic stripe patterns are fine.)
- NO visible scoreboard face (the boxes with team abbreviations and scores are text-heavy — avoid or keep out of frame).
- NO cartoon, illustration, meme, clip-art, 3D render, or stock-photo aesthetic. Must look like an unretouched photograph.
- NO faces of SPECIFIC named real athletes in recognizable close-up. Generic players are fine; don't render a real celebrity-athlete portrait.

Deliver a live-action NFL football photograph that fits THIS story."""
