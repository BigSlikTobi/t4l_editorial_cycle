# v2 Migrations — Clean Supabase Project

These migrations define the full schema for the new, standalone Supabase
project that replaces the legacy setup. Apply in order against an empty
project (SQL Editor; `supabase db push` is not used).

## Apply order

1. `001_extraction_jobs.sql` — shared job queue for the three GCF services.
2. `002_reference_data.sql` — **no DDL**; placeholder that documents the
   dependency on the `data_loading` cloud function. Run its CLI scripts
   **after** this migration to create and fill `public.players`,
   `public.teams`, `public.games`, etc.
3. `003_raw_articles.sql` — owned ingestion store (URL → content).
4. `004_article_entities.sql` — resolved entity mentions per article.
5. `005_article_topics.sql` — topics per article.
6. `006_editorial_state.sql` — cross-cycle dedup memory.
7. `007_team_article.sql` — published article output.
8. `008_curated_images.sql` — tier-3 image pool.
9. `009_article_images.sql` — image provenance.

All tables live in `public`. No PostgREST schema exposure toggle needed.

## Reference data loader

Run once (and thereafter on a cadence) against this project:

```bash
cd tackle_4_loss_intelligence/src/functions/data_loading
SUPABASE_URL=... SUPABASE_KEY=... python scripts/players_cli.py
SUPABASE_URL=... SUPABASE_KEY=... python scripts/games_cli.py --season 2026
```

The image selector tier 2 requires `public.players.headshot` and
`public.players.display_name`.

## Curated image pool

The curated image bucket + rows need to be copied from the legacy project:

1. Create a Storage bucket (default name `images`) in the new project.
2. Export `content.curated_images` rows from legacy and insert into new.
3. Mirror the storage objects (see legacy `scripts/upload_curated_pool.py`
   for upload helpers).
