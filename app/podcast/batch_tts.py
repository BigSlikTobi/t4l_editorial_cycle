"""Direct Gemini Batch TTS client for artifact-based podcast rendering."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.gemini_tts import _build_speech_config, _format_transcript, _write_wav
from app.podcast.schemas import MultiSpeakerTTSPayload

logger = logging.getLogger(__name__)

GEMINI_BATCH_TERMINAL_STATES = frozenset(
    {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
)
GEMINI_BATCH_SUCCESS_STATE = "JOB_STATE_SUCCEEDED"


class GeminiBatchTTSError(RuntimeError):
    """Base error for Gemini Batch TTS failures."""


class GeminiBatchTTSTimeout(GeminiBatchTTSError):
    """Raised when the batch did not finish inside the configured window."""


@dataclass(frozen=True)
class BatchRenderOutcome:
    batch_id: str
    output_paths: list[Path]
    durations_seconds: list[int]
    state: str


def _gemini_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


def _gemini_types() -> Any:
    from google.genai import types

    return types


def _state_name(batch: Any) -> str:
    state = getattr(batch, "state", None)
    return str(getattr(state, "name", state) or "UNKNOWN")


def _dest_file_name(batch: Any) -> str | None:
    dest = getattr(batch, "dest", None)
    if dest is None:
        return None
    return getattr(dest, "file_name", None) or getattr(dest, "fileName", None)


def build_batch_request(key: str, payload: MultiSpeakerTTSPayload) -> dict[str, Any]:
    """Build one JSONL request accepted by Gemini Batch generateContent."""

    types_mod = _gemini_types()
    speech_config = _build_speech_config(types_mod, payload.voice_map)
    speech_config_json = speech_config.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    return {
        "key": key,
        "request": {
            "contents": [
                {
                    "parts": [
                        {
                            "text": _format_transcript(
                                payload.lines,
                                style_prompt=payload.style_prompt,
                            )
                        }
                    ]
                }
            ],
            "generation_config": {
                "responseModalities": ["AUDIO"],
                "speechConfig": speech_config_json,
            },
        },
    }


def extract_audio_bytes_from_batch_response(row: dict[str, Any]) -> bytes:
    """Extract base64 inline audio bytes from one batch JSONL response row."""

    response = row.get("response")
    if not isinstance(response, dict):
        error = row.get("error")
        raise GeminiBatchTTSError(f"batch row has no response: {error or row}")
    candidates = response.get("candidates") or []
    if not candidates:
        raise GeminiBatchTTSError("batch response has no candidates")
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    chunks: list[bytes] = []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline, dict):
            continue
        data = inline.get("data")
        if isinstance(data, str):
            chunks.append(base64.b64decode(data))
        elif isinstance(data, bytes):
            chunks.append(data)
    if not chunks:
        raise GeminiBatchTTSError("batch response had no inline audio data")
    return b"".join(chunks)


class GeminiBatchTTSClient:
    """Submit, poll, and download Gemini Batch TTS audio chunks."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        poll_interval_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds

    async def render_payloads(
        self,
        *,
        payloads: list[MultiSpeakerTTSPayload],
        output_paths: list[Path],
        workdir: Path,
        display_name: str,
    ) -> BatchRenderOutcome:
        if not payloads:
            raise ValueError("render_payloads requires at least one payload")
        if len(payloads) != len(output_paths):
            raise ValueError("payloads and output_paths length mismatch")

        workdir.mkdir(parents=True, exist_ok=True)
        jsonl_path = workdir / "gemini_tts_batch_requests.jsonl"
        requests = [
            build_batch_request(f"chunk-{idx:03d}", payload)
            for idx, payload in enumerate(payloads)
        ]
        jsonl_path.write_text(
            "".join(json.dumps(req, ensure_ascii=False) + "\n" for req in requests),
            encoding="utf-8",
        )

        client = _gemini_client(self._api_key)
        types_mod = _gemini_types()

        def _create_batch() -> Any:
            uploaded_file = client.files.upload(
                file=str(jsonl_path),
                config=types_mod.UploadFileConfig(
                    display_name=display_name,
                    mime_type="jsonl",
                ),
            )
            return client.batches.create(
                model=self._model,
                src=uploaded_file.name,
                config={"display_name": display_name},
            )

        try:
            batch = await asyncio.to_thread(_create_batch)
        except Exception as exc:
            raise GeminiBatchTTSError("Gemini batch create failed") from exc
        batch_id = str(getattr(batch, "name", ""))
        if not batch_id:
            raise GeminiBatchTTSError(f"Gemini batch create returned no name: {batch}")
        logger.info("Created Gemini podcast TTS batch %s", batch_id)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while _state_name(batch) not in GEMINI_BATCH_TERMINAL_STATES:
            if loop.time() >= deadline:
                raise GeminiBatchTTSTimeout(
                    f"Gemini batch {batch_id} did not finish within {self._timeout:.0f}s"
                )
            await asyncio.sleep(self._poll_interval)
            try:
                batch = await asyncio.to_thread(client.batches.get, name=batch_id)
            except Exception as exc:
                logger.warning(
                    "Gemini podcast TTS batch %s status check failed; will retry "
                    "until timeout: %s",
                    batch_id,
                    exc,
                )
                continue
            logger.info("Gemini podcast TTS batch %s state=%s", batch_id, _state_name(batch))

        state = _state_name(batch)
        if state != GEMINI_BATCH_SUCCESS_STATE:
            raise GeminiBatchTTSError(f"Gemini batch {batch_id} finished with {state}")

        result_file = _dest_file_name(batch)
        if not result_file:
            raise GeminiBatchTTSError(f"Gemini batch {batch_id} succeeded without result file")

        try:
            result_bytes = await asyncio.to_thread(client.files.download, file=result_file)
        except Exception as exc:
            raise GeminiBatchTTSError(
                f"Gemini batch {batch_id} result download failed"
            ) from exc
        result_text = result_bytes.decode("utf-8") if isinstance(result_bytes, bytes) else str(result_bytes)
        result_path = workdir / "gemini_tts_batch_results.jsonl"
        result_path.write_text(result_text, encoding="utf-8")

        rows = [json.loads(line) for line in result_text.splitlines() if line.strip()]
        if len(rows) != len(output_paths):
            raise GeminiBatchTTSError(
                f"Gemini batch returned {len(rows)} rows for {len(output_paths)} chunks"
            )

        durations: list[int] = []
        for row, output_path in zip(rows, output_paths, strict=True):
            pcm = extract_audio_bytes_from_batch_response(row)
            durations.append(_write_wav(output_path, pcm))

        return BatchRenderOutcome(
            batch_id=batch_id,
            output_paths=output_paths,
            durations_seconds=durations,
            state=state,
        )
