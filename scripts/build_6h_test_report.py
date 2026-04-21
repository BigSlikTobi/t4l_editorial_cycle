"""Build an HTML report for the most recent 6-hour editorial-cycle test.

Cycle IDs are read from var/test_runs/summary.log (trailing "Run X/6" block).
For each cycle ID we look up editorial_state → supabase_article_ids → fetch
team_article rows and render per-cycle sections with images, headlines,
personas, mentioned players, and the full article body.

Image tier is inferred from the stored URL shape (the storage path segment
is deterministic per tier) so no cross-referencing with cycle logs is needed.

Usage:
    ./venv/bin/python scripts/build_6h_test_report.py

Writes var/6h_test_report.html and prints the file:// URL.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_LOG = ROOT / "var" / "test_runs" / "summary.log"
OUTPUT = ROOT / "var" / "6h_test_report.html"

# Cycle-summary line pattern:
# [20260420T073811Z] Run 1/6 OK: Cycle 66c96daa-... | 5 written, 0 updated | 0 duplicates prevented
RUN_LINE = re.compile(
    r"\[(?P<ts>\d{8}T\d{6}Z)\] Run (?P<n>\d+)/(?P<total>\d+) OK: "
    r"Cycle (?P<cid>[0-9a-f-]+) \| (?P<written>\d+) written, "
    r"(?P<updated>\d+) updated \| (?P<dups>\d+) duplicates prevented"
)


def _parse_latest_test_cycles() -> list[dict]:
    """Return the last test's cycles (contiguous block ending at 'test complete').

    We want the MOST RECENT test run, not historical ones. The summary log
    accumulates across runs — find the last 'test complete' line and walk
    back to its matching starting line.
    """
    lines = SUMMARY_LOG.read_text().splitlines()
    # Find last "test complete" line — that ends the most recent run.
    end_idx = None
    for i, line in enumerate(lines):
        if "test complete" in line:
            end_idx = i
    if end_idx is None:
        raise SystemExit("No 'test complete' line in summary.log")

    # Walk backward from end_idx collecting run-OK lines until the matching
    # Run 1 starter. Each test run's lines are grouped together in order.
    cycles: list[dict] = []
    for line in reversed(lines[:end_idx]):
        m = RUN_LINE.search(line)
        if m:
            cycles.append({
                "timestamp": m.group("ts"),
                "run_n": int(m.group("n")),
                "total": int(m.group("total")),
                "cycle_id": m.group("cid"),
                "written": int(m.group("written")),
                "updated": int(m.group("updated")),
                "dups": int(m.group("dups")),
            })
            if m.group("n") == "1":
                break
    cycles.reverse()
    return cycles


def _fetch(base: str, key: str, path: str, params: dict,
           profile: str | None = None) -> list[dict]:
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if profile:
        headers["Accept-Profile"] = profile
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()


def _fetch_editorial_state(base: str, key: str, cycle_ids: list[str]) -> list[dict]:
    if not cycle_ids:
        return []
    ids_clause = ",".join(f'"{c}"' for c in cycle_ids)
    return _fetch(
        base, key, "/rest/v1/editorial_state",
        {
            "cycle_id": f"in.({ids_clause})",
            "select": "cycle_id,supabase_article_id,cluster_headline,last_updated_at,source_urls",
            "order": "last_updated_at.desc",
        },
    )


def _fetch_articles(base: str, key: str, article_ids: list[int]) -> dict[int, dict]:
    if not article_ids:
        return {}
    ids_clause = ",".join(str(i) for i in article_ids)
    rows = _fetch(
        base, key, "/rest/v1/team_article",
        {
            "id": f"in.({ids_clause})",
            "select": "id,created_at,team,author,language,headline,sub_headline,"
                      "introduction,content,x_post,bullet_points,image,mentioned_players",
            "order": "id.asc",
        },
        profile="content",
    )
    return {r["id"]: r for r in rows}


def _fetch_players(base: str, key: str, player_ids: list[str]) -> dict[str, dict]:
    if not player_ids:
        return {}
    ids_clause = ",".join(f'"{pid}"' for pid in player_ids)
    rows = _fetch(
        base, key, "/rest/v1/players",
        {
            "player_id": f"in.({ids_clause})",
            "select": "player_id,display_name,position,latest_team",
        },
    )
    return {r["player_id"]: r for r in rows}


def _infer_tier(url: str | None) -> str:
    if not url:
        return "none"
    if url.startswith("asset://team_logo/"):
        return "team_logo"
    if url.startswith("asset://generic/"):
        return "generic_nfl"
    if "/storage/v1/object/public/images/" in url:
        if "image_search" in url:
            return "image_search"
        if "ai_generated" in url:
            return "ai_generated"
    if "static.www.nfl.com" in url and "/league/" in url:
        return "player_headshot"
    return "other"


TIER_COLORS = {
    "image_search":    ("#22c55e", "web"),      # green
    "player_headshot": ("#3b82f6", "headshot"), # blue
    "ai_generated":    ("#a855f7", "AI-gen"),   # purple
    "team_logo":       ("#f59e0b", "logo"),     # amber
    "generic_nfl":     ("#6b7280", "generic"),  # grey
    "other":           ("#475569", "other"),    # slate
    "none":            ("#991b1b", "none"),     # red
}


def _esc(v: Any) -> str:
    return "" if v is None else html.escape(str(v))


def _render_image(url: str | None, team: str | None) -> str:
    tier = _infer_tier(url)
    color, label = TIER_COLORS[tier]
    badge = (
        f"<span class='tier-badge' style='background:{color}'>"
        f"{label}</span>"
    )
    if tier == "team_logo":
        code = url.rsplit("/", 1)[-1] if url else team or "?"
        body = (
            f"<div class='img-placeholder logo-ph'>"
            f"<div class='logo-code'>{_esc(code)}</div>"
            f"<div class='logo-sub'>team logo</div></div>"
        )
    elif tier == "generic_nfl":
        body = (
            f"<div class='img-placeholder generic-ph'>"
            f"<div class='logo-code'>NFL</div>"
            f"<div class='logo-sub'>generic</div></div>"
        )
    elif tier == "none":
        body = "<div class='img-placeholder'>no image</div>"
    else:
        body = f"<img src='{_esc(url)}' alt='' loading='lazy'>"
    return f"<div class='img-wrap'>{body}{badge}</div>"


def _players_chips(ids: list[str], player_map: dict[str, dict]) -> str:
    if not ids:
        return ""
    chips: list[str] = []
    for pid in ids:
        p = player_map.get(pid)
        if p:
            name = p.get("display_name") or pid
            pos = p.get("position") or ""
            tm = p.get("latest_team") or ""
            extras = " · ".join(x for x in (pos, tm) if x)
            tooltip = f"{_esc(name)} ({_esc(extras)})" if extras else _esc(name)
            chips.append(
                f"<span class='chip resolved' title='{tooltip}'>{_esc(name)}</span>"
            )
        else:
            chips.append(
                f"<span class='chip unresolved' title='Not in public.players'>"
                f"{_esc(pid)}</span>"
            )
    return f"<div class='chips'>{''.join(chips)}</div>"


def _format_ts(ts: str) -> str:
    # 20260420T073811Z → 2026-04-20 07:38 UTC
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]} UTC"


def _render_article_card(art: dict, player_map: dict[str, dict]) -> str:
    tier = _infer_tier(art.get("image"))
    image_html = _render_image(art.get("image"), art.get("team"))
    content_paragraphs = ""
    body = (art.get("content") or "").strip()
    if body:
        paras = [p.strip() for p in body.split("\n\n") if p.strip()]
        content_paragraphs = "\n".join(
            f"<p>{_esc(p)}</p>" for p in paras
        )
    players_html = _players_chips(
        art.get("mentioned_players") or [], player_map
    )
    bullets = (art.get("bullet_points") or "").strip()
    bullets_html = ""
    if bullets:
        lines = [b.strip().lstrip("-•*").strip() for b in bullets.splitlines() if b.strip()]
        if lines:
            bullets_html = (
                "<div class='bullets'><div class='label'>Bullet points</div><ul>"
                + "".join(f"<li>{_esc(line)}</li>" for line in lines)
                + "</ul></div>"
            )
    return f"""
      <article class='article-card tier-{tier}'>
        <div class='card-grid'>
          {image_html}
          <div class='meta'>
            <div class='meta-row'>
              <span class='chip team'>{_esc(art.get('team') or '—')}</span>
              <span class='chip lang'>{_esc(art.get('language') or 'en-US')}</span>
              <span class='chip author'>{_esc(art.get('author') or '—')}</span>
              <span class='muted'>#{art.get('id')}</span>
            </div>
            <h3>{_esc(art.get('headline'))}</h3>
            <div class='sub'>{_esc(art.get('sub_headline'))}</div>
            {players_html}
          </div>
        </div>
        <div class='body'>
          <div class='intro'><p>{_esc(art.get('introduction') or '')}</p></div>
          <div class='content'>{content_paragraphs}</div>
          {bullets_html}
        </div>
      </article>
    """


def main() -> None:
    cycles = _parse_latest_test_cycles()
    assert cycles, "no cycles parsed"
    print(f"Parsed {len(cycles)} cycles from latest test run")

    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()

    cycle_ids = [c["cycle_id"] for c in cycles]
    state_rows = _fetch_editorial_state(base, key, cycle_ids)
    print(f"Fetched {len(state_rows)} editorial_state rows")

    # Group state rows by cycle_id
    state_by_cycle: dict[str, list[dict]] = {}
    for row in state_rows:
        state_by_cycle.setdefault(row["cycle_id"], []).append(row)

    all_article_ids = [r["supabase_article_id"] for r in state_rows]
    articles = _fetch_articles(base, key, all_article_ids)
    print(f"Fetched {len(articles)} team_article rows")

    all_player_ids = sorted({
        pid for a in articles.values()
        for pid in (a.get("mentioned_players") or [])
    })
    player_map = _fetch_players(base, key, all_player_ids)
    print(f"Resolved {len(player_map)}/{len(all_player_ids)} players")

    # KPIs
    total_written = sum(c["written"] for c in cycles)
    total_updated = sum(c["updated"] for c in cycles)
    total_dups = sum(c["dups"] for c in cycles)
    tier_counts: Counter[str] = Counter()
    for art in articles.values():
        tier_counts[_infer_tier(art.get("image"))] += 1

    kpi_cards = f"""
      <div class='kpis'>
        <div class='kpi'><div class='v'>{len(cycles)}</div><div class='l'>cycles</div></div>
        <div class='kpi'><div class='v'>{total_written}</div><div class='l'>written</div></div>
        <div class='kpi'><div class='v'>{total_updated}</div><div class='l'>updated</div></div>
        <div class='kpi'><div class='v'>{total_dups}</div><div class='l'>duplicates prevented</div></div>
      </div>
    """
    tier_rows = "\n".join(
        f"<tr><td><span class='tier-badge' style='background:{TIER_COLORS[t][0]}'>"
        f"{TIER_COLORS[t][1]}</span></td><td>{tier_counts[t]}</td>"
        f"<td>{(tier_counts[t] / max(1, sum(tier_counts.values())) * 100):.0f}%</td></tr>"
        for t in sorted(tier_counts, key=lambda x: -tier_counts[x])
    )

    # Per-cycle sections
    cycle_sections: list[str] = []
    for c in cycles:
        rows_for_cycle = state_by_cycle.get(c["cycle_id"], [])
        articles_in_cycle = [
            articles[r["supabase_article_id"]]
            for r in rows_for_cycle
            if r["supabase_article_id"] in articles
        ]
        cards = "\n".join(
            _render_article_card(a, player_map) for a in articles_in_cycle
        )
        empty_note = "" if articles_in_cycle else (
            "<p class='empty'>No articles persisted — cycle produced "
            f"{c['dups']} duplicate-prevention hit(s) only.</p>"
        )
        cycle_sections.append(f"""
          <section class='cycle'>
            <div class='cycle-head'>
              <div class='run-pill'>Run {c['run_n']}/{c['total']}</div>
              <div class='cycle-meta'>
                <div class='ts'>{_format_ts(c['timestamp'])}</div>
                <div class='cid muted mono'>{_esc(c['cycle_id'])}</div>
              </div>
              <div class='cycle-counts'>
                <span class='count'><b>{c['written']}</b> written</span>
                <span class='count'><b>{c['updated']}</b> updated</span>
                <span class='count'><b>{c['dups']}</b> dups prevented</span>
              </div>
            </div>
            {cards}
            {empty_note}
          </section>
        """)

    html_out = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>6-hour test report · T4L editorial cycle</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; padding: 0; background: #0b0f17; color: #e5e7eb; }}
  header.page {{ padding: 32px 40px 24px; border-bottom: 1px solid #1f2937;
                background: linear-gradient(180deg,#111827,#0b0f17); }}
  header.page h1 {{ margin: 0 0 4px; font-size: 24px; letter-spacing: -0.01em; }}
  header.page .sub {{ color: #94a3b8; font-size: 13px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 12px;
           margin-top: 20px; max-width: 720px; }}
  .kpi {{ background:#111827; border:1px solid #1f2937; border-radius:10px; padding:14px 16px; }}
  .kpi .v {{ font-size: 26px; font-weight: 700; line-height: 1; }}
  .kpi .l {{ color:#94a3b8; font-size: 12px; margin-top: 4px; }}
  table.tiers {{ margin-top: 20px; border-collapse: collapse; font-size: 13px; }}
  table.tiers td {{ padding: 4px 14px 4px 0; }}
  main {{ padding: 20px 40px 60px; max-width: 1100px; margin: 0 auto; }}
  section.cycle {{ margin: 36px 0; }}
  .cycle-head {{ display: flex; align-items: center; gap: 16px; margin-bottom: 14px;
                 padding: 12px 14px; background:#111827; border:1px solid #1f2937;
                 border-radius: 10px; }}
  .run-pill {{ background:#3b82f6; color:white; font-size:12px; font-weight:600;
               padding:4px 10px; border-radius:999px; }}
  .cycle-meta .ts {{ font-weight: 600; }}
  .cycle-meta .cid {{ font-size: 11px; }}
  .cycle-counts {{ margin-left:auto; display:flex; gap:14px; font-size: 13px; color:#cbd5e1; }}
  .cycle-counts b {{ color:#fff; }}
  article.article-card {{ background:#111827; border:1px solid #1f2937; border-radius:12px;
                          padding:16px; margin: 12px 0; }}
  .card-grid {{ display:grid; grid-template-columns: 240px 1fr; gap: 18px; }}
  .img-wrap {{ position:relative; width:240px; height:160px; border-radius:8px; overflow:hidden;
               background:#0b0f17; border:1px solid #1f2937; }}
  .img-wrap img {{ width:100%; height:100%; object-fit: cover; display:block; }}
  .img-placeholder {{ width:100%; height:100%; display:flex; flex-direction:column;
                      align-items:center; justify-content:center; color:#6b7280;
                      font-size: 13px; }}
  .img-placeholder.logo-ph {{ background:#1f2937; color:#f59e0b; }}
  .img-placeholder.generic-ph {{ background:#1f2937; color:#94a3b8; }}
  .logo-code {{ font-size: 28px; font-weight: 800; letter-spacing:1px; }}
  .logo-sub {{ font-size: 11px; color: #94a3b8; margin-top: 2px; text-transform:uppercase; }}
  .tier-badge {{ position:absolute; top:8px; left:8px; padding:3px 8px;
                 border-radius:999px; font-size:11px; color:white; font-weight:600; }}
  .meta-row {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom: 6px; }}
  .chip {{ font-size: 11px; padding: 2px 8px; border-radius: 4px;
           background: #1f2937; color: #cbd5e1; border: 1px solid #374151; }}
  .chip.team {{ background:#0f172a; color:#60a5fa; border-color:#1e3a8a; font-weight:600; }}
  .chip.author {{ background:#0f172a; color:#c4b5fd; border-color:#4c1d95; }}
  .chip.resolved {{ background:#022c22; color:#34d399; border-color:#065f46; }}
  .chip.unresolved {{ background:#2d1413; color:#fca5a5; border-color:#7f1d1d; }}
  .chips {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }}
  .muted {{ color:#6b7280; font-size: 12px; }}
  .mono {{ font-family: ui-monospace, 'SF Mono', Menlo, monospace; }}
  article h3 {{ margin: 6px 0 4px; font-size: 17px; letter-spacing:-0.01em; }}
  article .sub {{ color:#9ca3af; font-size: 13px; line-height: 1.4; }}
  .body {{ margin-top: 14px; border-top: 1px solid #1f2937; padding-top: 14px;
           font-size: 14px; color: #d1d5db; line-height: 1.55; }}
  .intro {{ font-style: italic; color:#a3a3a3; margin-bottom: 10px; }}
  .content p {{ margin: 0 0 10px; }}
  .bullets {{ margin-top: 12px; padding: 10px 14px; background: #0b0f17; border-radius: 8px; }}
  .bullets .label {{ font-size: 11px; text-transform: uppercase; color:#6b7280; margin-bottom: 6px; }}
  .bullets ul {{ margin: 0; padding-left: 18px; }}
  .empty {{ color: #6b7280; font-style: italic; text-align: center; margin: 12px 0 0; }}
</style>
</head>
<body>
  <header class='page'>
    <h1>6-hour editorial cycle test</h1>
    <div class='sub'>
      {len(cycles)} cycles · first at {_format_ts(cycles[0]['timestamp'])}
      · last at {_format_ts(cycles[-1]['timestamp'])}
    </div>
    {kpi_cards}
    <table class='tiers'>
      <thead><tr><th>Tier</th><th>Count</th><th>Share</th></tr></thead>
      <tbody>{tier_rows}</tbody>
    </table>
  </header>
  <main>
    {''.join(cycle_sections)}
  </main>
</body>
</html>
"""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html_out)
    print(f"Wrote {OUTPUT}")
    print(f"Open: file://{OUTPUT}")


if __name__ == "__main__":
    main()
