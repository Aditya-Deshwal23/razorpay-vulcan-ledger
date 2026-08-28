"""
Tests for the database layer's idempotency and double-counting guards.

These run against the REAL Postgres database, because every property here IS a
database property: a UNIQUE constraint's treatment of NULLs, an ON CONFLICT
target, a SAVEPOINT keeping a session usable after an IntegrityError. An
in-memory double would assert only that this module's own mocks agree with each
other.

test_bank_dedupe_hash_matches_postgres is the one the docstring of
config.database.bank_dedupe_hash points at by name: the Python hash and the SQL
expression that backfilled the column in 002_hardening.sql must agree, or rows
imported by the application and rows repaired by the migration would carry
different natural keys and neither would deduplicate against the other.

Every test writes under a unique run id and deletes its own rows.
"""
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from config.database import (
    AsyncSessionLocal,
    BankLedgerConflictError,
    bank_dedupe_hash,
    get_or_create_bank_entry,
    get_settlement_for_update,
    mark_bank_entry_reconciled,
    reconciliation_state_hash,
    record_human_decision,
    upsert_reconciliation,
    upsert_settlement,
)

# The migration's backfill expression, reproduced here so the equivalence test
# can evaluate it in Postgres. test_dedupe_expression_is_still_the_migrations
# asserts this text really is the one in 002_hardening.sql, so a future edit to
# the migration cannot leave this copy silently stale.
_MIGRATION_DEDUPE_SQL = """bank_name || '|' ||
            COALESCE(extracted_utr, '') || '|' ||
            credit_amount::text || '|' ||
            to_char(transaction_date, 'YYYY-MM-DD') || '|' ||
            raw_narration"""

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "database" / "migrations" / "002_hardening.sql"
)

# The same expression, run against bind parameters instead of columns. The casts
# are not embellishment: they reproduce the column types the migration's version
# read from (VARCHAR, NUMERIC(15,2), DATE), and NUMERIC(15,2) is the one that
# matters -- it is what makes 976.4 render as "976.40", exactly as the stored
# column would, and exactly as core.money.money_to_str does on the Python side.
_DEDUPE_HASH_PROBE = """
SELECT encode(
    digest(
        CAST(:bank_name AS text) || '|' ||
        COALESCE(CAST(:extracted_utr AS text), '') || '|' ||
        CAST(CAST(:credit_amount AS numeric(15,2)) AS text) || '|' ||
        to_char(CAST(:transaction_date AS date), 'YYYY-MM-DD') || '|' ||
        CAST(:raw_narration AS text),
        'sha256'
    ),
    'hex'
)
"""


async def _cleanup(run_id: str) -> None:
    """Delete every row a test wrote, in foreign-key-safe order."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM t_reconciliation_ledger WHERE settlement_id LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_bank_ledger WHERE raw_narration LIKE :p OR extracted_utr LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.commit()


async def _seed_settlement(session, settlement_id: str, *, utr=None) -> None:
    """One settlement whose arithmetic balances, for tests that need a valid FK."""
    await upsert_settlement(
        session,
        settlement_id=settlement_id,
        status="processed",
        gross_amount=Decimal("1000.00"),
        fees=Decimal("20.00"),
        taxes=Decimal("3.60"),
        refunds=Decimal("0.00"),
        adjustments=Decimal("0.00"),
        net_settlement=Decimal("976.40"),
        utr_reference=utr,
    )


def test_dedupe_expression_is_still_the_migrations():
    """
    Guard on the guard: the equivalence test below evaluates a copy of the
    migration's expression, so that copy has to be provably the same text. If
    002_hardening.sql is ever edited, this fails first and names the reason,
    rather than the equivalence test passing against a stale expression.
    """
    migration_sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert _MIGRATION_DEDUPE_SQL in migration_sql, (
        "the dedupe expression in 002_hardening.sql no longer matches the copy in "
        "this test file; update both, and check bank_dedupe_hash() agrees"
    )


@pytest.mark.parametrize(
    "bank_name,txn_date,amount,narration,utr",
    [
        # The ordinary case.
        ("HDFC", date(2026, 8, 26), Decimal("976.40"), "/INF/NEFT CR:HDFCX0000001/ACME", "HDFCX0000001"),
        # UTR-less: COALESCE(extracted_utr, '') is the half of the expression that
        # exists precisely for this row, and the half a plain UNIQUE could not do.
        ("ICICI", date(2026, 1, 1), Decimal("100.00"), "random credit no reference", None),
        # A narration containing the '|' separator itself, plus non-ASCII: both
        # sides must agree on UTF-8 bytes, not just on ASCII.
        ("AXIS", date(2025, 12, 31), Decimal("0.01"), "a|b|c ₹ payment", "AXISX0000002"),
        # Trailing-zero scale: Decimal("976.4") and NUMERIC(15,2) must render the
        # same way, or the same credit hashes two ways.
        ("HDFC", date(2026, 2, 28), Decimal("976.4"), "scale check", "HDFCX0000003"),
        # Largest amount DECIMAL(15,2) holds, and a negative-free upper bound check.
        ("HDFC", date(2026, 6, 15), Decimal("9999999999999.99"), "upper bound", None),
    ],
)
@pytest.mark.asyncio
async def test_bank_dedupe_hash_matches_postgres(bank_name, txn_date, amount, narration, utr):
    """
    The Python natural key and the SQL one must be the same string.

    Application inserts compute dedupe_hash in Python; 002_hardening.sql
    backfilled the pre-existing 264 rows with the SQL expression. If the two
    disagree by so much as a separator, a re-import of an already-ingested
    statement inserts a second copy of a credit that is already in the ledger --
    which is the double-count the column exists to prevent.
    """
    async with AsyncSessionLocal() as session:
        postgres_hash = await session.scalar(
            text(_DEDUPE_HASH_PROBE).bindparams(
                bank_name=bank_name,
                extracted_utr=utr,
                credit_amount=amount,
                transaction_date=txn_date,
                raw_narration=narration,
            )
        )

    assert postgres_hash == bank_dedupe_hash(
        bank_name=bank_name,
        transaction_date=txn_date,
        credit_amount=amount,
        raw_narration=narration,
        extracted_utr=utr,
    )


@pytest.mark.asyncio
async def test_reimporting_a_utr_less_credit_does_not_double_count():
    """
    The hole the dedupe hash was introduced to close.

    The old guard was UNIQUE (bank_name, extracted_utr, credit_amount). Because
    PostgreSQL treats NULLs as DISTINCT, that constraint stopped constraining the
    moment a UTR could not be parsed -- so re-importing a statement inserted a
    fresh copy of every UTR-less credit, and each copy was another payout the
    ledger believed it had received.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    narration = f"unparseable manual credit {run_id}"
    try:
        async with AsyncSessionLocal() as session:
            first, created_first = await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("500.00"),
                raw_narration=narration,
                extracted_utr=None,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            second, created_second = await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("500.00"),
                raw_narration=narration,
                extracted_utr=None,
            )
            await session.commit()

        assert created_first is True
        assert created_second is False
        assert second.entry_id == first.entry_id

        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                text("SELECT COUNT(*) FROM t_bank_ledger WHERE raw_narration = :n"),
                {"n": narration},
            )
        assert count == 1, "a UTR-less credit was ingested twice"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_decimal_scale_does_not_create_a_second_credit():
    """
    Decimal("500.4") and Decimal("500.40") are the same NUMERIC(15,2), so they
    must be the same credit. The hash normalizes through money_to_str for exactly
    this reason -- otherwise whichever scale a parser happened to produce would
    decide whether a re-import deduplicated.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    narration = f"scale sensitivity {run_id}"
    try:
        async with AsyncSessionLocal() as session:
            first, _ = await get_or_create_bank_entry(
                session,
                bank_name="ICICI",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("500.4"),
                raw_narration=narration,
                extracted_utr=None,
            )
            await session.commit()
        async with AsyncSessionLocal() as session:
            second, created = await get_or_create_bank_entry(
                session,
                bank_name="ICICI",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("500.40"),
                raw_narration=narration,
                extracted_utr=None,
            )
            await session.commit()

        assert created is False
        assert second.entry_id == first.entry_id
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_one_utr_cannot_be_two_different_credits():
    """
    One UTR is one real transfer. A second row claiming the same (bank, UTR) with
    a different amount is a data conflict -- the statement and our ledger disagree
    about what the bank sent -- so it is refused rather than merged or duplicated.

    The session must remain usable afterwards: the insert runs inside a SAVEPOINT
    so a batch can record this one record's failure and carry on with the rest.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    utr = f"HDFC{run_id}0001"
    try:
        async with AsyncSessionLocal() as session:
            await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("976.40"),
                raw_narration=f"/INF/NEFT CR:{utr}/ACME {run_id}",
                extracted_utr=utr,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            with pytest.raises(BankLedgerConflictError, match="one UTR is one transfer|One UTR is one transfer"):
                await get_or_create_bank_entry(
                    session,
                    bank_name="HDFC",
                    transaction_date=date(2026, 8, 26),
                    credit_amount=Decimal("1976.40"),  # a different amount
                    raw_narration=f"/INF/NEFT CR:{utr}/ACME {run_id} restated",
                    extracted_utr=utr,
                )
            # The savepoint claim, verified rather than assumed: the surrounding
            # transaction still works after the failed insert.
            assert await session.scalar(text("SELECT 1")) == 1
            await session.rollback()

        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                text("SELECT COUNT(*) FROM t_bank_ledger WHERE extracted_utr = :u"), {"u": utr}
            )
        assert count == 1
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_two_settlements_cannot_claim_the_same_bank_credit():
    """
    The constraint that stops one real payout backing two settlements. Without
    unique_recon_bank_entry, a matcher that guessed wrong once would let the same
    bank credit satisfy two settlements, and the ledger would report both as
    reconciled while only one payout ever arrived.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    first_id, second_id = f"setl_claim_a_{run_id}", f"setl_claim_b_{run_id}"
    try:
        async with AsyncSessionLocal() as session:
            entry, _ = await get_or_create_bank_entry(
                session,
                bank_name="AXIS",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("976.40"),
                raw_narration=f"single credit {run_id}",
                extracted_utr=None,
            )
            await _seed_settlement(session, first_id)
            await _seed_settlement(session, second_id)
            await session.commit()
            entry_id = entry.entry_id

        async with AsyncSessionLocal() as session:
            await upsert_reconciliation(
                session,
                settlement_id=first_id,
                bank_entry_id=entry_id,
                recon_state="DETERMINISTIC_MATCH",
                numeric_variance=Decimal("0.00"),
                cryptographic_state_hash=reconciliation_state_hash(
                    settlement_id=first_id,
                    recon_state="DETERMINISTIC_MATCH",
                    variance=Decimal("0.00"),
                    raw_narration=f"single credit {run_id}",
                ),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            with pytest.raises(BankLedgerConflictError, match="count one real payout twice"):
                await upsert_reconciliation(
                    session,
                    settlement_id=second_id,
                    bank_entry_id=entry_id,
                    recon_state="DETERMINISTIC_MATCH",
                    numeric_variance=Decimal("0.00"),
                    cryptographic_state_hash=reconciliation_state_hash(
                        settlement_id=second_id,
                        recon_state="DETERMINISTIC_MATCH",
                        variance=Decimal("0.00"),
                        raw_narration=f"single credit {run_id}",
                    ),
                )
            await session.rollback()

        async with AsyncSessionLocal() as session:
            claimants = await session.scalar(
                text("SELECT COUNT(*) FROM t_reconciliation_ledger WHERE bank_entry_id = :e"),
                {"e": entry_id},
            )
        assert claimants == 1
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_marking_a_credit_reconciled_is_idempotent_and_answerable():
    """
    is_reconciled has to be written by the code, not just declared in the schema.
    It existed as a column that nothing ever set, so "which credits are still
    outstanding?" answered "all of them" no matter how much had been matched.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        async with AsyncSessionLocal() as session:
            entry, _ = await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date(2026, 8, 26),
                credit_amount=Decimal("12.34"),
                raw_narration=f"flag check {run_id}",
                extracted_utr=None,
            )
            await session.commit()
            entry_id = entry.entry_id

        async with AsyncSessionLocal() as session:
            first = await mark_bank_entry_reconciled(session, entry_id)
            await session.commit()
        async with AsyncSessionLocal() as session:
            second = await mark_bank_entry_reconciled(session, entry_id)
            await session.commit()

        assert first is True, "the first call did not flip the flag"
        assert second is False, "a repeat call claimed to have changed something"

        async with AsyncSessionLocal() as session:
            flag = await session.scalar(
                text("SELECT is_reconciled FROM t_bank_ledger WHERE entry_id = :e"), {"e": entry_id}
            )
        assert flag is True
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_pipeline_rerun_never_overwrites_a_recorded_human_verdict():
    """
    The single most destructive thing a re-run could do: re-decide a settlement a
    human already ruled on. upsert_reconciliation's DO UPDATE is guarded on
    recon_state, so a terminal HITL state is left exactly as the reviewer left it
    and the caller is told it wrote nothing.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    settlement_id = f"setl_verdict_{run_id}"
    narration = f"human decided {run_id}"
    try:
        async with AsyncSessionLocal() as session:
            await _seed_settlement(session, settlement_id)
            await upsert_reconciliation(
                session,
                settlement_id=settlement_id,
                bank_entry_id=None,
                recon_state="PENDING_HITL_REVIEW",
                numeric_variance=Decimal("876.40"),
                cryptographic_state_hash=reconciliation_state_hash(
                    settlement_id=settlement_id,
                    recon_state="PENDING_HITL_REVIEW",
                    variance=Decimal("876.40"),
                    raw_narration=narration,
                ),
                ai_classification_reason="UNKNOWN_UNRESOLVABLE",
                batch_run_id=run_id,
            )
            await record_human_decision(
                session,
                settlement_id=settlement_id,
                decision="REJECTED",
                decided_by="ops.reviewer",
            )
            await session.commit()

        # The pipeline runs again over the same settlement, as it would after a
        # restart, and tries to write a fresh deterministic verdict.
        async with AsyncSessionLocal() as session:
            row, written = await upsert_reconciliation(
                session,
                settlement_id=settlement_id,
                bank_entry_id=None,
                recon_state="DETERMINISTIC_MATCH",
                numeric_variance=Decimal("0.00"),
                cryptographic_state_hash=reconciliation_state_hash(
                    settlement_id=settlement_id,
                    recon_state="DETERMINISTIC_MATCH",
                    variance=Decimal("0.00"),
                    raw_narration=narration,
                ),
                batch_run_id=run_id,
            )
            await session.commit()

        assert written is False, "the re-run believed it had written over a human verdict"
        assert row.recon_state == "HITL_REJECTED"

        async with AsyncSessionLocal() as session:
            persisted = (
                await session.execute(
                    text(
                        "SELECT recon_state, human_decision, human_decision_by, numeric_variance "
                        "FROM t_reconciliation_ledger WHERE settlement_id = :s"
                    ),
                    {"s": settlement_id},
                )
            ).one()
        assert persisted.recon_state == "HITL_REJECTED"
        assert persisted.human_decision == "REJECTED"
        assert persisted.human_decision_by == "ops.reviewer"
        # Even the variance is untouched: the re-run's 0.00 did not land.
        assert persisted.numeric_variance == Decimal("876.40")
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_floats_are_refused_at_the_persistence_boundary():
    """
    A float never reaches a money column. 0.1 + 0.2 is the canonical
    demonstration: it is not 0.3, and by the time it has been rounded into
    NUMERIC(15,2) the error is invisible and permanent. The boundary rejects the
    type outright rather than trying to decide when the loss is small enough.
    """
    async with AsyncSessionLocal() as session:
        with pytest.raises((TypeError, ValueError)):
            await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date(2026, 8, 26),
                credit_amount=0.1 + 0.2,  # a float, on purpose
                raw_narration="float boundary",
                extracted_utr=None,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_settlement_upsert_converges_and_locks():
    """
    Re-ingesting the same settlement updates the one row rather than inserting a
    second, and the row can then be locked FOR UPDATE -- the two properties every
    write path in the pipeline depends on.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    settlement_id = f"setl_converge_{run_id}"
    try:
        async with AsyncSessionLocal() as session:
            await _seed_settlement(session, settlement_id)
            await session.commit()

        async with AsyncSessionLocal() as session:
            await upsert_settlement(
                session,
                settlement_id=settlement_id,
                status="reconciled",  # a genuine change, not a no-op re-write
                gross_amount=Decimal("1000.00"),
                fees=Decimal("20.00"),
                taxes=Decimal("3.60"),
                refunds=Decimal("0.00"),
                adjustments=Decimal("0.00"),
                net_settlement=Decimal("976.40"),
                utr_reference=None,
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                text("SELECT COUNT(*) FROM t_razorpay_settlements WHERE settlement_id = :s"),
                {"s": settlement_id},
            )
            locked = await get_settlement_for_update(session, settlement_id)
            missing = await get_settlement_for_update(session, f"setl_absent_{run_id}")
            await session.commit()

        assert count == 1
        assert locked is not None
        assert locked.status == "reconciled"
        assert isinstance(locked.net_settlement, Decimal), "money came back as something other than Decimal"
        assert locked.net_settlement == Decimal("976.40")
        assert missing is None, "FOR UPDATE on a missing row must be None, not a crash"
    finally:
        await _cleanup(run_id)
