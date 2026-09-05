"""Read, upload, export, and human-decision routes for the reconciliation control tower."""
from __future__ import annotations

import csv
import asyncio
import hashlib
import io
import json
import logging
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import PurePath
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agents.graph import classification_from_state
from agents.hitl import HitlResumeError, apply_human_decision
from agents.schemas import IngestionSanitizer
from api.schemas import (
    AuditEvent,
    AuditResponse,
    BatchAcceptedResponse,
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
    AsyncSessionLocal,
    async_session_factory,
    BankLedgerEntry,
    BatchRegistry,
    HumanDecisionConflictError,
    ReconciliationEvent,
    ReconciliationLedger,
    RazorpaySettlement,
    get_db_session,
    get_or_create_bank_entry,
    mark_bank_entry_reconciled,
    reconciliation_state_hash,
    register_batch,
    upsert_reconciliation,
    upsert_settlement,
)
from config.settings import get_settings
from core.money import money_to_str, parse_money, quantize_money

router = APIRouter(prefix="/api", tags=["reconciliation"])
logger = logging.getLogger("vulcan.reconciliation")

_AUTO_RECONCILED = frozenset({"DETERMINISTIC_MATCH", "AI_RESOLVED"})
_HUMAN_RESOLVED = frozenset({"HITL_APPROVED", "HITL_REJECTED"})
_AI_VARIANCE_THRESHOLD = Decimal("0.05")
_CONFIDENCE_QUANTUM = Decimal("0.001")


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


def _summary(
    batch_run_id: str,
    states: list[str],
    last_activity_at,
    original_file_name: Optional[str] = None,
) -> BatchSummary:
    total = len(states)
    auto_reconciled = sum(state in _AUTO_RECONCILED for state in states)
    human_resolved = sum(state in _HUMAN_RESOLVED for state in states)
    needs_review = states.count("PENDING_HITL_REVIEW")
    return BatchSummary(
        batch_run_id=batch_run_id,
        original_file_name=original_file_name,
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
        record_id=settlement.record_id,
        batch_run_id=reconciliation.batch_run_id,
        recon_state=reconciliation.recon_state,
        discrepancy_reason=reconciliation.ai_classification_reason,
        rca_reason=reconciliation.rca_reason,
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


def _record_get(record: dict, *keys: str):
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _parse_amount(value: object, name: str, default: str = "0.00") -> Decimal:
    if value is None or (isinstance(value, str) and not value.strip()):
        return parse_money(default, name)
    if isinstance(value, Decimal):
        return quantize_money(value, name)
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a monetary amount, not bool")
    if isinstance(value, int):
        return parse_money(str(value), name)
    if isinstance(value, float):
        return parse_money(f"{value:.2f}", name)
    return parse_money(str(value).strip(), name)


def _decode_settlement_payload(filename: Optional[str], raw_bytes: bytes) -> list[dict]:
    if not raw_bytes or not raw_bytes.strip():
        raise ValueError("Empty file payload.")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("File must be UTF-8 encoded.") from exc

    stripped = text.lstrip()
    name = (filename or "").lower()
    looks_json = name.endswith(".json") or stripped.startswith("[") or stripped.startswith("{")
    if looks_json and not name.endswith(".csv"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("records") or data.get("items") or data.get("settlements")
        if not isinstance(data, list):
            raise ValueError("JSON payload must be a top-level array of settlement objects.")
        return [dict(row) for row in data if isinstance(row, dict)]

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV file has no header row.")
    rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError("CSV file contains a header but no data rows.")
    return rows


def _sanitize_records(raw_records: list[dict]) -> list[dict]:
    sanitized_records: list[dict] = []
    for record in raw_records:
        clean_narration = IngestionSanitizer(
            raw_text=str(_record_get(record, "narration", "raw_narration", "remarks") or "")
        ).raw_text
        prepared = dict(record)
        prepared["narration"] = clean_narration
        sanitized_records.append(prepared)
    return sanitized_records


async def _persist_reconciliation(
    session: AsyncSession,
    *,
    settlement_id: str,
    bank_entry_id,
    recon_state: str,
    variance: Decimal,
    narration: str,
    batch_id: str,
    ai_reason: Optional[str] = None,
    rca_reason: Optional[str] = None,
    ai_reported_variance: Optional[Decimal] = None,
    ai_confidence_score: Optional[Decimal] = None,
    agent_thread_id: Optional[str] = None,
) -> None:
    state_hash = reconciliation_state_hash(
        settlement_id=settlement_id,
        recon_state=recon_state,
        variance=variance,
        raw_narration=narration,
    )
    await upsert_reconciliation(
        session,
        settlement_id=settlement_id,
        bank_entry_id=bank_entry_id,
        recon_state=recon_state,
        numeric_variance=variance,
        evidence_narration=narration,
        cryptographic_state_hash=state_hash,
        ai_classification_reason=ai_reason,
        rca_reason=rca_reason,
        ai_reported_variance=ai_reported_variance,
        ai_confidence_score=ai_confidence_score,
        agent_thread_id=agent_thread_id,
        batch_run_id=batch_id,
    )


async def execute_batch_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
    batch_id: str,
    records: list[dict],
    graph,
) -> None:
    """Process sanitized upload rows on an independent DB session after the HTTP response."""
    logger.info("Starting batch reconciliation batch_id=%s records=%s", batch_id, len(records))
    async def process_record(index: int, record: dict) -> None:
        settlement_id = str(
            _record_get(record, "settlement_id", "id") or f"setl_{batch_id[:8]}_{index:03d}"
        )[:50]
        try:
            gross = _parse_amount(_record_get(record, "gross_amount", "gross"), "gross_amount", "0.00")
            fees = _parse_amount(_record_get(record, "gateway_fee", "fees", "fee"), "fees", "0.00")
            taxes = _parse_amount(_record_get(record, "tax_deducted", "taxes", "tax"), "taxes", "0.00")
            refunds = _parse_amount(_record_get(record, "refund_amount", "refunds"), "refunds", "0.00")
            adjustments = _parse_amount(
                _record_get(record, "cross_settlement_adj", "adjustments", "adjustment"),
                "adjustments",
                "0.00",
            )
            net_raw = _record_get(record, "net_settlement", "net_credit", "net")
            if net_raw is not None:
                net_settlement = _parse_amount(net_raw, "net_settlement")
            else:
                net_settlement = quantize_money(gross - fees - taxes - refunds - adjustments, "net_settlement")
            if gross == Decimal("0.00") and net_settlement != Decimal("0.00"):
                gross = net_settlement

            expected = quantize_money(gross - fees - taxes - refunds - adjustments, "expected_net")
            if net_settlement != expected:
                adjustments = quantize_money(gross - fees - taxes - refunds - net_settlement, "adjustments")

            bank_credit_raw = _record_get(record, "bank_credit", "credit_amount", "actual_credit")
            bank_credit = (
                _parse_amount(bank_credit_raw, "bank_credit") if bank_credit_raw is not None else None
            )
            bank_utr = _record_get(record, "bank_utr", "extracted_utr")
            bank_utr = str(bank_utr).strip() if bank_utr is not None else None
            settlement_utr = _record_get(record, "utr_reference", "settlement_utr", "utr") or bank_utr
            if settlement_utr is not None:
                settlement_utr = str(settlement_utr).strip()
            bank_name = str(_record_get(record, "bank_name", "bank") or "UNKNOWN")[:20]
            narration = record.get("narration") or (
                f"Settlement {settlement_id} net {money_to_str(net_settlement)} INR"
            )
            status_value = str(_record_get(record, "status") or "processed")[:20]
            record_id = str(_record_get(record, "record_id") or f"REC-{index:03d}")[:20]
            txn_date_raw = _record_get(record, "settlement_date", "transaction_date", "date")
            try:
                txn_date = date.fromisoformat(str(txn_date_raw)[:10]) if txn_date_raw else date.today()
            except ValueError:
                txn_date = date.today()

            if bank_credit is None:
                variance = net_settlement
            else:
                variance = quantize_money(net_settlement - bank_credit, "numeric_variance")
            abs_variance = abs(variance)
            missing_utr = not bank_utr
            needs_graph = abs_variance > _AI_VARIANCE_THRESHOLD or missing_utr

            async with session_factory() as session:
                await upsert_settlement(
                    session,
                    settlement_id=settlement_id,
                    status=status_value,
                    gross_amount=gross,
                    fees=fees,
                    taxes=taxes,
                    refunds=refunds,
                    adjustments=adjustments,
                    net_settlement=net_settlement,
                    utr_reference=settlement_utr,
                    record_id=record_id,
                )
                bank_entry_id = None
                if bank_credit is not None:
                    bank_entry, _ = await get_or_create_bank_entry(
                        session,
                        bank_name=bank_name,
                        transaction_date=txn_date,
                        credit_amount=bank_credit,
                        raw_narration=narration,
                        extracted_utr=bank_utr,
                    )
                    bank_entry_id = bank_entry.entry_id
                await session.commit()

            recon_state = "DETERMINISTIC_MATCH"
            ai_reason = None
            rca_reason = None
            ai_reported_variance = None
            ai_confidence = None
            thread_id = None

            if needs_graph:
                logger.info(
                    "LangGraph evaluating settlement_id=%s batch_id=%s variance=%s bank_utr=%s",
                    settlement_id,
                    batch_id,
                    money_to_str(abs_variance),
                    bank_utr or "MISSING",
                )
                if graph is None:
                    recon_state = "PENDING_HITL_REVIEW"
                else:
                    thread_id = str(uuid.uuid4())
                    sanitized = (
                        f"Expected net settlement: {money_to_str(net_settlement)} INR. "
                        f"Actual bank credit: {money_to_str(bank_credit) if bank_credit is not None else 'MISSING'} INR. "
                        f"Deterministic variance: {money_to_str(variance)} INR "
                        f"(upload routing threshold is {_AI_VARIANCE_THRESHOLD} INR). "
                        f"UTR extraction: {'succeeded (' + bank_utr + ')' if bank_utr else 'FAILED -- no UTR found'}. "
                        f"Raw bank narration: {narration!r}."
                    )
                    try:
                        agent_state = await graph.ainvoke(
                            {
                                "settlement_id": settlement_id,
                                "sanitized_context": sanitized,
                                "deterministic_variance_str": money_to_str(variance),
                            },
                            config={"configurable": {"thread_id": thread_id}},
                        )
                        classification = classification_from_state(agent_state)
                        if classification:
                            ai_reason = classification.discrepancy_reason
                            ai_reported_variance = classification.variance
                            ai_confidence = Decimal(str(classification.confidence_score)).quantize(
                                _CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP
                            )
                            rca_reason = agent_state.get("rca_reason")
                        if (
                            "__interrupt__" in agent_state
                            or agent_state.get("requires_human_review")
                            or (
                                classification is not None
                                and classification.suggested_action == "ROUTE_TO_HITL_PANEL"
                            )
                        ):
                            recon_state = "PENDING_HITL_REVIEW"
                        else:
                            recon_state = "AI_RESOLVED"
                    except Exception as exc:  # noqa: BLE001 — one row must not abort the batch
                        logger.exception(
                            "LangGraph failed for settlement_id=%s: %s", settlement_id, exc
                        )
                        recon_state = "PENDING_HITL_REVIEW"
            else:
                logger.info(
                    "Deterministic match settlement_id=%s batch_id=%s variance=%s",
                    settlement_id,
                    batch_id,
                    money_to_str(variance),
                )

            async with session_factory() as session:
                await _persist_reconciliation(
                    session,
                    settlement_id=settlement_id,
                    bank_entry_id=bank_entry_id,
                    recon_state=recon_state,
                    variance=variance,
                    narration=narration,
                    batch_id=batch_id,
                    ai_reason=ai_reason,
                    rca_reason=rca_reason,
                    ai_reported_variance=ai_reported_variance,
                    ai_confidence_score=ai_confidence,
                    agent_thread_id=thread_id,
                )
                if bank_entry_id is not None:
                    await mark_bank_entry_reconciled(session, bank_entry_id)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — continue remaining rows
            logger.exception(
                "Failed to reconcile row %s settlement_id=%s batch_id=%s: %s",
                index,
                settlement_id,
                batch_id,
                exc,
            )
    # Keep database and model-provider load bounded while independent records
    # make progress together. Each record owns its sessions and graph thread.
    chunk_size = 8
    indexed_records = list(enumerate(records, start=1))
    for offset in range(0, len(indexed_records), chunk_size):
        chunk = indexed_records[offset : offset + chunk_size]
        await asyncio.gather(*(process_record(index, record) for index, record in chunk))
    logger.info("Finished batch reconciliation batch_id=%s", batch_id)


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


async def _existing_payload_batch(session: AsyncSession, file_hash: str) -> Optional[str]:
    return await session.scalar(
        select(ReconciliationEvent.batch_run_id)
        .where(ReconciliationEvent.batch_run_id == file_hash)
        .limit(1)
    )


def _schedule_batch_pipeline(
    request: Request,
    background_tasks: BackgroundTasks,
    batch_id: str,
    records: list[dict],
) -> None:
    graph = getattr(getattr(request, "app", None), "state", None)
    graph = getattr(graph, "reconciliation_graph", None)
    background_tasks.add_task(
        execute_batch_reconciliation,
        async_session_factory,
        batch_id,
        records,
        graph,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse)
async def health(session: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    await session.execute(select(1))
    return HealthResponse(
        status="ok", database="connected", gemini_model=get_settings().gemini_model
    )


# ---------------------------------------------------------------------------
# Batch list & summary
# ---------------------------------------------------------------------------
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
                await session.scalar(
                    select(BatchRegistry.original_file_name).where(
                        BatchRegistry.batch_id == batch_run_id
                    )
                ),
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
        await session.scalar(
            select(BatchRegistry.original_file_name).where(BatchRegistry.batch_id == batch_run_id)
        ),
    )


async def _accept_settlement_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile,
    session: AsyncSession,
) -> BatchAcceptedResponse:
    content = await file.read()
    if not content or not content.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file payload.")

    file_hash = hashlib.sha256(content).hexdigest()
    existing_batch = await _existing_payload_batch(session, file_hash)
    if existing_batch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Duplicate Upload: File already processed under batch ID {file_hash}",
        )

    try:
        raw_records = _decode_settlement_payload(file.filename, content)
        sanitized_records = _sanitize_records(raw_records)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Data validation failed: {exc}",
        ) from exc

    original_file_name = PurePath(file.filename or "settlements.csv").name or "settlements.csv"
    await register_batch(session, file_hash, original_file_name)
    _schedule_batch_pipeline(request, background_tasks, file_hash, sanitized_records)
    return BatchAcceptedResponse(
        status="accepted",
        batch_id=file_hash,
        records=len(sanitized_records),
        original_file_name=original_file_name,
    )


# ---------------------------------------------------------------------------
# Batch Upload — POST /api/batches/upload
# ---------------------------------------------------------------------------
@router.post("/batches/upload", response_model=BatchAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_settlement_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Settlement batch as .csv or .json"),
    session: AsyncSession = Depends(get_db_session),
) -> BatchAcceptedResponse:
    """Accept a CSV or JSON settlement file and reconcile it after the response.

    The request-scoped database session is used only for duplicate detection.
    Pipeline writes run on ``async_session_factory`` so they are not closed when
    this request finishes.
    """
    return await _accept_settlement_upload(request, background_tasks, file, session)


# ---------------------------------------------------------------------------
# JSON Batch Upload — POST /api/batches/upload-json (Idempotent)
# ---------------------------------------------------------------------------
@router.post("/batches/upload-json", response_model=BatchAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_settlement_batch_json(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db_session),
) -> BatchAcceptedResponse:
    """Idempotent JSON (or CSV) ingestion that shares the background pipeline."""
    return await _accept_settlement_upload(request, background_tasks, file, session)


# ---------------------------------------------------------------------------
# Audit Export — GET /api/batches/{batch_id}/export
# ---------------------------------------------------------------------------
@router.get("/batches/{batch_run_id}/export")
async def export_batch(
    batch_run_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """
    Stream the full reconciliation state of a batch as a CSV file.

    The export includes every settlement in the batch with its Batch ID,
    Record ID, all financial components, reconciliation state, AI root-cause
    hypothesis, and the cryptographic audit fingerprint — the complete
    immutable compliance record a finance team needs.

    The file is streamed (never written to disk) to keep memory usage
    proportional to one row at a time, not to the whole batch.
    """
    rows = (
        await session.execute(
            select(ReconciliationLedger, RazorpaySettlement, BankLedgerEntry)
            .join(
                RazorpaySettlement,
                ReconciliationLedger.settlement_id == RazorpaySettlement.settlement_id,
            )
            .outerjoin(BankLedgerEntry, ReconciliationLedger.bank_entry_id == BankLedgerEntry.entry_id)
            .where(ReconciliationLedger.batch_run_id == batch_run_id)
            .order_by(RazorpaySettlement.record_id.asc().nullslast(), ReconciliationLedger.resolved_at.asc())
        )
    ).all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_run_id!r} not found or contains no reconciled settlements.",
        )

    original_file_name = await session.scalar(
        select(BatchRegistry.original_file_name).where(BatchRegistry.batch_id == batch_run_id)
    )
    export_stem = PurePath(original_file_name or batch_run_id).stem
    safe_name = "".join(character for character in export_stem if character.isalnum() or character in "-_ ").strip()
    safe_name = safe_name or "settlements"

    HEADERS = [
        "batch_id",
        "record_id",
        "settlement_id",
        "status",
        "gross_amount",
        "gateway_fee",
        "tax_deducted",
        "refund_amount",
        "cross_settlement_adj",
        "net_settlement",
        "bank_credit",
        "bank_utr",
        "settlement_utr",
        "bank_name",
        "deterministic_variance",
        "recon_state",
        "ai_classification_reason",
        "rca_reason",
        "confidence_score",
        "human_decision",
        "human_decision_by",
        "cryptographic_state_hash",
        "resolved_at",
    ]

    def _row(recon: ReconciliationLedger, s: RazorpaySettlement, b: Optional[BankLedgerEntry]) -> list:
        return [
            batch_run_id,
            s.record_id or "",
            recon.settlement_id,
            s.status,
            money_to_str(s.gross_amount),
            money_to_str(s.fees),
            money_to_str(s.taxes),
            money_to_str(s.refunds),
            money_to_str(s.adjustments),
            money_to_str(s.net_settlement),
            money_to_str(b.credit_amount) if b else "",
            b.extracted_utr or "" if b else "",
            s.utr_reference or "",
            b.bank_name if b else "",
            money_to_str(recon.numeric_variance),
            recon.recon_state,
            recon.ai_classification_reason or "",
            recon.rca_reason or "",
            f"{float(recon.ai_confidence_score):.3f}" if recon.ai_confidence_score is not None else "",
            recon.human_decision or "",
            recon.human_decision_by or "",
            recon.cryptographic_state_hash,
            recon.resolved_at.isoformat() if recon.resolved_at else "",
        ]

    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(HEADERS)
        yield buf.getvalue()

        for recon, settlement, bank in rows:
            buf = io.StringIO()
            writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(_row(recon, settlement, bank))
            yield buf.getvalue()

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}_audited.csv"',
            "X-Batch-ID": batch_run_id,
            "X-Row-Count": str(len(rows)),
        },
    )


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Settlement list
# ---------------------------------------------------------------------------
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
                record_id=settlement.record_id,
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


# ---------------------------------------------------------------------------
# Settlement detail
# ---------------------------------------------------------------------------
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
        record_id=settlement.record_id,
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
        rca_reason=recon.rca_reason,
        confidence_score=_confidence(recon.ai_confidence_score),
        human_decision=recon.human_decision,
        human_decision_by=recon.human_decision_by,
        human_decision_at=recon.human_decision_at,
        cryptographic_state_hash=recon.cryptographic_state_hash,
        resolved_at=recon.resolved_at,
        updated_at=recon.updated_at,
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Human decision
# ---------------------------------------------------------------------------
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
