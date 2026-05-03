-- Add tts_batch_id to team_roundup so a Gemini batch id is recoverable
-- from the DB even when the TTS process stage failed.
--
-- Why: when create succeeds but process never lands (timeout, worker
-- crash, etc.), the batch_id was previously only available in transient
-- log lines. Persisting it on the roundup row lets `scripts/tts_recover.py`
-- discover it without operator lookup in Gemini console.

ALTER TABLE public.team_roundup
    ADD COLUMN IF NOT EXISTS tts_batch_id text;

CREATE INDEX IF NOT EXISTS idx_team_roundup_tts_batch_id
    ON public.team_roundup (tts_batch_id)
    WHERE tts_batch_id IS NOT NULL;
