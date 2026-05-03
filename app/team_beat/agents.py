"""Agent factories for the team-beat module.

Two agents:
  * Team Beat Reporter — receives a team's 12h article window, decides
    to file or skip, writes EN+DE bodies in the persona voice. One
    bilingual call to keep voice + facts + filing decision atomic.
    Optionally exposes a `lookup_article_content` tool so it can fetch
    the full body of articles it judges load-bearing for the lead.
  * Radio Script — converts the DE body into a 90-120s anchor-framed
    German script with Gemini natural-language prosody (director's
    notes preamble + sparse inline audio tags).

The OpenAI Agents SDK + ModelSettings reuse follow the writer module's
pattern via `app/writer/model.py::build_model_settings` (24h prompt
cache, store=True).
"""

from __future__ import annotations

from collections.abc import Sequence

from agents import Agent
from agents.tool import FunctionTool

from app.config import Settings
from app.team_beat.prompts import get_prompt
from app.team_beat.schemas import BeatBrief, RadioScript
from app.writer.model import build_model_settings


def build_team_beat_reporter_agent(
    settings: Settings,
    *,
    tools: Sequence[FunctionTool] = (),
) -> Agent:
    """Build the per-team beat reporter agent.

    `tools` is optional so unit tests (and any caller that wants the
    headline-only behavior) can pass `()`. Production callers pass a
    one-element sequence with `lookup_article_content` so the agent
    can fetch full article bodies on demand. `parallel_tool_calls=False`
    is preserved — lookups should be a deliberate sequential signal,
    not a fan-out the model uses to dump every URL into context.
    """
    return Agent(
        name="Team Beat Reporter Agent",
        instructions=get_prompt("team_beat_reporter_agent"),
        model=settings.agent_model("team_beat_reporter_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=list(tools),
        output_type=BeatBrief,
    )


def build_radio_script_agent(settings: Settings) -> Agent:
    return Agent(
        name="Radio Script Agent (DE)",
        instructions=get_prompt("radio_script_agent"),
        model=settings.agent_model("radio_script_agent"),
        model_settings=build_model_settings(settings, parallel_tool_calls=False),
        tools=[],
        output_type=RadioScript,
    )
