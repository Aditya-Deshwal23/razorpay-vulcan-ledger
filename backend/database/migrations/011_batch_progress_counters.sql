-- 011_batch_progress_counters.sql
-- Durable intermediate metrics for live batch progress polling.

ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS resolved_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS rule_matched_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS exceptions_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN t_batch_registry.resolved_count IS
    'Rows persisted by the reconciliation worker so far.';
COMMENT ON COLUMN t_batch_registry.rule_matched_count IS
    'Rows resolved by deterministic rules so far.';
COMMENT ON COLUMN t_batch_registry.exceptions_count IS
    'Rows routed to AI or human review so far.';
