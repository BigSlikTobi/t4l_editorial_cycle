"""Pre-publish overview: every team_article in Supabase, paired EN/DE per
story fingerprint, joined with editorial_state and the orchestrator's
reasoning/source digests from local cycle JSONs.

Reads all var/test_runs/cycle_*.json + var/output.json for trace context.
Pulls every row of content.team_article + matching editorial_state rows.

Usage:
    ./venv/bin/python scripts/build_overview_report.py [--limit N]

Writes var/overview_report.html and prints the file:// URL.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "var" / "overview_report.html"
TRACE_GLOBS = [
    str(ROOT / "var" / "test_runs" / "cycle_*.json"),
    str(ROOT / "var" / "output.json"),
]


# ---------- Supabase fetch helpers ----------

def _fetch(base: str, key: str, path: str, params: dict, profile: str | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if profile:
        headers["Accept-Profile"] = profile
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=60.0)
    r.raise_for_status()
    return r.json()


def fetch_all_articles(limit: int | None = None) -> list[dict]:
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    page_size = 1000
    all_rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "select": "id,created_at,team,author,language,headline,sub_headline,"
                      "introduction,content,x_post,bullet_points,image,"
                      "mentioned_players,sources,story_fingerprint",
            "order": "id.desc",
            "limit": page_size,
            "offset": offset,
        }
        rows = _fetch(base, key, "/rest/v1/team_article", params)
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
        if limit and len(all_rows) >= limit:
            all_rows = all_rows[:limit]
            break
    return all_rows


def fetch_editorial_state(fingerprints: list[str]) -> dict[str, dict]:
    if not fingerprints:
        return {}
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    out: dict[str, dict] = {}
    chunk = 200
    for i in range(0, len(fingerprints), chunk):
        slice_ = fingerprints[i : i + chunk]
        ids = ",".join(f'"{fp}"' for fp in slice_)
        rows = _fetch(
            base, key, "/rest/v1/editorial_state",
            {"story_fingerprint": f"in.({ids})", "select": "*"},
        )
        for r in rows:
            out[r["story_fingerprint"]] = r
    return out


def fetch_players(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    s = get_settings()
    base = str(s.supabase_url).rstrip("/")
    key = s.supabase_service_role_key.get_secret_value()
    out: dict[str, dict] = {}
    chunk = 200
    for i in range(0, len(ids), chunk):
        slice_ = ids[i : i + chunk]
        ids_clause = ",".join(f'"{pid}"' for pid in slice_)
        rows = _fetch(
            base, key, "/rest/v1/players",
            {
                "player_id": f"in.({ids_clause})",
                "select": "player_id,display_name,headshot,position,latest_team",
            },
        )
        for r in rows:
            out[r["player_id"]] = r
    return out


# ---------- Trace ingest ----------

def load_traces() -> dict[str, dict]:
    """Map story_fingerprint -> latest story entry from cycle JSONs.

    Each entry adds: cycle_id, generated_at, action, reasoning,
    source_digests, news_value_score, rank, team_codes, player_mentions,
    existing_article_id, was_skipped (bool).
    """
    files: list[Path] = []
    for pattern in TRACE_GLOBS:
        files.extend(Path(p) for p in glob.glob(pattern))
    files = sorted(set(files))
    by_fp: dict[str, dict] = {}
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        cycle_id = data.get("cycle_id", "?")
        generated_at = data.get("generated_at", "?")
        plan = data.get("plan", {}) or {}
        for entry in plan.get("stories", []) or []:
            fp = entry.get("story_fingerprint")
            if not fp:
                continue
            existing = by_fp.get(fp)
            if not existing or generated_at > existing.get("generated_at", ""):
                by_fp[fp] = {**entry, "cycle_id": cycle_id,
                             "generated_at": generated_at, "was_skipped": False,
                             "_src_file": f.name}
        for entry in plan.get("skipped_stories", []) or []:
            fp = entry.get("story_fingerprint")
            if not fp or fp in by_fp:
                continue
            by_fp[fp] = {**entry, "cycle_id": cycle_id,
                         "generated_at": generated_at, "was_skipped": True,
                         "_src_file": f.name}
    return by_fp


# ---------- Render helpers ----------

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
    if "image_search" in image or "wikimedia" in image or "wikipedia" in image:
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


def _render_paragraphs(text: str | None) -> str:
    if not text:
        return "<em class='muted'>(empty)</em>"
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    return "".join(f"<p>{_esc(p)}</p>" for p in paragraphs)


def _render_bullets(value: Any) -> str:
    if not value:
        return "<em class='muted small'>none</em>"
    items = value if isinstance(value, list) else [
        ln.strip(" -*•\t") for ln in str(value).splitlines() if ln.strip()
    ]
    if not items:
        return "<em class='muted small'>none</em>"
    return "<ul class='bullets'>" + "".join(f"<li>{_esc(b)}</li>" for b in items) + "</ul>"


def _render_sources(sources: Any) -> str:
    if not sources:
        return "<em class='muted small'>none</em>"
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except Exception:
            return _esc(sources)
    if not isinstance(sources, list):
        return _esc(sources)
    items = []
    for s in sources:
        name = _esc(s.get("name") or "")
        url = _esc(s.get("url") or "")
        items.append(
            f"<li><a href='{url}' target='_blank' rel='noopener'>{name or url}</a>"
            f"<div class='src-url muted small'>{url}</div></li>"
        )
    return "<ul class='sources'>" + "".join(items) + "</ul>"


def _render_players_chips(ids: list[str], pmap: dict[str, dict]) -> str:
    if not ids:
        return "<span class='muted small'>none</span>"
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
                f"<div class='ps muted'>unknown id</div></div></div>"
            )
    return "<div class='chips'>" + "".join(chips) + "</div>"


def _render_digest(d: dict) -> str:
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
    url = _esc(d.get("url") or "")
    title = _esc(d.get("title") or url)
    return f"""
<div class='digest'>
  <div class='digest-head'>
    <a href='{url}' target='_blank' rel='noopener'>{title}</a>
    <span class='muted small'>· {_esc(d.get('source_name'))}</span>
    {conf_html}{status_html}
  </div>
  <p class='summary'>{_esc(d.get('summary'))}</p>
  {facts_html}
  {teams_html}
</div>
"""


def _render_trace(trace: dict | None) -> str:
    if not trace:
        return "<div class='trace none'><em class='muted small'>No orchestrator trace found in local cycle JSONs (may have been generated outside this workspace).</em></div>"
    score = trace.get("news_value_score")
    score_html = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
    digests = trace.get("source_digests") or []
    teams = trace.get("team_codes") or []
    pmentions = trace.get("player_mentions") or []
    action = trace.get("action") or ("skipped" if trace.get("was_skipped") else "?")
    return f"""
<div class='trace'>
  <div class='trace-head'>
    <span class='action action-{_esc(action)}'>{_esc(action)}</span>
    <span class='score'>news value {score_html}</span>
    <span class='muted small'>cycle {_esc(trace.get('cycle_id', '?'))[:8]}</span>
    <span class='muted small'>· {_esc(trace.get('generated_at', '?'))}</span>
    {f"<span class='muted small'>· src {_esc(trace.get('_src_file'))}</span>" if trace.get('_src_file') else ''}
  </div>
  <h5>Orchestrator reasoning</h5>
  <p class='reasoning'>{_esc(trace.get('reasoning') or '')}</p>
  <div class='trace-meta'>
    <div><h5>Team codes ({len(teams)})</h5>
      <div class='team-mentions'>{''.join(f"<span class='team-pill'>{_esc(t)}</span>" for t in teams) or '<span class=\"muted small\">none</span>'}</div>
    </div>
    <div><h5>Player mentions ({len(pmentions)})</h5>
      <div class='player-tags'>{''.join(f"<span class='player-tag'>{_esc(p.get('name') if isinstance(p, dict) else p)}</span>" for p in pmentions) or '<span class=\"muted small\">none</span>'}</div>
    </div>
  </div>
  <h5>Source digests ({len(digests)})</h5>
  <div class='digests'>{''.join(_render_digest(d) for d in digests) or '<em class=\"muted small\">none</em>'}</div>
</div>
"""


def _render_state(state: dict | None) -> str:
    if not state:
        return "<div class='state state-orphan'><em class='muted small'>No editorial_state record (this article exists in team_article but isn't tracked in cross-cycle state).</em></div>"
    src_urls = state.get("source_urls") or []
    src_html = ""
    if src_urls:
        src_html = "<ul class='src-urls'>" + "".join(
            f"<li><a href='{_esc(u)}' target='_blank' rel='noopener'>{_esc(u)}</a></li>"
            for u in src_urls
        ) + "</ul>"
    return f"""
<div class='state'>
  <div class='state-grid'>
    <div><span class='state-label'>state id</span><span class='state-val'>#{_esc(state.get('id'))}</span></div>
    <div><span class='state-label'>canonical article id</span><span class='state-val'>#{_esc(state.get('supabase_article_id'))}</span></div>
    <div><span class='state-label'>first published</span><span class='state-val'>{_esc(state.get('published_at'))}</span></div>
    <div><span class='state-label'>last updated</span><span class='state-val'>{_esc(state.get('last_updated_at'))}</span></div>
    <div><span class='state-label'>cycle id</span><span class='state-val'>{_esc(state.get('cycle_id'))}</span></div>
    <div><span class='state-label'>tracked source urls</span><span class='state-val'>{len(src_urls)}</span></div>
  </div>
  {src_html}
</div>
"""


def _render_article_card(a: dict, pmap: dict[str, dict]) -> str:
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
    <div class='content'>{_render_paragraphs(a.get('content'))}</div>
    <details class='extras'>
      <summary>bullets · X post · sources · mentioned players ({len(pids)})</summary>
      <div class='extras-body'>
        <div><h5>Bullet points</h5>{_render_bullets(a.get('bullet_points'))}</div>
        <div><h5>X post</h5><p class='xpost'>{_esc(a.get('x_post'))}</p></div>
        <div><h5>Article sources (reader-visible)</h5>{_render_sources(a.get('sources'))}</div>
        <div><h5>Mentioned players</h5>{_render_players_chips(pids, pmap)}</div>
      </div>
    </details>
  </div>
</div>
"""


def _render_story_block(
    fp: str,
    arts: list[dict],
    state: dict | None,
    trace: dict | None,
    pmap: dict[str, dict],
) -> str:
    arts_sorted = sorted(arts, key=lambda a: 0 if a.get("language") == "en-US" else 1)
    en = next((a for a in arts_sorted if a.get("language") == "en-US"), None)
    de = next((a for a in arts_sorted if a.get("language") == "de-DE"), None)
    headline = (en or de or arts_sorted[0]).get("headline") or "(no headline)"
    teams_seen = sorted({a.get("team") for a in arts_sorted if a.get("team")})
    pmentions_all = sorted({pid for a in arts_sorted for pid in (a.get("mentioned_players") or [])})

    lang_badges = []
    if en:
        lang_badges.append("<span class='lang-have en'>EN</span>")
    else:
        lang_badges.append("<span class='lang-miss'>EN missing</span>")
    if de:
        lang_badges.append("<span class='lang-have de'>DE</span>")
    else:
        lang_badges.append("<span class='lang-miss'>DE missing</span>")

    return f"""
<section class='story'>
  <div class='story-head'>
    <h2>{_esc(headline)}</h2>
    <div class='story-badges'>
      {''.join(lang_badges)}
      {''.join(f"<span class='team-pill'>{_esc(t)}</span>" for t in teams_seen)}
      <span class='fp muted small'>fp {_esc(fp)}</span>
    </div>
  </div>
  <div class='story-body'>
    <div class='story-left'>
      <h4>Editorial state</h4>
      {_render_state(state)}
      <h4>Entities — players ({len(pmentions_all)})</h4>
      {_render_players_chips(pmentions_all, pmap)}
      <h4>Agent trace</h4>
      {_render_trace(trace)}
    </div>
    <div class='story-right'>
      <div class='articles-grid'>
        {''.join(_render_article_card(a, pmap) for a in arts_sorted)}
      </div>
    </div>
  </div>
</section>
"""


CSS = """
:root {
  --bg:#0f1117; --card:#1a1d27; --card2:#141721; --border:#2a2d3a;
  --text:#e1e4ed; --muted:#8b8fa3; --cyan:#22d3ee; --green:#34d399;
  --blue:#60a5fa; --purple:#a78bfa; --yellow:#fbbf24; --orange:#fb923c;
  --red:#f87171; --pink:#f472b6;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: var(--bg); color: var(--text); margin: 0; padding: 24px;
       line-height: 1.55; }
h1 { margin: 0 0 4px; font-size: 26px; }
h2 { font-size: 19px; margin: 0 0 6px; line-height: 1.25; }
h3 { font-size: 16px; margin: 4px 0 4px; line-height: 1.3; }
h4 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
     color: var(--muted); margin: 16px 0 6px; }
h5 { font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
     color: var(--muted); margin: 10px 0 4px; }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: var(--muted); }
.small { font-size: 12px; }

.header { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.subtitle { color: var(--muted); font-size: 13px; margin-top: 4px; font-family: 'SF Mono', monospace; }

.kpis { display: flex; gap: 12px; margin: 16px 0; flex-wrap: wrap; }
.kpi { background: var(--card); border: 1px solid var(--border);
       border-radius: 10px; padding: 10px 16px; min-width: 80px; }
.kpi-val { font-size: 20px; font-weight: 700; color: var(--cyan); }
.kpi-label { font-size: 10px; color: var(--muted); text-transform: uppercase;
             letter-spacing: 1px; }

.toolbar { display:flex; gap:12px; align-items:center; margin: 16px 0;
           background: var(--card); border: 1px solid var(--border);
           border-radius: 10px; padding: 10px 14px; }
.toolbar input[type=search] { background: var(--card2); border: 1px solid var(--border);
                              color: var(--text); padding: 6px 10px; border-radius: 6px;
                              flex: 1; font-size: 13px; }
.toolbar label { font-size: 12px; color: var(--muted); display:flex; gap:4px; align-items:center; }

.story { background: var(--card2); border: 1px solid var(--border);
         border-radius: 14px; padding: 18px; margin-bottom: 18px; }
.story-head { padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.story-badges { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
.lang-have { padding: 2px 8px; border-radius: 5px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }
.lang-have.en { background:#153b2c; color:var(--green); }
.lang-have.de { background:#1b2f4a; color:var(--blue); }
.lang-miss { padding: 2px 8px; border-radius: 5px; font-size: 11px;
             background:#3a1f1f; color:var(--red); }
.fp { font-family: 'SF Mono', monospace; }

.story-body { display: grid; grid-template-columns: 420px 1fr; gap: 22px; padding-top: 12px; }
@media (max-width: 1200px) { .story-body { grid-template-columns: 1fr; } }
.story-left h4:first-child { margin-top: 0; }

.state { background:#11151c; border:1px solid var(--border); border-radius:8px; padding:10px 12px; }
.state-orphan { background:#2a1f1f; border-color: var(--red); }
.state-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 4px 14px; }
.state-grid > div { display:flex; flex-direction:column; font-size: 12px; }
.state-label { color: var(--muted); font-size: 10px; text-transform:uppercase; letter-spacing:1px; }
.state-val { color: var(--text); font-family: 'SF Mono', monospace; font-size: 12px; }
.src-urls { margin: 8px 0 0; padding-left: 16px; font-size: 11px; }
.src-urls li { margin-bottom: 2px; word-break: break-all; }

.trace { background:#11151c; border:1px solid var(--border); border-radius:8px; padding: 10px 12px; }
.trace.none { background:#1a1d27; border-style: dashed; }
.trace-head { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom: 6px; }
.action { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 5px;
          text-transform: uppercase; letter-spacing: 0.5px; }
.action-publish { background: #153b2c; color: var(--green); }
.action-update  { background: #1b2f4a; color: var(--blue); }
.action-skip    { background: #3a2c17; color: var(--orange); }
.action-skipped { background: #3a2c17; color: var(--orange); }
.score { background: #1e293b; color: var(--cyan); padding: 2px 8px; border-radius: 5px;
         font-size: 11px; font-weight: 600; font-family: 'SF Mono', monospace; }
.reasoning { font-size: 12px; color: #dfe3ee; margin: 4px 0 0;
             padding: 8px 10px; border-left: 3px solid var(--purple);
             background: #0d0f14; border-radius: 0 6px 6px 0; }
.trace-meta { display:grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 8px; }

.team-mentions, .player-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.team-pill { background: #1e293b; color: var(--cyan); padding: 2px 8px;
             border-radius: 5px; font-weight: 700; font-size: 11px; letter-spacing: 0.5px; }
.player-tag { background: #2a1f3a; color: var(--purple); padding: 2px 8px;
              border-radius: 5px; font-size: 11px; }

.digests { display: flex; flex-direction: column; gap: 8px; }
.digest { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 10px 12px; font-size: 12px; }
.digest-head { display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap;
               margin-bottom: 4px; }
.digest-head a { font-weight: 600; font-size: 13px; }
.summary { margin: 4px 0; font-size: 12px; color: #c1c5d1; }
.facts { margin: 6px 0 4px; padding-left: 18px; font-size: 11px; color: var(--muted); }
.facts li { margin-bottom: 2px; }
.conf { background:#153b2c; color:var(--green); padding:1px 6px; border-radius:4px;
        font-size:10px; font-family:'SF Mono', monospace; }
.status { background:#2a2d3a; color:var(--muted); padding:1px 6px; border-radius:4px; font-size:10px; }

.articles-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 1100px) { .articles-grid { grid-template-columns: 1fr; } }
.article { background: var(--card); border: 1px solid var(--border);
           border-radius: 12px; overflow: hidden; display:flex; flex-direction:column; }
.cover-wrap { position: relative; aspect-ratio: 16/9; background:#0d0f14; overflow:hidden; }
.cover { width: 100%; height: 100%; object-fit: cover; display: block; }
.img-ph { width: 100%; height: 100%; display:flex; align-items:center; justify-content:center;
          color: var(--muted); font-size: 13px;
          background: linear-gradient(135deg, #1f2433, #0d0f14);
          flex-direction: column; gap: 4px; }
.logo-code { font-size: 36px; font-weight: 800; color: var(--cyan); letter-spacing: 2px; }
.logo-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 2px; }
.tier-badge { position: absolute; top: 8px; right: 8px; background: rgba(0,0,0,0.75);
              padding: 3px 8px; border-radius: 8px; font-size: 10px;
              text-transform: uppercase; letter-spacing: 0.5px; font-weight: 700; }
.tier-curated { color: var(--pink); } .tier-web { color: var(--blue); }
.tier-headshot { color: var(--green); } .tier-logo { color: var(--orange); }
.tier-generic { color: var(--yellow); } .tier-none { color: var(--muted); }
.lang-badge { position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,0.75);
              padding: 3px 8px; border-radius: 8px; font-size: 10px;
              font-family: 'SF Mono', monospace; }
.body { padding: 14px 16px; flex: 1; display:flex; flex-direction:column; }
.byline { display:flex; gap:8px; align-items:center; font-size: 12px; margin-bottom: 6px; flex-wrap:wrap; }
.author { font-style: italic; font-size: 12px; }
.id { margin-left: auto; font-size: 11px; font-family: 'SF Mono', monospace; }
.sub { color: var(--muted); margin: 2px 0 10px; font-size: 13px; font-style: italic; }
.intro { font-size: 13px; color: #dfe3ee; margin: 0 0 10px;
         padding: 8px 12px; border-left: 3px solid var(--cyan);
         background: #11151c; border-radius: 0 6px 6px 0; }
.content { font-size: 13px; }
.content p { margin: 0 0 8px; }
.extras { margin-top: auto; border-top: 1px solid var(--border); padding-top: 10px; }
.extras summary { cursor: pointer; color: var(--muted); font-size: 11px;
                  text-transform: uppercase; letter-spacing: 1px; }
.extras summary::before { content: '\\25B6'; margin-right: 6px; font-size: 9px;
                          display: inline-block; transition: transform 0.2s; }
.extras[open] summary::before { transform: rotate(90deg); }
.extras-body { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; padding-top: 10px; }
.bullets { margin: 0; padding-left: 16px; font-size: 12px; }
.bullets li { margin-bottom: 3px; }
.xpost { margin: 0; padding: 6px 10px; background:#11151c; border-radius:6px;
         font-size: 12px; border-left: 3px solid var(--blue); }
.sources { margin: 0; padding-left: 16px; font-size: 12px; }
.sources li { margin-bottom: 4px; word-break: break-all; }
.src-url { font-size: 10px; word-break: break-all; }

.chips { display: flex; flex-direction: column; gap: 6px; }
.chip { display: flex; gap: 8px; align-items: center;
        background:#11151c; border:1px solid var(--border);
        border-radius:6px; padding: 5px 8px; }
.chip img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; background:#0d0f14; }
.chip.no-img { padding-left: 12px; }
.pn { font-weight: 600; font-size: 12px; }
.ps { font-size: 10px; color: var(--muted); }

.story.hidden { display:none; }
"""


SEARCH_JS = """
const input = document.getElementById('search');
const stories = Array.from(document.querySelectorAll('.story'));
input.addEventListener('input', () => {
  const q = input.value.trim().toLowerCase();
  for (const s of stories) {
    if (!q || s.innerText.toLowerCase().includes(q)) s.classList.remove('hidden');
    else s.classList.add('hidden');
  }
});
"""


def build_html(
    articles: list[dict],
    state_map: dict[str, dict],
    trace_map: dict[str, dict],
    pmap: dict[str, dict],
) -> str:
    by_fp: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []
    for a in articles:
        fp = a.get("story_fingerprint")
        if fp:
            by_fp[fp].append(a)
        else:
            orphans.append(a)

    # Sort stories by most-recent canonical article id desc
    fp_order = sorted(
        by_fp.keys(),
        key=lambda fp: max(a.get("id", 0) or 0 for a in by_fp[fp]),
        reverse=True,
    )

    en_count = sum(1 for a in articles if a.get("language") == "en-US")
    de_count = sum(1 for a in articles if a.get("language") == "de-DE")
    paired = sum(1 for fp in fp_order if any(a.get("language") == "en-US" for a in by_fp[fp])
                 and any(a.get("language") == "de-DE" for a in by_fp[fp]))

    tier_counts: dict[str, int] = defaultdict(int)
    for a in articles:
        tier_counts[_infer_tier(a.get("image"))[1]] += 1

    stories_html = "".join(
        _render_story_block(fp, by_fp[fp], state_map.get(fp), trace_map.get(fp), pmap)
        for fp in fp_order
    )
    if orphans:
        stories_html += (
            "<section class='story'><div class='story-head'><h2>Orphan articles "
            "(no story_fingerprint — pre-migration 007)</h2></div>"
            f"<div class='story-body'><div class='story-right'><div class='articles-grid'>"
            f"{''.join(_render_article_card(a, pmap) for a in orphans)}"
            "</div></div></div></section>"
        )

    state_tracked = sum(1 for fp in fp_order if state_map.get(fp))
    trace_tracked = sum(1 for fp in fp_order if trace_map.get(fp))

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>T4L Pre-Publish Overview — {len(articles)} articles</title>
<style>{CSS}</style>
</head>
<body>
  <div class='header'>
    <h1>T4L Pre-Publish Overview</h1>
    <div class='subtitle'>{len(articles)} articles · {len(fp_order)} stories · {len(orphans)} orphan(s)</div>
  </div>
  <div class='kpis'>
    <div class='kpi'><div class='kpi-val'>{len(articles)}</div><div class='kpi-label'>Articles</div></div>
    <div class='kpi'><div class='kpi-val'>{len(fp_order)}</div><div class='kpi-label'>Stories</div></div>
    <div class='kpi'><div class='kpi-val'>{paired}</div><div class='kpi-label'>EN+DE paired</div></div>
    <div class='kpi'><div class='kpi-val'>{en_count}</div><div class='kpi-label'>en-US</div></div>
    <div class='kpi'><div class='kpi-val'>{de_count}</div><div class='kpi-label'>de-DE</div></div>
    <div class='kpi'><div class='kpi-val'>{state_tracked}</div><div class='kpi-label'>w/ editorial_state</div></div>
    <div class='kpi'><div class='kpi-val'>{trace_tracked}</div><div class='kpi-label'>w/ trace</div></div>
  </div>
  <div class='kpis'>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-curated', 0)}</div><div class='kpi-label'>Curated</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-web', 0)}</div><div class='kpi-label'>Web</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-headshot', 0)}</div><div class='kpi-label'>Headshot</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-logo', 0)}</div><div class='kpi-label'>Logo</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-generic', 0)}</div><div class='kpi-label'>Generic</div></div>
    <div class='kpi'><div class='kpi-val'>{tier_counts.get('tier-none', 0)}</div><div class='kpi-label'>No image</div></div>
  </div>
  <div class='toolbar'>
    <input id='search' type='search' placeholder='Filter stories by headline, team, player, fingerprint, reasoning…' autofocus />
    <span class='muted small'>live filter</span>
  </div>
  {stories_html}
  <script>{SEARCH_JS}</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of articles fetched (default: all)")
    args = parser.parse_args()

    print("Fetching articles from content.team_article…")
    articles = fetch_all_articles(limit=args.limit)
    print(f"  → {len(articles)} articles")

    fps = sorted({a["story_fingerprint"] for a in articles if a.get("story_fingerprint")})
    print(f"Fetching editorial_state for {len(fps)} fingerprints…")
    state_map = fetch_editorial_state(fps)
    print(f"  → {len(state_map)} state records")

    print("Loading orchestrator traces from local cycle JSONs…")
    trace_map = load_traces()
    print(f"  → {len(trace_map)} trace entries")

    pids = sorted({pid for a in articles for pid in (a.get("mentioned_players") or [])})
    print(f"Fetching {len(pids)} player records for headshots…")
    pmap = fetch_players(pids)
    print(f"  → {len(pmap)} resolved")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(articles, state_map, trace_map, pmap))
    print(f"\nWrote {OUTPUT}")
    print(f"Open: file://{OUTPUT}")


if __name__ == "__main__":
    main()
