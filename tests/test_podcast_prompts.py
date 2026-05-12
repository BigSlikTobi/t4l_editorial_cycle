from __future__ import annotations

import pytest

from app.podcast.prompts import REQUIRED_PROMPTS, get_prompt, load_prompts


class TestPodcastPrompts:
    def test_all_required_prompts_present(self) -> None:
        prompts = load_prompts()
        assert REQUIRED_PROMPTS.issubset(prompts.keys())

    def test_lru_cache(self) -> None:
        # Two reads return the same dict identity (cached).
        assert load_prompts() is load_prompts()

    def test_prompts_are_non_empty(self) -> None:
        prompts = load_prompts()
        for key in REQUIRED_PROMPTS:
            assert prompts[key].strip(), f"Prompt {key!r} is empty"

    def test_unknown_prompt_raises(self) -> None:
        with pytest.raises(KeyError):
            get_prompt("does_not_exist")

    def test_dialogue_writers_distinct(self) -> None:
        en = get_prompt("dialogue_writer_agent_en")
        de = get_prompt("dialogue_writer_agent_de")
        assert en != de
        # German prompt should contain at least one diacritic / German word.
        assert any(token in de for token in ("Du", "Sprecher", "Wortzahl"))
