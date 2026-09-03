"use client";

import {
  BookOpenText,
  ChevronDown,
  ClipboardCheck,
  Fingerprint,
  FolderClock,
  LayoutDashboard,
  Menu,
  Settings,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
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
  const selectedBatch = params.get("batch") ?? batches[0]?.batch_run_id ?? "";

  const refreshBatches = useCallback(async () => {
    try {
      const response = await api.batches();
      setBatches(response.items);
    } catch {
      setBatches([]);
    }
  }, []);

  useEffect(() => {
    void refreshBatches();
    window.addEventListener("vulcan:batch-state-changed", refreshBatches);
    return () => window.removeEventListener("vulcan:batch-state-changed", refreshBatches);
  }, [refreshBatches]);

  function withBatch(href: string) {
    return selectedBatch && href !== "/batches" ? `${href}?batch=${encodeURIComponent(selectedBatch)}` : href;
  }

  function selectBatch(batchRunId: string) {
    const route = pathname === "/batches" ? "/" : pathname;
    router.push(`${route}?batch=${encodeURIComponent(batchRunId)}`);
    setOpen(false);
  }

  return (
    <div className="app-frame">
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
          <div className="batch-select-wrap">
            <label htmlFor="batch-select">Viewing run</label>
            <div className="select-frame">
              <select id="batch-select" value={selectedBatch} onChange={(event) => selectBatch(event.target.value)} disabled={!batches.length}>
                {!batches.length ? <option>Loading runs…</option> : batches.map((batch) => <option key={batch.batch_run_id} value={batch.batch_run_id}>{batch.batch_run_id}</option>)}
              </select>
              <ChevronDown size={15} aria-hidden="true" />
            </div>
          </div>
        </header>
        {children}
      </main>
    </div>
  );
}
