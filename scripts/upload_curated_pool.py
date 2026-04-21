"""Upload reviewed curated pool images to Supabase.

Workflow:
  1. scripts/submit_curated_batch.py generates PNGs into var/curated_pool/raw/
  2. You review the folder by hand, DELETE any bad / wrong / weird PNGs.
  3. This script scans the survivors and uploads them with metadata.

For each surviving PNG named `{slug}.png`:
  - Look up metadata in var/curated_pool_plan.json
  - Upload bytes to Supabase Storage at curated/{slug}.png (x-upsert=true)
  - Upsert a row in content.curated_images (keyed on slug)

Prereqs:
  - Migration 005_curated_images.sql applied
  - SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env

Usage:
    # Show what would be uploaded, no writes:
    ./venv/bin/python scripts/upload_curated_pool.py --dry-run

    # Upload everything in raw/:
    ./venv/bin/python scripts/upload_curated_pool.py

    # Restrict to a specific team:
    ./venv/bin/python scripts/upload_curated_pool.py --teams ARI
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import httpx

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("upload_curated_pool")

ROOT = Path(__file__).resolve().parent.parent
PLAN_JSON = ROOT / "var" / "curated_pool_plan.json"
RAW_DIR = ROOT / "var" / "curated_pool" / "raw"

BUCKET = "images"
STORAGE_PREFIX = "curated"


def _load_plan_by_slug() -> dict[str, dict]:
    if not PLAN_JSON.exists():
        raise SystemExit(f"{PLAN_JSON} not found — run build_curated_pool_plan.py")
    return {row["slug"]: row for row in json.loads(PLAN_JSON.read_text())}


def _list_raw_pngs(teams: set[str] | None) -> list[Path]:
    if not RAW_DIR.exists():
        raise SystemExit(f"{RAW_DIR} not found — run submit_curated_batch.py first")
    paths = sorted(RAW_DIR.glob("*.png"))
    if teams is None:
        return paths
    return [p for p in paths if p.stem.split("_", 1)[0] in teams]


async def _upload_one(
    client: httpx.AsyncClient,
    base: str,
    row: dict,
    png_bytes: bytes,
) -> str | None:
    """Upload PNG to storage and upsert metadata row. Returns public URL."""
    slug = row["slug"]
    path = f"{STORAGE_PREFIX}/{slug}.png"
    public_url = f"{base}/storage/v1/object/public/{BUCKET}/{path}"

    # 1. Upload bytes (x-upsert for idempotency)
    upload_resp = await client.post(
        f"{base}/storage/v1/object/{BUCKET}/{path}",
        content=png_bytes,
        headers={"Content-Type": "image/png", "x-upsert": "true"},
    )
    if upload_resp.status_code >= 400:
        logger.error("Upload failed for %s: %d %s",
                     slug, upload_resp.status_code, upload_resp.text[:200])
        return None

    # 2. Upsert metadata row (keyed on slug, which is the unique constraint)
    meta_resp = await client.post(
        f"{base}/rest/v1/curated_images",
        json={
            "slug": slug,
            "team_code": row["team_code"],
            "scene": row["scene"],
            "description": row["description"],
            "image_url": public_url,
            "generated_by": "gemini",
            "prompt": row["prompt"],
            "active": True,
        },
        headers={
            "Content-Type": "application/json",
            "Content-Profile": "content",
            "Accept-Profile": "content",
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
    )
    if meta_resp.status_code >= 400:
        logger.error("Metadata insert failed for %s: %d %s",
                     slug, meta_resp.status_code, meta_resp.text[:200])
        return None

    return public_url


async def _run(paths: list[Path], by_slug: dict[str, dict], dry_run: bool) -> None:
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()

    uploaded = 0
    skipped_no_plan = 0
    failed = 0

    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"Authorization": f"Bearer {key}", "apikey": key},
    ) as client:
        for i, path in enumerate(paths, 1):
            slug = path.stem
            row = by_slug.get(slug)
            if row is None:
                logger.warning("[%d/%d] %s — not in plan JSON, skipping",
                               i, len(paths), slug)
                skipped_no_plan += 1
                continue

            if dry_run:
                print(f"[{i}/{len(paths)}] WOULD upload {slug} "
                      f"(team={row['team_code']}, scene={row['scene']}, "
                      f"{path.stat().st_size} bytes)")
                continue

            png = path.read_bytes()
            url = await _upload_one(client, base, row, png)
            if url:
                logger.info("[%d/%d] %s → %s", i, len(paths), slug, url)
                uploaded += 1
            else:
                failed += 1

    print(f"\n=== Upload summary ===")
    print(f"Candidates:   {len(paths)}")
    if dry_run:
        print("(dry run — nothing written)")
    else:
        print(f"Uploaded:     {uploaded}")
        print(f"Not in plan:  {skipped_no_plan}")
        print(f"Failed:       {failed}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--teams", type=str,
                        help="Comma-separated team codes to upload (default: all in raw/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be uploaded, do not write")
    args = parser.parse_args()

    teams = (
        {t.strip().upper() for t in args.teams.split(",")} if args.teams else None
    )
    by_slug = _load_plan_by_slug()
    paths = _list_raw_pngs(teams)

    if not paths:
        raise SystemExit(
            f"No PNGs found in {RAW_DIR}" + (f" for teams {teams}" if teams else "")
        )

    print(f"Found {len(paths)} PNGs to upload")
    asyncio.run(_run(paths, by_slug, args.dry_run))


if __name__ == "__main__":
    main()
