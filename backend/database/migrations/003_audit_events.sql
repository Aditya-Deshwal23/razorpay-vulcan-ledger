-- 003_audit_events.sql
--
-- Make reconciliation evidence and state changes independently auditable.
--
-- 002_hardening introduced cryptographic_state_hash, but the hash includes
-- recon_state. A later human decision therefore changed the row's state while
-- leaving its old hash behind. It also left no immutable record of the prior
-- state for an operator or API to inspect. This migration retains the exact
-- narration used to derive a fingerprint, backfills it from the bank ledger,
-- and creates an append-only event trail for each persisted state transition.
--
-- The backfill refuses to make up evidence. Every existing reconciliation must
-- be able to point to the source narration that was already stored in the bank
-- ledger, or a human must repair that historical record before this migration
-- can claim it is auditable.

ALTER TABLE t_reconciliation_ledger
    ADD COLUMN IF NOT EXISTS evidence_narration TEXT;

UPDATE t_reconciliation_ledger r
SET evidence_narration = b.raw_narration
FROM t_bank_ledger b
WHERE r.bank_entry_id = b.entry_id
  AND r.evidence_narration IS NULL;

DO $$
DECLARE
    missing_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO missing_count
    FROM t_reconciliation_ledger
    WHERE evidence_narration IS NULL OR length(trim(evidence_narration)) = 0;

    IF missing_count > 0 THEN
        RAISE EXCEPTION
            'Cannot add evidence_narration: % reconciliation row(s) have no canonical '
            'bank narration to support their audit fingerprint. Repair those rows before '
            'claiming the ledger is auditable.', missing_count;
    END IF;
END $$;

ALTER TABLE t_reconciliation_ledger
    ALTER COLUMN evidence_narration SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_recon_evidence_narration_nonempty'
    ) THEN
        ALTER TABLE t_reconciliation_ledger
            ADD CONSTRAINT ck_recon_evidence_narration_nonempty
            CHECK (length(trim(evidence_narration)) > 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS t_reconciliation_events (
    event_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recon_id                    UUID NOT NULL
        REFERENCES t_reconciliation_ledger(recon_id) ON DELETE RESTRICT,
    settlement_id               VARCHAR(50) NOT NULL
        REFERENCES t_razorpay_settlements(settlement_id) ON DELETE RESTRICT,
    batch_run_id                VARCHAR(64),
    event_type                  VARCHAR(40) NOT NULL,
    from_state                  VARCHAR(30),
    to_state                    VARCHAR(30) NOT NULL,
    human_decision              VARCHAR(20),
    human_decision_by           VARCHAR(100),
    numeric_variance            DECIMAL(15, 2) NOT NULL,
    cryptographic_state_hash    VARCHAR(64) NOT NULL,
    occurred_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT unique_reconciliation_event_state UNIQUE (recon_id, to_state),
    CONSTRAINT ck_reconciliation_event_type CHECK (event_type IN (
        'RECONCILIATION_RECORDED',
        'RECONCILIATION_RECLASSIFIED',
        'HUMAN_DECISION_RECORDED'
    )),
    CONSTRAINT ck_reconciliation_event_state CHECK (to_state IN (
        'DETERMINISTIC_MATCH',
        'AI_RESOLVED',
        'PENDING_HITL_REVIEW',
        'HITL_APPROVED',
        'HITL_REJECTED'
    )),
    CONSTRAINT ck_reconciliation_event_decision CHECK (
        (event_type = 'HUMAN_DECISION_RECORDED'
            AND human_decision IN ('APPROVED', 'REJECTED')
            AND human_decision_by IS NOT NULL)
        OR
        (event_type <> 'HUMAN_DECISION_RECORDED'
            AND human_decision IS NULL
            AND human_decision_by IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_reconciliation_events_batch_time
    ON t_reconciliation_events (batch_run_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS ix_reconciliation_events_settlement_time
    ON t_reconciliation_events (settlement_id, occurred_at DESC);

-- Snapshot the existing current ledger rows once. `DO NOTHING` makes a
-- recovery re-run safe if a deployment stops after the table is created.
INSERT INTO t_reconciliation_events (
    recon_id,
    settlement_id,
    batch_run_id,
    event_type,
    from_state,
    to_state,
    human_decision,
    human_decision_by,
    numeric_variance,
    cryptographic_state_hash,
    occurred_at
)
SELECT
    r.recon_id,
    r.settlement_id,
    r.batch_run_id,
    CASE
        WHEN r.recon_state IN ('HITL_APPROVED', 'HITL_REJECTED')
            THEN 'HUMAN_DECISION_RECORDED'
        ELSE 'RECONCILIATION_RECORDED'
    END,
    NULL,
    r.recon_state,
    CASE WHEN r.recon_state IN ('HITL_APPROVED', 'HITL_REJECTED')
        THEN r.human_decision ELSE NULL END,
    CASE WHEN r.recon_state IN ('HITL_APPROVED', 'HITL_REJECTED')
        THEN r.human_decision_by ELSE NULL END,
    r.numeric_variance,
    r.cryptographic_state_hash,
    r.resolved_at
FROM t_reconciliation_ledger r
ON CONFLICT ON CONSTRAINT unique_reconciliation_event_state DO NOTHING;

-- Application code only appends events. Blocking UPDATE/DELETE at the database
-- level keeps an accidental ORM write from rewriting history; privileged DB
-- maintenance remains an explicit operational act rather than a normal path.
CREATE OR REPLACE FUNCTION reject_reconciliation_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('vulcan.allow_audit_maintenance', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        't_reconciliation_events is append-only; create a corrective event instead of % '
        '(controlled maintenance must SET LOCAL vulcan.allow_audit_maintenance = on)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS reject_reconciliation_event_mutation ON t_reconciliation_events;
CREATE TRIGGER reject_reconciliation_event_mutation
    BEFORE UPDATE OR DELETE ON t_reconciliation_events
    FOR EACH ROW
    EXECUTE FUNCTION reject_reconciliation_event_mutation();
