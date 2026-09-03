-- 004_backfill_batch_provenance.sql
--
-- 002_hardening added batch_run_id for every future reconciliation, but the
-- already-persisted synthetic benchmark rows predate that column. Their
-- settlement identifiers carry the generator's stable form
-- `setl_(perfect|edge)_<8-char-run-id>_<index>`, so leaving the column NULL
-- makes truthful historical batches invisible to the operations console.
--
-- Only this exact synthetic format is inferred. Any production-style settlement
-- identifier remains NULL rather than being guessed into a run it never had.

UPDATE t_reconciliation_ledger
SET batch_run_id = (regexp_match(
    settlement_id,
    '^setl_(?:perfect|edge)_([A-Z0-9]{8})_[0-9]+'
))[1]
WHERE batch_run_id IS NULL
  AND settlement_id ~ '^setl_(?:perfect|edge)_[A-Z0-9]{8}_[0-9]+';

-- Event rows are immutable to application writes. This one-time provenance
-- backfill changes no event meaning, state, actor, timestamp, or fingerprint;
-- it only restores the run key that lets historical records be read together.
SELECT set_config('vulcan.allow_audit_maintenance', 'on', true);

UPDATE t_reconciliation_events e
SET batch_run_id = r.batch_run_id
FROM t_reconciliation_ledger r
WHERE e.recon_id = r.recon_id
  AND e.batch_run_id IS NULL
  AND r.batch_run_id IS NOT NULL;
