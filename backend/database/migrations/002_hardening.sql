-- 002_hardening.sql
--
-- Phase 1-5 hardening. Closes the defects found by audit against the live
-- database:
--
--   1. unique_bank_entry cannot dedupe UTR-less credits, because PostgreSQL
--      treats NULLs as distinct in a UNIQUE constraint. A bank credit whose
--      narration has no parseable UTR could be re-imported without limit
--      (24 such rows existed for only 3 distinct real credits). Fixed with a
--      deterministic dedupe_hash over the full natural key, plus a partial
--      unique index making a parsed UTR exclusive per bank.
--   2. Nothing stopped two settlements from claiming the SAME bank credit --
--      double-counting a real payout. Fixed with UNIQUE(bank_entry_id).
--   3. PENDING_HITL_REVIEW was a terminal state: no LangGraph thread id was
--      persisted and there was no column to record a human decision, so a
--      review could never be completed or audited.
--   4. numeric_variance held whatever variance the LLM reported about itself.
--      The deterministic variance now owns that column and the model's claim
--      is quarantined in ai_reported_variance, so the two can be compared.
--   5. recon_state was a free VARCHAR -- a typo could invent a state that
--      every downstream aggregation would silently miss.
--   6. is_reconciled was never written by any code path, so every bank row
--      read as outstanding regardless of how much had been matched. Backfilled
--      here; written by config.database.mark_bank_entry_reconciled from now on.
--   7. Nothing tied a reconciliation row to the run that produced it, so
--      benchmarks were scoped by LIKE over a business key. Added batch_run_id.
--   8. A settlement's own numbers could contradict
--      Net = Gross - Fees - Taxes - Refunds - Adjustments. Now a CHECK.
--
-- Idempotent and safe to re-run. Refuses to proceed (RAISE EXCEPTION) rather
-- than silently mangling financial rows if the existing data cannot be
-- deduplicated unambiguously.

-- ---------------------------------------------------------------------------
-- 1. t_bank_ledger: deterministic dedupe key
-- ---------------------------------------------------------------------------
ALTER TABLE t_bank_ledger ADD COLUMN IF NOT EXISTS dedupe_hash VARCHAR(64);

-- Must stay byte-for-byte identical to bank_dedupe_hash() in
-- backend/config/database.py. test_bank_dedupe_hash_matches_postgres asserts
-- that equivalence so the two can never drift apart unnoticed.
UPDATE t_bank_ledger
SET dedupe_hash = encode(
        digest(
            bank_name || '|' ||
            COALESCE(extracted_utr, '') || '|' ||
            credit_amount::text || '|' ||
            to_char(transaction_date, 'YYYY-MM-DD') || '|' ||
            raw_narration,
            'sha256'
        ),
        'hex'
    )
WHERE dedupe_hash IS NULL;

-- Pre-flight: if two rows sharing a dedupe_hash are BOTH already claimed by a
-- reconciliation row, that is pre-existing double-counting. Collapsing them
-- would destroy an audit trail, so stop and make a human look at it.
DO $$
DECLARE
    conflicting TEXT;
BEGIN
    SELECT string_agg(dedupe_hash, ', ')
      INTO conflicting
      FROM (
        SELECT b.dedupe_hash
          FROM t_bank_ledger b
          JOIN t_reconciliation_ledger r ON r.bank_entry_id = b.entry_id
         GROUP BY b.dedupe_hash
        HAVING COUNT(*) > 1
      ) x;

    IF conflicting IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot add unique_bank_dedupe_hash: duplicate bank credits are each '
            'already reconciled against a different settlement (dedupe_hash: %). '
            'This is pre-existing double-counting and needs a human decision -- '
            'migration refuses to collapse reconciled financial rows.', conflicting;
    END IF;
END $$;

-- Canonical row per dedupe_hash: prefer the one a reconciliation row already
-- points at (at most one, guaranteed by the check above), then the oldest,
-- then the lowest entry_id. Fully deterministic.
CREATE TEMP TABLE _bank_dedupe_plan AS
SELECT
    b.entry_id,
    b.dedupe_hash,
    FIRST_VALUE(b.entry_id) OVER (
        PARTITION BY b.dedupe_hash
        ORDER BY (r.bank_entry_id IS NOT NULL) DESC, b.created_at ASC, b.entry_id ASC
    ) AS keep_id
FROM t_bank_ledger b
LEFT JOIN t_reconciliation_ledger r ON r.bank_entry_id = b.entry_id;

UPDATE t_reconciliation_ledger r
SET bank_entry_id = p.keep_id
FROM _bank_dedupe_plan p
WHERE r.bank_entry_id = p.entry_id
  AND p.entry_id <> p.keep_id;

DELETE FROM t_bank_ledger b
USING _bank_dedupe_plan p
WHERE b.entry_id = p.entry_id
  AND p.entry_id <> p.keep_id;

DROP TABLE _bank_dedupe_plan;

ALTER TABLE t_bank_ledger ALTER COLUMN dedupe_hash SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'unique_bank_dedupe_hash'
    ) THEN
        ALTER TABLE t_bank_ledger
            ADD CONSTRAINT unique_bank_dedupe_hash UNIQUE (dedupe_hash);
    END IF;
END $$;

-- One parsed UTR is one real NEFT/RTGS transfer, so a second credit claiming
-- the same (bank_name, extracted_utr) is a data conflict to surface, not
-- another payout to book. dedupe_hash alone cannot catch this: the bank
-- re-sending the same credit with a reworded narration produces a different
-- hash. Partial index, so the legitimately UTR-less rows are unaffected.
DO $$
DECLARE
    dup TEXT;
BEGIN
    SELECT string_agg(pair, ', ')
      INTO dup
      FROM (
        SELECT bank_name || '/' || extracted_utr AS pair
          FROM t_bank_ledger
         WHERE extracted_utr IS NOT NULL
         GROUP BY bank_name, extracted_utr
        HAVING COUNT(*) > 1
      ) x;

    IF dup IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot add ux_bank_ledger_utr: these (bank, UTR) pairs appear on more '
            'than one bank credit: %. One UTR is one transfer -- the duplicates need '
            'a human decision before this index can be enforced.', dup;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_ledger_utr
    ON t_bank_ledger (bank_name, extracted_utr)
    WHERE extracted_utr IS NOT NULL;

-- unique_bank_entry (bank_name, extracted_utr, credit_amount) is now strictly
-- redundant: for a non-NULL UTR, ux_bank_ledger_utr above is stronger (it
-- ignores the amount), and for a NULL UTR the old constraint never constrained
-- anything at all. Dropping it leaves exactly one conflict target for the
-- ingestion path -- keeping it would mean a re-import with a reworded narration
-- raised an unhandled IntegrityError on a constraint no longer expressing any
-- rule the schema needs. The ORM model in config/database.py declares only the
-- two surviving guards, so this keeps Python and PostgreSQL in agreement.
ALTER TABLE t_bank_ledger DROP CONSTRAINT IF EXISTS unique_bank_entry;

-- is_reconciled has existed since Phase 2 but no code path ever wrote it, so
-- every bank row read as outstanding no matter how many settlements had been
-- matched against it. Backfill it from the reconciliation ledger, which is the
-- authority on what is actually matched.
UPDATE t_bank_ledger b
SET is_reconciled = TRUE
WHERE b.is_reconciled = FALSE
  AND EXISTS (SELECT 1 FROM t_reconciliation_ledger r WHERE r.bank_entry_id = b.entry_id);

-- ---------------------------------------------------------------------------
-- 2. t_reconciliation_ledger: one bank credit can back at most one settlement
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    shared TEXT;
BEGIN
    SELECT string_agg(bank_entry_id::text, ', ')
      INTO shared
      FROM (
        SELECT bank_entry_id
          FROM t_reconciliation_ledger
         WHERE bank_entry_id IS NOT NULL
         GROUP BY bank_entry_id
        HAVING COUNT(*) > 1
      ) x;

    IF shared IS NOT NULL THEN
        RAISE EXCEPTION
            'Cannot add unique_recon_bank_entry: bank entries % are already claimed '
            'by more than one settlement. Resolve the double-counting first.', shared;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'unique_recon_bank_entry'
    ) THEN
        -- NULLs stay distinct here on purpose: many settlements may legitimately
        -- have no bank entry yet (unmatched), but a non-NULL entry is exclusive.
        ALTER TABLE t_reconciliation_ledger
            ADD CONSTRAINT unique_recon_bank_entry UNIQUE (bank_entry_id);
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. t_reconciliation_ledger: make HITL a completable, auditable flow
-- ---------------------------------------------------------------------------
ALTER TABLE t_reconciliation_ledger
    ADD COLUMN IF NOT EXISTS agent_thread_id     VARCHAR(200),
    ADD COLUMN IF NOT EXISTS ai_reported_variance DECIMAL(15, 2),
    ADD COLUMN IF NOT EXISTS ai_confidence_score  NUMERIC(4, 3),
    ADD COLUMN IF NOT EXISTS human_decision       VARCHAR(20),
    ADD COLUMN IF NOT EXISTS human_decision_by    VARCHAR(100),
    ADD COLUMN IF NOT EXISTS human_decision_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW();

DROP TRIGGER IF EXISTS set_updated_at_on_recon ON t_reconciliation_ledger;
CREATE TRIGGER set_updated_at_on_recon
    BEFORE UPDATE ON t_reconciliation_ledger
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_updated_at();

-- The HITL work queue is read constantly by an operator UI and is a tiny
-- fraction of the table -- a partial index keeps that lookup index-only.
CREATE INDEX IF NOT EXISTS ix_recon_pending_hitl
    ON t_reconciliation_ledger (resolved_at)
    WHERE recon_state = 'PENDING_HITL_REVIEW';

CREATE INDEX IF NOT EXISTS ix_recon_thread_id
    ON t_reconciliation_ledger (agent_thread_id)
    WHERE agent_thread_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. Closed state machine + decision integrity
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_recon_state_enum') THEN
        ALTER TABLE t_reconciliation_ledger
            ADD CONSTRAINT ck_recon_state_enum CHECK (recon_state IN (
                'DETERMINISTIC_MATCH',
                'AI_RESOLVED',
                'PENDING_HITL_REVIEW',
                'HITL_APPROVED',
                'HITL_REJECTED'
            ));
    END IF;

    -- A human decision and a terminal HITL state must always appear together:
    -- no decision without a state, no state without a decision and timestamp.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_recon_human_decision') THEN
        ALTER TABLE t_reconciliation_ledger
            ADD CONSTRAINT ck_recon_human_decision CHECK (
                (
                    human_decision IS NULL
                    AND human_decision_at IS NULL
                    AND recon_state NOT IN ('HITL_APPROVED', 'HITL_REJECTED')
                )
                OR (
                    human_decision IN ('APPROVED', 'REJECTED')
                    AND human_decision_at IS NOT NULL
                    AND recon_state = (
                        CASE human_decision
                            WHEN 'APPROVED' THEN 'HITL_APPROVED'
                            ELSE 'HITL_REJECTED'
                        END
                    )
                )
            );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_recon_confidence_range') THEN
        ALTER TABLE t_reconciliation_ledger
            ADD CONSTRAINT ck_recon_confidence_range CHECK (
                ai_confidence_score IS NULL
                OR (ai_confidence_score >= 0 AND ai_confidence_score <= 1)
            );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5. Batch provenance: which reconciliation run produced this row
-- ---------------------------------------------------------------------------
-- Without this, the only way to scope a benchmark to one run was
-- `settlement_id LIKE '%<run_id>%'` -- string surgery on a business key, which
-- silently matches unrelated rows. Metrics are now aggregated from an indexed
-- column that actually means "this run".
ALTER TABLE t_reconciliation_ledger
    ADD COLUMN IF NOT EXISTS batch_run_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_recon_batch_run
    ON t_reconciliation_ledger (batch_run_id)
    WHERE batch_run_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. t_razorpay_settlements: enforce the accounting equation in the database
-- ---------------------------------------------------------------------------
-- Net = Gross - Fees - Taxes - Refunds - Adjustments is the definition the
-- rules engine computes. Enforcing it here means no code path -- ORM, raw SQL,
-- or a future importer -- can persist a settlement whose own numbers disagree.
-- Verified against all pre-existing rows before being added.
DO $$
DECLARE
    bad_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO bad_rows
      FROM t_razorpay_settlements
     WHERE net_settlement <> gross_amount - fees - taxes - refunds - adjustments;

    IF bad_rows > 0 THEN
        RAISE EXCEPTION
            'Cannot add ck_settlement_accounting_equation: % existing rows violate '
            'Net = Gross - Fees - Taxes - Refunds - Adjustments.', bad_rows;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_settlement_accounting_equation'
    ) THEN
        ALTER TABLE t_razorpay_settlements
            ADD CONSTRAINT ck_settlement_accounting_equation CHECK (
                net_settlement = gross_amount - fees - taxes - refunds - adjustments
            );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_settlement_status_nonempty') THEN
        ALTER TABLE t_razorpay_settlements
            ADD CONSTRAINT ck_settlement_status_nonempty CHECK (length(trim(status)) > 0);
    END IF;
END $$;

