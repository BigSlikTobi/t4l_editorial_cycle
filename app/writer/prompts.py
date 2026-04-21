from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PROMPTS_PATH = Path(__file__).with_name("prompts.yml")
REQUIRED_PROMPTS = {
    "article_writer_agent",
    "article_writer_agent_de",
    "persona_selector_agent",
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
