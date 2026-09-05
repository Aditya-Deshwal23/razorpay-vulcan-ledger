-- 007_batch_upload_features.sql
--
-- Adds three things needed for the enterprise CSV upload, AI RCA, and audit
-- export features:
--
--   1. t_batch_registry  — sequential readable batch IDs (BATCH-001, BATCH-002).
--      A SERIAL primary key gives us the monotone integer; the application
--      formats it as zero-padded BATCH-NNN before writing to the FK column.
--
--   2. record_id column on t_razorpay_settlements — a within-batch sequential
--      identifier (REC-001, REC-002). Set by the CSV parser; NULL for
--      settlements ingested through the existing evaluation runner.
--
--   3. rca_reason column on t_reconciliation_ledger — the AI agent's plain-
--      language root-cause hypothesis, e.g. "Suspected 18% GST calculation
--      mismatch on gateway fees". NULL for deterministic matches that needed
--      no explanation.
--
-- All three additions are nullable / have defaults so existing rows and the
-- evaluation runner path are unaffected.

-- ============================================================================
-- 1. Batch registry — one row per uploaded CSV file
-- ============================================================================
CREATE TABLE IF NOT EXISTS t_batch_registry (
    sequence_num    SERIAL          PRIMARY KEY,          -- monotone counter
    batch_id        TEXT            NOT NULL UNIQUE,      -- BATCH-001, BATCH-002 …
    source          TEXT            NOT NULL DEFAULT 'CSV_UPLOAD',
    uploaded_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);

COMMENT ON TABLE t_batch_registry IS
    'Sequential registry of every batch ingested via the CSV upload API. '
    'sequence_num is the physical counter; batch_id is its human-readable form '
    '(BATCH-NNN). Immutable after insert.';

-- ============================================================================
-- 2. record_id on t_razorpay_settlements
-- ============================================================================
ALTER TABLE t_razorpay_settlements
    ADD COLUMN IF NOT EXISTS record_id VARCHAR(20) DEFAULT NULL;

COMMENT ON COLUMN t_razorpay_settlements.record_id IS
    'Within-batch sequential record identifier assigned by the CSV parser '
    '(REC-001, REC-002 …). NULL for settlements ingested via the evaluation '
    'runner or webhook paths.';

CREATE INDEX IF NOT EXISTS ix_settlements_record_id
    ON t_razorpay_settlements (record_id)
    WHERE record_id IS NOT NULL;

-- ============================================================================
-- 3. rca_reason on t_reconciliation_ledger
-- ============================================================================
ALTER TABLE t_reconciliation_ledger
    ADD COLUMN IF NOT EXISTS rca_reason VARCHAR(300) DEFAULT NULL;

COMMENT ON COLUMN t_reconciliation_ledger.rca_reason IS
    'AI-generated root-cause analysis hypothesis for why this settlement could '
    'not be deterministically matched. Written by the LangGraph agent; NULL for '
    'clean deterministic matches. Displayed to the human controller in the '
    'review queue as an amber badge.';
