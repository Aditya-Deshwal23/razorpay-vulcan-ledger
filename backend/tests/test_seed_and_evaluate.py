"""
Tests for backend.evaluation.seed_and_evaluate.

test_full_run_persists_and_reports_correctly is the integration test: it
runs the entire 52-record batch through the real parser, the real rules
engine, and a real graph (real Postgres checkpointer, fake LLM), then
verifies both the in-memory metrics AND what actually landed in Postgres
and in exceptions_manifest.json -- not just that the function returned
without raising.
"""
import json
import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from agents.graph import build_graph
from agents.schemas import ExceptionClassification
from config.database import AsyncSessionLocal
from evaluation.seed_and_evaluate import _MANIFEST_PATH, generate_synthetic_batch, run_evaluation
from parsers.mt940_processor import LegacyBankParser

CHECKPOINTER_DATABASE_URL = os.environ.get(
    "TEST_CHECKPOINTER_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/razorpay_vulcan_ledger",
)


class _ScenarioFakeLLM:
    """
    Stands in for the real Gemini call. Routes purely on the
    sanitized_context text seed_and_evaluate.py actually builds: a
    variance of "0.00 INR" means the math already balances (the
    chargeback/delayed-webhook/missing-utr scenarios), which a reasonable
    classifier would auto-approve; anything else is the genuinely
    unexplained anomaly, which should go to a human.
    """

    def __init__(self):
        self.call_count = 0

    async def ainvoke(self, prompt: str) -> ExceptionClassification:
        self.call_count += 1
        if "variance: 0.00 INR" in prompt:
            reason = "DELAYED_WEBHOOK_DELIVERY" if "delay" in prompt else "TEMPORAL_CROSS_SETTLEMENT"
            return ExceptionClassification(
                discrepancy_reason=reason,
                variance_str="0.00",
                suggested_action="AUTO_APPROVE_ADJUSTMENT",
                confidence_score=0.91,
            )
        return ExceptionClassification(
            discrepancy_reason="UNKNOWN_UNRESOLVABLE",
            variance_str="876.40",
            suggested_action="ROUTE_TO_HITL_PANEL",
            confidence_score=0.12,
        )


async def _delete_run_rows(*run_ids: str) -> None:
    """
    Remove every row one or more evaluation runs wrote, so the suite is
    re-runnable and leaves the developer's database as it found it.

    Bank rows are matched on the narration as well as the UTR: the four chaotic
    credits per run are deliberately UTR-less, so matching only on extracted_utr
    leaked them into every subsequent run.
    """
    async with AsyncSessionLocal() as session:
        for run_id in run_ids:
            pattern = f"%{run_id}%"
            await session.execute(
                text("DELETE FROM t_reconciliation_ledger WHERE settlement_id LIKE :p"),
                {"p": pattern},
            )
            await session.execute(
                text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :p"),
                {"p": pattern},
            )
            await session.execute(
                text(
                    "DELETE FROM t_bank_ledger WHERE extracted_utr LIKE :p "
                    "OR raw_narration LIKE :p"
                ),
                {"p": pattern},
            )
        await session.commit()


def test_generate_synthetic_batch_shape():
    batch = generate_synthetic_batch("TESTRNID")
    assert len(batch) == 52

    perfect, chaotic = batch[:48], batch[48:]
    assert len(perfect) == 48
    assert len(chaotic) == 4

    banks_seen = {r.bank_name for r in perfect}
    assert banks_seen == {"HDFC", "ICICI", "AXIS"}

    for record in perfect:
        utr = LegacyBankParser.extract_utr(record.bank_name, record.narration)
        assert utr is not None, f"perfect record for {record.bank_name} did not produce a parseable UTR"
        assert record.gross - record.fees - record.taxes - record.refunds - record.adjustments == record.bank_credit

    chaotic_descs = {r.settlement_id.split("_", 4)[-1] for r in chaotic}
    assert chaotic_descs == {"chargeback", "delayed_webhook", "unresolvable_anomaly", "missing_utr"}

    # Ground truth must be fixed by construction, not derived from a run.
    assert [r.expected_state for r in perfect] == ["DETERMINISTIC_MATCH"] * 48
    assert [r.expected_state for r in chaotic] == [
        "AI_RESOLVED",
        "AI_RESOLVED",
        "PENDING_HITL_REVIEW",
        "AI_RESOLVED",
    ]


def test_chaotic_records_are_utr_less_but_still_run_scoped():
    """
    The regression test for the defect that made a re-run report four spurious
    processing errors.

    The chaotic records must satisfy two properties at once:
      1. No extractable UTR -- that is what those scenarios exist to exercise.
      2. A narration that differs between runs. With no UTR, the bank-ledger
         dedupe hash is computed over the narration, so an unscoped narration
         produced an identical hash on every run; the second run then reused a
         bank credit that a previous run's reconciliation row already claimed,
         and unique_recon_bank_entry correctly refused the double-claim.
    """
    first = generate_synthetic_batch("AAAA1111")[48:]
    second = generate_synthetic_batch("BBBB2222")[48:]

    for record in first + second:
        assert LegacyBankParser.extract_utr(record.bank_name, record.narration) is None

    assert {r.narration for r in first}.isdisjoint({r.narration for r in second})
    # The delayed-webhook scenario is only distinguishable from the chargeback by
    # this word; losing it would silently merge two different classifications.
    assert any("delay" in r.narration for r in first)


@pytest.mark.asyncio
async def test_full_run_persists_and_reports_correctly(capsys, tmp_path):
    from contextlib import asynccontextmanager

    fake_llm = _ScenarioFakeLLM()
    run_id = uuid.uuid4().hex[:8].upper()
    # The published exceptions_manifest.json is a real benchmark artifact. A test
    # run must not overwrite it with synthetic-LLM output, so the manifest goes to
    # a temporary path and the real one is asserted untouched below.
    manifest_path = tmp_path / "exceptions_manifest.json"
    real_manifest_before = _MANIFEST_PATH.read_bytes() if _MANIFEST_PATH.exists() else None

    @asynccontextmanager
    async def fake_graph_context_manager():
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            yield build_graph(fake_llm, checkpointer)

    metrics = await run_evaluation(
        graph_context_manager=fake_graph_context_manager,
        run_id=run_id,
        manifest_path=manifest_path,
    )

    assert metrics["total_processed"] == 52
    assert metrics["deterministic_matches"] == 48
    assert metrics["ai_matches"] == 3
    assert metrics["hitl_exceptions"] == 1
    assert metrics["processing_errors"] == 0
    assert metrics["claim_conflicts"] == 0
    assert fake_llm.call_count == 4  # only the 4 chaotic records ever reach the agent

    # Every record reached the state the generator says a correct system should.
    assert metrics["ground_truth_agreements"] == 52
    assert metrics["ground_truth_disagreements"] == []
    assert metrics["ground_truth_accuracy"] == "100.00"
    assert metrics["match_rate"] == "98.08"  # 51/52
    assert metrics["states"] == {
        "DETERMINISTIC_MATCH": 48,
        "AI_RESOLVED": 3,
        "PENDING_HITL_REVIEW": 1,
    }

    console_output = capsys.readouterr().out
    assert "Final Match Rate:" in console_output
    assert "98.08%" in console_output  # 51/52
    assert "Ground-Truth Accuracy:    100.00%" in console_output

    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert len(manifest) == 1
    assert manifest[0]["reason"] == "UNKNOWN_UNRESOLVABLE"
    # The DETERMINISTIC variance, reported alongside the model's own claim rather
    # than replaced by it.
    assert manifest[0]["variance"] == "876.40"
    assert manifest[0]["ai_reported_variance"] == "876.40"
    assert manifest[0]["scenario"] == "unresolvable_anomaly"
    assert manifest[0]["agent_thread_id"] == f"eval-{manifest[0]['settlement_id']}"
    assert run_id in manifest[0]["settlement_id"]

    real_manifest_after = _MANIFEST_PATH.read_bytes() if _MANIFEST_PATH.exists() else None
    assert (
        real_manifest_after == real_manifest_before
    ), "the test run overwrote the published exceptions_manifest.json"

    async with AsyncSessionLocal() as session:
        settlement_count = await session.scalar(
            text("SELECT COUNT(*) FROM t_razorpay_settlements WHERE settlement_id LIKE :pattern"),
            {"pattern": f"%{run_id}%"},
        )
        recon_count = await session.scalar(
            text("SELECT COUNT(*) FROM t_reconciliation_ledger WHERE settlement_id LIKE :pattern"),
            {"pattern": f"%{run_id}%"},
        )
        state_counts = await session.execute(
            text(
                "SELECT recon_state, COUNT(*) FROM t_reconciliation_ledger "
                "WHERE settlement_id LIKE :pattern GROUP BY recon_state"
            ),
            {"pattern": f"%{run_id}%"},
        )
        states = dict(state_counts.all())

        # The agent's own figures are persisted for exactly the 4 records that
        # reached it, and the deterministic variance is never overwritten by the
        # model's claim.
        ai_rows = (
            await session.execute(
                text(
                    "SELECT recon_state, numeric_variance, ai_reported_variance, "
                    "ai_confidence_score, agent_thread_id, batch_run_id "
                    "FROM t_reconciliation_ledger WHERE settlement_id LIKE :pattern "
                    "AND ai_classification_reason IS NOT NULL"
                ),
                {"pattern": f"%{run_id}%"},
            )
        ).all()

        unclaimed_credits = await session.scalar(
            text(
                "SELECT COUNT(*) FROM t_bank_ledger b "
                "WHERE b.is_reconciled = FALSE AND EXISTS ("
                "  SELECT 1 FROM t_reconciliation_ledger r "
                "  WHERE r.bank_entry_id = b.entry_id AND r.batch_run_id = :run_id)"
            ),
            {"run_id": run_id},
        )

    assert settlement_count == 52
    assert recon_count == 52
    assert states.get("DETERMINISTIC_MATCH") == 48
    assert states.get("AI_RESOLVED") == 3
    assert states.get("PENDING_HITL_REVIEW") == 1

    assert len(ai_rows) == 4
    assert all(row.batch_run_id == run_id for row in ai_rows)
    assert all(row.agent_thread_id is not None for row in ai_rows)
    assert all(row.ai_confidence_score is not None for row in ai_rows)
    anomaly = [row for row in ai_rows if row.recon_state == "PENDING_HITL_REVIEW"]
    assert len(anomaly) == 1
    assert anomaly[0].numeric_variance == Decimal("876.40")

    # Every claimed credit is flagged reconciled, so "which credits are still
    # outstanding?" is an answerable question.
    assert unclaimed_credits == 0

    # Cleanup: this run's rows shouldn't linger for the next test run. Bank rows
    # are matched on the narration as well as the UTR, because the four chaotic
    # credits are deliberately UTR-less -- matching only on extracted_utr leaked
    # them into every subsequent run.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM t_reconciliation_ledger WHERE settlement_id LIKE :pattern"), {"pattern": f"%{run_id}%"}
        )
        await session.execute(
            text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :pattern"), {"pattern": f"%{run_id}%"}
        )
        await session.execute(
            text(
                "DELETE FROM t_bank_ledger WHERE extracted_utr LIKE :pattern "
                "OR raw_narration LIKE :pattern"
            ),
            {"pattern": f"%{run_id}%"},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_consecutive_batches_do_not_fight_over_the_same_bank_credit(tmp_path):
    """
    The regression test for the defect that made every run after the first report
    four failures.

    Two batches with DIFFERENT run ids are two different sets of settlements, so
    neither may inherit the other's bank credits. The chaotic records are
    deliberately UTR-less, which means their bank-ledger dedupe hash is computed
    over the narration; with an unscoped narration that hash was identical across
    runs, so the second batch's chaotic settlements resolved to the FIRST batch's
    bank credits -- already claimed by the first batch's reconciliation rows -- and
    unique_recon_bank_entry correctly refused to let one payout back two
    settlements.

    The right fix was to stop generating colliding data, not to relax the
    constraint: the constraint was the only thing standing between this batch and
    a double-counted payout.
    """
    from contextlib import asynccontextmanager

    first_id = uuid.uuid4().hex[:8].upper()
    second_id = uuid.uuid4().hex[:8].upper()

    @asynccontextmanager
    async def fake_graph_context_manager():
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            yield build_graph(_ScenarioFakeLLM(), checkpointer)

    try:
        first = await run_evaluation(
            graph_context_manager=fake_graph_context_manager,
            run_id=first_id,
            manifest_path=tmp_path / "first.json",
        )
        second = await run_evaluation(
            graph_context_manager=fake_graph_context_manager,
            run_id=second_id,
            manifest_path=tmp_path / "second.json",
        )

        for label, metrics in (("first", first), ("second", second)):
            assert metrics["claim_conflicts"] == 0, f"{label} batch reported claim conflicts"
            assert metrics["processing_errors"] == 0, f"{label} batch reported processing errors"
            assert metrics["states"] == {
                "DETERMINISTIC_MATCH": 48,
                "AI_RESOLVED": 3,
                "PENDING_HITL_REVIEW": 1,
            }, f"{label} batch reached unexpected states"

        # The two batches must own disjoint bank credits: 104 credits, no sharing.
        async with AsyncSessionLocal() as session:
            shared = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT bank_entry_id FROM t_reconciliation_ledger "
                    "  WHERE batch_run_id IN (:a, :b) AND bank_entry_id IS NOT NULL "
                    "  GROUP BY bank_entry_id HAVING COUNT(DISTINCT batch_run_id) > 1"
                    ") AS overlap"
                ),
                {"a": first_id, "b": second_id},
            )
        assert shared == 0
    finally:
        await _delete_run_rows(first_id, second_id)


@pytest.mark.asyncio
async def test_rerunning_the_same_batch_converges_instead_of_duplicating(tmp_path):
    """
    Re-running one batch under the SAME run id must converge onto the rows it
    already wrote -- 52 of each, not 104 -- because every write goes through the
    idempotent upsert_* helpers rather than a bare session.add().
    """
    from contextlib import asynccontextmanager

    run_id = uuid.uuid4().hex[:8].upper()

    @asynccontextmanager
    async def fake_graph_context_manager():
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL) as checkpointer:
            await checkpointer.setup()
            yield build_graph(_ScenarioFakeLLM(), checkpointer)

    try:
        first = await run_evaluation(
            graph_context_manager=fake_graph_context_manager,
            run_id=run_id,
            manifest_path=tmp_path / "first.json",
        )
        second = await run_evaluation(
            graph_context_manager=fake_graph_context_manager,
            run_id=run_id,
            manifest_path=tmp_path / "second.json",
        )

        for label, metrics in (("first", first), ("second", second)):
            assert metrics["claim_conflicts"] == 0, f"{label} run reported claim conflicts"
            assert metrics["processing_errors"] == 0, f"{label} run reported processing errors"
            assert metrics["states"] == {
                "DETERMINISTIC_MATCH": 48,
                "AI_RESOLVED": 3,
                "PENDING_HITL_REVIEW": 1,
            }, f"{label} run reached unexpected states"

        async with AsyncSessionLocal() as session:
            settlements = await session.scalar(
                text("SELECT COUNT(*) FROM t_razorpay_settlements WHERE settlement_id LIKE :p"),
                {"p": f"%{run_id}%"},
            )
            recons = await session.scalar(
                text("SELECT COUNT(*) FROM t_reconciliation_ledger WHERE settlement_id LIKE :p"),
                {"p": f"%{run_id}%"},
            )
            credits = await session.scalar(
                text(
                    "SELECT COUNT(*) FROM t_bank_ledger "
                    "WHERE extracted_utr LIKE :p OR raw_narration LIKE :p"
                ),
                {"p": f"%{run_id}%"},
            )

        assert settlements == 52
        assert recons == 52
        assert credits == 52
    finally:
        await _delete_run_rows(run_id)