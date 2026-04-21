import pytest

from app.writer.personas import (
    PERSONA_IDS,
    PERSONAS,
    byline_to_persona_id,
    get_persona,
)


def test_three_personas_exist():
    assert set(PERSONA_IDS) == {"analyst", "insider", "columnist"}
    assert len(PERSONAS) == 3


def test_each_persona_has_required_fields():
    for p in PERSONAS.values():
        assert p.id
        assert p.byline
        assert p.role
        assert p.style_guide
        assert len(p.style_guide) > 50  # non-trivial guidance


def test_bylines_are_unique():
    bylines = [p.byline for p in PERSONAS.values()]
    assert len(bylines) == len(set(bylines))


def test_get_persona_returns_frozen_record():
    p = get_persona("analyst")
    assert p.byline == "Marcus Reed"
    with pytest.raises(Exception):
        p.byline = "other"  # frozen dataclass


def test_get_persona_raises_on_unknown_id():
    with pytest.raises(KeyError):
        get_persona("fanboy")


def test_byline_roundtrip():
    for pid, persona in PERSONAS.items():
        assert byline_to_persona_id(persona.byline) == pid


def test_byline_unknown_returns_none():
    assert byline_to_persona_id("Some Stranger") is None
    assert byline_to_persona_id(None) is None
    assert byline_to_persona_id("") is None
