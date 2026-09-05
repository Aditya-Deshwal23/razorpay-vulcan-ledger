import type {
  ApiError,
  AuditEvent,
  BatchSummary,
  BatchUploadResponse,
  Health,
  ReconState,
  ReviewItem,
  SettlementDetail,
  SettlementListItem,
} from "@/types/api";

const baseUrl = typeof window !== "undefined"
  ? "" 
  : (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000");

// Global event bus for gracefully surfacing API failures to the UI.
// Emit a vulcan:toast event that ToastContainer (in app-shell.tsx) listens for.
export const apiEventBus = {
  emitToast: (title: string, description: string, type: "error" | "success" | "warning") => {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("vulcan:toast", { detail: { title, description, type } }));
    }
  },
};

function queryString(values: Record<string, string | number | undefined | null>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const rendered = query.toString();
  return rendered ? `?${rendered}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${baseUrl}${path}`;
  const controller = new AbortController();
  // Hard 12-second timeout — prevents the UI hanging indefinitely on a
  // stalled backend. Fires vulcan:toast so the operator sees the degraded state.
  const timeoutId = setTimeout(() => controller.abort(), 12000);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init?.headers },
      cache: "no-store",
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const error = new Error(body.detail ?? `${response.status} ${response.statusText}`) as ApiError;
      error.status = response.status;
      error.code = body.code;
      console.error(`API request failed: ${init?.method ?? "GET"} ${url} - ${error.message}`, body);
      throw error;
    }
    return response.json() as Promise<T>;

  } catch (caught: unknown) {
    clearTimeout(timeoutId);
    const err = caught as Error & { name?: string; message?: string };
    if (err.name === "AbortError") {
      apiEventBus.emitToast(
        "Gateway Timeout",
        "The server took too long to respond. The system may be operating in a degraded state.",
        "error"
      );
      throw new Error("Request timed out.");
    }
    if (caught instanceof TypeError && err.message?.includes("fetch")) {
      apiEventBus.emitToast("Network Offline", "Cannot reach the reconciliation engine.", "error");
    }
    console.error(`API request error: ${init?.method ?? "GET"} ${url}`, caught);
    throw caught;
  }
}

export const api = {
  health: () => request<Health>("/api/health"),
  batches: () => request<{ items: BatchSummary[] }>("/api/batches"),
  summary: (batchRunId: string) => request<BatchSummary>(`/api/batches/${encodeURIComponent(batchRunId)}/summary`),
  review: (batchRunId: string) =>
    request<{ items: ReviewItem[]; total: number }>(
      `/api/batches/${encodeURIComponent(batchRunId)}/review`,
    ),
  settlements: (filters: {
    batchRunId?: string;
    reconState?: ReconState;
    bankName?: string;
    query?: string;
    page?: number;
    pageSize?: number;
  }) =>
    request<{ items: SettlementListItem[]; total: number; page: number; page_size: number }>(
      `/api/settlements${queryString({
        batch_run_id: filters.batchRunId,
        recon_state: filters.reconState,
        bank_name: filters.bankName,
        query: filters.query,
        page: filters.page,
        page_size: filters.pageSize,
      })}`,
    ),
  settlement: (settlementId: string) =>
    request<SettlementDetail>(`/api/settlements/${encodeURIComponent(settlementId)}`),
  audit: (filters: { batchRunId?: string; settlementId?: string; limit?: number }) =>
    request<{ items: AuditEvent[] }>(
      `/api/audit${queryString({
        batch_run_id: filters.batchRunId,
        settlement_id: filters.settlementId,
        limit: filters.limit,
      })}`,
    ),
  decide: (settlementId: string, decision: "APPROVED" | "REJECTED", decidedBy: string) =>
    request<{
      settlement_id: string;
      decision: "APPROVED" | "REJECTED";
      recon_state: "HITL_APPROVED" | "HITL_REJECTED";
      newly_recorded: boolean;
    }>(`/api/settlements/${encodeURIComponent(settlementId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, decided_by: decidedBy }),
    }),

  /**
   * Upload a Razorpay settlement CSV file for ingestion.
   * Returns the newly assigned BATCH-NNN id and per-row results.
   */
  upload: async (file: File): Promise<BatchUploadResponse> => {
    const url = `${baseUrl}/api/batches/upload`;
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const response = await fetch(url, {
        method: "POST",
        body: form,
        // No Content-Type header — browser sets multipart boundary automatically.
        cache: "no-store",
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const error = new Error(body.detail ?? `${response.status} ${response.statusText}`) as ApiError;
        error.status = response.status;
        console.error(`API upload failed: POST ${url} - ${error.message}`, body);
        throw error;
      }
      return response.json() as Promise<BatchUploadResponse>;
    } catch (error) {
      console.error(`API upload error: POST ${url}`, error);
      throw error;
    }
  },

  /**
   * Download the full audit export CSV for a batch.
   * Returns a Blob that the caller can hand to URL.createObjectURL.
   */
  exportBatch: async (batchRunId: string): Promise<Blob> => {
    const url = `${baseUrl}/api/batches/${encodeURIComponent(batchRunId)}/export`;
    try {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const error = new Error(body.detail ?? `${response.status} ${response.statusText}`) as ApiError;
        error.status = response.status;
        console.error(`API export failed: GET ${url} - ${error.message}`, body);
        throw error;
      }
      return response.blob();
    } catch (error) {
      console.error(`API export error: GET ${url}`, error);
      throw error;
    }
  },
};
