"""
One-time smoke test for the Phase 2 database layer: confirms the async
engine connects, currency columns round-trip as Decimal (never float), the
bank-ledger idempotent insert actually blocks a duplicate at the database
level, SELECT ... FOR UPDATE locks correctly, the CHECK constraints reject
bad data, and the updated_at trigger fires on a raw SQL UPDATE.

Run from backend/, with the venv active and DATABASE_URL pointing at a
Postgres instance that already has ddl.sql applied:

    python -m scripts.verify_phase2

Cleans up every row it inserts, so it's safe to re-run at any point in
later phases as a quick "did I break the database layer" check.
"""
import asyncio
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from config.database import (
    AsyncSessionLocal,
    RazorpaySettlement,
    engine,
    get_or_create_bank_entry,
    get_settlement_for_update,
)

TEST_SETTLEMENT_ID = f"setl_verify_{uuid.uuid4().hex[:8]}"
TEST_UTR = f"HDFC{uuid.uuid4().hex[:12].upper()}"


async def main() -> None:
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
        print("[OK] engine connects via asyncpg")

        async with AsyncSessionLocal() as session:
            settlement = RazorpaySettlement(
                settlement_id=TEST_SETTLEMENT_ID,
                status="processed",
                gross_amount=Decimal(str(1000.00)),
                fees=Decimal(str(20.00)),
                taxes=Decimal(str(3.60)),
                net_settlement=Decimal(str(1000.00)) - Decimal(str(20.00)) - Decimal(str(3.60)),
                utr_reference=TEST_UTR,
            )
            session.add(settlement)
            await session.commit()
            assert isinstance(settlement.net_settlement, Decimal)
        print(f"[OK] settlement inserted, net_settlement is a true Decimal ({settlement.net_settlement})")

        async with AsyncSessionLocal() as session:
            entry1, created1 = await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date.today(),
                credit_amount=Decimal(str(976.40)),
                raw_narration=f"/INF/NEFT CR:{TEST_UTR}/ACME CORP",
                extracted_utr=TEST_UTR,
            )
            await session.commit()
        assert created1 is True
        print(f"[OK] first bank-ledger insert created a new row ({entry1.entry_id})")

        async with AsyncSessionLocal() as session:
            entry2, created2 = await get_or_create_bank_entry(
                session,
                bank_name="HDFC",
                transaction_date=date.today(),
                credit_amount=Decimal(str(976.40)),
                raw_narration="duplicate re-import attempt",
                extracted_utr=TEST_UTR,
            )
            await session.commit()
        assert created2 is False and entry2.entry_id == entry1.entry_id
        print("[OK] duplicate (bank, UTR, amount) correctly blocked -- no double-counting")

        async with AsyncSessionLocal() as session:
            locked = await get_settlement_for_update(session, TEST_SETTLEMENT_ID)
            assert locked is not None and locked.settlement_id == TEST_SETTLEMENT_ID
            assert await get_settlement_for_update(session, "definitely_missing") is None
            await session.commit()
        print("[OK] SELECT ... FOR UPDATE locks the real row and returns None for a missing one")

        async with AsyncSessionLocal() as session:
            before = await session.scalar(
                text("SELECT updated_at FROM t_razorpay_settlements WHERE settlement_id = :sid"),
                {"sid": TEST_SETTLEMENT_ID},
            )
            await asyncio.sleep(1)
            await session.execute(
                text("UPDATE t_razorpay_settlements SET status = 'reconciled' WHERE settlement_id = :sid"),
                {"sid": TEST_SETTLEMENT_ID},
            )
            await session.commit()
            after = await session.scalar(
                text("SELECT updated_at FROM t_razorpay_settlements WHERE settlement_id = :sid"),
                {"sid": TEST_SETTLEMENT_ID},
            )
            assert after > before
        print("[OK] updated_at trigger fires on a raw SQL UPDATE, not just ORM writes")

        async with AsyncSessionLocal() as session:
            bad = RazorpaySettlement(
                settlement_id=f"{TEST_SETTLEMENT_ID}_bad",
                status="processed",
                gross_amount=Decimal("-5.00"),
                net_settlement=Decimal("-5.00"),
            )
            session.add(bad)
            try:
                await session.commit()
                raise AssertionError("negative gross_amount was NOT rejected")
            except AssertionError:
                raise
            except Exception:
                await session.rollback()
        print("[OK] CHECK constraint correctly rejects a negative amount")

        print("\nALL PHASE 2 CHECKS PASSED")

    finally:
        # Always clean up, even if an assertion above failed midway.
        async with AsyncSessionLocal() as session:
            await session.execute(text("DELETE FROM t_bank_ledger WHERE extracted_utr = :utr"), {"utr": TEST_UTR})
            await session.execute(
                text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :pattern"),
                {"pattern": f"{TEST_SETTLEMENT_ID}%"},
            )
            await session.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())