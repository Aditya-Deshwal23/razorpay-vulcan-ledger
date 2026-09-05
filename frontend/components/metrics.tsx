import { ArrowRight, Bot, CheckCircle2, CircleDot, ClipboardCheck, Fingerprint, ShieldAlert, ShieldCheck } from "lucide-react";
import { memo, useMemo } from "react";
import Link from "next/link";

import { Money } from "@/components/primitives";
import { absoluteMoney, exceptionTitle } from "@/lib/format";
import type { BatchSummary, ReviewItem } from "@/types/api";

export const ReconciliationRail = memo(function ReconciliationRail({ summary }: { summary: BatchSummary }) {
  const parts = [
    { key: "deterministic_match", label: "Rules matched", count: summary.state_counts.deterministic_match, tone: "matched" },
    { key: "ai_resolved", label: "AI-resolved", count: summary.state_counts.ai_resolved, tone: "ai" },
    { key: "pending_hitl_review", label: "Needs review", count: summary.state_counts.pending_hitl_review, tone: "review" },
    { key: "human", label: "Human resolved", count: summary.human_resolved, tone: "approved" },
  ];
  return (
    <section className="rail-block" aria-label={`Reconciliation state distribution for ${summary.total} settlements`}>
      <div className="rail-track">
        {parts.filter((part) => part.count).map((part) => (
          <span
            className={`rail-segment rail-${part.tone}`}
            style={{ width: `${(part.count / summary.total) * 100}%` }}
            key={part.key}
            title={`${part.label}: ${part.count}`}
          />
        ))}
      </div>
      <div className="rail-legend">
        {parts.map((part) => (
          <span key={part.key}><i className={`legend-dot dot-${part.tone}`} />{part.count} {part.label}</span>
        ))}
      </div>
    </section>
  );
}, (previous, next) => (
  previous.summary.total === next.summary.total
  && previous.summary.human_resolved === next.summary.human_resolved
  && previous.summary.match_rate === next.summary.match_rate
  && previous.summary.state_counts.deterministic_match === next.summary.state_counts.deterministic_match
  && previous.summary.state_counts.ai_resolved === next.summary.state_counts.ai_resolved
  && previous.summary.state_counts.pending_hitl_review === next.summary.state_counts.pending_hitl_review
));

export const OverviewMetrics = memo(function OverviewMetrics({ summary, priority }: { summary: BatchSummary; priority?: ReviewItem }) {
  const reviewHref = useMemo(
    () => priority
      ? `/review?batch=${encodeURIComponent(summary.batch_run_id)}&settlement=${encodeURIComponent(priority.settlement_id)}`
      : `/review?batch=${encodeURIComponent(summary.batch_run_id)}`,
    [priority, summary.batch_run_id],
  );
  return (
    <>
      <section className="overview-verdict">
        <div className="match-verdict surface grain">
          <p className="eyebrow">Automated reconciliation</p>
          <div className="verdict-number">{summary.match_rate}<span>%</span></div>
          <p className="verdict-copy">{summary.auto_reconciled} of {summary.total} settlements cleared without a controller decision.</p>
          <ReconciliationRail summary={summary} />
        </div>
        <div className={`needs-you surface grain ${summary.needs_review ? "needs-you-active" : ""}`}>
          <div>
            <p className="eyebrow">Controller attention</p>
            <div className="needs-number">{summary.needs_review}</div>
            <p>{summary.needs_review === 1 ? "Settlement needs your judgment." : "Settlements need your judgment."}</p>
            {priority ? <p className="needs-priority"><span>Priority</span>{exceptionTitle(priority)} · <Money value={priority.deterministic_variance} /></p> : null}
          </div>
          {summary.needs_review ? (
            <Link href={reviewHref} className="primary-link"><ClipboardCheck size={17} aria-hidden="true" />Review queue <ArrowRight size={16} aria-hidden="true" /></Link>
          ) : (
            <span className="queue-clear"><ShieldCheck size={18} aria-hidden="true" />Queue clear</span>
          )}
        </div>
      </section>
      <section className="metrics-strip" aria-label="Track 04 metrics">
        <article><CircleDot size={17} aria-hidden="true" /><span><strong>{summary.total}</strong> total uploaded entries</span></article>
        <article><ShieldCheck size={17} aria-hidden="true" /><span><strong>{summary.match_rate}%</strong> successfully verified</span></article>
        <article><ClipboardCheck size={17} aria-hidden="true" /><span><strong>{summary.needs_review}</strong> exceptions needing review</span></article>
      </section>
    </>
  );
}, (previous, next) => (
  previous.summary.batch_run_id === next.summary.batch_run_id
  && previous.summary.total === next.summary.total
  && previous.summary.auto_reconciled === next.summary.auto_reconciled
  && previous.summary.needs_review === next.summary.needs_review
  && previous.summary.match_rate === next.summary.match_rate
  && previous.priority?.settlement_id === next.priority?.settlement_id
  && previous.priority?.deterministic_variance === next.priority?.deterministic_variance
));

export function VarianceComparison({ deterministic, reported }: { deterministic: string; reported: string | null }) {
  const aligned = reported === deterministic;
  return (
    <section className="variance-comparison" aria-label="Deterministic and AI-reported variance comparison">
      <div>
        <p>Ledger variance</p>
        <Money value={deterministic} />
        <small>Computed by deterministic rules</small>
      </div>
      <div className={aligned ? "variance-aligned" : "variance-diverged"}>
        <p>AI-reported variance</p>
        <Money value={reported} />
        <small>{reported === null ? "No model amount recorded" : aligned ? "Agrees with ledger math" : "Does not agree with ledger math"}</small>
      </div>
    </section>
  );
}

export function EvidenceBoundary({ item }: { item: ReviewItem }) {
  const arithmeticAgrees = absoluteMoney(item.deterministic_variance) === "0.00";
  const identityVerified = Boolean(item.settlement_utr && item.bank_utr && item.settlement_utr === item.bank_utr);
  const modelCopy = item.ai_reported_variance
    ? `Variance ₹${item.ai_reported_variance}${item.confidence_score === null ? "" : ` · ${Math.round(item.confidence_score * 100)}% confidence`}`
    : "No model amount recorded";
  return (
    <section className="evidence-boundary" aria-label="Reconciliation proof chain">
      <div className="boundary-heading"><p className="eyebrow">Reconciliation proof chain</p><span>Each layer has a different authority.</span></div>
      <ol>
        <li className="boundary-source"><CheckCircle2 size={16} aria-hidden="true" /><div><strong>Source records</strong><small>Settlement and bank evidence retained</small></div></li>
        <li className={arithmeticAgrees ? "boundary-pass" : "boundary-alert"}><CheckCircle2 size={16} aria-hidden="true" /><div><strong>{arithmeticAgrees ? "Arithmetic agrees" : "Arithmetic diverges"}</strong><small>{arithmeticAgrees ? "Expected net equals bank credit" : `Rules variance ₹${item.deterministic_variance}`}</small></div></li>
        <li className={item.ai_reported_variance ? "boundary-ai" : "boundary-muted"}><Bot size={16} aria-hidden="true" /><div><strong>Model signal</strong><small>{modelCopy}</small></div></li>
        <li className={identityVerified ? "boundary-pass" : "boundary-alert"}><ShieldAlert size={16} aria-hidden="true" /><div><strong>{identityVerified ? "Identity corroborated" : "Identity not proven"}</strong><small>{identityVerified ? "Settlement and bank UTR agree" : "No matching transfer reference"}</small></div></li>
        <li className="boundary-human"><ClipboardCheck size={16} aria-hidden="true" /><div><strong>Human authority</strong><small>Decision required before final state</small></div></li>
        <li className="boundary-audit"><Fingerprint size={16} aria-hidden="true" /><div><strong>State fingerprint</strong><small>Current evidence is hash-bound</small></div></li>
      </ol>
    </section>
  );
}
