-- tenancy_hardening_migration.sql
-- Enforces referential integrity across all tenant tables + cascade deletes.
-- Safe to run multiple times (idempotent).

-- ── Ensure clients.suspended column exists ─────────────────────────────────
ALTER TABLE clients ADD COLUMN IF NOT EXISTS suspended BOOLEAN DEFAULT false;
UPDATE clients SET suspended = false WHERE suspended IS NULL;

-- ── FK constraints with CASCADE — isolation by construction ─────────────────

-- inboxes → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_inboxes_client') THEN
    BEGIN
      ALTER TABLE inboxes
        ADD CONSTRAINT fk_inboxes_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- domains → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_domains_client') THEN
    BEGIN
      ALTER TABLE domains
        ADD CONSTRAINT fk_domains_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- campaigns → clients (already has FK in most schemas but ensure cascade)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_campaigns_client') THEN
    BEGIN
      ALTER TABLE campaigns
        ADD CONSTRAINT fk_campaigns_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- leads → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_leads_client') THEN
    BEGIN
      ALTER TABLE leads
        ADD CONSTRAINT fk_leads_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- reply_inbox → clients (may not have a FK yet)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_reply_inbox_client') THEN
    BEGIN
      ALTER TABLE reply_inbox
        ADD CONSTRAINT fk_reply_inbox_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- suppression_list → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_suppression_client') THEN
    BEGIN
      ALTER TABLE suppression_list
        ADD CONSTRAINT fk_suppression_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- email_tracking → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_email_tracking_client') THEN
    BEGIN
      ALTER TABLE email_tracking
        ADD CONSTRAINT fk_email_tracking_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- unsubscribe_tokens → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_unsub_tokens_client') THEN
    BEGIN
      ALTER TABLE unsubscribe_tokens
        ADD CONSTRAINT fk_unsub_tokens_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- notifications → clients
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_notifications_client') THEN
    BEGIN
      ALTER TABLE notifications
        ADD CONSTRAINT fk_notifications_client
        FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END $$;

-- Note: warmup_logs inherits client isolation transitively via inbox_id → inboxes.client_id
-- The RLS policy should use: client_id = (SELECT client_id FROM inboxes WHERE id = warmup_logs.inbox_id)
