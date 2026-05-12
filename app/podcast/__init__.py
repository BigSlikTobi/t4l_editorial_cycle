"""T4L Daily Briefing — personal NFL podcast generation.

Reads the same raw NFL feed the editorial cycle consumes, re-clusters
league-wide (broader than the team-anchored editorial cluster), generates
a two-persona script (color commentator + numbers analyst) in EN and DE,
and renders audio via a direct Gemini multi-speaker TTS integration.

The module is user-agnostic: it produces an episode given a language,
nothing more. Distribution to Spotify lives in `app.delivery`.
"""
