"""Prompt loader for the podcast module.

Mirrors `app/team_beat/prompts.py` exactly: YAML on disk, `@lru_cache`
read, required-key validation at load time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_PATH = Path(__file__).with_name("prompts.yml")
REQUIRED_PROMPTS = {
    "cluster_ranker_agent",
    "cold_open_writer_agent",
    "section_planner_agent",
    "player_of_day_research_agent",
    "team_of_day_research_agent",
    "deep_dive_research_agent",
    "section_synthesis_agent",
    "dialogue_writer_agent_en",
    "dialogue_writer_agent_de",
    "host_authority_pass_agent",
    "director_pass_agent",
    "episode_metadata_agent_en",
    "episode_metadata_agent_de",
}


@lru_cache(maxsize=1)
def load_prompts() -> dict[str, str]:
    raw = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Prompt file must be a mapping: {PROMPTS_PATH}")

    prompts: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"Prompt file contains non-string entry for key {key!r}")
        prompts[key] = value.strip()

    missing = sorted(REQUIRED_PROMPTS - prompts.keys())
    if missing:
        raise ValueError(f"Prompt file is missing required prompts: {', '.join(missing)}")
    return prompts


def get_prompt(name: str) -> str:
    prompts = load_prompts()
    try:
        return prompts[name]
    except KeyError as exc:
        raise KeyError(f"Unknown prompt name: {name}") from exc
