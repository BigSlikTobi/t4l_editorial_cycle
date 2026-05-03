# T4L Team Beat — MVP Definition

**Generated:** 2026-05-02
**Version:** 1.0
**Scope:** Phase 1, two teams end-to-end (NYJ + CHI), including audio.

---

## 1. Product Vision

A twice-daily automated NFL team beat that files bilingual written dispatches and DE-only audio drops for the **New York Jets** and **Chicago Bears** — filing only when news moves the needle, silent when it does not. Each dispatch is written in the voice of a fictional, fantasy-named team insider with a byline; the audio is read by one consistent studio-anchor host who relays the insider's filing.

---

## 2. Target User

DACH NFL fans who follow one or two teams closely and want a quick, intelligent German-language audio catch-up twice a day without scrolling a feed.

---

## 3. Problem & Hypothesis

NFL team news is fragmented, English-dominant, and always-on. DACH fans with a specific team loyalty have no low-friction, German-language way to stay current. A twice-daily drop from a named insider persona (written + audio) is a fundamentally different consumption pattern than a feed reader. We believe a >60% audio play-through rate validates the format.

---

## 4. Success Metrics

- **Primary:** audio play-through rate on DE drops (target ≥60% completion in pilot).
- **Secondary:** cycle reliability — every run yields either a filed roundup or a structured `no_news` record. Target: zero silent failures across 14 consecutive days.
- **Tertiary:** quality-gate pass rate on first draft of the beat-reporter output (baseline measurement only — no target at MVP).

---

## 5. KANO Feature Analysis

### Must-Haves

| # | Feature | Description |
|---|---------|-------------|
| 1 | Team-filtered feed ingestion | Post-fetch filter routes 12h-lookback raw articles to NYJ or CHI before the agent runs. Reuses `RawArticleDbReader` from `app/adapters.py`. |
| 2 | Team Beat Reporter Agent | Per-team agent. Reads filtered articles, decides whether to file, writes EN+DE brief in the team's insider persona voice. |
| 3 | Studio-anchor persona sketch | One named, committed character for DE radio framing ("[Anchor name] berichtet aus Chicago, hier ist was er hört…"). **Blocking creative decision** before any prompt YAML is written. |
| 4 | Radio Script Agent (DE only) | Converts the DE brief to a 90–120 s radio script in studio-anchor framing. Plain text with inline Gemini audio tags (see §6). No SSML. |
| 5 | Batch TTS step | Submits radio scripts to the `gemini_tts_batch_service` Cloud Run endpoints (see §7). Returns public MP3 URLs. |
| 6 | `public.team_roundup` table + `BeatRoundupWriter` | New table in the `public` schema (the new Supabase project does not provision the legacy `content` schema). Columns: `id`, `team_code`, `cycle_ts`, `persona_name`, `en_body`, `de_body`, `radio_script`, `audio_url`, `created_at`. New write adapter modeled on `ArticleWriter`, hits `/rest/v1/team_roundup` with no Content-Profile header (PostgREST routes to public by default). |
| 7 | `no_news` early exit | Clean exit path when the beat reporter judges nothing in the 12h window is editorially worthwhile. Logs `(team_code, cycle_ts, outcome="no_news", reason)`. No row written, no TTS call. Expected and frequent — never an error. |
| 8 | GitHub Actions cron at 04:00 + 16:00 UTC | New workflow `team-beat.yml`. Maps to 06:00 + 18:00 Berlin winter time. DST drift (60 min) is accepted. |
| 9 | `app/team_beat/` module with clean boundary | New module parallel to `app/editorial/` and `app/writer/`. Imports `app/adapters.py`, `app/team_codes.py`, `app/clients/base.py`. **No imports** from `app/editorial/` or `app/writer/` internals. This boundary is what makes the 32-team expansion a configuration change, not surgery. |
| 10 | Beat cycle state tracking | Structured per-run record: `(team_code, cycle_ts, outcome ∈ {filed, no_news, error})`. Lightweight table or structured log — only way to distinguish silent-failure from no-news at runtime. |

### Performance Indicators (included)

| Feature | Effort | Why included |
|---------|--------|--------------|
| Reuse existing `build_article_quality_gate_agent` on beat-reporter output before the radio-script step | Low | Beat reporter agent is brand-new and untested; gate cost is near-zero given existing infrastructure. Catches bad output before it consumes a TTS call. |
| Per-cycle batching of both teams in one TTS `create` call | Low | When both NYJ and CHI file in the same cycle, both scripts go in the same batch — fewer round trips, simpler manifest mapping. |

### Performance Indicators (deferred)

| Feature | Why deferred |
|---------|--------------|
| Per-team persona locking across cycles | At 2 teams it's trivially managed by convention. Becomes a Must-Have at 10+ teams. |
| DST-precise Berlin-time cron | 60-min drift is invisible at pilot scale. Revisit on first observed transition in production. |
| TTS retry logic on transient batch-service failures | Failure rate unknown. Log first, retry decorator after first incident. The async-job poller already handles transient submit/poll failures. |
| TTS audio quality gate | 4 calls/day max. Brief-level gate already catches upstream issues. |

### Delighter

| Feature | Effort | Why |
|---------|--------|-----|
| **Dateline byline stamp on the written brief** | Low (one prompt template change + one schema field — reuse `author` or add `dateline`) | Mirrors the audio's studio-anchor framing on the written surface. Creates "filed by [Persona], covering the [Team], [City]" wire-style register that makes the brief feel like a dispatch, not a summary. Drives the parasocial anchor that lifts return engagement. |

### Reverse features (do not build)

| Feature | Reason |
|---------|--------|
| EN audio | Doubles TTS cost and production complexity for unvalidated demand. DE-only is a deliberate editorial stance for a DACH-first product. |
| Per-cycle audio archiving / retention | Storage accumulates silently. Upsert-overwrite per cycle path is correct. Add archive only after replay demand is validated. |
| Frontend edge function for beat roundups | Premature API contract. Validate via Supabase dashboard, then design the edge function from observed query patterns. |
| Quality gate on radio script / generated audio | 4 TTS calls/day. Brief-level gate catches upstream quality. Adds cost for no measurable lift at this scale. |
| Multi-team expansion before pilot validation | Multiplies failure surface without multiplying learning. |
| Direct calls to the TTS worker URL | Worker is internal; the editorial repo only ever talks to submit + poll. |

---

## 6. TTS Approach (Gemini natural-language prosody)

Gemini TTS does **not** use SSML. Per https://ai.google.dev/gemini-api/docs/speech-generation it controls emotion, pacing, and prosody via two natural-language mechanisms that the Radio Script Agent must emit directly:

**Director's-notes preamble** — the script opens with a short stage direction the model interprets as voice guidance. Example:

> *Style: ein erfahrener DACH-Sportanker, ruhig, vertraut, keine Hektik. Pacing: gemessen, mit Atempausen vor Pointen. Tone: warm, leicht trocken, gelegentlich amüsiert.*

**Inline audio tags** — bracketed cues placed mid-script to shape delivery line by line. The Radio Script Agent prompt mandates the following set:

`[whispers]` `[excited]` `[sigh]` `[laughs]` `[pause]` `[matter-of-fact]` `[skeptical]` `[disappointed]` `[deadpan]`

Tags ride the prose; they do not replace it. They are placed sparingly (no more than one per ~25 words) and only where the editorial beat warrants the shift in delivery.

**Model:** `gemini-3.1-flash-tts-preview` (lowest-cost Gemini TTS model).
**Voice:** pinned via `Settings.tts_voice_name` for cross-cycle consistency. Default: `Kore` (final voice pick deferred to MVP setup; candidates: `Kore`, `Puck`, `Charon`, `Zephyr`).
**Language:** German via natural prompt — Gemini auto-detects from the script content.

---

## 7. Batch TTS Delivery

Audio production is owned by the sibling `gemini_tts_batch_service` (Cloud Run) — see `tackle_4_loss_intelligence/src/functions/gemini_tts_batch_service/README.md` for the source of truth. The editorial repo consumes it via the documented submit/poll protocol; cost control lives in the batch service, not in this repo.

**Endpoints:**

| Action | URL | Used by editorial repo |
|--------|-----|------------------------|
| Submit | `https://tts-batch-submit-hjm4dt4a5q-uc.a.run.app` | yes |
| Poll   | `https://tts-batch-poll-hjm4dt4a5q-uc.a.run.app`   | yes |
| Worker | `https://tts-batch-worker-hjm4dt4a5q-uc.a.run.app` | **no — internal** |

**Auth:** bearer token `TTS_BATCH_FUNCTION_AUTH_TOKEN` on every submit + poll request. Added to `.env` and to GitHub Actions secrets. `WORKER_TOKEN` is internal worker-to-worker and the editorial repo does not see it.

**Three-stage lifecycle per cycle.** The `app/team_beat/tts_client.py` wrapper orchestrates all three stages, each via the existing `AsyncJobClient` submit/poll wrapper from `app/clients/base.py`.

1. **`action=create`** — submit the batch with model + voice + an `items` array. When both NYJ and CHI file in the same cycle, both radio scripts are sent in **one** create call.

   ```json
   {
     "action": "create",
     "model_name": "gemini-3.1-flash-tts-preview",
     "voice_name": "Kore",
     "items": [
       {"id": "NYJ-2026-05-02T06:00:00Z", "text": "<radio script>", "title": "NYJ AM"},
       {"id": "CHI-2026-05-02T06:00:00Z", "text": "<radio script>", "title": "CHI AM"}
     ],
     "supabase": {"url": "<jobs-db-url>"}
   }
   ```
   Returns `batch_id`.

2. **`action=status`** — poll the Gemini batch state until `JOB_STATE_SUCCEEDED`. Loop with sleep between polls (reuse `extraction_poll_interval_seconds` semantics from existing async-job clients).

3. **`action=process`** — caller passes the storage destination. Service downloads the batch output and uploads MP3s to that location, returning a manifest with public URLs per item id.

   ```json
   {
     "action": "process",
     "batch_id": "<batch_id>",
     "storage": {"bucket": "team-beat-audio", "path_prefix": "gemini-tts-batch/2026-05-02_AM"},
     "supabase": {"url": "<jobs-db-url>"}
   }
   ```

**Item id convention:** `{team_code}-{cycle_iso_ts}` so the manifest result maps back to teams unambiguously.

**Storage:** caller-chosen bucket `team-beat-audio`; path prefix `gemini-tts-batch/{cycle_date}_{slot}` (e.g. `gemini-tts-batch/2026-05-02_AM`). Service handles the upload — **no separate `ImageUploader`-style step is needed**, simplifying the editorial-side workflow vs. the prior MVP draft. The returned MP3 public URL is stored in `public.team_roundup.audio_url`.

**Lifecycle:** upsert-overwrite per cycle path. No archive, no TTL. 4 files/day × 2 teams = 8 files/day worst case; storage is trivially small.

**New `Settings` fields** (in `app/config.py`):

| Field | Type | Default |
|-------|------|---------|
| `tts_batch_submit_url` | `AnyHttpUrl` | the submit URL above |
| `tts_batch_poll_url` | `AnyHttpUrl` | the poll URL above |
| `tts_batch_function_auth_token` | `SecretStr` | none — required at runtime |
| `tts_model_name` | `str` | `gemini-3.1-flash-tts-preview` |
| `tts_voice_name` | `str` | `Kore` |
| `tts_storage_bucket` | `str` | `team-beat-audio` |

---

## 8. Architecture Sketch

```
app/team_beat/
  __init__.py
  workflow.py        # cycle entrypoint: feed → per-team agents → TTS → write
  schemas.py         # BeatRoundup, BeatCycleResult, BeatItem
  personas.py        # 2 team personas (NYJ, CHI) + 1 studio anchor
  prompts.yml        # team_beat_reporter_agent (EN+DE), radio_script_agent (DE)
  prompts.py         # @lru_cache load_prompts() + get_prompt()
  agents.py          # build_team_beat_reporter_agent(), build_radio_script_agent()
  tts_client.py      # 3-stage create→status→process orchestrator over AsyncJobClient
```

**Storage:**
- New `public.team_roundup` table (migration: `008_team_roundup.sql`). The new Supabase project does not provision the legacy `content` schema, so all team-beat tables live in `public`.
- New Supabase Storage bucket `team-beat-audio` (created out-of-band, GRANTs as needed).

**Run:** new Typer subcommand `editorial-cycle beat` (in `app/cli.py`) mirroring `run_cycle`. Workflow accepts `--teams NYJ,CHI` for explicit control during local runs.

---

## 9. Reused Primitives

| Need | File | What's reused |
|------|------|---------------|
| 12h raw-feed read | `app/adapters.py` | `RawArticleDbReader.fetch_raw_articles(lookback_hours=12)` |
| Persona dataclass + bilingual lookup | `app/writer/personas.py` | `Persona`, `get_persona(id, language)` patterns |
| YAML prompt loading | `app/writer/prompts.py` | `@lru_cache load_prompts()` + `get_prompt()` pattern |
| Agent factories (OpenAI Agents SDK) | `app/writer/agents.py` | `Agent(name=..., instructions=get_prompt(...), model=settings.agent_model(...), output_type=...)` pattern |
| Quality gate | `app/writer/agents.py` | `build_article_quality_gate_agent(settings)` reused as-is on beat-reporter output |
| Workflow assembly | `app/writer/workflow.py` | Per-team parallel execution pattern |
| Async job submit/poll | `app/clients/base.py` | `AsyncJobClient` — composed three times (one per TTS action) |
| Supabase write adapter | `app/adapters.py` | `ArticleWriter` is the model for `BeatRoundupWriter` (service role + `Content-Profile: content` for writes) |
| Cross-cycle state | `app/adapters.py` | `EditorialStateStore` patterns inform the beat cycle state tracker shape |
| Team metadata | `app/team_codes.py` | `team_full_name`, `team_colors`, `normalize_team_codes` for prompt injection |
| Settings + per-agent model overrides | `app/config.py` | `Settings`, `agent_model(name)`, plus 6 new TTS fields (§7) |
| CLI entrypoint | `app/cli.py` | Typer subcommand pattern; new `beat` command mirrors `run_cycle` |
| GitHub Actions cron | `.github/workflows/editorial-cycle.yml` | New `team-beat.yml` cron `0 4,16 * * *` UTC; adds `TTS_BATCH_FUNCTION_AUTH_TOKEN` secret |
| Test conventions | `tests/` | pytest + `pytest-asyncio` + `httpx.MockTransport`; `Settings(_env_file=None)` + `monkeypatch.delenv` for hygiene |
| TTS contract source of truth | `tackle_4_loss_intelligence/src/functions/gemini_tts_batch_service/README.md` | Read-only reference; never imported |

---

## 10. Development Phases

**Phase 1 — Foundation (days 1–2)**
- Studio-anchor persona sketch (creative decision, blocking).
- 2 team persona sketches (NYJ, CHI insider voices) — fantasy names, archetypes (former player / former coach / front-office lifer), signature traits.
- `008_team_roundup.sql` migration + `BeatRoundupWriter` adapter.
- Beat cycle state tracking table or structured log.
- `app/team_beat/` module skeleton, team-filtered feed read, `no_news` exit path wired and logged.

**Phase 2 — Core value (days 3–5)**
- `team_beat_reporter_agent` (EN+DE) + prompt YAML.
- Quality gate wiring on the beat brief.
- `radio_script_agent` (DE) + prompt YAML, including the director's-notes preamble convention and the audio-tag set (§6).
- `tts_client.py` orchestrator: `create → status (loop) → process` over the three Cloud Run endpoints.
- `BeatRoundupWriter` write of the full row including `audio_url` from the manifest.

**Phase 3 — Differentiation + CI (days 6–8)**
- Dateline byline stamp on the written brief (Delighter).
- New `team-beat.yml` GitHub Actions workflow with cron + `TTS_BATCH_FUNCTION_AUTH_TOKEN` secret.
- Integration test: manual `editorial-cycle beat --teams NYJ,CHI` against the live batch service.
- Audit `article_entities` coverage for NYJ + CHI against 48h of historical feed data — confirm we're not silently starved of input.
- 14-day live monitoring window; collect first audio play-through samples.

**Estimated effort:** 5–8 focused days for a developer familiar with the existing codebase.

---

## 11. Confirmed Open-Question Calls

| Question | Call |
|----------|------|
| DST handling for Berlin-time cron | Hard-code UTC (`0 4,16 * * *`). Accept 60-min drift through DST transitions. |
| SSML support | Not applicable. Use Gemini's natural-language director's-notes preamble + inline audio tags (§6). |
| Audio file lifecycle | TTS service writes to `team-beat-audio` bucket; per-cycle path is upsert-overwritten. No archive, no TTL. |
| Studio-anchor character | **Blocking creative decision** before Phase 2 begins. One named character, consistent DACH football voice. |
| TTS budget | Owned by the batch service. The editorial repo enforces no ceiling and tracks no per-cycle cost. MVP submits both teams in one `create` call when both have filed. |
| TTS model id | `gemini-3.1-flash-tts-preview` |
| TTS voice | Pinned via `Settings.tts_voice_name` (default `Kore`); final pick made at MVP setup. |

---

## 12. Validation Hypothesis

**We believe** DACH NFL fans who follow the Jets or Bears **will listen through the full DE audio drop at a rate above 60%** **because** a twice-daily, team-specific, persona-anchored audio dispatch in German fills a gap no existing product fills.

**We'll know we're right when** the first 50 audio plays per team show ≥60% completion, with no forced cadence (every filed drop represents a genuine news event).

**We'll know the pilot is ready to scale beyond 2 teams when** the loop runs 14 consecutive days with zero silent failures (every run produces either `filed` or `no_news`), the play-through target is met, and the entity-coverage audit shows no systematic gaps in raw-feed coverage for either team.

---

## 13. Out of Scope

- EN audio
- Per-cycle audio archiving / retention policies
- Frontend edge function for beat roundups
- Per-persona TTS voice variation
- Live or streaming audio
- Modifications to the existing top-N editorial cycle
- Modifications to the sibling `gemini_tts_batch_service` repo
- Direct calls to the TTS batch worker URL (internal only)
- Comments, ratings, or user interaction on roundup content
- SSML markup (Gemini does not use it; see §6)
- Expansion beyond NYJ + CHI in this MVP

---

## Key technical risks

- `gemini-3.1-flash-tts-preview` audio quality is untested for this use case. If first-run output is unacceptably robotic, the only lever is prompt engineering on the radio script — plan for 1–2 iteration cycles on the director's-notes preamble and the audio-tag density.
- The batch service is a separate deployment owned in a sibling repo. Verify the three Cloud Run URLs respond `200` to `/health` from the editorial environment and that the bearer token is accepted, before the first end-to-end run.
- Team-level entity coverage in `article_entities` (driven by the ingestion pipeline) determines what the beat reporter actually sees. If NYJ or CHI tagging is patchy in the 48h pre-launch window, the beat reporter will consistently see thin input and over-trigger `no_news`. Audit before Phase 3.

---

*This MVP definition is the implementation contract for the T4L Team Beat Phase 1 build. It supersedes the in-conversation MVP sketch from 2026-05-01 (which referenced SSML and an undefined batch path).*
