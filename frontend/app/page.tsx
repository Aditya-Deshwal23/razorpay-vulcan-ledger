"use client";

import { ArrowRight, BookOpenText, ClipboardCheck, Fingerprint, Landmark, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { AuditTable } from "@/components/audit-table";
import { BatchUploader } from "@/components/batch-uploader";
import { EmptyState, InlineError, Money, SurfaceSkeleton } from "@/components/primitives";
import { OverviewMetrics } from "@/components/metrics";
import { api } from "@/lib/api";
import { compareAbsoluteMoney, exceptionTitle, formatDate, sumAbsoluteMoney } from "@/lib/format";
import { useBatches, useResource } from "@/lib/hooks";
import type { ApiError, ReviewItem } from "@/types/api";

export default function OverviewPage() {
  const params = useSearchParams();
  const router = useRouter();
  const batchResource = useBatches();
  const [acceptedUpload, setAcceptedUpload] = useState<{ batchId: string; records: number } | null>(null);
  const batchRunId = params.get("batch") ?? batchResource.batches[0]?.batch_run_id;
  const summaryResource = useResource(
    () => {
      if (!batchRunId) return Promise.resolve(null);
      return api.summary(batchRunId).catch((error: ApiError) => {
        if (error.status === 404) return null;
        throw error;
      });
    },
    [batchRunId],
  );
  const auditResource = useResource(
    () => batchRunId ? api.audit({ batchRunId, limit: 5 }) : Promise.resolve({ items: [] } as any),
    [batchRunId],
  );
  const reviewResource = useResource(
    () => batchRunId ? api.review(batchRunId) : Promise.resolve({ items: [], total: 0 }),
    [batchRunId],
  );

  function bindAcceptedBatch(batchId: string, _records: number) {
    setAcceptedUpload({ batchId, records: _records });
    router.push(`/?batch=${encodeURIComponent(batchId)}`);
    batchResource.refresh();
    window.dispatchEvent(new Event("vulcan:batch-state-changed"));
  }

  useEffect(() => {
    if (!batchRunId) return;
    const started = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - started > 180000) {
        window.clearInterval(timer);
        return;
      }
      batchResource.refresh();
      summaryResource.refresh();
      auditResource.refresh();
      reviewResource.refresh();
    }, 2000);
    return () => window.clearInterval(timer);
    // Refresh handles are stable enough for a batch-scoped poller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchRunId]);

  const processingProgress = acceptedUpload?.batchId === batchRunId && acceptedUpload.records > 0 && summaryResource.data
    ? Math.min(100, Math.round((summaryResource.data.total / acceptedUpload.records) * 100))
    : 0;
  const uploader = <div className="overview-uploader"><BatchUploader onSuccess={bindAcceptedBatch} progress={processingProgress} /></div>;

  if (batchResource.loading && !batchRunId) return <OverviewSkeleton />;
  if (batchResource.error) return <PageFrame><InlineError error={batchResource.error} retry={batchResource.refresh} /></PageFrame>;
  if (!batchRunId) return (
    <PageFrame>
      <header className="page-heading page-heading-overview">
        <div>
          <p className="eyebrow">Reconciliation control tower</p>
          <h1>Money, accounted for.</h1>
          <p className="page-intro">Upload a Razorpay settlement CSV to start your first reconciliation run.</p>
        </div>
      </header>
      {uploader}
    </PageFrame>
  );
  if (summaryResource.error) return <PageFrame><InlineError error={summaryResource.error} retry={summaryResource.refresh} /></PageFrame>;
  if (summaryResource.loading || !summaryResource.data) {
    return (
      <PageFrame>
        <header className="page-heading page-heading-overview">
          <div>
            <p className="eyebrow">Reconciliation control tower</p>
            <h1>Money, accounted for.</h1>
            <p className="page-intro">Processing batch <code>{batchRunId}</code> — waiting for ledger writes.</p>
          </div>
        </header>
        {uploader}
        <OverviewSkeleton />
      </PageFrame>
    );
  }

  const summary = summaryResource.data;
  const reviewItems = reviewResource.data?.items ?? [];
  const priority = useMemo(
    () => [...reviewItems].sort((left, right) => compareAbsoluteMoney(right.deterministic_variance, left.deterministic_variance))[0],
    [reviewItems],
  );
  return (
    <PageFrame>
      <header className="page-heading page-heading-overview">
        <div>
          <p className="eyebrow">Reconciliation control tower</p>
          <h1>Money, accounted for.</h1>
          <p className="page-intro">Batch <code>{summary.batch_run_id}</code> · last ledger activity {formatDate(summary.last_activity_at, true)}</p>
        </div>
      </header>

      {uploader}

      <OverviewMetrics summary={summary} priority={priority} />

      {reviewResource.loading ? <div className="overview-lower"><SurfaceSkeleton lines={5} /><SurfaceSkeleton lines={5} /></div> : reviewResource.error ? <InlineError error={reviewResource.error} retry={reviewResource.refresh} /> : <OperationalBrief items={reviewResource.data?.items ?? []} batchRunId={batchRunId} />}

      <section className="section-block">
        <div className="section-heading"><div><p className="eyebrow">Immutable activity</p><h2>Latest recorded evidence</h2></div><Link href={`/audit?batch=${encodeURIComponent(batchRunId)}`} className="text-link">Open audit trail <ArrowRight size={15} aria-hidden="true" /></Link></div>
        {auditResource.loading ? <SurfaceSkeleton lines={5} /> : auditResource.error ? <InlineError error={auditResource.error} retry={auditResource.refresh} /> : auditResource.data?.items.length ? <AuditTable events={auditResource.data.items} /> : <EmptyState title="No audit events for this run" body="Events appear when reconciliations or controller decisions are recorded." />}
      </section>
    </PageFrame>
  );
}

function OperationalBrief({ items, batchRunId }: { items: ReviewItem[]; batchRunId: string }) {
  const priority = [...items].sort((left, right) => compareAbsoluteMoney(right.deterministic_variance, left.deterministic_variance))[0];
  const openVariance = sumAbsoluteMoney(items.map((item) => item.deterministic_variance));
  const identityGaps = items.filter((item) => !item.settlement_utr || !item.bank_utr).length;
  return <section className="overview-lower operational-brief">
    <article className="surface evidence-note priority-card">
      <div className="section-label"><ShieldAlert size={17} aria-hidden="true" /><span>Review priority</span></div>
      {priority ? <><h2>{exceptionTitle(priority)}</h2><p><code>{priority.settlement_id}</code> · <Money value={priority.deterministic_variance} /> open deterministic variance</p><Link href={`/review?batch=${encodeURIComponent(batchRunId)}&settlement=${encodeURIComponent(priority.settlement_id)}`} className="secondary-link">Open the evidence <ArrowRight size={16} aria-hidden="true" /></Link></> : <><h2>No controller action is waiting.</h2><p>This run has no pending human decisions.</p><Link href={`/audit?batch=${encodeURIComponent(batchRunId)}`} className="secondary-link">Inspect the immutable record <Fingerprint size={16} aria-hidden="true" /></Link></>}
    </article>
    <article className="surface next-step live-exposure-card">
      <div className="section-label"><Landmark size={17} aria-hidden="true" /><span>Live exception exposure</span></div>
      <div className="exposure-number"><Money value={openVariance} /></div>
      <p>Absolute open deterministic variance across the active review queue.</p>
      <div className="exposure-detail"><strong>{identityGaps}</strong><span>{identityGaps === 1 ? "exception lacks" : "exceptions lack"} a verifiable transfer reference.</span></div>
      <Link href={`/ledger?batch=${encodeURIComponent(batchRunId)}&state=PENDING_HITL_REVIEW`} className="secondary-link">Inspect source records <BookOpenText size={16} aria-hidden="true" /></Link>
    </article>
  </section>;
}

function PageFrame({ children }: { children: React.ReactNode }) {
  return <div className="page-wrap">{children}</div>;
}

function OverviewSkeleton() {
  return <PageFrame><div className="skeleton-heading"><span /><span /></div><div className="overview-verdict"><SurfaceSkeleton lines={5} /><SurfaceSkeleton lines={5} /></div><SurfaceSkeleton lines={4} /></PageFrame>;
}
