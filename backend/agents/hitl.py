"""
Human-in-the-loop completion service for Razorpay Vulcan Ledger.

PENDING_HITL_REVIEW used to be where a reconciliation went to die: the LangGraph
thread that paused on interrupt() was never recorded anywhere, and the ledger had
no column for a verdict, so nothing could ever finish a review. This module is the
missing half. It owns exactly one operation -- "a named human decided APPROVED or
REJECTED on this settlement" -- and makes it safe:

    resume the paused agent thread  ->  persist the verdict  ->  commit

Both halves are idempotent, because the realistic failure is a double-submit or a
retried request, not an exotic race:
    - Resuming a thread that has no pending interrupt is a no-op that returns the
      thread's final state, so a second attempt does not re-run the agent.
    - config.database.record_human_decision locks the row FOR UPDATE and guards its
      UPDATE on recon_state, so a repeat of the SAME verdict returns
      written=False rather than rewriting history, and a DIFFERENT verdict raises
      HumanDecisionConflictError rather than silently overwriting the first.

Ordering, and what a crash between the two halves means:
    The graph is resumed BEFORE the verdict is committed, so the ledger never
    claims a decision the agent thread never received. The cost is the opposite
    window: a crash after resume but before commit leaves a resumed thread with an
    uncommitted verdict. That state is recoverable and self-correcting -- the row is
    still PENDING_HITL_REVIEW, so the reviewer's retry resumes an
    already-resumed thread (a no-op) and then commits. Nothing is double-counted in
    either direction, which is the property that matters for money.

No REST endpoint lives here on purpose: exposing this over HTTP is Phase 6's job,
and this module is deliberately shaped so that endpoint is a thin wrapper (load
queue, call apply_human_decision, return the outcome) rather than a place where new
decision logic gets invented.

Exception vectors handled:
    - The settlement has no reconciliation row, is not awaiting review, or already
      carries a different verdict -> HumanDecisionConflictError from
      record_human_decision, propagated unchanged. A conflicting human decision is
      exactly the thing that must never be resolved automatically.
    - The reconciliation row has no persisted agent_thread_id (it predates this
      flow, or the agent never ran) -> the verdict is still recorded, and the
      outcome reports graph_resumed=False. Refusing to record a real human decision
      because a checkpoint is missing would strand the row forever.
    - The resumed graph reports a different decision than the one requested ->
      HitlResumeError, loudly. It means the resume value was misinterpreted, and a
      ledger that disagrees with its own agent state is worse than a failed call.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import (
    HUMAN_DECISION_STATES,
    HumanDecisionConflictError,
    ReconciliationLedger,
    get_reconciliation_for_update,
    list_pending_hitl,
    record_human_decision,
)

class HitlResumeError(RuntimeError):
    """
    Raised when the agent thread cannot be brought into agreement with the human's
    verdict -- the resume itself failed, or the resumed graph reported a different
    decision than the one requested.
    """


class PendingReview(BaseModel):
    """
    One item of the human review queue, flattened for a caller that should not have
    to hold an ORM row (a Phase 6 API serializing JSON, for instance).

    Attributes:
        settlement_id: the settlement awaiting a verdict.
        variance: the DETERMINISTIC variance, as a canonical two-decimal string.
            This is the ledger's own arithmetic, not the model's claim.
        ai_reported_variance: what the agent said the variance was, or None if no
            agent ran. Kept separate from variance on purpose: a reviewer comparing
            the two is the cheapest available check on whether the model understood
            the case at all.
        ai_classification_reason: the agent's category, or None.
        ai_confidence_score: the agent's self-reported confidence, or None.
        agent_thread_id: the LangGraph thread paused on interrupt(), or None.
        batch_run_id: which evaluation run produced this row, or None.
    """

    model_config = ConfigDict(frozen=True)

    settlement_id: str
    variance: str
    ai_reported_variance: Optional[str]
    ai_classification_reason: Optional[str]
    ai_confidence_score: Optional[float]
    agent_thread_id: Optional[str]
    batch_run_id: Optional[str]

    @classmethod
    def from_row(cls, row: ReconciliationLedger) -> "PendingReview":
        """
        Project a reconciliation row into a queue item.

        Args:
            row: a row in PENDING_HITL_REVIEW.

        Returns:
            The corresponding PendingReview. Monetary values are rendered as
            strings, never floats, so serializing the queue cannot lose paise.
        """
        return cls(
            settlement_id=row.settlement_id,
            variance=str(row.numeric_variance),
            ai_reported_variance=(
                str(row.ai_reported_variance) if row.ai_reported_variance is not None else None
            ),
            ai_classification_reason=row.ai_classification_reason,
            ai_confidence_score=(
                float(row.ai_confidence_score) if row.ai_confidence_score is not None else None
            ),
            agent_thread_id=row.agent_thread_id,
            batch_run_id=row.batch_run_id,
        )


class HumanDecisionOutcome(BaseModel):
    """
    What actually happened when a verdict was applied.

    Attributes:
        settlement_id: the settlement decided.
        decision: "APPROVED" or "REJECTED", normalized.
        recon_state: the resulting state, "HITL_APPROVED" or "HITL_REJECTED".
        newly_recorded: False when this exact verdict was already on the row -- a
            safe repeat, not a second decision.
        graph_resumed: whether a paused agent thread was actually resumed. False
            when the row carried no agent_thread_id, or when the thread had already
            been resumed by an earlier attempt.
        agent_thread_id: the thread this verdict was applied to, if any.
    """

    model_config = ConfigDict(frozen=True)

    settlement_id: str
    decision: str
    recon_state: str
    newly_recorded: bool
    graph_resumed: bool
    agent_thread_id: Optional[str]

async def get_review_queue(
    session: AsyncSession, *, limit: int = 100, batch_run_id: Optional[str] = None
) -> list[PendingReview]:
    """
    The human review queue, oldest first.

    Args:
        session: an active AsyncSession.
        limit: maximum items to return.
        batch_run_id: restrict to one evaluation run, or None for all runs.

    Returns:
        PendingReview items for every reconciliation in PENDING_HITL_REVIEW. Empty
        when there is nothing to review, which is the desirable steady state rather
        than an error.
    """
    rows = await list_pending_hitl(session, limit=limit, batch_run_id=batch_run_id)
    return [PendingReview.from_row(row) for row in rows]


async def resume_agent_thread(graph, *, agent_thread_id: str, approved: bool) -> Optional[str]:
    """
    Deliver a human's verdict to the paused LangGraph thread.

    Args:
        graph: a compiled graph built against the SAME checkpointer the thread was
            paused on. Passed in rather than constructed here, so this is testable
            with an in-memory saver and so the caller controls the (expensive)
            Postgres checkpointer's lifetime.
        agent_thread_id: the thread id persisted on the reconciliation row.
        approved: the verdict, as the boolean the graph's interrupt payload expects.

    Returns:
        The graph's own record of the decision ("APPROVED"/"REJECTED"), or None if
        the thread had nothing to resume -- which is what an already-resumed thread
        looks like and is treated as success, not failure.

    Raises:
        HitlResumeError: if the resume call itself fails. The underlying exception is
            chained, never swallowed: a checkpointer that cannot be reached is an
            operational fault a human needs to see, not something to retry silently.
    """
    from langgraph.types import Command

    config = {"configurable": {"thread_id": agent_thread_id}}
    try:
        state = await graph.ainvoke(Command(resume={"approved": approved}), config=config)
    except Exception as exc:  # noqa: BLE001 -- re-raised as HitlResumeError with context
        raise HitlResumeError(
            f"could not resume agent thread {agent_thread_id!r} to deliver the "
            f"{'APPROVED' if approved else 'REJECTED'} verdict: {type(exc).__name__}: {exc}"
        ) from exc

    return state.get("human_decision") if isinstance(state, dict) else None


async def apply_human_decision(
    session: AsyncSession,
    *,
    settlement_id: str,
    decision: str,
    decided_by: str,
    graph=None,
) -> HumanDecisionOutcome:
    """
    Complete one human review: resume the agent thread, then record the verdict.

    Args:
        session: an active AsyncSession. This function COMMITS on success, because
            the verdict and the state transition must land together or not at all.
        settlement_id: the settlement under review.
        decision: "APPROVED" or "REJECTED" (case-insensitive).
        decided_by: who decided. Required -- an unattributed decision is not an
            audit trail.
        graph: a compiled graph on the same checkpointer the thread paused on. May
            be None when the caller only wants the ledger updated (there is no
            thread to resume, or the graph is unavailable); the outcome then reports
            graph_resumed=False rather than pretending a thread was released.

    Returns:
        A HumanDecisionOutcome describing what changed, including whether this was a
        first decision or a safe repeat.

    Raises:
        ValueError: if decision is not APPROVED/REJECTED or decided_by is blank.
        HumanDecisionConflictError: if there is nothing under review, or a different
            verdict is already recorded.
        HitlResumeError: if the agent thread could not be resumed, or came back
            reporting a different verdict than the one requested. Nothing is
            committed in that case -- the session is rolled back, so a disagreement
            between ledger and agent state cannot be persisted.
    """
    normalized = (decision or "").strip().upper()
    if normalized not in HUMAN_DECISION_STATES:
        raise ValueError(
            f"decision={decision!r} is not a legal human verdict. "
            f"Legal values: {sorted(HUMAN_DECISION_STATES)}"
        )
    approved = normalized == "APPROVED"
    # Checked here as well as in record_human_decision, because that check happens
    # AFTER the graph has been resumed. Rejecting an unattributed decision only at
    # the persistence step would release the agent thread and then refuse to record
    # why -- the one ordering this module exists to prevent.
    if not decided_by or not decided_by.strip():
        raise ValueError(
            "decided_by must name the reviewer -- an unattributed decision is not auditable"
        )

    # Locked first: the lock is what makes the read-then-resume-then-write sequence
    # below safe against a second reviewer arriving mid-flight.
    row = await get_reconciliation_for_update(session, settlement_id)
    if row is None:
        raise HumanDecisionConflictError(
            f"settlement {settlement_id!r} has no reconciliation row, so there is "
            "nothing under review to decide."
        )

    agent_thread_id = row.agent_thread_id
    graph_resumed = False

    try:
        if graph is not None and agent_thread_id and row.recon_state == "PENDING_HITL_REVIEW":
            graph_decision = await resume_agent_thread(
                graph, agent_thread_id=agent_thread_id, approved=approved
            )
            graph_resumed = graph_decision is not None
            if graph_decision is not None and graph_decision != normalized:
                raise HitlResumeError(
                    f"agent thread {agent_thread_id!r} recorded {graph_decision!r} but the "
                    f"reviewer submitted {normalized!r}. Refusing to persist a ledger "
                    "state that disagrees with the agent's own state."
                )

        updated, written = await record_human_decision(
            session,
            settlement_id=settlement_id,
            decision=normalized,
            decided_by=decided_by,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return HumanDecisionOutcome(
        settlement_id=settlement_id,
        decision=normalized,
        recon_state=updated.recon_state,
        newly_recorded=written,
        graph_resumed=graph_resumed,
        agent_thread_id=agent_thread_id,
    )


