"""Team Beat workflow — twice-daily, per-team end-to-end cycle.

Per cycle:
  1. Read the last 12h of raw articles from public.raw_articles via
     RawArticleDbReader.
  2. Filter per team (NYJ, CHI in MVP) on entity_type='team' matches.
  3. Per team, in parallel:
       a. Run the Team Beat Reporter Agent → BeatBrief.
            - If `should_file=False`, record `no_news` and stop.
       b. Run the Article Quality Gate Agent on the brief.
            - dismiss → record `error` (gated out).
            - rewrite → for MVP: log + accept the original (no rewrite
              loop yet to keep cycle simple; can add later).
       c. Run the Radio Script Agent (DE only) → RadioScript.
  4. Collect all surviving (BeatBrief, RadioScript) pairs and submit
     them in ONE Gemini TTS batch (`create → status → process`).
  5. Per team:
       - Upsert public.team_roundup with audio_url (or NULL if TTS
         failed for that item).
       - Record `filed` outcome (or `error` if persistence failed)
         in public.team_beat_cycle_state.

Design choices worth flagging:
  * Per-team error isolation: a crash in one team's pipeline does not
    abort the other. Every team gets a BeatCycleResult written, even on
    error.
  * Brief-without-audio is acceptable: if TTS fails for a team, we still
    persist the written brief with audio_url=NULL so it's recoverable.
  * The single TTS batch is the cost lever (per docs/team_beat_mvp.md §7).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agents import Agent, Runner

from app.config import Settings
from app.editorial.helpers import coerce_output
from app.editorial.tracing import build_run_config
from app.schemas import ArticleQualityDecision, RawArticle
from app.team_beat.agents import (
    build_radio_script_agent,
    build_team_beat_reporter_agent,
)
from app.team_beat.personas import (
    STUDIO_ANCHOR,
    TEAM_BEAT_PERSONAS,
    TeamBeatPersona,
    get_team_beat_persona,
    supported_team_codes,
)
from app.team_beat.schemas import (
    BeatBrief,
    BeatCycleResult,
    BeatOutcome,
    BeatRoundup,
    CycleSlot,
    RadioScript,
    TTSItem,
)
from app.team_beat.tools import build_article_lookup_tool
from app.team_beat.tts_client import TTSBatchClient, TTSBatchError
from app.team_codes import team_full_name
from app.writer.agents import build_article_quality_gate_agent

if TYPE_CHECKING:
    from app.adapters import (
        ArticleLookupFromDb,
        BeatCycleStateStore,
        BeatRoundupWriter,
        RawArticleDbReader,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamBeatCycleSummary:
    """High-level cycle outcome (returned from the workflow + CLI).

    Lists per-team results so the CLI can echo a one-line summary and
    monitoring can extract per-team counts. Stable shape; safe to
    serialise as JSON.
    """

    cycle_ts: datetime
    cycle_slot: CycleSlot
    teams: list[BeatCycleResult]

    @property
    def filed_count(self) -> int:
        return sum(1 for t in self.teams if t.outcome is BeatOutcome.FILED)

    @property
    def no_news_count(self) -> int:
        return sum(1 for t in self.teams if t.outcome is BeatOutcome.NO_NEWS)

    @property
    def error_count(self) -> int:
        return sum(1 for t in self.teams if t.outcome is BeatOutcome.ERROR)


# --- helpers ----------------------------------------------------------------


def _derive_cycle_slot(now: datetime) -> CycleSlot:
    """Map the cron firing UTC hour to a Berlin-time slot label.

    The cron runs at 04:00 + 16:00 UTC (= 06:00 + 18:00 Berlin winter
    time). DST drift is accepted at MVP scale per the doc's open-question
    call. Hour < 12 UTC = AM drop; otherwise PM.
    """
    return "AM" if now.hour < 12 else "PM"


def _filter_articles_for_team(
    articles: list[RawArticle], team_code: str
) -> list[RawArticle]:
    """Return articles tagged with this team.

    Match is on `entity_type == 'team'` AND `entity_id == team_code` to
    avoid false positives where the team name appears casually in the
    body without being a tagged entity.
    """
    out: list[RawArticle] = []
    for article in articles:
        for entity in article.entities:
            if entity.entity_type == "team" and entity.entity_id == team_code:
                out.append(article)
                break
    return out


def _serialize_articles_for_agent(articles: list[RawArticle]) -> list[dict]:
    """Compact dict shape suitable for the team-beat reporter agent input.

    The agent doesn't need the full ingestion schema — it needs URL,
    title, source, category, the entity list (so it can reason about
    named players in the window), and the article id (for traceability)."""
    return [
        {
            "id": a.id,
            "url": a.url,
            "title": a.title,
            "source_name": a.source_name,
            "category": a.category,
            "entities": [
                {"type": e.entity_type, "id": e.entity_id, "name": e.matched_name}
                for e in a.entities
            ],
        }
        for a in articles
    ]


def _tts_item_id(team_code: str, cycle_ts: datetime) -> str:
    """Deterministic item id per docs/team_beat_mvp.md §7.

    Format: `{team_code}-{cycle_iso_ts}`. The TTS batch service writes
    one MP3 per item id; we map it back to a team via this prefix.
    """
    return f"{team_code}-{cycle_ts.isoformat()}"


def _tts_path_prefix_suffix(cycle_ts: datetime, slot: CycleSlot) -> str:
    """Per-cycle subfolder under the configured prefix.

    Format: `YYYY-MM-DD_AM` / `YYYY-MM-DD_PM`. Upsert-overwriteable so
    cron reruns don't accumulate orphans.
    """
    return f"{cycle_ts.strftime('%Y-%m-%d')}_{slot}"


def _dateline_stamp_en(persona: TeamBeatPersona, cycle_ts: datetime) -> str:
    """Wire-style dateline header for the EN body (the Delighter, per
    docs/team_beat_mvp.md). Mirrors the audio anchor framing on the
    written surface: 'Filed by [byline] — covering the [team], [city] · [ts]'.
    """
    team = team_full_name(persona.team_code) or persona.team_code
    ts = cycle_ts.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"Filed by {persona.byline} — covering the {team}, "
        f"{persona.dateline_city} · {ts}"
    )


def _dateline_stamp_de(persona: TeamBeatPersona, cycle_ts: datetime) -> str:
    """German dateline header. Same shape, German register."""
    team = team_full_name(persona.team_code) or persona.team_code
    ts = cycle_ts.strftime("%d.%m.%Y %H:%M UTC")
    return (
        f"Bericht von {persona.byline} — Berichterstattung zu den {team} "
        f"aus {persona.dateline_city} · {ts}"
    )


def _stamp_brief_bodies(
    brief: BeatBrief, persona: TeamBeatPersona, cycle_ts: datetime
) -> BeatBrief:
    """Return a copy of `brief` with dateline stamps prepended to both
    bodies. Applied at persistence time — agents (quality gate, radio
    script) see the clean, unstamped prose so they don't moralize about
    the byline or echo it in the audio."""
    en = f"{_dateline_stamp_en(persona, cycle_ts)}\n\n{brief.en_body}"
    de = f"{_dateline_stamp_de(persona, cycle_ts)}\n\n{brief.de_body}"
    return brief.model_copy(update={"en_body": en, "de_body": de})


# --- per-team pipeline ------------------------------------------------------


@dataclass
class _TeamWork:
    """Mutable per-team state passed through the cycle stages.

    Only used inside TeamBeatWorkflow; not exported.

    `tts_batch_id` is set as soon as the create+status stage succeeds
    (across all teams in the cycle) — *before* process runs. This means
    even when process crashes/times out, the batch_id lands in
    public.team_roundup so `scripts/tts_recover.py` can recover the
    audio without operator lookup in the Gemini console.
    """

    team_code: str
    persona: TeamBeatPersona
    articles: list[RawArticle]
    brief: BeatBrief | None = None
    quality_decision: ArticleQualityDecision | None = None
    radio_script: RadioScript | None = None
    audio_url: str | None = None
    tts_batch_id: str | None = None
    outcome: BeatOutcome = BeatOutcome.ERROR
    reason: str = ""


class TeamBeatWorkflow:
    """Orchestrates one team-beat cycle for a configurable team set."""

    def __init__(
        self,
        *,
        settings: Settings,
        feed_reader: "RawArticleDbReader",
        roundup_writer: "BeatRoundupWriter",
        cycle_state_store: "BeatCycleStateStore",
        tts_client: TTSBatchClient,
        article_lookup: "ArticleLookupFromDb | None" = None,
        team_codes: tuple[str, ...] | None = None,
        lookback_hours: int = 12,
    ) -> None:
        self._settings = settings
        self._feed_reader = feed_reader
        self._roundup_writer = roundup_writer
        self._cycle_state_store = cycle_state_store
        self._tts_client = tts_client
        # Optional — when present, the reporter agent gets a lookup tool
        # so it can fetch full article bodies for the 1-3 articles it
        # judges load-bearing for the brief. When None (test fixtures,
        # CLI dry-runs without a DB), the agent works headline-only.
        self._article_lookup = article_lookup
        self._team_codes = team_codes or supported_team_codes()
        self._lookback_hours = lookback_hours

        # Validate scope upfront so a typo in --teams fails fast rather
        # than silently producing zero output.
        unknown = [c for c in self._team_codes if c not in TEAM_BEAT_PERSONAS]
        if unknown:
            raise ValueError(
                f"No team beat persona registered for: {unknown}. "
                f"MVP supports {sorted(TEAM_BEAT_PERSONAS.keys())}."
            )

        # The OpenAI Agents SDK reads OPENAI_API_KEY from the process env
        # at first model call. Pydantic has it stashed in a SecretStr;
        # bridge it here (mirror writer.workflow.WriterWorkflow.__init__).
        os.environ.setdefault(
            "OPENAI_API_KEY", settings.openai_api_key.get_secret_value()
        )

        reporter_tools = (
            (build_article_lookup_tool(self._article_lookup),)
            if self._article_lookup is not None
            else ()
        )
        self._reporter_agent = build_team_beat_reporter_agent(
            settings, tools=reporter_tools
        )
        self._quality_gate_agent = build_article_quality_gate_agent(settings)
        self._radio_script_agent = build_radio_script_agent(settings)

    # --- agent calls -------------------------------------------------------

    async def _run_reporter(
        self, work: _TeamWork, cycle_id: str
    ) -> BeatBrief:
        payload = {
            "team_code": work.team_code,
            "team_full_name": team_full_name(work.team_code) or work.team_code,
            "persona": dataclasses.asdict(work.persona),
            "lookback_hours": self._lookback_hours,
            "articles": _serialize_articles_for_agent(work.articles),
        }
        run_config = build_run_config(
            cycle_id,
            stage="team_beat_reporter",
            metadata={"team_code": work.team_code},
        )
        # max_turns=4 is the *code-enforced* hard cap on lookups. With
        # parallel_tool_calls=False (set in build_team_beat_reporter_agent)
        # and structured output_type=BeatBrief, every Agents SDK turn is
        # either ONE tool call OR the final structured response — never
        # both, never neither. So 4 turns = at most 3 lookup_article_content
        # calls before the model is forced to emit the brief, exactly
        # matching the 3-lookup budget the prompt declares. The prompt
        # discipline alone could be ignored; this turn ceiling cannot.
        result = await Runner.run(
            self._reporter_agent,
            json.dumps(payload, separators=(",", ":")),
            run_config=run_config,
            max_turns=4,
            auto_previous_response_id=True,
        )
        brief = coerce_output(result.final_output, BeatBrief)
        # Deterministic overrides — never trust the agent to echo input
        # constants verbatim.
        return brief.model_copy(update={
            "team_code": work.team_code,
            "persona_name": work.persona.byline,
            "dateline_city": brief.dateline_city or work.persona.dateline_city,
        })

    async def _run_quality_gate(
        self, work: _TeamWork, cycle_id: str
    ) -> ArticleQualityDecision:
        # Reuse the existing article quality gate agent. It's calibrated
        # against PublishableArticle but the schema overlap is enough for
        # MVP: we feed it the EN body wrapped in the article-shaped
        # payload it knows how to read.
        assert work.brief is not None
        article_shape = {
            "headline": work.brief.headline,
            "sub_headline": "",
            "introduction": "",
            "content": work.brief.en_body,
            "x_post": "",
            "bullet_points": [],
            "language": "en-US",
            "team": work.team_code,
            "image": "",
            "tts_file": "",
            "author": work.brief.persona_name,
            "mentioned_players": [],
            "sources": [],
            "story_fingerprint": f"team-beat:{_tts_item_id(work.team_code, datetime.now(UTC))}",
        }
        payload = {
            "story": {
                "cluster_headline": work.brief.headline,
                "story_fingerprint": article_shape["story_fingerprint"],
                "action": "publish",
                "news_value_score": 0.0,
                "team_codes": [work.team_code],
                "player_mentions": [],
            },
            "source_digests": [
                {
                    "story_id": a.id,
                    "url": a.url,
                    "title": a.title,
                    "source_name": a.source_name,
                    "summary": "",
                    "key_facts": [],
                    "confidence": 0.0,
                    "content_status": "thin",
                    "team_mentions": [work.team_code],
                }
                for a in work.articles
            ],
            "article": article_shape,
            "persona": {
                "id": work.persona.archetype,
                "byline": work.persona.byline,
                "role": work.persona.role_en,
                "style_guide": work.persona.style_guide_en,
            },
            "rewrite_attempt": 0,
        }
        run_config = build_run_config(
            cycle_id,
            stage="team_beat_quality_gate",
            metadata={"team_code": work.team_code},
        )
        try:
            result = await Runner.run(
                self._quality_gate_agent,
                json.dumps(payload, separators=(",", ":")),
                run_config=run_config,
                max_turns=3,
                auto_previous_response_id=True,
            )
            return coerce_output(result.final_output, ArticleQualityDecision)
        except Exception as exc:
            logger.warning(
                "Quality gate unavailable for %s: %s — fail-soft approve",
                work.team_code, exc,
            )
            return ArticleQualityDecision(
                decision="approve",
                impact_score=0.5,
                specificity_score=0.5,
                readworthiness_score=0.5,
                grounding_score=0.5,
                execution_score=0.5,
                reasoning="Quality gate unavailable; approving by fail-soft policy.",
                rewrite_brief=None,
            )

    async def _run_radio_script(
        self, work: _TeamWork, cycle_id: str
    ) -> RadioScript:
        assert work.brief is not None
        payload = {
            "team_code": work.team_code,
            "team_full_name": team_full_name(work.team_code) or work.team_code,
            "studio_anchor": dataclasses.asdict(STUDIO_ANCHOR),
            "reporter_byline": work.persona.byline,
            "reporter_role_de": work.persona.role_de,
            "dateline_city": work.persona.dateline_city,
            "headline": work.brief.headline,
            "de_body": work.brief.de_body,
        }
        run_config = build_run_config(
            cycle_id,
            stage="team_beat_radio_script",
            metadata={"team_code": work.team_code},
        )
        result = await Runner.run(
            self._radio_script_agent,
            json.dumps(payload, separators=(",", ":")),
            run_config=run_config,
            max_turns=3,
            auto_previous_response_id=True,
        )
        script = coerce_output(result.final_output, RadioScript)
        return script.model_copy(update={"team_code": work.team_code})

    # --- pipeline ----------------------------------------------------------

    async def _produce_brief_and_script(
        self, work: _TeamWork, cycle_id: str
    ) -> None:
        """Stages 1-3 for one team. Updates work.* in place. Errors are
        captured into work.outcome/reason; never re-raised."""
        try:
            work.brief = await self._run_reporter(work, cycle_id)
        except Exception as exc:
            work.outcome = BeatOutcome.ERROR
            work.reason = f"Beat reporter agent failed: {exc}"
            logger.exception("Reporter failed for %s", work.team_code)
            return

        if not work.brief.should_file:
            work.outcome = BeatOutcome.NO_NEWS
            work.reason = work.brief.skip_reason or "Beat reporter judged window not worth filing."
            logger.info(
                "Beat reporter %s: no_news (%s articles in window) — %s",
                work.team_code, len(work.articles), work.reason,
            )
            return

        try:
            work.quality_decision = await self._run_quality_gate(work, cycle_id)
        except Exception as exc:
            # _run_quality_gate already fail-soft approves on its own
            # exceptions, so this branch is for genuinely unexpected
            # crashes (e.g. coerce_output rejecting a malformed result).
            work.outcome = BeatOutcome.ERROR
            work.reason = f"Quality gate crashed unexpectedly: {exc}"
            logger.exception("Quality gate crashed for %s", work.team_code)
            return

        if work.quality_decision.decision == "dismiss":
            work.outcome = BeatOutcome.ERROR
            work.reason = (
                f"Quality gate dismissed brief: {work.quality_decision.reasoning}"
            )
            logger.info(
                "Beat reporter %s: dismissed by gate — %s",
                work.team_code, work.reason,
            )
            return
        # 'rewrite' is treated as 'accept original' for MVP; a rewrite
        # loop would mean a second reporter call + second gate call,
        # which we'll add only if first-cycle data shows it's needed.

        try:
            work.radio_script = await self._run_radio_script(work, cycle_id)
        except Exception as exc:
            work.outcome = BeatOutcome.ERROR
            work.reason = f"Radio script agent failed: {exc}"
            logger.exception("Radio script failed for %s", work.team_code)
            return

        # All three stages succeeded — TTS + persistence happen below in
        # a batched cross-team step.
        work.outcome = BeatOutcome.FILED  # provisional; TTS/persist may downgrade

    async def _run_tts_batch(
        self,
        works_with_scripts: list[_TeamWork],
        cycle_ts: datetime,
        cycle_slot: CycleSlot,
    ) -> None:
        """Create the Gemini batch, wait for SUCCEEDED, then process.

        Mutates each `work` in `works_with_scripts` in place:
          * Sets `work.tts_batch_id` after the create+status stage
            succeeds (so the batch_id lands in DB even when process
            fails).
          * Sets `work.audio_url` after process succeeds.

        All errors are caught and logged; the workflow keeps the
        `filed` outcome with a NULL audio_url. The batch_id alone
        is enough for `scripts/tts_recover.py` to finish the job
        out-of-band later.
        """
        if not works_with_scripts:
            return

        items: list[TTSItem] = []
        for work in works_with_scripts:
            assert work.radio_script is not None
            items.append(TTSItem(
                id=_tts_item_id(work.team_code, cycle_ts),
                text=work.radio_script.de_text,
                title=f"{work.team_code} {cycle_slot}",
            ))

        # Stage 1: create + status.
        # Capture the batch_id whether the stage succeeded or failed —
        # a failed batch's id is still useful for diagnostics. But only
        # advance to stage 2 when create_and_wait returned normally; a
        # TTSBatchError means the upstream Gemini batch is terminal in
        # a non-success state and there's no output file to process.
        batch_id: str | None = None
        create_succeeded = False
        try:
            batch_id = await self._tts_client.create_and_wait(items)
            create_succeeded = True
        except TTSBatchError as exc:
            batch_id = exc.batch_id  # may be None if create itself failed
            logger.error(
                "TTS create_and_wait failed (batch_id=%s, state=%s): %s",
                batch_id, exc.state, exc,
            )
        except Exception as exc:
            logger.exception("TTS create_and_wait crashed: %s", exc)

        # Persist the batch_id (success or diagnostic) on every team's
        # roundup row before process runs.
        for work in works_with_scripts:
            work.tts_batch_id = batch_id

        if not create_succeeded:
            return

        # Stage 2: process. Failures here are non-fatal — the batch_id
        # is already on every work, so `scripts/tts_recover.py` can
        # finish the job out-of-band later.
        try:
            outcome = await self._tts_client.process_batch(
                batch_id,
                [item.id for item in items],
                path_prefix_suffix=_tts_path_prefix_suffix(cycle_ts, cycle_slot),
            )
        except Exception as exc:
            logger.exception(
                "TTS process_batch crashed for batch_id=%s (audio recoverable "
                "via scripts/tts_recover.py): %s",
                batch_id, exc,
            )
            return

        for work in works_with_scripts:
            work.audio_url = outcome.url_for(_tts_item_id(work.team_code, cycle_ts))

    async def _persist_team(
        self,
        work: _TeamWork,
        cycle_ts: datetime,
        cycle_slot: CycleSlot,
    ) -> None:
        """Write team_roundup + team_beat_cycle_state for one team.

        Updates work.outcome/reason if persistence fails. Never raises.
        """
        roundup_id: int | None = None
        if work.outcome is BeatOutcome.FILED:
            assert work.brief is not None and work.radio_script is not None
            # Apply the wire-style dateline stamp here (post-gate, post-radio)
            # so the agents above never see it in the prose they reason about.
            stamped_brief = _stamp_brief_bodies(work.brief, work.persona, cycle_ts)
            roundup = BeatRoundup(
                team_code=work.team_code,
                cycle_ts=cycle_ts,
                cycle_slot=cycle_slot,
                persona_name=work.persona.byline,
                en_body=stamped_brief.en_body,
                de_body=stamped_brief.de_body,
                radio_script=work.radio_script.de_text,
                audio_url=work.audio_url,        # may be None on TTS process failure
                tts_batch_id=work.tts_batch_id,  # set by _run_tts_batch even on failure
            )
            try:
                roundup_id = await self._roundup_writer.upsert(roundup)
            except Exception as exc:
                work.outcome = BeatOutcome.ERROR
                work.reason = f"team_roundup upsert failed: {exc}"
                logger.exception("Roundup upsert failed for %s", work.team_code)

        result = BeatCycleResult(
            team_code=work.team_code,
            cycle_ts=cycle_ts,
            cycle_slot=cycle_slot,
            outcome=work.outcome,
            reason=work.reason,
            article_count=len(work.articles),
            roundup_id=roundup_id,
        )
        try:
            await self._cycle_state_store.record(result)
        except Exception as exc:
            # State logging failure is loud-but-non-fatal: the cycle did
            # what it did even if the audit row didn't land.
            logger.error(
                "Failed to record beat cycle state for %s: %s",
                work.team_code, exc,
            )

    # --- public entrypoint -------------------------------------------------

    async def run_cycle(
        self,
        *,
        cycle_id: str | None = None,
        now: datetime | None = None,
    ) -> TeamBeatCycleSummary:
        """Run one full team-beat cycle across all configured teams."""
        cycle_ts = (now or datetime.now(UTC)).replace(microsecond=0)
        cycle_slot: CycleSlot = _derive_cycle_slot(cycle_ts)
        cycle_id = cycle_id or f"team-beat-{cycle_ts.strftime('%Y%m%dT%H%M%SZ')}"

        logger.info(
            "Team beat cycle %s starting | slot=%s teams=%s lookback=%dh",
            cycle_id, cycle_slot, list(self._team_codes), self._lookback_hours,
        )

        # 1. One feed read for the whole cycle.
        all_articles = await self._feed_reader.fetch_raw_articles(
            lookback_hours=self._lookback_hours
        )
        logger.info(
            "Fetched %d raw articles in %dh window",
            len(all_articles), self._lookback_hours,
        )

        works = [
            _TeamWork(
                team_code=team,
                persona=get_team_beat_persona(team),
                articles=_filter_articles_for_team(all_articles, team),
            )
            for team in self._team_codes
        ]
        for work in works:
            logger.info(
                "Team %s: %d articles in window", work.team_code, len(work.articles)
            )

        # 2. Per-team agent pipeline in parallel.
        await asyncio.gather(*(
            self._produce_brief_and_script(work, cycle_id) for work in works
        ))

        # 3. Single TTS batch for all teams that produced a script.
        # _run_tts_batch mutates each work in place (tts_batch_id +
        # audio_url) so persistence reads the updated state.
        works_with_scripts = [w for w in works if w.radio_script is not None]
        await self._run_tts_batch(works_with_scripts, cycle_ts, cycle_slot)

        # 4. Persist per team (parallel; each catches its own errors).
        await asyncio.gather(*(
            self._persist_team(work, cycle_ts, cycle_slot) for work in works
        ))

        summary = TeamBeatCycleSummary(
            cycle_ts=cycle_ts,
            cycle_slot=cycle_slot,
            teams=[
                BeatCycleResult(
                    team_code=w.team_code,
                    cycle_ts=cycle_ts,
                    cycle_slot=cycle_slot,
                    outcome=w.outcome,
                    reason=w.reason,
                    article_count=len(w.articles),
                    roundup_id=None,  # only the DB row carries the id
                )
                for w in works
            ],
        )
        logger.info(
            "Team beat cycle %s done | filed=%d no_news=%d error=%d",
            cycle_id, summary.filed_count, summary.no_news_count, summary.error_count,
        )
        return summary

    async def close(self) -> None:
        """Close all owned async resources. Safe to call multiple times."""
        closeables: list = [
            self._feed_reader,
            self._roundup_writer,
            self._cycle_state_store,
            self._tts_client,
        ]
        if self._article_lookup is not None:
            closeables.append(self._article_lookup)
        for closeable in closeables:
            try:
                await closeable.close()
            except Exception as exc:
                logger.debug("Close failed for %s: %s", type(closeable).__name__, exc)


def build_default_team_beat_workflow(
    settings: Settings,
    *,
    team_codes: tuple[str, ...] | None = None,
    lookback_hours: int = 12,
) -> TeamBeatWorkflow:
    """Wire up a workflow with the live adapters from settings.

    Mirrors `build_default_orchestrator` for the editorial cycle. The
    runtime adapters are constructed here so the CLI doesn't need to
    know about them.
    """
    # Local imports avoid a startup-time circular: adapters.py imports
    # team_beat.schemas; this module imports adapters only here.
    from app.adapters import (
        ArticleLookupFromDb,
        BeatCycleStateStore,
        BeatRoundupWriter,
        RawArticleDbReader,
    )
    from app.clients.base import SupabaseJobsConfig

    if not settings.tts_batch_submit_url or not settings.tts_batch_poll_url:
        raise ValueError(
            "TTS_BATCH_SUBMIT_URL and TTS_BATCH_POLL_URL must be configured "
            "in settings to run the team-beat cycle."
        )
    auth_token = (
        settings.tts_batch_function_auth_token.get_secret_value()
        if settings.tts_batch_function_auth_token
        else None
    )

    base_url = str(settings.supabase_url)
    service_key = settings.supabase_service_role_key.get_secret_value()

    feed_reader = RawArticleDbReader(
        base_url=base_url,
        service_role_key=service_key,
    )
    roundup_writer = BeatRoundupWriter(
        base_url=base_url,
        service_role_key=service_key,
    )
    cycle_state_store = BeatCycleStateStore(
        base_url=base_url,
        service_role_key=service_key,
    )
    article_lookup = ArticleLookupFromDb(
        base_url=base_url,
        service_role_key=service_key,
    )
    tts_client = TTSBatchClient(
        submit_url=str(settings.tts_batch_submit_url),
        poll_url=str(settings.tts_batch_poll_url),
        supabase=SupabaseJobsConfig(url=base_url),
        auth_token=auth_token,
        model_name=settings.tts_model_name,
        voice_name=settings.tts_voice_name,
        storage_bucket=settings.tts_storage_bucket,
        storage_path_prefix=settings.tts_storage_path_prefix,
        job_poll_interval_seconds=settings.extraction_poll_interval_seconds,
        job_timeout_seconds=settings.extraction_timeout_seconds,
        create_timeout_seconds=settings.tts_create_timeout_seconds,
        status_action_timeout_seconds=settings.tts_status_action_timeout_seconds,
        process_timeout_seconds=settings.tts_process_timeout_seconds,
        status_poll_interval_seconds=settings.tts_status_poll_interval_seconds,
        status_timeout_seconds=settings.tts_status_poll_timeout_seconds,
    )

    return TeamBeatWorkflow(
        settings=settings,
        feed_reader=feed_reader,
        roundup_writer=roundup_writer,
        cycle_state_store=cycle_state_store,
        tts_client=tts_client,
        article_lookup=article_lookup,
        team_codes=team_codes,
        lookback_hours=lookback_hours,
    )
