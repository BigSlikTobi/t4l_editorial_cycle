"""Generate curated-pool images via Gemini.

Pilots first (a few teams) are run in --mode sync for immediate feedback so
prompts can be iterated quickly. The full 250-image run will use --mode batch
(not yet implemented) for the 50% price discount.

Outputs:
  - var/curated_pool/raw/{slug}.png        — one PNG per successful generation
  - var/curated_pool/submission_log.csv    — per-slug status, cost, timestamp

Usage:
    # Pilot for Arizona Cardinals (7 images, ~$0.47 sync):
    ./venv/bin/python scripts/submit_curated_batch.py --teams ARI

    # Multiple teams:
    ./venv/bin/python scripts/submit_curated_batch.py --teams ARI,KC,DAL

    # Just the generic scenes (26 images):
    ./venv/bin/python scripts/submit_curated_batch.py --generic

    # All 250 at once (still sync — use --mode batch for 50% off):
    ./venv/bin/python scripts/submit_curated_batch.py --all

    # Show what WOULD be sent without calling the API or spending money:
    ./venv/bin/python scripts/submit_curated_batch.py --teams ARI --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("submit_curated_batch")

ROOT = Path(__file__).resolve().parent.parent
PLAN_JSON = ROOT / "var" / "curated_pool_plan.json"
RAW_DIR = ROOT / "var" / "curated_pool" / "raw"
LOG_CSV = ROOT / "var" / "curated_pool" / "submission_log.csv"
BATCH_JOB_FILE = ROOT / "var" / "curated_pool" / "batch_job.json"

GEMINI_BASE = "https://generativelanguage.googleapis.com"

# Sync-tier price per 1K image (as of pricing page fetched this session).
# Used only for cost estimation in dry-run / summary output.
PRICE_PER_IMAGE_SYNC = 0.067
PRICE_PER_IMAGE_BATCH = 0.034


def _load_plan() -> list[dict]:
    if not PLAN_JSON.exists():
        raise SystemExit(
            f"{PLAN_JSON} not found — run scripts/build_curated_pool_plan.py first"
        )
    return json.loads(PLAN_JSON.read_text())


def _filter_rows(
    plan: list[dict],
    *,
    teams: list[str] | None,
    generic: bool,
    all_: bool,
    scenes: list[str] | None,
) -> list[dict]:
    if all_:
        subset = list(plan)
    elif generic and not teams:
        subset = [r for r in plan if r["team_code"] is None]
    elif teams and not generic:
        team_set = {t.upper() for t in teams}
        subset = [r for r in plan if r["team_code"] in team_set]
    elif teams and generic:
        team_set = {t.upper() for t in teams}
        subset = [
            r for r in plan if r["team_code"] in team_set or r["team_code"] is None
        ]
    else:
        raise SystemExit(
            "Pick at least one scope: --teams X,Y | --generic | --all"
        )

    if scenes:
        scene_set = set(scenes)
        subset = [r for r in subset if r["scene"] in scene_set]
    return subset


async def _generate_one(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    prompt: str,
) -> bytes | None:
    """Call Gemini generateContent and return the raw PNG bytes, or None.

    Mirrors the shape of app/writer/image_clients.GeminiImageClient — kept
    duplicated here so this script has no dependency on the live-selector
    client (different lifetime, no retry needed for a one-shot run).
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    response = await client.post(url, params={"key": api_key}, json=payload)
    if response.status_code >= 400:
        logger.warning("Gemini %d: %s", response.status_code, response.text[:200])
        return None
    data = response.json()
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"], validate=True)
                except Exception as exc:
                    logger.warning("Invalid base64 in Gemini response: %s", exc)
                    return None
    # No image in response — log finishReason for diagnostics (SAFETY etc).
    reasons = [c.get("finishReason") for c in data.get("candidates", [])]
    logger.warning("No image in Gemini response; finishReasons=%s", reasons)
    return None


async def _run_sync(
    rows: list[dict], api_key: str, model: str, skip_existing: bool
) -> list[dict]:
    """Run rows one at a time. Returns status dicts for the log."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    status: list[dict] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for i, row in enumerate(rows, 1):
            slug = row["slug"]
            out_path = RAW_DIR / f"{slug}.png"

            if skip_existing and out_path.exists():
                logger.info("[%d/%d] %s — already exists, skipping", i, len(rows), slug)
                status.append({
                    "slug": slug, "status": "skipped_existing",
                    "bytes": out_path.stat().st_size, "error": "",
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                continue

            logger.info("[%d/%d] Generating %s...", i, len(rows), slug)
            try:
                png = await _generate_one(client, api_key, model, row["prompt"])
            except Exception as exc:
                logger.error("[%d/%d] %s — exception: %s", i, len(rows), slug, exc)
                status.append({
                    "slug": slug, "status": "error", "bytes": 0,
                    "error": str(exc)[:300],
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                continue

            if png is None:
                status.append({
                    "slug": slug, "status": "no_image", "bytes": 0,
                    "error": "Gemini returned no inlineData",
                    "timestamp": datetime.now(UTC).isoformat(),
                })
                continue

            out_path.write_bytes(png)
            status.append({
                "slug": slug, "status": "success", "bytes": len(png),
                "error": "", "timestamp": datetime.now(UTC).isoformat(),
            })
    return status


# --- Batch mode --------------------------------------------------------


def _build_jsonl(rows: list[dict]) -> bytes:
    """One line per request. Key = slug (echoed back in output)."""
    lines: list[str] = []
    for row in rows:
        obj = {
            "key": row["slug"],
            "request": {
                "contents": [{"parts": [{"text": row["prompt"]}]}],
                "generation_config": {"responseModalities": ["IMAGE"]},
            },
        }
        lines.append(json.dumps(obj, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _upload_jsonl(
    client: httpx.AsyncClient, api_key: str, jsonl: bytes, display_name: str
) -> str:
    """Resumable upload per Gemini docs. Returns files/XXX identifier."""
    # Step 1: start — response header X-Goog-Upload-URL is the target for bytes.
    start = await client.post(
        f"{GEMINI_BASE}/upload/v1beta/files",
        headers={
            "x-goog-api-key": api_key,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(jsonl)),
            "X-Goog-Upload-Header-Content-Type": "application/jsonl",
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": display_name}},
    )
    if start.status_code >= 400:
        raise SystemExit(f"Upload start failed: {start.status_code} {start.text[:300]}")
    upload_url = start.headers.get("x-goog-upload-url") or start.headers.get(
        "X-Goog-Upload-URL"
    )
    if not upload_url:
        raise SystemExit(
            f"No upload URL in response headers. Headers: {dict(start.headers)}"
        )

    # Step 2: upload + finalize in one call.
    finalize = await client.post(
        upload_url,
        content=jsonl,
        headers={
            "Content-Length": str(len(jsonl)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    if finalize.status_code >= 400:
        raise SystemExit(
            f"Upload finalize failed: {finalize.status_code} {finalize.text[:300]}"
        )
    body = finalize.json()
    file_name = body.get("file", {}).get("name")
    if not file_name:
        raise SystemExit(f"No file name in upload response: {body}")
    return file_name  # e.g. "files/abc123"


async def _create_batch(
    client: httpx.AsyncClient, api_key: str, model: str, file_name: str, display: str
) -> str:
    resp = await client.post(
        f"{GEMINI_BASE}/v1beta/models/{model}:batchGenerateContent",
        params={"key": api_key},
        json={
            "batch": {
                "display_name": display,
                "input_config": {"file_name": file_name},
            }
        },
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Batch create failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    job_name = body.get("name")
    if not job_name:
        raise SystemExit(f"No job name in batch response: {body}")
    return job_name  # e.g. "batches/XXXX"


async def _poll_batch(
    client: httpx.AsyncClient, api_key: str, job_name: str
) -> dict:
    """Block until state is terminal. Returns the final job object."""
    poll_interval = 20.0
    max_interval = 120.0
    while True:
        resp = await client.get(
            f"{GEMINI_BASE}/v1beta/{job_name}",
            params={"key": api_key},
        )
        if resp.status_code >= 400:
            logger.warning("Poll returned %d: %s", resp.status_code, resp.text[:200])
            await asyncio.sleep(poll_interval)
            continue
        body = resp.json()
        state = body.get("metadata", {}).get("state") or body.get("state")
        logger.info("Batch %s state=%s", job_name, state)
        # API returns BATCH_STATE_* (e.g. BATCH_STATE_PENDING, BATCH_STATE_SUCCEEDED).
        # Earlier docs mentioned JOB_STATE_* — accept both to be forward-compatible.
        if state and (state.endswith("_SUCCEEDED") or state.endswith("_FAILED")
                      or state.endswith("_CANCELLED") or state.endswith("_EXPIRED")):
            return body
        await asyncio.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.3, max_interval)


async def _download_output(
    client: httpx.AsyncClient, api_key: str, output_file: str
) -> bytes:
    resp = await client.get(
        f"{GEMINI_BASE}/v1beta/{output_file}:download",
        params={"alt": "media", "key": api_key},
        follow_redirects=True,
    )
    if resp.status_code >= 400:
        raise SystemExit(
            f"Output download failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.content


def _parse_output_jsonl(data: bytes) -> dict[str, dict]:
    """Map slug → response object. Each line: {'key': '...', 'response': {...}}."""
    out: dict[str, dict] = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed line: %s", exc)
            continue
        key = obj.get("key")
        if key:
            out[key] = obj
    return out


def _extract_png(response_obj: dict) -> bytes | None:
    """Pull first inlineData image out of a single response entry."""
    response = response_obj.get("response")
    if response is None:
        return None
    for cand in response.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"], validate=True)
                except Exception as exc:
                    logger.warning("Invalid base64: %s", exc)
                    return None
    return None


async def _resume_batch(api_key: str) -> list[dict]:
    """Poll an already-submitted batch job (saved in var/curated_pool/batch_job.json)
    and download + save outputs. Does NOT re-upload or re-submit."""
    if not BATCH_JOB_FILE.exists():
        raise SystemExit(f"{BATCH_JOB_FILE} not found — nothing to resume")
    saved = json.loads(BATCH_JOB_FILE.read_text())
    job_name = saved["job_name"]
    slugs: list[str] = saved["slugs"]
    logger.info("Resuming batch %s (%d slugs)", job_name, len(slugs))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120.0) as client:
        final = await _poll_batch(client, api_key, job_name)
        state = final.get("metadata", {}).get("state") or final.get("state")
        if not state or not state.endswith("_SUCCEEDED"):
            logger.error("Resumed batch did not succeed: state=%s body=%s",
                         state, json.dumps(final)[:500])
            return [{
                "slug": s, "status": f"batch_{state}", "bytes": 0,
                "error": "", "timestamp": datetime.now(UTC).isoformat(),
            } for s in slugs]

        output_file = (
            final.get("metadata", {})
                 .get("output", {})
                 .get("responsesFile")
            or final.get("response", {})
                    .get("responsesFile")
            or final.get("metadata", {})
                 .get("output", {})
                 .get("dest", {})
                 .get("file_name")
            or final.get("response", {})
                    .get("responses_file")
            or final.get("metadata", {})
                    .get("output", {})
                    .get("file_name")
        )
        if not output_file:
            logger.error("Succeeded but no output file in response: %s",
                         json.dumps(final)[:800])
            return []
        logger.info("Downloading output file %s...", output_file)
        raw = await _download_output(client, api_key, output_file)
        by_slug = _parse_output_jsonl(raw)
        logger.info("Parsed %d result rows from output", len(by_slug))

    status: list[dict] = []
    for slug in slugs:
        entry = by_slug.get(slug)
        if entry is None:
            status.append({
                "slug": slug, "status": "missing_in_output", "bytes": 0,
                "error": "", "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        png = _extract_png(entry)
        if png is None:
            status.append({
                "slug": slug, "status": "no_image", "bytes": 0,
                "error": json.dumps(entry.get("response", {}).get("candidates", [{}])[0].get("finishReason", ""))[:200],
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        (RAW_DIR / f"{slug}.png").write_bytes(png)
        status.append({
            "slug": slug, "status": "success", "bytes": len(png),
            "error": "", "timestamp": datetime.now(UTC).isoformat(),
        })
    return status


async def _run_batch(
    rows: list[dict], api_key: str, model: str, skip_existing: bool
) -> list[dict]:
    """Full submit-poll-download-parse cycle. Persists job name for resume."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    BATCH_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Skip rows that already have a PNG on disk (same as sync)
    filtered = []
    pre_status: list[dict] = []
    for row in rows:
        out_path = RAW_DIR / f"{row['slug']}.png"
        if skip_existing and out_path.exists():
            pre_status.append({
                "slug": row["slug"], "status": "skipped_existing",
                "bytes": out_path.stat().st_size, "error": "",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        filtered.append(row)

    if not filtered:
        logger.info("All %d rows already have PNGs on disk — nothing to submit",
                    len(rows))
        return pre_status

    logger.info("Submitting batch of %d items (model=%s)", len(filtered), model)
    display = f"curated-pool-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. Upload JSONL
        jsonl = _build_jsonl(filtered)
        logger.info("Uploading JSONL (%d bytes)...", len(jsonl))
        file_name = await _upload_jsonl(client, api_key, jsonl, display)
        logger.info("Uploaded: %s", file_name)

        # 2. Create batch
        job_name = await _create_batch(client, api_key, model, file_name, display)
        logger.info("Created batch job: %s", job_name)
        BATCH_JOB_FILE.write_text(json.dumps({
            "job_name": job_name,
            "input_file": file_name,
            "display_name": display,
            "slugs": [r["slug"] for r in filtered],
            "created_at": datetime.now(UTC).isoformat(),
        }, indent=2))

        # 3. Poll
        logger.info("Polling for completion (this may take minutes to hours)...")
        final = await _poll_batch(client, api_key, job_name)
        state = final.get("metadata", {}).get("state") or final.get("state")
        if not state or not state.endswith("_SUCCEEDED"):
            logger.error("Batch did not succeed: state=%s body=%s",
                         state, json.dumps(final)[:500])
            return pre_status + [{
                "slug": r["slug"], "status": f"batch_{state}", "bytes": 0,
                "error": str(final.get("metadata", {}).get("error", ""))[:300],
                "timestamp": datetime.now(UTC).isoformat(),
            } for r in filtered]

        # 4. Download + parse output file
        output_file = (
            final.get("metadata", {})
                 .get("output", {})
                 .get("responsesFile")
            or final.get("response", {})
                    .get("responsesFile")
            or final.get("metadata", {})
                 .get("output", {})
                 .get("dest", {})
                 .get("file_name")
            or final.get("response", {})
                    .get("responses_file")
            or final.get("metadata", {})
                    .get("output", {})
                    .get("file_name")
        )
        if not output_file:
            logger.error("Succeeded but no output file in response: %s",
                         json.dumps(final)[:500])
            return pre_status

        logger.info("Downloading output file %s...", output_file)
        raw = await _download_output(client, api_key, output_file)
        by_slug = _parse_output_jsonl(raw)
        logger.info("Parsed %d result rows", len(by_slug))

    # 5. Save PNGs
    post_status: list[dict] = []
    for row in filtered:
        slug = row["slug"]
        entry = by_slug.get(slug)
        if entry is None:
            post_status.append({
                "slug": slug, "status": "missing_in_output", "bytes": 0,
                "error": "", "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        png = _extract_png(entry)
        if png is None:
            post_status.append({
                "slug": slug, "status": "no_image", "bytes": 0,
                "error": json.dumps(entry.get("response", {}).get("candidates", [{}])[0].get("finishReason", ""))[:200],
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue
        (RAW_DIR / f"{slug}.png").write_bytes(png)
        post_status.append({
            "slug": slug, "status": "success", "bytes": len(png),
            "error": "", "timestamp": datetime.now(UTC).isoformat(),
        })

    return pre_status + post_status


# --- Logging -----------------------------------------------------------


def _write_log(status: list[dict]) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "slug", "status", "bytes", "error"]
        )
        if new_file:
            writer.writeheader()
        for row in status:
            writer.writerow(row)


def _print_summary(rows: list[dict], status: list[dict], mode: str) -> None:
    succeeded = sum(1 for s in status if s["status"] == "success")
    skipped = sum(1 for s in status if s["status"] == "skipped_existing")
    failed = sum(1 for s in status if s["status"] in {"no_image", "error"})
    price = PRICE_PER_IMAGE_SYNC if mode == "sync" else PRICE_PER_IMAGE_BATCH
    billable = succeeded
    print(f"\n=== Run complete (mode={mode}) ===")
    print(f"Planned:   {len(rows)}")
    print(f"Succeeded: {succeeded}")
    print(f"Skipped:   {skipped}")
    print(f"Failed:    {failed}")
    print(f"Estimated spend: ${billable * price:.2f} (at ${price}/image)")
    print(f"Raw PNGs in: {RAW_DIR}")
    print(f"Log:         {LOG_CSV}")


def main() -> None:
    parser = argparse.ArgumentParser()
    scope = parser.add_argument_group("scope (pick at least one)")
    scope.add_argument("--teams", type=str, help="Comma-separated team codes, e.g. ARI,KC")
    scope.add_argument("--generic", action="store_true", help="Include generic (no-team) scenes")
    scope.add_argument("--all", action="store_true", help="All 250 rows")
    scope.add_argument("--scenes", type=str, help="Filter to specific scene keys, comma-separated")
    parser.add_argument("--mode", choices=["sync", "batch"], default="sync",
                        help="sync = immediate per-request (pilot); batch = 50%% off async (not yet implemented)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan + estimated cost, do NOT call the API")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Don't re-generate a slug whose PNG already exists (default: on)")
    parser.add_argument("--regenerate", action="store_true",
                        help="Force regeneration even if PNG already exists")
    parser.add_argument("--resume", action="store_true",
                        help="Resume polling an in-flight batch from var/curated_pool/batch_job.json "
                             "instead of submitting new. Ignores scope/mode flags.")
    args = parser.parse_args()

    if args.resume:
        settings = get_settings()
        if settings.gemini_api_key is None:
            raise SystemExit("GEMINI_API_KEY not set in .env")
        api_key = settings.gemini_api_key.get_secret_value()
        status = asyncio.run(_resume_batch(api_key))
        _write_log(status)
        print(f"\nResume complete. Raw PNGs in: {RAW_DIR}")
        return

    teams = [t.strip() for t in args.teams.split(",")] if args.teams else None
    scenes = [s.strip() for s in args.scenes.split(",")] if args.scenes else None
    plan = _load_plan()
    rows = _filter_rows(
        plan, teams=teams, generic=args.generic, all_=args.all, scenes=scenes
    )

    if not rows:
        raise SystemExit("No rows matched the given filters")

    price = PRICE_PER_IMAGE_SYNC if args.mode == "sync" else PRICE_PER_IMAGE_BATCH
    print(f"Matched {len(rows)} rows → estimated ${len(rows) * price:.2f} at "
          f"${price}/image ({args.mode} tier)")

    if args.dry_run:
        print("\n--- DRY RUN (no API calls, no spend) ---")
        for r in rows[:5]:
            print(f"\n[{r['slug']}] team={r['team_code']} scene={r['scene']}")
            print(f"  prompt (first 200 chars): {r['prompt'][:200]}...")
        if len(rows) > 5:
            print(f"\n... and {len(rows) - 5} more.")
        return

    settings = get_settings()
    if settings.gemini_api_key is None:
        raise SystemExit("GEMINI_API_KEY not set in .env")
    api_key = settings.gemini_api_key.get_secret_value()
    model = settings.gemini_image_model

    skip_existing = args.skip_existing and not args.regenerate
    if args.mode == "sync":
        status = asyncio.run(_run_sync(rows, api_key, model, skip_existing))
    else:
        status = asyncio.run(_run_batch(rows, api_key, model, skip_existing))
    _write_log(status)
    _print_summary(rows, status, args.mode)


if __name__ == "__main__":
    main()
