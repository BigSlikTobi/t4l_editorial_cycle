"""T4L Team Beat — twice-daily, per-team NFL roundups.

See docs/team_beat_mvp.md for product context. The module is layered onto
the existing editorial agency: it reuses the raw feed reader, the OpenAI
Agents SDK patterns, the persona system shape, and the Supabase auth
patterns from app/adapters.py — but it imports nothing from app/editorial/
or app/writer/ internals so the boundary stays clean for the eventual
32-team expansion.
"""
