"""
Sandboxed LangGraph orchestration for Razorpay Vulcan Ledger.

This module builds the agent that handles reconciliation exceptions the
deterministic rules engine (backend/core/rules_engine.py) could not resolve. The
LLM is structurally sandboxed: it receives a plain sanitized string built by the
caller and returns a validated ExceptionClassification
(backend/agents/schemas.py) -- it never sees a database row, a connection
string, or raw SQL, and this module never gives it one.

Dependency injection, on purpose:
    build_graph() takes the LLM (already wrapped in
    .with_structured_output(ExceptionClassification)), an optional checkpointer,
    and an optional AgentRuntimeConfig as plain arguments rather than
    constructing them at import time. This is what makes the graph unit-testable
    with a fake LLM (no real API key, no network call) and is why importing this
    module never touches the network or the database by itself -- only calling
    reconciliation_graph() or build_graph() does.

The model advises; our code decides:
    Every judgement that can change money is re-checked deterministically after
    the LLM answers, because a language model's self-reported certainty is not
    evidence:
      - confidence floor: an "AUTO_APPROVE_ADJUSTMENT" below
        config.min_auto_approve_confidence is overridden to human review.
      - variance cross-check: if the caller supplies the deterministic variance,
        an LLM variance that disagrees by more than MATCH_TOLERANCE means the
        model was reasoning about different numbers than the ledger holds, so
        the verdict goes to a human.
    Neither guard rewrites the model's classification -- the original verdict is
    persisted verbatim for audit, and the override is recorded as a reason in
    review_reasons. Falsifying what the model said to make routing look tidy
    would destroy the only record of the disagreement.

Exception vectors handled:
    - The LLM hangs -> each attempt is wrapped in asyncio.wait_for
      (config.timeout_seconds), so one unresponsive provider connection cannot
      stall an entire batch indefinitely.
    - The LLM returns malformed JSON / fails schema validation -> caught in
      llm_reasoning_node, the validation error is fed back into the next
      prompt (that is what makes the retry a self-correction rather than a
      re-roll), retried up to config.max_attempts times with a bounded delay
      between attempts, then falls back to a deterministic
      UNKNOWN_UNRESOLVABLE / ROUTE_TO_HITL_PANEL / confidence_score=0.0
      classification. The pipeline never crashes on a bad LLM response; it
      degrades to "ask a human" instead.
    - Routing to human review -> human_in_the_loop_node calls interrupt(),
      pausing the graph. Resuming requires a checkpointer (see
      reconciliation_graph()) and a Command(resume=...) call with the human's
      decision.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from agents.schemas import ExceptionClassification
from core.money import parse_money
from core.rules_engine import MATCH_TOLERANCE

#: Default bounded retry count. Kept as a module constant (not only inside
#: AgentRuntimeConfig) because it is part of this module's documented contract
#: and appears verbatim in the "attempt N/M" audit lines.
MAX_LLM_ATTEMPTS = 2

#: Defaults used when no AgentRuntimeConfig is supplied. These are the
#: production-safe values, not test conveniences -- reconciliation_graph()
#: overrides them from settings so they can be tuned without a code change.
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_MIN_AUTO_APPROVE_CONFIDENCE = 0.85

@dataclass(frozen=True)
class AgentRuntimeConfig:
    """
    Every knob that governs how hard the agent tries and when it defers to a
    human, in one immutable object.

    Passed explicitly into build_graph so a unit test can construct a graph with
    no environment at all, while the real entry point
    (reconciliation_graph) sources the same fields from validated settings.

    Attributes:
        max_attempts: how many times one exception may be sent to the LLM
            before the graph gives up and routes to human review.
        timeout_seconds: hard ceiling on a single LLM call.
        retry_backoff_seconds: base delay before a retry; the Nth retry waits
            N * this. Zero means retry immediately.
        min_auto_approve_confidence: the model's own confidence_score must be
            at least this for an AUTO_APPROVE_ADJUSTMENT to stand.
    """

    max_attempts: int = MAX_LLM_ATTEMPTS
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    min_auto_approve_confidence: float = DEFAULT_MIN_AUTO_APPROVE_CONFIDENCE

    @classmethod
    def from_settings(cls, settings) -> "AgentRuntimeConfig":
        """
        Build a config from a config.settings.Settings instance.

        Args:
            settings: the validated Settings object. Its fields are already
                range-checked (timeout > 0, confidence within [0, 1]), so this
                method does no validation of its own.

        Returns:
            An AgentRuntimeConfig mirroring the environment's configuration.
        """
        return cls(
            max_attempts=settings.llm_max_attempts,
            timeout_seconds=settings.llm_timeout_seconds,
            retry_backoff_seconds=settings.llm_retry_backoff_seconds,
            min_auto_approve_confidence=settings.ai_auto_approve_min_confidence,
        )


class SandboxedAgentState(TypedDict, total=False):
    """
    Graph state. Deliberately flat and JSON-primitive-only, which is a durability
    requirement rather than a style preference: every value here is written to the
    checkpointer, and a PENDING_HITL_REVIEW thread can sit in that checkpoint for
    as long as a human takes to decide. LangGraph's serializer already warns that
    deserializing an unregistered custom type out of a checkpoint "will be blocked
    in a future version" -- so a Pydantic model stored here would strand precisely
    the threads that still need a person, on the day the library is upgraded.

    Keys:
        settlement_id: business key of the settlement under review; carried so
            the interrupt payload can name what a human is being asked about.
        sanitized_context: the ONLY thing the LLM sees. A plain string built by
            the caller from already-sanitized values.
        deterministic_variance_str: optional. The rules engine's variance as a
            canonical two-decimal string (e.g. "-500.00"). When present, the
            LLM's reported variance is cross-checked against it.
        classification: the model's verdict as a plain dict -- ExceptionClassification
            .model_dump(), whose four fields are all str/float, so it round-trips
            through any checkpointer without a custom codec. Stored verbatim,
            never rewritten by a guard. Read it back with
            classification_from_state(), which revalidates it into the real model
            rather than letting callers duck-type a dict.
        requires_human_review: set by our deterministic guards when the model's
            verdict may not stand on its own. Routing reads this, so an
            overridden auto-approval cannot slip through.
        review_reasons: why review was forced, one plain sentence per reason.
        human_decision: "APPROVED" / "REJECTED", filled in on resume.
        error_log: full audit trail of every failed attempt and every override.
    """

    settlement_id: str
    sanitized_context: str
    deterministic_variance_str: Optional[str]
    classification: Optional[dict]
    requires_human_review: bool
    review_reasons: list[str]
    human_decision: Optional[str]
    error_log: list[str]


def classification_from_state(state) -> Optional[ExceptionClassification]:
    """
    Recover the typed verdict from graph state (or from the dict ainvoke returns).

    Args:
        state: a SandboxedAgentState, or any mapping with a "classification" key --
            which is what `await graph.ainvoke(...)` hands back.

    Returns:
        The ExceptionClassification, or None if the graph never reached the
        reasoning node. Revalidated through the Pydantic model on the way out, so
        a checkpoint written by an older build cannot smuggle a variance_str that
        would fail today's validation into a caller expecting a clean amount.

    Raises:
        pydantic.ValidationError: if the stored dict is not a valid
            classification. That means the checkpoint is corrupt or was written
            by an incompatible schema, which a human needs to see -- silently
            substituting a fallback verdict would put words in the model's mouth.
    """
    stored = state.get("classification")
    if stored is None:
        return None
    if isinstance(stored, ExceptionClassification):
        # Tolerated for the benefit of a caller holding pre-serialization state
        # (e.g. a node's own return value); never produced by the checkpointer.
        return stored
    return ExceptionClassification.model_validate(stored)


def _deterministic_guards(
    *,
    classification: ExceptionClassification,
    deterministic_variance_str: Optional[str],
    config: AgentRuntimeConfig,
) -> list[str]:
    """
    Re-check the model's verdict against facts our own code owns.

    Args:
        classification: the model's validated verdict.
        deterministic_variance_str: the rules engine's variance as a canonical
            two-decimal string, or None if the caller did not supply it.
        config: the runtime thresholds to judge against.

    Returns:
        A list of human-readable reasons the verdict must go to a human. Empty
        means the verdict may stand as the model gave it.

    Exception vectors handled:
        An unparseable deterministic_variance_str is itself a reason for human
        review rather than a crash: the caller passed something this module
        cannot interpret, and quietly ignoring it would mean silently skipping
        the cross-check that exists to catch exactly that class of mistake.
    """
    reasons: list[str] = []

    if (
        classification.suggested_action == "AUTO_APPROVE_ADJUSTMENT"
        and classification.confidence_score < config.min_auto_approve_confidence
    ):
        reasons.append(
            f"model asked to auto-approve at confidence "
            f"{classification.confidence_score:.3f}, below the required "
            f"{config.min_auto_approve_confidence:.3f} -- overridden to human review"
        )

    if deterministic_variance_str is not None:
        try:
            deterministic_variance = parse_money(
                deterministic_variance_str, "deterministic_variance_str"
            )
        except (TypeError, ValueError) as exc:
            reasons.append(
                f"deterministic variance {deterministic_variance_str!r} could not be "
                f"parsed for cross-check ({type(exc).__name__}: {exc}), so the "
                "model's verdict cannot be corroborated -- routing to human review"
            )
        else:
            drift = abs(classification.variance - deterministic_variance)
            if drift > MATCH_TOLERANCE:
                reasons.append(
                    f"model reported variance {classification.variance} INR but the "
                    f"ledger's deterministic variance is {deterministic_variance} INR "
                    f"(differs by {drift} INR, tolerance +/-{MATCH_TOLERANCE} INR) -- "
                    "the model was not reasoning about this settlement's numbers, so "
                    "routing to human review"
                )

    return reasons


def _fallback_classification() -> ExceptionClassification:
    """
    The verdict used when the LLM could not be made to produce a valid one.

    Returns:
        A deliberately maximally-cautious ExceptionClassification: an unknown
        reason, zero variance claimed, routed to a human, confidence 0.0. Every
        field is chosen so that nothing downstream can mistake it for a real
        model answer or auto-apply it.
    """
    return ExceptionClassification(
        discrepancy_reason="UNKNOWN_UNRESOLVABLE",
        variance_str="0.00",
        suggested_action="ROUTE_TO_HITL_PANEL",
        confidence_score=0.0,
    )

def build_graph(llm, checkpointer=None, *, config: Optional[AgentRuntimeConfig] = None):
    """
    Compile the sandboxed reconciliation graph.

    Args:
        llm: a Runnable whose .ainvoke(prompt: str) returns an
            ExceptionClassification -- normally
            ChatGoogleGenerativeAI(...).with_structured_output(
                ExceptionClassification), but any object with a matching
            ainvoke is accepted, which is what lets tests inject a fake.
        checkpointer: an optional LangGraph checkpointer (e.g.
            AsyncPostgresSaver, or InMemorySaver for tests). Required for the
            human_in_the_loop interrupt to actually pause/resume across separate
            invoke calls; without one, interrupt() still works within a single
            invoke but state is lost the moment that call returns.
        config: retry/timeout/confidence thresholds. Defaults to
            AgentRuntimeConfig() (the production-safe defaults) so a caller that
            has no settings object -- a unit test -- still gets sane behaviour.

    Returns:
        A compiled LangGraph graph. Call `await graph.ainvoke(state, config)` to
        run it, and `await graph.ainvoke(Command(resume=...), config)` with the
        same thread_id in config to resume past an interrupt.
    """
    runtime = config or AgentRuntimeConfig()

    async def fetch_context_node(state: SandboxedAgentState) -> dict:
        """
        Pure pass-through node establishing the sandbox boundary: this is the
        only place state enters the graph, and it never reaches back into a
        database -- sanitized_context must already be a plain string by the time
        it gets here (built by the caller, e.g. the Phase 5 evaluation runner).
        """
        return {"error_log": list(state.get("error_log", []))}

    async def llm_reasoning_node(state: SandboxedAgentState) -> dict:
        """
        Ask the sandboxed LLM to classify the exception, retrying with the
        previous failure quoted back to it, then re-check whatever it says
        against our own deterministic guards.

        Returns:
            A state update carrying the classification (the model's own, or the
            cautious fallback), whether human review is required, why, and the
            accumulated error_log.

        Exception vectors handled:
            Any exception from the LLM -- timeout, transport error, schema
            validation failure, or a structured-output layer returning the wrong
            type -- is recorded and retried up to runtime.max_attempts, then
            degrades to _fallback_classification(). Nothing raised by the model
            or its transport escapes this node, because a single bad response
            must not abort a batch of hundreds of settlements.
        """
        error_log = list(state.get("error_log", []))
        base_prompt = (
            f"Analyze this reconciliation discrepancy log: {state['sanitized_context']}"
        )

        classification: Optional[ExceptionClassification] = None
        last_error: Optional[BaseException] = None

        for attempt in range(1, runtime.max_attempts + 1):
            prompt = base_prompt
            if last_error is not None:
                # Self-correction, not a re-roll: the model is told precisely how
                # its previous answer failed. Retrying with the identical prompt
                # would just resample the same failure mode.
                prompt = (
                    f"{base_prompt}\n\n"
                    f"Your previous response was rejected: "
                    f"{type(last_error).__name__}: {last_error}. "
                    "Return a response that satisfies the required schema exactly."
                )
            try:
                candidate = await asyncio.wait_for(
                    llm.ainvoke(prompt), timeout=runtime.timeout_seconds
                )
                if not isinstance(candidate, ExceptionClassification):
                    raise TypeError(
                        f"structured output returned {type(candidate).__name__}, "
                        "not ExceptionClassification"
                    )
                classification = candidate
                break
            except asyncio.TimeoutError as exc:
                last_error = exc
                error_log.append(
                    f"attempt {attempt}/{runtime.max_attempts}: TimeoutError: LLM did "
                    f"not respond within {runtime.timeout_seconds}s"
                )
            except Exception as exc:  # noqa: BLE001 -- deliberately broad: any failure here must degrade, not crash
                last_error = exc
                error_log.append(
                    f"attempt {attempt}/{runtime.max_attempts}: {type(exc).__name__}: {exc}"
                )

            if attempt < runtime.max_attempts and runtime.retry_backoff_seconds > 0:
                # Bounded, attempt-scaled pause. Hammering a rate-limited or
                # overloaded provider with no pause lengthens the outage.
                await asyncio.sleep(runtime.retry_backoff_seconds * attempt)

        if classification is None:
            classification = _fallback_classification()
            error_log.append(
                f"all {runtime.max_attempts} attempts failed; falling back to "
                f"UNKNOWN_UNRESOLVABLE. Last error: "
                f"{type(last_error).__name__}: {last_error}"
            )

        reasons = _deterministic_guards(
            classification=classification,
            deterministic_variance_str=state.get("deterministic_variance_str"),
            config=runtime,
        )
        error_log.extend(f"deterministic override: {reason}" for reason in reasons)

        return {
            # Dumped, not stored as a model: see SandboxedAgentState.classification.
            "classification": classification.model_dump(),
            "requires_human_review": bool(reasons),
            "review_reasons": reasons,
            "error_log": error_log,
        }

    def human_in_the_loop_node(state: SandboxedAgentState) -> dict:
        """
        Pause the graph and surface the classification for human review.

        interrupt() raises a GraphInterrupt under the hood; LangGraph catches it,
        persists the state via the checkpointer, and returns the interrupt
        payload to the caller of ainvoke() instead of a normal state update.
        Calling ainvoke() again with the same thread_id and
        Command(resume=<decision>) resumes execution right here, with
        interrupt()'s return value set to <decision>.

        The payload carries review_reasons so the operator sees why the item
        reached them -- "the model wanted to auto-approve at 0.20 confidence" and
        "the model's variance disagrees with the ledger" call for very different
        human judgement, and a queue that flattens them into "needs review" makes
        that judgement harder.

        Returns:
            {"human_decision": "APPROVED" | "REJECTED"}. A resume value of
            {"approved": bool} is the canonical form; a bare truthy/falsey value
            is also accepted, so a caller that resumes with True/False is not
            silently recorded as a rejection.
        """
        classification = classification_from_state(state)
        if classification is None:
            # Unreachable through the compiled graph -- the only edge into this node
            # comes from llm_reasoning, which always sets a classification. Raised
            # rather than defaulted so a future edge that skips reasoning fails
            # loudly instead of asking a human to review a blank payload.
            raise RuntimeError(
                f"human_in_the_loop reached with no classification in state for "
                f"settlement {state.get('settlement_id')!r}"
            )
        decision = interrupt(
            {
                "settlement_id": state.get("settlement_id"),
                "discrepancy_reason": classification.discrepancy_reason,
                "variance": str(classification.variance),
                "confidence_score": classification.confidence_score,
                "review_reasons": list(state.get("review_reasons", [])),
                "question": "Approve this adjustment?",
            }
        )
        approved = decision.get("approved") if isinstance(decision, dict) else bool(decision)
        return {"human_decision": "APPROVED" if approved else "REJECTED"}

    def route_after_reasoning(state: SandboxedAgentState) -> Literal["human_in_the_loop", "__end__"]:
        """
        Send anything a human must see to human_in_the_loop, and nothing else.

        Two independent conditions route here: the model asking for it, and our
        own guards overriding the model. Checking requires_human_review FIRST
        matters -- an overridden auto-approval would otherwise fall through to
        END and be treated as an approved adjustment.
        """
        if state.get("requires_human_review"):
            return "human_in_the_loop"
        classification = classification_from_state(state)
        if classification is not None and classification.suggested_action == "ROUTE_TO_HITL_PANEL":
            return "human_in_the_loop"
        return END

    builder = StateGraph(SandboxedAgentState)
    builder.add_node("fetch_context", fetch_context_node)
    builder.add_node("llm_reasoning", llm_reasoning_node)
    builder.add_node("human_in_the_loop", human_in_the_loop_node)

    builder.set_entry_point("fetch_context")
    builder.add_edge("fetch_context", "llm_reasoning")
    builder.add_conditional_edges(
        "llm_reasoning",
        route_after_reasoning,
        {"human_in_the_loop": "human_in_the_loop", END: END},
    )
    builder.add_edge("human_in_the_loop", END)

    return builder.compile(checkpointer=checkpointer)

@asynccontextmanager
async def reconciliation_graph():
    """
    Real-usage entry point for Phase 5 onward: builds the actual Gemini LLM
    (model name from settings.gemini_model, configurable without a code change)
    and a real Postgres-backed async checkpointer, ensures its tables exist, and
    yields a ready-to-invoke compiled graph.

    Secrets are read at exactly this point and nowhere else in the module:
    settings.require_google_api_key() fails with an actionable message if the key
    is missing, and the checkpointer DSN is unwrapped from SecretStr here so it
    never sits in a module-level variable that a traceback could print.

    No generation parameters are passed to the model on purpose: Gemini 3.x
    removed the sampling knobs, and langchain-google-genai nulls temperature for
    Gemini 3+ anyway. Passing none is the supported form.

    Usage:
        async with reconciliation_graph() as graph:
            result = await graph.ainvoke(
                {"settlement_id": "...", "sanitized_context": "..."},
                config={"configurable": {"thread_id": "..."}},
            )

    Exception vectors handled:
        - GOOGLE_API_KEY missing/blank -> RuntimeError from
          require_google_api_key() before any network call is attempted.
        - Checkpointer database unreachable -> the psycopg error propagates from
          AsyncPostgresSaver; there is no retry loop here because a human needs
          to start Postgres.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from config.settings import get_settings

    settings = get_settings()
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.require_google_api_key(),
    ).with_structured_output(ExceptionClassification)

    async with AsyncPostgresSaver.from_conn_string(
        settings.checkpointer_database_url.get_secret_value()
    ) as checkpointer:
        await checkpointer.setup()
        yield build_graph(
            llm, checkpointer, config=AgentRuntimeConfig.from_settings(settings)
        )





