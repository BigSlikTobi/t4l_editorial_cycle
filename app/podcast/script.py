"""Script composition pipeline.

Pure functions that orchestrate the four podcast agents to turn a list
of `PodcastCluster` objects into a fully directed `PodcastScript`.

Flow:
    clusters
      → cluster_ranker_agent (narrative angles + adjusted weights)
      → cold_open_writer_agent (rapid-fire teaser)
      → dialogue_writer_agent (EN or DE body + outro)
      → host_authority_pass_agent (tighten host confidence + specificity)
      → director_pass_agent (prosody hints on every line)
      → PodcastScript

The agent calls are awaited via `agents.Runner.run` per the SDK pattern
used elsewhere in the repo. Errors propagate; the workflow layer
translates them into DB `failed` rows.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from agents import Runner

from app.config import Settings
from app.podcast.agents import (
    build_cluster_ranker_agent,
    build_cold_open_writer_agent,
    build_deep_dive_research_agent,
    build_dialogue_writer_agent,
    build_director_pass_agent,
    build_episode_metadata_agent,
    build_host_authority_pass_agent,
    build_player_of_day_research_agent,
    build_section_planner_agent,
    build_section_synthesis_agent,
    build_team_of_day_research_agent,
)
from app.podcast.personas import ANALYST_PERSONA, COLOR_PERSONA
from app.podcast.schemas import (
    EpisodeMetadata,
    PodcastCluster,
    PodcastContinuityContext,
    PodcastLanguage,
    PodcastScript,
    PodcastSection,
    PodcastSectionPlan,
    ScriptLine,
)

logger = logging.getLogger(__name__)


def _coerce_ranked_clusters(
    raw: object, original: list[PodcastCluster]
) -> list[PodcastCluster]:
    """Best-effort merge of agent output back into typed PodcastClusters.

    The cluster_ranker agent returns a JSON list; we don't trust it to
    invent or drop clusters, so we look up each by `cluster_id` against
    the originals and overlay only `narrative_angle` and `story_weight`.
    """
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("cluster ranker emitted non-JSON: %s", exc)
            return original
    else:
        payload = raw

    if not isinstance(payload, list):
        logger.warning("cluster ranker output is not a list; falling back to originals")
        return original

    by_id = {c.cluster_id: c for c in original}
    result: list[PodcastCluster] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("cluster_id")
        if cid not in by_id:
            continue
        base = by_id[cid]
        new_weight = entry.get("story_weight")
        new_angle = entry.get("narrative_angle")
        result.append(
            base.model_copy(
                update={
                    "story_weight": float(new_weight) if isinstance(new_weight, (int, float)) else base.story_weight,
                    "narrative_angle": new_angle if isinstance(new_angle, str) else base.narrative_angle,
                }
            )
        )

    if not result:
        return original

    # Preserve any clusters the agent failed to mention.
    seen = {c.cluster_id for c in result}
    for c in original:
        if c.cluster_id not in seen:
            result.append(c)

    result.sort(key=lambda c: c.story_weight, reverse=True)
    return result


def _coerce_cold_open_lines(raw: object) -> list[ScriptLine]:
    """Cold-open agent returns a JSON list of `{speaker, text}` dicts."""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("cold open emitted non-JSON: %s", exc)
            return []
    else:
        payload = raw
    if not isinstance(payload, list):
        return []
    lines: list[ScriptLine] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        speaker = entry.get("speaker")
        text = entry.get("text")
        if speaker not in {"color", "analyst"} or not isinstance(text, str):
            continue
        lines.append(ScriptLine(speaker=speaker, text=text))  # type: ignore[arg-type]
    return lines


def _fallback_section_plan(
    *, ranked: list[PodcastCluster], run_date: date
) -> PodcastSectionPlan:
    lead = ranked[0] if ranked else None
    lead_title = lead.headline if lead else "NFL morning board"
    lead_summary = lead.narrative_angle or lead.summary if lead else ""
    return PodcastSectionPlan(
        run_date=run_date,
        red_line=(
            "Start with the strongest news, spotlight one connected player, "
            "zoom out to one connected team, then finish with the broader "
            "football question behind the day."
        ),
        sections=[
            PodcastSection(
                kind="news",
                title="News",
                handover="Start with the strongest news on the board.",
                research_summary=lead_summary,
            ),
            PodcastSection(
                kind="player_of_day",
                title="Player of the Day",
                handover="Take one player from the news and put him under the light.",
                research_summary=lead_title,
            ),
            PodcastSection(
                kind="team_of_day",
                title="Team of the Day",
                handover="Now widen it to the team context.",
                research_summary=lead_title,
            ),
            PodcastSection(
                kind="deep_dive",
                title="Deep Dive",
                handover="Close with the bigger football question.",
                research_summary=lead_summary,
            ),
        ],
        rejected_candidates=[],
        notes="Fallback plan created because section planner output was unavailable.",
    )


def _coerce_section_plan(
    raw: object, *, ranked: list[PodcastCluster], run_date: date
) -> PodcastSectionPlan:
    if isinstance(raw, PodcastSectionPlan):
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("section planner emitted non-JSON; using fallback plan")
            return _fallback_section_plan(ranked=ranked, run_date=run_date)
    try:
        return PodcastSectionPlan.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - tolerate planner drift
        logger.warning("section planner output invalid: %s; using fallback plan", exc)
        return _fallback_section_plan(ranked=ranked, run_date=run_date)


def _research_text(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "model_dump_json"):
        return raw.model_dump_json(indent=2)
    return json.dumps(raw, ensure_ascii=False, indent=2)


def _word_count(script: PodcastScript) -> int:
    return sum(len(line.text.split()) for line in script.all_lines())


def _line_shape(script: PodcastScript) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
    sections = [(section.kind, len(section.lines)) for section in script.sections]
    lines = [(line.speaker, line.text) for line in script.all_lines()]
    return sections, lines


def _same_script_shape(before: PodcastScript, after: PodcastScript) -> bool:
    before_sections, before_lines = _line_shape(before)
    after_sections, after_lines = _line_shape(after)
    if before_sections != after_sections or len(before_lines) != len(after_lines):
        return False
    return all(
        before_speaker == after_speaker
        for (before_speaker, _), (after_speaker, _) in zip(before_lines, after_lines, strict=True)
    )


_BRANDED_INTRO_TEXT: dict[str, str] = {
    # Spelled out so Gemini doesn't ad-lib around the brand. The
    # trailing ellipsis earns a real beat in TTS; the prosody hints
    # reinforce it so the line lands as a standalone bolt statement
    # before the headlines roll in.
    "en-US": "Tackle 4 Loss. American Football Morning Show...",
    "de-DE": "Tackle 4 Loss. American Football Morning Show...",
}


def branded_intro_line(language: PodcastLanguage) -> ScriptLine:
    """The fixed, branded first line of every episode.

    Prepended to the cold-open section before the rapid-fire headlines.
    The brand name itself is English by design (it IS the show's name),
    but the line's delivery direction is performed in the episode's
    language by Gemini. The prosody hints are intentionally loaded so
    the line lands as a standalone declaration with a real beat after.
    """
    return ScriptLine(
        speaker="color",
        text=_BRANDED_INTRO_TEXT.get(language, _BRANDED_INTRO_TEXT["en-US"]),
        prosody_hints=[
            "bold declaration",
            "voice lifting",
            "punchy",
            "deliberate",
            "lets it land",
            "long pause",
        ],
    )


def _empty_script(language: PodcastLanguage, run_date: date) -> PodcastScript:
    """Minimum-viable script for a slow news day with zero clusters.

    The dialogue writer is allowed to produce a tiny show on slow days,
    but if the feed is literally empty we fall back to a one-line
    apology rather than calling the agent with nothing.
    """
    apology = (
        "No new league-wide stories today — checking back tomorrow."
        if language == "en-US"
        else "Heute keine neuen ligaweiten Storys — bis morgen."
    )
    return PodcastScript(
        language=language,
        run_date=run_date,
        cold_open=[],
        body=[ScriptLine(speaker="color", text=apology)],
        outro=[],
        story_count=0,
        word_count=len(apology.split()),
    )


async def compose_script(
    clusters: list[PodcastCluster],
    *,
    language: PodcastLanguage,
    run_date: date,
    target_word_count: int,
    settings: Settings,
    continuity_context: PodcastContinuityContext | None = None,
) -> PodcastScript:
    """Run all four podcast agents in sequence; return a directed script.

    Pure orchestration — no I/O beyond the agent calls. Errors from any
    agent surface as exceptions; callers (the workflow) translate to DB
    failure rows.
    """
    if not clusters:
        logger.warning("compose_script called with zero clusters")
        return _empty_script(language, run_date)

    # 1. Cluster ranker.
    ranker = build_cluster_ranker_agent(settings)
    ranker_input = json.dumps(
        [c.model_dump(mode="json") for c in clusters],
        ensure_ascii=False,
    )
    ranker_result = await Runner.run(ranker, ranker_input)
    ranked = _coerce_ranked_clusters(ranker_result.final_output, clusters)

    # 2. Cold open writer.
    cold_open_writer = build_cold_open_writer_agent(settings)
    cold_open_input = json.dumps(
        {
            "language": language,
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked[:6]],
        },
        ensure_ascii=False,
    )
    cold_open_result = await Runner.run(cold_open_writer, cold_open_input)
    cold_open_lines = _coerce_cold_open_lines(cold_open_result.final_output)
    # Prepend the branded show-open line ONLY when no song-mode music
    # is configured. In song-mode the music file itself carries the
    # brand vocal, so a spoken brand line would be redundant.
    if not settings.podcast_intro_song_mode:
        cold_open_lines = [branded_intro_line(language), *cold_open_lines]

    # 3. Four-section planning + specialist research.
    planner = build_section_planner_agent(settings)
    planner_input = json.dumps(
        {
            "language": language,
            "run_date": run_date.isoformat(),
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked],
            "continuity_context": continuity_context.model_dump(mode="json")
            if continuity_context
            else None,
        },
        ensure_ascii=False,
    )
    planner_result = await Runner.run(planner, planner_input)
    section_plan = _coerce_section_plan(
        planner_result.final_output, ranked=ranked, run_date=run_date
    )

    research_input = json.dumps(
        {
            "language": language,
            "run_date": run_date.isoformat(),
            "section_plan": section_plan.model_dump(mode="json"),
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked],
            "continuity_context": continuity_context.model_dump(mode="json")
            if continuity_context
            else None,
        },
        ensure_ascii=False,
    )
    player_research_result = await Runner.run(
        build_player_of_day_research_agent(settings), research_input
    )
    team_research_result = await Runner.run(
        build_team_of_day_research_agent(settings), research_input
    )
    deep_dive_research_result = await Runner.run(
        build_deep_dive_research_agent(settings), research_input
    )
    section_research = {
        "player_of_day": _research_text(player_research_result.final_output),
        "team_of_day": _research_text(team_research_result.final_output),
        "deep_dive": _research_text(deep_dive_research_result.final_output),
    }

    synthesis_input = json.dumps(
        {
            "language": language,
            "run_date": run_date.isoformat(),
            "section_plan": section_plan.model_dump(mode="json"),
            "section_research": section_research,
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked],
            "continuity_context": continuity_context.model_dump(mode="json")
            if continuity_context
            else None,
        },
        ensure_ascii=False,
    )
    synthesis_result = await Runner.run(
        build_section_synthesis_agent(settings), synthesis_input
    )
    section_synthesis = _research_text(synthesis_result.final_output)

    # 4. Dialogue writer (EN or DE).
    dialogue_writer = build_dialogue_writer_agent(settings, language=language)
    dialogue_input = json.dumps(
        {
            "language": language,
            "run_date": run_date.isoformat(),
            "target_word_count": target_word_count,
            "cold_open": [line.model_dump(mode="json") for line in cold_open_lines],
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked],
            "section_plan": section_plan.model_dump(mode="json"),
            "section_research": section_research,
            "section_synthesis": section_synthesis,
            "continuity_context": continuity_context.model_dump(mode="json")
            if continuity_context
            else None,
            "personas": {
                "color": {
                    "byline": COLOR_PERSONA.byline,
                    "style_guide": COLOR_PERSONA.style_guide_en
                    if language == "en-US"
                    else COLOR_PERSONA.style_guide_de,
                },
                "analyst": {
                    "byline": ANALYST_PERSONA.byline,
                    "style_guide": ANALYST_PERSONA.style_guide_en
                    if language == "en-US"
                    else ANALYST_PERSONA.style_guide_de,
                },
            },
        },
        ensure_ascii=False,
    )
    dialogue_result = await Runner.run(dialogue_writer, dialogue_input)
    if not isinstance(dialogue_result.final_output, PodcastScript):
        raise TypeError(
            f"dialogue writer returned {type(dialogue_result.final_output).__name__}, "
            "expected PodcastScript"
        )
    script: PodcastScript = dialogue_result.final_output
    # Ensure the cold open is preserved (the writer is told to pass it
    # through unchanged but trust-but-verify).
    if not script.cold_open and cold_open_lines:
        script = script.model_copy(update={"cold_open": cold_open_lines})

    # 5. Host Authority pass — rewrite weak/generic host statements
    # into confident, specific football talk before performance cues.
    authority_agent = build_host_authority_pass_agent(settings)
    authority_input = json.dumps(
        {
            "script": script.model_dump(mode="json"),
            "ranked_clusters": [c.model_dump(mode="json") for c in ranked],
            "section_plan": section_plan.model_dump(mode="json"),
            "section_research": section_research,
            "section_synthesis": section_synthesis,
            "continuity_context": continuity_context.model_dump(mode="json")
            if continuity_context
            else None,
            "personas": {
                "color": {
                    "byline": COLOR_PERSONA.byline,
                    "style_guide": COLOR_PERSONA.style_guide_en
                    if language == "en-US"
                    else COLOR_PERSONA.style_guide_de,
                },
                "analyst": {
                    "byline": ANALYST_PERSONA.byline,
                    "style_guide": ANALYST_PERSONA.style_guide_en
                    if language == "en-US"
                    else ANALYST_PERSONA.style_guide_de,
                },
            },
        },
        ensure_ascii=False,
    )
    authority_result = await Runner.run(authority_agent, authority_input)
    if isinstance(authority_result.final_output, PodcastScript):
        authority_script = authority_result.final_output
    elif isinstance(authority_result.final_output, dict):
        authority_script = PodcastScript.model_validate(authority_result.final_output)
    elif isinstance(authority_result.final_output, str):
        authority_script = PodcastScript.model_validate_json(authority_result.final_output)
    else:
        raise TypeError(
            f"host authority pass returned {type(authority_result.final_output).__name__}, "
            "expected PodcastScript"
        )
    if not _same_script_shape(script, authority_script):
        raise ValueError("host authority pass changed script structure or speakers")
    script = authority_script

    # 6. Director pass.
    director = build_director_pass_agent(settings)
    director_input = script.model_dump_json()
    director_result = await Runner.run(director, director_input)
    if isinstance(director_result.final_output, PodcastScript):
        script = director_result.final_output

    # 7. Episode metadata (title + summary for Spotify). Failures here
    # are non-fatal — the deliver step falls back to a template if the
    # fields are absent.
    metadata = await _generate_metadata(
        clusters=ranked,
        language=language,
        run_date=run_date,
        settings=settings,
    )

    # Final word count + story count, computed deterministically.
    final = script.model_copy(
        update={
            "story_count": len(ranked),
            "word_count": _word_count(script),
            "language": language,
            "run_date": run_date,
            "episode_title": metadata.title if metadata else None,
            "episode_summary": metadata.summary if metadata else None,
        }
    )
    return final


async def _generate_metadata(
    *,
    clusters: list[PodcastCluster],
    language: PodcastLanguage,
    run_date: date,
    settings: Settings,
) -> EpisodeMetadata | None:
    """Run the metadata agent. Returns None on any failure (caller
    handles fallback) so a flaky metadata pass never aborts a render."""
    try:
        agent = build_episode_metadata_agent(settings, language=language)
        payload = json.dumps(
            {
                "language": language,
                "run_date": run_date.isoformat(),
                "clusters": [
                    {
                        "cluster_id": c.cluster_id,
                        "headline": c.headline,
                        "summary": c.summary,
                        "narrative_angle": c.narrative_angle,
                    }
                    for c in clusters
                ],
            },
            ensure_ascii=False,
        )
        result = await Runner.run(agent, payload)
        if isinstance(result.final_output, EpisodeMetadata):
            return result.final_output
        # Some SDK versions return a dict / str — try to coerce.
        if isinstance(result.final_output, dict):
            return EpisodeMetadata.model_validate(result.final_output)
        if isinstance(result.final_output, str):
            return EpisodeMetadata.model_validate_json(result.final_output)
        logger.warning(
            "metadata agent returned %s; falling back to template",
            type(result.final_output).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.warning("metadata agent failed: %s — falling back", exc)
        return None
