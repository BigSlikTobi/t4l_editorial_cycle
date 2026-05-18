from __future__ import annotations

from datetime import date

import pytest

from app.podcast.factcheck import (
    PodcastClaim,
    PodcastSource,
    assert_valid_episode,
    strip_script_claim_refs,
    validate_claims_and_sources,
    validate_naturalness,
)
from app.podcast.schemas import PodcastScript, ScriptLine


def _source() -> PodcastSource:
    return PodcastSource(
        id="S1",
        title="Team report",
        url="https://example.com/story",
        publisher="Example",
    )


def _claim() -> PodcastClaim:
    return PodcastClaim(
        id="1",
        text="The quarterback threw three touchdowns.",
        source_ids=["S1"],
        claim_type="stat",
        confidence="high",
        number_checked=True,
    )


def _script(lines: list[ScriptLine]) -> PodcastScript:
    return PodcastScript(
        language="de-DE",
        run_date=date(2026, 5, 13),
        body=lines,
    )


def test_claim_validation_rejects_unknown_script_marker() -> None:
    script = _script([ScriptLine(speaker="color", text="Drei Touchdowns. [C99]")])
    result = validate_claims_and_sources(
        sources=[_source()],
        claims=[_claim()],
        script=script,
    )
    assert not result.ok
    assert any("unknown claim 99" in error for error in result.errors)


def test_claim_validation_rejects_sensitive_unmarked_line() -> None:
    script = _script([ScriptLine(speaker="color", text="Er war bei 42 Prozent Druckrate.")])
    result = validate_claims_and_sources(
        sources=[_source()],
        claims=[_claim()],
        script=script,
    )
    assert not result.ok
    assert any("without a claim marker" in error for error in result.errors)


def test_strip_script_claim_refs_removes_audio_markers() -> None:
    script = _script([ScriptLine(speaker="analyst", text="Das ist klar. [C1]")])
    clean = strip_script_claim_refs(script)
    assert clean.body[0].text == "Das ist klar."


def test_claim_validation_rejects_on_air_source_meta_language() -> None:
    script = _script(
        [
            ScriptLine(
                speaker="analyst",
                text="Wie aus den Quellen zu erkennen ist, waren es drei Touchdowns. [C1]",
            )
        ]
    )
    result = validate_claims_and_sources(
        sources=[_source()],
        claims=[_claim()],
        script=script,
    )
    assert not result.ok
    assert any("source/meta attribution" in error for error in result.errors)


def test_claim_validation_allows_expert_voice_with_hidden_marker() -> None:
    script = _script(
        [ScriptLine(speaker="analyst", text="Das waren drei Touchdowns. [C1]")]
    )
    result = validate_claims_and_sources(
        sources=[_source()],
        claims=[_claim()],
        script=script,
    )
    assert result.ok


def test_naturalness_rejects_strict_robotic_alternation() -> None:
    body = [
        ScriptLine(speaker="color" if idx % 2 == 0 else "analyst", text="Okay.")
        for idx in range(12)
    ]
    result = validate_naturalness(
        script=_script(body),
        host_memory="\"Tape on, let's go.\"",
    )
    assert not result.ok
    assert any("robotic" in error for error in result.errors)


def test_naturalness_rejects_forced_unsourced_disagreement() -> None:
    script = _script(
        [
            ScriptLine(speaker="color", text="Tape on, let's go."),
            ScriptLine(speaker="analyst", text="Sehe ich anders, Marcus."),
        ]
    )
    result = validate_naturalness(
        script=script,
        host_memory="\"Tape on, let's go.\"",
    )
    assert not result.ok
    assert any("disagreement" in error for error in result.errors)


def test_valid_episode_accepts_sourced_tension_and_memory_beats() -> None:
    script = _script(
        [
            ScriptLine(speaker="color", text="Tape on, let's go."),
            ScriptLine(speaker="analyst", text="Du willst wieder Trailer-Stimme."),
            ScriptLine(speaker="color", text="Wir parken das auf dem Whiteboard."),
            ScriptLine(speaker="analyst", text="Da geh ich halb mit: drei Touchdowns. [C1]"),
        ]
    )
    assert_valid_episode(
        sources=[_source()],
        claims=[_claim()],
        script=script,
        host_memory="\"Tape on, let's go.\"",
    )

def test_naturalness_warns_on_repeated_contrast_formula() -> None:
    script = _script(
        [
            ScriptLine(speaker="color", text="Das ist kein Bauchgefühl, das ist ein Warnschild."),
            ScriptLine(speaker="analyst", text="Das ist keine Krise, sondern ein Test."),
        ]
    )

    result = validate_naturalness(
        script=script,
        host_memory="\"Tape on, let's go.\"",
    )

    assert result.ok
    assert any("contrast formula" in warning for warning in result.warnings)


def test_claim_validation_rejects_missing_source_reference() -> None:
    bad_claim = _claim().model_copy(update={"source_ids": ["S404"]})
    script = _script([ScriptLine(speaker="color", text="Drei Touchdowns. [C1]")])
    result = validate_claims_and_sources(
        sources=[_source()],
        claims=[bad_claim],
        script=script,
    )
    assert not result.ok
    assert any("missing source S404" in error for error in result.errors)
