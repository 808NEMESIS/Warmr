-- conditional_steps_migration.sql
-- Adds conditional sending logic to sequence_steps.
-- Safe to run multiple times.

ALTER TABLE sequence_steps
  ADD COLUMN IF NOT EXISTS condition_type TEXT DEFAULT 'always',
  ADD COLUMN IF NOT EXISTS condition_step INT,
  ADD COLUMN IF NOT EXISTS condition_skip_to INT;

-- condition_type values:
--   always         — send unconditionally (default)
--   if_opened      — only send if previous step was opened
--   if_not_opened  — only send if previous step was NOT opened
--   if_clicked     — only send if previous step had a link click
--   if_not_clicked — only send if previous step had no link click
--
-- condition_step: which previous step to check (defaults to step_number - 1)
-- condition_skip_to: if condition fails, jump to this step number (NULL = skip lead entirely)

COMMENT ON COLUMN sequence_steps.condition_type IS
  'always | if_opened | if_not_opened | if_clicked | if_not_clicked';
