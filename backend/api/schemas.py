"""Pydantic response contracts for the reconciliation operations API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ReconState = Literal[
    "DETERMINISTIC_MATCH",
    "AI_RESOLVED",
    "PENDING_HITL_REVIEW",
    "HITL_APPROVED",
    "HITL_REJECTED",
]


class StateCounts(BaseModel):
    deterministic_match: int = 0
    ai_resolved: int = 0
    pending_hitl_review: int = 0
    hitl_approved: int = 0
    hitl_rejected: int = 0


class BatchSummary(BaseModel):
    batch_run_id: str
    last_activity_at: datetime
    total: int
    auto_reconciled: int
    needs_review: int
    human_resolved: int
    match_rate: str
    state_counts: StateCounts


class BatchListResponse(BaseModel):
    items: list[BatchSummary]


class ReviewItem(BaseModel):
    settlement_id: str
    batch_run_id: Optional[str]
    recon_state: ReconState
    discrepancy_reason: Optional[str]
    deterministic_variance: str
    ai_reported_variance: Optional[str]
    confidence_score: Optional[float]
    expected_net: str
    gross_amount: str
    fees: str
    taxes: str
    refunds: str
    adjustments: str
    settlement_utr: Optional[str]
    bank_name: Optional[str]
    bank_credit: Optional[str]
    bank_transaction_date: Optional[date]
    bank_utr: Optional[str]
    raw_narration: str
    cryptographic_state_hash: str
    created_at: datetime


class ReviewQueueResponse(BaseModel):
    items: list[ReviewItem]
    total: int


class SettlementListItem(BaseModel):
    settlement_id: str
    batch_run_id: Optional[str]
    recon_state: ReconState
    expected_net: str
    bank_credit: Optional[str]
    deterministic_variance: str
    bank_name: Optional[str]
    settlement_utr: Optional[str]
    bank_utr: Optional[str]
    resolved_at: datetime


class SettlementListResponse(BaseModel):
    items: list[SettlementListItem]
    total: int
    page: int
    page_size: int


class SettlementDetail(BaseModel):
    settlement_id: str
    status: str
    batch_run_id: Optional[str]
    recon_state: ReconState
    gross_amount: str
    fees: str
    taxes: str
    refunds: str
    adjustments: str
    expected_net: str
    settlement_utr: Optional[str]
    bank_entry_id: Optional[str]
    bank_name: Optional[str]
    bank_credit: Optional[str]
    bank_transaction_date: Optional[date]
    bank_utr: Optional[str]
    raw_narration: str
    is_bank_reconciled: Optional[bool]
    deterministic_variance: str
    ai_reported_variance: Optional[str]
    discrepancy_reason: Optional[str]
    confidence_score: Optional[float]
    human_decision: Optional[str]
    human_decision_by: Optional[str]
    human_decision_at: Optional[datetime]
    cryptographic_state_hash: str
    resolved_at: datetime
    updated_at: datetime


class AuditEvent(BaseModel):
    event_id: str
    settlement_id: str
    batch_run_id: Optional[str]
    event_type: str
    from_state: Optional[ReconState]
    to_state: ReconState
    human_decision: Optional[str]
    human_decision_by: Optional[str]
    deterministic_variance: str
    cryptographic_state_hash: str
    occurred_at: datetime


class AuditResponse(BaseModel):
    items: list[AuditEvent]


class DecisionRequest(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    decided_by: str = Field(min_length=2, max_length=100)

    model_config = ConfigDict(str_strip_whitespace=True)


class DecisionResponse(BaseModel):
    settlement_id: str
    decision: Literal["APPROVED", "REJECTED"]
    recon_state: Literal["HITL_APPROVED", "HITL_REJECTED"]
    newly_recorded: bool
    graph_resumed: bool
    agent_thread_id: Optional[str]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: Literal["connected"]
    gemini_model: str
