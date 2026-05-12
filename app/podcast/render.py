"""Bridge between a finished `PodcastScript` and the Gemini TTS client.

Pure functions plus the multi-segment renderer:

  * `script_to_payload`: flatten the whole script into one Gemini
    payload (used when no background music is configured).
  * `script_to_segment_payloads`: split the script into a cold-open
    payload and a body+outro payload (used when music is configured —
    each segment is rendered separately so we can stitch music in
    between).
  * `render_to_audio`: drive the Gemini client; on multi-segment runs
    delegate the music stitching to `audio_compose.compose_episode`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path

from app.clients.gemini_tts import GeminiTTSClient
from app.config import Settings
from app.podcast.audio_compose import MusicConfig, compose_episode
from app.podcast.personas import ANALYST_PERSONA, COLOR_PERSONA
from app.podcast.schemas import (
    MultiSpeakerTTSPayload,
    PodcastLanguage,
    PodcastScript,
    RenderResult,
    ScriptLine,
)

logger = logging.getLogger(__name__)


def _line_text_with_hints(text: str, hints: list[str]) -> str:
    """Prepend an inline parenthetical of audio cues to the line text.

    Following Gemini's advanced-prompting guide: cues like `(laughs)`,
    `(sighs)`, `(deadpan)` immediately before a phrase are interpreted
    as delivery direction or audible reactions. Multiple hints are
    comma-separated inside a single set of parentheses.
    """
    if not hints:
        return text
    cleaned = [h.strip() for h in hints if h and h.strip()]
    if not cleaned:
        return text
    cue = "(" + ", ".join(cleaned) + ") "
    return cue + text


def _build_style_prompt(
    language: PodcastLanguage, *, register_hint: str | None = None
) -> str:
    """Compose the natural-language style prompt prepended to the transcript.

    Mirrors Gemini's advanced-prompting recipe: describe each speaker
    in vivid performance-direction language, list the kinds of reactions
    that should feel at home in the read, and signal that the inline
    parenthetical cues are real direction (not stage directions to be
    spoken). The user explicitly wants more emotion than less, so this
    is generous about laughter, sighs, pauses, and energy shifts.
    """
    if language == "en-US":
        color_brief = COLOR_PERSONA.delivery_brief_en
        analyst_brief = ANALYST_PERSONA.delivery_brief_en
        header = (
            "TTS the following morning NFL podcast conversation between "
            "two co-hosts: Marcus (Marcus Hale, the host who frames "
            "the day) and Robin (Robin Donnelly, the technical "
            "breakdown analyst). Both are former athletes turned daily "
            "podcasters in their late 30s. Both speak unfiltered "
            "straight-talk. They are co-hosts who genuinely like each "
            "other and have done this together for years.\n\n"
            "DELIVERY — CRITICAL: Both speakers use a standard American "
            "accent. Both are grounded in the chest with high energy. "
            "Energy comes from VOLUME, INFLECTION, and SELECTIVE "
            "EMPHASIS — never from talking fast. Punchy, staccato "
            "rhythm when excited; slower, more deliberate cadence for "
            "analytical points. Let words land. Give punchlines room "
            "to breathe. Naturalistic breathing — audible breath, "
            "quick sigh, sharp inhale before a big point.\n\n"
            "CHEMISTRY — CRITICAL: Marcus drives the narrative and "
            "asks; Robin answers with technical depth. Marcus reacts to "
            "Robin's breakdowns like a fan would — laughs, exhales, says "
            "'truly' or 'come on.' Robin respects Marcus's framing and "
            "builds on it. Neither is cold; neither dominates. Real "
            "conversation, not a monologue with backup.\n\n"
            "Treat parenthetical cues like (laughs), (sharp inhale), "
            "(excited), (sighs), (deliberate), (let it land), "
            "(staccato), (slower) as performance direction — DO NOT "
            "read the words inside parentheses; perform them. Lean "
            "INTO the emotion. Two engaged human voices in a real "
            "morning conversation, mics open, coffee hot, tape "
            "rolling."
        )
        speaker_briefs = (
            f"### Marcus (Marcus Hale)\n\n{color_brief}\n\n"
            f"### Robin (Robin Donnelly)\n\n{analyst_brief}"
        )
    else:  # de-DE
        color_brief = COLOR_PERSONA.delivery_brief_de
        analyst_brief = ANALYST_PERSONA.delivery_brief_de
        header = (
            "TTS das folgende Morgen-NFL-Podcast-Gespräch zwischen zwei "
            "Co-Hosts: Marcus (Marcus Hale, der Host, der den Tag "
            "rahmt) und Robin (Robin Donnelly, der/die technische "
            "Breakdown-Analyst:in). Beide sind ehemalige Athleten Ende "
            "30, heute täglich Podcaster. Beide sprechen ungefilterten "
            "Klartext. Sie sind Co-Hosts, die sich wirklich mögen und "
            "das seit Jahren zusammen machen.\n\n"
            "SPRACHE & AKZENTE — KRITISCH: BEIDE Sprecher sprechen "
            "DEUTSCH, mit feinen Akzent-Unterschieden. 'color' "
            "(Marcus, aus Berlin) liefert mit SEHR LEICHTER Berliner "
            "Sprachfärbung — nicht als Dialekt erkennbar, nur als "
            "winziger regionaler Touch (gelegentlich leicht clipped, "
            "minimal hartes 'g'). NIEMALS 'watt', 'icke', 'wa' oder "
            "Berliner Schnauze. 'analyst' (Robin, US-Amerikaner) "
            "liefert Deutsch mit LEICHTEM AMERIKANISCHEM AKZENT — "
            "leicht rolliges 'r', englische Vokal-Färbung, fließend "
            "aber hörbar 'nicht Muttersprachler'. DENGLISH IST "
            "ERLAUBT: englische Eigennamen (Marcus Hale, Robin "
            "Donnelly, Spielernamen, Teamnamen) und NFL-Fachbegriffe "
            "(EPA, DVOA, route, coverage, snap, gap, first-down, "
            "touchdown, blitz, sack, audible, RPO, play-action) "
            "werden ENGLISCH/AMERIKANISCH ausgesprochen, nicht "
            "eingedeutscht — bei BEIDEN Sprechern.\n\n"
            "LIEFERUNG — KRITISCH: Beide aus dem Brustkorb mit hoher "
            "Energie. Energie kommt aus LAUTSTÄRKE, BETONUNG und "
            "SELEKTIVER EMPHASE — nie aus Schnellsprechen. Punchiger, "
            "Staccato-Rhythmus bei Aufregung; langsamere, bewusste "
            "Kadenz für analytische Punkte. Worte landen lassen. "
            "Pointen Raum geben. Natürliche Atmung — hörbares "
            "Ausatmen, kurzer Seufzer, scharfes Einatmen vor einem "
            "großen Punkt.\n\n"
            "CHEMIE — KRITISCH: Marcus treibt die Erzählung und stellt "
            "Fragen; Robin antwortet mit technischer Tiefe. Marcus "
            "reagiert auf Robins Breakdowns wie ein Fan — lacht, atmet "
            "aus, sagt 'echt' oder 'komm schon'. Robin respektiert "
            "Marcus' Rahmen und baut darauf auf. Keiner ist kalt; "
            "keiner dominiert. Echtes Gespräch, kein Monolog mit "
            "Backup.\n\n"
            "Klammerhinweise wie (lacht), (scharfes Einatmen), "
            "(aufgeregt), (seufzt), (bewusst), (landen lassen), "
            "(staccato), (langsamer) sind Regieanweisungen — sprich "
            "die Wörter in Klammern NICHT aus; performe sie. Geh IN "
            "die Emotion. Zwei engagierte menschliche Stimmen in "
            "einem echten Morgengespräch, Mikros offen, Kaffee heiß, "
            "Tape läuft."
        )
        speaker_briefs = (
            f"### Marcus (Marcus Hale)\n\n{color_brief}\n\n"
            f"### Robin (Robin Donnelly)\n\n{analyst_brief}"
        )
    full = f"{header}\n\n{speaker_briefs}"
    if register_hint:
        full = f"## Voice Register\n\n{register_hint}\n\n{full}"
    return full


def _build_continuation_style_prompt(
    language: PodcastLanguage, *, register_hint: str | None = None
) -> str:
    """Short style prompt for body chunks AFTER the first one.

    Critical: chunks 2..N are mid-conversation. Re-sending the full
    persona briefs (with their 'Audio Profile / Scene / Director's
    Notes' framing) causes Gemini to RESTART the show each chunk —
    adding ~25–30s of fresh 'welcome back' intro audio per chunk.
    This minimal prompt locks in the speaker mapping + language and
    forbids any restart framing.
    """
    if language == "en-US":
        body = (
            "TTS the following continuation of an ongoing NFL morning "
            "podcast between two co-hosts: Marcus (Marcus Hale) and "
            "Robin (Robin Donnelly). The show is ALREADY UNDER WAY — "
            "do NOT introduce yourselves, do NOT say 'welcome back', "
            "do NOT add any framing or branding. Pick up mid-conversation "
            "and continue speaking exactly as written.\n\n"
            "PER-SPEAKER DELIVERY (carry over from earlier in the show — "
            "do NOT drift toward neutral mid-conversation):\n"
            "• Marcus: warm, energetic, lived-in broadcaster voice; "
            "punchy staccato when excited; slows for analysis; audible "
            "breaths and quick reactions; standard American accent; "
            "grounded in the chest.\n"
            "• Robin: grounded former-pro voice; punchy on explosive "
            "plays, deliberate cadence on technical breakdown; "
            "blue-collar grit; standard American accent; chest-deep "
            "resonance; never strident.\n\n"
            "Treat parenthetical cues like (laughs), (sighs), "
            "(deliberate), (pause) as performance direction — DO NOT "
            "read the words in parentheses; perform them."
        )
    else:
        body = (
            "TTS die folgende Fortsetzung eines laufenden NFL-Morgen-"
            "Podcasts zwischen zwei Co-Hosts: Marcus (Marcus Hale, "
            "aus Berlin) und Robin (Robin Donnelly, aus den USA). "
            "Die Show LÄUFT BEREITS — stell dich NICHT vor, sag NICHT "
            "'willkommen zurück', füg KEINE Einleitung oder Branding "
            "hinzu. Setz mitten im Gespräch fort und sprich exakt "
            "wie geschrieben.\n\n"
            "PRO-SPRECHER-LIEFERUNG (von vorher in der Show übernehmen — "
            "NICHT mitten in der Show zu neutralem Hochdeutsch driften):\n"
            "• Marcus (aus Berlin): SEHR LEICHTE Berliner Sprachfärbung "
            "bleibt durchgehend aktiv — nur als feiner regionaler Touch "
            "(gelegentlich clipped Tempo, minimal hartes 'g'). NIEMALS "
            "'watt', 'icke' oder Berliner Schnauze — die Färbung ist "
            "subtil, nicht als Dialekt erkennbar.\n"
            "• Robin (aus den USA): leichter, hörbarer AMERIKANISCHER "
            "AKZENT bleibt die ganze Show über aktiv. Leicht rolliges "
            "'r', englische Vokal-Färbung, fließend aber hörbar 'nicht "
            "Muttersprachler'. Niemals neutrales Hochdeutsch.\n\n"
            "DENGLISH BLEIBT AKTIV: englische Namen und NFL-Fachbegriffe "
            "(Eagles, Chiefs, EPA, route, coverage, snap, gap, "
            "first-down, touchdown, blitz, sack, audible, RPO, "
            "play-action) werden ENGLISCH/AMERIKANISCH ausgesprochen — "
            "bei beiden Sprechern, NICHT eingedeutscht.\n\n"
            "Klammerhinweise wie (lacht), (seufzt), (bewusst), (Pause) "
            "sind Regieanweisungen — sprich die Wörter in Klammern "
            "NICHT aus; performe sie."
        )
    if register_hint:
        body = f"{register_hint}\n\n{body}"
    return body


def _gemini_speaker_for(internal_id: str) -> str:
    """Map internal speaker IDs to the proper names Gemini expects.

    Gemini multi-speaker TTS works much more reliably when speaker
    tags in the transcript are PROPER NAMES (matching their docs
    examples like "Joe:" / "Jane:") rather than generic role labels
    like "color"/"analyst". With generic labels the model sometimes
    defaults both speakers to the same voice — exactly the bug the
    user observed when changing voice_analyst to 'zephyr' didn't
    flip Robin's voice. Using first names from the persona bylines
    ("Marcus" / "Robin") fixes it.
    """
    if internal_id == "color":
        return COLOR_PERSONA.byline.split()[0]
    if internal_id == "analyst":
        return ANALYST_PERSONA.byline.split()[0]
    # Coerce unknown (e.g. "narrator") to the color host.
    return COLOR_PERSONA.byline.split()[0]


def _voice_map(settings: Settings) -> dict[str, str]:
    """Gemini-facing voice map keyed by the proper-name speaker tags."""
    return {
        COLOR_PERSONA.byline.split()[0]: settings.podcast_gemini_voice_color,
        ANALYST_PERSONA.byline.split()[0]: settings.podcast_gemini_voice_analyst,
    }


def _flatten_lines(sections: list[list[ScriptLine]]) -> list[tuple[str, str]]:
    """Flatten script sections into (gemini_speaker, text) tuples.

    Translates the internal speaker IDs ("color"/"analyst") to the
    proper-name speaker tags Gemini matches against its voice map.
    """
    out: list[tuple[str, str]] = []
    for section in sections:
        for line in section:
            speaker_tag = _gemini_speaker_for(line.speaker)
            text = _line_text_with_hints(line.text, line.prosody_hints).strip()
            if not text:
                continue
            out.append((speaker_tag, text))
    return out


def script_to_payload(
    script: PodcastScript,
    *,
    settings: Settings,
    title: str,
) -> MultiSpeakerTTSPayload:
    """Flatten a script into one (speaker, text) list for Gemini.

    Cold open → body → outro. Lines marked `narrator` are coerced to
    `color` (we only register two voices). Empty lines are dropped.
    Each line's prosody hints are inlined as a leading parenthetical.
    A natural-language style prompt is composed from the persona
    delivery briefs and attached to the payload — the TTS client
    prepends it to the transcript.
    """
    return MultiSpeakerTTSPayload(
        language=script.language,
        lines=_flatten_lines([script.cold_open, script.body, script.outro]),
        voice_map=_voice_map(settings),
        title=title,
        style_prompt=_build_style_prompt(
            script.language, register_hint=settings.podcast_voice_register_hint
        ),
    )


def script_to_segment_payloads(
    script: PodcastScript,
    *,
    settings: Settings,
    title: str,
) -> tuple[MultiSpeakerTTSPayload | None, MultiSpeakerTTSPayload]:
    """Split the script into (cold_open_payload, body+outro_payload).

    Used for the multi-segment render path (when background music is
    configured) so the cold open can be rendered separately and have
    music mixed under it before the sting + body land. Returns `None`
    for the cold-open payload when the script has no cold-open lines.
    """
    voice_map = _voice_map(settings)
    style_prompt = _build_style_prompt(
        script.language, register_hint=settings.podcast_voice_register_hint
    )

    cold_open_lines = _flatten_lines([script.cold_open])
    body_lines = _flatten_lines([script.body, script.outro])

    cold_open_payload: MultiSpeakerTTSPayload | None = None
    if cold_open_lines:
        cold_open_payload = MultiSpeakerTTSPayload(
            language=script.language,
            lines=cold_open_lines,
            voice_map=voice_map,
            title=f"{title} — Cold Open",
            style_prompt=style_prompt,
        )

    body_payload = MultiSpeakerTTSPayload(
        language=script.language,
        lines=body_lines or [("color", "(warm) That's it for today.")],
        voice_map=voice_map,
        title=title,
        style_prompt=style_prompt,
    )
    return cold_open_payload, body_payload


def _audio_filename(language: PodcastLanguage, run_date: date) -> str:
    """Filename with a UTC wall-clock suffix so re-renders never overwrite.

    Format: `{run_date}_{language}_{HHMMSSZ}.wav`. The DB row always
    points to the most recent path via the upsert, so delivery still
    finds the right file; older files stay on disk for A/B comparison
    until a cleanup cron sweeps them.
    """
    suffix = datetime.now(UTC).strftime("%H%M%SZ")
    return f"{run_date.isoformat()}_{language}_{suffix}.wav"


def _segment_filename(
    language: PodcastLanguage, run_date: date, kind: str
) -> str:
    suffix = datetime.now(UTC).strftime("%H%M%SZ")
    return f"{run_date.isoformat()}_{language}_{kind}_{suffix}.wav"


def _build_music_config(settings: Settings) -> MusicConfig:
    return MusicConfig(
        intro_music_path=settings.podcast_intro_music_path,
        sting_music_path=settings.podcast_sting_music_path,
        intro_solo_seconds=settings.podcast_intro_solo_seconds,
        intro_tail_seconds=settings.podcast_intro_tail_seconds,
        bed_volume_db=settings.podcast_music_bed_volume_db,
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
        sting_max_seconds=settings.podcast_sting_max_seconds,
        sting_fade_out_seconds=settings.podcast_sting_fade_out_seconds,
        song_mode=settings.podcast_intro_song_mode,
        song_vocal_intro_seconds=settings.podcast_song_vocal_intro_seconds,
        song_transition_seconds=settings.podcast_song_transition_seconds,
        song_sting_seconds=settings.podcast_song_sting_seconds,
        song_fade_out_seconds=settings.podcast_song_fade_out_seconds,
        postprocess_enabled=settings.podcast_postprocess_enabled,
        postprocess_target_lufs=settings.podcast_postprocess_target_lufs,
        postprocess_true_peak_db=settings.podcast_postprocess_true_peak_db,
        postprocess_loudness_range_lu=settings.podcast_postprocess_loudness_range_lu,
        postprocess_highpass_hz=settings.podcast_postprocess_highpass_hz,
    )


def _chunk_lines(
    lines: list[tuple[str, str]], *, max_chars: int
) -> list[list[tuple[str, str]]]:
    """Split `(speaker, text)` lines into chunks each ≤ max_chars.

    Splits are made at speaker-turn boundaries — never inside a single
    spoken line. Each chunk gets at least one line even if it exceeds
    the soft cap on its own (so a single long line isn't dropped).
    """
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_chars = 0
    for speaker, text in lines:
        # Speaker prefix + ": " adds ~ len(speaker) + 2 chars when rendered
        # in the Gemini transcript format.
        line_chars = len(text) + len(speaker) + 2
        if current and current_chars + line_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append((speaker, text))
        current_chars += line_chars
    if current:
        chunks.append(current)
    return chunks


async def _concat_voice_chunks(
    paths: list[Path], *, output_path: Path, music: MusicConfig
) -> None:
    """Concatenate body-chunk WAVs into a single body.wav (no music)."""
    from app.podcast.audio_compose import ConcatInput, _ffmpeg_concat

    await _ffmpeg_concat(
        inputs=[ConcatInput(path=p) for p in paths],
        music=music,
        output_path=output_path,
    )


async def _silence_wav(
    output_path: Path, *, duration_seconds: float, music: MusicConfig
) -> None:
    """Generate `duration_seconds` of digital silence at 24 kHz mono PCM."""
    from app.podcast.audio_compose import _run

    argv = [
        music.ffmpeg_path, "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=mono:sample_rate=24000",
        "-t", f"{duration_seconds}",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    rc, _stdout, stderr = await _run(argv)
    if rc != 0:
        from app.podcast.audio_compose import AudioComposeError

        raise AudioComposeError(
            f"ffmpeg silence generation failed (rc={rc}): "
            f"{stderr.decode(errors='replace')[-300:]}"
        )


def _resolve_client(
    settings: Settings, client: GeminiTTSClient | None
) -> GeminiTTSClient:
    if client is not None:
        return client
    api_key = settings.gemini_api_key
    if api_key is None:
        raise RuntimeError(
            "gemini_api_key is not configured; set it in .env to render podcast audio"
        )
    return GeminiTTSClient(
        api_key=api_key.get_secret_value(),
        model=settings.podcast_gemini_tts_model,
        timeout_seconds=settings.podcast_gemini_timeout_seconds,
        max_retries=settings.podcast_gemini_max_retries,
        retry_base_seconds=settings.podcast_gemini_retry_base_seconds,
    )


async def _render_payload_to_disk(
    *,
    payload: MultiSpeakerTTSPayload,
    output_path: Path,
    settings: Settings,
    client: GeminiTTSClient,
) -> int:
    """Render one payload to a WAV on disk; return duration_seconds."""
    if settings.podcast_force_single_voice:
        outcome = await client.render_single_voice(
            transcript_lines=payload.lines,
            voice_name=settings.podcast_gemini_voice_color,
            output_path=output_path,
            style_prompt=payload.style_prompt,
        )
    else:
        outcome = await client.render_multi_speaker(
            transcript_lines=payload.lines,
            voice_map=payload.voice_map,
            output_path=output_path,
            style_prompt=payload.style_prompt,
        )
    return outcome.duration_seconds


async def render_to_audio(
    payload_or_script: MultiSpeakerTTSPayload | PodcastScript,
    *,
    run_date: date,
    settings: Settings,
    client: GeminiTTSClient | None = None,
    title: str | None = None,
) -> RenderResult:
    """Render to a WAV on disk and return a RenderResult.

    Two paths:

    * **No music configured** — pass either a `MultiSpeakerTTSPayload`
      (legacy callers) or a `PodcastScript` (new callers). Single
      Gemini call, single WAV, no ffmpeg involvement.

    * **Music configured** — pass a `PodcastScript`; the script is
      split into a cold-open payload + a body+outro payload, each
      rendered separately by Gemini, then stitched together with
      ffmpeg around the configured intro/sting music. Returns the
      final composed WAV's path + duration.
    """
    music = _build_music_config(settings)
    output_dir = Path(settings.podcast_audio_temp_dir)
    resolved_client = _resolve_client(settings, client)

    # Single-render path: no music, accept either input shape.
    if not music.has_any_music:
        if isinstance(payload_or_script, PodcastScript):
            payload = script_to_payload(
                payload_or_script,
                settings=settings,
                title=title or f"T4L Daily — {run_date.isoformat()}",
            )
        else:
            payload = payload_or_script
        output_path = output_dir / _audio_filename(payload.language, run_date)
        duration = await _render_payload_to_disk(
            payload=payload,
            output_path=output_path,
            settings=settings,
            client=resolved_client,
        )
        logger.info("Podcast audio ready (single-render): %s (%ds)", output_path, duration)
        return RenderResult(
            audio_path=str(output_path),
            duration_seconds=duration,
            mime_type="audio/wav",
        )

    # Multi-segment path: music is configured.
    if not isinstance(payload_or_script, PodcastScript):
        raise TypeError(
            "render_to_audio requires a PodcastScript when music is configured "
            "(intro and/or sting). Got a flat payload."
        )
    script = payload_or_script
    cold_open_payload, body_payload = script_to_segment_payloads(
        script,
        settings=settings,
        title=title or f"T4L Daily — {run_date.isoformat()}",
    )

    workdir = output_dir / "compose"
    workdir.mkdir(parents=True, exist_ok=True)

    cold_open_path: Path | None = None
    if cold_open_payload is not None:
        # IMPORTANT: render the cold open as ONE Gemini call (brand
        # line included). Splitting the brand line into its own short
        # call triggered a Gemini TTS pathology where a very short
        # transcript paired with a long style prompt caused the model
        # to ad-lib / loop intros, producing ~6 min of audio for one
        # line. The "standalone" feel of the brand line is now carried
        # by its prosody hints + period punctuation; an extra inline
        # ellipsis enforces a pause in the read.
        cold_open_path = output_dir / _segment_filename(
            script.language, run_date, "coldopen"
        )
        await _render_payload_to_disk(
            payload=cold_open_payload,
            output_path=cold_open_path,
            settings=settings,
            client=resolved_client,
        )

    # Body chunking: long single Gemini calls degrade in pacing
    # toward the end of the generation, so we split at speaker
    # boundaries and stitch the chunks together.
    #
    # CRITICAL: only the FIRST body chunk gets the full style prompt
    # (which contains the persona "Audio Profile / Scene / Director's
    # Notes" sections that signal the start of a show). Subsequent
    # chunks get a continuation-only prompt — without this, Gemini
    # re-reads the persona briefs as "start of show" cues and adds
    # ~25-30s of fresh intro audio at the head of every chunk.
    continuation_prompt = _build_continuation_style_prompt(
        script.language, register_hint=settings.podcast_voice_register_hint
    )
    body_chunks = _chunk_lines(
        body_payload.lines, max_chars=settings.podcast_tts_chunk_max_chars
    )
    body_chunk_paths: list[Path] = []
    for idx, chunk_lines in enumerate(body_chunks):
        chunk_path = workdir / f"body_chunk_{idx:02d}.wav"
        chunk_style = body_payload.style_prompt if idx == 0 else continuation_prompt
        chunk_payload = body_payload.model_copy(
            update={"lines": chunk_lines, "style_prompt": chunk_style}
        )
        await _render_payload_to_disk(
            payload=chunk_payload,
            output_path=chunk_path,
            settings=settings,
            client=resolved_client,
        )
        body_chunk_paths.append(chunk_path)
    logger.info("Rendered body in %d chunks", len(body_chunk_paths))

    body_path = output_dir / _segment_filename(script.language, run_date, "body")
    if len(body_chunk_paths) == 1:
        # Single chunk — still pass through ffmpeg so the format is
        # normalized for downstream compose.
        await _concat_voice_chunks(
            body_chunk_paths, output_path=body_path, music=music
        )
    else:
        await _concat_voice_chunks(
            body_chunk_paths, output_path=body_path, music=music
        )

    final_path = output_dir / _audio_filename(script.language, run_date)
    duration = await compose_episode(
        cold_open_voice_path=cold_open_path,
        body_voice_path=body_path,
        music=music,
        output_path=final_path,
        workdir=workdir,
    )
    logger.info(
        "Podcast audio ready (multi-segment + music): %s (%ds)",
        final_path, duration,
    )
    return RenderResult(
        audio_path=str(final_path),
        duration_seconds=duration,
        mime_type="audio/wav",
    )
