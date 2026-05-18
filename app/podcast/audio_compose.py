"""ffmpeg-based audio composition for the podcast.

Stitches the rendered voice segments together with optional background
music (intro bed under cold-open voice) and a transition sting between
cold open and body.

Two stages, each one ffmpeg invocation:

1. **Intro section** — if intro music is configured, mix it under the
   cold-open voice with a solo head, ducked bed under the voice, and
   a tail fade-out. If no intro music, the cold-open voice IS the
   intro section.

2. **Final concat** — concatenate `intro_section + sting? + body`
   into the final WAV. Inputs that aren't provided are skipped.

Everything stays at 16-bit signed PCM mono @ 24 kHz to match Gemini's
output format. Music files in any sample rate / channel count get
auto-resampled by ffmpeg's `aresample` and downmixed by `pan`/`amix`
defaults.

ffmpeg is invoked via `asyncio.create_subprocess_exec` so a long
compose doesn't block the event loop. Errors are surfaced with the
last 500 chars of stderr for actionable diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Output format constants — match `app/clients/gemini_tts.py`.
_TARGET_SAMPLE_RATE = 24_000
_TARGET_CHANNELS = 1


class AudioComposeError(RuntimeError):
    """Raised when an ffmpeg compose step fails."""


@dataclass
class MusicConfig:
    """Caller-supplied music + ducking + post-processing parameters."""

    intro_music_path: Path | None
    sting_music_path: Path | None
    intro_solo_seconds: float
    intro_tail_seconds: float
    bed_volume_db: float
    ffmpeg_path: str
    ffprobe_path: str
    player_of_day_jingle_path: Path | None = None
    team_of_day_jingle_path: Path | None = None
    deep_dive_jingle_path: Path | None = None
    sting_max_seconds: float = 30.0
    sting_fade_out_seconds: float = 2.0
    section_jingle_max_seconds: float = 10.0
    section_jingle_fade_out_seconds: float = 1.0
    # Single-song mode: one music file carries the brand vocal head,
    # ducks (volume-only, no pitch shift) under the voice, resumes at
    # full volume for the sting, then fades out. When True,
    # `sting_music_path` is ignored.
    song_mode: bool = True
    song_vocal_intro_seconds: float = 7.0
    song_transition_seconds: float = 1.0
    song_sting_seconds: float = 25.0
    song_fade_out_seconds: float = 3.0
    # Post-processing knobs — EBU R128 loudnorm + safety limiter + HPF.
    postprocess_enabled: bool = True
    postprocess_target_lufs: float = -16.0
    postprocess_true_peak_db: float = -1.0
    postprocess_loudness_range_lu: float = 11.0
    postprocess_highpass_hz: int = 80

    @property
    def has_any_music(self) -> bool:
        return any(
            path is not None
            for path in (
                self.intro_music_path,
                self.sting_music_path,
                self.player_of_day_jingle_path,
                self.team_of_day_jingle_path,
                self.deep_dive_jingle_path,
            )
        )


@dataclass
class ConcatInput:
    """One audio input for `_ffmpeg_concat`, with optional shaping.

    The shaping ops apply in this order: aresample → pan-to-mono →
    atrim (head crop) → afade out → volume gain. Only the trim/fade
    are configurable here; resample + downmix run unconditionally so
    heterogeneous formats (44.1k stereo MP3, 48k stereo WAV, …) all
    end up uniform 24 kHz mono PCM before concat.
    """

    path: Path
    max_duration_seconds: float | None = None
    fade_out_seconds: float | None = None


async def _run(argv: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout, stderr


async def probe_duration_seconds(path: Path, *, ffprobe_path: str) -> float:
    """Return audio duration in seconds. Raises on probe failure."""
    rc, stdout, stderr = await _run(
        [
            ffprobe_path,
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            str(path),
        ]
    )
    if rc != 0:
        raise AudioComposeError(
            f"ffprobe failed for {path}: {stderr.decode(errors='replace')[-300:]}"
        )
    try:
        payload = json.loads(stdout.decode("utf-8"))
        return float(payload["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AudioComposeError(
            f"ffprobe returned unexpected payload for {path}: {exc}"
        ) from exc


def _build_intro_filter(
    *,
    cold_open_duration_s: float,
    solo_s: float,
    tail_s: float,
    bed_db: float,
) -> str:
    """Build the filter_complex string for intro music + cold-open mix.

    Inputs assumed:
      [0:a] = intro music
      [1:a] = cold-open voice

    Output label: [intro_section]

    The intro music is sliced into three pieces: pre-bed (full volume,
    short fade-in), bed (ducked under voice), tail (ducked + fade out).
    Voice is delayed by `solo_s` so it lands when the bed begins.
    """
    bed_start = solo_s
    bed_end = solo_s + cold_open_duration_s
    tail_end = bed_end + tail_s
    voice_delay_ms = int(solo_s * 1000)

    # Music: pre-bed (full + 0.3s fade in) | bed (ducked) | tail (ducked + fade out).
    return (
        f"[0:a]aresample={_TARGET_SAMPLE_RATE},pan=mono|c0=0.5*c0+0.5*c1,"
        f"atrim=0:{bed_start},afade=t=in:st=0:d=0.3[m_pre];"
        f"[0:a]aresample={_TARGET_SAMPLE_RATE},pan=mono|c0=0.5*c0+0.5*c1,"
        f"atrim={bed_start}:{bed_end},volume={bed_db}dB[m_bed];"
        f"[0:a]aresample={_TARGET_SAMPLE_RATE},pan=mono|c0=0.5*c0+0.5*c1,"
        f"atrim={bed_end}:{tail_end},volume={bed_db}dB,"
        f"afade=t=out:st=0:d={tail_s}[m_tail];"
        f"[m_pre][m_bed][m_tail]concat=n=3:v=0:a=1[m_full];"
        f"[1:a]adelay={voice_delay_ms}|{voice_delay_ms},apad=pad_dur={tail_s + 0.5}[v_delayed];"
        f"[m_full][v_delayed]amix=inputs=2:duration=longest:dropout_transition=0[intro_section]"
    )


async def _ffmpeg_intro_mix(
    *,
    intro_music_path: Path,
    cold_open_voice_path: Path,
    cold_open_duration_s: float,
    music: MusicConfig,
    output_path: Path,
) -> None:
    """Run ffmpeg to produce the intro section (music + cold-open voice)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = _build_intro_filter(
        cold_open_duration_s=cold_open_duration_s,
        solo_s=music.intro_solo_seconds,
        tail_s=music.intro_tail_seconds,
        bed_db=music.bed_volume_db,
    )
    argv = [
        music.ffmpeg_path,
        "-y",
        "-i", str(intro_music_path),
        "-i", str(cold_open_voice_path),
        "-filter_complex", filter_complex,
        "-map", "[intro_section]",
        "-ac", str(_TARGET_CHANNELS),
        "-ar", str(_TARGET_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    logger.debug("ffmpeg intro-mix: %s", shlex.join(argv))
    rc, _stdout, stderr = await _run(argv)
    if rc != 0:
        raise AudioComposeError(
            f"ffmpeg intro-mix failed (rc={rc}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )


def _build_song_intro_filter(
    *,
    cold_open_duration_s: float,
    vocal_intro_s: float,
    transition_s: float,
    sting_seconds: float,
    sting_fade_out_s: float,
    bed_db: float,
) -> str:
    """Build the filter graph for the single-song intro pipeline.

    Inputs assumed:
      [0:a] = song (e.g. the_iron_crown.mp3)
      [1:a] = cold-open voice (headlines, no spoken brand line)

    Output label: [intro_section]

    Pipeline: ONE continuous music stream from the source, sliced to
    cover the full intro section length, with a piecewise-linear
    volume envelope applied so the music smoothly ducks under the
    voice and smoothly returns. Voice is mixed in on top during the
    bed period. No pitch shift — the music plays at its natural rate
    throughout, only volume changes.

    Volume envelope:
      0..vocal_intro_s              → full volume (1.0)
      ramp down (transition_s)       → 1.0 → bed_lin
      bed under voice                → bed_lin
      ramp up (transition_s)         → bed_lin → 1.0
      sting                          → full volume
      last fade_out_s                → fade to silence

    The source music is `aloop`-ed so a long cold open never runs out
    of music — silence-after-source-end would otherwise create the
    "25 second gap" the user can hear.
    """
    sr = _TARGET_SAMPLE_RATE
    bed_lin = pow(10.0, bed_db / 20.0)

    t_ramp_down_start = vocal_intro_s
    t_bed_start = vocal_intro_s + transition_s
    t_bed_end = t_bed_start + cold_open_duration_s
    t_ramp_up_end = t_bed_end + transition_s
    t_fade_start = t_ramp_up_end + sting_seconds
    t_section_end = t_fade_start + sting_fade_out_s

    # Piecewise linear volume envelope. The nested if() chain reads
    # easily one branch per envelope segment: solo, ramp down, bed,
    # ramp up, full (sting).
    vol_expr = (
        f"if(lt(t,{t_ramp_down_start}),1,"
        f"if(lt(t,{t_bed_start}),"
        f"1-(t-{t_ramp_down_start})/{transition_s}*(1-{bed_lin}),"
        f"if(lt(t,{t_bed_end}),{bed_lin},"
        f"if(lt(t,{t_ramp_up_end}),"
        f"{bed_lin}+(t-{t_bed_end})/{transition_s}*(1-{bed_lin}),"
        f"1))))"
    )

    voice_delay_ms = int(t_bed_start * 1000)

    return (
        # Music: loop the source to guarantee enough length, slice to
        # the full intro section, apply the volume envelope, then
        # afade-out the tail.
        f"[0:a]aresample={sr},pan=mono|c0=0.5*c0+0.5*c1,"
        f"aloop=loop=4:size=99999999,"
        f"atrim=0:{t_section_end},"
        f"volume='{vol_expr}':eval=frame,"
        f"afade=t=out:st={t_fade_start}:d={sting_fade_out_s}[music_env];"
        # Voice: delay so it lands at the start of the bed period
        # (after the music has finished ramping down to bed level).
        f"[1:a]aresample={sr},pan=mono|c0=0.5*c0+0.5*c1,"
        f"adelay={voice_delay_ms}|{voice_delay_ms}[voice];"
        # Mix music + voice. duration=longest keeps the music tail
        # (sting + fade) audible past the voice's end.
        f"[music_env][voice]amix=inputs=2:duration=longest:"
        f"dropout_transition=0[intro_section]"
    )


async def _ffmpeg_song_intro(
    *,
    song_path: Path,
    cold_open_voice_path: Path,
    cold_open_duration_s: float,
    music: MusicConfig,
    output_path: Path,
) -> None:
    """Single-song pipeline: brand vocal → bed under voice → sting → fade."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_complex = _build_song_intro_filter(
        cold_open_duration_s=cold_open_duration_s,
        vocal_intro_s=music.song_vocal_intro_seconds,
        transition_s=music.song_transition_seconds,
        sting_seconds=music.song_sting_seconds,
        sting_fade_out_s=music.song_fade_out_seconds,
        bed_db=music.bed_volume_db,
    )
    argv = [
        music.ffmpeg_path,
        "-y",
        "-i", str(song_path),
        "-i", str(cold_open_voice_path),
        "-filter_complex", filter_complex,
        "-map", "[intro_section]",
        "-ac", str(_TARGET_CHANNELS),
        "-ar", str(_TARGET_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    logger.debug("ffmpeg song-intro: %s", shlex.join(argv))
    rc, _stdout, stderr = await _run(argv)
    if rc != 0:
        raise AudioComposeError(
            f"ffmpeg song-intro failed (rc={rc}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )


def _build_input_chain(idx: int, ci: ConcatInput) -> str:
    """Filter chain for one concat input: resample → mono → trim? → fade?"""
    out_label = f"a{idx}"
    parts = [
        f"aresample={_TARGET_SAMPLE_RATE}",
        "pan=mono|c0=0.5*c0+0.5*c1",
    ]
    if ci.max_duration_seconds is not None:
        parts.append(f"atrim=0:{ci.max_duration_seconds}")
    if ci.fade_out_seconds is not None and ci.fade_out_seconds > 0:
        # Fade out anchored to the (trimmed) end of the clip. We use
        # `atrim` first so the fade always lands on the intended tail
        # rather than somewhere mid-track.
        end = ci.max_duration_seconds
        if end is None:
            # No trim: caller asked for fade against the natural end —
            # ffmpeg's afade=t=out needs a start time, so we use a very
            # large `st` only when paired with `atrim`. Without a known
            # end, skip fade rather than fade against something unknown.
            logger.warning(
                "ConcatInput requested fade_out without max_duration — skipping"
            )
        else:
            fade_start = max(0.0, end - ci.fade_out_seconds)
            parts.append(
                f"afade=t=out:st={fade_start}:d={ci.fade_out_seconds}"
            )
    return f"[{idx}:a]{','.join(parts)}[{out_label}]"


async def _ffmpeg_concat(
    *,
    inputs: list[ConcatInput],
    music: MusicConfig,
    output_path: Path,
) -> None:
    """Concatenate the listed audio inputs into a single WAV.

    Each input is normalized (resample to 24 kHz, downmix to mono) and
    optionally trimmed + fade-out'd before concat. This is what lets
    the sting be capped to N seconds with a tail fade without
    requiring a separate ffmpeg invocation.
    """
    if not inputs:
        raise AudioComposeError("ffmpeg concat needs at least one input")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    argv: list[str] = [music.ffmpeg_path, "-y"]
    for ci in inputs:
        argv.extend(["-i", str(ci.path)])

    chains: list[str] = []
    labels: list[str] = []
    for idx, ci in enumerate(inputs):
        chains.append(_build_input_chain(idx, ci))
        labels.append(f"[a{idx}]")
    chains.append(f"{''.join(labels)}concat=n={len(inputs)}:v=0:a=1[final]")
    filter_complex = ";".join(chains)

    argv.extend(
        [
            "-filter_complex", filter_complex,
            "-map", "[final]",
            "-ac", str(_TARGET_CHANNELS),
            "-ar", str(_TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            str(output_path),
        ]
    )
    logger.debug("ffmpeg concat: %s", shlex.join(argv))
    rc, _stdout, stderr = await _run(argv)
    if rc != 0:
        raise AudioComposeError(
            f"ffmpeg concat failed (rc={rc}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )


def _postprocess_chain(music: MusicConfig) -> str | None:
    """Build the ffmpeg audio-filter chain for podcast-grade post-processing.

    EBU R128 loudness normalization + safety limiter + HPF. Returned
    as a single `-af` filter chain string; None when disabled.
    """
    if not music.postprocess_enabled:
        return None
    parts = [
        f"highpass=f={music.postprocess_highpass_hz}",
        (
            f"loudnorm=I={music.postprocess_target_lufs}:"
            f"TP={music.postprocess_true_peak_db}:"
            f"LRA={music.postprocess_loudness_range_lu}"
        ),
        f"alimiter=limit={music.postprocess_true_peak_db}dB",
    ]
    return ",".join(parts)


async def _ffmpeg_postprocess(
    *,
    input_path: Path,
    output_path: Path,
    music: MusicConfig,
) -> None:
    """Apply HPF + loudnorm + limiter to `input_path`, write to `output_path`."""
    chain = _postprocess_chain(music)
    if chain is None:
        # Postprocessing disabled — fall back to a stream copy.
        argv = [
            music.ffmpeg_path, "-y",
            "-i", str(input_path),
            "-c:a", "pcm_s16le",
            "-ac", str(_TARGET_CHANNELS),
            "-ar", str(_TARGET_SAMPLE_RATE),
            str(output_path),
        ]
    else:
        argv = [
            music.ffmpeg_path, "-y",
            "-i", str(input_path),
            "-af", chain,
            "-c:a", "pcm_s16le",
            "-ac", str(_TARGET_CHANNELS),
            "-ar", str(_TARGET_SAMPLE_RATE),
            str(output_path),
        ]
    logger.debug("ffmpeg postprocess: %s", shlex.join(argv))
    rc, _stdout, stderr = await _run(argv)
    if rc != 0:
        raise AudioComposeError(
            f"ffmpeg postprocess failed (rc={rc}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )


async def compose_episode(
    *,
    cold_open_voice_path: Path | None,
    body_voice_path: Path,
    body_section_voice_paths: list[Path] | None = None,
    music: MusicConfig,
    output_path: Path,
    workdir: Path,
) -> int:
    """Compose the final episode WAV from voice segments and music.

    Returns the duration (seconds) of the output. Stages:

    1. If `cold_open_voice_path` and `intro_music_path` are both set,
       build `intro_section.wav` as music+voice mix.
       Else if only `cold_open_voice_path` is set, intro_section is
       just the cold-open voice file.
       Else (no cold open), intro_section is omitted.
    2. Concat: `[intro_section?] [sting?] [news] [player_jingle?]
       [player] [team_jingle?] [team] [deep_dive_jingle?]
       [deep_dive]` when section voice paths are supplied; otherwise
       use the legacy `[body]` input.

    File-not-found warnings: any music path that doesn't exist on disk
    is skipped with a logged warning; the rest of the pipeline still
    runs so a missing music file never breaks delivery.
    """
    workdir.mkdir(parents=True, exist_ok=True)

    # Resolve usable music paths (exist on disk).
    intro_path = music.intro_music_path
    if intro_path is not None and not intro_path.exists():
        logger.warning("Intro music not found, skipping: %s", intro_path)
        intro_path = None

    sting_path = music.sting_music_path
    if sting_path is not None and not sting_path.exists():
        logger.warning("Sting music not found, skipping: %s", sting_path)
        sting_path = None

    section_jingles = [
        music.player_of_day_jingle_path,
        music.team_of_day_jingle_path,
        music.deep_dive_jingle_path,
    ]
    usable_section_jingles: list[Path | None] = []
    for path in section_jingles:
        if path is not None and not path.exists():
            logger.warning("Section jingle not found, skipping: %s", path)
            usable_section_jingles.append(None)
        else:
            usable_section_jingles.append(path)

    # Stage 1: intro_section.
    # In song-mode, the single song file handles brand vocal + bed +
    # sting + fade all in one ffmpeg pass — no separate sting concat.
    song_mode_active = (
        music.song_mode
        and intro_path is not None
        and cold_open_voice_path is not None
    )

    intro_section_path: Path | None = None
    if cold_open_voice_path is not None:
        if song_mode_active:
            cold_open_dur = await probe_duration_seconds(
                cold_open_voice_path, ffprobe_path=music.ffprobe_path
            )
            intro_section_path = workdir / "intro_section.wav"
            await _ffmpeg_song_intro(
                song_path=intro_path,  # type: ignore[arg-type]
                cold_open_voice_path=cold_open_voice_path,
                cold_open_duration_s=cold_open_dur,
                music=music,
                output_path=intro_section_path,
            )
        elif intro_path is not None:
            cold_open_dur = await probe_duration_seconds(
                cold_open_voice_path, ffprobe_path=music.ffprobe_path
            )
            intro_section_path = workdir / "intro_section.wav"
            await _ffmpeg_intro_mix(
                intro_music_path=intro_path,
                cold_open_voice_path=cold_open_voice_path,
                cold_open_duration_s=cold_open_dur,
                music=music,
                output_path=intro_section_path,
            )
        else:
            # No intro music, intro_section is just the cold-open voice.
            intro_section_path = cold_open_voice_path

    # Stage 2: concat [intro_section?] [sting?] [body].
    # Song-mode embeds the sting inside intro_section, so skip the
    # separate sting concat input entirely.
    concat_inputs: list[ConcatInput] = []
    if intro_section_path is not None:
        concat_inputs.append(ConcatInput(path=intro_section_path))
    if sting_path is not None and not song_mode_active:
        # Cap the sting and fade its tail so a 2-min track delivers a
        # tight 30-second podcast-style transition.
        concat_inputs.append(
            ConcatInput(
                path=sting_path,
                max_duration_seconds=music.sting_max_seconds,
                fade_out_seconds=music.sting_fade_out_seconds,
            )
        )
    if body_section_voice_paths:
        for idx, section_path in enumerate(body_section_voice_paths):
            if idx > 0:
                jingle = usable_section_jingles[idx - 1]
                if jingle is not None:
                    concat_inputs.append(
                        ConcatInput(
                            path=jingle,
                            max_duration_seconds=music.section_jingle_max_seconds,
                            fade_out_seconds=music.section_jingle_fade_out_seconds,
                        )
                    )
            concat_inputs.append(ConcatInput(path=section_path))
    else:
        concat_inputs.append(ConcatInput(path=body_voice_path))

    # Stage 2: concat into a raw composite (no post-processing yet).
    raw_composite = workdir / "raw_composite.wav"
    await _ffmpeg_concat(
        inputs=concat_inputs, music=music, output_path=raw_composite
    )

    # Stage 3: podcast-grade post-processing — HPF + EBU R128 loudnorm
    # + safety limiter — yields the final WAV the user listens to.
    await _ffmpeg_postprocess(
        input_path=raw_composite, output_path=output_path, music=music
    )

    duration = await probe_duration_seconds(
        output_path, ffprobe_path=music.ffprobe_path
    )
    return int(duration)
