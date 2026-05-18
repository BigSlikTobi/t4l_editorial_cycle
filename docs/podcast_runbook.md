# T4L Daily Briefing — Personal Podcast Runbook

## Current production direction — Codex artifact runtime

The German morning podcast is moving to a Codex-owned artifact runtime:

```bash
./venv/bin/editorial-cycle podcast init-codex-run
# Codex researches selected clusters and writes ledgers + script.de-DE.json.
./venv/bin/editorial-cycle podcast validate-artifacts var/podcast/episodes/YYYY-MM-DD
./venv/bin/editorial-cycle podcast render-artifact var/podcast/episodes/YYYY-MM-DD
./venv/bin/editorial-cycle podcast publish-artifact var/podcast/episodes/YYYY-MM-DD
```

This path does not use `public.podcast_episodes` as its state machine.
The episode directory under `var/podcast/episodes/YYYY-MM-DD/` is the audit
trail. Rendering prefers Gemini Batch TTS with `gemini-3.1-flash-tts-preview`
and falls back to synchronous Gemini TTS only when the batch fails or after
12 ten-minute status checks without completion. Initialization also writes
`analytics_pack.json` from
nflverse data via `nflreadpy`; Robin's research should use that file for
player/team/schedule/roster stats, derived usage context, and
`angle_candidates`. Those candidates are not script-ready claims; Codex should
use them to run a second source pass and develop football questions that move
the story beyond the news hook. Cite approved nflverse stats as `NFLVERSE` in
the claim ledger. Initialization also writes `pronunciation_guide.json`, which
is passed into Gemini TTS so brand terms and hard names are spoken correctly.
Publishing remains personal-only, but now rehearses a friends-safe packet:
`public_metadata.json`, `show_notes.md`, `publication_safety_report.json`,
`thumbnail_prompt.json`, and a copyright-friendly generated thumbnail. The
Spotify upload uses the cleaned metadata and thumbnail while the full show
notes stay in the episode directory. Before rendering, Codex must also complete
a host-authority pass and write `host_authority_notes.md` so Marcus and Robin's
scripted takes sound specific, owned, and football-literate without adding
unsupported claims.
The older DB-backed `podcast produce → latest-id → deliver` commands remain
available as a fallback while the Codex runtime is rolled out.

The daily NFL podcast pipeline that drops a 20–30 min two-persona
episode (EN + DE) into the user's personal Spotify library every
morning. Personal-only MVP; runs on the user's Hostinger VPS.

This runbook covers install, OAuth bootstrap, daily operation, and
on-call procedures. The architecture is documented in
`/Users/tobiaslatta/.claude/plans/precious-petting-kahan.md` and
referenced as the source of truth for module boundaries.

---

## 1. One-time setup on the VPS

### Dependencies

```bash
# System
sudo apt update
sudo apt install -y python3.13 python3.13-venv ffmpeg

# Save-to-Spotify CLI (Spotify's official Personal Podcasts tool,
# beta as of 2026-05-07). One-line installer (downloads the binary
# and drops it on PATH):
curl -fsSL https://saveto.spotify.com/install.sh | bash
# Version-pin if you need a specific release:
# curl -fsSL https://saveto.spotify.com/install.sh | bash -s -- --version 0.1.1

# Repo + Python deps
git clone <repo-url> /opt/t4l
cd /opt/t4l
python3.13 -m venv venv
./venv/bin/pip install -e '.[dev]'
```

### `.env`

Create `/opt/t4l/.env` with the project's standard secrets plus the
podcast-specific ones:

```env
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://....supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
GEMINI_API_KEY=AIza...

# Podcast (overrides — defaults are fine for most cases)
# PODCAST_DEFAULT_LANGUAGE=en-US
# PODCAST_TARGET_WORD_COUNT=4200
# PODCAST_GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview
# PODCAST_GEMINI_VOICE_COLOR=Fenrir
# PODCAST_GEMINI_VOICE_ANALYST=Charon
# PODCAST_AUDIO_TEMP_DIR=/tmp/t4l_podcast

# Background music + sting (both optional)
# PODCAST_INTRO_MUSIC_PATH=/opt/t4l/assets/music/intro.wav
# PODCAST_STING_MUSIC_PATH=/opt/t4l/assets/music/sting.wav
# PODCAST_INTRO_SOLO_SECONDS=4.0       # music plays solo before voice
# PODCAST_INTRO_TAIL_SECONDS=1.5       # fade out after cold-open ends
# PODCAST_MUSIC_BED_VOLUME_DB=-18.0    # ducked level under voice

# Delivery
# SPOTIFY_TOKEN_PATH=/root/.config/save-to-spotify/token.json
# SAVE_TO_SPOTIFY_CLI_PATH=save-to-spotify
# SAVE_TO_SPOTIFY_SHOW_ID=optional-personal-show-id

# Personal-podcast rehearsal publication packet
# PODCAST_THUMBNAIL_ENABLED=true
# PODCAST_THUMBNAIL_IMAGE_MODEL=gpt-image-2
# PODCAST_THUMBNAIL_IMAGE_QUALITY=medium
# PODCAST_THUMBNAIL_IMAGE_SIZE=1024x1024
# PODCAST_THUMBNAIL_CLI_PATH=/root/.codex/skills/.system/imagegen/scripts/image_gen.py
```

### Background music (optional)

The renderer supports two slots:

- **Intro music** (`PODCAST_INTRO_MUSIC_PATH`) — plays solo for
  `PODCAST_INTRO_SOLO_SECONDS` (default 4s), then ducks to
  `PODCAST_MUSIC_BED_VOLUME_DB` (default −18 dB) under the cold-open
  voice, then fades out over `PODCAST_INTRO_TAIL_SECONDS` (default
  1.5s) after the cold open ends.
- **Sting** (`PODCAST_STING_MUSIC_PATH`) — plays solo between the
  cold open and the body of the show. Use a 3–5s short transition
  cue.

Drop your music files anywhere readable by the cron user (e.g.
`/opt/t4l/assets/music/intro.wav` and `…/sting.wav`) and point the
env vars at them. WAV/MP3/M4A/OGG all work; ffmpeg auto-resamples
and downmixes to mono 24 kHz to match Gemini's voice output.

If a file is configured but missing on disk, the renderer logs a
warning and proceeds without that segment — never breaks delivery.

When neither path is set, the pipeline takes the original single-
render path with no music involvement.

### Apply migration

Migration `010_podcast_episodes.sql` (per CLAUDE.md, applied manually
via Supabase SQL Editor — `supabase db push` is NOT used). Paste the
file contents into the SQL Editor and run.

Verify:

```sql
SELECT count(*) FROM public.podcast_episodes;
SELECT * FROM information_schema.tables WHERE table_name = 'podcast_episodes';
```

### Spotify OAuth bootstrap

The Save-to-Spotify CLI uses personal OAuth: a token file at
`~/.config/save-to-spotify/token.json` (respects `$XDG_CONFIG_HOME`).
The login flow opens a browser by default, but the CLI also supports
a paste-the-code headless flow that works on a VPS.

**Option A — bootstrap on your laptop, copy token to VPS:**

1. Install on a machine with a browser: `curl -fsSL
   https://saveto.spotify.com/install.sh | bash`
2. `save-to-spotify auth login` (browser opens, grant scopes).
3. Verify: `save-to-spotify auth status` should print your account.
4. Locate the token at `~/.config/save-to-spotify/token.json`.
5. `scp ~/.config/save-to-spotify/token.json
   user@vps:~/.config/save-to-spotify/token.json`
6. On the VPS: `chmod 600 ~/.config/save-to-spotify/token.json` then
   `save-to-spotify auth status` to confirm the token works there.

**Option B — headless on the VPS directly:**

```bash
save-to-spotify auth login --no-browser
# Follow the prompt: open the printed URL on your phone/laptop,
# authorize, paste the returned code back into the VPS terminal.
```

If the refresh token expires (Spotify scopes vary; some refresh
indefinitely, others ~60 days), repeat steps 1–4. Document expiry in
your calendar.

---

## 2. Daily operation

### Crontab

```
0 4 * * * /opt/t4l/scripts/podcast_daily.sh
```

The 04:00 cron is in the VPS's local timezone (set the VPS to
`Europe/Berlin` so DST shifts handle themselves). At 04:00 CET/CEST
the script runs produce → deliver for both languages. Episodes
typically land in Spotify by 04:30–05:00, comfortably before the 07:00
commute target.

### Logs

Each daily run writes to `var/podcast/daily-<UTC-timestamp>.log`. The
last 7 days of logs are kept by default; set up logrotate if you want
longer retention:

```
/opt/t4l/var/podcast/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
}
```

### Status query

```sql
SELECT id, run_date, language, status, story_count, word_count,
       duration_seconds, error_message, delivered_at
  FROM public.podcast_episodes
 WHERE run_date >= current_date - interval '7 days'
 ORDER BY run_date DESC, language;
```

State machine:
- `pending` — row created, generation not yet started
- `rendering` — script generated, TTS in flight
- `rendered` — audio file on disk, awaiting delivery
- `delivered` — uploaded to Spotify, audio file deleted
- `failed` — see `error_message`

---

## 3. CLI cheat sheet

```bash
# Inspect today's script without spending TTS budget.
./venv/bin/editorial-cycle podcast produce \
    --language en-US --dry-run \
    --output-script var/podcast/dryrun-en.json

# Real produce (one language).
./venv/bin/editorial-cycle podcast produce --language en-US

# Look up the most recent rendered episode.
./venv/bin/editorial-cycle podcast latest-id --language en-US --status rendered

# Dry-run delivery (logs the save-to-spotify argv).
./venv/bin/editorial-cycle podcast deliver 42 --dry-run

# Real delivery.
./venv/bin/editorial-cycle podcast deliver 42

# Manual end-to-end for one day (both languages).
./scripts/podcast_daily.sh
```

---

## 4. Recovery procedures

### A failed render

```sql
SELECT id, error_message FROM public.podcast_episodes
 WHERE status = 'failed' AND run_date = current_date;
```

Most common causes:
- **Gemini TTS quota / 429** — re-run produce. Output is idempotent
  (the (run_date, language) unique constraint upserts).
- **OpenAI agent error** — rare; usually a transient model timeout.
- **Empty feed** — produce intentionally ships an apology script;
  this should NOT show up as `failed`. If it does, that's a bug.

```bash
./venv/bin/editorial-cycle podcast produce --language en-US
```

### A failed delivery

If status is `failed` AND audio_local_path is non-null, the audio is
still on disk. Re-run delivery:

```bash
./venv/bin/editorial-cycle podcast deliver <episode_id>
```

If the audio file was already deleted but Spotify upload failed
afterward, re-produce:

```bash
./venv/bin/editorial-cycle podcast produce --language en-US
./venv/bin/editorial-cycle podcast deliver <new_episode_id>
```

### Spotify token expired

```
save-to-spotify exited 1: token expired / refresh failed
```

Repeat the OAuth bootstrap (section 1.4). The local token file lives
at the path in `SPOTIFY_TOKEN_PATH`.

### VPS down or cron skipped

Daily delivery is a single-shot 04:00 cron with no automatic retry.
If the VPS is offline at 04:00, that day's episode does not happen.
Two options:
- Run `./scripts/podcast_daily.sh` manually after VPS recovers (any
  time before bedtime). The episode lands in Spotify late but lands.
- Skip the day. The `(run_date, language)` unique constraint means
  you can run produce later in the day with a different `--run-date`
  override (not currently exposed via CLI; see open work below).

---

## 5. Open follow-ups (post-MVP)

- **`--run-date` CLI flag** for backfilling missed days.
- **Multi-voice output validation**: confirm Gemini 2.5 multi-speaker
  audio quality at the full 25-min length once we have a week of real
  episodes. If it's choppy or one voice dominates, flip
  `PODCAST_FORCE_SINGLE_VOICE=true` and ride the single-voice
  fallback while iterating.
- **Audio cleanup cron**: a small `find /tmp/t4l_podcast -mtime +2
  -delete` weekly cleans up files that delivery missed deleting.
- **v2 multi-user**: when ready, swap `app/delivery/spotify.py` for a
  Spotify Web API integration backed by per-user OAuth in
  `public.user_spotify_link`. The boundary holds; the produce side
  doesn't change.

---

## 6. Architecture quick reference

- `app/podcast/` — generation: feed pull → cluster → script → render
- `app/delivery/` — distribution: Spotify CLI wrapper + dispatcher
- `app/clients/gemini_tts.py` — direct Gemini multi-speaker TTS client
  (separate from team-beat's Cloud Run worker)
- `app/adapters.py::PodcastEpisodeWriter` — the audit-log adapter
- `public.podcast_episodes` — one row per (run_date, language)

Module boundaries are enforced by code review; `app/podcast/` does not
import from `app/delivery/` and vice versa. Both share only
`app/schemas.py` types and `app/adapters.py` adapters.
