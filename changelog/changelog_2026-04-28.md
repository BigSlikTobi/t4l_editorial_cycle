# Changelog — 2026-04-28

## Summary
Comprehensive prompt engineering review and rewrite across all editorial and writer agents, focusing on fixing generic/samey article output, correcting a missing DE quality-gate rewrite block, and improving editorial memory coverage by feeding it positive examples alongside rejections.

## Changes

### Prompt rewrites — editorial agents (`app/editorial/prompts.yml`)
- Rewrote `article_data_agent` prompt: tightened roundup/ranking extraction rules and thin-content routing
- Rewrote `story_cluster_agent` prompt: sharpened cluster-boundary and entity-matching instructions
- Rewrote `editorial_orchestrator_agent` prompt: cleaner ranking rationale requirements

### Prompt rewrites — writer agents (`app/writer/prompts.yml`)
- `article_writer_agent` (EN): made persona-load-bearing — added per-persona structural targets (word count, paragraph length, intro shape), opening examples, and angle defaults. Structural rules now live in the prompt; voice/character in the persona's `style_guide`. Intended to fix the "all articles feel generic and similar" problem.
- `article_writer_agent_de` (DE): mirrored the EN rewrite. Added the missing `quality_gate_feedback` / rewrite handling block (was absent entirely — bug fix). Reframed DE voice toward modern DACH NFL voices (ran.de, Detail Football, Footballerei) instead of dry agentur tone.
- `persona_selector_agent`: updated persona descriptions to match new character-sketch style guides
- `article_quality_gate_agent`: added score calibration anchors and explicit hard-fail triggers
- `editorial_memory_agent`: restructured wiki schema to capture both "what works" and "what to avoid" sections

### Persona character sketches (`app/writer/personas.py`)
- Rewrote all 6 style guides (3 EN + 3 DE) from rule lists into evocative first-person character sketches
- Structural rules (word count, paragraph length) moved to prompts; personas now own voice/character only

### Sampled clean-approve feedback to editorial memory (`app/writer/workflow.py`)
- Added 1-in-5 sampled clean-approve feedback path: when a story is approved on the first attempt, there is now a 20% chance (deterministic by fingerprint hash) that a positive feedback event is written to editorial memory
- Added `hashlib` import
- Ensures the memory wiki accumulates "what works" lessons, not only rejection patterns

### Image validator prompt tightening (`app/writer/image_validator.py`)
- Condensed Gate 1 (topic relevance) and Gate 2 (rejection categories) to be more concise and scannable
- Added telemetry-parseable rejection prefixes: `off-topic:`, `wrong-sport:`, `wrong-team:`, `portrait:`, `dated:`, `low-quality:` — makes automated rejection reason analysis tractable without regex

## Files Modified
- `app/editorial/prompts.yml` — rewrote article_data_agent, story_cluster_agent, editorial_orchestrator_agent prompts
- `app/writer/prompts.yml` — rewrote article_writer_agent (EN+DE), persona_selector_agent, article_quality_gate_agent, editorial_memory_agent prompts; added missing DE quality-gate rewrite block
- `app/writer/personas.py` — rewrote all 6 persona style_guide strings as character sketches; removed structural rules now owned by prompts
- `app/writer/workflow.py` — added 1-in-5 sampled clean-approve feedback to editorial memory; added `hashlib` import
- `app/writer/image_validator.py` — tightened vision prompt text; added telemetry rejection prefixes

## Code Quality Notes
- Tests: 191 passed, 0 failed, 0 skipped (./venv/bin/pytest tests/ -v)
- Linting: no lint command configured for Python backend; no TODO/FIXME/debug artifacts found in changed files
- No syntax errors or import issues detected
- `image_validator.py` diff is prompt-text-only — no logic changes, no behavioral regression risk

## Open Items / Carry-over
- Migration `007_add_story_fingerprint.sql` still pending in Supabase SQL Editor
- `curated_pool_spec.py` changes reviewed but deferred (out of editorial-cycle path)
- RLS grant `GRANT SELECT ON content.team_article TO anon;` still pending for frontend edge functions
- Consider adding automated tests for the 1-in-5 sampling logic in `workflow.py` if the ratio is ever made configurable
