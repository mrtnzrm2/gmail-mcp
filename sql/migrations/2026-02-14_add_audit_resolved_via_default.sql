ALTER TABLE IF EXISTS audit_events
ADD COLUMN IF NOT EXISTS resolved_via_default boolean NOT NULL DEFAULT false;
