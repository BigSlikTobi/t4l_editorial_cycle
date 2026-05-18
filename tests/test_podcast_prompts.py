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

    def test_dialogue_prompts_reject_professor_register(self) -> None:
        en = get_prompt("dialogue_writer_agent_en")
        de = get_prompt("dialogue_writer_agent_de")

        assert "two professors" in en
        assert "locker-room / barbershop / sports-bar language" in en
        assert "kein Harvard-Panel" in de
        assert "Straße statt Seminarraum" in de
        assert "Dozentenmodus" in de

    def test_german_prompts_require_tts_safe_abbreviations(self) -> None:
        cold_open = get_prompt("cold_open_writer_agent")
        de = get_prompt("dialogue_writer_agent_de")

        assert "TTS-safe spoken German" in cold_open
        assert '"1pm" → "ein Uhr nachmittags"' in cold_open
        assert '"vs." → "gegen"' in cold_open
        assert "TTS-SICHERE SCHREIBWEISE" in de
        assert '"1pm" oder "1 PM" wird "ein Uhr nachmittags"' in de
        assert '"Q1", "Q2", "Q3", "Q4"' in de
        assert "erstes Quarter" in de

    def test_german_prompt_limits_shared_shorthand_patterns(self) -> None:
        de = get_prompt("dialogue_writer_agent_de")

        assert "Tape on, let's go" in de
        assert "Kaffee auf, Tape an" not in de
        assert "maximal ein Whiteboard-Callback" in de
        assert "Trailer-Stimme\" höchstens einmal" in de
        assert "Das ist kein X, das ist Y" in de

    def test_german_prompt_requires_direct_listener_address_and_denglish(self) -> None:
        de = get_prompt("dialogue_writer_agent_de")

        assert "DENGLISH IST NICHT NUR ERLAUBT, SONDERN DER DEFAULT" in de
        assert "Football-Wörter auf Englisch" in de
        assert "DIREKT mit \"du\"" in de
        assert "der Hörer" in de

    def test_german_prompt_allows_stronger_fact_grounded_opinion(self) -> None:
        de = get_prompt("dialogue_writer_agent_de")

        assert "Mehr echte Meinung, mehr Reibung" in de
        assert "Ich bleib dabei" in de
        assert "Da komm ich nicht mit" in de
        assert "mit ihrer Meinung aus dem Segment rausgehen" in de

    def test_german_prompt_rejects_artificial_foreign_word_filler(self) -> None:
        de = get_prompt("dialogue_writer_agent_de")

        assert "Vermeide künstliche deutsche Fremdwörter" in de
        assert "operationalisieren" in de
        assert "das killt dich" in de

    def test_prompts_require_four_section_research_agency(self) -> None:
        prompts = load_prompts()

        assert "section_planner_agent" in prompts
        assert "player_of_day_research_agent" in prompts
        assert "team_of_day_research_agent" in prompts
        assert "deep_dive_research_agent" in prompts
        assert "section_synthesis_agent" in prompts
        assert "Maximum depth matters" in prompts["player_of_day_research_agent"]
        assert "Maximum depth matters" in prompts["team_of_day_research_agent"]
        assert "Deep Dive is mandatory" in prompts["deep_dive_research_agent"]
        assert "red line" in prompts["section_synthesis_agent"]

    def test_host_authority_pass_prompt_requires_confident_grounded_rewrites(self) -> None:
        prompt = get_prompt("host_authority_pass_agent")

        assert "Host Authority Pass" in prompt
        assert "sound owned" in prompt
        assert "confident uncertainty" in prompt
        assert "Never invent a fact" in prompt
        assert "Do not add new claim markers" in prompt
        assert "Preserve natural German Denglish" in prompt
