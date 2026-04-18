-- ============================================================
-- Warmr — Admin role migration
-- Run this in the Supabase SQL editor after the base schema
-- ============================================================

-- 1. Add is_admin flag to clients table
ALTER TABLE clients
  ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS notes TEXT;

-- 2. Update RLS policies so admins can read/write ALL rows
--    (the service-role key already bypasses RLS; these policies
--     allow admin users to query directly via the anon/user key too)

-- inboxes: admins can see all inboxes
DROP POLICY IF EXISTS "Admins can access all inboxes" ON inboxes;
CREATE POLICY "Admins can access all inboxes"
  ON inboxes FOR ALL
  USING (
    client_id = auth.uid()::text
    OR EXISTS (
      SELECT 1 FROM clients
      WHERE clients.id = auth.uid()
        AND clients.is_admin = true
    )
  );

-- domains: admins can see all domains
DROP POLICY IF EXISTS "Admins can access all domains" ON domains;
CREATE POLICY "Admins can access all domains"
  ON domains FOR ALL
  USING (
    client_id = auth.uid()::text
    OR EXISTS (
      SELECT 1 FROM clients
      WHERE clients.id = auth.uid()
        AND clients.is_admin = true
    )
  );

-- campaigns (if table exists)
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'campaigns') THEN
    DROP POLICY IF EXISTS "Admins can access all campaigns" ON campaigns;
    EXECUTE '
      CREATE POLICY "Admins can access all campaigns"
        ON campaigns FOR ALL
        USING (
          client_id = auth.uid()::text
          OR EXISTS (
            SELECT 1 FROM clients
            WHERE clients.id = auth.uid()
              AND clients.is_admin = true
          )
        )';
  END IF;
END $$;

-- clients: admins can see all client rows
ALTER TABLE clients ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can see their own client row" ON clients;
CREATE POLICY "Users can see their own client row"
  ON clients FOR SELECT
  USING (id = auth.uid());

DROP POLICY IF EXISTS "Users can update their own client row" ON clients;
CREATE POLICY "Users can update their own client row"
  ON clients FOR UPDATE
  USING (id = auth.uid());

DROP POLICY IF EXISTS "Admins can see all client rows" ON clients;
CREATE POLICY "Admins can see all client rows"
  ON clients FOR ALL
  USING (
    id = auth.uid()
    OR EXISTS (
      SELECT 1 FROM clients c2
      WHERE c2.id = auth.uid()
        AND c2.is_admin = true
    )
  );

-- 3. Promote the first registered user to admin
--    (Run manually after your first signup, replacing the email)
-- UPDATE clients SET is_admin = true WHERE email = 'jouw@email.nl';
