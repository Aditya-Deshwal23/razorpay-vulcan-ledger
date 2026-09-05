"use client";

import { ArrowRight, BookOpenText, ClipboardCheck, Fingerprint, Landmark, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo } from "react";

import { AuditTable } from "@/components/audit-table";
import { BatchUploader } from "@/components/batch-uploader";
import { EmptyState, InlineError, Money, SurfaceSkeleton } from "@/components/primitives";
import { OverviewMetrics } from "@/components/metrics";
import { api } from "@/lib/api";
import { compareAbsoluteMoney, exceptionTitle, formatDate, shortId, sumAbsoluteMoney } from "@/lib/format";
import { useBatches, useResource } from "@/lib/hooks";
import type { ApiError, ReviewItem } from "@/types/api";

export default function OverviewPage() {
  const params = useSearchParams();
  const router = useRouter();
  const batchResource = useBatches();
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
  const isBatchProcessing = Boolean(
    batchRunId
    && (!summaryResource.data
      || summaryResource.data.status === "processing"
      || summaryResource.data.resolved_count < (summaryResource.data.total_uploaded_entries ?? 0)),
  );

  function bindAcceptedBatch(batchId: string, _records: number) {
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
    }, isBatchProcessing ? 1000 : 2000);
    return () => window.clearInterval(timer);
    // Refresh handles are stable enough for a batch-scoped poller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchRunId, isBatchProcessing]);

  const reviewItems = useMemo(() => reviewResource.data?.items ?? [], [reviewResource.data?.items]);
  const priority = useMemo(
    () => [...reviewItems].sort((left, right) => compareAbsoluteMoney(right.deterministic_variance, left.deterministic_variance))[0],
    [reviewItems],
  );
  const summary = summaryResource.data ?? {
    batch_run_id: batchRunId ?? "",
    original_file_name: null,
    total_uploaded_entries: null,
    status: "processing",
    resolved_count: 0,
    rule_matched_count: 0,
    exceptions_count: 0,
    last_activity_at: new Date().toISOString(),
    total: 0,
    auto_reconciled: 0,
    needs_review: 0,
    human_resolved: 0,
    match_rate: "0.00",
    state_counts: {
      deterministic_match: 0,
      ai_resolved: 0,
      pending_hitl_review: 0,
      hitl_approved: 0,
      hitl_rejected: 0,
    },
  };
  const totalEntries = summary.total_uploaded_entries ?? summary.total ?? 0;
  const resolvedEntries = Math.min(
    summary.resolved_count ?? summary.total,
    totalEntries || summary.total,
  );
  const processingProgress = totalEntries > 0
    ? Math.min(100, Math.round((resolvedEntries / totalEntries) * 100))
    : 0;
  const isComplete = summary.status === "completed"
    || (totalEntries > 0 && resolvedEntries >= totalEntries);
  const progressActive = Boolean(batchRunId && !isComplete);
  const uploader = <div className="overview-uploader"><BatchUploader onSuccess={bindAcceptedBatch} progress={processingProgress} progressActive={progressActive} /></div>;

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
  return (
    <PageFrame>
      <header className="page-heading page-heading-overview">
        <div>
          <p className="eyebrow">Reconciliation control tower</p>
          <h1>Money, accounted for.</h1>
          <p className="page-intro">{summary.original_file_name ?? (summaryResource.loading ? "Loading batch..." : shortId(summary.batch_run_id))} · {summaryResource.loading ? "processing live" : `last ledger activity ${formatDate(summary.last_activity_at, true)}`}</p>
        </div>
      </header>

      <BatchProgressBanner
        active={Boolean(batchRunId)}
        complete={isComplete}
        progress={processingProgress}
        resolvedEntries={resolvedEntries}
        totalEntries={totalEntries}
      />
      {uploader}

      <OverviewMetrics summary={summary} priority={priority} />

      {reviewResource.error ? <InlineError error={reviewResource.error} retry={reviewResource.refresh} /> : <OperationalBrief items={reviewResource.data?.items ?? []} batchRunId={batchRunId} />}

      <section className="section-block">
        <div className="section-heading"><div><p className="eyebrow">Immutable activity</p><h2>Latest recorded evidence</h2></div><Link href={`/audit?batch=${encodeURIComponent(batchRunId)}`} className="text-link">Open audit trail <ArrowRight size={15} aria-hidden="true" /></Link></div>
        {auditResource.error ? <InlineError error={auditResource.error} retry={auditResource.refresh} /> : auditResource.data?.items.length ? <AuditTable events={auditResource.data.items} /> : <EmptyState title="Processing audit trail" body="Events will appear here as records are reconciled." />}
      </section>
    </PageFrame>
  );
}

function BatchProgressBanner({
  active,
  complete,
  progress,
  resolvedEntries,
  totalEntries,
}: {
  active: boolean;
  complete: boolean;
  progress: number;
  resolvedEntries: number;
  totalEntries: number;
}) {
  if (!active) return null;
  return (
    <section className={`batch-progress-banner ${complete ? "batch-progress-complete" : ""}`} aria-live="polite">
      <div className="batch-progress-status">
        {!complete ? <span className="batch-progress-dot" aria-hidden="true" /> : null}
        <strong>
          {complete
            ? "Reconciliation complete — 100% verified"
            : `Processing batch... (${resolvedEntries} of ${totalEntries} records settled)`}
        </strong>
        <span>{progress}% reconciled</span>
      </div>
      <div className="batch-progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} aria-label="Batch reconciliation progress">
        <span style={{ width: `${progress}%` }} />
      </div>
    </section>
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
