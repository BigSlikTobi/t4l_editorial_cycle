"""Vision-based validation for candidate article images.

Two checks:
  1. does_image_match — asks a vision model whether the image plausibly
     matches the article headline/intro. Used to gate tier 1 (image_selection)
     and tier 3 (AI-generated) candidates.
  2. image_contains_text — asks the same model whether the image contains
     any readable text/watermarks. Used only for AI-generated candidates
     where we require a no-text result.
"""

from __future__ import annotations

import logging
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class _MatchVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    matches: bool
    reason: str


class _TextVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contains_text: bool
    notes: str


class ImageValidator:
    def __init__(self, api_key: str, model: str = "gpt-5.4-mini") -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def does_image_match(
        self,
        image_url: str,
        headline: str,
        introduction: str,
        *,
        expected_team_code: str | None = None,
        expected_team_name: str | None = None,
    ) -> tuple[bool, str]:
        """Return (matches, reason). Fail-closed: any error → (False, reason).

        Stricter than it looks: if `expected_team_*` is provided the image is
        rejected when it clearly shows a DIFFERENT current NFL team's jersey,
        helmet, or field branding — even if the article mentions that other
        team's name in context (e.g. a manager's former team). "Plausible NFL
        content" alone is no longer enough to accept.
        """
        team_clause = ""
        if expected_team_code and expected_team_name:
            team_clause = (
                f"\nEXPECTED CURRENT TEAM CONTEXT: {expected_team_name} "
                f"({expected_team_code}).\n"
                f"Reject ONLY on positive contradiction — i.e. a DIFFERENT "
                f"current NFL team's clearly-identifiable jersey wordmark, "
                f"helmet logo, or end-zone branding dominates the image. "
                f"Ambiguity (uniforms in plausible team colors without visible "
                f"marks, or a generic football scene with no team shown at all) "
                f"is NOT a contradiction — accept it. Historical affiliations "
                f"mentioned in the article do NOT justify showing another team's "
                f"current marks.\n"
            )

        prompt = (
            "You are validating whether an image is APPROPRIATE as the lead "
            "image for a CURRENT NFL news article. Editorial sites use a mix "
            "of literal (player/team) and mood/contextual imagery (stadiums, "
            "sidelines, equipment); both are acceptable IF they actually "
            "depict football content AND don't misrepresent the story's "
            "current context.\n\n"
            f"Article headline: {headline}\n"
            f"Article intro: {introduction}"
            f"{team_clause}\n"
            "Reply ONLY in JSON with keys `matches` (boolean) and `reason` "
            "(one short sentence). When matches=false, the reason MUST start "
            "with one of these prefixes (for telemetry):\n"
            "  off-topic: / wrong-sport: / wrong-team: / portrait: / "
            "  dated: / low-quality:\n\n"
            "=== GATE 1 — TOPIC RELEVANCE (must pass first) ===\n"
            "Does the image visibly depict NFL / American football subject matter?\n"
            "Valid: players in uniform/practice gear, coaches in team gear on a "
            "sideline, a football field with yard lines/goal posts, a stadium "
            "(interior or exterior), the football itself, a helmet/pads, a "
            "locker room, an NFL press conference or draft setting, a referee "
            "in stripes, or any visibly football-related scene with a clear "
            "football cue (ball, pads, field, helmet, scoreboard, goalpost, "
            "sideline, end zone, pylons, podium with NFL context).\n"
            "REJECT (off-topic:) if no football cue is visible: wildlife, "
            "landscapes without a stadium, random people not in football "
            "context, cityscapes, food, vehicles, abstract art that is not a "
            "football-color pattern, diagrams, maps, book/movie covers, or "
            "any subject a reader would describe as 'not about football at "
            "all.' Uniform colors alone are NOT football evidence.\n\n"
            "=== GATE 2 — If Gate 1 passed, apply these rejections ===\n"
            "- wrong-sport: clearly baseball, basketball, soccer, hockey, etc.\n"
            "- wrong-team: image clearly shows another current NFL team's "
            "  jersey wordmark, helmet logo, or end-zone branding. Uniform "
            "  COLOR alone is ambiguous — only reject when a DIFFERENT team's "
            "  identifying mark is actually visible.\n"
            "- portrait: static headshot/mugshot/combine-style body shot with "
            "  a plain or studio backdrop. We want action/sideline/practice/"
            "  locker/press/front-office/stadium/equipment scenes — NOT a "
            "  posed one-person portrait.\n"
            "- dated: heavily yellowed, visible film grain, uniform styling "
            "  from clearly decades ago, or historical-archive look when the "
            "  story is current.\n"
            "- low-quality: amateur, blurry to distraction, badly cropped, "
            "  consumer-stock watermarked, meme/cartoon/clip-art/3D render, "
            "  screenshot, infographic, stats card.\n"
            "- broken/corrupted/offensive: use prefix `low-quality:`.\n\n"
            "=== GATE 3 — If Gate 2 didn't reject, accept when ===\n"
            "- The image shows the expected team's visible identity in a "
            "  current-looking action/sideline/practice scene, OR\n"
            "- The image is team-agnostic football context (generic stadium, "
            "  unbranded equipment, crowd shot) that doesn't contradict the "
            "  expected team, OR\n"
            "- The image is an editorial mood shot with clear football setting.\n\n"
            "Doubt handling:\n"
            "- Topic-relevance doubt → REJECT (off-topic:).\n"
            "- Portrait/dated/low-quality/wrong-sport doubt → REJECT.\n"
            "- Team-identity ambiguity (can't tell which team, but clearly "
            "  football) → ACCEPT.\n"
            "Name the concrete visual element you used in `reason` after the "
            "prefix (e.g. 'wrong-team: Bills helmet logo dominates' or "
            "'portrait: studio backdrop, posed single subject')."
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=200,
            )
            content = resp.choices[0].message.content or "{}"
            verdict = _MatchVerdict.model_validate_json(content)
            return verdict.matches, verdict.reason
        except Exception as exc:
            logger.warning("Image match validation failed: %s", exc)
            return False, f"low-quality: validator error: {exc}"

    async def image_contains_text(self, image_url: str) -> tuple[bool, str]:
        """Return (contains_text, notes). Fail-closed: error → (True, reason) so
        we do NOT accept an image we couldn't verify for the no-text rule.

        Calibrated to reject *legible* text a reader would notice, not every
        abstract mark that vaguely resembles a letter. Blurry yard-line
        numbers in the deep background, decorative squiggles, or shapes that
        could be interpreted as letters only by generous pattern-matching
        should NOT trigger rejection — otherwise AI generation almost never
        succeeds on sports scenes.
        """
        prompt = (
            "This is an NFL sports photograph. Flag problematic WORD-TEXT that "
            "could misidentify or mislead (team names, player names, sponsors, "
            "captions, watermarks) — NOT the expected numerals of the sport. "
            "Reply ONLY in JSON with keys `contains_text` (boolean) and "
            "`notes` (short sentence). When contains_text=true, `notes` MUST "
            "start with one of these prefixes (for telemetry):\n"
            "  wordmark: / caption: / watermark: / scoreboard: / sponsor: / signage:\n"
            "When contains_text=false, prefix with `clean:` followed by a brief "
            "note (e.g. `clean: only jersey numbers visible`).\n\n"
            "Set contains_text=TRUE for any of:\n"
            "- Readable WORDS: team nicknames, city names, player names, "
            "  sponsor names, slogans, headlines, captions, signage with letters.\n"
            "- Specific team wordmarks (jersey chest wordmark, end-zone team "
            "  name) or non-NFL brand wordmarks (NIKE, FedEx, etc.).\n"
            "- Scoreboard face showing team abbreviations AND scores together.\n"
            "- Watermarks or photographer attribution text.\n\n"
            "Set contains_text=FALSE (acceptable) for:\n"
            "- Jersey NUMBERS on players (e.g. '87', '22').\n"
            "- Yard-line NUMBERS on the field (e.g. '30', '50').\n"
            "- Down-and-distance markers with small digits.\n"
            "- The NFL shield logo itself (it contains 'NFL' but is the league "
            "  mark — acceptable on any NFL editorial image).\n"
            "- NFL-branded press-conference backdrops (the league shield/logo "
            "  alone, no specific team wordmarks).\n"
            "- Decorative or abstract shapes vaguely resembling letters.\n"
            "- Heavily blurred background signage where no word is legible.\n\n"
            "Key rule: NUMBERS alone are fine in sports photography. Only "
            "specific WORDS or wordmarks (team-specific or non-NFL sponsor) "
            "trigger rejection. The NFL shield itself is fine.\n"
            "Doubt handling (fail-closed): when in doubt about a word-like "
            "element → TRUE. When in doubt about a numeric-only element → FALSE."
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=120,
            )
            content = resp.choices[0].message.content or "{}"
            verdict = _TextVerdict.model_validate_json(content)
            return verdict.contains_text, verdict.notes
        except Exception as exc:
            logger.warning("OCR validation failed, treating as contains_text=True: %s", exc)
            return True, f"wordmark: validator error: {exc}"
