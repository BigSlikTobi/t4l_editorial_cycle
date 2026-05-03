"""HARVEST cycle for the team-beat produce/harvest split.

The team-beat cycle (``editorial-cycle beat``, on the team-beat.yml
cron) produces briefs and submits Gemini TTS batches but DOES NOT wait
for them to finish. Briefs land in ``public.team_roundup`` with
``audio_url=NULL`` and ``tts_batch_id`` set to the Gemini batch name.
This script is the other half: every ~30 min via team-beat-harvest.yml,
it scans the unfinished rows, checks each batch's state via the Gemini
API directly, and processes the ones that have reached
``JOB_STATE_SUCCEEDED``.

Decision matrix per row:
    JOB_STATE_SUCCEEDED  → run process action, PATCH audio_url
    JOB_STATE_PENDING    → skip (try again next cycle)
    JOB_STATE_RUNNING    → skip
    JOB_STATE_FAILED     → log, leave audio_url=NULL (no recovery possible)
    JOB_STATE_CANCELLED  → log, leave audio_url=NULL
    JOB_STATE_EXPIRED    → log, leave audio_url=NULL

Idempotent: runs that find no ready batches exit 0. The TTS service uses
upsert semantics on Storage and our PATCH is a straight overwrite, so
re-running mid-flight is safe.

Usage:
    # Default — harvest everything ready in the most recent cycle.
    ./venv/bin/python scripts/tts_recover.py

    # All cycles with NULL audio (not just most recent).
    ./venv/bin/python scripts/tts_recover.py --all-cycles

    # Pin a specific batch (e.g. legacy rows persisted before migration 009).
    ./venv/bin/python scripts/tts_recover.py --batch-id batches/abc123

    # Skip the Gemini state precheck (force process even if the API
    # says pending — useful for legacy rows where state is unknown).
    ./venv/bin/python scripts/tts_recover.py --batch-id batches/x --skip-state-check

    # Dry run (state-check + process, but no team_roundup PATCH).
    ./venv/bin/python scripts/tts_recover.py --dry-run
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
    supabase_url: str,
    service_key: str,
    *,
    cycle_ts_iso: str | None,
    all_cycles: bool = False,
) -> list[dict[str, Any]]:
    """Return team_roundup rows with audio_url IS NULL.

    Mode selection:
      * cycle_ts_iso set → scope to that cycle (explicit override)
      * all_cycles=True  → every NULL-audio row across every cycle
                            (the harvest cron uses this so a stale
                            row from yesterday doesn't get permanently
                            stranded by a fresh one landing today)
      * default (auto)   → most recent cycle only (one-shot recovery
                            for an operator who just saw a failure)

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
        if cycle_ts_iso or all_cycles:
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

    # 1. Determine scope from DB (or CLI overrides).
    if args.teams and args.cycle_ts:
        teams = [t.strip().upper() for t in args.teams.split(",") if t.strip()]
        rows_all = await _read_pending_roundups(
            supabase_base, service_key, cycle_ts_iso=args.cycle_ts
        )
        rows_by_team = {r["team_code"]: r for r in rows_all}
        rows = [rows_by_team[t] for t in teams if t in rows_by_team]
        missing_in_db = [t for t in teams if t not in rows_by_team]
        if missing_in_db:
            print(
                f"WARN: --teams included {missing_in_db} but no matching "
                f"team_roundup row exists for cycle_ts={args.cycle_ts}; skipping."
            )
    else:
        rows = await _read_pending_roundups(
            supabase_base,
            service_key,
            cycle_ts_iso=args.cycle_ts,
            all_cycles=args.all_cycles,
        )

    if not rows:
        print("No team_roundup rows with audio_url IS NULL — nothing to harvest.")
        return 0

    # 2. Validate TTS service config.
    if not settings.tts_batch_submit_url or not settings.tts_batch_poll_url:
        print(
            "ERROR: TTS_BATCH_SUBMIT_URL / TTS_BATCH_POLL_URL must be set in .env",
            file=sys.stderr,
        )
        return 2
    if not settings.gemini_api_key and not args.skip_state_check:
        print(
            "ERROR: GEMINI_API_KEY is not configured. Either set it (so the "
            "harvest can check batch state via the Gemini API) or pass "
            "--skip-state-check to force process on every batch_id.",
            file=sys.stderr,
        )
        return 2
    auth_token = (
        settings.tts_batch_function_auth_token.get_secret_value()
        if settings.tts_batch_function_auth_token
        else None
    )
    gemini_api_key = (
        settings.gemini_api_key.get_secret_value()
        if settings.gemini_api_key
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
        gemini_api_key=gemini_api_key,
        job_poll_interval_seconds=settings.extraction_poll_interval_seconds,
        process_timeout_seconds=settings.tts_process_timeout_seconds,
    )

    # 3. Group rows by (cycle_ts, batch_id). Multiple cycles can have
    #    NULL-audio rows simultaneously; each cycle is one Gemini batch.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rows_with_no_batch_id: list[dict[str, Any]] = []
    for row in rows:
        bid = args.batch_id or row.get("tts_batch_id")
        if not bid:
            rows_with_no_batch_id.append(row)
            continue
        groups[(row["cycle_ts"], bid)].append(row)

    if rows_with_no_batch_id and not args.batch_id:
        print(
            f"WARN: {len(rows_with_no_batch_id)} row(s) have no tts_batch_id "
            "and no --batch-id was passed — they cannot be harvested. Most "
            "likely cause: cycle ran before migration 009. Affected rows:"
        )
        for r in rows_with_no_batch_id[:5]:
            print(f"    cycle_ts={r['cycle_ts']} team={r['team_code']} id={r['id']}")
        if len(rows_with_no_batch_id) > 5:
            print(f"    … and {len(rows_with_no_batch_id) - 5} more.")

    print(f"Found {len(groups)} batch group(s) to inspect:")
    for (cycle_ts_iso, batch_id), group_rows in groups.items():
        cycle_slot = group_rows[0]["cycle_slot"]
        teams_csv = ",".join(r["team_code"] for r in group_rows)
        print(f"  cycle_ts={cycle_ts_iso} slot={cycle_slot} batch={batch_id} teams=[{teams_csv}]")

    # 4. For each group: check Gemini state, then process if SUCCEEDED.
    total_processed = 0
    total_skipped_pending = 0
    total_skipped_failed = 0
    total_patch_successes = 0
    total_patch_failures = 0
    try:
        for (cycle_ts_iso, batch_id), group_rows in groups.items():
            cycle_ts = datetime.fromisoformat(cycle_ts_iso.replace("Z", "+00:00"))
            cycle_slot = group_rows[0]["cycle_slot"]
            teams = [r["team_code"] for r in group_rows]
            item_ids = [_tts_item_id(team, cycle_ts) for team in teams]
            path_suffix = _tts_path_prefix_suffix(cycle_ts, cycle_slot)

            print()
            print(f"=== {batch_id} (cycle {cycle_ts_iso} {cycle_slot}) ===")

            # 4a. State check (unless --skip-state-check).
            if not args.skip_state_check:
                try:
                    state = await client.check_batch_state(batch_id)
                except Exception as exc:
                    print(f"  state check FAILED: {exc!s}; skipping group.")
                    total_skipped_failed += len(group_rows)
                    continue
                print(f"  Gemini state: {state}")
                if state == "JOB_STATE_PENDING" or state == "JOB_STATE_RUNNING":
                    print("  → still pending; skip (will retry next cycle).")
                    total_skipped_pending += len(group_rows)
                    continue
                if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
                    print(f"  → terminal {state}; no recovery possible. Leaving audio_url=NULL.")
                    total_skipped_failed += len(group_rows)
                    continue
                if state != "JOB_STATE_SUCCEEDED":
                    print(f"  → unrecognized state {state!r}; skip.")
                    total_skipped_pending += len(group_rows)
                    continue

            # 4b. Process the batch.
            try:
                outcome = await client.process_batch(
                    batch_id, item_ids, path_prefix_suffix=path_suffix
                )
            except Exception as exc:
                print(f"  process FAILED: {exc!s}; skipping group.")
                total_skipped_failed += len(group_rows)
                continue

            successes: list[tuple[dict, str]] = []
            failures: list[tuple[str, str]] = []
            for row, item_id in zip(group_rows, item_ids):
                url = outcome.url_for(item_id)
                if url:
                    print(f"  ✓ {row['team_code']}: {url}")
                    successes.append((row, url))
                else:
                    err = next(
                        (i.error for i in outcome.items if i.item_id == item_id),
                        "no manifest entry",
                    )
                    print(f"  ✗ {row['team_code']}: {err}")
                    failures.append((row["team_code"], err or "unknown"))

            if args.dry_run:
                print("  --dry-run: skipping PATCH.")
                total_processed += len(successes)
                continue

            # 4c. PATCH team_roundup.audio_url for each success.
            for row, url in successes:
                try:
                    await _patch_roundup_audio(
                        supabase_base, service_key,
                        roundup_id=row["id"], audio_url=url,
                    )
                    total_patch_successes += 1
                    total_processed += 1
                except Exception as exc:
                    print(f"  PATCH FAILED for id={row['id']} ({row['team_code']}): {exc}")
                    total_patch_failures += 1
    finally:
        await client.close()

    print()
    print(
        f"Harvest summary: processed={total_processed} "
        f"skipped_pending={total_skipped_pending} "
        f"skipped_failed={total_skipped_failed} "
        f"patch_successes={total_patch_successes} "
        f"patch_failures={total_patch_failures}"
    )
    # Exit non-zero only when something we tried to do failed (not when
    # we deliberately skipped pending batches — those are expected).
    return 0 if total_patch_failures == 0 and total_skipped_failed == 0 else 1


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
    parser.add_argument(
        "--all-cycles",
        action="store_true",
        help=(
            "Harvest every NULL-audio row across every cycle (not just "
            "the most recent). The harvest cron uses this so a stale "
            "row from a prior firing doesn't get permanently stranded."
        ),
    )
    parser.add_argument(
        "--skip-state-check",
        action="store_true",
        help=(
            "Skip the Gemini-API batch state precheck. Useful for legacy "
            "rows whose batch state is unknown or for forcing process "
            "when you already know the batch is ready."
        ),
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
