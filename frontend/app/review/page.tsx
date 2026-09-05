"use client";

import { AlertTriangle, ArrowRight, Check, ChevronRight, ShieldAlert, X, Zap } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { useSearchParams } from "next/navigation";

import { HashFingerprint, InlineError, Money, StateBadge, SurfaceSkeleton } from "@/components/primitives";
import { EvidenceBoundary, VarianceComparison } from "@/components/metrics";
import { api } from "@/lib/api";
import { exceptionExplanation, exceptionTitle, formatDate, shortId } from "@/lib/format";
import { useBatches, useResource } from "@/lib/hooks";
import type { ReviewItem } from "@/types/api";

type Decision = "APPROVED" | "REJECTED";
type DecisionResult = { settlementId: string; decision: Decision; newlyRecorded: boolean };

export default function ReviewPage() {
  const params = useSearchParams();
  const batches = useBatches();
  const batchRunId = params.get("batch") ?? batches.batches[0]?.batch_run_id;
  const queue = useResource(
    () => batchRunId ? api.review(batchRunId) : Promise.resolve({ items: [] as ReviewItem[], total: 0 }),
    [batchRunId],
  );
  const [selectedId, setSelectedId] = useState<string | null>(() => params.get("settlement"));
  const [intent, setIntent] = useState<Decision | null>(null);
  const [reviewer, setReviewer] = useState("finance.controller");
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [lastDecision, setLastDecision] = useState<DecisionResult | null>(null);

  if (batches.loading && !batchRunId) return <ReviewSkeleton />;
  if (batches.error) return <ReviewFrame><InlineError error={batches.error} retry={batches.refresh} /></ReviewFrame>;
  if (!batchRunId) return <ReviewFrame><InlineError error={new Error("Choose a reconciliation batch before opening the review queue.")} /></ReviewFrame>;
  if (queue.loading || !queue.data) return <ReviewSkeleton />;
  if (queue.error) return <ReviewFrame><InlineError error={queue.error} retry={queue.refresh} /></ReviewFrame>;
  if (!queue.data.items.length) return <QueueClear batchRunId={batchRunId} lastDecision={lastDecision} />;

  const selected = queue.data.items.find((item) => item.settlement_id === selectedId) ?? queue.data.items[0];
  async function confirmDecision() {
    if (!intent || reviewer.trim().length < 2) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const result = await api.decide(selected.settlement_id, intent, reviewer.trim());
      setLastDecision({ settlementId: selected.settlement_id, decision: intent, newlyRecorded: result.newly_recorded });
      setIntent(null);
      queue.refresh();
      batches.refresh();
      window.dispatchEvent(new Event("vulcan:batch-state-changed"));
    } catch (caught) {
      setActionError(caught);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <ReviewFrame>
      <header className="page-heading review-heading">
        <div><p className="eyebrow">Human-in-the-loop review</p><h1>Decide with the evidence in view.</h1><p className="page-intro">{queue.data.total} {queue.data.total === 1 ? "settlement remains" : "settlements remain"} in batch <code>{batchRunId}</code>.</p></div>
        <span className="queue-count"><ShieldAlert size={17} aria-hidden="true" />{queue.data.total} outstanding</span>
      </header>
      {lastDecision ? <section className="decision-success" role="status"><Check size={18} aria-hidden="true" /><span><strong>{lastDecision.decision === "APPROVED" ? "Approved" : "Rejected"}</strong> <code>{lastDecision.settlementId}</code> {lastDecision.newlyRecorded ? "and appended its audit record." : "was already recorded; no duplicate audit event was created."}</span><Link href={`/audit?batch=${encodeURIComponent(batchRunId)}&settlement=${encodeURIComponent(lastDecision.settlementId)}`}>View record <ArrowRight size={15} /></Link></section> : null}
      <section className="review-workspace">
        <aside className="review-queue-panel" aria-label="Review queue">
          <div className="queue-panel-title"><span>Queue</span><small>Oldest first</small></div>
          <div className="queue-items" style={{ overflowY: "auto", maxHeight: "800px" }}>
            {/*
              Virtualization enforcement: Renders a maximum of 50 DOM nodes simultaneously.
              Prevents browser thread exhaustion when processing batches of 500+ records.
            */}
            {queue.data.items.slice(0, 50).map((item, index) => (
              <QueueItem
                item={item}
                index={index}
                selected={item.settlement_id === selected.settlement_id}
                onSelect={() => { setSelectedId(item.settlement_id); setIntent(null); setActionError(null); }}
                key={item.settlement_id}
              />
            ))}
            {queue.data.items.length > 50 && (
              <div style={{ padding: "12px", textAlign: "center", color: "var(--ash)", fontSize: "11px", fontFamily: "var(--font-mono)" }}>
                + {queue.data.items.length - 50} more items pending review...
              </div>
            )}
          </div>
        </aside>
        <article className="review-detail surface grain">
          <ReviewEvidence item={selected} />
          <DecisionPanel intent={intent} reviewer={reviewer} submitting={submitting} error={actionError} onIntent={setIntent} onReviewer={setReviewer} onCancel={() => { setIntent(null); setActionError(null); }} onConfirm={confirmDecision} />
        </article>
      </section>
    </ReviewFrame>
  );
}

function QueueItem({ item, index, selected, onSelect }: { item: ReviewItem; index: number; selected: boolean; onSelect: () => void }) {
  return <button type="button" className={`queue-item ${selected ? "queue-item-selected" : ""}`} onClick={onSelect} aria-current={selected ? "true" : undefined}><span className="queue-index">{String(index + 1).padStart(2, "0")}</span><span className="queue-item-main"><strong className="queue-business-title">{exceptionTitle(item)}</strong><code title={item.settlement_id}>{shortId(item.settlement_id)}</code><small>{item.discrepancy_reason?.replaceAll("_", " ") ?? "Unclassified exception"}</small></span><span className="queue-item-money"><Money value={item.deterministic_variance} /><ChevronRight size={15} aria-hidden="true" /></span></button>;
}

function ReviewEvidence({ item }: { item: ReviewItem }) {
  const confidence = item.confidence_score === null ? null : Math.round(item.confidence_score * 100);
  return <>
    <div className="detail-kicker"><StateBadge state={item.recon_state} /><span>Opened {formatDate(item.created_at, true)}</span></div>
    <div className="detail-title-row"><div><p className="eyebrow">Settlement under review</p><h2>{exceptionTitle(item)}</h2><code className="detail-settlement-id" title={item.settlement_id}>{shortId(item.settlement_id)}</code></div>{confidence !== null ? <div className="confidence"><span>Classification confidence</span><div><i style={{ width: `${confidence}%` }} /><em>{confidence}%</em></div></div> : null}</div>
    <section className="reason-callout"><AlertTriangle size={19} aria-hidden="true" /><div><p>Why controller judgment is required</p><strong>{item.discrepancy_reason?.replaceAll("_", " ") ?? "No classification recorded"}</strong><small>{exceptionExplanation(item)}</small></div></section>
    <VarianceComparison deterministic={item.deterministic_variance} reported={item.ai_reported_variance} />
    {item.rca_reason ? (
      <section className="rca-badge" aria-label="AI root cause analysis">
        <Zap size={15} aria-hidden="true" />
        <div>
          <p className="eyebrow">AI root cause analysis</p>
          <strong>{item.rca_reason}</strong>
        </div>
      </section>
    ) : null}
    <section className="evidence-braid" aria-label="Settlement, bank credit, and controller evidence">
      <div className="braid-node"><span>Razorpay expected net</span><Money value={item.expected_net} /><small>Settlement UTR · {item.settlement_utr ?? "not supplied"}</small></div>
      <div className="braid-join" aria-hidden="true" /><div className="braid-node"><span>Bank credit</span><Money value={item.bank_credit} /><small>{item.bank_name ?? "No bank entry"} · {item.bank_utr ?? "UTR not recoverable"}</small></div>
      <div className="braid-join" aria-hidden="true" /><div className="braid-node braid-node-decision"><span>Controller boundary</span><strong>Human decision required</strong><small>Rules and model output stay separated</small></div>
    </section>
    <EvidenceBoundary item={item} />
    <section className="evidence-grid"><div><p className="eyebrow">Bank evidence</p><dl><div><dt>Institution</dt><dd>{item.bank_name ?? "No bank entry linked"}</dd></div><div><dt>Value date</dt><dd>{formatDate(item.bank_transaction_date)}</dd></div><div><dt>Bank UTR</dt><dd><code>{item.bank_utr ?? "Not recoverable"}</code></dd></div></dl></div><div><p className="eyebrow">Original narration</p><blockquote>{item.raw_narration}</blockquote></div></section>
    <section className="fingerprint-row"><div><p className="eyebrow">Current audit fingerprint</p><HashFingerprint value={item.cryptographic_state_hash} /></div><p>This fingerprint covers the current state, ledger variance, and preserved bank narration.</p></section>
  </>;
}

function DecisionPanel({ intent, reviewer, submitting, error, onIntent, onReviewer, onCancel, onConfirm }: { intent: Decision | null; reviewer: string; submitting: boolean; error: unknown; onIntent: (value: Decision) => void; onReviewer: (value: string) => void; onCancel: () => void; onConfirm: () => void }) {
  if (intent) return <section className={`decision-confirm decision-${intent.toLowerCase()}`}><div><p className="eyebrow">Confirm {intent === "APPROVED" ? "approval" : "rejection"}</p><h3>{intent === "APPROVED" ? "Record an approved adjustment?" : "Record a rejected adjustment?"}</h3><p>The decision will be attributed to the named reviewer and added to the immutable audit trail.</p><small className="state-preview">Resulting state: {intent === "APPROVED" ? "HITL_APPROVED" : "HITL_REJECTED"}</small></div><label>Reviewer identity<input value={reviewer} onChange={(event) => onReviewer(event.target.value)} disabled={submitting} aria-invalid={reviewer.trim().length < 2} /></label>{error ? <InlineError error={error} /> : null}<div className="confirm-actions"><button type="button" className="secondary-button" onClick={onCancel} disabled={submitting}>Cancel</button><button type="button" className={`decision-button ${intent === "APPROVED" ? "approve-button" : "reject-button"}`} onClick={onConfirm} disabled={submitting || reviewer.trim().length < 2}>{submitting ? "Confirming…" : `Confirm ${intent.toLowerCase()}`}</button></div></section>;
  return <section className="decision-panel"><div><p className="eyebrow">Controller decision</p><h3>Make a deliberate call.</h3><p>Both actions write a named, immutable record. Neither choice is preselected.</p></div><div className="decision-actions"><button type="button" className="decision-button reject-button" onClick={() => onIntent("REJECTED")}><X size={17} aria-hidden="true" />Reject</button><button type="button" className="decision-button approve-button" onClick={() => onIntent("APPROVED")}><Check size={17} aria-hidden="true" />Approve</button></div></section>;
}

function QueueClear({ batchRunId, lastDecision }: { batchRunId: string; lastDecision: DecisionResult | null }) { return <ReviewFrame><section className="queue-clear-screen surface grain"><Check size={32} aria-hidden="true" /><p className="eyebrow">Review queue</p><h1>Queue clear.</h1><p>There are no pending controller decisions in batch <code>{batchRunId}</code>.</p>{lastDecision ? <Link href={`/audit?batch=${encodeURIComponent(batchRunId)}&settlement=${encodeURIComponent(lastDecision.settlementId)}`} className="primary-link">Verify latest decision <ArrowRight size={16} /></Link> : <Link href={`/?batch=${encodeURIComponent(batchRunId)}`} className="secondary-link">Return to overview <ArrowRight size={16} /></Link>}</section></ReviewFrame>; }
function ReviewFrame({ children }: { children: React.ReactNode }) { return <div className="page-wrap review-page">{children}</div>; }
function ReviewSkeleton() { return <ReviewFrame><div className="skeleton-heading"><span /><span /></div><section className="review-workspace"><SurfaceSkeleton lines={6} /><SurfaceSkeleton lines={14} /></section></ReviewFrame>; }
