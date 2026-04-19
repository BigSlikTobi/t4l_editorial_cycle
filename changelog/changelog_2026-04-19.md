# Changelog — 2026-04-19

## Summary
Built out the full image cascade for the editorial cycle: overhauled AI image generation prompts to produce team-colored in-game action shots, added Wikimedia Commons as a tier-1 CC fallback, and tightened the image validator to catch cross-team and archival-image false positives. End-to-end cycle ran cleanly at close of day with 3 articles written and all image tiers exercised.

## Changes

### Image Cascade — AI Generation (tier 3)
- Replaced "cinematic/moody editorial" style with "wire-service journalistic photojournalism" framing
- Removed the old scene catalog that was producing empty-stadium/generic shots
- Hard-biased toward in-game action (tackles, catches, sacks, touchdowns)
- Injected team full name + uniform color palette into the prompt (e.g. "Kansas City Chiefs, red and gold") instead of opaque team codes — AI images now show team-colored action

### Image Cascade — Wikimedia Commons (new tier 1 fallback)
- Added `WikimediaCommonsClient` in `app/writer/image_clients.py` — uses the public MediaWiki API, no API key required
- Runs after Google CC Search when that source returns nothing or the validator rejects its result
- Fixed 403 errors from `upload.wikimedia.org` by adding `User-Agent` header to the selector's `_http` client
- Query strategy: player name alone when a dominant player is present; team code + "NFL football" for team-level stories
- Tier-1 hit rate went from ~0% to consistently returning real CC photos

### Image Cascade — Validator (`app/writer/image_validator.py`)
- `does_image_match` now accepts `expected_team_code` and `expected_team_name` kwargs
- Explicitly rejects: different-team-wordmark contradictions (fixes Raiders/Patriots historical-affiliation bug), static portraits/mugshots, dated archival photos, low-quality images
- Ambiguity on team identity = accept; only positive contradiction (visible wrong-team wordmark) = reject
- OCR validator `image_contains_text` loosened to accept jersey numbers and yard-line numbers, still rejects words/wordmarks/scoreboards

### Team Codes (`app/team_codes.py` — new file)
- Added `TEAM_FULL_NAMES` dict and `team_full_name()` helper (used by validator)
- Added `TEAM_COLORS` dict and `team_colors()` helper (used by AI image prompt)

### Writer Workflow & Orchestration
- `app/writer/workflow.py`: wired image cascade into article generation pipeline
- `app/orchestration.py`: propagated team context through to image selector
- `app/writer/persona_selector.py`, `app/writer/personas.py`: persona system added (writer voice selection per article)

### Editorial Phase (carried from prior days, now committed)
- `app/editorial/helpers.py`: fingerprint helpers, dedup-plan, URL overlap ratio, resolve-existing-article-ids logic
- `app/editorial/tools.py`, `app/editorial/workflow.py`, `app/editorial/prompts.yml`: orchestrator improvements
- `app/adapters.py`, `app/schemas.py`, `app/config.py`: schema and adapter additions supporting all of the above

### Supabase Migrations (new, not yet applied)
- `supabase/migrations/002_add_source_urls.sql`
- `supabase/migrations/003_add_author.sql`
- `supabase/migrations/004_add_mentioned_players.sql`

### Scripts (`scripts/` — new utility scripts)
- `audit_team_codes.py` — one-off audit of team code coverage
- `build_articles_view.py` — local view builder for published articles
- `build_quality_report.py` — quality report generator
- `rerun_image_cascade.py` — re-runs image selection on already-published articles

### Tests
- 102 tests, all passing (0.30s)
- Added tests for Wikimedia fallback path (4 new: `test_wikimedia_fallback_used_when_google_returns_none`, `test_wikimedia_fallback_used_when_google_rejected`, `test_wikimedia_prefers_player_name_when_available`, `test_validator_receives_expected_team_context`)
- Updated `FakeValidator` to accept `**kwargs` for forward compatibility with new `expected_team_*` params
- Added test suites: `test_personas.py`, `test_player_enrichment.py`, `test_team_codes.py`

## Files Modified
- `app/writer/image_selector.py` — full image cascade orchestration, AI prompt overhaul, Wikimedia wiring
- `app/writer/image_clients.py` — new `WikimediaCommonsClient`
- `app/writer/image_validator.py` — team contradiction detection, archival/portrait rejection, OCR loosening
- `app/writer/personas.py` — new: persona definitions
- `app/writer/persona_selector.py` — new: persona selection logic
- `app/writer/workflow.py` — image cascade + persona integration
- `app/writer/prompts.yml` — prompt updates for writer agent
- `app/writer/prompts.py` — minor prompt loader update
- `app/team_codes.py` — new: full names, colors, canonicalization helpers
- `app/orchestration.py` — team context propagation to image pipeline
- `app/adapters.py` — adapter additions
- `app/schemas.py` — schema additions
- `app/config.py` — config additions
- `app/editorial/helpers.py` — fingerprint, dedup-plan, URL overlap, resolve-existing-article-ids
- `app/editorial/tools.py` — orchestrator tool improvements
- `app/editorial/workflow.py` — workflow improvements
- `app/editorial/prompts.yml` — prompt updates
- `run_12h_test.sh` — minor update
- `tests/conftest.py` — fixture updates
- `tests/test_config.py` — config test additions
- `tests/test_helpers.py` — helper test additions
- `tests/test_image_selector.py` — new: image cascade tests
- `tests/test_personas.py` — new: persona tests
- `tests/test_player_enrichment.py` — new: player enrichment tests
- `tests/test_team_codes.py` — new: team codes tests
- `supabase/migrations/002_add_source_urls.sql` — new migration (not yet applied)
- `supabase/migrations/003_add_author.sql` — new migration (not yet applied)
- `supabase/migrations/004_add_mentioned_players.sql` — new migration (not yet applied)
- `scripts/audit_team_codes.py` — new utility script
- `scripts/build_articles_view.py` — new utility script
- `scripts/build_quality_report.py` — new utility script
- `scripts/rerun_image_cascade.py` — new utility script

## Code Quality Notes
- Tests: 102 passed, 0 failed, 0 skipped (0.30s)
- No linting step configured for Python backend
- No TODOs, FIXMEs, HAKCs, or debug print statements found in any changed file
- End-to-end cycle ran cleanly: 3 articles written, validator correctly caught a wrong-team Wikimedia hit (USC photo for a Georgia arrest story)

## Open Items / Carry-over
- Supabase migrations 002–004 have not been applied yet — apply via SQL Editor before next production cycle
- No `npm run lint` for UI (no UI files changed today)
- `scripts/` utilities are one-off/debug tools; no tests added for them yet
