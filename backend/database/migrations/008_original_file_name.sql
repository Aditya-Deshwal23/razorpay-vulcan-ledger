-- 008_original_file_name.sql
-- Preserve the operator-facing name of each uploaded settlement file.

ALTER TABLE t_batch_registry
    ADD COLUMN IF NOT EXISTS original_file_name VARCHAR(255) NOT NULL DEFAULT 'settlements.csv';

COMMENT ON COLUMN t_batch_registry.original_file_name IS
    'Original sanitized filename supplied with the uploaded settlement batch.';
