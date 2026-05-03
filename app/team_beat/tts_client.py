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
from typing import Any, Iterable

from app.clients.base import AsyncJobClient, JobFailedError, SupabaseJobsConfig

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
    """Composes one AsyncJobClient and orchestrates create→status→process."""

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
        # Async-job protocol cadence (each submit→poll cycle):
        job_poll_interval_seconds: float = 2.0,
        # Default per-stage timeout used when no per-stage override is set.
        # Real production values are passed per-stage below.
        job_timeout_seconds: float = 300.0,
        # Per-stage submit→poll timeouts. Gemini batch creation can take
        # 10-20+ minutes round-trip in the worker; status checks are short;
        # process spans MP3 download + per-item upload to Supabase Storage.
        create_timeout_seconds: float | None = None,
        status_action_timeout_seconds: float | None = None,
        process_timeout_seconds: float | None = None,
        # Outer Gemini-state poll cadence (between successive `status` calls):
        status_poll_interval_seconds: float = 30.0,
        status_timeout_seconds: float = 1800.0,
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
        self._status_poll_interval = status_poll_interval_seconds
        self._status_timeout = status_timeout_seconds
        # Per-stage overrides — None means "use the underlying client's
        # default timeout for this stage too".
        self._create_timeout = create_timeout_seconds
        self._status_action_timeout = status_action_timeout_seconds
        self._process_timeout = process_timeout_seconds

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "TTSBatchClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --- public API --------------------------------------------------------
    #
    # Two ways to drive a cycle:
    #   * synthesize()              — one-shot create→status→process; raises on
    #                                 anything that prevents process from
    #                                 producing a manifest.
    #   * create_and_wait() +       — split form. The caller captures the
    #     process_batch()             batch_id between stages so it can be
    #                                 persisted to DB before process runs,
    #                                 making partial failures recoverable.
    # Both forms share the same stage methods underneath.

    async def synthesize(
        self,
        items: Iterable[TTSItem],
        *,
        path_prefix_suffix: str | None = None,
    ) -> TTSBatchOutcome:
        """Run the full create→status→process lifecycle.

        `path_prefix_suffix` is appended to `storage_path_prefix` so each
        cycle gets its own deterministic, upsert-overwriteable folder
        (e.g. `gemini-tts-batch/2026-05-02_AM`). When omitted we use the
        configured prefix verbatim.
        """
        item_list = list(items)
        if not item_list:
            raise ValueError("synthesize() requires at least one TTSItem")

        batch_id = await self.create_and_wait(item_list)
        return await self.process_batch(
            batch_id,
            [item.id for item in item_list],
            path_prefix_suffix=path_prefix_suffix,
        )

    async def create_and_wait(self, items: Iterable[TTSItem]) -> str:
        """Submit a `create` job, then poll Gemini state until SUCCEEDED.

        Returns the Gemini batch_id. Raises TTSBatchError if the upstream
        Gemini batch reaches any terminal non-success state, or if the
        outer status loop exceeds its deadline. The batch_id may still be
        attached to the raised exception (TTSBatchError.batch_id) when
        possible so the caller can persist it for recovery.
        """
        item_list = list(items)
        if not item_list:
            raise ValueError("create_and_wait() requires at least one TTSItem")

        batch_id = await self._create(item_list)
        terminal_state = await self._wait_for_terminal_state(batch_id)
        if terminal_state != GEMINI_SUCCESS_STATE:
            raise TTSBatchError(
                f"Gemini batch {batch_id} reached terminal non-success state {terminal_state}",
                batch_id=batch_id,
                state=terminal_state,
            )
        return batch_id

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

    async def _create(self, items: list[TTSItem]) -> str:
        payload: dict[str, Any] = {
            "action": "create",
            "model_name": self._model_name,
            "voice_name": self._voice_name,
            "items": [
                {"id": item.id, "text": item.text, "title": item.title}
                for item in items
            ],
        }
        logger.info(
            "TTS create: model=%s voice=%s items=%d (timeout=%ss)",
            self._model_name, self._voice_name, len(items),
            self._create_timeout,
        )
        result = await self._client.run(payload, timeout_seconds=self._create_timeout)
        batch_id = result.get("batch_id")
        if not batch_id:
            raise TTSBatchError(
                f"create response missing batch_id: {result}",
            )
        logger.info("TTS create returned batch_id=%s state=%s", batch_id, result.get("status"))
        return str(batch_id)

    async def _status_once(self, batch_id: str) -> dict[str, Any]:
        return await self._client.run(
            {"action": "status", "batch_id": batch_id},
            timeout_seconds=self._status_action_timeout,
        )

    async def _wait_for_terminal_state(self, batch_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + self._status_timeout
        last_state: str | None = None
        while True:
            result = await self._status_once(batch_id)
            state = result.get("status")
            if state != last_state:
                logger.info("TTS status batch=%s state=%s", batch_id, state)
                last_state = state
            if state in GEMINI_TERMINAL_STATES:
                return str(state)
            if asyncio.get_event_loop().time() >= deadline:
                raise TTSBatchError(
                    f"Gemini batch {batch_id} did not reach terminal state within "
                    f"{self._status_timeout}s (last state={state})",
                    batch_id=batch_id,
                    state=state,
                )
            await asyncio.sleep(self._status_poll_interval)

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
