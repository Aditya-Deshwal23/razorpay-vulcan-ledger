"use client";

import {
  BookOpenText,
  ChevronDown,
  ClipboardCheck,
  Download,
  Fingerprint,
  FolderClock,
  LayoutDashboard,
  Loader2,
  Menu,
  Settings,
  Trash2,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { shortId } from "@/lib/format";
import type { BatchSummary } from "@/types/api";

const navItems = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/review", label: "Review", icon: ClipboardCheck },
  { href: "/batches", label: "Batches", icon: FolderClock },
  { href: "/ledger", label: "Ledger", icon: BookOpenText },
  { href: "/audit", label: "Audit", icon: Fingerprint },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const params = useSearchParams();
  const router = useRouter();
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const [open, setOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [batchError, setBatchError] = useState<string | null>(null);
  const uploadRef = useRef<HTMLInputElement>(null);
  const selectedBatch = params.get("batch") ?? batches[0]?.batch_run_id ?? "";

  const refreshBatches = useCallback(async () => {
    try {
      const response = await api.batches();
      setBatches(response.items);
      setBatchError(null);
    } catch (caught) {
      setBatchError(caught instanceof Error ? caught.message : "Unable to load reconciliation runs.");
    }
  }, []);

  useEffect(() => {
    void refreshBatches();
    window.addEventListener("vulcan:batch-state-changed", refreshBatches);
    return () => window.removeEventListener("vulcan:batch-state-changed", refreshBatches);
  }, [refreshBatches]);

  useEffect(() => {
    if (!selectedBatch) return;
    if (batches.some((batch) => batch.batch_run_id === selectedBatch)) return;
    const timer = window.setInterval(() => { void refreshBatches(); }, 2000);
    return () => window.clearInterval(timer);
  }, [selectedBatch, batches, refreshBatches]);

  function withBatch(href: string) {
    return selectedBatch && href !== "/batches" ? `${href}?batch=${encodeURIComponent(selectedBatch)}` : href;
  }

  function selectBatch(batchRunId: string) {
    const route = pathname === "/batches" ? "/" : pathname;
    router.push(`${route}?batch=${encodeURIComponent(batchRunId)}`);
    setOpen(false);
  }

  async function handleExport() {
    if (!selectedBatch || exporting) return;
    setExporting(true);
    try {
      const blob = await api.exportBatch(selectedBatch);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      const originalName = batches.find((batch) => batch.batch_run_id === selectedBatch)?.original_file_name;
      const sourceName = originalName || `${shortId(selectedBatch)}.csv`;
      anchor.download = `audited_${sourceName}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch {
      // Silently ignore — export errors are rare and the user can retry.
    } finally {
      setExporting(false);
    }
  }

  async function handleUpload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const result = await api.upload(file);
      window.dispatchEvent(new Event("vulcan:batch-state-changed"));
      router.push(`/?batch=${encodeURIComponent(result.batch_id)}`);
    } catch {
      // Silently ignore — the BatchUploader on the overview page shows errors.
    } finally {
      if (uploadRef.current) uploadRef.current.value = "";
    }
  }

  async function handleDelete() {
    if (!selectedBatch || deleting) return;
    if (!window.confirm("Delete this batch and all of its audit records?")) return;
    setDeleting(true);
    try {
      await api.deleteBatch(selectedBatch);
      setBatches((current) => current.filter((batch) => batch.batch_run_id !== selectedBatch));
      router.replace("/");
      window.dispatchEvent(new Event("vulcan:batch-state-changed"));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="app-frame">
      <ToastContainer />
      <aside className={`sidebar ${open ? "sidebar-open" : ""}`}>
        <Link href={withBatch("/")} className="wordmark" onClick={() => setOpen(false)}>
          <span className="wordmark-mark" aria-hidden="true">V</span>
          <span><b>VULCAN</b><small>ledger</small></span>
        </Link>
        <nav aria-label="Primary navigation">
          {navItems.map(({ href, label, icon: Icon }) => {
            const isActive = href === "/" ? pathname === "/" : pathname.startsWith(href);
            const pending = label === "Review" ? batches.find((batch) => batch.batch_run_id === selectedBatch)?.needs_review : 0;
            return (
              <Link
                href={withBatch(href)}
                className={`nav-link ${isActive ? "nav-link-active" : ""}`}
                key={href}
                onClick={() => setOpen(false)}
              >
                <Icon size={18} strokeWidth={1.6} aria-hidden="true" />
                <span>{label}</span>
                {pending ? <em>{pending}</em> : null}
              </Link>
            );
          })}
        </nav>
        <Link href="/settings" className={`nav-link nav-settings ${pathname === "/settings" ? "nav-link-active" : ""}`} onClick={() => setOpen(false)}>
          <Settings size={18} strokeWidth={1.6} aria-hidden="true" />
          <span>Settings</span>
        </Link>
      </aside>
      {open ? <button type="button" className="scrim" aria-label="Close navigation" onClick={() => setOpen(false)} /> : null}
      <main className="main-canvas">
        <header className="topbar">
          <button type="button" className="mobile-menu icon-button" aria-label="Open navigation" onClick={() => setOpen(true)}>
            <Menu size={20} aria-hidden="true" />
          </button>
          <div className="topbar-controls">
          <div className="batch-select-wrap">
            <label htmlFor="batch-select">Viewing run</label>
            <div className="select-frame">
              <select id="batch-select" value={selectedBatch} onChange={(event) => selectBatch(event.target.value)} disabled={!selectedBatch && !batches.length}>
                {!batches.length && !selectedBatch ? <option value="">{batchError ? "Runs unavailable" : "Loading runs…"}</option> : null}
                {selectedBatch && !batches.some((batch) => batch.batch_run_id === selectedBatch) ? (
                  <option value={selectedBatch}>{shortId(selectedBatch)} (processing)</option>
                ) : null}
                {batches.map((batch) => <option key={batch.batch_run_id} value={batch.batch_run_id}>{batch.original_file_name ?? shortId(batch.batch_run_id)}</option>)}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
            {batchError ? <button type="button" className="batch-retry" onClick={() => void refreshBatches()}>Retry</button> : null}
          </div>
          <button
            type="button"
            className="topbar-button topbar-button-danger"
            onClick={handleDelete}
            disabled={!selectedBatch || deleting}
            title="Delete the selected batch"
            aria-label="Delete selected batch"
          >
            <Trash2 size={15} aria-hidden="true" />
            <span>{deleting ? "Deleting…" : "Delete Batch"}</span>
          </button>
          <div className="topbar-actions">
            {/* Upload CSV — quick trigger; full UI with drag-and-drop is on the overview page */}
            <input
              ref={uploadRef}
              type="file"
              accept=".csv,.json,text/csv,application/json"
              className="sr-only"
              aria-hidden="true"
              tabIndex={-1}
              onChange={handleUpload}
            />
            <button
              type="button"
              className="topbar-button topbar-button-outlined"
              onClick={() => uploadRef.current?.click()}
              title="Upload a new settlement CSV"
              aria-label="Upload settlement CSV"
            >
              <Upload size={15} aria-hidden="true" />
              <span>Upload CSV</span>
            </button>
            <button
              type="button"
              className="topbar-button topbar-button-outlined"
              onClick={handleExport}
              disabled={!selectedBatch || exporting}
              title={selectedBatch ? `Export audit CSV for ${selectedBatch}` : "Select a batch first"}
              aria-label="Export audit report as CSV"
            >
              {exporting ? (
                <Loader2 size={15} className="spinner" aria-hidden="true" />
              ) : (
                <Download size={15} aria-hidden="true" />
              )}
              <span>{exporting ? "Exporting…" : "Export Audit"}</span>
            </button>
          </div>
          </div>
        </header>
        {children}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToastContainer — intercepts vulcan:toast events fired by apiEventBus and
// renders a Blade-styled non-blocking overlay for network/timeout failures.
// ---------------------------------------------------------------------------
function ToastContainer() {
  const [toast, setToast] = useState<{ title: string; description: string; type: string } | null>(null);

  useEffect(() => {
    const handleToast = (e: Event) => {
      const detail = (e as CustomEvent<{ title: string; description: string; type: string }>).detail;
      setToast(detail);
      setTimeout(() => setToast(null), 6000);
    };
    window.addEventListener("vulcan:toast", handleToast);
    return () => window.removeEventListener("vulcan:toast", handleToast);
  }, []);

  if (!toast) return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      style={{
        position: "fixed",
        bottom: "24px",
        right: "24px",
        zIndex: 9999,
        background:
          toast.type === "error"
            ? "rgba(91, 36, 29, 0.95)"
            : "rgba(48, 91, 78, 0.95)",
        border: `1px solid ${
          toast.type === "error"
            ? "rgba(201, 104, 82, 0.8)"
            : "rgba(107, 178, 157, 0.8)"
        }`,
        backdropFilter: "blur(8px)",
        padding: "16px 20px",
        borderRadius: "var(--radius-panel)",
        color: "var(--parchment)",
        boxShadow: "0 8px 24px rgba(0,0,0,0.6)",
        maxWidth: "340px",
        fontFamily: "var(--font-sans)",
      }}
    >
      <strong style={{ display: "block", marginBottom: "6px", fontSize: "14px" }}>
        {toast.title}
      </strong>
      <span style={{ fontSize: "13px", color: "var(--ash)", lineHeight: 1.4 }}>
        {toast.description}
      </span>
    </div>
  );
}
