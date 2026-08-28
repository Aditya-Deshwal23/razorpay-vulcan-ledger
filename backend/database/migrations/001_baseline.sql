-- 001_baseline.sql
--
-- Canonical baseline schema for Razorpay Vulcan Ledger (Phase 2).
--
-- Written to be idempotent: running it against a database that already has
-- the Phase 2 schema is a no-op, and running it against an empty database
-- reproduces that schema exactly. This is what makes a fresh machine
-- reproducible without anyone hand-applying SQL.
--
-- LangGraph's own checkpoint tables (checkpoints, checkpoint_blobs,
-- checkpoint_writes, checkpoint_migrations) are deliberately NOT defined
-- here -- langgraph-checkpoint-postgres's AsyncPostgresSaver.setup()
-- creates and migrates them itself.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------------------
-- 1. Razorpay Settlements Ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_razorpay_settlements (
    internal_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id    VARCHAR(50) NOT NULL UNIQUE,
    status           VARCHAR(20) NOT NULL,
    gross_amount     DECIMAL(15, 2) NOT NULL CHECK (gross_amount >= 0),
    fees             DECIMAL(15, 2) NOT NULL DEFAULT 0.00 CHECK (fees >= 0),
    taxes            DECIMAL(15, 2) NOT NULL DEFAULT 0.00 CHECK (taxes >= 0),
    refunds          DECIMAL(15, 2) NOT NULL DEFAULT 0.00 CHECK (refunds >= 0),
    adjustments      DECIMAL(15, 2) NOT NULL DEFAULT 0.00,
    net_settlement   DECIMAL(15, 2) NOT NULL,
    utr_reference    VARCHAR(50),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_settlements_utr ON t_razorpay_settlements (utr_reference);
CREATE INDEX IF NOT EXISTS ix_settlements_status ON t_razorpay_settlements (status);

-- Trigger-enforced updated_at: fires on ANY update to this table, including
-- raw SQL that bypasses the ORM entirely -- not just SQLAlchemy's onupdate=.
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_updated_at_on_settlements ON t_razorpay_settlements;
CREATE TRIGGER set_updated_at_on_settlements
    BEFORE UPDATE ON t_razorpay_settlements
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. Bank Ledger -- normalized MT940 / CSV ingestion
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_bank_ledger (
    entry_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_name           VARCHAR(50) NOT NULL,
    transaction_date    DATE NOT NULL,
    credit_amount       DECIMAL(15, 2) NOT NULL CHECK (credit_amount >= 0),
    raw_narration       TEXT NOT NULL,
    extracted_utr       VARCHAR(50),
    is_reconciled       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_bank_entry UNIQUE (bank_name, extracted_utr, credit_amount)
);

CREATE INDEX IF NOT EXISTS ix_bank_ledger_utr ON t_bank_ledger (extracted_utr);
CREATE INDEX IF NOT EXISTS ix_bank_ledger_unreconciled
    ON t_bank_ledger (is_reconciled) WHERE is_reconciled = FALSE;

-- ---------------------------------------------------------------------------
-- 3. Core Reconciliation Audit Trail
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS t_reconciliation_ledger (
    recon_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    settlement_id               VARCHAR(50) NOT NULL
        REFERENCES t_razorpay_settlements(settlement_id) ON DELETE RESTRICT,
    bank_entry_id               UUID REFERENCES t_bank_ledger(entry_id) ON DELETE RESTRICT,
    recon_state                 VARCHAR(30) NOT NULL,
    numeric_variance            DECIMAL(15, 2) NOT NULL DEFAULT 0.00,
    ai_classification_reason    VARCHAR(100),
    cryptographic_state_hash    VARCHAR(64) NOT NULL,
    resolved_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_settlement_recon UNIQUE (settlement_id)
);

CREATE INDEX IF NOT EXISTS ix_recon_state ON t_reconciliation_ledger (recon_state);
