"""T4L Daily Briefing — distribution module.

Owns the bridge between a rendered podcast episode (a local MP3 path
plus DB metadata) and the user's personal Spotify library. For the
personal-only MVP this wraps the `save-to-spotify` CLI; the v2
multi-user subscription model swaps this implementation for the
Spotify Web API behind the same `Delivery` boundary.

The module never knows how the audio was generated; it reads a row
and uploads.
"""
