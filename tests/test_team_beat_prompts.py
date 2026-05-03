"""Smoke tests for the team-beat prompt loader."""

from __future__ import annotations

import pytest

from app.team_beat.prompts import REQUIRED_PROMPTS, get_prompt, load_prompts


class TestPromptsContract:
    def test_required_prompts_present(self) -> None:
        prompts = load_prompts()
        for required in REQUIRED_PROMPTS:
            assert required in prompts, f"missing required prompt: {required}"

    def test_team_beat_reporter_documents_news_reactive_optout(self) -> None:
        # The should_file=False opt-out is load-bearing; if the prompt
        # ever drops it the agent will start force-filing thin output.
        body = get_prompt("team_beat_reporter_agent")
        assert "should_file" in body
        assert "skip_reason" in body
        assert "no_news" in body.lower()

    def test_radio_script_documents_no_ssml_and_anchor_frame(self) -> None:
        body = get_prompt("radio_script_agent")
        # Must spell out that SSML is not used.
        assert "SSML" in body
        # Must mention the natural-language audio tags.
        assert "[pause]" in body
        # Must establish the studio-anchor frame (third-person reporter).
        assert "studio_anchor" in body or "Studio" in body or "anchor" in body.lower()
        assert "third person" in body.lower() or "THIRD person" in body or "RELAYS" in body

    def test_get_prompt_unknown_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown prompt name"):
            get_prompt("nonexistent_prompt")
