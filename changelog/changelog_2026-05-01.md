# Changelog — 2026-05-01

## Summary
Fixed a non-fatal OpenAI traces 400 error caused by integer metadata values, and resolved a silent editorial-memory truncation bug that was cutting off `rewrite_lessons.md` before it reached the writer agent.

## Changes
- **Fix: OpenAI traces metadata type error** — `rewrite_attempt` was passed as an `int` in two `build_run_config` metadata dicts (`article_quality_gate` stage at ~line 192, `editorial_memory_update` stage at ~line 295 in `writer/workflow.py`). The OpenAI traces ingest API requires all metadata values to be strings; changed both to `str(rewrite_attempt)`.
- **Fix: editorial memory truncation** — `MAX_WIKI_PAGE_CHARS` was 2400 chars, silently cutting `editorial_memory/wiki/rewrite_lessons.md` (grown to 3729 chars) before it reached the article writer. Raised to 7000.
- **Fix: total memory budget** — `MAX_MEMORY_CHARS` raised from 7000 to 12000 to accommodate the wider per-page cap without the combined wiki context immediately hitting the aggregate ceiling.
- **Investigation: memory persistence in CI** — confirmed the `editorial-memory` branch sync flow in GitHub Actions works correctly; the truncation cap was the real cause of memory not reaching the writer, not a sync issue.
- **Deferred: wiki-summary-agent** — a smarter alternative (agent that summarizes wiki pages before injection) was identified but deferred. A one-time remote agent (trig_01SsDgRVtaEWNHQGBb32DFZd) is scheduled for 2026-05-15 07:07 UTC to revisit and recommend whether to build it.

## Files Modified
- `app/writer/workflow.py` — stringified `rewrite_attempt` int → str in two `build_run_config` metadata dicts (lines ~192, ~295).
- `app/writer/editorial_memory.py` — raised `MAX_WIKI_PAGE_CHARS` 2400→7000 and `MAX_MEMORY_CHARS` 7000→12000.

## Code Quality Notes
- All 191 tests passed (`./venv/bin/pytest tests/ -v`).
- No linting step configured; no UI files changed.
- No TODO/FIXME/debug artifacts introduced.

## Open Items / Carry-over
- Migration `007_add_story_fingerprint.sql` (`story_fingerprint text` on `content.team_article`) remains pending in Supabase SQL Editor.
- RLS: `content.team_article` still missing `GRANT SELECT ON content.team_article TO anon;` — edge functions return empty results until applied.
- Remote agent trig_01SsDgRVtaEWNHQGBb32DFZd fires 2026-05-15 to evaluate the wiki-summary-agent decision.
