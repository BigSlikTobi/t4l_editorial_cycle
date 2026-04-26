# Changelog — 2026-04-26 (v2)

## Summary
Added two Supabase Edge Functions — `get-articles` and `get-article-detail` — that expose `content.team_article` records to the Flutter frontend. Both functions are deployed and verified reachable; `get-articles` returns 200 with empty results (RLS grant for the anon role on `content.team_article` is still pending).

## Changes

### New: `supabase/config.toml`
- Supabase CLI project config (`project_id = aiknjzinyxzhoseyxqev`).
- `verify_jwt = true` for both new functions, requiring a valid JWT from callers.

### New: `supabase/functions/_shared/cors.ts`
- Shared CORS headers (`Access-Control-Allow-Origin: *`) and two helpers: `jsonResponse()` (wraps body as JSON with CORS headers) and `preflight()` (handles OPTIONS, returns 204).

### New: `supabase/functions/_shared/supabase.ts`
- `clientFromRequest(req)` — reads `SUPABASE_URL` + `SUPABASE_ANON_KEY` from env, creates a `SupabaseClient` with the caller's `Authorization` header forwarded, `persistSession: false`.

### New: `supabase/functions/get-articles/` (`index.ts`, `deno.json`)
- POST endpoint for paginated article listing.
- Request body: `{ language?: string, limit?: number, cursor?: { created_at, id } | null }`.
- Defaults: `language = "en-US"`, `limit = 20` (clamped 1–50). Validates that `language` is one of `en-US` or `de-DE`.
- Cursor pagination on `(created_at DESC, id DESC)`. Cursor condition: `created_at < ts OR (created_at = ts AND id < cursor_id)`.
- Response: `{ items: [...], next_cursor: { created_at, id } | null }`. `next_cursor` is `null` when the page is shorter than `limit`.
- Returns only list-safe columns (`id`, `headline`, `sub_headline`, `introduction`, `image`, `team`, `language`, `author`, `created_at`, `updated_at`) — no full article body in list view.

### New: `supabase/functions/get-article-detail/` (`index.ts`, `deno.json`)
- POST endpoint for single article fetch with player enrichment.
- Request body: `{ id: number }`.
- Fetches full `team_article` row via `.maybeSingle()`, returns 404 if not found.
- Enriches `mentioned_players`: resolves each player ID against `public.players` (`player_id`, `display_name`, `headshot`) and replaces the raw ID array with a structured array of `{ player_id, display_name, headshot }` objects. Falls back to `display_name: player_id` if the player row is missing.

## Files Modified
- `supabase/config.toml` — new (CLI project config + function JWT settings)
- `supabase/functions/_shared/cors.ts` — new (shared CORS + response helpers)
- `supabase/functions/_shared/supabase.ts` — new (shared Supabase client factory)
- `supabase/functions/get-articles/index.ts` — new (paginated article list function)
- `supabase/functions/get-articles/deno.json` — new (Deno import map)
- `supabase/functions/get-article-detail/index.ts` — new (article detail + player enrichment function)
- `supabase/functions/get-article-detail/deno.json` — new (Deno import map)

## Code Quality Notes
- Tests: **155 passed, 0 failed** — no Python changes; existing test suite unaffected.
- Linting: not applicable — no UI or Python files changed. TypeScript files are Deno edge functions (no local lint toolchain configured).
- No debug statements, TODO comments, or FIXME markers in the new TypeScript files.
- `get-articles` does not expose `body`, `bullets`, `x_post`, or `sources` in list view — correct for bandwidth reasons.
- `verify_jwt = true` in `config.toml` means both functions require a valid Supabase JWT. The Flutter app must pass its anon JWT in the `Authorization: Bearer <token>` header.

## Open Items / Carry-over
- **RLS not configured for anon role on `content.team_article`** — `get-articles` returns 200 with empty `items` because the anon JWT has no SELECT grant on the table. Next step: add `GRANT SELECT ON content.team_article TO anon;` (or an RLS policy) in Supabase SQL Editor.
- Migration 007 (`story_fingerprint text` on `content.team_article`) still **pending**.
- `scripts/architecture_graph_manual.yml`, `scripts/build_architecture_graph.py`, and `tests/test_architecture_graph.py` remain untracked from a prior session — not part of today's work; carry forward.
- "Source-as-story" prompt guardrails from earlier today have not yet been exercised against a live roundup source in production. Confirm with a future cycle.
