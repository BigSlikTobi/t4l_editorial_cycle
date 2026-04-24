# Changelog — 2026-04-24

## Summary
Diagnosed and fixed a production crash in the ingestion worker where PostgREST returned SQLSTATE 21000 on upserts to `article_entities` and `article_topics`. The knowledge-extraction LLM can emit duplicate entities or topics for the same article within a single batch; the fix deduplicates rows client-side before the POST so every conflict-target key is unique within the statement.

## Changes
- **fix: deduplicate entities and topics in `update_knowledge` before upsert**
  - `article_entities` deduplicated by `(article_id, entity_type, entity_id)` — the table PK
  - `article_topics` deduplicated by `(article_id, topic)` — the table PK
  - On collision within a batch, the row with the higher `confidence` is kept (`None` treated as 0)
  - Prevents PostgREST SQLSTATE 21000 "ON CONFLICT DO UPDATE command cannot affect row a second time"
- **test: add `test_update_knowledge_dedupes_entities_and_topics`**
  - Covers the entity-dedup path (two rows for the same player, different confidence — higher wins)
  - Covers the topic-dedup path (two rows for the same topic, different confidence — higher wins)
  - Uses the existing `httpx.MockTransport` pattern consistent with the rest of the test module

## Files Modified
- `app/ingestion/store.py` — `update_knowledge()` now builds `entity_rows_by_key` and `topic_rows_by_key` dicts keyed on the respective PKs, keeping the highest-confidence row per key before POSTing
- `tests/test_ingestion_store.py` — added `test_update_knowledge_dedupes_entities_and_topics` (7 → 7 passing; new test is item 6 of 7)

## Code Quality Notes
- Tests: 150 passed, 0 failed, 0 skipped (full suite)
- No debug artifacts, TODO/FIXME markers, or commented-out blocks found in changed files
- No linting step configured for the Python backend (no `ruff`/`flake8` entry in `pyproject.toml` scripts)

## Open Items / Carry-over
- Migration `007_add_story_fingerprint.sql` remains **pending** (noted in CLAUDE.md — pre-existing carry-over)
- The untracked files `scripts/architecture_graph_manual.yml`, `scripts/build_architecture_graph.py`, and `tests/test_architecture_graph.py` are not part of today's fix; they were excluded from this commit intentionally
