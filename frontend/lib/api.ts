import type {
  ApiError,
  AuditEvent,
  BatchSummary,
  Health,
  ReconState,
  ReviewItem,
  SettlementDetail,
  SettlementListItem,
} from "@/types/api";

const baseUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function queryString(values: Record<string, string | number | undefined | null>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const rendered = query.toString();
  return rendered ? `?${rendered}` : "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail ?? `${response.status} ${response.statusText}`) as ApiError;
    error.status = response.status;
    error.code = body.code;
    throw error;
  }
  return response.json() as Promise<T>;
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
};
