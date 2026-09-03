"use client";

import { ArrowRight, ChevronLeft, ChevronRight, Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { HashFingerprint, InlineError, Money, StateBadge, SurfaceSkeleton } from "@/components/primitives";
import { VarianceComparison } from "@/components/metrics";
import { api } from "@/lib/api";
import { formatDate, sumMoney } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { ReconState, SettlementListItem } from "@/types/api";

const states: Array<{ value: ReconState | ""; label: string }> = [
  { value: "", label: "All reconciliation states" },
  { value: "DETERMINISTIC_MATCH", label: "Matched by rules" },
  { value: "AI_RESOLVED", label: "AI-resolved" },
  { value: "PENDING_HITL_REVIEW", label: "Needs review" },
  { value: "HITL_APPROVED", label: "Approved" },
  { value: "HITL_REJECTED", label: "Rejected" },
];

export default function LedgerPage() {
  const params = useSearchParams();
  const batchRunId = params.get("batch") ?? undefined;
  const initialQuery = params.get("query") ?? "";
  const requestedState = (params.get("state") ?? "") as ReconState | "";
  const [query, setQuery] = useState(initialQuery);
  const [debouncedQuery, setDebouncedQuery] = useState(initialQuery);
  const [reconState, setReconState] = useState<ReconState | "">(requestedState);
  const [bankName, setBankName] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<SettlementListItem | null>(null);

  useEffect(() => { const timer = window.setTimeout(() => { setDebouncedQuery(query); setPage(1); }, 260); return () => window.clearTimeout(timer); }, [query]);
  useEffect(() => { setReconState(requestedState); setPage(1); }, [requestedState]);
  const filters = useMemo(() => ({ batchRunId, reconState: reconState || undefined, bankName: bankName || undefined, query: debouncedQuery || undefined, page, pageSize: 20 }), [batchRunId, reconState, bankName, debouncedQuery, page]);
  const results = useResource(() => api.settlements(filters), [filters.batchRunId, filters.reconState, filters.bankName, filters.query, filters.page]);
  const detail = useResource(() => selected ? api.settlement(selected.settlement_id) : Promise.resolve(null), [selected?.settlement_id]);

  const totalPages = results.data ? Math.max(1, Math.ceil(results.data.total / results.data.page_size)) : 1;
  return <div className="page-wrap ledger-page"><header className="page-heading"><div><p className="eyebrow">Settlement explorer</p><h1>Trace every settlement back to its evidence.</h1><p className="page-intro">Search by settlement ID, UTR, or exact money amount. Filters operate on the live ledger.</p></div>{batchRunId ? <span className="batch-stamp"><span>Scoped run</span><strong>{batchRunId}</strong></span> : null}</header>
    <section className="ledger-tools surface"><label className="search-field"><Search size={17} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Settlement ID, UTR, or amount" aria-label="Search ledger" />{query ? <button type="button" className="icon-button" onClick={() => setQuery("")} aria-label="Clear search"><X size={15} /></button> : null}</label><label><span><SlidersHorizontal size={15} />State</span><select value={reconState} onChange={(event) => { setReconState(event.target.value as ReconState | ""); setPage(1); }}>{states.map((state) => <option key={state.label} value={state.value}>{state.label}</option>)}</select></label><label><span>Bank</span><input value={bankName} onChange={(event) => { setBankName(event.target.value.toUpperCase()); setPage(1); }} placeholder="e.g. HDFC" /></label></section>
    <section className={`ledger-layout ${selected ? "ledger-detail-open" : ""}`}><article className="surface ledger-table-panel">{results.loading ? <SurfaceSkeleton lines={10} /> : results.error ? <InlineError error={results.error} retry={results.refresh} /> : results.data && results.data.items.length ? <><div className="ledger-summary"><span>{results.data.total} matching settlement{results.data.total === 1 ? "" : "s"}</span><small>Page {results.data.page} of {totalPages}</small></div><div className="ledger-table-wrap"><table className="ledger-table"><thead><tr><th>Settlement</th><th>State</th><th>Expected net</th><th>Bank credit</th><th>Variance</th><th>Bank</th><th>Recorded</th></tr></thead><tbody>{results.data.items.map((item) => <tr key={item.settlement_id} className={selected?.settlement_id === item.settlement_id ? "ledger-row-selected" : ""} onClick={() => setSelected(item)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelected(item); } }}><td><strong>{item.settlement_id}</strong><small>{item.settlement_utr ?? "No settlement UTR"}</small></td><td><StateBadge state={item.recon_state} /></td><td><Money value={item.expected_net} /></td><td><Money value={item.bank_credit} /></td><td><Money value={item.deterministic_variance} /></td><td>{item.bank_name ?? "—"}</td><td className="muted">{formatDate(item.resolved_at)}</td></tr>)}</tbody></table></div><div className="pagination"><button type="button" className="secondary-button" disabled={page === 1} onClick={() => setPage((value) => value - 1)}><ChevronLeft size={16} />Previous</button><span>Showing {results.data.items.length} records</span><button type="button" className="secondary-button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>Next<ChevronRight size={16} /></button></div></> : <div className="table-empty"><Search size={28} /><h2>No settlements match these filters</h2><p>Try removing a filter or search by the exact settlement ID, UTR, or amount.</p></div>}</article>{selected ? <SettlementDrawer item={selected} detail={detail.data} loading={detail.loading} error={detail.error} onClose={() => setSelected(null)} retry={detail.refresh} /> : null}</section>
  </div>;
}

function SettlementDrawer({ item, detail, loading, error, onClose, retry }: { item: SettlementListItem; detail: Awaited<ReturnType<typeof api.settlement>> | null; loading: boolean; error: unknown; onClose: () => void; retry: () => void }) {
  return <aside className="settlement-drawer surface grain"><button type="button" className="drawer-close icon-button" onClick={onClose} aria-label="Close settlement detail"><X size={18} /></button>{loading || !detail ? <SurfaceSkeleton lines={12} /> : error ? <InlineError error={error} retry={retry} /> : <><div className="drawer-title"><p className="eyebrow">Settlement evidence</p><h2>{item.settlement_id}</h2><StateBadge state={detail.recon_state} /></div><VarianceComparison deterministic={detail.deterministic_variance} reported={detail.ai_reported_variance} /><div className="drawer-columns"><section><p className="eyebrow">Settlement accounting</p><dl><div><dt>Gross</dt><dd><Money value={detail.gross_amount} /></dd></div><div><dt>Fees + taxes</dt><dd><Money value={sumMoney([detail.fees, detail.taxes])} /></dd></div><div><dt>Refunds + adjustments</dt><dd><Money value={sumMoney([detail.refunds, detail.adjustments])} /></dd></div><div><dt>Expected net</dt><dd><Money value={detail.expected_net} /></dd></div></dl></section><section><p className="eyebrow">Bank record</p><dl><div><dt>Institution</dt><dd>{detail.bank_name ?? "No linked bank record"}</dd></div><div><dt>Credit</dt><dd><Money value={detail.bank_credit} /></dd></div><div><dt>Value date</dt><dd>{formatDate(detail.bank_transaction_date)}</dd></div><div><dt>Bank UTR</dt><dd><code>{detail.bank_utr ?? "Not recoverable"}</code></dd></div></dl></section></div><section className="narration-section"><p className="eyebrow">Evidence narration</p><blockquote>{detail.raw_narration}</blockquote></section><section className="drawer-fingerprint"><p className="eyebrow">Current audit fingerprint</p><HashFingerprint value={detail.cryptographic_state_hash} /></section><a href={`/audit?${new URLSearchParams({ settlement: detail.settlement_id, ...(detail.batch_run_id ? { batch: detail.batch_run_id } : {}) })}`} className="secondary-link">View its immutable events <ArrowRight size={16} /></a></>}</aside>;
}
