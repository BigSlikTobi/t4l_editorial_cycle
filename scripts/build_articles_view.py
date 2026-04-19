"""Simple article viewer — shows the N most recent team_article rows with
all their rendered fields plus images and mentioned players.

Usage:
    ./venv/bin/python scripts/build_articles_view.py [--limit 30]

Writes var/articles_view.html and prints the file:// URL.
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var" / "articles_view.html"


def _fetch(
    base: str, key: str, path: str, params: dict, profile: str | None = None
) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if profile:
        headers["Accept-Profile"] = profile
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()


def fetch_articles(limit: int) -> list[dict]:
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    return _fetch(
        base, key, "/rest/v1/team_article",
        {
            "select": "id,created_at,team,author,language,headline,sub_headline,"
                      "introduction,content,x_post,bullet_points,image,mentioned_players",
            "order": "id.desc",
            "limit": limit,
        },
        profile="content",
    )


def fetch_players(player_ids: list[str]) -> dict[str, dict]:
    if not player_ids:
        return {}
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    ids_clause = ",".join(f'"{pid}"' for pid in player_ids)
    rows = _fetch(
        base, key, "/rest/v1/players",
        {
            "player_id": f"in.({ids_clause})",
            "select": "player_id,display_name,headshot,position,latest_team",
        },
    )
    return {r["player_id"]: r for r in rows}


def _esc(v: Any) -> str:
    return "" if v is None else html.escape(str(v))


def _render_image(url: str | None, team: str | None) -> str:
    if not url:
        return "<div class='img-placeholder'>no image</div>"
    if url.startswith("asset://team_logo/"):
        code = url.rsplit("/", 1)[-1]
        return (
            f"<div class='img-placeholder logo-ph'>"
            f"<div class='logo-code'>{_esc(code)}</div>"
            f"<div class='logo-label'>team logo asset</div></div>"
        )
    if url.startswith("asset://generic/"):
        label = url.rsplit("/", 1)[-1].upper()
        return (
            f"<div class='img-placeholder logo-ph'>"
            f"<div class='logo-code'>{_esc(label)}</div>"
            f"<div class='logo-label'>generic asset</div></div>"
        )
    return f"<img class='cover' src='{_esc(url)}' alt='' loading='lazy'/>"


def _infer_tier(image: str | None) -> tuple[str, str]:
    """Return (label, css-class) for the image tier badge."""
    if not image:
        return ("no image", "tier-none")
    if image.startswith("asset://team_logo/"):
        return ("team logo", "tier-logo")
    if image.startswith("asset://generic/"):
        return ("generic NFL", "tier-generic")
    if "ai_generated" in image:
        return ("AI generated", "tier-ai")
    if "image_search" in image:
        return ("web image", "tier-web")
    if "static.www.nfl.com" in image:
        return ("player headshot", "tier-headshot")
    return ("image", "tier-web")


def _render_content(text: str | None) -> str:
    if not text:
        return "<em class='muted'>(empty)</em>"
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    return "".join(f"<p>{_esc(p)}</p>" for p in paragraphs)


def _render_bullets(value: Any) -> str:
    if not value:
        return ""
    items: list[str] = []
    if isinstance(value, list):
        items = [str(v) for v in value]
    elif isinstance(value, str):
        items = [ln.strip(" -*•\t") for ln in value.splitlines() if ln.strip()]
    if not items:
        return ""
    return "<ul class='bullets'>" + "".join(f"<li>{_esc(b)}</li>" for b in items) + "</ul>"


def _render_players(
    player_ids: list[str], player_map: dict[str, dict]
) -> str:
    if not player_ids:
        return "<span class='muted small'>none tagged</span>"
    chips: list[str] = []
    for pid in player_ids:
        info = player_map.get(pid)
        if info and info.get("headshot"):
            chips.append(
                f"<div class='player-chip'>"
                f"<img src='{_esc(info['headshot'])}' alt=''/>"
                f"<div class='pc-meta'>"
                f"<div class='pc-name'>{_esc(info.get('display_name', pid))}</div>"
                f"<div class='pc-sub'>{_esc(info.get('position', ''))} · "
                f"{_esc(info.get('latest_team', ''))}</div></div></div>"
            )
        elif info:
            chips.append(
                f"<div class='player-chip no-img'>"
                f"<div class='pc-meta'><div class='pc-name'>"
                f"{_esc(info.get('display_name', pid))}</div>"
                f"<div class='pc-sub muted'>{_esc(pid)} (no headshot)</div>"
                f"</div></div>"
            )
        else:
            chips.append(
                f"<div class='player-chip no-img'>"
                f"<div class='pc-meta'><div class='pc-name'>"
                f"{_esc(pid)}</div>"
                f"<div class='pc-sub muted'>unknown id</div></div></div>"
            )
    return "<div class='player-list'>" + "".join(chips) + "</div>"


def build_html(articles: list[dict], player_map: dict[str, dict]) -> str:
    cards: list[str] = []
    for a in articles:
        tier_label, tier_class = _infer_tier(a.get("image"))
        player_ids = a.get("mentioned_players") or []
        cards.append(
            f"""
<article class='article'>
  <div class='cover-wrap'>{_render_image(a.get('image'), a.get('team'))}
    <span class='tier-badge {tier_class}'>{_esc(tier_label)}</span>
  </div>
  <div class='body'>
    <div class='byline'>
      <span class='team-pill'>{_esc(a.get('team', '?'))}</span>
      <span class='author'>by {_esc(a.get('author') or 'unknown')}</span>
      <span class='id muted'>#{_esc(a.get('id'))}</span>
    </div>
    <h2>{_esc(a.get('headline'))}</h2>
    <p class='sub'>{_esc(a.get('sub_headline'))}</p>
    <p class='intro'>{_esc(a.get('introduction'))}</p>
    <div class='content'>{_render_content(a.get('content'))}</div>
    <details class='extras'>
      <summary>bullet points · X post · mentioned players</summary>
      <div class='extras-body'>
        <div class='ext-col'>
          <h4>Bullet points</h4>
          {_render_bullets(a.get('bullet_points'))}
        </div>
        <div class='ext-col'>
          <h4>X post</h4>
          <p class='xpost'>{_esc(a.get('x_post'))}</p>
        </div>
        <div class='ext-col'>
          <h4>Mentioned players ({len(player_ids)})</h4>
          {_render_players(player_ids, player_map)}
        </div>
      </div>
    </details>
  </div>
</article>
"""
        )

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>T4L Articles — Latest {len(articles)}</title>
<style>
:root {{
  --bg:#0f1117; --card:#1a1d27; --border:#2a2d3a; --text:#e1e4ed;
  --muted:#8b8fa3; --cyan:#22d3ee; --green:#34d399; --blue:#60a5fa;
  --purple:#a78bfa; --yellow:#fbbf24; --orange:#fb923c;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); margin: 0; padding: 32px;
       line-height: 1.6; }}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
h2 {{ font-size: 22px; margin: 6px 0 4px; line-height: 1.25; }}
h4 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
      color: var(--muted); margin: 0 0 8px; }}
.subtitle {{ color: var(--muted); margin-bottom: 24px; font-size: 14px; }}
.muted {{ color: var(--muted); }}
.small {{ font-size: 12px; }}

.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
         gap: 20px; }}

.article {{ background: var(--card); border: 1px solid var(--border);
            border-radius: 14px; overflow: hidden; display: flex;
            flex-direction: column; }}
.cover-wrap {{ position: relative; aspect-ratio: 16/9; background: #0d0f14;
               overflow: hidden; }}
.cover {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.img-placeholder {{ width:100%; height:100%; display:flex; align-items:center;
                    justify-content:center; color: var(--muted); font-size: 14px;
                    background: linear-gradient(135deg, #1f2433, #0d0f14); }}
.logo-ph {{ flex-direction: column; gap: 4px; }}
.logo-code {{ font-size: 42px; font-weight: 800; color: var(--cyan); letter-spacing: 2px; }}
.logo-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
               letter-spacing: 2px; }}

.tier-badge {{ position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.7);
               padding: 4px 10px; border-radius: 10px; font-size: 11px;
               text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }}
.tier-headshot {{ color: var(--green); }}
.tier-web      {{ color: var(--blue); }}
.tier-ai       {{ color: var(--purple); }}
.tier-logo     {{ color: var(--orange); }}
.tier-generic  {{ color: var(--yellow); }}
.tier-none     {{ color: var(--muted); }}

.body {{ padding: 18px 20px; flex: 1; display: flex; flex-direction: column; }}
.byline {{ display: flex; gap: 10px; align-items: center; font-size: 13px;
           margin-bottom: 4px; flex-wrap: wrap; }}
.team-pill {{ background: #1e293b; color: var(--cyan); padding: 2px 10px;
              border-radius: 6px; font-weight: 700; font-size: 12px; letter-spacing: 1px; }}
.author {{ color: var(--text); font-style: italic; }}
.id {{ margin-left: auto; font-size: 12px; font-family: 'SF Mono', monospace; }}

.sub {{ color: var(--muted); margin: 4px 0 12px; font-size: 14px; font-style: italic; }}
.intro {{ font-size: 15px; color: #dfe3ee; margin: 0 0 12px;
          padding: 10px 14px; border-left: 3px solid var(--cyan);
          background: #11151c; border-radius: 0 6px 6px 0; }}
.content {{ font-size: 14px; }}
.content p {{ margin: 0 0 10px; }}

.extras {{ margin-top: auto; border-top: 1px solid var(--border); padding-top: 12px; }}
.extras summary {{ cursor: pointer; color: var(--muted); font-size: 12px;
                   text-transform: uppercase; letter-spacing: 1px; }}
.extras summary::before {{ content: '\\25B6'; margin-right: 8px; font-size: 10px;
                           display: inline-block; transition: transform 0.2s; }}
.extras[open] summary::before {{ transform: rotate(90deg); }}
.extras-body {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
                padding-top: 12px; }}
.ext-col:first-child {{ grid-column: 1 / -1; }}
.ext-col:nth-child(2), .ext-col:nth-child(3) {{ grid-column: auto; }}

.bullets {{ margin: 0; padding-left: 18px; font-size: 13px; }}
.bullets li {{ margin-bottom: 4px; }}
.xpost {{ margin: 0; padding: 8px 12px; background: #11151c;
          border-radius: 6px; font-size: 13px; border-left: 3px solid var(--blue); }}

.player-list {{ display: flex; flex-direction: column; gap: 8px; }}
.player-chip {{ display: flex; gap: 10px; align-items: center;
                background: #11151c; border: 1px solid var(--border);
                border-radius: 8px; padding: 6px 10px; }}
.player-chip img {{ width: 40px; height: 40px; border-radius: 50%;
                    object-fit: cover; background: #0d0f14; }}
.player-chip.no-img {{ padding-left: 14px; }}
.pc-name {{ font-weight: 600; font-size: 13px; }}
.pc-sub {{ font-size: 11px; color: var(--muted); }}

.kpis {{ display: flex; gap: 14px; margin: 20px 0 28px; flex-wrap: wrap; }}
.kpi {{ background: var(--card); border: 1px solid var(--border);
        border-radius: 10px; padding: 12px 18px; }}
.kpi-val {{ font-size: 22px; font-weight: 700; color: var(--cyan); }}
.kpi-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
              letter-spacing: 1px; }}
</style>
</head>
<body>
  <h1>T4L Articles</h1>
  <div class='subtitle'>Latest {len(articles)} articles from <code>content.team_article</code>.
    Expand the details section for bullets, X posts, and mentioned players.</div>
  <div class='kpis'>
    <div class='kpi'><div class='kpi-val'>{len(articles)}</div><div class='kpi-label'>Articles</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-headshot')}</div><div class='kpi-label'>Headshot</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-web')}</div><div class='kpi-label'>Web image</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-ai')}</div><div class='kpi-label'>AI generated</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-logo')}</div><div class='kpi-label'>Team logo</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-generic')}</div><div class='kpi-label'>Generic NFL</div></div>
    <div class='kpi'><div class='kpi-val'>{_count_by_tier(articles, 'tier-none')}</div><div class='kpi-label'>No image</div></div>
  </div>
  <div class='grid'>
    {''.join(cards)}
  </div>
</body>
</html>
"""


def _count_by_tier(articles: list[dict], tier_class: str) -> int:
    return sum(1 for a in articles if _infer_tier(a.get("image"))[1] == tier_class)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30, help="Max articles to show")
    args = parser.parse_args()

    articles = fetch_articles(args.limit)
    print(f"Fetched {len(articles)} articles")

    all_player_ids = sorted({
        pid for a in articles for pid in (a.get("mentioned_players") or [])
    })
    player_map = fetch_players(all_player_ids)
    print(f"Resolved {len(player_map)}/{len(all_player_ids)} players")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(articles, player_map))
    print(f"Wrote {OUTPUT}")
    print(f"Open: file://{OUTPUT}")


if __name__ == "__main__":
    main()
