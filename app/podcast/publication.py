"""Personal-podcast publication rehearsal artifacts.

This module keeps the current personal-only Spotify flow, but makes the
listener-facing packet look like something we would be comfortable sharing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.podcast.artifacts import (
    load_claims,
    load_script,
    load_sources,
    read_json,
    validate_episode_artifacts,
    write_json,
)
from app.podcast.factcheck import PodcastClaim, PodcastSource
from app.podcast.schemas import PodcastScript


AI_DISCLOSURE_DE = (
    "Diese Folge wurde KI-gestützt aus den unten genannten öffentlichen Quellen "
    "erstellt, mit synthetischen Stimmen gesprochen und vor der Veröffentlichung "
    "gegen Quellen- und Claim-Notizen geprüft."
)

INTERNAL_METADATA_RE = re.compile(
    r"(?i)(codex|runtime|artifact|manifest|vps|supabase|openai|gemini|token|"
    r"prompt|/users/|/tmp/|\.json|erstellt im codex-runtime-flow)"
)
PRIVATE_SCRIPT_RE = re.compile(
    r"(?i)(tobias|tobi|hostinger|supabase|openai|gemini|codex|vps|token|"
    r"secret|api[_ -]?key|gmail|calendar|client|kunde|meeting|/users/|/tmp/)"
)
GENERIC_TITLE_RE = re.compile(r"(?i)^T4L Morgenbriefing(?:\s*-\s*\d{4}-\d{2}-\d{2})?$")
MAX_SPOTIFY_SUMMARY_CHARS = 400
MAX_THUMBNAIL_BYTES = 1_000_000


@dataclass(frozen=True)
class PreparedPublication:
    """Listener-facing artifact packet used by the personal upload step."""

    title: str
    summary: str
    show_notes_path: Path
    metadata_path: Path
    safety_report_path: Path
    thumbnail_prompt_path: Path
    thumbnail_path: Path | None


def _clean_title(manifest: dict[str, Any], script: PodcastScript) -> str:
    title = (script.episode_title or manifest.get("title") or "").strip()
    if not title or GENERIC_TITLE_RE.match(title) or INTERNAL_METADATA_RE.search(title):
        section_title = script.sections[0].title if script.sections else "NFL-Morgen"
        title = f"T4L Morgen: {section_title}"
    return title[:120].strip()


def _clean_summary(manifest: dict[str, Any], script: PodcastScript) -> str:
    summary = (script.episode_summary or manifest.get("summary") or "").strip()
    if not summary or INTERNAL_METADATA_RE.search(summary):
        titles = [section.title for section in script.sections[:3] if section.title]
        if titles:
            summary = "Marcus und Robin sprechen über " + ", ".join(titles) + "."
        else:
            summary = "Marcus und Robin sortieren die wichtigsten NFL-Themen des Morgens."
    return _trim_sentence(summary, MAX_SPOTIFY_SUMMARY_CHARS)


def _trim_sentence(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    trimmed = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(".,;:")
    return trimmed + "…"


def _public_sources(sources: list[PodcastSource]) -> list[PodcastSource]:
    public: list[PodcastSource] = []
    seen: set[str] = set()
    for source in sources:
        url = source.url.strip()
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        seen.add(url)
        public.append(source)
    return public


def _uses_nflverse(claims: list[PodcastClaim]) -> bool:
    return any(source_id.upper() == "NFLVERSE" for claim in claims for source_id in claim.source_ids)


def _build_show_notes(
    *,
    title: str,
    summary: str,
    sources: list[PodcastSource],
    claims: list[PodcastClaim],
) -> str:
    lines = [
        f"# {title}",
        "",
        summary,
        "",
        "## Hinweis",
        "",
        AI_DISCLOSURE_DE,
        "",
        "## Quellen",
        "",
    ]
    for source in sources:
        label = source.title.strip() or source.publisher.strip() or source.url
        publisher = source.publisher.strip()
        prefix = f"{publisher}: " if publisher and publisher not in label else ""
        lines.append(f"- {prefix}[{label}]({source.url})")
    if _uses_nflverse(claims) and not any(source.id.upper() == "NFLVERSE" for source in sources):
        lines.append("- nflverse: [nflreadpy](https://nflreadpy.nflverse.com/)")
    lines.extend(
        [
            "",
            "## Quellenpolitik",
            "",
            (
                "Die Quellenliste enthält nur öffentliche Links und Datenquellen. "
                "Interne Notizen, Claim-IDs, lokale Dateien und Automationsdetails "
                "werden nicht veröffentlicht."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def build_thumbnail_prompt(
    *,
    title: str,
    summary: str,
    script: PodcastScript,
) -> str:
    topics = ", ".join(section.title for section in script.sections[:4] if section.title)
    return "\n".join(
        [
            "Use case: stylized-concept",
            "Asset type: square podcast episode thumbnail, 1024x1024",
            f"Episode title for context only, do not render text: {title}",
            f"Episode summary: {summary}",
            f"Topic context: {topics}",
            "Primary request: Create a copyright-friendly editorial football image for a German NFL morning podcast episode.",
            "Scene/backdrop: abstract night stadium lights, generic football field texture, play-diagram lines, schedule-board energy.",
            "Subject: anonymous football atmosphere and tactical pressure, no recognizable person.",
            "Style: premium sports editorial, cinematic, high contrast, polished but not photorealistic likeness-driven.",
            "Composition: strong central visual hook, square crop, clear at small thumbnail size, no text areas required.",
            "Constraints: no text, no letters, no numbers, no logos, no NFL shield, no team marks, no team uniforms, no helmet decals, no player likenesses, no recognizable real people, no broadcast graphics, no copied source imagery, no watermark.",
        ]
    )


async def generate_thumbnail_with_cli(
    *,
    settings: Settings,
    prompt: str,
    output_path: Path,
) -> None:
    if not settings.podcast_thumbnail_cli_path.exists():
        raise FileNotFoundError(f"thumbnail CLI not found: {settings.podcast_thumbnail_cli_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())
    argv = [
        "python",
        str(settings.podcast_thumbnail_cli_path),
        "generate",
        "--model",
        settings.podcast_thumbnail_image_model,
        "--quality",
        settings.podcast_thumbnail_image_quality,
        "--size",
        settings.podcast_thumbnail_image_size,
        "--output-format",
        "png",
        "--prompt",
        prompt,
        "--out",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or stdout).decode(errors="replace")[-800:]
        raise RuntimeError(f"thumbnail generation failed ({proc.returncode}): {tail}")


def _image_dimensions(path: Path) -> tuple[str, int, int]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "png", int(width), int(height)
    if data.startswith(b"\xff\xd8"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            segment_len = int.from_bytes(data[idx : idx + 2], "big")
            if marker in range(0xC0, 0xC4):
                height = int.from_bytes(data[idx + 3 : idx + 5], "big")
                width = int.from_bytes(data[idx + 5 : idx + 7], "big")
                return "jpeg", width, height
            idx += segment_len
    raise ValueError(f"unsupported thumbnail image format: {path}")


def validate_thumbnail(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"thumbnail not found: {path}")
    size = path.stat().st_size
    if size > MAX_THUMBNAIL_BYTES:
        raise ValueError(f"thumbnail is too large for Spotify: {size} bytes")
    fmt, width, height = _image_dimensions(path)
    if width != height:
        raise ValueError(f"thumbnail must be square; got {width}x{height}")
    return {"path": str(path), "format": fmt, "width": width, "height": height, "bytes": size}


def normalize_thumbnail(path: Path) -> Path:
    """Crop/resize/compress generated art into a Spotify-safe square image."""

    try:
        info = validate_thumbnail(path)
    except ValueError:
        info = None
    if info and info["bytes"] <= MAX_THUMBNAIL_BYTES:
        return path

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency/runtime guard
        raise RuntimeError(
            "Pillow is required to normalize oversized/non-square thumbnails"
        ) from exc

    with Image.open(path) as image:
        image = image.convert("RGB")
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side))
        image = image.resize((1024, 1024), Image.Resampling.LANCZOS)
        out_path = path.with_suffix(".jpg")
        quality = 90
        while quality >= 60:
            image.save(out_path, format="JPEG", quality=quality, optimize=True)
            if out_path.stat().st_size <= MAX_THUMBNAIL_BYTES:
                validate_thumbnail(out_path)
                return out_path
            quality -= 10
    raise ValueError(f"could not compress thumbnail below {MAX_THUMBNAIL_BYTES} bytes")


async def prepare_publication_rehearsal(
    *,
    episode_dir: Path,
    settings: Settings,
    dry_run: bool = False,
) -> PreparedPublication:
    validate_episode_artifacts(episode_dir)

    manifest = read_json(episode_dir / "manifest.json")
    script = load_script(episode_dir)
    sources = load_sources(episode_dir)
    claims = load_claims(episode_dir)
    audio_probe = read_json(episode_dir / "audio_probe.json")
    if not audio_probe.get("audio_path"):
        raise ValueError("audio_probe.json has no audio_path")

    title = _clean_title(manifest, script)
    summary = _clean_summary(manifest, script)
    public_sources = _public_sources(sources)
    if not public_sources:
        raise ValueError("no public sources available for listener-facing show notes")
    for line in script.all_lines():
        if PRIVATE_SCRIPT_RE.search(line.text):
            raise ValueError("script contains private or internal operational wording")
    if INTERNAL_METADATA_RE.search(title) or INTERNAL_METADATA_RE.search(summary):
        raise ValueError("public metadata contains internal operational wording")

    show_notes = _build_show_notes(
        title=title,
        summary=summary,
        sources=public_sources,
        claims=claims,
    )
    if re.search(r"\[C[A-Za-z0-9_.:-]+\]|source_ledger|claim_ledger|research_|/Users/|/tmp/", show_notes):
        raise ValueError("show notes contain internal artifact details")

    thumbnail_path = episode_dir / "thumbnail.png"
    prompt = build_thumbnail_prompt(title=title, summary=summary, script=script)
    thumbnail_prompt = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": settings.podcast_thumbnail_image_model,
        "quality": settings.podcast_thumbnail_image_quality,
        "size": settings.podcast_thumbnail_image_size,
        "prompt": prompt,
        "copyright_safety": {
            "no_logos": True,
            "no_real_people_or_player_likenesses": True,
            "no_source_images": True,
            "no_generated_text": True,
        },
    }

    thumbnail_info: dict[str, Any] | None = None
    final_thumbnail_path: Path | None = None
    if settings.podcast_thumbnail_enabled:
        if not thumbnail_path.exists() and not dry_run:
            await generate_thumbnail_with_cli(
                settings=settings,
                prompt=prompt,
                output_path=thumbnail_path,
            )
        if thumbnail_path.exists():
            final_thumbnail_path = normalize_thumbnail(thumbnail_path)
            thumbnail_info = validate_thumbnail(final_thumbnail_path)
        elif not dry_run:
            raise FileNotFoundError(f"thumbnail not found: {thumbnail_path}")

    metadata = {
        "title": title,
        "summary": summary,
        "ai_disclosure": AI_DISCLOSURE_DE,
        "source_count": len(public_sources),
        "thumbnail_path": str(final_thumbnail_path) if thumbnail_info else None,
    }
    report = {
        "ok": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": {
            "artifact_validation": "passed",
            "public_sources": len(public_sources),
            "metadata_clean": True,
            "show_notes_clean": True,
            "thumbnail": thumbnail_info or ("skipped_for_dry_run" if dry_run else None),
        },
    }

    metadata_path = episode_dir / "public_metadata.json"
    show_notes_path = episode_dir / "show_notes.md"
    safety_report_path = episode_dir / "publication_safety_report.json"
    thumbnail_prompt_path = episode_dir / "thumbnail_prompt.json"

    write_json(metadata_path, metadata)
    show_notes_path.write_text(show_notes, encoding="utf-8")
    write_json(safety_report_path, report)
    write_json(thumbnail_prompt_path, thumbnail_prompt)

    return PreparedPublication(
        title=title,
        summary=summary,
        show_notes_path=show_notes_path,
        metadata_path=metadata_path,
        safety_report_path=safety_report_path,
        thumbnail_prompt_path=thumbnail_prompt_path,
        thumbnail_path=final_thumbnail_path if thumbnail_info else None,
    )


def dry_run_thumbnail_invocation(settings: Settings, output_path: Path) -> str:
    return shlex.join(
        [
            "python",
            str(settings.podcast_thumbnail_cli_path),
            "generate",
            "--model",
            settings.podcast_thumbnail_image_model,
            "--quality",
            settings.podcast_thumbnail_image_quality,
            "--size",
            settings.podcast_thumbnail_image_size,
            "--output-format",
            "png",
            "--prompt",
            "<thumbnail_prompt>",
            "--out",
            str(output_path),
        ]
    )
