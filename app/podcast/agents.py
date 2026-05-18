"""Agent factories for the podcast module.

Four agents in the script-generation pipeline:

  * Cluster ranker — adds news-judgment-driven `narrative_angle` and
    re-weights clusters beyond entity-count signal.
  * Cold open writer — 60-90s rapid-fire two-voice tease before intro.
  * Dialogue writer (EN + DE — same factory, different prompt key) —
    composes the body of the podcast as a back-and-forth between the
    two personas.
  * Director pass — adds prosody hints to every line for the TTS render.

Mirrors `app/team_beat/agents.py` exactly: OpenAI Agents SDK, structured
outputs, `build_model_settings` for prompt caching.
"""

from __future__ import annotations

from agents import Agent

from app.config import Settings
from app.podcast.prompts import get_prompt
from app.podcast.schemas import EpisodeMetadata, PodcastScript, PodcastSectionPlan
from app.writer.model import build_model_settings


def build_cluster_ranker_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Cluster Ranker Agent",
        instructions=get_prompt("cluster_ranker_agent"),
        model=settings.agent_model("podcast_cluster_ranker_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        # Output is a JSON list of clusters; we validate downstream in
        # `script.py::_coerce_ranked_clusters` because Pydantic root models
        # for `list[...]` are awkward across SDK versions.
    )


def build_cold_open_writer_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Cold Open Writer Agent",
        instructions=get_prompt("cold_open_writer_agent"),
        model=settings.agent_model("podcast_cold_open_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
    )


def build_section_planner_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Section Planner Agent",
        instructions=get_prompt("section_planner_agent"),
        model=settings.agent_model("podcast_cluster_ranker_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PodcastSectionPlan,
    )


def build_player_of_day_research_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Player of the Day Research Agent",
        instructions=get_prompt("player_of_day_research_agent"),
        model=settings.agent_model("podcast_dialogue_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
    )


def build_team_of_day_research_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Team of the Day Research Agent",
        instructions=get_prompt("team_of_day_research_agent"),
        model=settings.agent_model("podcast_dialogue_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
    )


def build_deep_dive_research_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Deep Dive Research Agent",
        instructions=get_prompt("deep_dive_research_agent"),
        model=settings.agent_model("podcast_dialogue_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
    )


def build_section_synthesis_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Section Synthesis Agent",
        instructions=get_prompt("section_synthesis_agent"),
        model=settings.agent_model("podcast_dialogue_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
    )


def build_dialogue_writer_agent(settings: Settings, *, language: str) -> Agent:
    """One factory, two prompts. `language` selects the prompt key."""
    if language == "en-US":
        prompt_key = "dialogue_writer_agent_en"
        name = "Podcast Dialogue Writer Agent (EN)"
    elif language == "de-DE":
        prompt_key = "dialogue_writer_agent_de"
        name = "Podcast Dialogue Writer Agent (DE)"
    else:
        raise ValueError(f"Unsupported podcast language: {language!r}")
    return Agent(
        name=name,
        instructions=get_prompt(prompt_key),
        model=settings.agent_model("podcast_dialogue_writer_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PodcastScript,
    )


def build_director_pass_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Director Pass Agent",
        instructions=get_prompt("director_pass_agent"),
        model=settings.agent_model("podcast_director_pass_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PodcastScript,
    )


def build_host_authority_pass_agent(settings: Settings) -> Agent:
    return Agent(
        name="Podcast Host Authority Pass Agent",
        instructions=get_prompt("host_authority_pass_agent"),
        model=settings.agent_model("podcast_host_authority_pass_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=PodcastScript,
    )


def build_episode_metadata_agent(settings: Settings, *, language: str) -> Agent:
    """Generate the per-episode title + summary sent to Spotify.

    Tiny call — input is the ranked clusters' headlines + summaries,
    output is a typed EpisodeMetadata. Language-aware so each language
    edition has metadata in the right tongue.
    """
    if language == "en-US":
        prompt_key = "episode_metadata_agent_en"
        name = "Podcast Episode Metadata Agent (EN)"
    elif language == "de-DE":
        prompt_key = "episode_metadata_agent_de"
        name = "Podcast Episode Metadata Agent (DE)"
    else:
        raise ValueError(f"Unsupported podcast language: {language!r}")
    return Agent(
        name=name,
        instructions=get_prompt(prompt_key),
        model=settings.agent_model("podcast_episode_metadata_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=EpisodeMetadata,
    )
