"""Cycle report: run analysis (plan + reasoning + sources) + complete articles.

Reads var/output.json from the most recent `editorial-cycle run --output-json`
and the matching articles from `content.team_article` (both languages per story).

Usage:
    ./venv/bin/python scripts/build_cycle_report.py [--input var/output.json]

Writes var/cycle_report.html and prints the file:// URL.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "var" / "output.json"
OUTPUT = ROOT / "var" / "cycle_report.html"


def _fetch(base, key, path, params, profile=None):
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if profile:
        headers["Accept-Profile"] = profile
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()


def fetch_articles_by_fps(fingerprints: list[str]) -> list[dict]:
    if not fingerprints:
        return []
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    ids = ",".join(f'"{fp}"' for fp in fingerprints)
    return _fetch(
        base, key, "/rest/v1/team_article",
        {
            "select": "id,story_fingerprint,team,author,language,headline,"
                      "sub_headline,introduction,content,x_post,bullet_points,"
                      "image,mentioned_players,sources",
            "story_fingerprint": f"in.({ids})",
            "order": "id.desc",
        },
        profile="content",
    )


def fetch_players(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    ids_clause = ",".join(f'"{pid}"' for pid in ids)
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


def _infer_tier(image: str | None) -> tuple[str, str]:
    if not image:
        return ("no image", "tier-none")
    if image.startswith("asset://team_logo/"):
        return ("team logo", "tier-logo")
    if image.startswith("asset://generic/"):
        return ("generic NFL", "tier-generic")
    if "curated" in image:
        return ("curated pool", "tier-curated")
    if "image_search" in image:
        return ("web image", "tier-web")
    if "static.www.nfl.com" in image:
        return ("player headshot", "tier-headshot")
    return ("image", "tier-web")


def _render_image(url: str | None) -> str:
    if not url:
        return "<div class='img-ph'>no image</div>"
    if url.startswith("asset://team_logo/"):
        code = url.rsplit("/", 1)[-1]
        return f"<div class='img-ph logo'><div class='logo-code'>{_esc(code)}</div><div class='logo-label'>team logo asset</div></div>"
    if url.startswith("asset://generic/"):
        label = url.rsplit("/", 1)[-1].upper()
        return f"<div class='img-ph logo'><div class='logo-code'>{_esc(label)}</div><div class='logo-label'>generic asset</div></div>"
    return f"<img class='cover' src='{_esc(url)}' alt='' loading='lazy'/>"


def _render_content(text: str | None) -> str:
    if not text:
        return "<em class='muted'>(empty)</em>"
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    return "".join(f"<p>{_esc(p)}</p>" for p in paragraphs)


def _render_bullets(value: Any) -> str:
    if not value:
        return ""
    items = value if isinstance(value, list) else [
        ln.strip(" -*\u2022\t") for ln in str(value).splitlines() if ln.strip()
    ]
    if not items:
        return ""
    return "<ul class='bullets'>" + "".join(f"<li>{_esc(b)}</li>" for b in items) + "</ul>"


def _render_players(ids: list[str], pmap: dict[str, dict]) -> str:
    if not ids:
        return "<span class='muted small'>none tagged</span>"
    chips = []
    for pid in ids:
        info = pmap.get(pid)
        if info and info.get("headshot"):
            chips.append(
                f"<div class='chip'><img src='{_esc(info['headshot'])}' alt=''/>"
                f"<div><div class='pn'>{_esc(info.get('display_name', pid))}</div>"
                f"<div class='ps'>{_esc(info.get('position', ''))} · {_esc(info.get('latest_team', ''))}</div></div></div>"
            )
        elif info:
            chips.append(
                f"<div class='chip no-img'><div><div class='pn'>{_esc(info.get('display_name', pid))}</div>"
                f"<div class='ps muted'>{_esc(pid)}</div></div></div>"
            )
        else:
            chips.append(
                f"<div class='chip no-img'><div><div class='pn'>{_esc(pid)}</div>"
                f"<div class='ps muted'>unknown</div></div></div>"
            )
    return "<div class='chips'>" + "".join(chips) + "</div>"


def _render_source_digest(d: dict) -> str:
    facts = d.get("key_facts") or []
    facts_html = ""
    if facts:
        facts_html = "<ul class='facts'>" + "".join(f"<li>{_esc(f)}</li>" for f in facts) + "</ul>"
    teams = d.get("team_mentions") or []
    teams_html = (
        "<div class='team-mentions'>" +
        "".join(f"<span class='team-pill'>{_esc(t)}</span>" for t in teams) +
        "</div>"
    ) if teams else ""
    conf = d.get("confidence")
    conf_html = f"<span class='conf'>conf {conf:.2f}</span>" if isinstance(conf, (int, float)) else ""
    status = d.get("content_status") or ""
    status_html = f"<span class='status'>{_esc(status)}</span>" if status else ""
    return f"""
<div class='digest'>
  <div class='digest-head'>
    <a href='{_esc(d.get('url'))}' target='_blank' rel='noopener'>{_esc(d.get('title') or d.get('url'))}</a>
    <span class='muted small'>&middot; {_esc(d.get('source_name'))}</span>
    {conf_html}{status_html}
  </div>
  <p class='summary'>{_esc(d.get('summary'))}</p>
  {facts_html}
  {teams_html}
</div>
"""


def _render_article(a: dict, pmap: dict[str, dict]) -> str:
    tier_label, tier_class = _infer_tier(a.get("image"))
    pids = a.get("mentioned_players") or []
    lang = a.get("language") or "?"
    lang_flag = "\U0001F1FA\U0001F1F8" if lang == "en-US" else ("\U0001F1E9\U0001F1EA" if lang == "de-DE" else "")
    return f"""
<div class='article'>
  <div class='cover-wrap'>{_render_image(a.get('image'))}
    <span class='tier-badge {tier_class}'>{_esc(tier_label)}</span>
    <span class='lang-badge'>{lang_flag} {_esc(lang)}</span>
  </div>
  <div class='body'>
    <div class='byline'>
      <span class='team-pill'>{_esc(a.get('team', '?'))}</span>
      <span class='author'>by {_esc(a.get('author') or 'unknown')}</span>
      <span class='id muted'>#{_esc(a.get('id'))}</span>
    </div>
    <h3>{_esc(a.get('headline'))}</h3>
    <p class='sub'>{_esc(a.get('sub_headline'))}</p>
    <p class='intro'>{_esc(a.get('introduction'))}</p>
    <div class='content'>{_render_content(a.get('content'))}</div>
    <details class='extras'>
      <summary>bullet points &middot; X post &middot; mentioned players ({len(pids)})</summary>
      <div class='extras-body'>
        <div><h4>Bullet points</h4>{_render_bullets(a.get('bullet_points'))}</div>
        <div><h4>X post</h4><p class='xpost'>{_esc(a.get('x_post'))}</p></div>
        <div><h4>Mentioned players</h4>{_render_players(pids, pmap)}</div>
      </div>
    </details>
  </div>
</div>
"""


def _render_story(
    story: dict,
    articles_by_fp: dict[str, list[dict]],
    pmap: dict[str, dict],
) -> str:
    fp = story["story_fingerprint"]
    arts = sorted(articles_by_fp.get(fp, []), key=lambda a: a.get("language") != "en-US")
    score = story.get("news_value_score")
    score_html = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
    teams = story.get("team_codes") or []
    teams_html = "".join(f"<span class='team-pill'>{_esc(t)}</span>" for t in teams)
    pmentions = story.get("player_mentions") or []
    players_html = "".join(
        f"<span class='player-tag'>{_esc(p.get('name'))}</span>"
        for p in pmentions
    )
    digests_html = "".join(_render_source_digest(d) for d in story.get("source_digests") or [])
    action = story.get("action", "?")
    existing = story.get("existing_article_id")
    existing_html = (
        f"<span class='existing'>updating #{_esc(existing)}</span>" if existing else ""
    )
    arts_html = "".join(_render_article(a, pmap) for a in arts) or "<em class='muted'>(no articles persisted)</em>"
    return f"""
<section class='story'>
  <div class='story-head'>
    <div class='rank'>#{_esc(story.get('rank'))}</div>
    <div class='story-meta'>
      <h2>{_esc(story.get('cluster_headline'))}</h2>
      <div class='story-badges'>
        <span class='action action-{_esc(action)}'>{_esc(action)}</span>
        <span class='score'>score {score_html}</span>
        <span class='fp muted small'>fp {_esc(fp)}</span>
        {existing_html}
      </div>
    </div>
  </div>
  <div class='story-body'>
    <div class='story-left'>
      <h4>Reasoning</h4>
      <p class='reasoning'>{_esc(story.get('reasoning'))}</p>
      <h4>Team codes ({len(teams)})</h4>
      <div class='team-mentions'>{teams_html}</div>
      <h4>Player mentions ({len(pmentions)})</h4>
      <div class='player-tags'>{players_html or '<span class=\"muted small\">none</span>'}</div>
      <h4>Source digests ({len(story.get('source_digests') or [])})</h4>
      <div class='digests'>{digests_html}</div>
    </div>
    <div class='story-right'>
      <div class='articles-grid'>{arts_html}</div>
    </div>
  </div>
</section>
"""


def build_html(output_data: dict, articles: list[dict], pmap: dict[str, dict]) -> str:
    plan = output_data.get("plan", {})
    published_stories = plan.get("stories") or []
    skipped_stories = plan.get("skipped_stories") or []
    stories = [*published_stories, *skipped_stories]
    articles_by_fp: dict[str, list[dict]] = {}
    for a in articles:
        articles_by_fp.setdefault(a.get("story_fingerprint") or "", []).append(a)

    cycle_id = output_data.get("cycle_id", "?")
    generated_at = output_data.get("generated_at", "?")
    written = output_data.get("articles_written", len(articles))
    updated = output_data.get("articles_updated", 0)
    prevented = output_data.get("prevented_duplicates", 0)
    warnings = output_data.get("warnings") or []

    tier_counts = {"tier-curated": 0, "tier-web": 0, "tier-headshot": 0,
                   "tier-logo": 0, "tier-generic": 0, "tier-none": 0}
    for a in articles:
        _, cls = _infer_tier(a.get("image"))
        tier_counts[cls] = tier_counts.get(cls, 0) + 1

    lang_counts = {"en-US": 0, "de-DE": 0}
    for a in articles:
        lang_counts[a.get("language", "?")] = lang_counts.get(a.get("language", "?"), 0) + 1

    stories_html = "".join(_render_story(s, articles_by_fp, pmap) for s in stories)

    warn_html = ""
    if warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
        warn_html = f"<div class='warnings'><h4>Warnings ({len(warnings)})</h4><ul>{items}</ul></div>"

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>T4L Cycle Report &mdash; {_esc(cycle_id)[:8]}</title>
<style>
:root {{
  --bg:#0f1117; --card:#1a1d27; --card2:#141721; --border:#2a2d3a;
  --text:#e1e4ed; --muted:#8b8fa3; --cyan:#22d3ee; --green:#34d399;
  --blue:#60a5fa; --purple:#a78bfa; --yellow:#fbbf24; --orange:#fb923c;
  --red:#f87171; --pink:#f472b6;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); margin: 0; padding: 24px;
       line-height: 1.55; }}
h1 {{ margin: 0 0 4px; font-size: 26px; }}
h2 {{ font-size: 20px; margin: 0 0 6px; line-height: 1.25; }}
h3 {{ font-size: 17px; margin: 4px 0 4px; line-height: 1.3; }}
h4 {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
      color: var(--muted); margin: 16px 0 6px; }}
a {{ color: var(--blue); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.muted {{ color: var(--muted); }}
.small {{ font-size: 12px; }}

.header {{ margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.subtitle {{ color: var(--muted); font-size: 13px; margin-top: 4px; font-family: 'SF Mono', monospace; }}

.kpis {{ display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }}
.kpi {{ background: var(--card); border: 1px solid var(--border);
        border-radius: 10px; padding: 10px 16px; min-width: 80px; }}
.kpi-val {{ font-size: 20px; font-weight: 700; color: var(--cyan); }}
.kpi-label {{ font-size: 10px; color: var(--muted); text-transform: uppercase;
              letter-spacing: 1px; }}
.kpi.written .kpi-val {{ color: var(--green); }}
.kpi.updated .kpi-val {{ color: var(--blue); }}
.kpi.prevented .kpi-val {{ color: var(--yellow); }}
.kpi.curated .kpi-val {{ color: var(--pink); }}
.kpi.web .kpi-val {{ color: var(--blue); }}
.kpi.headshot .kpi-val {{ color: var(--green); }}
.kpi.logo .kpi-val {{ color: var(--orange); }}
.kpi.generic .kpi-val {{ color: var(--yellow); }}

.warnings {{ background: #2a1f1f; border: 1px solid var(--red);
             border-radius: 10px; padding: 10px 16px; margin-bottom: 16px; }}
.warnings h4 {{ color: var(--red); margin: 0 0 6px; }}
.warnings ul {{ margin: 0; padding-left: 20px; font-size: 13px; }}

.story {{ background: var(--card2); border: 1px solid var(--border);
          border-radius: 14px; padding: 20px; margin-bottom: 22px; }}
.story-head {{ display: flex; gap: 16px; align-items: flex-start;
               padding-bottom: 14px; border-bottom: 1px solid var(--border); }}
.rank {{ font-size: 28px; font-weight: 800; color: var(--cyan);
         min-width: 50px; font-family: 'SF Mono', monospace; }}
.story-meta {{ flex: 1; }}
.story-badges {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 6px; }}
.action {{ font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 6px;
          text-transform: uppercase; letter-spacing: 0.5px; }}
.action-publish {{ background: #153b2c; color: var(--green); }}
.action-update {{ background: #1b2f4a; color: var(--blue); }}
.action-skip {{ background: #3a2c17; color: var(--orange); }}
.score {{ background: #1e293b; color: var(--cyan); padding: 3px 10px;
          border-radius: 6px; font-size: 12px; font-weight: 600;
          font-family: 'SF Mono', monospace; }}
.existing {{ background: #1b2f4a; color: var(--blue); padding: 3px 10px;
             border-radius: 6px; font-size: 11px; }}
.fp {{ font-family: 'SF Mono', monospace; }}

.story-body {{ display: grid; grid-template-columns: 400px 1fr; gap: 24px;
               padding-top: 14px; }}
@media (max-width: 1200px) {{ .story-body {{ grid-template-columns: 1fr; }} }}

.story-left h4:first-child {{ margin-top: 0; }}
.reasoning {{ font-size: 13px; color: #dfe3ee; margin: 0;
              padding: 10px 12px; border-left: 3px solid var(--purple);
              background: #11151c; border-radius: 0 6px 6px 0; }}

.team-mentions {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.team-pill {{ background: #1e293b; color: var(--cyan); padding: 2px 8px;
              border-radius: 5px; font-weight: 700; font-size: 11px;
              letter-spacing: 0.5px; }}
.player-tags {{ display: flex; flex-wrap: wrap; gap: 4px; }}
.player-tag {{ background: #2a1f3a; color: var(--purple); padding: 2px 8px;
               border-radius: 5px; font-size: 11px; }}

.digests {{ display: flex; flex-direction: column; gap: 8px; }}
.digest {{ background: var(--card); border: 1px solid var(--border);
           border-radius: 8px; padding: 10px 12px; font-size: 12px; }}
.digest-head {{ display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap;
                margin-bottom: 4px; }}
.digest-head a {{ font-weight: 600; font-size: 13px; }}
.summary {{ margin: 4px 0; font-size: 12px; color: #c1c5d1; }}
.facts {{ margin: 6px 0 4px; padding-left: 18px; font-size: 11px; color: var(--muted); }}
.facts li {{ margin-bottom: 2px; }}
.conf {{ background: #153b2c; color: var(--green); padding: 1px 6px;
         border-radius: 4px; font-size: 10px; font-family: 'SF Mono', monospace; }}
.status {{ background: #2a2d3a; color: var(--muted); padding: 1px 6px;
           border-radius: 4px; font-size: 10px; }}

.articles-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 900px) {{ .articles-grid {{ grid-template-columns: 1fr; }} }}

.article {{ background: var(--card); border: 1px solid var(--border);
            border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }}
.cover-wrap {{ position: relative; aspect-ratio: 16/9; background: #0d0f14;
               overflow: hidden; }}
.cover {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.img-ph {{ width: 100%; height: 100%; display: flex; align-items: center;
           justify-content: center; color: var(--muted); font-size: 13px;
           background: linear-gradient(135deg, #1f2433, #0d0f14);
           flex-direction: column; gap: 4px; }}
.logo-code {{ font-size: 36px; font-weight: 800; color: var(--cyan); letter-spacing: 2px; }}
.logo-label {{ font-size: 10px; color: var(--muted); text-transform: uppercase;
               letter-spacing: 2px; }}

.tier-badge {{ position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.75);
               padding: 3px 8px; border-radius: 8px; font-size: 10px;
               text-transform: uppercase; letter-spacing: 0.5px; font-weight: 700; }}
.tier-curated  {{ color: var(--pink); }}
.tier-web      {{ color: var(--blue); }}
.tier-headshot {{ color: var(--green); }}
.tier-logo     {{ color: var(--orange); }}
.tier-generic  {{ color: var(--yellow); }}
.tier-none     {{ color: var(--muted); }}

.lang-badge {{ position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.75);
               padding: 3px 8px; border-radius: 8px; font-size: 10px;
               font-family: 'SF Mono', monospace; }}

.body {{ padding: 14px 16px; flex: 1; display: flex; flex-direction: column; }}
.byline {{ display: flex; gap: 8px; align-items: center; font-size: 12px;
           margin-bottom: 6px; flex-wrap: wrap; }}
.byline .team-pill {{ font-size: 11px; }}
.author {{ font-style: italic; font-size: 12px; }}
.id {{ margin-left: auto; font-size: 11px; font-family: 'SF Mono', monospace; }}

.sub {{ color: var(--muted); margin: 2px 0 10px; font-size: 13px; font-style: italic; }}
.intro {{ font-size: 13px; color: #dfe3ee; margin: 0 0 10px;
          padding: 8px 12px; border-left: 3px solid var(--cyan);
          background: #11151c; border-radius: 0 6px 6px 0; }}
.content {{ font-size: 13px; }}
.content p {{ margin: 0 0 8px; }}

.extras {{ margin-top: auto; border-top: 1px solid var(--border); padding-top: 10px; }}
.extras summary {{ cursor: pointer; color: var(--muted); font-size: 11px;
                   text-transform: uppercase; letter-spacing: 1px; }}
.extras summary::before {{ content: '\\25B6'; margin-right: 6px; font-size: 9px;
                           display: inline-block; transition: transform 0.2s; }}
.extras[open] summary::before {{ transform: rotate(90deg); }}
.extras-body {{ display: grid; grid-template-columns: 1fr; gap: 12px; padding-top: 10px; }}
.bullets {{ margin: 0; padding-left: 16px; font-size: 12px; }}
.bullets li {{ margin-bottom: 3px; }}
.xpost {{ margin: 0; padding: 6px 10px; background: #11151c;
          border-radius: 6px; font-size: 12px; border-left: 3px solid var(--blue); }}

.chips {{ display: flex; flex-direction: column; gap: 6px; }}
.chip {{ display: flex; gap: 8px; align-items: center;
         background: #11151c; border: 1px solid var(--border);
         border-radius: 6px; padding: 5px 8px; }}
.chip img {{ width: 32px; height: 32px; border-radius: 50%;
             object-fit: cover; background: #0d0f14; }}
.chip.no-img {{ padding-left: 12px; }}
.pn {{ font-weight: 600; font-size: 12px; }}
.ps {{ font-size: 10px; color: var(--muted); }}
</style>
</head>
<body>
  <div class='header'>
    <h1>T4L Cycle Report</h1>
    <div class='subtitle'>cycle <code>{_esc(cycle_id)}</code> &middot; generated {_esc(generated_at)}</div>
  </div>

  <div class='kpis'>
    <div class='kpi'><div class='kpi-val'>{len(published_stories)}</div><div class='kpi-label'>Published</div></div>
    <div class='kpi'><div class='kpi-val'>{len(skipped_stories)}</div><div class='kpi-label'>Skipped</div></div>
    <div class='kpi written'><div class='kpi-val'>{written}</div><div class='kpi-label'>Written</div></div>
    <div class='kpi updated'><div class='kpi-val'>{updated}</div><div class='kpi-label'>Updated</div></div>
    <div class='kpi prevented'><div class='kpi-val'>{prevented}</div><div class='kpi-label'>Duplicates prevented</div></div>
    <div class='kpi'><div class='kpi-val'>{lang_counts.get('en-US', 0)}</div><div class='kpi-label'>en-US</div></div>
    <div class='kpi'><div class='kpi-val'>{lang_counts.get('de-DE', 0)}</div><div class='kpi-label'>de-DE</div></div>
  </div>

  <div class='kpis'>
    <div class='kpi curated'><div class='kpi-val'>{tier_counts.get('tier-curated', 0)}</div><div class='kpi-label'>Curated</div></div>
    <div class='kpi web'><div class='kpi-val'>{tier_counts.get('tier-web', 0)}</div><div class='kpi-label'>Web</div></div>
    <div class='kpi headshot'><div class='kpi-val'>{tier_counts.get('tier-headshot', 0)}</div><div class='kpi-label'>Headshot</div></div>
    <div class='kpi logo'><div class='kpi-val'>{tier_counts.get('tier-logo', 0)}</div><div class='kpi-label'>Logo</div></div>
    <div class='kpi generic'><div class='kpi-val'>{tier_counts.get('tier-generic', 0)}</div><div class='kpi-label'>Generic</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-none', 0)}</div><div class='kpi-label'>None</div></div>
  </div>

  {warn_html}

  {stories_html}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="cycle output JSON (from --output-json)")
    args = parser.parse_args()

    data = json.loads(args.input.read_text())
    stories = data.get("plan", {}).get("stories", [])
    fps = [s["story_fingerprint"] for s in stories if s.get("story_fingerprint")]
    print(f"Loaded cycle {data.get('cycle_id', '?')[:8]} with {len(stories)} stories")

    articles = fetch_articles_by_fps(fps)
    print(f"Fetched {len(articles)} articles from Supabase")

    all_pids = sorted({
        pid for a in articles for pid in (a.get("mentioned_players") or [])
    })
    pmap = fetch_players(all_pids)
    print(f"Resolved {len(pmap)}/{len(all_pids)} players")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(data, articles, pmap))
    print(f"Wrote {OUTPUT}")
    print(f"Open: file://{OUTPUT}")


if __name__ == "__main__":
    main()
