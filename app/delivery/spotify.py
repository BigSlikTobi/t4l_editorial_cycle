"""Save-to-Spotify CLI wrapper for Spotify Personal Podcasts.

Subprocess invocation of `save-to-spotify` — the official CLI Spotify
ships for their Personal Podcasts beta. The CLI handles OAuth via a
token file at `~/.config/save-to-spotify/token.json` (respects
`$XDG_CONFIG_HOME`). The whole module is the v2-replaceable boundary:
when we move to a multi-user subscription model with our own Spotify
dev app + per-user OAuth, we swap this implementation for one that
calls the Spotify Web API directly, behind the same `dispatch`
interface.

Real CLI shape (per github.com/spotify/save-to-spotify):
    save-to-spotify upload <FILE> --title <TITLE> [flags]

Key flags:
    --title       (required) episode title
    --summary     episode description
    --show-id     target show ID/URI to publish under
    --new-show    create a new show with this title
    --language    language code (default: en)
    --image       cover art (.jpg/.png, max 1 MB)
    --json        emit structured output (for scripting)
    --timeout     request timeout override

Notes:
- File is POSITIONAL (not a flag).
- There is no `--token-path`; the CLI reads the XDG path.
- Token bootstrap: `save-to-spotify auth login` (or `--no-browser`).
- Status polling: `save-to-spotify episodes status <id> --wait`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from pathlib import Path

from app.config import Settings
from app.delivery.schemas import DeliveryResult

logger = logging.getLogger(__name__)


def _spotify_lang_code(language: str | None) -> str | None:
    """Best-effort: BCP-47 `en-US` → CLI `en`. None passes through."""
    if not language:
        return None
    return language.split("-")[0].lower()


def _build_argv(
    *,
    cli_path: str,
    audio_path: str,
    title: str,
    summary: str,
    show_id: str | None,
    language: str | None,
) -> list[str]:
    """Build the save-to-spotify upload argv.

    File is POSITIONAL. We pass --json so success/failure carries a
    parseable payload (episode_id, etc.). `--language` lets the CLI
    route the right language metadata into the Spotify episode.
    """
    argv: list[str] = [
        cli_path,
        "upload",
        audio_path,
        "--title",
        title,
        "--summary",
        summary,
        "--json",
    ]
    if show_id:
        argv.extend(["--show-id", show_id])
    lang_code = _spotify_lang_code(language)
    if lang_code:
        argv.extend(["--language", lang_code])
    return argv


def _parse_episode_id(stdout: str) -> str | None:
    """Pull an episode id out of the CLI's stdout.

    With `--json`, the CLI emits a JSON object — we look for any of the
    common keys (`episode_id`, `id`, `episode`). Falls back to a regex
    on a `episode_id: ...` line for non-JSON output. None when nothing
    parses; the delivery is still considered successful if exit code
    was 0 (we just can't link the episode back later).
    """
    stdout = stdout.strip()
    if not stdout:
        return None

    try:
        payload = json.loads(stdout)
        if isinstance(payload, dict):
            for key in ("episode_id", "id", "episode", "uri"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            # nested under `episode: {id: ...}`
            ep = payload.get("episode")
            if isinstance(ep, dict):
                for key in ("id", "uri", "episode_id"):
                    value = ep.get(key)
                    if isinstance(value, str) and value:
                        return value
    except json.JSONDecodeError:
        pass

    match = re.search(r"episode[_\s]*id[:\s]+([A-Za-z0-9_:/.-]+)", stdout, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


class SaveToSpotifyDelivery:
    """Wraps `save-to-spotify upload` for a single episode."""

    def __init__(self, settings: Settings) -> None:
        self._cli_path = settings.save_to_spotify_cli_path
        self._token_path = settings.spotify_token_path
        self._show_id = settings.save_to_spotify_show_id

    async def dispatch(
        self,
        *,
        audio_path: str,
        title: str,
        summary: str,
        dry_run: bool,
        language: str | None = None,
    ) -> DeliveryResult:
        argv = _build_argv(
            cli_path=self._cli_path,
            audio_path=audio_path,
            title=title,
            summary=summary,
            show_id=self._show_id,
            language=language,
        )
        invocation = shlex.join(argv)

        if dry_run:
            logger.info("Dry run: would invoke %s", invocation)
            return DeliveryResult(
                success=True,
                spotify_episode_id=None,
                error_message=None,
                invocation=invocation + "  # DRY RUN — not executed",
            )

        if not Path(audio_path).exists():
            return DeliveryResult(
                success=False,
                error_message=f"audio file not found: {audio_path}",
                invocation=invocation,
            )
        if not self._token_path.exists():
            return DeliveryResult(
                success=False,
                error_message=(
                    f"Spotify token not found at {self._token_path}. "
                    "Bootstrap with `save-to-spotify auth login` on this "
                    "machine, or run `save-to-spotify auth login --no-browser` "
                    "on the VPS for a paste-the-code flow."
                ),
                invocation=invocation,
            )

        logger.info("Invoking: %s", invocation)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
        except FileNotFoundError as exc:
            return DeliveryResult(
                success=False,
                error_message=(
                    f"save-to-spotify CLI not found ({exc}). Install via "
                    "`curl -fsSL https://saveto.spotify.com/install.sh | bash`."
                ),
                invocation=invocation,
            )

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")

        if proc.returncode != 0:
            tail = (stderr or stdout)[-500:]
            return DeliveryResult(
                success=False,
                error_message=f"save-to-spotify exited {proc.returncode}: {tail}",
                invocation=invocation,
            )

        episode_id = _parse_episode_id(stdout)
        logger.info(
            "save-to-spotify upload OK (episode_id=%s, stdout_len=%d)",
            episode_id, len(stdout),
        )
        if episode_id is None and stdout.strip():
            # We didn't find a known key — surface the raw stdout so we
            # can tune the parser to whatever shape the CLI actually
            # uses. Capped to keep the log line readable.
            logger.warning(
                "Could not extract Spotify episode id; raw stdout: %s",
                stdout.strip()[:400],
            )
        return DeliveryResult(
            success=True,
            spotify_episode_id=episode_id,
            invocation=invocation,
        )
