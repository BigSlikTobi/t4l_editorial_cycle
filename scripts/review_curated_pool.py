"""Local review UI for curated pool PNGs.

Usage:
    ./venv/bin/python scripts/review_curated_pool.py
    # then open http://localhost:8765

Workflow:
  - Default state per image is "keep"; click Reject on duds.
  - Click "Upload approved" to push all non-rejected, non-uploaded images to
    Supabase (bucket images/curated/, table content.curated_images).
  - Decisions + upload state persist in var/curated_pool/decisions.json so you
    can close and resume.

No external deps — stdlib http.server + httpx (already in venv) for upload.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asyncio

import httpx

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("review_curated_pool")

PLAN_JSON = ROOT / "var" / "curated_pool_plan.json"
RAW_DIR = ROOT / "var" / "curated_pool" / "raw"
DECISIONS_FILE = ROOT / "var" / "curated_pool" / "decisions.json"
BUCKET = "images"
STORAGE_PREFIX = "curated"

PORT = 8765

_plan_by_slug: dict[str, dict] = {}
_decisions: dict[str, str] = {}  # slug -> "keep" | "reject" | "uploaded"
_lock = threading.Lock()


def _load_plan() -> None:
    global _plan_by_slug
    if not PLAN_JSON.exists():
        raise SystemExit(f"{PLAN_JSON} not found")
    _plan_by_slug = {row["slug"]: row for row in json.loads(PLAN_JSON.read_text())}


def _load_decisions() -> None:
    global _decisions
    if DECISIONS_FILE.exists():
        _decisions = json.loads(DECISIONS_FILE.read_text())
    else:
        _decisions = {}


def _save_decisions() -> None:
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DECISIONS_FILE.write_text(json.dumps(_decisions, indent=2, sort_keys=True))


def _status_for(slug: str) -> str:
    return _decisions.get(slug, "keep")


def _list_pngs() -> list[Path]:
    return sorted(RAW_DIR.glob("*.png"))


# ---- upload ----

async def _upload_one(
    client: httpx.AsyncClient, base: str, row: dict, png_bytes: bytes
) -> str | None:
    slug = row["slug"]
    path = f"{STORAGE_PREFIX}/{slug}.png"
    public_url = f"{base}/storage/v1/object/public/{BUCKET}/{path}"

    up = await client.post(
        f"{base}/storage/v1/object/{BUCKET}/{path}",
        content=png_bytes,
        headers={"Content-Type": "image/png", "x-upsert": "true"},
    )
    if up.status_code >= 400:
        logger.error("Upload %s: %d %s", slug, up.status_code, up.text[:200])
        return None

    meta = await client.post(
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
    if meta.status_code >= 400:
        logger.error("Meta %s: %d %s", slug, meta.status_code, meta.text[:200])
        return None
    return public_url


async def _upload_approved() -> dict:
    settings = get_settings()
    base = str(settings.supabase_url).rstrip("/")
    key = settings.supabase_service_role_key.get_secret_value()

    to_upload: list[tuple[str, Path]] = []
    with _lock:
        for path in _list_pngs():
            slug = path.stem
            status = _decisions.get(slug, "keep")
            if status in ("keep",):  # not rejected, not already uploaded
                if slug in _plan_by_slug:
                    to_upload.append((slug, path))

    uploaded = 0
    failed = 0
    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"Authorization": f"Bearer {key}", "apikey": key},
    ) as client:
        for slug, path in to_upload:
            row = _plan_by_slug[slug]
            url = await _upload_one(client, base, row, path.read_bytes())
            if url:
                uploaded += 1
                with _lock:
                    _decisions[slug] = "uploaded"
                    _save_decisions()
            else:
                failed += 1
    return {"uploaded": uploaded, "failed": failed, "attempted": len(to_upload)}


# ---- HTML rendering ----

PAGE_CSS = """
body { font-family: -apple-system, sans-serif; margin: 0; background: #111; color: #eee; }
.toolbar { position: sticky; top: 0; background: #222; padding: 12px 20px; z-index: 10;
           display: flex; gap: 16px; align-items: center; border-bottom: 1px solid #333; }
.toolbar h1 { margin: 0; font-size: 16px; font-weight: 500; }
.counts { font-size: 13px; color: #aaa; }
.btn { background: #3366cc; color: white; border: 0; padding: 8px 16px; border-radius: 4px;
       cursor: pointer; font-size: 14px; }
.btn:hover { background: #4477dd; }
.btn.secondary { background: #444; }
.filter { background: #222; color: #eee; border: 1px solid #555; padding: 6px 10px;
          border-radius: 4px; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
        gap: 12px; padding: 16px; }
.card { background: #1a1a1a; border-radius: 6px; overflow: hidden; border: 2px solid #1a1a1a;
        display: flex; flex-direction: column; }
.card.reject { border-color: #a33; opacity: 0.45; }
.card.uploaded { border-color: #2a7; }
.card img { width: 100%; height: 200px; object-fit: cover; background: #000; cursor: pointer; }
.card .meta { padding: 8px 10px; font-size: 12px; line-height: 1.4; }
.card .slug { font-family: monospace; font-size: 11px; color: #9cf; }
.card .scene { color: #ddd; margin-top: 2px; }
.card .desc { color: #888; margin-top: 3px; font-size: 11px; }
.card .actions { display: flex; gap: 6px; padding: 0 10px 10px; }
.card button { flex: 1; padding: 5px; font-size: 12px; border: 1px solid #444; border-radius: 3px;
               background: #2a2a2a; color: #ddd; cursor: pointer; }
.card button.active-keep { background: #275; border-color: #3a7; color: white; }
.card button.active-reject { background: #833; border-color: #c44; color: white; }
.badge { display: inline-block; padding: 2px 6px; font-size: 10px; border-radius: 3px;
         margin-left: 4px; }
.badge.uploaded { background: #2a7; color: white; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,0.9); display: none;
         align-items: center; justify-content: center; z-index: 100; cursor: zoom-out; }
.modal.open { display: flex; }
.modal img { max-width: 95vw; max-height: 95vh; }
"""

PAGE_JS = """
async function setDecision(slug, action, btn) {
  const resp = await fetch('/api/decision', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({slug, action})
  });
  if (!resp.ok) { alert('failed'); return; }
  const card = document.querySelector(`.card[data-slug="${slug}"]`);
  card.className = 'card ' + (action === 'reject' ? 'reject' : '');
  card.querySelectorAll('.actions button').forEach(b => {
    b.classList.remove('active-keep', 'active-reject');
  });
  btn.classList.add(action === 'keep' ? 'active-keep' : 'active-reject');
  updateCounts();
}

function updateCounts() {
  const all = document.querySelectorAll('.card:not(.uploaded)');
  const rejected = document.querySelectorAll('.card.reject:not(.uploaded)');
  const uploaded = document.querySelectorAll('.card.uploaded');
  document.getElementById('c-total').textContent = all.length + uploaded.length;
  document.getElementById('c-keep').textContent = all.length - rejected.length;
  document.getElementById('c-reject').textContent = rejected.length;
  document.getElementById('c-uploaded').textContent = uploaded.length;
}

async function uploadAll() {
  if (!confirm('Upload all kept (non-rejected, non-uploaded) images to Supabase?')) return;
  const btn = document.getElementById('upload-btn');
  btn.disabled = true; btn.textContent = 'Uploading...';
  const resp = await fetch('/api/upload', { method: 'POST' });
  const data = await resp.json();
  btn.disabled = false; btn.textContent = 'Upload approved';
  alert(`Uploaded: ${data.uploaded}/${data.attempted}  Failed: ${data.failed}`);
  location.reload();
}

function openModal(src) {
  const m = document.getElementById('modal');
  document.getElementById('modal-img').src = src;
  m.classList.add('open');
}
document.addEventListener('click', e => {
  if (e.target.id === 'modal' || e.target.id === 'modal-img') {
    document.getElementById('modal').classList.remove('open');
  }
});

function applyFilter() {
  const team = document.getElementById('f-team').value;
  const status = document.getElementById('f-status').value;
  document.querySelectorAll('.card').forEach(c => {
    const teamOk = !team || c.dataset.team === team;
    let statusOk = true;
    if (status === 'keep') statusOk = !c.classList.contains('reject') && !c.classList.contains('uploaded');
    else if (status === 'reject') statusOk = c.classList.contains('reject');
    else if (status === 'uploaded') statusOk = c.classList.contains('uploaded');
    c.style.display = (teamOk && statusOk) ? '' : 'none';
  });
}
"""


def _render_page() -> bytes:
    pngs = _list_pngs()
    teams = sorted({p.stem.split("_", 1)[0] for p in pngs if p.stem.split("_", 1)[0].isupper()})
    teams = ["(all)"] + teams + ["generic"]

    cards = []
    counts = {"keep": 0, "reject": 0, "uploaded": 0}
    for path in pngs:
        slug = path.stem
        row = _plan_by_slug.get(slug, {})
        status = _status_for(slug)
        counts[status] = counts.get(status, 0) + 1
        team = row.get("team_code") or ("generic" if slug.startswith("generic_") else "?")
        scene = row.get("scene", "?")
        desc = (row.get("description") or "")[:120]
        card_class = "card"
        if status == "reject":
            card_class += " reject"
        elif status == "uploaded":
            card_class += " uploaded"
        uploaded_badge = '<span class="badge uploaded">uploaded</span>' if status == "uploaded" else ""
        keep_active = "active-keep" if status == "keep" else ""
        reject_active = "active-reject" if status == "reject" else ""
        disabled = "disabled" if status == "uploaded" else ""
        cards.append(f"""
<div class="{card_class}" data-slug="{slug}" data-team="{team}">
  <img src="/img/{slug}.png" onclick="openModal(this.src)" loading="lazy">
  <div class="meta">
    <div class="slug">{slug}{uploaded_badge}</div>
    <div class="scene">{team} · {scene}</div>
    <div class="desc">{desc}</div>
  </div>
  <div class="actions">
    <button class="{keep_active}" {disabled}
            onclick="setDecision('{slug}', 'keep', this)">Keep</button>
    <button class="{reject_active}" {disabled}
            onclick="setDecision('{slug}', 'reject', this)">Reject</button>
  </div>
</div>
""")

    team_opts = "".join(
        f'<option value="{"" if t == "(all)" else t}">{t}</option>' for t in teams
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Curated pool review</title>
<style>{PAGE_CSS}</style></head><body>
<div class="toolbar">
  <h1>Curated pool review</h1>
  <span class="counts">
    <span id="c-total">{len(pngs)}</span> total ·
    <span id="c-keep">{counts.get("keep", 0)}</span> keep ·
    <span id="c-reject">{counts.get("reject", 0)}</span> reject ·
    <span id="c-uploaded">{counts.get("uploaded", 0)}</span> uploaded
  </span>
  <select class="filter" id="f-team" onchange="applyFilter()">{team_opts}</select>
  <select class="filter" id="f-status" onchange="applyFilter()">
    <option value="">all</option><option value="keep">keep</option>
    <option value="reject">reject</option><option value="uploaded">uploaded</option>
  </select>
  <button class="btn" id="upload-btn" onclick="uploadAll()">Upload approved</button>
</div>
<div class="grid">{"".join(cards)}</div>
<div class="modal" id="modal"><img id="modal-img"></div>
<script>{PAGE_JS}</script>
</body></html>"""
    return html.encode("utf-8")


# ---- HTTP handler ----

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        return

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            body = _render_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/img/"):
            slug = p[len("/img/"):].removesuffix(".png")
            path = RAW_DIR / f"{slug}.png"
            if not path.exists():
                self.send_error(404); return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if p == "/api/decision":
            payload = json.loads(body or b"{}")
            slug = payload.get("slug")
            action = payload.get("action")
            if action not in ("keep", "reject"):
                self.send_error(400, "bad action"); return
            with _lock:
                # don't overwrite 'uploaded'
                if _decisions.get(slug) != "uploaded":
                    _decisions[slug] = action
                    _save_decisions()
            self._json({"ok": True, "slug": slug, "action": action})
            return

        if p == "/api/upload":
            result = asyncio.run(_upload_approved())
            self._json(result)
            return

        self.send_error(404)

    def _json(self, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    _load_plan()
    _load_decisions()
    pngs = _list_pngs()
    print(f"Loaded {len(pngs)} PNGs, {len(_plan_by_slug)} plan rows, "
          f"{len(_decisions)} prior decisions")
    print(f"Serving on http://localhost:{PORT}  (Ctrl-C to stop)")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
