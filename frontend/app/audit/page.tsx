"use client";

import { Fingerprint, LockKeyhole } from "lucide-react";
import { useSearchParams } from "next/navigation";

import { AuditStateChain, AuditTable } from "@/components/audit-table";
import { EmptyState, InlineError, SurfaceSkeleton } from "@/components/primitives";
import { api } from "@/lib/api";
import { useBatches, useResource } from "@/lib/hooks";

export default function AuditPage() {
  const params = useSearchParams();
  const batches = useBatches();
  const batchRunId = params.get("batch") ?? batches.batches[0]?.batch_run_id;
  const settlementId = params.get("settlement") ?? undefined;
  const audit = useResource(() => batchRunId ? api.audit({ batchRunId, settlementId, limit: 200 }) : Promise.resolve({ items: [] } as any), [batchRunId, settlementId]);
  return <div className="page-wrap audit-page"><header className="page-heading"><div><p className="eyebrow">Append-only reconciliation events</p><h1>An audit trail should be hard to edit and easy to read.</h1><p className="page-intro">Each row is a backend-recorded state snapshot, linked to the exact hash held at that point in the reconciliation lifecycle.</p></div><span className="integrity-stamp"><LockKeyhole size={17} />Append-only</span></header><section className="audit-explainer surface"><Fingerprint size={20} aria-hidden="true" /><p><strong>What you are seeing:</strong> current ledger rows are operational state; this stream is the separate immutable record of each reconciliation and controller decision. A later decision creates a new event, it does not rewrite the preceding one.</p></section>{audit.loading ? <SurfaceSkeleton lines={12} /> : audit.error ? <InlineError error={audit.error} retry={audit.refresh} /> : audit.data?.items.length ? <>{settlementId ? <AuditStateChain events={audit.data.items} /> : null}<AuditTable events={audit.data.items} showBatch /></> : <EmptyState title="No audit event matches this scope" body="Choose a reconciliation batch or open a settlement from the ledger to inspect its recorded events." />}</div>;
}
