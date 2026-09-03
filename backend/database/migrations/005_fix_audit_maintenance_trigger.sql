-- 005_fix_audit_maintenance_trigger.sql
--
-- 003's append-only trigger correctly rejected ordinary UPDATE/DELETE, but
-- returned OLD in the narrowly allowed maintenance branch. In a BEFORE UPDATE
-- trigger that preserves the previous values, so 004's safe batch-provenance
-- annotation became a no-op. Return NEW only under the explicit transaction-
-- local maintenance flag; normal application writes remain hard failures.

CREATE OR REPLACE FUNCTION reject_reconciliation_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('vulcan.allow_audit_maintenance', true) = 'on' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        't_reconciliation_events is append-only; create a corrective event instead of % '
        '(controlled maintenance must SET LOCAL vulcan.allow_audit_maintenance = on)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

-- Retry the metadata-only backfill now that controlled maintenance can apply
-- its intended value. No event state, actor, fingerprint, or timestamp is
-- changed.
SELECT set_config('vulcan.allow_audit_maintenance', 'on', true);

UPDATE t_reconciliation_events e
SET batch_run_id = r.batch_run_id
FROM t_reconciliation_ledger r
WHERE e.recon_id = r.recon_id
  AND e.batch_run_id IS NULL
  AND r.batch_run_id IS NOT NULL;
