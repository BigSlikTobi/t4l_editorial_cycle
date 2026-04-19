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
            "sidelines, equipment); both are acceptable IF they don't misrepresent "
            "the story's current context.\n\n"
            f"Article headline: {headline}\n"
            f"Article intro: {introduction}"
            f"{team_clause}\n"
            "Reply ONLY in JSON with keys `matches` (boolean) and `reason` "
            "(one short sentence naming the specific concrete reason).\n\n"
            "REJECT (matches=false) when any of these apply:\n"
            "- Different sport (baseball, basketball, soccer, hockey, etc.).\n"
            "- Different-team contradiction: the image clearly shows another "
            "  current NFL team's jersey wordmark, helmet logo, or end-zone "
            "  branding. Uniform COLOR alone is ambiguous (many teams share "
            "  red, blue, green etc.) — only reject when a DIFFERENT team's "
            "  identifying mark is actually visible, not when colors merely "
            "  don't prove the expected team.\n"
            "- Static portrait / headshot / mugshot / combine-style body shot "
            "  with a plain backdrop: these do not work as news-article lead "
            "  images. We want action, sideline, practice, locker room, coach-"
            "  talking-to-player, front-office, stadium, or equipment scenes — "
            "  NOT a posed one-person portrait against a studio or draped "
            "  background.\n"
            "- Obviously dated: heavily yellowed, visibly film-grain, uniform "
            "  styling from clearly decades ago, or a historical archive look "
            "  when the story is about current events.\n"
            "- Low quality: obviously amateur, blurry to the point of distraction, "
            "  cropped or compressed badly, watermarked from a consumer stock "
            "  site, meme/cartoon/clip-art, screenshot, infographic, or stats card.\n"
            "- Offensive, irrelevant, or clearly broken/corrupted.\n\n"
            "ACCEPT (matches=true) ONLY when:\n"
            "- The image shows the expected team's visible identity (uniform "
            "  color, helmet, field branding) in a CURRENT-looking action or "
            "  sideline/practice scene, OR\n"
            "- The image is team-agnostic NFL/football context (generic stadium "
            "  interior, grass/field, goalposts, unbranded equipment, crowd shot) "
            "  that doesn't contradict the expected team, OR\n"
            "- The image is an editorial mood shot (empty stadium at dusk, "
            "  silhouette in football setting, team-color abstract) that clearly "
            "  fits the story and doesn't display a contradicting team's marks.\n\n"
            "Doubt handling:\n"
            "- Portrait / dated / low-quality / wrong-sport doubts → prefer REJECT.\n"
            "- Team-identity ambiguity (can't tell which team, no marks visible) "
            "  → prefer ACCEPT if the scene is otherwise news-appropriate.\n"
            "Name the concrete reason in `reason` (what specifically you saw, "
            "not a generic 'not matching')."
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
            return False, f"validator error: {exc}"

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
            "`notes` (short sentence).\n\n"
            "Set contains_text=TRUE when the image contains any of:\n"
            "- Readable WORDS: team nicknames, city names, player names, sponsor "
            "  names, slogans, headlines, captions, press-backdrop logos with "
            "  letters, signage with letters.\n"
            "- Visible brand wordmarks or logos-with-letters (e.g. 'NIKE', 'FedEx', "
            "  a team wordmark across a jersey chest or an end-zone).\n"
            "- Scoreboard face showing team abbreviations AND scores together.\n"
            "- Watermarks or photographer attribution text.\n\n"
            "Set contains_text=FALSE (acceptable in a sports photo) when the image "
            "contains only:\n"
            "- Jersey NUMBERS on players (e.g. '87', '22') — these are expected "
            "  and NOT problematic.\n"
            "- Yard-line NUMBERS on the field (e.g. '30', '50') — expected.\n"
            "- Down-and-distance markers with small digits.\n"
            "- Decorative or abstract shapes that vaguely resemble letters.\n"
            "- Heavily blurred background signage where no word can be read.\n"
            "- Team-color patterns with no discernible letters.\n\n"
            "Key rule: NUMBERS alone are fine in sports photography. Only WORDS "
            "(or brand wordmarks) trigger rejection. When in doubt about a "
            "numeric-only element → FALSE. When in doubt about a word-like "
            "element → TRUE."
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
            return True, f"validator error: {exc}"
