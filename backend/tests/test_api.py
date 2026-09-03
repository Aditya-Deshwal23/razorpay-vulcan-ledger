"""Integration tests for the operations API against the real local Postgres."""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from config.database import (
    AsyncSessionLocal,
    reconciliation_state_hash,
    upsert_reconciliation,
    upsert_settlement,
)
from main import app


async def _seed_review_item(run_id: str) -> str:
    settlement_id = f"setl_api_{run_id}"
    narration = f"/TXT/NEFT-API-{run_id} manual settlement review"
    async with AsyncSessionLocal() as session:
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
            utr_reference=None,
        )
        await upsert_reconciliation(
            session,
            settlement_id=settlement_id,
            bank_entry_id=None,
            recon_state="PENDING_HITL_REVIEW",
            numeric_variance=Decimal("876.40"),
            evidence_narration=narration,
            cryptographic_state_hash=reconciliation_state_hash(
                settlement_id=settlement_id,
                recon_state="PENDING_HITL_REVIEW",
                variance=Decimal("876.40"),
                raw_narration=narration,
            ),
            ai_classification_reason="UNKNOWN_UNRESOLVABLE",
            ai_reported_variance=Decimal("999.99"),
            ai_confidence_score=Decimal("0.120"),
            batch_run_id=run_id,
        )
        await session.commit()
    return settlement_id


async def _cleanup(run_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET LOCAL vulcan.allow_audit_maintenance = 'on'"))
        await session.execute(
            text("DELETE FROM t_reconciliation_events WHERE settlement_id LIKE :pattern"),
            {"pattern": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_reconciliation_ledger WHERE settlement_id LIKE :pattern"),
            {"pattern": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :pattern"),
            {"pattern": f"%{run_id}%"},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_operations_api_exposes_evidence_and_audits_a_decision():
    run_id = f"API{uuid.uuid4().hex[:5].upper()}"
    settlement_id = await _seed_review_item(run_id)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                health = await client.get("/api/health")
                assert health.status_code == 200
                assert health.json()["database"] == "connected"

                summary = await client.get(f"/api/batches/{run_id}/summary")
                assert summary.status_code == 200
                assert summary.json()["needs_review"] == 1
                assert summary.json()["match_rate"] == "0.00"

                queue = await client.get(f"/api/batches/{run_id}/review")
                assert queue.status_code == 200
                assert queue.json()["total"] == 1
                item = queue.json()["items"][0]
                assert item["settlement_id"] == settlement_id
                assert item["deterministic_variance"] == "876.40"
                assert item["ai_reported_variance"] == "999.99"
                assert item["raw_narration"].endswith("manual settlement review")

                ledger = await client.get("/api/settlements", params={"query": settlement_id})
                assert ledger.status_code == 200
                assert ledger.json()["total"] == 1

                detail = await client.get(f"/api/settlements/{settlement_id}")
                assert detail.status_code == 200
                assert detail.json()["expected_net"] == "976.40"
                assert detail.json()["bank_name"] is None

                before = await client.get("/api/audit", params={"batch_run_id": run_id})
                assert [
                    (event["event_type"], event["to_state"])
                    for event in before.json()["items"]
                ] == [("RECONCILIATION_RECORDED", "PENDING_HITL_REVIEW")]

                decision = await client.post(
                    f"/api/settlements/{settlement_id}/decision",
                    json={"decision": "APPROVED", "decided_by": "finance.controller"},
                )
                assert decision.status_code == 200
                assert decision.json()["recon_state"] == "HITL_APPROVED"
                assert decision.json()["newly_recorded"] is True

                same_decision = await client.post(
                    f"/api/settlements/{settlement_id}/decision",
                    json={"decision": "APPROVED", "decided_by": "another.controller"},
                )
                assert same_decision.status_code == 200
                assert same_decision.json()["newly_recorded"] is False

                conflict = await client.post(
                    f"/api/settlements/{settlement_id}/decision",
                    json={"decision": "REJECTED", "decided_by": "another.controller"},
                )
                assert conflict.status_code == 409
                assert "already APPROVED" in conflict.json()["detail"]

                after = await client.get("/api/audit", params={"batch_run_id": run_id})
                assert {(event["event_type"], event["to_state"]) for event in after.json()["items"]} == {
                    ("RECONCILIATION_RECORDED", "PENDING_HITL_REVIEW"),
                    ("HUMAN_DECISION_RECORDED", "HITL_APPROVED"),
                }
    finally:
        await _cleanup(run_id)
