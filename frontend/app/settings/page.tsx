"use client";

import { CheckCircle2, Database, Radio, ShieldCheck } from "lucide-react";

import { InlineError, SurfaceSkeleton } from "@/components/primitives";
import { api } from "@/lib/api";
import { useResource } from "@/lib/hooks";

export default function SettingsPage() {
  const health = useResource(() => api.health(), []);
  return <div className="page-wrap settings-page"><header className="page-heading"><div><p className="eyebrow">System status</p><h1>Operational foundations.</h1><p className="page-intro">This surface intentionally reports health and configuration, not financial controls.</p></div></header>{health.loading ? <SurfaceSkeleton lines={4} /> : health.error ? <InlineError error={health.error} retry={health.refresh} /> : health.data ? <section className="settings-grid"><article className="surface setting-card"><Database size={20} /><div><p className="eyebrow">Ledger database</p><h2><CheckCircle2 size={18} /> Connected</h2><p>The PostgreSQL reconciliation ledger is reachable through the operations API.</p></div></article><article className="surface setting-card"><Radio size={20} /><div><p className="eyebrow">Classification runtime</p><h2><code>{health.data.gemini_model}</code></h2><p>Only exceptions reach the model. Settlement arithmetic remains deterministic.</p></div></article><article className="surface setting-card setting-wide"><ShieldCheck size={20} /><div><p className="eyebrow">Review integrity</p><h2>Named decisions, immutable events.</h2><p>Human approvals and rejections are guarded against conflicting retries and written as append-only reconciliation events with state fingerprints.</p></div></article></section> : null}</div>;
}
