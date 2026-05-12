#!/usr/bin/env bash
# T4L Daily Briefing — VPS cron entrypoint.
#
# Produces today's EN + DE episodes and uploads them to the user's
# personal Spotify library. Designed for an unattended 04:00 local-time
# daily cron; lands episodes well before the 07:00 commute target.
#
# Usage (from crontab):
#   0 4 * * * /opt/t4l/scripts/podcast_daily.sh
#
# All four steps run sequentially. The script exits non-zero only if a
# step that prevents delivery fails — produce or deliver. Per-language
# failures do NOT abort the script; the other language still ships.

set -uo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="var/podcast"
mkdir -p "$LOG_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$LOG_DIR/daily-${TS}.log"

CLI="./venv/bin/editorial-cycle"

run_lang() {
    local lang="$1"
    echo "=== [$lang] produce ==="
    if ! "$CLI" podcast produce --language "$lang"; then
        echo "[$lang] PRODUCE FAILED — skipping deliver"
        return 1
    fi

    local episode_id
    if ! episode_id=$("$CLI" podcast latest-id --language "$lang" --status rendered); then
        echo "[$lang] no rendered episode found; skipping deliver"
        return 1
    fi
    echo "[$lang] rendered episode_id=$episode_id"

    echo "=== [$lang] deliver ==="
    if ! "$CLI" podcast deliver "$episode_id"; then
        echo "[$lang] DELIVER FAILED for episode #$episode_id"
        return 1
    fi
}

{
    echo "T4L Daily Podcast — $TS"
    echo "------------------------"

    en_status=0
    de_status=0
    run_lang "en-US" || en_status=$?
    run_lang "de-DE" || de_status=$?

    echo "------------------------"
    echo "EN exit=$en_status  DE exit=$de_status"

    # Aggregate exit: zero iff both languages delivered.
    if [ $en_status -eq 0 ] && [ $de_status -eq 0 ]; then
        echo "DAILY OK"
        exit 0
    else
        echo "DAILY PARTIAL — at least one language failed"
        exit 1
    fi
} >> "$LOG" 2>&1
