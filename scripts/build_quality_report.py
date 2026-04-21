"""Build an HTML quality-review page for the last 12-cycle test run.

Fetches every article produced by the 12 recorded cycles from Supabase
(joining editorial_state -> team_article) and renders a page that surfaces
each article's headline, sub-headline, team code, introduction, full
content, bullet points, and the source URLs the cluster was built from.

Output: var/test_runs/quality_report.html
"""

from __future__ import annotations

import glob
import html
import json
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "var" / "test_runs"
OUTPUT = TEST_DIR / "quality_report.html"


def load_cycles() -> list[dict[str, Any]]:
    files = sorted(
        glob.glob(str(TEST_DIR / "cycle_*.json")),
        key=lambda p: int(Path(p).stem.split("_")[1]),
    )
    cycles = []
    for path in files:
        data = json.loads(Path(path).read_text())
        cycles.append(
            {
                "cycle_num": int(Path(path).stem.split("_")[1]),
                "cycle_id": data["cycle_id"],
                "generated_at": data.get("generated_at"),
                "articles_written": data.get("articles_written", 0),
                "articles_updated": data.get("articles_updated", 0),
                "plan": data.get("plan", {}),
            }
        )
    return cycles


def fetch_editorial_state(cycle_ids: list[str]) -> list[dict[str, Any]]:
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    ids_clause = ",".join(f'"{cid}"' for cid in cycle_ids)
    resp = httpx.get(
        f"{base}/rest/v1/editorial_state",
        params={"cycle_id": f"in.({ids_clause})", "select": "*"},
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_articles(article_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not article_ids:
        return {}
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()
    id_list = ",".join(str(i) for i in article_ids)
    resp = httpx.get(
        f"{base}/rest/v1/team_article",
        params={"id": f"in.({id_list})", "select": "*"},
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Accept-Profile": "content",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return {row["id"]: row for row in resp.json()}


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value))


def _render_content(text: str | None) -> str:
    if not text:
        return "<em class='muted'>(empty)</em>"
    # Content is plain text with paragraph breaks — preserve line breaks.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]
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


def _render_sources(urls: list[str]) -> str:
    if not urls:
        return "<span class='muted'>none recorded</span>"
    return "<ul class='sources'>" + "".join(
        f'<li><a href="{_esc(u)}" target="_blank" rel="noopener">{_esc(u)}</a></li>'
        for u in urls
    ) + "</ul>"


def build_html(cycles: list[dict[str, Any]], state_rows: list[dict[str, Any]], articles: dict[int, dict[str, Any]]) -> str:
    # Map: cycle_id -> list[state_row]
    state_by_cycle: dict[str, list[dict[str, Any]]] = {}
    for row in state_rows:
        state_by_cycle.setdefault(row["cycle_id"], []).append(row)

    # Map: fingerprint -> plan story (for reasoning/team_codes/action)
    plan_by_cycle: dict[str, dict[str, dict[str, Any]]] = {}
    for c in cycles:
        plan_by_cycle[c["cycle_id"]] = {
            s["story_fingerprint"]: s for s in c["plan"].get("stories", [])
        }

    total_articles = sum(len(v) for v in state_by_cycle.values())

    sections: list[str] = []
    for c in cycles:
        rows = state_by_cycle.get(c["cycle_id"], [])
        rows.sort(key=lambda r: r.get("supabase_article_id") or 0)
        cycle_plan = plan_by_cycle[c["cycle_id"]]

        if not rows:
            sections.append(
                f"""
<section class='cycle empty'>
  <h2>Cycle {c['cycle_num']} · <span class='muted'>{_esc(c['generated_at'])}</span></h2>
  <p class='muted'>No articles written or updated in this cycle.</p>
</section>
"""
            )
            continue

        cards: list[str] = []
        for row in rows:
            article_id = row.get("supabase_article_id")
            article = articles.get(article_id)
            plan_story = cycle_plan.get(row.get("story_fingerprint"), {})
            team_codes = plan_story.get("team_codes") or []
            action = plan_story.get("action") or "?"
            news_score = plan_story.get("news_value_score")
            reasoning = plan_story.get("reasoning") or ""
            source_urls = row.get("source_urls") or []

            if article is None:
                cards.append(
                    f"<article class='card missing'><h3>Article {article_id} — not found in team_article</h3>"
                    f"<p class='muted'>Fingerprint: {_esc(row.get('story_fingerprint'))}</p></article>"
                )
                continue

            team_tags = "".join(
                f"<span class='team-tag'>{_esc(t)}</span>" for t in team_codes
            )
            persisted_team = article.get("team")
            persisted_team_html = (
                f"<span class='team-tag persisted'>persisted: {_esc(persisted_team)}</span>"
                if persisted_team
                else "<span class='team-tag persisted warn-tag'>persisted: NULL</span>"
            )
            score_html = (
                f"<span class='score'>score {news_score:.2f}</span>"
                if isinstance(news_score, (int, float))
                else ""
            )

            cards.append(
                f"""
<article class='card'>
  <header class='card-head'>
    <div class='card-title'>
      <div class='badges'>
        <span class='badge badge-{_esc(action)}'>{_esc(action)}</span>
        {score_html}
        {persisted_team_html}
        {team_tags}
      </div>
      <h3>{_esc(article.get('headline'))}</h3>
      <p class='sub'>{_esc(article.get('sub_headline'))}</p>
    </div>
    <div class='card-meta'>
      <div>article_id: <code>{article_id}</code></div>
      <div>fingerprint: <code>{_esc(row.get('story_fingerprint'))}</code></div>
    </div>
  </header>
  <div class='card-body'>
    <div class='col'>
      <h4>Introduction</h4>
      <p class='intro'>{_esc(article.get('introduction'))}</p>
      <h4>Content</h4>
      <div class='content-body'>{_render_content(article.get('content'))}</div>
    </div>
    <aside class='col side'>
      <h4>Bullet Points</h4>
      {_render_bullets(article.get('bullet_points'))}
      <h4>X Post</h4>
      <p class='xpost'>{_esc(article.get('x_post'))}</p>
      <h4>Source URLs ({len(source_urls)})</h4>
      {_render_sources(source_urls)}
      {f"<h4>Editor Reasoning</h4><p class='muted small'>{_esc(reasoning)}</p>" if reasoning else ""}
    </aside>
  </div>
</article>
"""
            )

        sections.append(
            f"""
<section class='cycle'>
  <h2>Cycle {c['cycle_num']} · <span class='muted'>{_esc(c['generated_at'])}</span>
    <span class='pill'>{len(rows)} article{'s' if len(rows) != 1 else ''}</span>
    <span class='pill written'>{c['articles_written']} written</span>
    <span class='pill updated'>{c['articles_updated']} updated</span>
  </h2>
  {''.join(cards)}
</section>
"""
        )

    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>T4L Editorial Quality Review — Last 12 Cycles</title>
<style>
  :root {{
    --bg:#0f1117; --card:#1a1d27; --border:#2a2d3a; --text:#e1e4ed; --muted:#8b8fa3;
    --green:#34d399; --blue:#60a5fa; --yellow:#fbbf24; --red:#f87171; --cyan:#22d3ee; --purple:#a78bfa;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); color:var(--text); margin:0; padding:24px; line-height:1.6; }}
  h1 {{ margin:0 0 6px; font-size:28px; }}
  h2 {{ font-size:20px; margin:40px 0 18px; color:var(--cyan); border-bottom:1px solid var(--border); padding-bottom:8px; }}
  h3 {{ font-size:18px; margin:0 0 4px; }}
  h4 {{ font-size:12px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); margin:16px 0 6px; }}
  .subtitle {{ color:var(--muted); font-size:14px; margin-bottom:24px; }}
  .muted {{ color:var(--muted); }}
  .small {{ font-size:13px; }}
  .kpi-row {{ display:flex; gap:16px; flex-wrap:wrap; margin:20px 0 30px; }}
  .kpi {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 20px; min-width:140px; }}
  .kpi-val {{ font-size:28px; font-weight:700; color:var(--cyan); }}
  .kpi-label {{ font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 20px; margin-bottom:16px; }}
  .card.missing {{ border-color:#7f1d1d; }}
  .card-head {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:12px; }}
  .card-title {{ flex:1; }}
  .card-meta {{ text-align:right; font-size:12px; color:var(--muted); min-width:240px; }}
  .card-meta code {{ background:#0d0f14; padding:1px 6px; border-radius:4px; color:var(--purple); font-size:11px; }}
  .card-body {{ display:flex; gap:24px; }}
  .col {{ flex:1; min-width:0; }}
  .side {{ flex:0 0 340px; }}
  .sub {{ color:var(--muted); font-size:14px; margin:4px 0 0; font-style:italic; }}
  .intro {{ font-size:15px; margin:0 0 10px; color:#dfe3ee; }}
  .content-body p {{ margin:0 0 12px; }}
  .xpost {{ background:#11151c; border-left:3px solid var(--blue); padding:8px 12px; border-radius:4px; font-size:14px; }}
  .bullets {{ margin:0; padding-left:20px; }}
  .bullets li {{ margin-bottom:4px; }}
  .sources {{ margin:0; padding-left:20px; font-size:12px; }}
  .sources li {{ margin-bottom:4px; word-break:break-all; }}
  .sources a {{ color:var(--cyan); text-decoration:none; }}
  .sources a:hover {{ text-decoration:underline; }}
  .badges {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:6px; align-items:center; }}
  .badge {{ padding:2px 10px; border-radius:10px; font-size:11px; font-weight:600; text-transform:uppercase; }}
  .badge-publish {{ background:#064e3b; color:var(--green); }}
  .badge-update {{ background:#1e3a5f; color:var(--blue); }}
  .badge-skip {{ background:#374151; color:var(--muted); }}
  .score {{ background:#2d2042; color:var(--purple); padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }}
  .team-tag {{ background:#1e293b; color:var(--cyan); padding:2px 8px; border-radius:4px; font-size:11px; }}
  .team-tag.persisted {{ background:#143321; color:var(--green); }}
  .team-tag.warn-tag {{ background:#422006; color:var(--yellow); }}
  .pill {{ background:#22252f; color:var(--muted); padding:2px 10px; border-radius:10px; font-size:12px; margin-left:8px; font-weight:500; }}
  .pill.written {{ background:#064e3b; color:var(--green); }}
  .pill.updated {{ background:#1e3a5f; color:var(--blue); }}
  section.cycle.empty {{ opacity:0.6; }}
  a {{ color:var(--cyan); }}
</style>
</head>
<body>
  <h1>T4L Editorial Quality Review</h1>
  <div class='subtitle'>Articles produced during the 12-cycle test run — headline, sub-headline, intro, content, bullets, X post, and source URLs. Use this to evaluate writing quality and flag team-code issues.</div>
  <div class='kpi-row'>
    <div class='kpi'><div class='kpi-val'>{len(cycles)}</div><div class='kpi-label'>Cycles</div></div>
    <div class='kpi'><div class='kpi-val'>{total_articles}</div><div class='kpi-label'>Articles produced</div></div>
    <div class='kpi'><div class='kpi-val'>{sum(c['articles_written'] for c in cycles)}</div><div class='kpi-label'>Written</div></div>
    <div class='kpi'><div class='kpi-val'>{sum(c['articles_updated'] for c in cycles)}</div><div class='kpi-label'>Updated</div></div>
  </div>
  {''.join(sections)}
</body>
</html>
"""


def main() -> None:
    cycles = load_cycles()
    cycle_ids = [c["cycle_id"] for c in cycles]
    print(f"Loaded {len(cycles)} cycles")

    state_rows = fetch_editorial_state(cycle_ids)
    print(f"Fetched {len(state_rows)} editorial_state rows")

    article_ids = sorted({r["supabase_article_id"] for r in state_rows if r.get("supabase_article_id")})
    print(f"Fetching {len(article_ids)} team_article rows")
    articles = fetch_articles(article_ids)
    print(f"Received {len(articles)} articles")

    html_out = build_html(cycles, state_rows, articles)
    OUTPUT.write_text(html_out)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
