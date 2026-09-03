"use client";

import { Check, CircleDot, Copy, OctagonX, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { conciseId, formatMoney, stateMeta } from "@/lib/format";
import type { ReconState } from "@/types/api";

export function Money({ value, className = "" }: { value: string | null | undefined; className?: string }) {
  if (value === null || value === undefined) return <span className={className}>—</span>;
  return (
    <span className={`money ${className}`}>
      <span className="money-symbol">₹</span>
      {formatMoney(value)}
    </span>
  );
}

export function StateBadge({ state }: { state: ReconState }) {
  const meta = stateMeta[state];
  const Icon =
    meta.tone === "matched" || meta.tone === "approved"
      ? Check
      : meta.tone === "ai"
        ? CircleDot
        : OctagonX;
  return (
    <span className={`state-badge state-${meta.tone}`}>
      <Icon aria-hidden="true" size={13} strokeWidth={2} />
      {meta.label}
    </span>
  );
}

export function HashFingerprint({ value, compact = false }: { value: string; compact?: boolean }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }
  return (
    <span className={`hash-fingerprint ${compact ? "hash-compact" : ""}`} title={value}>
      <code>{conciseId(value, compact ? 8 : 12, compact ? 5 : 8)}</code>
      <button type="button" className="icon-button" onClick={copy} aria-label="Copy full audit fingerprint">
        {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      </button>
      <span className="sr-only" aria-live="polite">{copied ? "Audit fingerprint copied" : ""}</span>
    </span>
  );
}

export function InlineError({ error, retry }: { error: unknown; retry?: () => void }) {
  const detail = error instanceof Error ? error.message : "The request could not be completed.";
  return (
    <section className="inline-error" role="alert">
      <OctagonX size={19} aria-hidden="true" />
      <div>
        <strong>Data could not be loaded</strong>
        <p><code>{detail}</code></p>
      </div>
      {retry ? <button type="button" className="text-button" onClick={retry}>Retry</button> : null}
    </section>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <section className="empty-state">
      <ShieldCheck size={28} aria-hidden="true" />
      <div>
        <h2>{title}</h2>
        <p>{body}</p>
      </div>
    </section>
  );
}

export function SurfaceSkeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div className="surface skeleton" aria-label="Loading" role="status">
      {Array.from({ length: lines }, (_, index) => <span key={index} className="skeleton-line" />)}
      <span className="sr-only">Loading data</span>
    </div>
  );
}
