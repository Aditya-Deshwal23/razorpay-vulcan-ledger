"""Read and human-decision routes for the reconciliation control tower."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.hitl import HitlResumeError, apply_human_decision
from api.schemas import (
    AuditEvent,
    AuditResponse,
    BatchListResponse,
    BatchSummary,
    DecisionRequest,
    DecisionResponse,
    HealthResponse,
    ReconState,
    ReviewItem,
    ReviewQueueResponse,
    SettlementDetail,
    SettlementListItem,
    SettlementListResponse,
    StateCounts,
)
from config.database import (
    HUMAN_DECISION_STATES,
    BankLedgerEntry,
    HumanDecisionConflictError,
    ReconciliationEvent,
    ReconciliationLedger,
    RazorpaySettlement,
    get_db_session,
)
from config.settings import get_settings
from core.money import money_to_str, parse_money

router = APIRouter(prefix="/api", tags=["reconciliation"])

_AUTO_RECONCILED = frozenset({"DETERMINISTIC_MATCH", "AI_RESOLVED"})
_HUMAN_RESOLVED = frozenset({"HITL_APPROVED", "HITL_REJECTED"})


def _state_counts(states: list[str]) -> StateCounts:
    counts = defaultdict(int)
    for recon_state in states:
        counts[recon_state] += 1
    return StateCounts(
        deterministic_match=counts["DETERMINISTIC_MATCH"],
        ai_resolved=counts["AI_RESOLVED"],
        pending_hitl_review=counts["PENDING_HITL_REVIEW"],
        hitl_approved=counts["HITL_APPROVED"],
        hitl_rejected=counts["HITL_REJECTED"],
    )


def _match_rate(auto_reconciled: int, total: int) -> str:
    if not total:
        return "0.00"
    return str(
        (Decimal(auto_reconciled) / Decimal(total) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def _summary(batch_run_id: str, states: list[str], last_activity_at) -> BatchSummary:
    total = len(states)
    auto_reconciled = sum(state in _AUTO_RECONCILED for state in states)
    human_resolved = sum(state in _HUMAN_RESOLVED for state in states)
    needs_review = states.count("PENDING_HITL_REVIEW")
    return BatchSummary(
        batch_run_id=batch_run_id,
        last_activity_at=last_activity_at,
        total=total,
        auto_reconciled=auto_reconciled,
        needs_review=needs_review,
        human_resolved=human_resolved,
        match_rate=_match_rate(auto_reconciled, total),
        state_counts=_state_counts(states),
    )


def _money(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else money_to_str(value)


def _confidence(value: Optional[Decimal]) -> Optional[float]:
    return None if value is None else float(value)


def _review_item(
    reconciliation: ReconciliationLedger,
    settlement: RazorpaySettlement,
    bank: Optional[BankLedgerEntry],
) -> ReviewItem:
    return ReviewItem(
        settlement_id=reconciliation.settlement_id,
        batch_run_id=reconciliation.batch_run_id,
        recon_state=reconciliation.recon_state,
        discrepancy_reason=reconciliation.ai_classification_reason,
        deterministic_variance=money_to_str(reconciliation.numeric_variance),
        ai_reported_variance=_money(reconciliation.ai_reported_variance),
        confidence_score=_confidence(reconciliation.ai_confidence_score),
        expected_net=money_to_str(settlement.net_settlement),
        gross_amount=money_to_str(settlement.gross_amount),
        fees=money_to_str(settlement.fees),
        taxes=money_to_str(settlement.taxes),
        refunds=money_to_str(settlement.refunds),
        adjustments=money_to_str(settlement.adjustments),
        settlement_utr=settlement.utr_reference,
        bank_name=bank.bank_name if bank else None,
        bank_credit=_money(bank.credit_amount) if bank else None,
        bank_transaction_date=bank.transaction_date if bank else None,
        bank_utr=bank.extracted_utr if bank else None,
        raw_narration=reconciliation.evidence_narration,
        cryptographic_state_hash=reconciliation.cryptographic_state_hash,
        created_at=reconciliation.resolved_at,
    )


async def _batch_rows(session: AsyncSession, batch_run_id: str):
    return (
        await session.execute(
            select(ReconciliationLedger.recon_state, ReconciliationLedger.updated_at)
            .where(ReconciliationLedger.batch_run_id == batch_run_id)
            .order_by(ReconciliationLedger.updated_at.desc())
        )
    ).all()


async def _batch_activity(session: AsyncSession, batch_run_id: str):
    return await session.scalar(
        select(func.max(ReconciliationEvent.occurred_at)).where(
            ReconciliationEvent.batch_run_id == batch_run_id
        )
    )


@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    await session.execute(select(1))
    return HealthResponse(
        status="ok", database="connected", gemini_model=get_settings().gemini_model
    )


@router.get("/batches", response_model=BatchListResponse)
async def list_batches(
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> BatchListResponse:
    rows = (
        await session.execute(
            select(
                ReconciliationLedger.batch_run_id,
                ReconciliationLedger.recon_state,
                ReconciliationLedger.updated_at,
            )
            .where(ReconciliationLedger.batch_run_id.is_not(None))
            .order_by(ReconciliationLedger.updated_at.desc())
        )
    ).all()

    grouped: dict[str, list] = {}
    for row in rows:
        if row.batch_run_id not in grouped:
            if len(grouped) == limit:
                continue
            grouped[row.batch_run_id] = []
        if row.batch_run_id in grouped:
            grouped[row.batch_run_id].append(row)

    summaries = [
            _summary(
                batch_run_id,
                [row.recon_state for row in batch_rows],
                await _batch_activity(session, batch_run_id)
                or max(row.updated_at for row in batch_rows),
            )
            for batch_run_id, batch_rows in grouped.items()
    ]
    summaries.sort(key=lambda summary: (summary.needs_review, summary.last_activity_at), reverse=True)
    return BatchListResponse(items=summaries)


@router.get("/batches/{batch_run_id}/summary", response_model=BatchSummary)
async def batch_summary(
    batch_run_id: str, session: AsyncSession = Depends(get_db_session)
) -> BatchSummary:
    rows = await _batch_rows(session, batch_run_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return _summary(
        batch_run_id,
        [row.recon_state for row in rows],
        await _batch_activity(session, batch_run_id) or max(row.updated_at for row in rows),
    )


@router.get("/batches/{batch_run_id}/review", response_model=ReviewQueueResponse)
async def review_queue(
    batch_run_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> ReviewQueueResponse:
    rows = (
        await session.execute(
            select(ReconciliationLedger, RazorpaySettlement, BankLedgerEntry)
            .join(
                RazorpaySettlement,
                ReconciliationLedger.settlement_id == RazorpaySettlement.settlement_id,
            )
            .outerjoin(BankLedgerEntry, ReconciliationLedger.bank_entry_id == BankLedgerEntry.entry_id)
            .where(
                ReconciliationLedger.batch_run_id == batch_run_id,
                ReconciliationLedger.recon_state == "PENDING_HITL_REVIEW",
            )
            .order_by(ReconciliationLedger.resolved_at.asc())
            .limit(limit)
        )
    ).all()
    return ReviewQueueResponse(
        items=[_review_item(recon, settlement, bank) for recon, settlement, bank in rows],
        total=len(rows),
    )


@router.get("/settlements", response_model=SettlementListResponse)
async def list_settlements(
    batch_run_id: Optional[str] = None,
    recon_state: Optional[ReconState] = None,
    bank_name: Optional[str] = None,
    query: Optional[str] = Query(default=None, min_length=1, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=10, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> SettlementListResponse:
    filters = []
    if batch_run_id:
        filters.append(ReconciliationLedger.batch_run_id == batch_run_id)
    if recon_state:
        filters.append(ReconciliationLedger.recon_state == recon_state)
    if bank_name:
        filters.append(BankLedgerEntry.bank_name == bank_name)

    if query:
        term = query.strip()
        search_filters = [
            ReconciliationLedger.settlement_id.ilike(f"%{term}%"),
            RazorpaySettlement.utr_reference.ilike(f"%{term}%"),
            BankLedgerEntry.extracted_utr.ilike(f"%{term}%"),
        ]
        try:
            amount = parse_money(term, "query")
        except (TypeError, ValueError, InvalidOperation):
            amount = None
        if amount is not None:
            search_filters.extend(
                [
                    RazorpaySettlement.net_settlement == amount,
                    BankLedgerEntry.credit_amount == amount,
                ]
            )
        filters.append(or_(*search_filters))

    base_query = (
        select(ReconciliationLedger, RazorpaySettlement, BankLedgerEntry)
        .join(
            RazorpaySettlement,
            ReconciliationLedger.settlement_id == RazorpaySettlement.settlement_id,
        )
        .outerjoin(BankLedgerEntry, ReconciliationLedger.bank_entry_id == BankLedgerEntry.entry_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base_query.subquery()))
    rows = (
        await session.execute(
            base_query.order_by(ReconciliationLedger.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    return SettlementListResponse(
        items=[
            SettlementListItem(
                settlement_id=recon.settlement_id,
                batch_run_id=recon.batch_run_id,
                recon_state=recon.recon_state,
                expected_net=money_to_str(settlement.net_settlement),
                bank_credit=_money(bank.credit_amount) if bank else None,
                deterministic_variance=money_to_str(recon.numeric_variance),
                bank_name=bank.bank_name if bank else None,
                settlement_utr=settlement.utr_reference,
                bank_utr=bank.extracted_utr if bank else None,
                resolved_at=recon.resolved_at,
            )
            for recon, settlement, bank in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/settlements/{settlement_id}", response_model=SettlementDetail)
async def settlement_detail(
    settlement_id: str, session: AsyncSession = Depends(get_db_session)
) -> SettlementDetail:
    row = (
        await session.execute(
            select(ReconciliationLedger, RazorpaySettlement, BankLedgerEntry)
            .join(
                RazorpaySettlement,
                ReconciliationLedger.settlement_id == RazorpaySettlement.settlement_id,
            )
            .outerjoin(BankLedgerEntry, ReconciliationLedger.bank_entry_id == BankLedgerEntry.entry_id)
            .where(ReconciliationLedger.settlement_id == settlement_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settlement not found")
    recon, settlement, bank = row
    return SettlementDetail(
        settlement_id=recon.settlement_id,
        status=settlement.status,
        batch_run_id=recon.batch_run_id,
        recon_state=recon.recon_state,
        gross_amount=money_to_str(settlement.gross_amount),
        fees=money_to_str(settlement.fees),
        taxes=money_to_str(settlement.taxes),
        refunds=money_to_str(settlement.refunds),
        adjustments=money_to_str(settlement.adjustments),
        expected_net=money_to_str(settlement.net_settlement),
        settlement_utr=settlement.utr_reference,
        bank_entry_id=str(bank.entry_id) if bank else None,
        bank_name=bank.bank_name if bank else None,
        bank_credit=_money(bank.credit_amount) if bank else None,
        bank_transaction_date=bank.transaction_date if bank else None,
        bank_utr=bank.extracted_utr if bank else None,
        raw_narration=recon.evidence_narration,
        is_bank_reconciled=bank.is_reconciled if bank else None,
        deterministic_variance=money_to_str(recon.numeric_variance),
        ai_reported_variance=_money(recon.ai_reported_variance),
        discrepancy_reason=recon.ai_classification_reason,
        confidence_score=_confidence(recon.ai_confidence_score),
        human_decision=recon.human_decision,
        human_decision_by=recon.human_decision_by,
        human_decision_at=recon.human_decision_at,
        cryptographic_state_hash=recon.cryptographic_state_hash,
        resolved_at=recon.resolved_at,
        updated_at=recon.updated_at,
    )


@router.get("/audit", response_model=AuditResponse)
async def audit_log(
    batch_run_id: Optional[str] = None,
    settlement_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> AuditResponse:
    filters = []
    if batch_run_id:
        filters.append(ReconciliationEvent.batch_run_id == batch_run_id)
    if settlement_id:
        filters.append(ReconciliationEvent.settlement_id == settlement_id)
    rows = (
        await session.scalars(
            select(ReconciliationEvent)
            .where(*filters)
            .order_by(ReconciliationEvent.occurred_at.desc(), ReconciliationEvent.event_id.desc())
            .limit(limit)
        )
    ).all()
    return AuditResponse(
        items=[
            AuditEvent(
                event_id=str(event.event_id),
                settlement_id=event.settlement_id,
                batch_run_id=event.batch_run_id,
                event_type=event.event_type,
                from_state=event.from_state,
                to_state=event.to_state,
                human_decision=event.human_decision,
                human_decision_by=event.human_decision_by,
                deterministic_variance=money_to_str(event.numeric_variance),
                cryptographic_state_hash=event.cryptographic_state_hash,
                occurred_at=event.occurred_at,
            )
            for event in rows
        ]
    )


@router.post(
    "/settlements/{settlement_id}/decision",
    response_model=DecisionResponse,
    status_code=status.HTTP_200_OK,
)
async def decide_settlement(
    settlement_id: str,
    payload: DecisionRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> DecisionResponse:
    try:
        outcome = await apply_human_decision(
            session,
            settlement_id=settlement_id,
            decision=payload.decision,
            decided_by=payload.decided_by,
            graph=request.app.state.reconciliation_graph,
        )
    except HumanDecisionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except HitlResumeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Review could not be confirmed: {exc}",
        ) from exc
    except (OperationalError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if outcome.decision not in HUMAN_DECISION_STATES:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid decision")
    return DecisionResponse(**outcome.model_dump())
