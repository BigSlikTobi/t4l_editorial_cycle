# Changelog — 2026-04-26

## Summary
Added a pre-publish overview HTML report that surfaces all 206 team_articles paired by story fingerprint, and tightened both the `article_data_agent` and `article_writer_agent` (EN + DE) prompts to eliminate a "source-as-story" failure mode where articles described the structure of a source publication instead of its underlying football facts.

## Changes

### New: `scripts/build_overview_report.py`
- Fetches every row from `content.team_article` via paginated PostgREST (public schema, no profile header).
- Pairs EN + DE articles per `story_fingerprint`; joins `editorial_state` records by fingerprint.
- Ingests orchestrator trace data (reasoning, source_digests, news_value_score, action) from `var/output.json` and `var/test_runs/cycle_*.json`.
- Resolves player headshots from `public.players` for all `mentioned_players` IDs.
- Renders `var/overview_report.html`: dark-theme two-column layout, KPI strip (article count, story count, EN/DE split, paired count, image tier mix, state/trace coverage), live text filter, per-story cards with editorial state, agent trace + source digests, and side-by-side EN/DE article cards.
- Fixed a 406 error during development: `team_article` is no longer exposed under the `content` schema via PostgREST — it is reachable as `/rest/v1/team_article` with no profile header (public schema routing).
- Supports `--limit N` CLI flag for quick testing.

### Prompt fix: `app/editorial/prompts.yml` — `article_data_agent`
- Added "Roundup / ranking / grade extraction rule" block.
- For multi-team source pieces (draft grades, power rankings, mock drafts, "winners and losers", all-32 reviews), instructs the agent to extract row-level verdicts (grade, rank, pick, named player, quoted reason) as `key_facts`, not column-level meta about the source's methodology or framing.
- If the source only carries column-level meta and no team-specific verdicts, instructs `content_status="thin"` and `confidence <= 0.3`, which routes to the writer's existing "thin → write shorter" path.
- Updated `summary` and `key_facts` field instructions to reinforce roundup-specific extraction.

### Prompt fix: `app/writer/prompts.yml` — `article_writer_agent` (EN) and `article_writer_agent_de` (DE)
- Added "Source-as-story anti-pattern" block to both prompts (DE version translated, not just copy-pasted).
- Bans framings where the subject of a sentence is the publication or the act of publishing/grading/ranking rather than a football fact.
- Explicitly bans phrases like "X's piece", "X's framework", "the format pushes", "puts every team on the same scale", etc.
- Requires writers to focus on the team's specific row in a roundup: their grade, rank, pick, or named player.
- Authorizes shorter articles over padded ones; adds a per-paragraph self-check test ("what new football fact did this paragraph give the reader?").
- The two prompt changes interlock: the `article_data_agent` now surfaces row-level facts, and the `article_writer_agent` is now instructed to write from those facts rather than the source's framing.

## Files Modified
- `scripts/build_overview_report.py` — new script (overview HTML report)
- `app/editorial/prompts.yml` — added "Roundup / ranking / grade extraction rule" to `article_data_agent`
- `app/writer/prompts.yml` — added "Source-as-story anti-pattern" block to `article_writer_agent` (EN) and `article_writer_agent_de` (DE)

## Code Quality Notes
- Tests: **155 passed, 0 failed** (`./venv/bin/pytest tests/ -v`)
- Linting: not run (no changes to Python logic files — only a new utility script and YAML prompts)
- No TODO/FIXME/debug prints found in the new script
- No syntax errors detected

## Open Items / Carry-over
- The new "source-as-story" guardrails were not stress-tested with a roundup source in this session. The verification cycle (articles #203–206) covered Cowboys defense and Jets draft — neither was a multi-team grade/ranking piece. A follow-up cycle with a roundup source would confirm the interlocking prompts work end-to-end.
- Migration 007 (`story_fingerprint text` on `content.team_article`) is still marked **pending** in CLAUDE.md. The overview report handles pre-migration articles as "orphan" cards.
- `scripts/build_architecture_graph.py`, `scripts/architecture_graph_manual.yml`, and `tests/test_architecture_graph.py` remain as pre-existing untracked files — not part of this session's work.
