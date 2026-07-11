-- 2026-07-11_capaciteit_terug.sql — Werkpakket W0/W1 (mailbox health plan).
--
-- inbox_status_log: append-only audit-trail van élke statuswissel. De
-- pauze van 26-27 mei was zes weken lang onherleidbaar omdat geen enkel
-- pad vastlegde wie/wat/waarom pauzeerde. Dit is de tabel die dat
-- permanent oplost — en het 7-dagen-venster voedt dat de eeuwige
-- auto_pause_count_24h-teller vervangt in de promotiecriteria.
--
-- Draai VÓÓR de code-deploy. Gedrag zonder tabel: writers loggen luid en
-- gaan door (fail-soft — een gemiste log-rij mag geen pauze/resume
-- blokkeren); het promotiecriterium valt terug op warmup_logs.

CREATE TABLE IF NOT EXISTS inbox_status_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    inbox_id    UUID NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    reason      TEXT NOT NULL,   -- smtp_errors | bounce_rate | critical_reputation | auto_resume | promotion | manual | forensic_backfill
    source      TEXT NOT NULL,   -- diagnostics | bounce_handler | auto_promote | operator | ...
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inbox_status_log_inbox
  ON inbox_status_log (inbox_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_status_log_paused
  ON inbox_status_log (inbox_id, created_at DESC) WHERE to_status = 'paused';

COMMENT ON TABLE inbox_status_log IS
  'Append-only: elke inboxes.status-wissel is één rij (wie/wat/waarom). Nooit updaten of deleten. Voedt het 7d-pauzevenster in de promotiecriteria (vervangt auto_pause_count_24h).';

-- ── Forensic backfill: de gereconstrueerde geschiedenis van de fuik ─────────
-- (bron: warmup_logs action='auto_paused' 26-27 mei + diagnostics.err.log)
INSERT INTO inbox_status_log (inbox_id, from_status, to_status, reason, source, detail)
SELECT wl.inbox_id, 'warmup', 'paused', 'smtp_errors', 'forensic_backfill',
       jsonb_build_object('notes', wl.notes, 'original_timestamp', wl.timestamp,
                          'root_cause', 'Claude 529 generation-errors geteld als SMTP-errors')
FROM warmup_logs wl
WHERE wl.action = 'auto_paused'
  AND NOT EXISTS (
    SELECT 1 FROM inbox_status_log sl
    WHERE sl.inbox_id = wl.inbox_id AND sl.reason = 'smtp_errors'
      AND sl.source = 'forensic_backfill'
      AND sl.detail->>'original_timestamp' = wl.timestamp::text
  );

-- ── Verificatie ─────────────────────────────────────────────────────────────
SELECT to_status, reason, source, COUNT(*) FROM inbox_status_log
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
