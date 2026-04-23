"""One-off: copy curated_images rows + storage objects from the legacy
Supabase project into the new one.

Reads `content.curated_images` from the legacy project, downloads each image
from its public storage URL, uploads it to the new project's `images`
bucket at the same path, and inserts a row into `public.curated_images`
with a rewritten image_url.

Idempotent — uses `x-upsert: true` on storage, and
`Prefer: resolution=merge-duplicates` + `on_conflict=slug` on the row
insert.

Usage:
  ./venv/bin/python scripts/migrate_curated_images.py
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import httpx


LEGACY_URL = "https://yqtiuzhedkfacwgormhn.supabase.co"
LEGACY_KEY = os.environ.get("LEGACY_SUPABASE_KEY", "")

NEW_URL = "https://aiknjzinyxzhoseyxqev.supabase.co"
NEW_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
NEW_BUCKET = "images"

BATCH = 50


async def fetch_legacy_rows(client: httpx.AsyncClient) -> list[dict]:
    headers = {
        "apikey": LEGACY_KEY,
        "Authorization": f"Bearer {LEGACY_KEY}",
        "Accept-Profile": "content",
    }
    response = await client.get(
        f"{LEGACY_URL}/rest/v1/curated_images",
        headers=headers,
        params={"select": "*", "order": "id.asc"},
    )
    response.raise_for_status()
    return response.json()


async def copy_one(
    client: httpx.AsyncClient, row: dict
) -> tuple[str, str | None]:
    """Download one image from legacy, upload to new. Returns (slug, err)."""
    legacy_url = row["image_url"]
    try:
        resp = await client.get(legacy_url)
        resp.raise_for_status()
        content = resp.content
    except Exception as exc:
        return row["slug"], f"download failed: {exc}"

    path = urlparse(legacy_url).path
    # path looks like /storage/v1/object/public/images/curated/ARI_sideline.png
    # We need the object key after the bucket: curated/ARI_sideline.png
    try:
        key = path.split(f"/public/{NEW_BUCKET}/", 1)[1]
    except IndexError:
        return row["slug"], f"cannot parse bucket path: {path}"

    upload_url = f"{NEW_URL}/storage/v1/object/{NEW_BUCKET}/{key}"
    try:
        up = await client.post(
            upload_url,
            content=content,
            headers={
                "apikey": NEW_KEY,
                "Authorization": f"Bearer {NEW_KEY}",
                "Content-Type": resp.headers.get("content-type", "image/png"),
                "x-upsert": "true",
            },
        )
        if up.status_code >= 400:
            return row["slug"], f"upload failed ({up.status_code}): {up.text[:200]}"
    except Exception as exc:
        return row["slug"], f"upload crashed: {exc}"

    return row["slug"], None


async def insert_rows(client: httpx.AsyncClient, rows: list[dict]) -> None:
    """Bulk-insert all rows into the new project in one round-trip."""
    payload = []
    for row in rows:
        legacy_path = urlparse(row["image_url"]).path
        key = legacy_path.split(f"/public/{NEW_BUCKET}/", 1)[1]
        new_url = f"{NEW_URL}/storage/v1/object/public/{NEW_BUCKET}/{key}"
        payload.append({
            "slug": row["slug"],
            "team_code": row.get("team_code"),
            "scene": row["scene"],
            "description": row.get("description") or row["slug"],
            "image_url": new_url,
            "generated_by": row.get("generated_by") or "gemini",
            "prompt": row.get("prompt"),
            "active": row.get("active", True),
        })

    response = await client.post(
        f"{NEW_URL}/rest/v1/curated_images?on_conflict=slug",
        headers={
            "apikey": NEW_KEY,
            "Authorization": f"Bearer {NEW_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=payload,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"insert failed ({response.status_code}): {response.text[:500]}"
        )
    print(f"Inserted/upserted {len(payload)} rows")


async def main() -> int:
    if not LEGACY_KEY:
        print("Set LEGACY_SUPABASE_KEY in the env before running.")
        return 2

    async with httpx.AsyncClient(timeout=60.0) as client:
        rows = await fetch_legacy_rows(client)
        print(f"Fetched {len(rows)} curated_images rows from legacy")

        errors: list[tuple[str, str]] = []
        ok = 0
        for i in range(0, len(rows), BATCH):
            batch = rows[i : i + BATCH]
            results = await asyncio.gather(
                *(copy_one(client, r) for r in batch)
            )
            for slug, err in results:
                if err:
                    errors.append((slug, err))
                else:
                    ok += 1
            print(f"Batch {i // BATCH + 1}: {ok}/{len(rows)} uploaded")

        if errors:
            print(f"\n{len(errors)} upload errors:")
            for slug, err in errors[:10]:
                print(f"  {slug}: {err}")

        successful_slugs = {r["slug"] for r in rows} - {s for s, _ in errors}
        successful_rows = [r for r in rows if r["slug"] in successful_slugs]
        await insert_rows(client, successful_rows)

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
