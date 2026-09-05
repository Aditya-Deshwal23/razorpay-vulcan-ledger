-- 009_batch_record_count.sql
-- Persist the upload cardinality so progress survives navigation and reloads.

ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS total_records INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN t_batch_registry.total_records IS
    'Number of data rows accepted from the uploaded payload.';
