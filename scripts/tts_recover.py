"""Recover audio for a completed Gemini TTS batch whose `process` step never landed.

Use case: ``editorial-cycle beat`` ran, the briefs landed in
``team_roundup`` with ``audio_url=NULL``, but the upstream Gemini batch
finished and the MP3s are ready in Google's batch output file. This
script runs only the ``process`` action against the
gemini_tts_batch_service Cloud Run endpoints to download the MP3s,
upload them to the configured Supabase Storage bucket, then PATCH each
team_roundup row with its audio_url.

Default behaviour: target the most recent set of filed roundup rows that
share a ``cycle_ts`` and have ``audio_url IS NULL`` (i.e. the cycle that
produced the gap). The cycle's teams + cycle_ts are read from
``public.team_roundup``; the script does NOT need the original cycle log.

Usage:
    # Auto-discover everything (teams + cycle_ts + batch_id) from the
    # most recent cycle that has team_roundup rows with audio_url IS NULL.
    # batch_id resolves from team_roundup.tts_batch_id (migration 009).
    ./venv/bin/python scripts/tts_recover.py

    # Pin a specific batch (e.g. legacy rows persisted before migration 009):
    ./venv/bin/python scripts/tts_recover.py --batch-id batches/abc123

    # Be fully explicit:
    ./venv/bin/python scripts/tts_recover.py \\
        --batch-id batches/abc123 \\
        --cycle-ts 2026-05-02T16:54:22+00:00 \\
        --teams PHI,DAL,GB,SF

    # Dry run (run process action, print resulting URLs, skip the PATCH):
    ./venv/bin/python scripts/tts_recover.py --dry-run

The script is safe to re-run: the TTS service uses upsert semantics on
the storage bucket, and the PATCH is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

from app.clients.base import SupabaseJobsConfig
from app.config import get_settings
from app.team_beat.tts_client import TTSBatchClient
from app.team_beat.workflow import _tts_item_id, _tts_path_prefix_suffix

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- DB helpers


async def _read_pending_roundups(
    supabase_url: str, service_key: str, *, cycle_ts_iso: str | None
) -> list[dict[str, Any]]:
    """Return team_roundup rows with audio_url IS NULL.

    When ``cycle_ts_iso`` is provided, scope to that cycle. Otherwise
    return rows from the most recent cycle that has any NULL audio_url.

    Selects `tts_batch_id` so the caller can auto-discover the Gemini
    batch id without needing it on the command line.
    """
    base = supabase_url.rstrip("/")
    headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
    async with httpx.AsyncClient(headers=headers, timeout=15) as c:
        params: dict[str, str] = {
            "select": "id,team_code,cycle_ts,cycle_slot,audio_url,tts_batch_id",
            "audio_url": "is.null",
            "order": "cycle_ts.desc",
        }
        if cycle_ts_iso:
            params["cycle_ts"] = f"eq.{cycle_ts_iso}"
        r = await c.get(f"{base}/rest/v1/team_roundup", params=params)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return []
        if cycle_ts_iso:
            return rows
        # Auto-mode: keep only rows from the most recent distinct cycle_ts.
        latest_ts = rows[0]["cycle_ts"]
        return [row for row in rows if row["cycle_ts"] == latest_ts]


async def _patch_roundup_audio(
    supabase_url: str,
    service_key: str,
    *,
    roundup_id: int,
    audio_url: str,
) -> None:
    base = supabase_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15) as c:
        r = await c.patch(
            f"{base}/rest/v1/team_roundup",
            params={"id": f"eq.{roundup_id}"},
            json={"audio_url": audio_url},
        )
        r.raise_for_status()


# ------------------------------------------------------------- main routine


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    supabase_base = str(settings.supabase_url)
    service_key = settings.supabase_service_role_key.get_secret_value()

    # 1. Determine cycle scope from DB (or CLI overrides).
    if args.teams and args.cycle_ts:
        teams = [t.strip().upper() for t in args.teams.split(",") if t.strip()]
        cycle_ts_iso = args.cycle_ts
        cycle_ts = datetime.fromisoformat(cycle_ts_iso.replace("Z", "+00:00"))
        # Fetch existing rows so we can compute slot and PATCH later.
        rows = await _read_pending_roundups(
            supabase_base, service_key, cycle_ts_iso=cycle_ts_iso
        )
        # Allow CLI teams to be a subset of what's in DB.
        rows_by_team = {r["team_code"]: r for r in rows}
        scoped = [rows_by_team[t] for t in teams if t in rows_by_team]
        missing_in_db = [t for t in teams if t not in rows_by_team]
        if missing_in_db:
            print(
                f"WARN: --teams included {missing_in_db} but no matching "
                f"team_roundup row exists for cycle_ts={cycle_ts_iso}; skipping."
            )
        rows = scoped
    else:
        rows = await _read_pending_roundups(
            supabase_base, service_key, cycle_ts_iso=args.cycle_ts
        )

    if not rows:
        print("No team_roundup rows with audio_url IS NULL — nothing to recover.")
        return 0

    # All rows in `rows` share a single cycle_ts (auto-mode filters that
    # way; explicit-mode passes cycle_ts directly).
    cycle_ts_iso = rows[0]["cycle_ts"]
    cycle_ts = datetime.fromisoformat(cycle_ts_iso.replace("Z", "+00:00"))
    cycle_slot = rows[0]["cycle_slot"]
    teams = [r["team_code"] for r in rows]

    # Resolve batch_id: CLI override wins, else read from team_roundup.
    # Migration 009 added tts_batch_id; older rows persisted before that
    # migration won't have it and require --batch-id on the command line.
    batch_id = args.batch_id
    if not batch_id:
        candidates = sorted({r.get("tts_batch_id") for r in rows if r.get("tts_batch_id")})
        if not candidates:
            print(
                "ERROR: no --batch-id provided and no tts_batch_id stored on the "
                "selected team_roundup rows. Either:\n"
                "  - rerun with --batch-id batches/<id>, or\n"
                "  - apply migration 009 + run a fresh cycle so tts_batch_id is persisted.",
                file=sys.stderr,
            )
            return 2
        if len(candidates) > 1:
            print(
                f"ERROR: rows in this cycle reference multiple batch ids: {candidates}\n"
                "  Pass --batch-id explicitly to choose one.",
                file=sys.stderr,
            )
            return 2
        batch_id = candidates[0]
        print(f"Auto-discovered batch_id from team_roundup.tts_batch_id: {batch_id}")

    print(f"Recovering audio for cycle_ts={cycle_ts_iso} slot={cycle_slot}")
    print(f"  Teams ({len(teams)}): {', '.join(teams)}")
    print(f"  Batch id: {batch_id}")

    # 2. Compute item ids + path prefix the original cycle would have used.
    item_ids = [_tts_item_id(team, cycle_ts) for team in teams]
    path_suffix = _tts_path_prefix_suffix(cycle_ts, cycle_slot)
    print(f"  Path prefix suffix: {path_suffix}")
    print(f"  Item ids: {item_ids}")

    # 3. Validate config + build TTS client.
    if not settings.tts_batch_submit_url or not settings.tts_batch_poll_url:
        print(
            "ERROR: TTS_BATCH_SUBMIT_URL / TTS_BATCH_POLL_URL must be set in .env",
            file=sys.stderr,
        )
        return 2
    auth_token = (
        settings.tts_batch_function_auth_token.get_secret_value()
        if settings.tts_batch_function_auth_token
        else None
    )

    client = TTSBatchClient(
        submit_url=str(settings.tts_batch_submit_url),
        poll_url=str(settings.tts_batch_poll_url),
        supabase=SupabaseJobsConfig(url=supabase_base),
        auth_token=auth_token,
        model_name=settings.tts_model_name,
        voice_name=settings.tts_voice_name,
        storage_bucket=settings.tts_storage_bucket,
        storage_path_prefix=settings.tts_storage_path_prefix,
        job_poll_interval_seconds=settings.extraction_poll_interval_seconds,
        job_timeout_seconds=settings.extraction_timeout_seconds,
        # status loop tunables don't matter — we skip status entirely.
        status_poll_interval_seconds=settings.tts_status_poll_interval_seconds,
        status_timeout_seconds=settings.tts_status_poll_timeout_seconds,
    )

    # 4. Run the process action only.
    try:
        outcome = await client.process_batch(
            batch_id, item_ids, path_prefix_suffix=path_suffix
        )
    finally:
        await client.close()

    print(f"\nProcess returned manifest for batch {outcome.batch_id}:")
    successes: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    for team, item_id in zip(teams, item_ids):
        url = outcome.url_for(item_id)
        if url:
            print(f"  ✓ {team}: {url}")
            successes.append((team, url))
        else:
            err = next(
                (i.error for i in outcome.items if i.item_id == item_id),
                "no manifest entry",
            )
            print(f"  ✗ {team}: {err}")
            failures.append((team, err or "unknown"))

    if args.dry_run:
        print("\n--dry-run set; skipping PATCH of team_roundup rows.")
        return 0 if not failures else 1

    # 5. PATCH team_roundup.audio_url for each successful item.
    rows_by_team = {r["team_code"]: r for r in rows}
    print()
    for team, url in successes:
        roundup_id = rows_by_team[team]["id"]
        await _patch_roundup_audio(
            supabase_base, service_key, roundup_id=roundup_id, audio_url=url
        )
        print(f"  PATCHed team_roundup id={roundup_id} ({team}) audio_url ✓")

    print(f"\nDone. {len(successes)} recovered, {len(failures)} failed.")
    return 0 if not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-id",
        default=None,
        help=(
            "Gemini batch id (format: 'batches/abc123'). When omitted, "
            "auto-discover from team_roundup.tts_batch_id (added in "
            "migration 009). Required for legacy rows persisted before "
            "that migration."
        ),
    )
    parser.add_argument(
        "--cycle-ts",
        type=str,
        default=None,
        help=(
            "ISO cycle_ts (e.g. '2026-05-02T16:54:22+00:00'). When omitted, "
            "auto-detect the most recent cycle with NULL audio_url rows."
        ),
    )
    parser.add_argument(
        "--teams",
        type=str,
        default="",
        help=(
            "Comma-separated team codes. When omitted, recover all teams "
            "in the chosen cycle. Requires --cycle-ts when set."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the TTS process action but skip the team_roundup PATCH.",
    )
    args = parser.parse_args()

    if args.teams and not args.cycle_ts:
        print(
            "ERROR: --teams requires --cycle-ts (otherwise auto-detection "
            "could pick a different cycle than the one your teams belong to).",
            file=sys.stderr,
        )
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
