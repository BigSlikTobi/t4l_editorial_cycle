# Changelog — 2026-04-24 (v2)

## Summary
Resolved issue #12: service secrets (OpenAI API key, Supabase service role key) were being sent inside cloud-function job payloads. All three knowledge-extraction cloud functions now authenticate callers via a bearer token read from the environment, and the client side sends that token in an `Authorization` header instead of embedding secrets in request bodies.

## Changes

### `t4l_editorial_cycle` — client-side (branch: `issue-12/remove-service-secrets-from-payloads`)
- **`SupabaseJobsConfig` no longer carries `key`** — submit and poll request bodies are now secret-free
- **`KnowledgeExtractionClient` no longer sends `api_key` in the payload** — was leaking the OpenAI key to the cloud function over HTTPS in plain JSON
- **`AsyncJobClient` sends `Authorization: Bearer $EXTRACTION_FUNCTION_AUTH_TOKEN`** on every submit and poll call
- **New `extraction_function_auth_token: SecretStr | None` Settings field** — ingestion worker fails fast at startup if the token is unset
- **`.github/workflows/ingestion-worker.yml`** — threads the new `EXTRACTION_FUNCTION_AUTH_TOKEN` GitHub secret through to the runner environment
- **New test file `tests/test_clients_knowledge.py`** — 9 tests asserting payload safety: no `api_key` in body, `Authorization` header present, correct bearer value
- **Updated `tests/test_clients_base.py`** — extended coverage of `AsyncJobClient` auth header injection (+68 lines)
- **Updated `tests/test_clients_news.py` and `tests/test_ingestion_worker.py`** — minor fixture alignment
- **New `docs/rotating-extraction-function-auth-token.md`** — step-by-step rotation runbook for the shared bearer token

### `tackle_4_loss_intelligence` — server-side (branch: `issue-12/read-service-secrets-from-env`)
- All three `functions/main.py` now require the bearer token via `_check_caller_auth()`
- `_parse_supabase()` and `_parse_llm()` read credentials from Cloud Run env vars instead of accepting them from the caller
- `deploy.sh` threads `EXTRACTION_FUNCTION_AUTH_TOKEN`, `SUPABASE_SERVICE_ROLE_KEY`, and `OPENAI_API_KEY` through as Cloud Run env vars
- Already deployed live on Cloud Run (9 services updated)

### Operational changes applied
- GitHub secret `EXTRACTION_FUNCTION_AUTH_TOKEN` set on `t4l_editorial_cycle` repo
- Cloud Run env vars set on 9 services: `EXTRACTION_FUNCTION_AUTH_TOKEN` on 6 (submit/poll pairs), `SUPABASE_SERVICE_ROLE_KEY` on all 9, `OPENAI_API_KEY` on knowledge-submit and knowledge-worker

## Files Modified

### `t4l_editorial_cycle`
- `app/clients/base.py` — `AsyncJobClient` adds `Authorization` header; `SupabaseJobsConfig` drops `key`
- `app/clients/knowledge_extraction.py` — `KnowledgeExtractionClient` removes `api_key` from payload
- `app/clients/news_extraction.py` — fixture alignment
- `app/clients/url_content.py` — fixture alignment
- `app/config.py` — new `extraction_function_auth_token: SecretStr | None` field
- `app/ingestion/worker.py` — fail-fast guard if `extraction_function_auth_token` is unset
- `.github/workflows/ingestion-worker.yml` — passes new secret to runner
- `docs/rotating-extraction-function-auth-token.md` — new rotation runbook
- `tests/test_clients_base.py` — extended `AsyncJobClient` auth header tests
- `tests/test_clients_knowledge.py` — new file, 9 tests
- `tests/test_clients_news.py` — minor fixture update
- `tests/test_ingestion_worker.py` — minor fixture update

## Code Quality Notes
- Tests: **155 passed, 0 failed, 0 skipped** (full suite on `main`; feature branch confirmed 155 green locally)
- GitHub Actions ingestion-worker workflow ran green end-to-end against the feature branch
- No debug artifacts, TODO/FIXME markers, or commented-out blocks in changed files
- No linting step configured for the Python backend

## Open Items / Carry-over
- **PR review pending** — neither PR is merged yet; both branches pushed and tested green
  - `t4l_editorial_cycle`: https://github.com/BigSlikTobi/t4l_editorial_cycle/pull/new/issue-12/remove-service-secrets-from-payloads
  - `tackle_4_loss_intelligence`: https://github.com/BigSlikTobi/tackle_4_loss_intelligence/pull/new/issue-12/read-service-secrets-from-env
- Migration `007_add_story_fingerprint.sql` remains **pending** (pre-existing carry-over)
- Untracked files `scripts/architecture_graph_manual.yml`, `scripts/build_architecture_graph.py`, `tests/test_architecture_graph.py` are intentionally excluded from commits
