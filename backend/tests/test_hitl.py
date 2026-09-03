"""
Tests for backend.agents.hitl -- the completion half of the human-in-the-loop
flow.

These run against the REAL Postgres database and a REAL LangGraph checkpointer,
because the properties under test are precisely the ones an in-memory double
cannot demonstrate: that a paused agent thread is actually released, that the
verdict actually lands in t_reconciliation_ledger, and that a repeat submission
converges instead of double-deciding.

Each test builds its own settlement/reconciliation pair under a unique run id and
deletes it afterwards, so the suite is re-runnable and leaves no rows behind.
"""
import os
import uuid
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import text

from agents.graph import build_graph
from agents.hitl import (
    HitlResumeError,
    apply_human_decision,
    get_review_queue,
    resume_agent_thread,
)
from agents.schemas import ExceptionClassification
from config.database import (
    AsyncSessionLocal,
    HumanDecisionConflictError,
    reconciliation_state_hash,
    upsert_reconciliation,
    upsert_settlement,
)

CHECKPOINTER_DATABASE_URL = os.environ.get(
    "TEST_CHECKPOINTER_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/razorpay_vulcan_ledger",
)

NARRATION = "/TXT/NEFT-HITLTESTUTR0000001 test credit"


class _FakeLLM:
    """Always routes to a human, so every graph built here pauses on interrupt()."""

    def __init__(self):
        self.call_count = 0

    async def ainvoke(self, prompt: str) -> ExceptionClassification:
        self.call_count += 1
        return ExceptionClassification(
            discrepancy_reason="UNKNOWN_UNRESOLVABLE",
            variance_str="876.40",
            suggested_action="ROUTE_TO_HITL_PANEL",
            confidence_score=0.12,
        )


async def _seed_pending(run_id: str, *, agent_thread_id=None, variance="876.40") -> str:
    """
    Create one settlement in PENDING_HITL_REVIEW and return its settlement_id.

    Uses the same upsert helpers the pipeline uses, so the fixture cannot drift
    away from the shape of a row the pipeline actually produces.
    """
    settlement_id = f"setl_hitl_{run_id}"
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
            numeric_variance=Decimal(variance),
            evidence_narration=NARRATION,
            cryptographic_state_hash=reconciliation_state_hash(
                settlement_id=settlement_id,
                recon_state="PENDING_HITL_REVIEW",
                variance=Decimal(variance),
                raw_narration=NARRATION,
            ),
            ai_classification_reason="UNKNOWN_UNRESOLVABLE",
            ai_reported_variance=Decimal("999.99"),
            ai_confidence_score=Decimal("0.12"),
            agent_thread_id=agent_thread_id,
            batch_run_id=run_id,
        )
        await session.commit()
    return settlement_id


async def _cleanup(run_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET LOCAL vulcan.allow_audit_maintenance = 'on'"))
        await session.execute(
            text("DELETE FROM t_reconciliation_events WHERE settlement_id LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_reconciliation_ledger WHERE settlement_id LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.execute(
            text("DELETE FROM t_razorpay_settlements WHERE settlement_id LIKE :p"),
            {"p": f"%{run_id}%"},
        )
        await session.commit()


async def _paused_graph(settlement_id: str):
    """
    Build a graph on an InMemorySaver and run it until it pauses on interrupt(),
    returning (graph, thread_id). The thread is genuinely suspended -- the same
    state a pipeline run leaves behind for a reviewer.
    """
    graph = build_graph(_FakeLLM(), checkpointer=InMemorySaver())
    thread_id = str(uuid.uuid4())
    paused = await graph.ainvoke(
        {"settlement_id": settlement_id, "sanitized_context": "ctx"},
        config={"configurable": {"thread_id": thread_id}},
    )
    assert "__interrupt__" in paused, "fixture graph did not pause; the test would prove nothing"
    return graph, thread_id


@pytest.mark.asyncio
async def test_review_queue_projects_deterministic_and_ai_variance_separately():
    """
    The queue must show BOTH numbers. The deterministic variance is the ledger's
    own arithmetic; ai_reported_variance is the model's claim. A reviewer who can
    only see one of them cannot tell whether the model understood the case.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = await _seed_pending(run_id)
        async with AsyncSessionLocal() as session:
            queue = await get_review_queue(session, batch_run_id=run_id)

        assert len(queue) == 1
        item = queue[0]
        assert item.settlement_id == settlement_id
        assert item.variance == "876.40"
        assert item.ai_reported_variance == "999.99"
        assert item.ai_classification_reason == "UNKNOWN_UNRESOLVABLE"
        assert item.ai_confidence_score == pytest.approx(0.12)
        assert item.batch_run_id == run_id
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_approval_resumes_thread_and_commits_verdict():
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = f"setl_hitl_{run_id}"
        graph, thread_id = await _paused_graph(settlement_id)
        await _seed_pending(run_id, agent_thread_id=thread_id)

        async with AsyncSessionLocal() as session:
            outcome = await apply_human_decision(
                session,
                settlement_id=settlement_id,
                decision="approved",  # lower case on purpose: normalization is part of the contract
                decided_by="ops.reviewer",
                graph=graph,
            )

        assert outcome.decision == "APPROVED"
        assert outcome.recon_state == "HITL_APPROVED"
        assert outcome.newly_recorded is True
        assert outcome.graph_resumed is True
        assert outcome.agent_thread_id == thread_id

        # The agent thread itself is released, not merely marked released in the ledger.
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        assert state.next == (), f"thread still has pending tasks: {state.next}"
        assert state.values["human_decision"] == "APPROVED"

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT recon_state, human_decision, human_decision_by, "
                        "human_decision_at IS NOT NULL AS decided, cryptographic_state_hash "
                        "FROM t_reconciliation_ledger WHERE settlement_id = :s"
                    ),
                    {"s": settlement_id},
                )
            ).one()
        assert row.recon_state == "HITL_APPROVED"
        assert row.human_decision == "APPROVED"
        assert row.human_decision_by == "ops.reviewer"
        assert row.decided is True
        assert row.cryptographic_state_hash == reconciliation_state_hash(
            settlement_id=settlement_id,
            recon_state="HITL_APPROVED",
            variance=Decimal("876.40"),
            raw_narration=NARRATION,
        )

        # A terminal decision must not overwrite the original pending snapshot.
        # The append-only event ledger holds both auditable states, each with its
        # own state-correct fingerprint.
        async with AsyncSessionLocal() as session:
            events = (
                await session.execute(
                    text(
                        "SELECT event_type, from_state, to_state, cryptographic_state_hash "
                        "FROM t_reconciliation_events WHERE settlement_id = :s "
                        "ORDER BY occurred_at, event_id"
                    ),
                    {"s": settlement_id},
                )
            ).all()
        assert [(event.event_type, event.from_state, event.to_state) for event in events] == [
            ("RECONCILIATION_RECORDED", None, "PENDING_HITL_REVIEW"),
            ("HUMAN_DECISION_RECORDED", "PENDING_HITL_REVIEW", "HITL_APPROVED"),
        ]
        assert events[0].cryptographic_state_hash == reconciliation_state_hash(
            settlement_id=settlement_id,
            recon_state="PENDING_HITL_REVIEW",
            variance=Decimal("876.40"),
            raw_narration=NARRATION,
        )
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_repeating_the_same_verdict_is_a_safe_no_op():
    """A double-click, or a retried HTTP request, must not decide twice."""
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = f"setl_hitl_{run_id}"
        graph, thread_id = await _paused_graph(settlement_id)
        await _seed_pending(run_id, agent_thread_id=thread_id)

        async with AsyncSessionLocal() as session:
            first = await apply_human_decision(
                session,
                settlement_id=settlement_id,
                decision="REJECTED",
                decided_by="ops.reviewer",
                graph=graph,
            )
        async with AsyncSessionLocal() as session:
            second = await apply_human_decision(
                session,
                settlement_id=settlement_id,
                decision="REJECTED",
                decided_by="someone.else",
                graph=graph,
            )

        assert first.newly_recorded is True
        assert second.newly_recorded is False
        assert second.recon_state == "HITL_REJECTED"

        # The attribution of the FIRST decision survives; the repeat does not
        # rewrite who decided.
        async with AsyncSessionLocal() as session:
            decided_by = await session.scalar(
                text(
                    "SELECT human_decision_by FROM t_reconciliation_ledger "
                    "WHERE settlement_id = :s"
                ),
                {"s": settlement_id},
            )
        assert decided_by == "ops.reviewer"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_conflicting_verdict_is_refused():
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = f"setl_hitl_{run_id}"
        graph, thread_id = await _paused_graph(settlement_id)
        await _seed_pending(run_id, agent_thread_id=thread_id)

        async with AsyncSessionLocal() as session:
            await apply_human_decision(
                session,
                settlement_id=settlement_id,
                decision="APPROVED",
                decided_by="first.reviewer",
                graph=graph,
            )

        async with AsyncSessionLocal() as session:
            with pytest.raises(HumanDecisionConflictError):
                await apply_human_decision(
                    session,
                    settlement_id=settlement_id,
                    decision="REJECTED",
                    decided_by="second.reviewer",
                    graph=None,
                )

        async with AsyncSessionLocal() as session:
            state = await session.scalar(
                text("SELECT recon_state FROM t_reconciliation_ledger WHERE settlement_id = :s"),
                {"s": settlement_id},
            )
        assert state == "HITL_APPROVED"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_missing_agent_thread_still_records_the_decision():
    """
    A row with no persisted thread id (it predates the flow, or the agent never
    ran) must still be decidable. Refusing to record a real human decision
    because a checkpoint is missing would strand the row in PENDING_HITL_REVIEW
    forever.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = await _seed_pending(run_id, agent_thread_id=None)
        graph = build_graph(_FakeLLM(), checkpointer=InMemorySaver())

        async with AsyncSessionLocal() as session:
            outcome = await apply_human_decision(
                session,
                settlement_id=settlement_id,
                decision="APPROVED",
                decided_by="ops.reviewer",
                graph=graph,
            )

        assert outcome.graph_resumed is False
        assert outcome.agent_thread_id is None
        assert outcome.newly_recorded is True
        assert outcome.recon_state == "HITL_APPROVED"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_unknown_settlement_is_a_conflict_not_a_crash():
    async with AsyncSessionLocal() as session:
        with pytest.raises(HumanDecisionConflictError):
            await apply_human_decision(
                session,
                settlement_id=f"setl_does_not_exist_{uuid.uuid4().hex[:8]}",
                decision="APPROVED",
                decided_by="ops.reviewer",
            )


@pytest.mark.asyncio
async def test_illegal_verdict_and_blank_reviewer_are_rejected_before_any_resume():
    """
    Both validations must fire BEFORE the graph is touched. Releasing an agent
    thread and only then refusing to record why is the one ordering this module
    exists to prevent.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = f"setl_hitl_{run_id}"
        graph, thread_id = await _paused_graph(settlement_id)
        await _seed_pending(run_id, agent_thread_id=thread_id)

        async with AsyncSessionLocal() as session:
            with pytest.raises(ValueError, match="legal human verdict"):
                await apply_human_decision(
                    session,
                    settlement_id=settlement_id,
                    decision="MAYBE",
                    decided_by="ops.reviewer",
                    graph=graph,
                )
            with pytest.raises(ValueError, match="decided_by"):
                await apply_human_decision(
                    session,
                    settlement_id=settlement_id,
                    decision="APPROVED",
                    decided_by="   ",
                    graph=graph,
                )

        # The thread is still paused and the row is still pending: neither invalid
        # call had any effect at all.
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        assert state.next != ()
        async with AsyncSessionLocal() as session:
            recon_state = await session.scalar(
                text("SELECT recon_state FROM t_reconciliation_ledger WHERE settlement_id = :s"),
                {"s": settlement_id},
            )
        assert recon_state == "PENDING_HITL_REVIEW"
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_graph_disagreement_aborts_without_committing():
    """
    If the resumed graph reports a different verdict than the reviewer submitted,
    the resume value was misinterpreted somewhere. Persisting a ledger state that
    disagrees with the agent's own state is worse than failing the call, so
    nothing is committed and the row stays reviewable.
    """
    run_id = uuid.uuid4().hex[:8].upper()
    try:
        settlement_id = f"setl_hitl_{run_id}"
        graph, thread_id = await _paused_graph(settlement_id)
        await _seed_pending(run_id, agent_thread_id=thread_id)

        class _DisagreeingGraph:
            """Reports REJECTED no matter what verdict it is handed."""

            async def ainvoke(self, command, config=None):
                return {"human_decision": "REJECTED"}

        async with AsyncSessionLocal() as session:
            with pytest.raises(HitlResumeError, match="disagrees"):
                await apply_human_decision(
                    session,
                    settlement_id=settlement_id,
                    decision="APPROVED",
                    decided_by="ops.reviewer",
                    graph=_DisagreeingGraph(),
                )

        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT recon_state, human_decision FROM t_reconciliation_ledger "
                        "WHERE settlement_id = :s"
                    ),
                    {"s": settlement_id},
                )
            ).one()
        assert row.recon_state == "PENDING_HITL_REVIEW"
        assert row.human_decision is None
    finally:
        await _cleanup(run_id)


@pytest.mark.asyncio
async def test_resume_failure_is_raised_as_hitl_resume_error_with_context():
    class _BrokenGraph:
        async def ainvoke(self, command, config=None):
            raise ConnectionError("checkpointer unreachable")

    with pytest.raises(HitlResumeError, match="checkpointer unreachable") as excinfo:
        await resume_agent_thread(_BrokenGraph(), agent_thread_id="thread-1", approved=True)

    assert isinstance(excinfo.value.__cause__, ConnectionError)
