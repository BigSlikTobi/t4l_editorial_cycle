# Rotating `EXTRACTION_FUNCTION_AUTH_TOKEN`

The shared bearer token that authenticates the editorial-cycle ingestion
client against the six extraction Cloud Run services (news / url_content /
article_knowledge × submit + poll). Rotate on suspected leak, departing
contributors with historical access, or on a scheduled cadence.

Not needed when rotating `SUPABASE_SERVICE_ROLE_KEY` or `OPENAI_API_KEY` —
those live in separate places and don't involve this token.

## Where the token lives

Five locations, all must hold the same value:

1. Local `.env` (editorial-cycle repo) — for `editorial-cycle run` locally.
2. GitHub Actions secret `EXTRACTION_FUNCTION_AUTH_TOKEN` on
   `BigSlikTobi/t4l_editorial_cycle` — for the scheduled `ingestion-worker`
   workflow.
3. Cloud Run env var on `news-extraction-submit`.
4. Cloud Run env var on `news-extraction-poll`.
5. Cloud Run env var on `url-content-submit`.
6. Cloud Run env var on `url-content-poll`.
7. Cloud Run env var on `article-knowledge-submit`.
8. Cloud Run env var on `article-knowledge-poll`.

Workers (`*-worker`) do NOT hold this token — they're gated by
`WORKER_TOKEN`, a separate internal secret.

## Rotation procedure

Do it in one sitting. Between step 2 and step 3 there is a window where
old callers (anything using the previous token) will 401; between step 3
and step 4 the ingestion workflow will 401 until you push.

```bash
# 1. Generate the new token
NEW_TOKEN=$(openssl rand -hex 32)
echo "$NEW_TOKEN"   # stash in a password manager before continuing

# 2. Update all six Cloud Run services
for FN in news-extraction-submit news-extraction-poll \
          url-content-submit url-content-poll \
          article-knowledge-submit article-knowledge-poll
do
  gcloud run services update "$FN" --region=us-central1 \
    --update-env-vars EXTRACTION_FUNCTION_AUTH_TOKEN="$NEW_TOKEN"
done

# 3. Update local .env (editor of your choice — do NOT commit the file)
$EDITOR ~/Projects/github/bigsliktobi/T4L/t4l_editorial_cycle/.env

# 4. Update the GitHub secret
printf '%s' "$NEW_TOKEN" | gh secret set EXTRACTION_FUNCTION_AUTH_TOKEN \
  --repo BigSlikTobi/t4l_editorial_cycle
```

## Verify

```bash
cd ~/Projects/github/bigsliktobi/T4L/t4l_editorial_cycle

# Local side (reads from .env)
./venv/bin/ingestion-worker

# CI side (reads from GitHub secret)
gh workflow run ingestion-worker.yml
gh run watch
```

Both should complete without a 401. If only one side works, the values
drifted — re-check whichever side fails.

## Rolling back

If a rotation goes wrong and production calls are 401'ing, the fastest
recovery is to put the previous token back on the six services and on
whichever client side moved first. Keep the previous value in your
password manager for at least a day after rotation in case you need
this escape hatch.
