"""German article flow: persona lookup by archetype+language, reverse byline
lookup across both registries, and adapter lookup by (fingerprint, language)."""

from __future__ import annotations

import pytest

from app.writer.personas import (
    PERSONA_IDS,
    PERSONAS,
    PERSONAS_DE,
    byline_to_persona_id,
    get_persona,
)


# --- Persona registry ---------------------------------------------------


def test_en_and_de_share_archetype_ids():
    assert set(PERSONAS.keys()) == set(PERSONAS_DE.keys())
    assert set(PERSONAS.keys()) == set(PERSONA_IDS)


def test_get_persona_defaults_to_english():
    p = get_persona("analyst")
    assert p.byline == "Marcus Reed"


def test_get_persona_german_variant():
    p = get_persona("analyst", "de-DE")
    assert p.byline == "Marc Richter"
    assert p.role.startswith("Cap")


def test_get_persona_all_three_archetypes_have_de_names():
    assert get_persona("analyst", "de-DE").byline == "Marc Richter"
    assert get_persona("insider", "de-DE").byline == "Jana Hoffmann"
    assert get_persona("columnist", "de-DE").byline == "Lena Weber"


def test_get_persona_unknown_language_falls_back_to_english():
    # Defensive: any non-'de-DE' language returns the English persona.
    assert get_persona("analyst", "fr-FR").byline == "Marcus Reed"


def test_get_persona_invalid_id_raises():
    with pytest.raises(KeyError):
        get_persona("unknown", "de-DE")


# --- Byline reverse lookup ---------------------------------------------


def test_byline_to_persona_id_recognizes_english_bylines():
    assert byline_to_persona_id("Marcus Reed") == "analyst"
    assert byline_to_persona_id("Jenna Alvarez") == "insider"
    assert byline_to_persona_id("Casey Whitaker") == "columnist"


def test_byline_to_persona_id_recognizes_german_bylines():
    assert byline_to_persona_id("Marc Richter") == "analyst"
    assert byline_to_persona_id("Jana Hoffmann") == "insider"
    assert byline_to_persona_id("Lena Weber") == "columnist"


def test_byline_to_persona_id_unknown_returns_none():
    assert byline_to_persona_id("Some Random Person") is None
    assert byline_to_persona_id(None) is None
    assert byline_to_persona_id("") is None
