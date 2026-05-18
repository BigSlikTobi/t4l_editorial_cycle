"""Claim/source and dialogue-naturalness validation for podcast artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.podcast.schemas import PodcastScript, ScriptLine


CLAIM_REF_RE = re.compile(r"\[C(?P<id>[A-Za-z0-9_.:-]+)\]")
_SENSITIVE_UNSOURCED_RE = re.compile(
    r"(?i)(\b\d+(?:[.,]\d+)?\b|%|prozent|million|milliard|verletz|trade|vertrag|"
    r"quote|zitat|sagt(?:e)?|bericht(?:et)?|sperre|anklage|verfahren|dvoa|epa|"
    r"pressure|rate|yards|touchdowns?|interceptions?)"
)
_DISAGREEMENT_RE = re.compile(
    r"(?i)(sehe ich anders|widersprech|moment|stopp|zu hart|zu großzügig|"
    r"falsche frage|das zeigt das tape so nicht|ich geh nicht mit)"
)
_OLD_FRIEND_RE = re.compile(
    r"(?i)(tape on|let'?s go|whiteboard|filmraum ohne fenster|trailer-stimme|"
    r"du weißt|weisst du|wie immer|alter freund|parken das|da geh ich halb mit)"
)
_JOKE_RE = re.compile(r"(?i)(lacht|chuckle|witz|teas|neck|trailer-stimme|whiteboard)")
_ON_AIR_SOURCE_META_RE = re.compile(
    r"(?i)("
    r"laut\s+(?:quelle|quellen|bericht|berichten|meldung|medienbericht|"
    r"team[- ]?mitteilung|nfl\.com|espn|yahoo|sports illustrated|si\.com)|"
    r"(?:den\s+)?quellen\s+zufolge|"
    r"wie\s+(?:aus\s+)?(?:den\s+)?quellen\s+(?:zu\s+)?(?:erkennen|sehen|lesen)|"
    r"wie\s+(?:berichtet|gemeldet)\s+wurde|"
    r"die\s+quellenlage|"
    r"unsere\s+quellen|"
    r"according\s+to|"
    r"per\s+(?:source|report)|"
    r"as\s+reported\s+by"
    r")"
)
_CONTRAST_FORMULA_RE = re.compile(
    r"(?i)\b(?:das\s+ist\s+)?kein(?:e|en|er|es)?\b.{0,80}\b(?:das\s+ist|sondern)\b"
)


class PodcastSource(BaseModel):
    """One source in an episode source ledger."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    url: str
    publisher: str = ""
    published_at: str = ""
    accessed_at: str = ""
    source_type: str = "web"
    reliability_note: str = ""


class PodcastClaim(BaseModel):
    """One classified factual claim used by the German podcast script."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    source_ids: list[str] = Field(default_factory=list)
    claim_type: str = "fact"
    confidence: Literal["low", "medium", "high"] = "medium"
    exact_quote: str | None = None
    number_checked: bool = False
    status: Literal["supported", "speculation"] = "supported"


@dataclass(frozen=True)
class PodcastValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]


def _line_refs(line: ScriptLine) -> set[str]:
    return {match.group("id") for match in CLAIM_REF_RE.finditer(line.text)}


def _all_lines(script: PodcastScript) -> list[ScriptLine]:
    return script.all_lines()


def strip_claim_refs(text: str) -> str:
    """Remove claim markers before audio synthesis."""

    return CLAIM_REF_RE.sub("", text).replace("  ", " ").strip()


def strip_script_claim_refs(script: PodcastScript) -> PodcastScript:
    """Return a copy of a script with claim markers removed from every line."""

    def clean(line: ScriptLine) -> ScriptLine:
        return line.model_copy(update={"text": strip_claim_refs(line.text)})

    return script.model_copy(
        update={
            "cold_open": [clean(line) for line in script.cold_open],
            "sections": [
                section.model_copy(
                    update={"lines": [clean(line) for line in section.lines]}
                )
                for section in script.sections
            ],
            "body": [clean(line) for line in script.body],
            "outro": [clean(line) for line in script.outro],
        }
    )


def validate_claims_and_sources(
    *,
    sources: list[PodcastSource],
    claims: list[PodcastClaim],
    script: PodcastScript,
) -> PodcastValidationResult:
    """Validate ledger integrity and script claim marker coverage."""

    errors: list[str] = []
    warnings: list[str] = []
    source_ids = {source.id for source in sources}
    claim_ids: set[str] = set()

    for source in sources:
        if source.id in source_ids and not source.url:
            errors.append(f"source {source.id} has no url")

    for claim in claims:
        if claim.id in claim_ids:
            errors.append(f"duplicate claim id: {claim.id}")
        claim_ids.add(claim.id)

        if claim.status == "speculation":
            if claim.source_ids:
                warnings.append(
                    f"speculation claim {claim.id} has source_ids; keep wording speculative"
                )
            continue

        if not claim.source_ids:
            errors.append(f"claim {claim.id} has no source_ids")
        for source_id in claim.source_ids:
            if source_id not in source_ids:
                errors.append(f"claim {claim.id} references missing source {source_id}")
        if claim.exact_quote and not claim.source_ids:
            errors.append(f"quote claim {claim.id} must cite a source")

    refs_seen: set[str] = set()
    for index, line in enumerate(_all_lines(script), start=1):
        refs = _line_refs(line)
        refs_seen.update(refs)
        for ref in refs:
            if ref not in claim_ids:
                errors.append(f"script line {index} references unknown claim {ref}")
        if _ON_AIR_SOURCE_META_RE.search(line.text):
            errors.append(
                f"script line {index} uses on-air source/meta attribution; "
                "keep sourcing in ledgers and let hosts speak from expertise"
            )
        if _SENSITIVE_UNSOURCED_RE.search(line.text) and not refs:
            errors.append(
                f"script line {index} contains a stat/sensitive claim without a claim marker"
            )

    for claim in claims:
        if claim.status == "supported" and claim.id not in refs_seen:
            warnings.append(f"supported claim {claim.id} is not referenced in the script")

    return PodcastValidationResult(ok=not errors, errors=errors, warnings=warnings)


def _extract_memory_phrases(host_memory: str) -> list[str]:
    phrases = [match.group(1).strip() for match in re.finditer(r'"([^"]{4,80})"', host_memory)]
    return [phrase for phrase in phrases if len(phrase.split()) >= 2]


def validate_naturalness(
    *,
    script: PodcastScript,
    host_memory: str,
) -> PodcastValidationResult:
    """Conservative post-script check for robotic or fake-conflict dialogue."""

    errors: list[str] = []
    warnings: list[str] = []
    body = (
        [line for section in script.sections for line in section.lines]
        if script.sections
        else script.body
    )

    if len(body) >= 12 and all(
        body[idx].speaker != body[idx - 1].speaker for idx in range(1, len(body))
    ):
        errors.append("body uses strict speaker alternation for 12+ lines; conversation feels robotic")

    old_friend_beats = sum(1 for line in body if _OLD_FRIEND_RE.search(line.text))
    if len(body) >= 12 and old_friend_beats < 3:
        errors.append("body has fewer than three host-memory/old-friends beats")

    contrast_formula_count = sum(
        1 for line in body if _CONTRAST_FORMULA_RE.search(line.text)
    )
    if contrast_formula_count > 1:
        warnings.append(
            "body repeats the 'kein X, sondern/das ist Y' contrast formula; "
            "vary the phrasing so it does not become a show pattern"
        )

    for index, line in enumerate(body, start=1):
        if _DISAGREEMENT_RE.search(line.text) and not _line_refs(line):
            errors.append(
                f"body line {index} contains disagreement without a supporting claim marker"
            )
        if _JOKE_RE.search(line.text) and _SENSITIVE_UNSOURCED_RE.search(line.text) and not _line_refs(line):
            errors.append(
                f"body line {index} appears to mix a joke with an unsourced factual claim"
            )

    for phrase in _extract_memory_phrases(host_memory):
        count = sum(line.text.lower().count(phrase.lower()) for line in _all_lines(script))
        if count > 2:
            errors.append(f"catchphrase overused: {phrase!r} appears {count} times")
        elif count == 0:
            warnings.append(f"memory phrase unused: {phrase!r}")

    return PodcastValidationResult(ok=not errors, errors=errors, warnings=warnings)


def assert_valid_episode(
    *,
    sources: list[PodcastSource],
    claims: list[PodcastClaim],
    script: PodcastScript,
    host_memory: str,
) -> None:
    """Raise ValueError if claim/source or naturalness validation fails."""

    claim_result = validate_claims_and_sources(
        sources=sources,
        claims=claims,
        script=script,
    )
    naturalness_result = validate_naturalness(script=script, host_memory=host_memory)
    errors = [*claim_result.errors, *naturalness_result.errors]
    if errors:
        raise ValueError("podcast artifact validation failed:\n" + "\n".join(f"- {e}" for e in errors))
