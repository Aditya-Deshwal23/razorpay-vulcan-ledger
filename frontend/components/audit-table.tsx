"use client";

import Link from "next/link";

import { HashFingerprint, Money, StateBadge } from "@/components/primitives";
import { formatDate, shortId } from "@/lib/format";
import type { AuditEvent } from "@/types/api";

function eventLabel(event: AuditEvent) {
  if (event.event_type === "HUMAN_DECISION_RECORDED") return `Controller ${event.human_decision?.toLowerCase() ?? "decision"}`;
  if (event.event_type === "RECONCILIATION_RECLASSIFIED") return "Reconciliation reclassified";
  return "Reconciliation recorded";
}

export function AuditTable({ events, showBatch = false }: { events: AuditEvent[]; showBatch?: boolean }) {
  return (
    <div className="audit-table-wrap">
      <table className="audit-table">
        <thead>
          <tr>
            <th>When</th><th>Recorded event</th><th>Settlement</th>{showBatch ? <th>Batch</th> : null}<th>Transition</th><th>Variance</th><th>Fingerprint</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.event_id}>
              <td className="muted date-cell">{formatDate(event.occurred_at, true)}</td>
              <td><strong>{eventLabel(event)}</strong>{event.human_decision_by ? <small>by {event.human_decision_by}</small> : null}</td>
              <td><Link href={`/ledger?query=${encodeURIComponent(event.settlement_id)}`} className="mono-link" title={event.settlement_id}>{shortId(event.settlement_id)}</Link></td>
              {showBatch ? <td><code>{event.original_file_name ?? (event.batch_run_id ? shortId(event.batch_run_id) : "—")}</code></td> : null}
              <td className="transition-cell">{event.from_state ? <StateBadge state={event.from_state} /> : <span className="muted">Created</span>}<span className="transition-arrow">→</span><StateBadge state={event.to_state} /></td>
              <td>{Math.abs(Number(event.variance_amount || "0")) > 0 ? <span className="variance-pill variance-negative"><Money value={event.variance_amount} /> Variance</span> : <span className="variance-pill variance-neutral">Status/Data mismatch</span>}</td>
              <td><HashFingerprint value={event.cryptographic_state_hash} compact /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AuditStateChain({ events }: { events: AuditEvent[] }) {
  const chronological = [...events].reverse();
  return (
    <section className="audit-state-chain" aria-label="Immutable state history">
      <div><p className="eyebrow">State history</p><h2>Recorded transitions, in order.</h2></div>
      <ol>
        {chronological.map((event, index) => (
          <li key={event.event_id}>
            <span className={`chain-node chain-node-${event.to_state.toLowerCase()}`} aria-hidden="true">{index + 1}</span>
            <div><strong>{eventLabel(event)}</strong><span>{event.from_state ? `${event.from_state} → ${event.to_state}` : `Created as ${event.to_state}`}</span><small>{formatDate(event.occurred_at, true)}{event.human_decision_by ? ` · ${event.human_decision_by}` : ""}</small></div>
            <HashFingerprint value={event.cryptographic_state_hash} compact />
          </li>
        ))}
      </ol>
    </section>
  );
}
