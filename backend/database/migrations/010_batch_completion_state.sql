-- 010_batch_completion_state.sql
-- Durable lifecycle state for atomic completion reporting.

ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'accepted';
ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS processed_records INTEGER NOT NULL DEFAULT 0;
ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

COMMENT ON COLUMN t_batch_registry.status IS
    'Batch lifecycle: accepted, processing, completed, or failed.';
