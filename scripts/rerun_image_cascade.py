"""Re-run ONLY the image-selection cascade against existing team_article rows.

Useful for iterating on prompt/validator tuning without having to wait for
fresh news to survive dedup. Reconstructs a minimal StoryEntry from the
persisted row (team, mentioned_players joined to public.players for names,
headline + intro + content for article_text + required_terms), runs the
cascade, and optionally writes the new image URL back to team_article.

Usage:
    # Dry run on last 10 articles (no DB writes, just prints results):
    ./venv/bin/python scripts/rerun_image_cascade.py --limit 10

    # Target specific article IDs:
    ./venv/bin/python scripts/rerun_image_cascade.py --article-ids 2546,2547,2548

    # Actually write the new image back to team_article:
    ./venv/bin/python scripts/rerun_image_cascade.py --limit 10 --write
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.orchestration import build_default_orchestrator
from app.schemas import ArticleDigest, PlayerMention, PublishableArticle, StoryEntry
from app.writer.image_selector import HeadshotBudget

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rerun_image_cascade")


def _fetch_articles(base: str, key: str, limit: int, ids: list[int] | None) -> list[dict]:
    params: dict[str, Any] = {
        "select": "id,headline,sub_headline,introduction,content,x_post,"
                  "bullet_points,team,author,language,image,mentioned_players",
    }
    if ids:
        params["id"] = f"in.({','.join(str(i) for i in ids)})"
        params["order"] = "id.desc"
    else:
        params["order"] = "id.desc"
        params["limit"] = limit
    r = httpx.get(
        f"{base}/rest/v1/team_article",
        params=params,
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Accept-Profile": "content",
        },
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _fetch_player_names(base: str, key: str, player_ids: list[str]) -> dict[str, str]:
    if not player_ids:
        return {}
    ids_clause = ",".join(f'"{pid}"' for pid in player_ids)
    r = httpx.get(
        f"{base}/rest/v1/players",
        params={"player_id": f"in.({ids_clause})", "select": "player_id,display_name"},
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=30.0,
    )
    r.raise_for_status()
    return {row["player_id"]: row.get("display_name") or row["player_id"] for row in r.json()}


def _build_story(row: dict, player_name_map: dict[str, str]) -> StoryEntry:
    """Reconstruct enough of a StoryEntry for the image selector to work.

    We don't have the original source_digests (those live only in-memory during
    a cycle), but we DO have the article text itself — which is what the
    selector uses for required_terms and article_text downstream. Build a
    single synthetic digest from the article body so tier 1 has something to
    search against.
    """
    player_ids = row.get("mentioned_players") or []
    player_mentions = [
        PlayerMention(id=pid, name=player_name_map.get(pid, pid)) for pid in player_ids
    ]

    synthetic_digest = ArticleDigest(
        story_id=f"rerun-{row['id']}",
        url=f"rerun://article/{row['id']}",
        title=row["headline"],
        source_name="rerun",
        summary=row.get("sub_headline") or row["headline"],
        key_facts=[],
        confidence=1.0,
        content_status="full",
    )

    team_codes = [row["team"]] if row.get("team") else []

    return StoryEntry(
        rank=1,
        cluster_headline=row["headline"],
        story_fingerprint=row.get("story_fingerprint") or f"rerun-{row['id']}",
        action="publish",
        news_value_score=0.9,
        reasoning="image-cascade rerun",
        source_digests=[synthetic_digest],
        team_codes=team_codes,
        player_mentions=player_mentions,
    )


def _build_article(row: dict) -> PublishableArticle:
    return PublishableArticle(
        team=row.get("team") or "NFL",
        language=row.get("language") or "en-US",
        headline=row["headline"],
        sub_headline=row.get("sub_headline") or "",
        introduction=row.get("introduction") or "",
        content=row.get("content") or "",
        x_post=row.get("x_post") or "",
        bullet_points=row.get("bullet_points") or "",
        story_fingerprint=row.get("story_fingerprint") or f"rerun-{row['id']}",
        author=row.get("author"),
        mentioned_players=row.get("mentioned_players") or [],
        image=row.get("image"),
    )


async def _update_image(
    client: httpx.AsyncClient, base: str, key: str, article_id: int, new_url: str
) -> bool:
    r = await client.patch(
        f"{base}/rest/v1/team_article?id=eq.{article_id}",
        json={"image": new_url},
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": "application/json",
            "Content-Profile": "content",
        },
    )
    if r.status_code >= 400:
        logger.warning("PATCH failed for #%d: %s", article_id, r.text[:200])
        return False
    return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--article-ids", type=str, help="Comma-separated IDs to target")
    parser.add_argument("--write", action="store_true", help="Actually PATCH team_article with new image")
    args = parser.parse_args()

    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()

    ids = None
    if args.article_ids:
        ids = [int(x.strip()) for x in args.article_ids.split(",") if x.strip()]

    rows = _fetch_articles(base, key, args.limit, ids)
    print(f"Fetched {len(rows)} articles")

    all_player_ids = sorted({
        pid for r in rows for pid in (r.get("mentioned_players") or [])
    })
    player_name_map = _fetch_player_names(base, key, all_player_ids)

    orch = build_default_orchestrator(settings)
    try:
        selector = orch._writer._image_selector
        if selector is None:
            print("ERROR: image_selector not configured (missing env vars?)")
            return

        # One budget across all reruns — same 50% rule as a real cycle.
        budget = HeadshotBudget.for_cycle(len(rows), ratio=0.5)
        print(f"Headshot budget for this rerun: {budget.capacity}/{len(rows)}")

        async with httpx.AsyncClient(timeout=30.0) as patch_client:
            print(f"\n{'id':>5} {'team':>4} {'→ tier':<18} {'old → new':<20} notes")
            print("-" * 120)

            results: list[dict] = []
            for row in rows:
                story = _build_story(row, player_name_map)
                article = _build_article(row)
                try:
                    result = await selector.select(
                        article, story,
                        cycle_id=f"rerun",
                        headshot_budget=budget,
                    )
                except Exception as exc:
                    print(f"{row['id']:>5} ERROR: {exc}")
                    continue

                old = row.get("image") or "(none)"
                new = result.url or "(none)"
                changed = old != new
                change_tag = "CHANGED" if changed else "same"
                print(
                    f"{row['id']:>5} {str(row.get('team') or ''):>4} {result.tier:<18} {change_tag:<8} "
                    f"{result.notes[:70]}"
                )
                print(f"      old: {old[:100]}")
                print(f"      new: {new[:100]}")

                results.append({
                    "article_id": row["id"],
                    "headline": row["headline"],
                    "tier": result.tier,
                    "old": old,
                    "new": new,
                    "changed": changed,
                    "notes": result.notes,
                })

                if args.write and result.url and changed:
                    ok = await _update_image(patch_client, base, key, row["id"], result.url)
                    print(f"      {'✓ wrote' if ok else '✗ write failed'}")

        # Summary
        print("\n=== Summary ===")
        by_tier: dict[str, int] = {}
        changed_count = 0
        for r in results:
            by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
            if r["changed"]:
                changed_count += 1
        for tier, n in sorted(by_tier.items(), key=lambda x: -x[1]):
            print(f"  {tier:<18} {n}")
        print(f"  ----")
        print(f"  changed:           {changed_count}/{len(results)}")
        if not args.write:
            print("\n(dry-run — pass --write to persist new images to team_article)")
    finally:
        await orch.close()


if __name__ == "__main__":
    asyncio.run(main())
