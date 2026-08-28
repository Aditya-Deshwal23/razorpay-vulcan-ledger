"""
Async SQLAlchemy engine, session factory, ORM models, and the idempotent
write helpers for the Razorpay Vulcan Ledger reconciliation database.

This module owns the only two things allowed to know about SQL in this
codebase: the SQLAlchemy engine/session machinery, and the declarative models
that mirror backend/database/migrations/*.sql column-for-column. Every other
module (parsers, rules engine, LangGraph agent, HITL service, evaluation
runner) talks to the database exclusively through the async session and the
helpers below -- never through a raw connection string of its own, and never
by hand-rolling its own INSERT.

Currency handling:
    Every NUMERIC(15,2) column maps to decimal.Decimal (SQLAlchemy's Numeric
    is asdecimal=True by default). Amounts are pushed through
    core.money.quantize_money before they reach a statement, so a float, a
    NaN, or a third decimal place is rejected in Python with an actionable
    message instead of becoming a numeric-overflow or a silently rounded row.

Idempotency, concretely:
    - t_bank_ledger is keyed by dedupe_hash, a SHA-256 over the full natural
      key of a bank credit. PostgreSQL treats NULLs as DISTINCT in a UNIQUE
      constraint, so the previous (bank_name, extracted_utr, credit_amount)
      constraint silently allowed unlimited re-imports of any credit whose
      narration had no parseable UTR. A hash over COALESCE(utr,'') has no
      such hole.
    - t_reconciliation_ledger is keyed by settlement_id, so re-running the
      pipeline converges instead of duplicating. The upsert deliberately
      refuses to overwrite a row a human has already decided on.
    - A non-NULL bank_entry_id may back at most one settlement
      (unique_recon_bank_entry), which is what stops one real payout from
      being counted twice.

Exception vectors handled:
    - DB unreachable -> sqlalchemy.exc.OperationalError on first use of the
      engine; the FastAPI layer should turn that into a 503.
    - Duplicate bank credit -> handled at the database level with ON CONFLICT
      DO NOTHING, not by catching IntegrityError after the fact.
    - A bank credit already claimed by a different settlement, or a second
      row claiming the same UTR -> BankLedgerConflictError, raised from
      inside a SAVEPOINT so the caller's transaction stays usable and the
      batch can record the conflict and continue.
    - A pipeline re-run over a settlement a human already approved/rejected
      -> the upsert's WHERE clause skips it; the human decision survives.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import AsyncIterator, Optional, Sequence

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from config.settings import get_settings
from core.money import money_to_str, quantize_money

settings = get_settings()

# ---------------------------------------------------------------------------
# The reconciliation state machine, in Python
# ---------------------------------------------------------------------------
# Mirrors ck_recon_state_enum in 002_hardening.sql. recon_state used to be a
# free VARCHAR, so a typo could invent a state that every downstream
# aggregation would silently miss -- the benchmark would just under-report.
#: Every legal value of t_reconciliation_ledger.recon_state.
RECON_STATES: frozenset[str] = frozenset(
    {
        "DETERMINISTIC_MATCH",
        "AI_RESOLVED",
        "PENDING_HITL_REVIEW",
        "HITL_APPROVED",
        "HITL_REJECTED",
    }
)

#: States that record a completed human decision. A pipeline re-run must never
#: overwrite one of these -- see upsert_reconciliation().
TERMINAL_HITL_STATES: frozenset[str] = frozenset({"HITL_APPROVED", "HITL_REJECTED"})

#: The only legal human verdicts, mapped to the state each one produces. Kept
#: in step with ck_recon_human_decision, which enforces the same pairing in
#: the database so neither side can drift.
HUMAN_DECISION_STATES: dict[str, str] = {
    "APPROVED": "HITL_APPROVED",
    "REJECTED": "HITL_REJECTED",
}


class BankLedgerConflictError(RuntimeError):
    """
    A bank credit could not be written or attached without contradicting a row
    that already exists -- e.g. a second row claiming a UTR another credit
    already owns, or a settlement trying to claim a bank credit another
    settlement is already reconciled against.

    Raised instead of letting IntegrityError escape, so callers can record a
    specific, explainable exception for that record and keep processing the
    rest of the batch. Never swallowed: the message names the conflicting
    values.
    """


class HumanDecisionConflictError(RuntimeError):
    """
    A human decision was submitted for a reconciliation row that is not
    awaiting review, or that already carries a different decision.

    This is what makes the HITL flow safe to expose over an API later: a
    double-click, a retried request, or two reviewers acting at once cannot
    silently flip an already-recorded verdict.
    """


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------
# pool_pre_ping guards against stale connections after the DB restarts or an
# idle timeout drops the socket -- without it, the first query on a dead
# connection raises instead of transparently reconnecting.
#
# echo is settings.sql_echo, an explicit opt-in. It used to be
# (settings.environment == "development"), which meant every development run
# dumped every monetary value of every row into the console -- noise that
# buried real errors and printed financial data into terminal scrollback.
engine = create_async_engine(
    settings.database_url.get_secret_value(),
    echo=settings.sql_echo,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """
    FastAPI dependency that yields a request-scoped AsyncSession.

    Usage:
        @app.get("/settlements/{settlement_id}")
        async def read_settlement(
            settlement_id: str, session: AsyncSession = Depends(get_db_session)
        ):
            ...

    The session is committed if the request handler completes without raising,
    and rolled back otherwise -- callers never need their own try/except
    around commit/rollback for the happy path. The exception is always
    re-raised, never swallowed.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Deterministic hashes -- reproducible from SQL as well as from Python
# ---------------------------------------------------------------------------
def _canonical_date(value: date | datetime, name: str) -> str:
    """
    Render a date the way PostgreSQL's to_char(d, 'YYYY-MM-DD') does.

    Args:
        value: a date, or a datetime (its date part is used -- a datetime here
            means the caller has a timestamp where the bank gave a value date,
            and including a time component would make the same credit hash
            differently on every import).
        name: field name, for the error message.

    Returns:
        e.g. "2026-08-26".

    Raises:
        TypeError: if value is neither a date nor a datetime.
    """
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    raise TypeError(f"{name} must be a date or datetime, got {type(value).__name__}: {value!r}")


def bank_dedupe_hash(
    *,
    bank_name: str,
    transaction_date: date,
    credit_amount: Decimal,
    raw_narration: str,
    extracted_utr: Optional[str],
) -> str:
    """
    The deterministic natural key of one bank credit.

    MUST stay byte-for-byte identical to the expression in
    backend/database/migrations/002_hardening.sql:

        bank_name || '|' || COALESCE(extracted_utr, '') || '|' ||
        credit_amount::text || '|' ||
        to_char(transaction_date, 'YYYY-MM-DD') || '|' || raw_narration

    tests/test_database_idempotency.py::test_bank_dedupe_hash_matches_postgres
    asserts that equivalence against a live database, so the Python and SQL
    definitions cannot drift apart unnoticed.

    Why a hash and not a plain multi-column UNIQUE: PostgreSQL treats NULLs as
    distinct in a UNIQUE constraint, so any constraint mentioning
    extracted_utr stops constraining the moment the UTR is unparseable --
    exactly the rows most likely to be re-imported. COALESCE inside the hash
    removes the hole. (PG 15+ offers NULLS NOT DISTINCT, but a single stored
    hash also gives the ingestion path one unambiguous conflict target.)

    Args:
        bank_name: bank identifier, e.g. "HDFC".
        transaction_date: the bank's value date for the credit.
        credit_amount: a Decimal; normalized to two decimal places, so
            Decimal("976.4") and Decimal("976.40") hash identically -- just as
            they are the same NUMERIC(15,2) value in PostgreSQL.
        raw_narration: the untouched statement line.
        extracted_utr: the parsed UTR, or None.

    Returns:
        64-character lowercase hex SHA-256 digest.

    Raises:
        TypeError / ValueError: via core.money.quantize_money if credit_amount
            is a float, non-finite, over-precise, or out of DECIMAL(15,2)
            range; via _canonical_date if transaction_date is not a date.
    """
    canonical = "|".join(
        (
            bank_name,
            extracted_utr or "",
            money_to_str(credit_amount, "credit_amount"),
            _canonical_date(transaction_date, "transaction_date"),
            raw_narration,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reconciliation_state_hash(
    *,
    settlement_id: str,
    recon_state: str,
    variance: Decimal,
    raw_narration: str,
) -> str:
    """
    Tamper-evident fingerprint of the inputs behind one reconciliation verdict.

    Not a security control -- an audit trail. Given the same inputs it always
    reproduces the same hash, so a later reviewer can tell whether a ledger row
    was quietly edited after the fact.

    The variance goes through money_to_str, so Decimal("0") and Decimal("0.00")
    -- the same stored NUMERIC(15,2) -- can never produce two different hashes
    for the same decision. Before that normalization the hash depended on the
    incidental scale of whatever Decimal the caller happened to hold.

    Args:
        settlement_id: the settlement this verdict is about.
        recon_state: the resulting state; must be in RECON_STATES.
        variance: the DETERMINISTIC variance (never the LLM's self-reported
            figure -- that is data being audited, not part of the audit key).
        raw_narration: the bank narration the decision was made against.

    Returns:
        64-character lowercase hex SHA-256 digest.

    Raises:
        ValueError: if recon_state is not a known state, or variance is not a
            valid two-decimal amount.
    """
    if recon_state not in RECON_STATES:
        raise ValueError(
            f"recon_state={recon_state!r} is not a known reconciliation state. "
            f"Legal values: {sorted(RECON_STATES)}"
        )
    canonical = "|".join(
        (settlement_id, recon_state, money_to_str(variance, "variance"), raw_narration)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    """Shared declarative base for every ORM model in this module."""

    pass


# ---------------------------------------------------------------------------
# 1. Razorpay Settlements Ledger
# ---------------------------------------------------------------------------
class RazorpaySettlement(Base):
    """
    One row per Razorpay settlement (from a `settlement.processed` webhook or a
    backfill import). Mirrors t_razorpay_settlements.

    All five monetary inputs and net_settlement are Decimal, never float.
    ck_settlement_accounting_equation additionally enforces
    Net = Gross - Fees - Taxes - Refunds - Adjustments in the database, so no
    code path -- ORM, raw SQL, or a future importer -- can persist a settlement
    whose own numbers disagree with the equation the rules engine computes.
    """

    __tablename__ = "t_razorpay_settlements"

    internal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    settlement_id: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    taxes: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    refunds: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    adjustments: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=Decimal("0.00"))
    net_settlement: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    utr_reference: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("gross_amount >= 0", name="ck_settlements_gross_nonneg"),
        CheckConstraint("fees >= 0", name="ck_settlements_fees_nonneg"),
        CheckConstraint("taxes >= 0", name="ck_settlements_taxes_nonneg"),
        CheckConstraint("refunds >= 0", name="ck_settlements_refunds_nonneg"),
        CheckConstraint(
            "net_settlement = gross_amount - fees - taxes - refunds - adjustments",
            name="ck_settlement_accounting_equation",
        ),
        CheckConstraint("length(trim(status)) > 0", name="ck_settlement_status_nonempty"),
    )


# ---------------------------------------------------------------------------
# 2. Bank Ledger
# ---------------------------------------------------------------------------
class BankLedgerEntry(Base):
    """
    One row per normalized bank credit, ingested from an MT940 statement or a
    narration feed (see backend/parsers/mt940_processor.py). Mirrors
    t_bank_ledger.

    dedupe_hash is the physical guard against double-counting: re-importing the
    same statement file hits ON CONFLICT on the second pass instead of
    duplicating the credit, and unlike the old
    (bank_name, extracted_utr, credit_amount) constraint it keeps working when
    the UTR is NULL. See bank_dedupe_hash() and get_or_create_bank_entry().

    ux_bank_ledger_utr additionally makes a parsed UTR exclusive per bank: one
    real NEFT/RTGS transfer is one bank credit, so a second row claiming the
    same UTR is a conflict to surface, not a row to insert.
    """

    __tablename__ = "t_bank_ledger"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    bank_name: Mapped[str] = mapped_column(String(50), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    raw_narration: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_utr: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("dedupe_hash", name="unique_bank_dedupe_hash"),
        CheckConstraint("credit_amount >= 0", name="ck_bank_ledger_credit_nonneg"),
        Index(
            "ux_bank_ledger_utr",
            "bank_name",
            "extracted_utr",
            unique=True,
            postgresql_where=text("extracted_utr IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# 3. Reconciliation Audit Trail
# ---------------------------------------------------------------------------
class ReconciliationLedger(Base):
    """
    One row per settlement once it has been through the deterministic rules
    engine and/or the LangGraph agent. Mirrors t_reconciliation_ledger.

    UNIQUE(settlement_id) makes re-running the pipeline converge instead of
    duplicating; UNIQUE(bank_entry_id) makes one real bank credit back at most
    one settlement.

    Attributes worth calling out:
        numeric_variance: the DETERMINISTIC variance from the rules engine.
            This column used to hold whatever variance the LLM reported about
            itself, which made every downstream total a claim by the model
            rather than a measurement.
        ai_reported_variance: the model's self-reported figure, quarantined
            here so the two can be compared instead of conflated.
        ai_confidence_score: the model's self-reported confidence, 0-1
            (ck_recon_confidence_range).
        agent_thread_id: the LangGraph thread this row's reasoning lives on.
            Without it, PENDING_HITL_REVIEW was a terminal state -- there was
            no way to resume the paused graph and finish the review.
        human_decision / human_decision_by / human_decision_at: the audit
            trail of the review. ck_recon_human_decision enforces that a
            decision, its timestamp, and the matching terminal state always
            appear together.
        batch_run_id: which evaluation run produced this row, so metrics can be
            scoped by an indexed column instead of a LIKE over a business key.
    """

    __tablename__ = "t_reconciliation_ledger"

    recon_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    settlement_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("t_razorpay_settlements.settlement_id", ondelete="RESTRICT"),
        nullable=False,
    )
    bank_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("t_bank_ledger.entry_id", ondelete="RESTRICT"),
        nullable=True,
    )
    recon_state: Mapped[str] = mapped_column(String(30), nullable=False)
    numeric_variance: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=Decimal("0.00")
    )
    ai_classification_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ai_reported_variance: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    ai_confidence_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), nullable=True)
    agent_thread_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    human_decision: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    human_decision_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    human_decision_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    batch_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    cryptographic_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("settlement_id", name="unique_settlement_recon"),
        UniqueConstraint("bank_entry_id", name="unique_recon_bank_entry"),
    )


# ---------------------------------------------------------------------------
# Idempotent write helpers -- the row-safe patterns every phase (parsers,
# rules engine, HITL service, evaluation runner) reuses rather than
# hand-rolling its own INSERT/UPDATE logic.
# ---------------------------------------------------------------------------
async def get_or_create_bank_entry(
    session: AsyncSession,
    *,
    bank_name: str,
    transaction_date: date,
    credit_amount: Decimal,
    raw_narration: str,
    extracted_utr: Optional[str],
) -> tuple[BankLedgerEntry, bool]:
    """
    Idempotently insert a bank ledger row, tolerating re-imports of the same
    statement file and concurrent parser runs.

    Args:
        session: an active AsyncSession.
        bank_name: bank identifier, e.g. "HDFC", "ICICI".
        transaction_date: the bank's value date for the credit.
        credit_amount: a Decimal (never a float); normalized to two decimals.
        raw_narration: the untouched bank statement line, kept for audit.
        extracted_utr: UTR parsed from raw_narration, or None if unparseable.

    Returns:
        (entry, created). created is False when this exact credit -- same bank,
        UTR, amount, value date, and narration -- was already ingested, meaning
        the caller should treat it as "already seen" rather than double-counting
        it downstream.

    Raises:
        BankLedgerConflictError: if a DIFFERENT credit already claims this
            (bank_name, extracted_utr). One real NEFT/RTGS transfer is one
            credit; two rows disagreeing about its amount, date, or narration is
            a data conflict a human must look at, not something to merge. Raised
            from inside a SAVEPOINT, so the caller's surrounding transaction
            remains usable and a batch can record this record's failure and
            carry on.
        RuntimeError: if the conflicting row vanished between the failed insert
            and the re-read (a delete racing an import). A hard failure rather
            than returning None to a caller that expects a row.
        TypeError / ValueError: from quantize_money, if credit_amount is a
            float, non-finite, or over-precise.
    """
    amount = quantize_money(credit_amount, "credit_amount")
    dedupe = bank_dedupe_hash(
        bank_name=bank_name,
        transaction_date=transaction_date,
        credit_amount=amount,
        raw_narration=raw_narration,
        extracted_utr=extracted_utr,
    )

    stmt = (
        pg_insert(BankLedgerEntry)
        .values(
            bank_name=bank_name,
            transaction_date=transaction_date,
            credit_amount=amount,
            raw_narration=raw_narration,
            extracted_utr=extracted_utr,
            dedupe_hash=dedupe,
        )
        .on_conflict_do_nothing(constraint="unique_bank_dedupe_hash")
        .returning(BankLedgerEntry)
    )

    try:
        async with session.begin_nested():
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
    except IntegrityError as exc:
        detail = str(getattr(exc, "orig", exc))
        if "ux_bank_ledger_utr" in detail:
            raise BankLedgerConflictError(
                f"bank credit ({bank_name}, UTR {extracted_utr}) conflicts with an "
                "existing credit that already claims that UTR but has a different "
                "amount, value date, or narration. One UTR is one transfer -- this "
                "needs a human decision, not a silent merge."
            ) from exc
        raise BankLedgerConflictError(
            f"bank credit ({bank_name}, UTR {extracted_utr}, {money_to_str(amount)}) "
            f"violated a uniqueness constraint: {detail}"
        ) from exc

    if row is not None:
        return row, True

    existing = await session.scalar(
        select(BankLedgerEntry).where(BankLedgerEntry.dedupe_hash == dedupe)
    )
    if existing is None:
        raise RuntimeError(
            f"bank ledger conflict on dedupe_hash {dedupe} but no matching row on "
            "re-read -- the conflicting row was deleted mid-import."
        )
    return existing, False


async def mark_bank_entry_reconciled(session: AsyncSession, entry_id: uuid.UUID) -> bool:
    """
    Flag a bank credit as reconciled, idempotently.

    is_reconciled existed but was never written by any code path, so every one
    of the 264 live bank rows read as unreconciled no matter how many
    settlements had been matched against them -- an operator query like
    "which credits are still outstanding?" returned everything.

    Args:
        session: an active AsyncSession.
        entry_id: the bank ledger row to flag.

    Returns:
        True if this call flipped the flag, False if it was already set (a
        second call is a no-op, not an error).
    """
    result = await session.execute(
        update(BankLedgerEntry)
        .where(BankLedgerEntry.entry_id == entry_id, BankLedgerEntry.is_reconciled.is_(False))
        .values(is_reconciled=True)
    )
    return bool(result.rowcount)


async def upsert_settlement(
    session: AsyncSession,
    *,
    settlement_id: str,
    status: str,
    gross_amount: Decimal,
    fees: Decimal,
    taxes: Decimal,
    refunds: Decimal,
    adjustments: Decimal,
    net_settlement: Decimal,
    utr_reference: Optional[str],
) -> RazorpaySettlement:
    """
    Insert or converge one settlement row, so re-running an import is a no-op
    rather than an IntegrityError.

    The previous code path was a plain session.add(RazorpaySettlement(...)),
    which meant re-running the batch with the same run id aborted the record
    with a duplicate-key error and counted it as a processing failure.

    Args:
        session: an active AsyncSession.
        settlement_id: the Razorpay settlement id (the business key).
        status: settlement status, e.g. "processed". Must be non-blank.
        gross_amount, fees, taxes, refunds, adjustments: Decimal components.
        net_settlement: the net payout. MUST equal
            gross - fees - taxes - refunds - adjustments; checked here so the
            caller gets a named error instead of a raw CHECK violation.
        utr_reference: the UTR Razorpay reported for the payout, or None.

    Returns:
        The persisted RazorpaySettlement row.

    Raises:
        ValueError: if status is blank, or the accounting equation does not
            hold for the amounts supplied.
        TypeError / ValueError: from quantize_money for any float, non-finite,
            or over-precise amount.
    """
    if not status or not status.strip():
        raise ValueError(f"status for settlement {settlement_id!r} must be a non-blank string")

    gross = quantize_money(gross_amount, "gross_amount")
    fee = quantize_money(fees, "fees")
    tax = quantize_money(taxes, "taxes")
    refund = quantize_money(refunds, "refunds")
    adjustment = quantize_money(adjustments, "adjustments")
    net = quantize_money(net_settlement, "net_settlement")

    expected = quantize_money(gross - fee - tax - refund - adjustment, "expected_net")
    if net != expected:
        raise ValueError(
            f"settlement {settlement_id!r} violates the accounting equation: "
            f"net_settlement {money_to_str(net)} != gross {money_to_str(gross)} "
            f"- fees {money_to_str(fee)} - taxes {money_to_str(tax)} "
            f"- refunds {money_to_str(refund)} - adjustments {money_to_str(adjustment)} "
            f"= {money_to_str(expected)}"
        )

    values = dict(
        settlement_id=settlement_id,
        status=status.strip(),
        gross_amount=gross,
        fees=fee,
        taxes=tax,
        refunds=refund,
        adjustments=adjustment,
        net_settlement=net,
        utr_reference=utr_reference,
    )

    stmt = (
        pg_insert(RazorpaySettlement)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["settlement_id"],
            set_={k: v for k, v in values.items() if k != "settlement_id"},
        )
        .returning(RazorpaySettlement)
    )
    row = (await session.execute(stmt)).scalar_one()
    return row


async def upsert_reconciliation(
    session: AsyncSession,
    *,
    settlement_id: str,
    bank_entry_id: Optional[uuid.UUID],
    recon_state: str,
    numeric_variance: Decimal,
    cryptographic_state_hash: str,
    ai_classification_reason: Optional[str] = None,
    ai_reported_variance: Optional[Decimal] = None,
    ai_confidence_score: Optional[Decimal] = None,
    agent_thread_id: Optional[str] = None,
    batch_run_id: Optional[str] = None,
) -> tuple[ReconciliationLedger, bool]:
    """
    Insert or converge the reconciliation verdict for one settlement.

    Safety properties, in order of importance:
        1. A row whose recon_state is already HITL_APPROVED or HITL_REJECTED is
           NEVER overwritten. A pipeline re-run over a settlement a human has
           already ruled on leaves that verdict exactly as it was -- silently
           re-deciding it would erase the audit trail the review exists to
           create.
        2. numeric_variance is the deterministic variance. The model's own
           figure goes to ai_reported_variance so the two can be compared.
        3. A bank credit already reconciled against a different settlement is
           refused (unique_recon_bank_entry) rather than double-counted.

    Args:
        session: an active AsyncSession.
        settlement_id: the settlement being reconciled; must already exist
            (foreign key).
        bank_entry_id: the bank credit this settlement is matched to, or None
            when no credit could be identified.
        recon_state: one of RECON_STATES.
        numeric_variance: the deterministic variance, as a Decimal.
        cryptographic_state_hash: from reconciliation_state_hash().
        ai_classification_reason: the agent's category, or None for a purely
            deterministic match.
        ai_reported_variance: the agent's self-reported variance, or None.
        ai_confidence_score: the agent's self-reported confidence, 0-1, or None.
        agent_thread_id: the LangGraph thread id, required in practice for any
            row that may need resuming (PENDING_HITL_REVIEW).
        batch_run_id: the evaluation run that produced this row.

    Returns:
        (row, written). written is False only when an existing row was left
        untouched because a human had already decided it.

    Raises:
        ValueError: if recon_state is not in RECON_STATES, or a terminal HITL
            state is passed here (those are produced only by
            record_human_decision, never by the pipeline).
        BankLedgerConflictError: if bank_entry_id is already reconciled against
            a different settlement.
    """
    if recon_state not in RECON_STATES:
        raise ValueError(
            f"recon_state={recon_state!r} is not a known reconciliation state. "
            f"Legal values: {sorted(RECON_STATES)}"
        )
    if recon_state in TERMINAL_HITL_STATES:
        raise ValueError(
            f"recon_state={recon_state!r} records a human decision and may only be "
            "set through record_human_decision(), which writes the decision, its "
            "author, and its timestamp atomically with the state."
        )

    values = dict(
        settlement_id=settlement_id,
        bank_entry_id=bank_entry_id,
        recon_state=recon_state,
        numeric_variance=quantize_money(numeric_variance, "numeric_variance"),
        ai_classification_reason=ai_classification_reason,
        ai_reported_variance=(
            None if ai_reported_variance is None
            else quantize_money(ai_reported_variance, "ai_reported_variance")
        ),
        ai_confidence_score=ai_confidence_score,
        agent_thread_id=agent_thread_id,
        batch_run_id=batch_run_id,
        cryptographic_state_hash=cryptographic_state_hash,
    )

    stmt = (
        pg_insert(ReconciliationLedger)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["settlement_id"],
            set_={k: v for k, v in values.items() if k != "settlement_id"},
            # The guard that protects a completed review from a pipeline re-run.
            where=~ReconciliationLedger.recon_state.in_(sorted(TERMINAL_HITL_STATES)),
        )
        .returning(ReconciliationLedger)
    )

    try:
        async with session.begin_nested():
            row = (await session.execute(stmt)).scalar_one_or_none()
    except IntegrityError as exc:
        detail = str(getattr(exc, "orig", exc))
        if "unique_recon_bank_entry" in detail:
            raise BankLedgerConflictError(
                f"bank credit {bank_entry_id} is already reconciled against a "
                f"different settlement, so settlement {settlement_id!r} cannot claim "
                "it too -- that would count one real payout twice."
            ) from exc
        raise

    if row is not None:
        return row, True

    # The DO UPDATE's WHERE excluded this row: a human decision is already
    # recorded and must survive. Return it untouched so the caller can report
    # "left alone" rather than believing it wrote something.
    existing = await session.scalar(
        select(ReconciliationLedger).where(ReconciliationLedger.settlement_id == settlement_id)
    )
    if existing is None:
        raise RuntimeError(
            f"reconciliation upsert for {settlement_id!r} neither inserted nor found an "
            "existing row -- the conflicting row was deleted mid-write."
        )
    return existing, False


async def get_settlement_for_update(
    session: AsyncSession, settlement_id: str
) -> Optional[RazorpaySettlement]:
    """
    Fetch a settlement row with a row-level lock held for the rest of the
    current transaction (SELECT ... FOR UPDATE), so two concurrent
    reconciliation workers can never process the same settlement at once.

    Args:
        session: an active AsyncSession inside a transaction that will commit or
            roll back promptly. Holding this lock across a slow LLM call
            serializes the whole pipeline -- fetch, decide, write, and commit in
            one tight block.
        settlement_id: the Razorpay settlement_id to lock.

    Returns:
        The locked row, or None if the settlement has not been ingested (which
        callers should treat as "not ingested", not as a locking failure).

    Exception vectors handled:
        If another transaction holds the lock, PostgreSQL blocks until it
        commits or rolls back rather than raising. Callers needing a hard
        deadline should wrap this in asyncio.wait_for().
    """
    return await session.scalar(
        select(RazorpaySettlement)
        .where(RazorpaySettlement.settlement_id == settlement_id)
        .with_for_update()
    )


async def get_reconciliation_for_update(
    session: AsyncSession, settlement_id: str
) -> Optional[ReconciliationLedger]:
    """
    Fetch a reconciliation row with SELECT ... FOR UPDATE.

    This is what makes recording a human decision safe under concurrency: two
    reviewers (or one impatient double-click) serialize on this lock, so the
    second attempt sees the first one's committed decision instead of racing it.

    Args:
        session: an active AsyncSession inside a short transaction.
        settlement_id: the settlement whose reconciliation row to lock.

    Returns:
        The locked row, or None if this settlement has not been reconciled yet.
    """
    return await session.scalar(
        select(ReconciliationLedger)
        .where(ReconciliationLedger.settlement_id == settlement_id)
        .with_for_update()
    )


async def list_pending_hitl(
    session: AsyncSession, *, limit: int = 100, batch_run_id: Optional[str] = None
) -> Sequence[ReconciliationLedger]:
    """
    The human review queue: every reconciliation awaiting a decision, oldest
    first.

    Backed by the ix_recon_pending_hitl partial index, so this stays cheap even
    when the pending set is a tiny fraction of the table. This is the read a
    Phase 6 API/UI will expose; it lives here so the query and its index stay
    together.

    Args:
        session: an active AsyncSession.
        limit: maximum rows to return.
        batch_run_id: restrict to one evaluation run, or None for all.

    Returns:
        Rows in PENDING_HITL_REVIEW, oldest resolved_at first.
    """
    query = select(ReconciliationLedger).where(
        ReconciliationLedger.recon_state == "PENDING_HITL_REVIEW"
    )
    if batch_run_id is not None:
        query = query.where(ReconciliationLedger.batch_run_id == batch_run_id)
    result = await session.scalars(query.order_by(ReconciliationLedger.resolved_at).limit(limit))
    return result.all()


async def record_human_decision(
    session: AsyncSession,
    *,
    settlement_id: str,
    decision: str,
    decided_by: str,
) -> tuple[ReconciliationLedger, bool]:
    """
    Record a reviewer's verdict on a pending reconciliation, atomically and
    idempotently.

    This is the database half of the HITL flow; agents/hitl.py drives the
    LangGraph side and calls this to persist the outcome. Together they are what
    turn PENDING_HITL_REVIEW from a dead end into a completable state.

    Concurrency and repeat safety:
        The row is locked with SELECT ... FOR UPDATE first, and the UPDATE
        itself is guarded by `recon_state = 'PENDING_HITL_REVIEW'`. So a
        double-submit, a retried HTTP request, or two reviewers acting at once
        cannot produce two decisions: the first wins, and a repeat of the SAME
        decision returns the existing row with written=False instead of
        rewriting it or raising.

    Args:
        session: an active AsyncSession. The caller commits.
        settlement_id: the settlement under review.
        decision: "APPROVED" or "REJECTED" (case-insensitive).
        decided_by: who decided. Required -- an unattributed decision is not an
            audit trail.

    Returns:
        (row, written). written is False when this exact decision was already
        recorded (a safe repeat).

    Raises:
        ValueError: if decision is not APPROVED/REJECTED, or decided_by is
            blank.
        HumanDecisionConflictError: if the settlement has no reconciliation row,
            if it is not awaiting review, or if a DIFFERENT decision is already
            recorded against it.
    """
    normalized = (decision or "").strip().upper()
    if normalized not in HUMAN_DECISION_STATES:
        raise ValueError(
            f"decision={decision!r} is not a legal human verdict. "
            f"Legal values: {sorted(HUMAN_DECISION_STATES)}"
        )
    if not decided_by or not decided_by.strip():
        raise ValueError("decided_by must name the reviewer -- an unattributed decision is not auditable")

    row = await get_reconciliation_for_update(session, settlement_id)
    if row is None:
        raise HumanDecisionConflictError(
            f"settlement {settlement_id!r} has no reconciliation row, so there is "
            "nothing under review to decide."
        )

    if row.recon_state in TERMINAL_HITL_STATES:
        if row.human_decision == normalized:
            return row, False
        raise HumanDecisionConflictError(
            f"settlement {settlement_id!r} is already {row.human_decision} "
            f"(by {row.human_decision_by} at {row.human_decision_at}); refusing to "
            f"overwrite it with {normalized}."
        )

    if row.recon_state != "PENDING_HITL_REVIEW":
        raise HumanDecisionConflictError(
            f"settlement {settlement_id!r} is in state {row.recon_state}, not "
            "PENDING_HITL_REVIEW -- only a settlement actually awaiting review can "
            "be decided."
        )

    result = await session.execute(
        update(ReconciliationLedger)
        .where(
            ReconciliationLedger.settlement_id == settlement_id,
            ReconciliationLedger.recon_state == "PENDING_HITL_REVIEW",
        )
        .values(
            recon_state=HUMAN_DECISION_STATES[normalized],
            human_decision=normalized,
            human_decision_by=decided_by.strip(),
            human_decision_at=func.now(),
        )
    )
    if not result.rowcount:
        # Cannot happen while we hold the FOR UPDATE lock; if it ever does, the
        # invariant is broken and silence would be worse than a loud failure.
        raise HumanDecisionConflictError(
            f"decision for {settlement_id!r} was not applied even though the row was "
            "locked in PENDING_HITL_REVIEW -- concurrent modification detected."
        )

    await session.refresh(row)
    return row, True
