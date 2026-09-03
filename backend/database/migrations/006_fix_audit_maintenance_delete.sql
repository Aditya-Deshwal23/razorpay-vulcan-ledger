-- 006_fix_audit_maintenance_delete.sql
--
-- PostgreSQL requires a BEFORE DELETE trigger to return OLD to perform the
-- deletion, while BEFORE UPDATE must return NEW to permit the replacement row.
-- Keep controlled test/maintenance cleanup possible without allowing ordinary
-- event mutation.

CREATE OR REPLACE FUNCTION reject_reconciliation_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('vulcan.allow_audit_maintenance', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION
        't_reconciliation_events is append-only; create a corrective event instead of % '
        '(controlled maintenance must SET LOCAL vulcan.allow_audit_maintenance = on)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;
