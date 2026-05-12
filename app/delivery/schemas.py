"""Schemas for the delivery module.

Kept deliberately tiny: the delivery module's purpose is to take a
rendered episode (already in the DB + on disk) and ship it to the
user's Spotify library. The whole module is the v2-replaceable layer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeliveryResult:
    """Outcome of one delivery attempt."""

    success: bool
    spotify_episode_id: str | None = None
    error_message: str | None = None
    invocation: str | None = None  # the argv string for audit logs
