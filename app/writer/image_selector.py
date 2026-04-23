"""Image selection cascade for writer output.

Tier order (agreed with product):
  1. image_selection cloud function + vision validator (Google CC)
  1b. Wikimedia Commons + vision validator (same tier, tried after Google)
  2. Curated pool lookup (content.curated_images) — pre-generated & reviewed
  3. Player headshot from public.players (dominant player only)
  4. Team logo (Flutter asset reference: asset://team_logo/{TEAM_CODE})
  5. None

Principle: wrong image > bad quality > no image. Web sources (tier 1) go
through the vision validator. Curated pool, headshots, and logos are all
safe-by-construction (vetted or deterministic) and skip validation.

The curated tier and the headshot tier each have per-cycle budgets (default
50% each) so the feed doesn't look monotonous.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from app.adapters import ImageUploader
from app.schemas import PlayerMention, PublishableArticle, StoryEntry
from app.team_codes import normalize_team_codes, team_full_name
from app.writer.image_clients import (
    ImageCandidate,
    ImageSelectionClient,
    WikimediaCommonsClient,
)
from app.writer.image_validator import ImageValidator

logger = logging.getLogger(__name__)

Tier = Literal[
    "image_search", "curated_pool", "player_headshot",
    "team_logo", "generic_nfl", "none",
]

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
    """Per-cycle cap on how many articles may use a given bounded tier.

    Used by both the curated-pool tier and the player-headshot tier. Capping
    prevents a monotonous feed (all mugshots, or all stock scenes) and forces
    surplus articles to fall through to the next tier. Async-safe — selectors
    run in parallel and share one budget instance.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, capacity)
        self._used = 0
        self._lock = asyncio.Lock()

    @classmethod
    def for_cycle(
        cls,
        publishable_count: int,
        ratio: float = 0.5,
        *,
        round_up: bool = False,
    ) -> "HeadshotBudget":
        """`round_up=False` (default) floors so the cap never exceeds the
        ratio. `round_up=True` ceils so the cap never falls below it —
        useful for the headshot tier where we'd rather cover one extra
        dominant-player story than cover none. Either way, any positive
        publishable_count guarantees at least 1 slot.
        """
        if not publishable_count:
            return cls(0)
        raw = publishable_count * ratio
        cap = math.ceil(raw) if round_up else math.floor(raw)
        return cls(max(1, cap))

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


# Scene classification — map an article/story to ordered (team, scene)
# probes against the curated pool. First hit wins.
#
# Team scenes (per team, in content.curated_images with team_code set):
#   offense_action, defense_action, sideline, celebration,
#   pregame_tunnel, locker_room, stadium_wide
#
# Generic scenes (team_code IS NULL):
#   press_conference, front_office, draft_room, medical_training,
#   coach_player_convo, referee, media_scrum, empty_field_dusk,
#   stadium_exterior, practice_generic, combine

_TEAM_SCENES_DEFAULT_ORDER: tuple[str, ...] = (
    "offense_action", "defense_action", "sideline",
    "stadium_wide", "pregame_tunnel", "locker_room", "celebration",
)

# Keyword → (team scene bias, generic scene bias). Both optional — team wins
# when we have a team_code and a team scene fits; generic fills in otherwise.
_SCENE_RULES: tuple[tuple[tuple[str, ...], str | None, str | None], ...] = (
    # (keywords, team_scene, generic_scene)
    (("touchdown", "td ", "win ", "victory", "celebrat", "walk-off"),
     "celebration", None),
    (("sack", "interception", "int ", "tackle", "defense ", "defensive",
      "linebacker", " safety ", "cornerback", "turnover"),
     "defense_action", None),
    (("pass ", "passing", "quarterback", " qb ", "receiver", "catch",
      "throw", "offense ", "offensive", "rushing", "touchdown pass"),
     "offense_action", None),
    (("head coach", "coordinator", "staff ", "fired", "hired"),
     "sideline", "press_conference"),
    (("draft", "prospect", "combine", "mock"),
     None, "draft_room"),
    (("injur", "concussion", " knee ", " ir ", "hamstring", "ankle",
      "shoulder", "rehab", "surgery"),
     None, "medical_training"),
    (("trade", "sign ", "signing", "contract", "extension", "agree",
      "release", "waive", "cut ", "deal ", "free agent"),
     None, "press_conference"),
    (("referee", "penalty", "flag", "overturn", "replay", "ruling"),
     None, "referee"),
    (("practice", "ota", "minicamp", "training camp"),
     None, "practice_generic"),
    (("locker room", "post-game", "postgame"),
     "locker_room", None),
)


def _scene_candidates(
    article: PublishableArticle, story: StoryEntry, team: str | None
) -> list[tuple[str | None, str]]:
    """Return ordered (team_code, scene) probes against the curated pool."""
    text = f"{article.headline} {article.introduction}".lower()
    team_hits: list[tuple[str | None, str]] = []
    generic_hits: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()

    def _add(lst: list, item: tuple[str | None, str]) -> None:
        if item not in seen:
            seen.add(item)
            lst.append(item)

    for keywords, team_scene, generic_scene in _SCENE_RULES:
        if any(kw in text for kw in keywords):
            if team and team_scene:
                _add(team_hits, (team, team_scene))
            if generic_scene:
                _add(generic_hits, (None, generic_scene))

    # Team default rotation for variety across stories (deterministic per
    # fingerprint so re-runs pick the same scene).
    if team:
        idx = int(hashlib.md5(story.story_fingerprint.encode()).hexdigest(), 16)
        rotated = (
            _TEAM_SCENES_DEFAULT_ORDER[idx % len(_TEAM_SCENES_DEFAULT_ORDER):]
            + _TEAM_SCENES_DEFAULT_ORDER[:idx % len(_TEAM_SCENES_DEFAULT_ORDER)]
        )
        for scene in rotated:
            _add(team_hits, (team, scene))

    # Generic last-resort
    for scene in ("empty_field_dusk", "stadium_exterior"):
        _add(generic_hits, (None, scene))

    return team_hits + generic_hits


class ImageSelector:
    def __init__(
        self,
        *,
        supabase_url: str,
        supabase_service_role_key: str,
        image_client: ImageSelectionClient | None,
        validator: ImageValidator,
        uploader: ImageUploader | None = None,
        wikimedia_client: WikimediaCommonsClient | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._supabase_base = supabase_url.rstrip("/")
        self._supabase_key = supabase_service_role_key
        self._image_client = image_client
        self._wikimedia_client = wikimedia_client
        self._validator = validator
        self._uploader = uploader
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
        curated_budget: "HeadshotBudget | None" = None,
    ) -> ImageResult:
        """Run the cascade. Each tier's failure is logged; never raises."""
        normalized_teams = normalize_team_codes(story.team_codes)
        # Prefer the writer-chosen team — for multi-team clusters (e.g. a
        # league-wide roundup), `team_codes[0]` is just alphabetical and
        # often doesn't match the article's actual subject. The writer
        # sets `article.team` based on what the piece is really about.
        primary_team = None
        if article.team and article.team in normalized_teams:
            primary_team = article.team
        elif normalized_teams:
            primary_team = normalized_teams[0]
        dominant_player = self._pick_dominant_player(article, story)

        # Tier 1 pre-check: if a dominant player exists AND the headshot
        # tier still has budget, skip the web search — a safe mugshot
        # is coming next and will win.
        skip_tier1 = (
            dominant_player is not None
            and (headshot_budget is None or headshot_budget.capacity > headshot_budget.used)
        )

        # Tier 1: web search (Google CC + Wikimedia, each validator-gated)
        if not skip_tier1:
            result = await self._try_image_search(
                article, story, cycle_id=cycle_id, primary_team=primary_team
            )
            if result.url:
                return result
        else:
            logger.info(
                "Skipping tier 1 for %s (dominant player %s → headshot likely wins)",
                article.headline[:60], dominant_player.id if dominant_player else "?",
            )

        # Tier 2: player headshot — gated by per-cycle budget (default 40%)
        result = await self._try_player_headshot(
            article, story, dominant_player=dominant_player, budget=headshot_budget
        )
        if result.url:
            return result

        # Tier 3: curated pool (uncapped by default — team scene coverage
        # is complete, so this is the safe fallback for everything else)
        result = await self._try_curated(
            article, story, primary_team=primary_team, budget=curated_budget
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
    # Tier 2 — curated pool (content.curated_images)
    # ------------------------------------------------------------------

    async def _try_curated(
        self,
        article: PublishableArticle,
        story: StoryEntry,
        *,
        primary_team: str | None,
        budget: "HeadshotBudget | None",
    ) -> ImageResult:
        """Probe the curated pool in scene-priority order. First active row
        matching (team_code, scene) wins; budget is consumed only on success."""
        if budget is not None and budget.capacity == 0:
            return ImageResult(None, "curated_pool", "budget disabled")

        candidates = _scene_candidates(article, story, primary_team)
        tried: list[str] = []
        for team_code, scene in candidates:
            label = f"{team_code or 'generic'}/{scene}"
            row = await self._lookup_curated(team_code, scene, story.story_fingerprint)
            if row is None:
                tried.append(label)
                continue
            if budget is not None and not await budget.try_consume():
                return ImageResult(
                    None, "curated_pool",
                    f"budget exhausted ({budget.capacity} used); "
                    f"would have used {label}",
                )
            return ImageResult(
                url=row["image_url"], tier="curated_pool",
                notes=f"{label} (slug={row['slug']})",
            )

        return ImageResult(None, "curated_pool", f"no match (tried {len(tried)} probes)")

    async def _lookup_curated(
        self, team_code: str | None, scene: str, fingerprint: str,
    ) -> dict | None:
        """Query content.curated_images for the first active row matching
        (team_code, scene). When multiple variants exist (generic scenes
        often have 2–3), pick deterministically by fingerprint so the same
        story gets the same image on re-runs."""
        params: dict[str, str] = {
            "active": "eq.true",
            "scene": f"eq.{scene}",
            "select": "slug,image_url,team_code,scene",
            "order": "slug.asc",
        }
        if team_code is None:
            params["team_code"] = "is.null"
        else:
            params["team_code"] = f"eq.{team_code}"

        try:
            resp = await self._http.get(
                f"{self._supabase_base}/rest/v1/curated_images",
                params=params,
            )
        except Exception as exc:
            logger.warning("curated lookup error (%s/%s): %s", team_code, scene, exc)
            return None
        if resp.status_code >= 400:
            logger.warning(
                "curated lookup HTTP %s (%s/%s): %s",
                resp.status_code, team_code, scene, resp.text[:120],
            )
            return None
        rows = resp.json() or []
        if not rows:
            return None
        idx = int(hashlib.md5(fingerprint.encode()).hexdigest(), 16) % len(rows)
        return rows[idx]

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

