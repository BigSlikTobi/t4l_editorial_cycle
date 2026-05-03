"""Three-stage orchestrator over the gemini_tts_batch_service Cloud Run endpoints.

Lifecycle (per cycle, see docs/team_beat_mvp.md §7):

    1. action=create   → submit + poll → returns Gemini batch_id + initial state
    2. action=status   → submit + poll → returns current Gemini batch state.
                          Loop until state == JOB_STATE_SUCCEEDED.
                          (Each `status` call is itself a submit→poll cycle
                          against our async-job protocol; we sleep
                          `tts_status_poll_interval_seconds` between iterations.)
    3. action=process  → submit + poll → batch service downloads MP3s and
                          uploads them to the caller-chosen Storage bucket;
                          returns a manifest of {id → public_url}.

Each numbered step is one `AsyncJobClient.run(payload)` call against the
shared submit/poll endpoints — `AsyncJobClient` already handles auth,
retries, and our internal job timeout. The status loop is the only
multi-step piece; everything else is straight-line.

The TTS service itself ignores `key` in the supabase block (it reads its
own service-role from env), but `AsyncJobClient`'s `SupabaseJobsConfig`
is positional-friendly and we pass the editorial repo's Supabase URL so
the batch service can write to the shared `extraction_jobs` table.

Failure semantics:
- create / process job-level failure → JobFailedError propagates out of run().
- Gemini batch reaching JOB_STATE_FAILED / CANCELLED / EXPIRED → TTSBatchError.
- Status loop exceeds `status_timeout_seconds` → TTSBatchError.
- A subset of items in `process` failing is *not* fatal: we return a
  TTSBatchOutcome with `public_url=None` for the failed item ids and let
  the workflow decide.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from app.clients.base import (
    AsyncJobClient,
    JobFailedError,
    JobTimeoutError,
    SupabaseJobsConfig,
)

from .schemas import TTSBatchOutcome, TTSItem, TTSResult

logger = logging.getLogger(__name__)


# Gemini batch states the service returns under `result.status`. The complete
# enum is documented in the Google GenAI SDK; we treat anything in
# {SUCCEEDED, FAILED, CANCELLED, EXPIRED} as terminal.
GEMINI_TERMINAL_STATES = frozenset(
    {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
)
GEMINI_SUCCESS_STATE = "JOB_STATE_SUCCEEDED"


class TTSBatchError(RuntimeError):
    """Raised when the upstream Gemini batch reaches a terminal non-success
    state, or when our local status-poll loop exceeds its deadline."""

    def __init__(self, message: str, *, batch_id: str | None = None, state: str | None = None) -> None:
        super().__init__(message)
        self.batch_id = batch_id
        self.state = state


class TTSBatchClient:
    """Drives the gemini_tts_batch_service in a produce/harvest split.

    Two responsibilities:
      * `create_and_wait()` — submit a batch and capture its Gemini
        batch_id quickly. The PRODUCE cycle calls this and immediately
        persists; it does NOT wait for the batch to finish. If the
        async-job worker stalls (the documented failure mode where the
        worker successfully creates the Gemini batch but crashes before
        writing terminal state), batch_id is recovered by listing
        Gemini batches via the Gemini API directly.
      * `process_batch()` — for an already-SUCCEEDED batch, call the
        worker's process action to download MP3s and upload to Storage.
        The HARVEST cycle (scripts/tts_recover.py on its own cron)
        calls this only after confirming the batch is SUCCEEDED.

    The Gemini API key is required for the produce path's recovery and
    for `check_batch_state()` (used by the harvest script). It's
    optional only because some unit tests construct the client without
    it; runtime callers always pass it.
    """

    def __init__(
        self,
        *,
        submit_url: str,
        poll_url: str,
        supabase: SupabaseJobsConfig,
        auth_token: str | None,
        model_name: str,
        voice_name: str,
        storage_bucket: str,
        storage_path_prefix: str = "gemini-tts-batch",
        gemini_api_key: str | None = None,
        # Async-job protocol cadence (each submit→poll cycle):
        job_poll_interval_seconds: float = 2.0,
        # Default per-stage timeout used when no per-stage override is set.
        # Real production values are passed per-stage below.
        job_timeout_seconds: float = 300.0,
        # Per-stage submit→poll timeouts. The produce path uses
        # `create_short_timeout_seconds` (fast happy-path; on timeout
        # it falls back to Gemini-API recovery). Process timeout is the
        # ceiling for one MP3 download+upload run.
        create_short_timeout_seconds: float = 120.0,
        status_action_timeout_seconds: float | None = None,
        process_timeout_seconds: float | None = None,
        # Recovery window for Gemini-API listing — when the worker
        # stalls, we look for batches created within this many seconds
        # of our submit (with a small grace period for clock skew).
        gemini_recovery_window_seconds: float = 600.0,
    ) -> None:
        self._client = AsyncJobClient(
            submit_url=submit_url,
            poll_url=poll_url,
            supabase=supabase,
            auth_token=auth_token,
            poll_interval_seconds=job_poll_interval_seconds,
            timeout_seconds=job_timeout_seconds,
        )
        self._model_name = model_name
        self._voice_name = voice_name
        self._storage_bucket = storage_bucket
        self._storage_path_prefix = storage_path_prefix
        self._gemini_api_key = gemini_api_key
        # Per-stage overrides — None means "use the underlying client's
        # default timeout for this stage too".
        self._create_short_timeout = create_short_timeout_seconds
        self._status_action_timeout = status_action_timeout_seconds
        self._process_timeout = process_timeout_seconds
        self._gemini_recovery_window = gemini_recovery_window_seconds

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "TTSBatchClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- public API --------------------------------------------------------
    #
    # Produce/harvest split:
    #
    #   PRODUCE (twice-daily team-beat cycle):
    #     create_and_wait(items) → batch_id
    #         Submits a Gemini batch, captures its batch_id quickly
    #         (short async-job poll, falls back to Gemini-API listing
    #         if the worker stalls). Does NOT wait for the batch to
    #         finish. Persist the batch_id and exit.
    #
    #   HARVEST (every 30 min via team-beat-harvest.yml):
    #     check_batch_state(batch_id) → "JOB_STATE_SUCCEEDED" | …
    #     process_batch(batch_id, item_ids, ...) → TTSBatchOutcome
    #         Skip rows whose batch isn't SUCCEEDED yet. For SUCCEEDED
    #         rows, run the process action against the worker (which
    #         downloads MP3s and uploads them to Storage), then PATCH
    #         team_roundup.audio_url.
    #
    # The recovery script (scripts/tts_recover.py) is the harvest CLI.

    async def create_and_wait(
        self,
        items: Iterable[TTSItem],
        *,
        on_worker_stall: "Callable[[str, str], Awaitable[None]] | None" = None,
    ) -> str:
        """Submit a Gemini batch and return its batch_id.

        Happy path: short async-job poll (~120s default) → worker
        returns the batch_id → return.

        Stall path: async-job poll times out → list Gemini batches via
        the Gemini API directly, find ours by `create_time` window →
        return. The optional `on_worker_stall(job_id, reason)` callback
        is invoked with our async-job's job_id so the caller can PATCH
        the row to status='failed' (preventing the sibling cleanup from
        re-POSTing it and creating a duplicate Gemini batch).

        Raises TTSBatchError ONLY when both paths fail (the worker
        stalled AND the Gemini API returned no recent matching batch).
        """
        item_list = list(items)
        if not item_list:
            raise ValueError("create_and_wait() requires at least one TTSItem")

        submit_time = datetime.now(UTC)
        # Submit eagerly so we have the async-job id even if poll fails.
        # Reading internals of AsyncJobClient is ugly; the cleanest path
        # is to call submit() + poll_once() ourselves.
        payload = self._build_create_payload(item_list)
        try:
            job_id = await self._client.submit(payload)
        except JobFailedError as exc:
            raise TTSBatchError(
                f"TTS create submit rejected: {exc}",
            ) from exc
        logger.info(
            "TTS create submitted: job_id=%s items=%d (waiting up to %ss for worker)",
            job_id, len(item_list), self._create_short_timeout,
        )

        try:
            result = await self._poll_until_terminal(
                job_id, timeout_seconds=self._create_short_timeout
            )
            batch_id = result.get("batch_id")
            if batch_id:
                logger.info("TTS create returned batch_id=%s (happy path)", batch_id)
                return str(batch_id)
            # Defensive: terminal success but no batch_id is a worker bug
            # we should still try to recover from.
            logger.warning(
                "TTS create terminal-succeeded but result has no batch_id: %s",
                result,
            )
        except JobTimeoutError:
            logger.warning(
                "TTS create poll timed out after %ss; falling back to "
                "Gemini-API listing to recover batch_id",
                self._create_short_timeout,
            )
            if on_worker_stall is not None:
                try:
                    await on_worker_stall(
                        job_id,
                        f"create poll timeout after {self._create_short_timeout}s",
                    )
                except Exception:
                    logger.exception(
                        "on_worker_stall callback failed for job_id=%s; "
                        "proceeding to Gemini-API recovery anyway", job_id,
                    )
        except JobFailedError as exc:
            # The worker reported terminal failure on our async-job
            # protocol. The Gemini batch may or may not have been
            # created depending on where the worker died — try recovery
            # before giving up.
            logger.warning(
                "TTS create reported failure (%s); trying Gemini-API recovery", exc,
            )

        # Recovery path: list recent Gemini batches and pick ours.
        recovered = await self.discover_recent_batch_id(submit_time=submit_time)
        if recovered is None:
            raise TTSBatchError(
                "TTS create failed AND no Gemini batch was created in the "
                f"last {self._gemini_recovery_window:g}s. The submit may "
                "have errored before reaching Gemini; nothing to harvest."
            )
        logger.info(
            "TTS create recovered batch_id=%s via Gemini-API listing", recovered,
        )
        return recovered

    async def discover_recent_batch_id(
        self, submit_time: datetime, *, window_seconds: float | None = None
    ) -> str | None:
        """List Gemini batches and return the most-recent one created
        on or after `submit_time` (with a 30s grace for clock skew).

        Returns None when no candidate batch exists in the window — the
        caller should treat this as "nothing was actually submitted to
        Gemini" rather than "still pending".

        Single-tenant assumption: when multiple batches fall in the
        window we take the most recent. At cycle frequency (twice
        daily, single producer) this is correct; if you ever run two
        team-beat cycles overlapping in the same Gemini project, label
        batches with a unique `display_name` and match on it instead.
        """
        if not self._gemini_api_key:
            logger.error(
                "Gemini API key not configured on TTSBatchClient; cannot "
                "recover batch_id when worker stalls."
            )
            return None
        client = _gemini_client(self._gemini_api_key)
        window = window_seconds or self._gemini_recovery_window
        cutoff = submit_time - timedelta(seconds=30)
        candidates: list[tuple[datetime, str]] = []
        # SDK lists newest-first by create_time; short-circuit when older.
        for batch in client.batches.list(config={"page_size": 20}):
            ct = getattr(batch, "create_time", None)
            name = getattr(batch, "name", None)
            if ct is None or name is None:
                continue
            if ct >= cutoff:
                candidates.append((ct, str(name)))
            else:
                break
            if len(candidates) >= 20:
                break
        if not candidates:
            logger.warning(
                "Gemini-API recovery: no batches created since %s "
                "(submit_time=%s, window=%ss)",
                cutoff.isoformat(), submit_time.isoformat(), window,
            )
            return None
        candidates.sort(reverse=True)
        chosen = candidates[0][1]
        if len(candidates) > 1:
            logger.warning(
                "Gemini-API recovery: %d candidates in window; using newest %s. "
                "Other candidates: %s",
                len(candidates), chosen, [c[1] for c in candidates[1:]],
            )
        return chosen

    async def check_batch_state(self, batch_id: str) -> str:
        """Read the current Gemini batch state via the Gemini API directly.

        Used by the harvest script to decide whether to skip a roundup
        row (still pending) or process it (SUCCEEDED). Routes around
        the async-job worker's status action — Gemini's own SDK is
        more reliable for this read.

        Returns the state string (e.g. "JOB_STATE_SUCCEEDED",
        "JOB_STATE_PENDING"). Raises TTSBatchError on transport errors
        the caller should surface.
        """
        if not self._gemini_api_key:
            raise TTSBatchError(
                "Gemini API key not configured; cannot check batch state."
            )
        client = _gemini_client(self._gemini_api_key)
        try:
            batch = client.batches.get(name=batch_id)
        except Exception as exc:
            raise TTSBatchError(
                f"Gemini batch state read failed for {batch_id}: {exc}",
                batch_id=batch_id,
            ) from exc
        state = getattr(batch.state, "name", batch.state)
        return str(state) if state is not None else "UNKNOWN"

    async def process_batch(
        self,
        batch_id: str,
        item_ids: list[str],
        *,
        path_prefix_suffix: str | None = None,
    ) -> TTSBatchOutcome:
        """Run the `process` action for an already-succeeded batch.

        Used by both the live workflow (after create_and_wait) and the
        recovery script (when a previous run's process step never
        landed). The batch's output file is retrievable for several days
        after JOB_STATE_SUCCEEDED, so this is safe to call long after
        the original create.

        `item_ids` is what was submitted in `create` — used for mapping
        manifest entries back to the caller's domain and surfacing
        "missing from manifest" entries explicitly.
        """
        prefix = self._storage_path_prefix
        if path_prefix_suffix:
            prefix = f"{prefix}/{path_prefix_suffix}"
        manifest = await self._process(batch_id, prefix)
        stub_items = [TTSItem(id=i, text="", title="") for i in item_ids]
        return _outcome_from_manifest(batch_id, stub_items, manifest)

    # Back-compat alias for `scripts/tts_recover.py` and any external
    # callers that hit the older name. The semantics are identical.
    process_existing_batch = process_batch

    # --- stages ------------------------------------------------------------

    def _build_create_payload(self, items: list[TTSItem]) -> dict[str, Any]:
        """Render the action=create body the worker expects."""
        return {
            "action": "create",
            "model_name": self._model_name,
            "voice_name": self._voice_name,
            "items": [
                {"id": item.id, "text": item.text, "title": item.title}
                for item in items
            ],
        }

    async def _poll_until_terminal(
        self, job_id: str, *, timeout_seconds: float
    ) -> dict[str, Any]:
        """Poll our async-job protocol until terminal or `timeout_seconds`.

        Mirrors `AsyncJobClient.run`'s polling semantics but for an
        already-submitted job_id (since `create_and_wait` calls submit
        directly so it has the id even when polling fails)."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            data = await self._client.poll_once(job_id)
            status = data.get("status")
            if status == "succeeded":
                result = data.get("result")
                if result is None:
                    raise JobFailedError(f"Job {job_id} succeeded with no result")
                return result
            if status == "failed":
                raise JobFailedError(
                    f"Job {job_id} failed: {data.get('error')}",
                    error=data.get("error"),
                )
            if status == "expired":
                raise JobFailedError(f"Job {job_id} expired before completion")
            if status not in ("queued", "running"):
                raise JobFailedError(f"Job {job_id} returned unknown status {status!r}")
            if asyncio.get_event_loop().time() >= deadline:
                raise JobTimeoutError(
                    f"Job {job_id} did not finish within {timeout_seconds}s"
                )
            await asyncio.sleep(self._client._poll_interval)  # noqa: SLF001

    async def _process(self, batch_id: str, path_prefix: str) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "action": "process",
            "batch_id": batch_id,
            "storage": {
                "bucket": self._storage_bucket,
                "path_prefix": path_prefix,
            },
        }
        logger.info(
            "TTS process: batch_id=%s bucket=%s prefix=%s (timeout=%ss)",
            batch_id, self._storage_bucket, path_prefix, self._process_timeout,
        )
        result = await self._client.run(payload, timeout_seconds=self._process_timeout)
        results = result.get("results") or []
        failures = result.get("failures") or []
        logger.info(
            "TTS process returned: succeeded=%d failed=%d",
            len(results), len(failures),
        )
        # Stitch the two lists into one keyed by id so callers don't have to
        # know about the success/failure split.
        merged: list[dict[str, Any]] = []
        for entry in results:
            merged.append({
                "id": entry.get("id"),
                "public_url": entry.get("public_url"),
                "error": None,
            })
        for entry in failures:
            err = entry.get("error")
            merged.append({
                "id": entry.get("id"),
                "public_url": None,
                "error": err if isinstance(err, str) else str(err) if err is not None else "unknown",
            })
        return merged


def _gemini_client(api_key: str):
    """Lazy-import google.genai and return a configured Client.

    Lazy so module load stays cheap (the SDK pulls in protobuf and a
    transitive grpc tree) and so unit tests can mock the import via
    `monkeypatch.setattr` without needing the real SDK installed.
    """
    from google import genai
    return genai.Client(api_key=api_key)


def _outcome_from_manifest(
    batch_id: str,
    requested: list[TTSItem],
    manifest: list[dict[str, Any]],
) -> TTSBatchOutcome:
    """Build a TTSBatchOutcome including a placeholder for any requested
    item the manifest entirely omitted (which would indicate a service
    bug, but we record it explicitly rather than silently drop)."""
    by_id = {entry.get("id"): entry for entry in manifest if entry.get("id")}
    out: list[TTSResult] = []
    for item in requested:
        entry = by_id.pop(item.id, None)
        if entry is None:
            out.append(TTSResult(item_id=item.id, public_url=None, error="missing_from_manifest"))
        else:
            out.append(TTSResult(
                item_id=item.id,
                public_url=entry.get("public_url"),
                error=entry.get("error"),
            ))
    # Anything left in `by_id` is a manifest entry the service returned for an
    # id we never submitted — surface it for telemetry but don't fail.
    for unexpected_id, entry in by_id.items():
        logger.warning("TTS manifest contains unexpected id %r: %s", unexpected_id, entry)
    return TTSBatchOutcome(batch_id=batch_id, items=out)
