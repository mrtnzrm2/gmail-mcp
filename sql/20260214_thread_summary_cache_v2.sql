ALTER TABLE IF EXISTS thread_summaries_cache
ADD COLUMN IF NOT EXISTS thread_last_msg_id TEXT NULL;

ALTER TABLE IF EXISTS thread_summaries_cache
ADD COLUMN IF NOT EXISTS thread_last_internal_ms BIGINT NULL;

CREATE INDEX IF NOT EXISTS ix_thread_summaries_cache_user_account_thread
ON thread_summaries_cache (user_id, account_id, thread_id);

ALTER TABLE IF EXISTS audit_events
ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE IF EXISTS audit_events
ADD COLUMN IF NOT EXISTS hybrid_applied BOOLEAN;

UPDATE audit_events SET hybrid_applied=false WHERE hybrid_applied IS NULL;

ALTER TABLE IF EXISTS audit_events
ALTER COLUMN hybrid_applied SET DEFAULT false;

ALTER TABLE IF EXISTS audit_events
ALTER COLUMN hybrid_applied SET NOT NULL;

ALTER TABLE IF EXISTS audit_events
ADD COLUMN IF NOT EXISTS hybrid_model TEXT NULL;
