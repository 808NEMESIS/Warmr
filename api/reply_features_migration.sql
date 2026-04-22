-- =====================================================================
-- Migration: reply-send + intent-psychology + meeting-intent on reply_inbox
-- Adds new columns; backward compatible — all nullable with sensible defaults.
-- =====================================================================

-- Threading: we need Message-ID and References from the original reply to
-- craft a proper RFC-5322 reply that lands in the existing thread.
ALTER TABLE reply_inbox
    ADD COLUMN IF NOT EXISTS message_id        TEXT,
    ADD COLUMN IF NOT EXISTS references_header TEXT;

-- Intent-psychology analysis (stored once, regenerated on demand).
-- Full structured output from the analyzer — Claude Opus returns JSON.
ALTER TABLE reply_inbox
    ADD COLUMN IF NOT EXISTS intent_analysis   JSONB,
    ADD COLUMN IF NOT EXISTS intent_analyzed_at TIMESTAMP;

-- Meeting-intent: separate from classification because a reply can be
-- "interested" AND signal "book a meeting now" simultaneously.
ALTER TABLE reply_inbox
    ADD COLUMN IF NOT EXISTS meeting_intent BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS urgency        TEXT;  -- low | medium | high | null

-- Outbound reply-send tracking.
ALTER TABLE reply_inbox
    ADD COLUMN IF NOT EXISTS reply_sent_at   TIMESTAMP,
    ADD COLUMN IF NOT EXISTS reply_sent_body TEXT,
    ADD COLUMN IF NOT EXISTS reply_sent_subject TEXT;

-- Index for the "high-priority inbox view" — meeting-intent replies float up.
CREATE INDEX IF NOT EXISTS idx_reply_inbox_meeting
    ON reply_inbox (client_id, meeting_intent, received_at DESC)
    WHERE meeting_intent = true;

-- Index for "not yet replied to" — powers the unified-inbox "needs action" count.
CREATE INDEX IF NOT EXISTS idx_reply_inbox_unreplied
    ON reply_inbox (client_id, received_at DESC)
    WHERE reply_sent_at IS NULL;
