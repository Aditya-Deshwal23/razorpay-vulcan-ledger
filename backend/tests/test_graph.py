"""
Tests for backend.agents.graph.

Uses a fake LLM (no network, no API key) so these tests are fast and
deterministic, and cover both the self-correction retry path and the
interrupt/resume human-in-the-loop path. Most tests use LangGraph's
InMemorySaver, which is enough to prove the interrupt/resume plumbing
itself works; test_checkpointer_persists_across_separate_graph_instances
additionally proves it against a REAL Postgres-backed AsyncPostgresSaver,
across two independently-built graph objects, to confirm state survives
what would be two separate process invocations -- not just one Python
object's memory.
"""
import os
import uuid
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.graph import build_graph, classification_from_state
from agents.schemas import ExceptionClassification

CHECKPOINTER_DATABASE_URL = os.environ.get(
    "TEST_CHECKPOINTER_DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/razorpay_vulcan_ledger",
)


class _FakeLLM:
    """
    Stand-in for ChatGoogleGenerativeAI(...).with_structured_output(...).

    responses is consumed one at a time per .ainvoke() call; an entry that
    is an Exception instance is raised instead of returned, so a test can
    script "fails twice, then succeeds" or "always fails".
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    async def ainvoke(self, prompt: str):
        self.call_count += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _classification(**overrides) -> ExceptionClassification:
    defaults = dict(
        discrepancy_reason="TEMPORAL_CROSS_SETTLEMENT",
        variance_str="500.00",
        suggested_action="AUTO_APPROVE_ADJUSTMENT",
        confidence_score=0.95,
    )
    defaults.update(overrides)
    return ExceptionClassification(**defaults)


@pytest.mark.asyncio
async def test_happy_path_auto_approve_never_hits_hitl():
    llm = _FakeLLM([_classification(suggested_action="AUTO_APPROVE_ADJUSTMENT", confidence_score=0.97)])
    graph = build_graph(llm, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"settlement_id": "setl_1", "sanitized_context": "ctx"},
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )

    assert "__interrupt__" not in result
    assert classification_from_state(result).suggested_action == "AUTO_APPROVE_ADJUSTMENT"
    assert result["error_log"] == []
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_self_correction_recovers_after_one_bad_attempt():
    llm = _FakeLLM(
        [
            ValueError("malformed JSON from model"),
            _classification(suggested_action="AUTO_APPROVE_ADJUSTMENT"),
        ]
    )
    graph = build_graph(llm, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"settlement_id": "setl_2", "sanitized_context": "ctx"},
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )

    assert llm.call_count == 2
    assert len(result["error_log"]) == 1
    assert "attempt 1/2" in result["error_log"][0]
    assert classification_from_state(result).suggested_action == "AUTO_APPROVE_ADJUSTMENT"


@pytest.mark.asyncio
async def test_self_correction_falls_back_to_hitl_after_max_attempts():
    llm = _FakeLLM([ValueError("bad json"), ValueError("bad json again")])
    graph = build_graph(llm, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"settlement_id": "setl_3", "sanitized_context": "ctx"},
        config={"configurable": {"thread_id": str(uuid.uuid4())}},
    )

    assert llm.call_count == 2
    # 2 per-attempt failures + 1 final fallback-summary line
    assert len(result["error_log"]) == 3
    assert "__interrupt__" in result
    classification = classification_from_state(result)
    assert classification.discrepancy_reason == "UNKNOWN_UNRESOLVABLE"
    assert classification.suggested_action == "ROUTE_TO_HITL_PANEL"
    assert classification.confidence_score == 0.0


@pytest.mark.asyncio
async def test_low_confidence_routes_to_hitl_and_resumes_on_approval():
    llm = _FakeLLM([_classification(suggested_action="ROUTE_TO_HITL_PANEL", confidence_score=0.2)])
    graph = build_graph(llm, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    paused = await graph.ainvoke({"settlement_id": "setl_4", "sanitized_context": "ctx"}, config=config)
    assert "__interrupt__" in paused
    payload = paused["__interrupt__"][0].value
    assert payload["settlement_id"] == "setl_4"
    assert payload["variance"] == "500.00"

    resumed = await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert "__interrupt__" not in resumed
    assert resumed["human_decision"] == "APPROVED"


@pytest.mark.asyncio
async def test_resume_with_rejection_is_recorded():
    llm = _FakeLLM([_classification(suggested_action="ROUTE_TO_HITL_PANEL", confidence_score=0.2)])
    graph = build_graph(llm, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    await graph.ainvoke({"settlement_id": "setl_5", "sanitized_context": "ctx"}, config=config)
    resumed = await graph.ainvoke(Command(resume={"approved": False}), config=config)

    assert resumed["human_decision"] == "REJECTED"


@pytest.mark.asyncio
async def test_checkpointer_persists_across_separate_graph_instances():
    """
    The real proof: pause on one graph object, then build a SECOND,
    independent graph object (fresh build_graph() call, standing in for a
    separate process picking the thread back up later) against the SAME
    Postgres-backed checkpointer, and confirm it can resume the first
    graph's paused thread. InMemorySaver could never prove this -- its
    state doesn't outlive the Python object holding it.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    async with AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL) as checkpointer:
        await checkpointer.setup()

        llm_1 = _FakeLLM([_classification(suggested_action="ROUTE_TO_HITL_PANEL", confidence_score=0.1)])
        graph_1 = build_graph(llm_1, checkpointer=checkpointer)
        paused = await graph_1.ainvoke(
            {"settlement_id": "setl_cross_process", "sanitized_context": "ctx"}, config=config
        )
        assert "__interrupt__" in paused

        # A brand-new graph object, its own fresh closures/nodes -- nothing
        # shared with graph_1 except the checkpointer and the thread_id.
        llm_2 = _FakeLLM([])  # must NOT be called; resuming should not re-run llm_reasoning
        graph_2 = build_graph(llm_2, checkpointer=checkpointer)
        resumed = await graph_2.ainvoke(Command(resume={"approved": True}), config=config)

        assert resumed["human_decision"] == "APPROVED"
        assert llm_2.call_count == 0


@pytest.mark.asyncio
async def test_paused_checkpoint_holds_only_json_primitives():
    """
    The regression test for a durability defect that would only have surfaced on a
    library upgrade, long after the damage was unavoidable.

    The graph used to store the ExceptionClassification model itself in state.
    LangGraph's serializer accepted it while warning that deserializing an
    unregistered custom type from a checkpoint "will be blocked in a future
    version" -- which means the threads that would have failed to load are exactly
    the ones parked in PENDING_HITL_REVIEW waiting for a person, i.e. the money
    nobody has accounted for yet.

    So the state a paused thread leaves behind must be plain JSON: not "currently
    serializable", but serializable by inspection. json.dumps is the check, because
    it has no custom codecs to fall back on.
    """
    import json

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async with AsyncPostgresSaver.from_conn_string(CHECKPOINTER_DATABASE_URL) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(
            _FakeLLM([_classification(suggested_action="ROUTE_TO_HITL_PANEL", confidence_score=0.2)]),
            checkpointer=checkpointer,
        )
        paused = await graph.ainvoke(
            {"settlement_id": "setl_json_only", "sanitized_context": "ctx"}, config=config
        )
        assert "__interrupt__" in paused, "the thread did not pause; nothing was checkpointed"

        # Read the state back through the checkpointer, not from the invoke return
        # value: this is the round trip a reviewer's resume actually performs.
        stored = await graph.aget_state(config)

    assert isinstance(stored.values["classification"], dict)
    json.dumps(stored.values)  # raises TypeError on any non-JSON value

    # Round-tripping still yields the typed verdict, with a real Decimal variance --
    # the dict is a storage format, not a downgrade of the contract.
    recovered = classification_from_state(stored.values)
    assert isinstance(recovered, ExceptionClassification)
    assert recovered.variance == Decimal("500.00")
    assert recovered.suggested_action == "ROUTE_TO_HITL_PANEL"


def test_classification_from_state_tolerates_absence_and_refuses_corruption():
    """
    None means "the graph never reached the reasoning node" and is a legitimate
    answer. A malformed stored dict is not: substituting a fallback verdict there
    would attribute a classification to the model that it never produced, so the
    ValidationError is allowed to propagate.
    """
    from pydantic import ValidationError

    assert classification_from_state({}) is None
    assert classification_from_state({"classification": None}) is None

    with pytest.raises(ValidationError):
        classification_from_state({"classification": {"discrepancy_reason": "NOT_A_CATEGORY"}})

    # A live model instance is accepted unchanged, for a caller holding a node's
    # own return value rather than checkpointed state.
    live = _classification()
    assert classification_from_state({"classification": live}) is live