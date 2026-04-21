# Changelog — 2026-04-21 (session 2)

## Summary
Follow-up session adding beat-reporter writing texture to both language prompts,
a standalone HTML cycle report script for post-run inspection, and a GitHub Actions
workflow that automates the editorial cycle on a 2-hour cadence.

## Changes

### Prompt quality — beat-reporter texture
- Added "Beat-reporter texture" guidance block to `article_writer_agent` (EN) and
  `article_writer_agent_de` (DE) in `app/writer/prompts.yml`.
  - Cap: ~15% of prose; never overdo it.
  - Active, specific verbs over generic ones (`"shrugged off"` not `"said"`).
  - Scene and stakes language permitted only when it flows from source facts.
  - Sentence-length variation: mix one short punchy sentence into longer runs.
  - Concrete nouns preferred (`"The Monday presser"` over `"media availability"`).
  - Hard guardrail: texture sits on top of facts, never replaces them.
  - DE version mirrors EN rules in idiomatic German.

### New: cycle report script (`scripts/build_cycle_report.py`)
- Reads `var/output.json` (from `editorial-cycle run --output-json`) and fetches
  matching `content.team_article` rows by fingerprint via Supabase PostgREST.
- Renders `var/cycle_report.html` and prints the `file://` URL.
- Per-story card layout:
  - Left panel: rank, score, action, reasoning, team codes, player mentions, source digests.
  - Right panel: EN + DE articles, cover image, tier badge, language badge, full content,
    collapsible extras (bullets, X post, mentioned players with headshots).
- Header KPI bar: stories / written / updated / prevented / language split / image tier mix.
- Usage: `./venv/bin/python scripts/build_cycle_report.py [--input var/output.json]`

### New: GitHub Actions workflow (`.github/workflows/editorial-cycle.yml`)
- Schedule: `0 */2 * * *` (every 2 hours); aligns `LOOKBACK_HOURS=2` to feed window.
- `workflow_dispatch` with `top_n` and `lookback_hours` overrides.
- Concurrency group `editorial-cycle`, `cancel-in-progress: false` — no overlap allowed.
- Timeout: 30 minutes.
- Uploads `var/output.json` as artifact `cycle-{run_id}` (14-day retention).
- Required secrets documented in CLAUDE.md; model overrides via repo vars.

## Files Modified
- `app/writer/prompts.yml` — beat-reporter texture block added to EN and DE writer prompts

## Files Added
- `scripts/build_cycle_report.py` — HTML cycle report generator (new)
- `.github/workflows/editorial-cycle.yml` — scheduled CI workflow (new)
- `CLAUDE.md` — added Commands entry for cycle report, Prompts section note, and new
  "CI / GitHub Actions" + "Cycle Report Script" sections

## Code Quality Notes
- Tests: 117 passed, 0 failed, 0 errors (full suite `./venv/bin/pytest tests/ -v`)
- No linting step configured for Python; no issues observed in new/modified files
- `build_cycle_report.py` uses `httpx` directly (not through an adapter) — consistent
  with its nature as a standalone dev tool, not production code

## Open Items / Carry-over
- Apply `supabase/migrations/007_add_story_fingerprint.sql` before that column is needed
- 9 curated images hit Gemini safety filter — retry with `--mode sync` later
- Orphaned DE rows 2592/2594/2596 in `content.team_article` to delete when convenient
- Fresh Supabase project migration — brainstormed, not executed
- Add required secrets to GitHub repo settings before first scheduled workflow run
