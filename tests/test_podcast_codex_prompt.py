from __future__ import annotations

from pathlib import Path


PROMPT_PATH = Path("scripts/podcast_codex_morning.md")


def test_codex_morning_prompt_requires_independent_research_agents() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Spawn two research agents in parallel" in prompt
    assert "investigate independently" in prompt
    assert "must not share notes" in prompt
    assert "`analytics_pack.json`" in prompt
    assert "Do not merge the two research reports" in prompt


def test_codex_morning_prompt_maps_investigations_to_hosts() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Marcus brings findings from `research_a.md`" in prompt
    assert "Robin brings findings from `research_b.md`" in prompt
    assert "they compare investigations on air" in prompt


def test_codex_morning_prompt_uses_nflreadpy_as_robin_stat_source() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "nflverse/nflreadpy" in prompt
    assert '"id": "NFLVERSE"' in prompt
    assert 'source_ids": ["NFLVERSE"]' in prompt
    assert "Robin may use nflverse stats from `analytics_pack.json`" in prompt


def test_codex_morning_prompt_requires_pronunciation_workflow() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "`pronunciation_guide.json`" in prompt
    assert "T4L is pronounced" in prompt
    assert "Tyler Shough" in prompt
    assert "Tee Vier Ell" in prompt
    assert "less perfect on purpose" in prompt


def test_codex_morning_prompt_requires_analytics_angle_mining() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "`angle_candidates`" in prompt
    assert "Analytics Angle Mining" in prompt
    assert "This podcast is not a news read" in prompt
    assert "football question" in prompt
    assert "follow-up web investigation" in prompt
    assert "what would be overclaiming" in prompt


def test_codex_morning_prompt_keeps_sources_off_air() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Source attribution is for the hidden ledgers" in prompt
    assert "not for the hosts' spoken voice" in prompt
    assert "wie aus den" in prompt
    assert "source_ledger.json" in prompt


def test_codex_morning_prompt_requires_four_section_specialist_research() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Four-Section Specialist Research" in prompt
    assert "section_plan.json" in prompt
    assert "research_player_of_day.md" in prompt
    assert "research_team_of_day.md" in prompt
    assert "research_deep_dive.md" in prompt
    assert "section_synthesis.md" in prompt
    assert "Deep Dive is mandatory" in prompt
    assert "3,750 spoken words" in prompt
    assert "NFLVERSE" in prompt


def test_codex_morning_prompt_rejects_professor_register() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Harvard professors" in prompt
    assert "language from the street" in prompt
    assert "language from the books" in prompt
    assert "no \"Diskurs\"" in prompt
    assert "former player would say it naturally" in prompt


def test_codex_morning_prompt_requires_denglish_direct_listener_and_opinion() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "natural\n  Denglish" in prompt
    assert "direct listener beat" in prompt
    assert "Ich bleib dabei" in prompt
    assert "operationalisieren" in prompt


def test_codex_morning_prompt_requires_host_authority_pass() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Host Authority Pass" in prompt
    assert "host_authority_notes.md" in prompt
    assert "confident uncertainty" in prompt
    assert "no new unsupported facts" in prompt


def test_codex_morning_prompt_requires_tts_safe_abbreviations() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "TTS-safe" in prompt
    assert "ein Uhr nachmittags" in prompt
    assert "8:30 AM" in prompt
    assert "gegen" in prompt
    assert "Be cautious with abbreviations" in prompt
    assert "erstes Quarter" in prompt


def test_codex_morning_prompt_limits_shared_shorthand_patterns() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert "Tape on, let's go." in prompt
    assert "Do not\n  use \"Kaffee auf, Tape an.\"" in prompt
    assert "at most one Whiteboard callback" in prompt
    assert "Trailer-Stimme" in prompt
    assert "comparison formulas" in prompt
