from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.schemas import ArticleQualityDecision, PublishableArticle, StoryEntry
from app.writer.personas import Persona

WIKI_DIRNAME = "wiki"
RAW_FEEDBACK_DIRNAME = "raw_feedback"
REWRITE_LESSONS_FILE = "rewrite_lessons.md"
MAX_MEMORY_CHARS = 12000
MAX_WIKI_PAGE_CHARS = 7000


def _read_markdown(path: Path, *, max_chars: int = MAX_WIKI_PAGE_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[truncated]"


def load_editorial_memory(memory_dir: Path, story: StoryEntry) -> str:
    """Return compact markdown coaching notes for the EN writer.

    This intentionally reads a maintained wiki, not raw event logs. The content
    is advisory context; the writer prompt keeps source_digests as the factual
    authority.
    """
    wiki_dir = memory_dir / WIKI_DIRNAME
    if not wiki_dir.exists():
        return ""

    pages: list[tuple[str, str]] = []
    preferred = [
        "what_makes_a_story_readworthy.md",
        "headline_patterns_that_work.md",
        "thin_story_rejection_rules.md",
        REWRITE_LESSONS_FILE,
    ]
    seen: set[Path] = set()
    for name in preferred:
        path = wiki_dir / name
        if path.exists():
            pages.append((name, _read_markdown(path)))
            seen.add(path)
    for path in sorted(wiki_dir.glob("*.md")):
        if path not in seen:
            pages.append((path.name, _read_markdown(path)))

    heading = (
        "Editorial memory for this EN draft. Treat as coaching, not facts.\n"
        f"Story: {story.cluster_headline}\n"
        f"Teams: {', '.join(story.team_codes) or 'unknown'}\n"
    )
    sections = [heading]
    for name, text in pages:
        if not text:
            continue
        sections.append(f"\n--- {name} ---\n{text}")

    memory = "\n".join(sections).strip()
    if len(memory) <= MAX_MEMORY_CHARS:
        return memory
    return memory[:MAX_MEMORY_CHARS].rstrip() + "\n\n[editorial memory truncated]"


def build_feedback_event_markdown(
    *,
    cycle_id: str,
    story: StoryEntry,
    article: PublishableArticle,
    persona: Persona,
    decision: ArticleQualityDecision,
    rewrite_attempt: int,
) -> str:
    timestamp = datetime.now(UTC).isoformat()
    brief = decision.rewrite_brief or ""
    return (
        f"## {timestamp} | {decision.decision.upper()} | attempt {rewrite_attempt}\n\n"
        f"- cycle_id: `{cycle_id}`\n"
        f"- fingerprint: `{story.story_fingerprint}`\n"
        f"- story: {story.cluster_headline}\n"
        f"- headline: {article.headline}\n"
        f"- persona: {persona.byline} ({persona.id})\n"
        f"- scores: impact={decision.impact_score:.2f}, "
        f"specificity={decision.specificity_score:.2f}, "
        f"readworthiness={decision.readworthiness_score:.2f}, "
        f"grounding={decision.grounding_score:.2f}, "
        f"execution={decision.execution_score:.2f}\n"
        f"- reasoning: {decision.reasoning}\n"
        f"- rewrite_brief: {brief or 'n/a'}\n\n"
    )


def append_raw_feedback(memory_dir: Path, event_markdown: str) -> Path:
    raw_dir = memory_dir / RAW_FEEDBACK_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{datetime.now(UTC).date().isoformat()}.md"
    if not path.exists():
        path.write_text("# Editorial Feedback Events\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event_markdown)
    return path


def read_rewrite_lessons(memory_dir: Path) -> str:
    return _read_markdown(
        memory_dir / WIKI_DIRNAME / REWRITE_LESSONS_FILE,
        max_chars=8000,
    )


def write_rewrite_lessons(memory_dir: Path, markdown: str) -> Path:
    wiki_dir = memory_dir / WIKI_DIRNAME
    wiki_dir.mkdir(parents=True, exist_ok=True)
    path = wiki_dir / REWRITE_LESSONS_FILE
    path.write_text(markdown.strip() + "\n", encoding="utf-8")
    return path


def build_memory_revision_payload(
    *,
    existing_markdown: str,
    feedback_event_markdown: str,
) -> dict[str, str]:
    return {
        "target_page": f"{WIKI_DIRNAME}/{REWRITE_LESSONS_FILE}",
        "existing_markdown": existing_markdown,
        "new_feedback_event": feedback_event_markdown,
    }
