"""Direct Gemini multi-speaker TTS client.

Distinct from `app/team_beat/tts_client.py` (which calls a sibling Cloud
Run worker that wraps Gemini's batch TTS). This client talks to the
Gemini API directly via the `google-genai` SDK, so the podcast pipeline
runs end-to-end on the VPS without any sidecar service.

Gemini 2.5 TTS preview returns 16-bit signed PCM @ 24 kHz inline. We
wrap the bytes in a WAV header and write them to disk. The downstream
Save-to-Spotify CLI accepts WAV directly, so no transcoding is needed.

For a 25-min target episode the dialogue writer caps word count at
~4200 words, comfortably under the preview model's per-response limit.
If we ever bump the target into territory that requires chunking, do
it here behind the same `render_multi_speaker` interface — callers
shouldn't care.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Gemini 2.5 TTS preview: 16-bit signed PCM, mono, 24 kHz.
_PCM_SAMPLE_WIDTH_BYTES = 2
_PCM_SAMPLE_RATE_HZ = 24_000
_PCM_CHANNELS = 1


class GeminiTTSError(Exception):
    """Base for failures from the direct Gemini TTS client."""


class GeminiTTSTimeout(GeminiTTSError):
    """Raised when the SDK call exceeds the configured timeout."""


class GeminiTTSEmptyResponse(GeminiTTSError):
    """Raised when Gemini returns no audio bytes."""


def _is_retryable_gemini_exception(exc: BaseException) -> bool:
    """True for transient server errors / rate limits worth retrying.

    The `google-genai` SDK distinguishes ClientError (4xx, terminal) from
    ServerError (5xx, transient) and raises both via a common APIError
    base. We only retry the server-side / rate-limit variety.
    """
    try:
        from google.genai import errors as genai_errors
    except ImportError:
        return False
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        # 429 is the one client-side code worth retrying (rate limit).
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return status == 429
    return False


@dataclass
class GeminiRenderOutcome:
    """Internal — what the client returns to the render layer."""

    audio_path: Path
    duration_seconds: int
    sample_rate_hz: int = _PCM_SAMPLE_RATE_HZ
    channels: int = _PCM_CHANNELS


def _gemini_client(api_key: str) -> Any:
    """Lazy-import google.genai and return a configured Client.

    Mirrors `app/team_beat/tts_client._gemini_client`: keeps module load
    cheap (the SDK pulls in protobuf + grpc) and lets unit tests
    `monkeypatch.setattr` without needing the SDK at import time.
    """
    from google import genai

    return genai.Client(api_key=api_key)


def _gemini_types() -> Any:
    """Lazy-import the `google.genai.types` namespace."""
    from google.genai import types

    return types


def _normalize_voice_name(name: str) -> str:
    """Normalize a Gemini prebuilt voice name to its canonical form.

    Gemini's voice catalogue (Zephyr, Puck, Charon, Kore, Fenrir, …) is
    case-sensitive on the wire. A misspelled-case name (e.g. lowercase
    "zephyr") can silently fall back to a default voice — which causes
    the symptom of one speaker's voice flipping between renders. We
    title-case as a defensive normalize so env-var typos don't poison
    the multi-speaker mapping.
    """
    stripped = name.strip()
    if not stripped:
        return stripped
    # Title-case while preserving any letters the caller intentionally
    # left lowercase mid-word (e.g. "umbriel" → "Umbriel").
    return stripped[0].upper() + stripped[1:]


def _build_speech_config(types_mod: Any, voice_map: dict[str, str]) -> Any:
    """Build a multi-speaker SpeechConfig from a {speaker → voice} map.

    Speaker keys must match the names used in the transcript text
    (proper names like "Marcus"/"Robin"). Voice names are normalized
    to title case so lowercase env-var typos don't fall back to a
    default voice. Gemini's multi_speaker_voice_config accepts up to
    two speaker entries today; if the map has more we raise.
    """
    if len(voice_map) > 2:
        raise GeminiTTSError(
            f"Gemini multi-speaker mode supports at most 2 speakers; got {len(voice_map)}"
        )

    speaker_configs = [
        types_mod.SpeakerVoiceConfig(
            speaker=speaker,
            voice_config=types_mod.VoiceConfig(
                prebuilt_voice_config=types_mod.PrebuiltVoiceConfig(
                    voice_name=_normalize_voice_name(voice)
                ),
            ),
        )
        for speaker, voice in voice_map.items()
    ]

    return types_mod.SpeechConfig(
        multi_speaker_voice_config=types_mod.MultiSpeakerVoiceConfig(
            speaker_voice_configs=speaker_configs,
        ),
    )


def _build_single_speaker_speech_config(types_mod: Any, voice_name: str) -> Any:
    """Single-voice fallback for `force_single_voice` mode."""
    return types_mod.SpeechConfig(
        voice_config=types_mod.VoiceConfig(
            prebuilt_voice_config=types_mod.PrebuiltVoiceConfig(voice_name=voice_name),
        ),
    )


def _format_transcript(
    lines: list[tuple[str, str]],
    *,
    style_prompt: str | None = None,
) -> str:
    """Render `(speaker, text)` lines as Gemini's expected transcript.

    Per Gemini's speech-generation docs, an optional natural-language
    style instruction precedes the transcript and tells the model how
    each speaker should sound — emotional register, cadence, reactions.
    The transcript itself uses `Speaker: ...` prefixes that Gemini
    matches against the `speaker_voice_configs` entries.

    Inline parenthetical cues like `(laughs)`, `(sighs)`, `(deadpan)`
    are handled per-line by the upstream render layer; this function
    is format-agnostic about them.
    """
    rendered: list[str] = []
    if style_prompt:
        rendered.append(style_prompt.strip())
        rendered.append("")  # blank line between style and transcript
    for speaker, text in lines:
        text = text.strip()
        if not text:
            continue
        rendered.append(f"{speaker}: {text}")
    return "\n".join(rendered)


def _format_transcript_single_voice(
    lines: list[tuple[str, str]],
    *,
    style_prompt: str | None = None,
) -> str:
    """Single-voice fallback: collapse speaker turns into [TAG] markers."""
    rendered: list[str] = []
    if style_prompt:
        rendered.append(style_prompt.strip())
        rendered.append("")
    for speaker, text in lines:
        text = text.strip()
        if not text:
            continue
        rendered.append(f"[{speaker.upper()}] {text}")
    return "\n".join(rendered)


def _extract_pcm_bytes(response: Any) -> bytes:
    """Pull inline audio bytes out of a Gemini generate_content response.

    The SDK packs them under `candidates[0].content.parts[*].inline_data.data`.
    Multiple parts get concatenated. Defensively tolerate missing fields
    so we surface a clean error instead of an AttributeError.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise GeminiTTSEmptyResponse("Gemini response had no candidates")
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    chunks: list[bytes] = []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        if data:
            chunks.append(data if isinstance(data, bytes) else bytes(data))
    if not chunks:
        raise GeminiTTSEmptyResponse("Gemini response had no audio bytes")
    return b"".join(chunks)


def _write_wav(path: Path, pcm_bytes: bytes) -> int:
    """Write 16-bit mono PCM @ 24 kHz to disk as a WAV file.

    Returns duration in seconds (rounded to int — adequate for the
    audit log; the renderer doesn't need ms precision).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(_PCM_CHANNELS)
        wav.setsampwidth(_PCM_SAMPLE_WIDTH_BYTES)
        wav.setframerate(_PCM_SAMPLE_RATE_HZ)
        wav.writeframes(pcm_bytes)
    sample_count = len(pcm_bytes) // (_PCM_SAMPLE_WIDTH_BYTES * _PCM_CHANNELS)
    return int(sample_count / _PCM_SAMPLE_RATE_HZ)


class GeminiTTSClient:
    """Direct Gemini TTS — single multi-speaker call, write WAV.

    Construct once per process; reused across episodes if multiple are
    rendered in a single VPS run (EN + DE).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int = 4,
        retry_base_seconds: float = 4.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max(1, max_retries)
        self._retry_base = max(0.1, retry_base_seconds)

    async def render_multi_speaker(
        self,
        *,
        transcript_lines: list[tuple[str, str]],
        voice_map: dict[str, str],
        output_path: Path,
        style_prompt: str | None = None,
    ) -> GeminiRenderOutcome:
        """Render a multi-speaker transcript to a WAV file at `output_path`.

        `style_prompt` is an optional natural-language instruction
        prepended to the transcript per the Gemini advanced-prompting
        guide — describes how each speaker should sound, emotional
        register, pacing, allowed reactions.
        """
        if not transcript_lines:
            raise GeminiTTSError("transcript_lines is empty")
        if not voice_map:
            raise GeminiTTSError("voice_map is empty")

        types_mod = _gemini_types()
        speech_config = _build_speech_config(types_mod, voice_map)
        contents = _format_transcript(transcript_lines, style_prompt=style_prompt)

        return await self._render(
            contents=contents,
            speech_config=speech_config,
            output_path=output_path,
            types_mod=types_mod,
        )

    async def render_single_voice(
        self,
        *,
        transcript_lines: list[tuple[str, str]],
        voice_name: str,
        output_path: Path,
        style_prompt: str | None = None,
    ) -> GeminiRenderOutcome:
        """Single-voice fallback — script collapsed to [SPEAKER] tags."""
        if not transcript_lines:
            raise GeminiTTSError("transcript_lines is empty")

        types_mod = _gemini_types()
        speech_config = _build_single_speaker_speech_config(types_mod, voice_name)
        contents = _format_transcript_single_voice(
            transcript_lines, style_prompt=style_prompt
        )

        return await self._render(
            contents=contents,
            speech_config=speech_config,
            output_path=output_path,
            types_mod=types_mod,
        )

    async def _render(
        self,
        *,
        contents: str,
        speech_config: Any,
        output_path: Path,
        types_mod: Any,
    ) -> GeminiRenderOutcome:
        client = _gemini_client(self._api_key)
        config = types_mod.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=speech_config,
        )

        def _call() -> Any:
            return client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )

        # Retry loop for transient Gemini 5xx / 429s. Each attempt has
        # the full `self._timeout` budget; cumulative wall time can
        # therefore exceed the single-attempt timeout.
        attempt = 0
        last_exc: BaseException | None = None
        response = None
        while attempt < self._max_retries:
            attempt += 1
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(_call), timeout=self._timeout
                )
                break
            except asyncio.TimeoutError as exc:
                # Timeouts are treated as retryable up to max_retries —
                # in practice they're rare and re-issuing the same call
                # is the right move when Gemini is slow.
                last_exc = exc
                if attempt >= self._max_retries:
                    raise GeminiTTSTimeout(
                        f"Gemini TTS render exceeded {self._timeout:.0f}s "
                        f"(after {attempt} attempts)"
                    ) from exc
                backoff = min(60.0, self._retry_base * (2 ** (attempt - 1)))
                logger.warning(
                    "Gemini TTS timeout on attempt %d/%d; retrying in %.1fs",
                    attempt, self._max_retries, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            except Exception as exc:  # SDK / transport / API errors
                last_exc = exc
                retryable = _is_retryable_gemini_exception(exc)
                if not retryable or attempt >= self._max_retries:
                    raise GeminiTTSError(
                        f"Gemini TTS call failed (after {attempt} attempt(s)): {exc}"
                    ) from exc
                backoff = min(60.0, self._retry_base * (2 ** (attempt - 1)))
                logger.warning(
                    "Gemini TTS transient error on attempt %d/%d (%s); "
                    "retrying in %.1fs",
                    attempt, self._max_retries, exc, backoff,
                )
                await asyncio.sleep(backoff)
                continue

        if response is None:
            # Defensive: loop should have either broken with a response
            # or raised — this guards against a logic regression.
            raise GeminiTTSError(
                f"Gemini TTS retry loop exited without response: {last_exc}"
            )

        pcm_bytes = _extract_pcm_bytes(response)
        duration = _write_wav(output_path, pcm_bytes)
        logger.info(
            "Rendered Gemini TTS audio: %s (%d bytes, ~%ds)",
            output_path, len(pcm_bytes), duration,
        )
        return GeminiRenderOutcome(audio_path=output_path, duration_seconds=duration)
