"""Pre-flight guard for the team-beat cron.

Problem this solves: when our local AsyncJobClient times out on a
gemini_tts_batch ``create`` (or any stage), the row in
``public.extraction_jobs`` stays at ``status='running'`` until either:
  * the worker eventually writes terminal status (best case), or
  * ``expires_at`` passes and the sibling repo's cleanup workflow drops
    it, or
  * the same cleanup workflow ``re-POSTs the row to the worker``,
    triggering a brand-new Gemini batch run for the same payload —
    duplicate spend, duplicate audio.

This script runs at the start of every team-beat cron firing and
forcibly fails any non-terminal ``gemini_tts_batch`` rows so the cleanup
workflow ignores them. The downside (the worker's eventual terminal
result is discarded if it ever arrives) is acceptable because the
recovery script (`scripts/tts_recover.py`) can fetch the audio
out-of-band whenever needed — we don't actually need the worker's
result row to land.

Usage:
    ./venv/bin/python scripts/team_beat_preflight.py
    ./venv/bin/python scripts/team_beat_preflight.py --dry-run

Exit codes:
    0 — preflight cleared (no stale rows OR all stale rows successfully marked failed)
    1 — preflight could not clear stale rows (PATCH failed, etc.) — caller should NOT proceed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


# We only target rows that are still "alive" from the cleanup workflow's
# perspective. Rows past expires_at will be dropped by the cleanup on
# its next firing — no action needed from us.
_TARGET_STATUSES = ("queued", "running")
_SERVICE = "gemini_tts_batch"


async def _list_stale_rows(c: httpx.AsyncClient, base_url: str) -> list[dict[str, Any]]:
    """Return non-terminal gemini_tts_batch rows still inside expires_at."""
    now_iso = datetime.now(UTC).isoformat()
    statuses_csv = ",".join(_TARGET_STATUSES)
    r = await c.get(
        f"{base_url}/rest/v1/extraction_jobs",
        params={
            "select": "job_id,status,input,created_at,expires_at,attempts",
            "service": f"eq.{_SERVICE}",
            "status": f"in.({statuses_csv})",
            "expires_at": f"gt.{now_iso}",
            "order": "created_at.asc",
        },
    )
    r.raise_for_status()
    return r.json()


async def _mark_failed(
    c: httpx.AsyncClient,
    base_url: str,
    job_id: str,
    *,
    reason: str,
) -> bool:
    """PATCH a row to status='failed' so the sibling cleanup skips it.

    Returns True on success, False on PATCH error (caller should treat
    as a hard failure — leaving stale rows means duplicate Gemini batches).
    """
    payload = {
        "status": "failed",
        "finished_at": datetime.now(UTC).isoformat(),
        "error": {
            "code": "preflight_canceled",
            "message": reason,
            "retryable": False,
        },
    }
    r = await c.patch(
        f"{base_url}/rest/v1/extraction_jobs",
        params={"job_id": f"eq.{job_id}"},
        json=payload,
        headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
    )
    if r.status_code >= 300:
        logger.error(
            "Preflight PATCH failed for job_id=%s (HTTP %d): %s",
            job_id, r.status_code, r.text[:200],
        )
        return False
    return True


async def run(*, dry_run: bool) -> int:
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    headers = {"Authorization": f"Bearer {key}", "apikey": key}

    async with httpx.AsyncClient(headers=headers, timeout=15) as c:
        try:
            stale = await _list_stale_rows(c, base)
        except httpx.HTTPError as exc:
            print(f"ERROR: could not list extraction_jobs ({exc})", file=sys.stderr)
            return 1

        if not stale:
            print("Preflight clear: no stale gemini_tts_batch rows.")
            return 0

        print(f"Found {len(stale)} stale gemini_tts_batch row(s):")
        for row in stale:
            inp = row.get("input") or {}
            action = inp.get("action") if isinstance(inp, dict) else None
            print(
                f"  job_id={row['job_id']} status={row['status']} action={action} "
                f"created_at={row.get('created_at')} expires_at={row.get('expires_at')} "
                f"attempts={row.get('attempts')}"
            )

        if dry_run:
            print("\n--dry-run: not patching.")
            return 0

        all_ok = True
        for row in stale:
            ok = await _mark_failed(
                c, base, row["job_id"],
                reason="Preflight cancellation by team-beat cron to prevent duplicate Gemini batches.",
            )
            if ok:
                print(f"  ✓ marked failed: {row['job_id']}")
            else:
                all_ok = False

        return 0 if all_ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List stale rows without patching them.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
