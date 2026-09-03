export type ReconState =
  | "DETERMINISTIC_MATCH"
  | "AI_RESOLVED"
  | "PENDING_HITL_REVIEW"
  | "HITL_APPROVED"
  | "HITL_REJECTED";

export type StateCounts = {
  deterministic_match: number;
  ai_resolved: number;
  pending_hitl_review: number;
  hitl_approved: number;
  hitl_rejected: number;
};

export type BatchSummary = {
  batch_run_id: string;
  last_activity_at: string;
  total: number;
  auto_reconciled: number;
  needs_review: number;
  human_resolved: number;
  match_rate: string;
  state_counts: StateCounts;
};

export type ReviewItem = {
  settlement_id: string;
  batch_run_id: string | null;
  recon_state: ReconState;
  discrepancy_reason: string | null;
  deterministic_variance: string;
  ai_reported_variance: string | null;
  confidence_score: number | null;
  expected_net: string;
  gross_amount: string;
  fees: string;
  taxes: string;
  refunds: string;
  adjustments: string;
  settlement_utr: string | null;
  bank_name: string | null;
  bank_credit: string | null;
  bank_transaction_date: string | null;
  bank_utr: string | null;
  raw_narration: string;
  cryptographic_state_hash: string;
  created_at: string;
};

export type SettlementListItem = {
  settlement_id: string;
  batch_run_id: string | null;
  recon_state: ReconState;
  expected_net: string;
  bank_credit: string | null;
  deterministic_variance: string;
  bank_name: string | null;
  settlement_utr: string | null;
  bank_utr: string | null;
  resolved_at: string;
};

export type SettlementDetail = {
  settlement_id: string;
  status: string;
  batch_run_id: string | null;
  recon_state: ReconState;
  gross_amount: string;
  fees: string;
  taxes: string;
  refunds: string;
  adjustments: string;
  expected_net: string;
  settlement_utr: string | null;
  bank_entry_id: string | null;
  bank_name: string | null;
  bank_credit: string | null;
  bank_transaction_date: string | null;
  bank_utr: string | null;
  raw_narration: string;
  is_bank_reconciled: boolean | null;
  deterministic_variance: string;
  ai_reported_variance: string | null;
  discrepancy_reason: string | null;
  confidence_score: number | null;
  human_decision: string | null;
  human_decision_by: string | null;
  human_decision_at: string | null;
  cryptographic_state_hash: string;
  resolved_at: string;
  updated_at: string;
};

export type AuditEvent = {
  event_id: string;
  settlement_id: string;
  batch_run_id: string | null;
  event_type: string;
  from_state: ReconState | null;
  to_state: ReconState;
  human_decision: string | null;
  human_decision_by: string | null;
  deterministic_variance: string;
  cryptographic_state_hash: string;
  occurred_at: string;
};

export type Health = {
  status: "ok";
  database: "connected";
  gemini_model: string;
};

export type ApiError = Error & { status?: number; code?: string };
