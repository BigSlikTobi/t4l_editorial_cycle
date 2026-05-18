from __future__ import annotations

from app.podcast.personas import (
    ANALYST_PERSONA,
    COLOR_PERSONA,
    PODCAST_PERSONAS,
    get_podcast_persona,
)


class TestPodcastPersonas:
    def test_two_personas_registered(self) -> None:
        assert set(PODCAST_PERSONAS.keys()) == {"color", "analyst"}

    def test_unique_bylines(self) -> None:
        assert COLOR_PERSONA.byline != ANALYST_PERSONA.byline

    def test_both_have_en_de_style_guides(self) -> None:
        for persona in (COLOR_PERSONA, ANALYST_PERSONA):
            assert len(persona.style_guide_en) > 200
            assert len(persona.style_guide_de) > 200

    def test_archetypes_are_distinct(self) -> None:
        assert COLOR_PERSONA.archetype == "former-athlete-host"
        assert ANALYST_PERSONA.archetype == "former-pro-analyst"

    def test_get_persona_round_trip(self) -> None:
        assert get_podcast_persona("color") is COLOR_PERSONA
        assert get_podcast_persona("analyst") is ANALYST_PERSONA

    def test_german_personas_support_denglish_listener_and_opinion(self) -> None:
        assert "natürlichem Denglish" in COLOR_PERSONA.style_guide_de
        assert "direkt mit 'du'" in COLOR_PERSONA.style_guide_de
        assert "eigener Meinung" in ANALYST_PERSONA.style_guide_de
        assert "Ich bleib dabei" in ANALYST_PERSONA.style_guide_de
