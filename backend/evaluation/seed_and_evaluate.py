"""
Batch validation benchmark runner for Razorpay Vulcan Ledger -- "the demo bar"
from the buildathon brief: a self-contained 52-record synthetic batch (48
perfectly balanced settlements, 4 deliberately chaotic ones) driven through the
full pipeline -- UTR parsing, the deterministic rules engine, and the sandboxed
LangGraph agent -- with results persisted to Postgres and a clean console report
plus an auditable exceptions manifest.

What each record's outcome means, and why the three states are kept apart:
    DETERMINISTIC_MATCH  the arithmetic balances within MATCH_TOLERANCE AND a UTR
                         proves the bank credit belongs to this settlement. Both
                         halves are required: a coincidentally equal amount with
                         no UTR is not proof of identity, and counting it as one
                         would inflate the deterministic match rate with guesses.
    AI_RESOLVED          the agent explained the discrepancy with enough
                         confidence for the deterministic guards in
                         agents/graph.py to let it stand.
    PENDING_HITL_REVIEW  nobody could resolve it. This is a queue entry for
                         agents/hitl.py, not a failure.

Ground truth and accuracy:
    Every generated record carries the state a CORRECT system should reach for it
    (SyntheticRecord.expected_state), so the report can state agreement with
    ground truth rather than only counting states. That number is the honest one:
    a run can hit a 98% "match rate" while classifying the wrong records, and
    only the ground-truth comparison shows it.

Persistence, and what is deliberately NOT trusted:
    numeric_variance is always the DETERMINISTIC variance -- the ledger's own
    arithmetic. The model's figure is quarantined in ai_reported_variance and its
    confidence in ai_confidence_score, so a reviewer can compare the two. Writing
    the model's number into numeric_variance (which this runner used to do) makes
    the ledger's own arithmetic unrecoverable.

Re-run safety:
    Every settlement_id, UTR, and chaotic narration in the generated batch is
    scoped by a fresh run id, and every write goes through the idempotent
    upsert_* helpers. Re-running the benchmark converges rather than colliding.
    The chaotic records are the reason the narrations are scoped too: they are
    deliberately UTR-less, so their bank-ledger dedupe hash is computed over the
    narration, and an unscoped narration produced the SAME hash on every run --
    the second run then found a bank credit a previous run's reconciliation row
    already claimed and reported four spurious processing errors.

Exception vectors handled:
    - The bank credit a record needs is already claimed by another settlement's
      reconciliation row -> BankLedgerConflictError, counted as a named
      claim_conflicts outcome, NOT folded into processing_errors. A double-claim
      is the ledger correctly refusing to double-count a payout; calling it a
      generic error hides the one failure mode that matters most here.
    - Any other exception on a single record -> counted as a processing error and
      recorded in the manifest with its type and message. The batch continues;
      one bad row cannot abort the run.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

from agents.graph import classification_from_state, reconciliation_graph
from config.database import (
    AsyncSessionLocal,
    BankLedgerConflictError,
    get_or_create_bank_entry,
    mark_bank_entry_reconciled,
    reconciliation_state_hash,
    upsert_reconciliation,
    upsert_settlement,
)
from core.money import money_to_str
from core.rules_engine import DeterministicRulesEngine
from parsers.mt940_processor import LegacyBankParser

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = _PROJECT_ROOT / "exceptions_manifest.json"

# Confidence is a probability, not money, and t_reconciliation_ledger stores it
# as NUMERIC(4,3). Quantizing here rather than letting PostgreSQL round means the
# stored value is the one this process decided on.
_CONFIDENCE_QUANTUM = Decimal("0.001")


@dataclass(frozen=True)
class SyntheticRecord:
    """
    One row of the synthetic batch, before any processing.

    Attributes:
        settlement_id: run-scoped business key.
        bank_name: HDFC / ICICI / AXIS -- which narration convention applies.
        narration: the raw bank narration, exactly as a statement would carry it.
        gross, fees, taxes, refunds, adjustments: the settlement's components.
        bank_credit: what the bank actually credited.
        transaction_date: the credit's value date.
        scenario: "perfect", or the name of the chaotic case.
        expected_state: the recon_state a CORRECT system should reach for this
            record. This is the benchmark's ground truth, fixed by construction
            when the record is generated -- never derived from what the run
            actually produced, which would make the accuracy figure circular.
    """

    settlement_id: str
    bank_name: str
    narration: str
    gross: Decimal
    fees: Decimal
    taxes: Decimal
    refunds: Decimal
    adjustments: Decimal
    bank_credit: Decimal
    transaction_date: date
    scenario: str
    expected_state: str


def generate_synthetic_batch(run_id: str) -> list[SyntheticRecord]:
    """
    Build the exact 52-record batch the buildathon brief specifies: 48 perfectly
    balanced settlements (rotated across HDFC/ICICI/AXIS to exercise every parser
    pattern from Phase 3) and 4 deliberately chaotic anomalies.

    Args:
        run_id: short unique uppercase-alphanumeric string folded into every
            settlement_id, UTR, AND chaotic narration in this batch, so
            re-running the script never collides with rows a previous run wrote.

    Returns:
        Exactly 52 SyntheticRecord objects: indices 0-47 are the perfect batch,
        48-51 are the four chaotic scenarios, each carrying its ground-truth
        expected_state.

    Note on the chaotic narrations:
        They must remain UTR-less -- that is the whole point of those scenarios --
        while still differing between runs. run_id is 8 characters, so embedding
        it cannot accidentally create a UTR-shaped (16-22 character) token, and
        the surrounding words are lower case, so neither the ICICI pattern nor
        the generic fallback can match. The word "delay" in the delayed-webhook
        narration is load-bearing: it is how a classifier (real or test double)
        can tell that scenario apart from the chargeback, which has identical
        arithmetic.
    """
    records: list[SyntheticRecord] = []
    banks_cycle = ["HDFC", "ICICI", "AXIS"]
    narration_of = {
        "HDFC": lambda utr: f"/INF/NEFT CR:{utr}/ACME CORP",
        "ICICI": lambda utr: f"/TXT/NEFT-{utr}",
        "AXIS": lambda utr: f"NEFT-{utr}",
    }

    gross, fees, taxes = Decimal("1000.00"), Decimal("20.00"), Decimal("3.60")
    perfect_net = gross - fees - taxes  # 976.40

    for i in range(48):
        bank = banks_cycle[i % len(banks_cycle)]
        utr = f"{bank[:4]}{run_id}{i:04d}"  # 4 + 8 + 4 = 16 chars, meets the 16-22 UTR pattern
        records.append(
            SyntheticRecord(
                settlement_id=f"setl_perfect_{run_id}_{i}",
                bank_name=bank,
                narration=narration_of[bank](utr),
                gross=gross,
                fees=fees,
                taxes=taxes,
                refunds=Decimal("0.00"),
                adjustments=Decimal("0.00"),
                bank_credit=perfect_net,
                transaction_date=date.today(),
                scenario="perfect",
                # Math balances and the UTR proves identity: nothing else is needed.
                expected_state="DETERMINISTIC_MATCH",
            )
        )

    chaotic = [
        dict(
            desc="chargeback",
            # Balances only after a 500.00 adjustment, and carries no UTR -- so it
            # cannot be matched deterministically, but it IS explainable.
            narration=f"/TXT/NEFT-chargeback reversal batch {run_id} no reference",
            adjustments=Decimal("500.00"),
            bank_credit=Decimal("476.40"),
            expected_state="AI_RESOLVED",
        ),
        dict(
            desc="delayed_webhook",
            narration=f"/TXT/NEFT-failed delay retry batch {run_id}",
            adjustments=Decimal("0.00"),
            bank_credit=Decimal("976.40"),
            expected_state="AI_RESOLVED",
        ),
        dict(
            desc="unresolvable_anomaly",
            # 876.40 short with no explanation anywhere in the data. No honest
            # system can resolve this one; a human has to look at it.
            narration=f"/TXT/NEFT-unknown manual credit batch {run_id}",
            adjustments=Decimal("0.00"),
            bank_credit=Decimal("100.00"),
            expected_state="PENDING_HITL_REVIEW",
        ),
        dict(
            desc="missing_utr",
            narration=f"random text with no utr present anywhere batch {run_id}",
            adjustments=Decimal("0.00"),
            bank_credit=Decimal("976.40"),
            expected_state="AI_RESOLVED",
        ),
    ]
    for i, scenario in enumerate(chaotic):
        records.append(
            SyntheticRecord(
                settlement_id=f"setl_edge_{run_id}_{i}_{scenario['desc']}",
                bank_name="ICICI",
                narration=scenario["narration"],
                gross=gross,
                fees=fees,
                taxes=taxes,
                refunds=Decimal("0.00"),
                adjustments=scenario["adjustments"],
                bank_credit=scenario["bank_credit"],
                transaction_date=date.today(),
                scenario=scenario["desc"],
                expected_state=scenario["expected_state"],
            )
        )

    return records


def _sanitized_context(record: SyntheticRecord, math_result, utr: Optional[str]) -> str:
    """
    Build the ONLY thing the LLM is ever shown for this record.

    No database row, no primary key, no full statement text -- a flat string of
    the facts needed to classify the discrepancy. The wording is stable on
    purpose: it is the agent's entire input, so changing it changes the benchmark.

    Args:
        record: the record being processed.
        math_result: the ReconciliationResult from the deterministic engine.
        utr: the extracted UTR, or None.

    Returns:
        The sanitized context string.
    """
    return (
        f"Expected net settlement: {money_to_str(math_result.expected_net)} INR. "
        f"Actual bank credit: {money_to_str(record.bank_credit)} INR. "
        f"Deterministic variance: {money_to_str(math_result.variance)} INR (tolerance is 0.02 INR). "
        f"UTR extraction: {'succeeded (' + utr + ')' if utr else 'FAILED -- no UTR found in narration'}. "
        f"Raw bank narration: {record.narration!r}."
    )


async def _process_record(
    record: SyntheticRecord, graph, run_id: str, metrics: dict, exceptions: list
) -> None:
    """
    Run one record through parsing, the deterministic rules engine, and (only if
    needed) the LangGraph agent, persisting the outcome and updating
    metrics/exceptions in place.

    Transaction shape, and why it is three steps rather than one:
        1. Persist the bank credit and the settlement, then COMMIT.
        2. Call the agent (network, seconds, retries) holding NO transaction.
        3. Persist the verdict, then COMMIT.
        Step 2 used to run inside the transaction opened in step 1, so every
        in-flight LLM call held database locks and an idle-in-transaction
        connection for its entire duration. With a real model and a real batch
        that is how a reconciliation run exhausts the connection pool.

    Metrics are updated only AFTER the verdict commits, so a record that fails
    to persist is never counted as resolved.

    Args:
        record: the synthetic record to process.
        graph: the compiled LangGraph agent.
        run_id: the batch run id, persisted on every reconciliation row.
        metrics: mutated in place.
        exceptions: mutated in place; becomes the exceptions manifest.

    Exception vectors handled:
        BankLedgerConflictError is counted separately as a claim conflict (the
        ledger refusing to let two settlements claim one credit); anything else
        is counted as a processing error. Neither propagates -- one bad record
        must not abort the batch -- but both are reported, never swallowed.
    """
    try:
        utr = LegacyBankParser.extract_utr(record.bank_name, record.narration)
        math_result = DeterministicRulesEngine.evaluate_match(
            gross=record.gross,
            fees=record.fees,
            taxes=record.taxes,
            refunds=record.refunds,
            adjustments=record.adjustments,
            bank_credit=record.bank_credit,
        )

        # Step 1: the two facts about the outside world, committed on their own.
        async with AsyncSessionLocal() as session:
            bank_entry, _ = await get_or_create_bank_entry(
                session,
                bank_name=record.bank_name,
                transaction_date=record.transaction_date,
                credit_amount=record.bank_credit,
                raw_narration=record.narration,
                extracted_utr=utr,
            )
            bank_entry_id = bank_entry.entry_id
            await upsert_settlement(
                session,
                settlement_id=record.settlement_id,
                status="processed",
                gross_amount=record.gross,
                fees=record.fees,
                taxes=record.taxes,
                refunds=record.refunds,
                adjustments=record.adjustments,
                net_settlement=math_result.expected_net,
                utr_reference=utr,
            )
            await session.commit()

        # Step 2: reason about it, holding nothing open. A deterministic match
        # never reaches the agent at all -- that is the point of having one.
        classification = None
        agent_thread_id = None
        manifest_entry = None
        sanitized_context = None

        if math_result.is_resolved and utr is not None:
            recon_state = "DETERMINISTIC_MATCH"
        else:
            sanitized_context = _sanitized_context(record, math_result, utr)
            agent_thread_id = f"eval-{record.settlement_id}"
            agent_result = await graph.ainvoke(
                {
                    "settlement_id": record.settlement_id,
                    "sanitized_context": sanitized_context,
                    # The ledger's own arithmetic, handed to the graph so its
                    # guards can cross-check the model's claimed variance against
                    # it instead of taking the model's word for the number.
                    "deterministic_variance_str": money_to_str(math_result.variance),
                },
                config={"configurable": {"thread_id": agent_thread_id}},
            )
            # State holds a plain dict so it survives checkpointing; this
            # revalidates it back into the typed verdict, so .variance is a real
            # Decimal rather than a string this module has to trust.
            classification = classification_from_state(agent_result)

            if "__interrupt__" in agent_result:
                recon_state = "PENDING_HITL_REVIEW"
                manifest_entry = {
                    "settlement_id": record.settlement_id,
                    "batch_run_id": run_id,
                    "scenario": record.scenario,
                    # Both numbers, labelled. The deterministic one is the
                    # ledger's; ai_reported_variance is the model's claim.
                    "variance": money_to_str(math_result.variance),
                    "ai_reported_variance": money_to_str(classification.variance),
                    "reason": classification.discrepancy_reason,
                    "confidence_score": classification.confidence_score,
                    "review_reasons": list(agent_result.get("review_reasons") or []),
                    "agent_thread_id": agent_thread_id,
                    "raw_context": sanitized_context,
                }
            else:
                recon_state = "AI_RESOLVED"

        # Step 3: the verdict, committed on its own.
        async with AsyncSessionLocal() as session:
            await upsert_reconciliation(
                session,
                settlement_id=record.settlement_id,
                bank_entry_id=bank_entry_id,
                recon_state=recon_state,
                numeric_variance=math_result.variance,
                cryptographic_state_hash=reconciliation_state_hash(
                    settlement_id=record.settlement_id,
                    recon_state=recon_state,
                    variance=math_result.variance,
                    raw_narration=record.narration,
                ),
                ai_classification_reason=(
                    classification.discrepancy_reason if classification is not None else None
                ),
                ai_reported_variance=(
                    classification.variance if classification is not None else None
                ),
                ai_confidence_score=(
                    Decimal(str(classification.confidence_score)).quantize(
                        _CONFIDENCE_QUANTUM, rounding=ROUND_HALF_UP
                    )
                    if classification is not None
                    else None
                ),
                agent_thread_id=agent_thread_id,
                batch_run_id=run_id,
            )
            # The credit is now claimed by this settlement's reconciliation row,
            # whatever the state -- including PENDING_HITL_REVIEW, where the claim
            # is exactly what must stop a second settlement taking the same money
            # while a human is still deciding.
            await mark_bank_entry_reconciled(session, bank_entry_id)
            await session.commit()

        metrics["states"][recon_state] = metrics["states"].get(recon_state, 0) + 1
        if recon_state == "DETERMINISTIC_MATCH":
            metrics["deterministic_matches"] += 1
        elif recon_state == "AI_RESOLVED":
            metrics["ai_matches"] += 1
        else:
            metrics["hitl_exceptions"] += 1
        if recon_state == record.expected_state:
            metrics["ground_truth_agreements"] += 1
        else:
            metrics["ground_truth_disagreements"].append(
                {
                    "settlement_id": record.settlement_id,
                    "scenario": record.scenario,
                    "expected_state": record.expected_state,
                    "actual_state": recon_state,
                }
            )
        if manifest_entry is not None:
            exceptions.append(manifest_entry)

    except BankLedgerConflictError as exc:
        # Not a generic failure: the ledger refused to let this settlement claim a
        # bank credit another settlement's reconciliation already owns. Counted
        # under its own name so it can never be mistaken for a transient error.
        metrics["claim_conflicts"] += 1
        exceptions.append(
            {
                "settlement_id": record.settlement_id,
                "batch_run_id": run_id,
                "scenario": record.scenario,
                "variance": None,
                "reason": "BANK_CREDIT_ALREADY_CLAIMED",
                "raw_context": f"{type(exc).__name__}: {exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001 -- one bad record must never abort the batch
        metrics["processing_errors"] += 1
        exceptions.append(
            {
                "settlement_id": record.settlement_id,
                "batch_run_id": run_id,
                "scenario": record.scenario,
                "variance": None,
                "reason": "PROCESSING_ERROR",
                "raw_context": f"{type(exc).__name__}: {exc}",
            }
        )


def _percentage(part: int, whole: int) -> Decimal:
    """
    A two-decimal percentage, computed in Decimal.

    Args:
        part: the numerator.
        whole: the denominator.

    Returns:
        part/whole as a percentage, or 0.00 for an empty batch -- an empty run has
        no rate, and reporting one would be inventing a number.
    """
    if not whole:
        return Decimal("0.00")
    return (Decimal(part) / Decimal(whole) * Decimal(100)).quantize(Decimal("0.01"))


async def run_evaluation(
    graph_context_manager=reconciliation_graph,
    run_id: str | None = None,
    manifest_path: Optional[Path] = None,
) -> dict:
    """
    Run the full 52-record benchmark batch end to end: generate it, process every
    record, print the aggregated console report, and write the exceptions
    manifest.

    Args:
        graph_context_manager: a zero-argument async context manager factory
            yielding a compiled graph, matching agents.graph.reconciliation_graph's
            signature. Defaults to the real Gemini + Postgres-checkpointed graph;
            tests substitute a fake LLM here to run without network access or an
            API key.
        run_id: override the auto-generated run id. Normally left as None (a fresh
            one is generated every run); tests pass a known value so they can
            query the database for exactly this run's rows afterward.
        manifest_path: where to write the exceptions manifest. Defaults to
            exceptions_manifest.json at the project root -- the real, committed
            artifact. Tests pass a temporary path, because a test run overwriting
            the published manifest replaces a real benchmark result with a
            synthetic one, which is precisely the kind of quiet dishonesty this
            file's numbers are supposed to be free of.

    Returns:
        The metrics dict, for callers/tests that want it programmatically. Keys:
        total_processed, deterministic_matches, ai_matches, hitl_exceptions,
        claim_conflicts, processing_errors, states, ground_truth_agreements,
        ground_truth_disagreements, match_rate, ground_truth_accuracy, run_id.
    """
    run_id = run_id or uuid.uuid4().hex[:8].upper()
    target_manifest = manifest_path or _MANIFEST_PATH
    batch = generate_synthetic_batch(run_id)
    metrics: dict = {
        "run_id": run_id,
        "total_processed": len(batch),
        "deterministic_matches": 0,
        "ai_matches": 0,
        "hitl_exceptions": 0,
        "claim_conflicts": 0,
        "processing_errors": 0,
        "states": {},
        "ground_truth_agreements": 0,
        "ground_truth_disagreements": [],
    }
    exceptions: list[dict] = []

    async with graph_context_manager() as graph:
        for record in batch:
            await _process_record(record, graph, run_id, metrics, exceptions)

    total = metrics["total_processed"]
    total_matches = metrics["deterministic_matches"] + metrics["ai_matches"]
    metrics["match_rate"] = str(_percentage(total_matches, total))
    metrics["ground_truth_accuracy"] = str(_percentage(metrics["ground_truth_agreements"], total))

    print("\n--- BATCH METRICS ---")
    print(f"Run ID:                   {run_id}")
    print(f"Total Processed:          {total}")
    print(f"Deterministic Matches:    {metrics['deterministic_matches']}")
    print(f"AI Matches:               {metrics['ai_matches']}")
    print(f"Unresolvable HITL:        {metrics['hitl_exceptions']}")
    if metrics["claim_conflicts"]:
        print(f"Bank Credit Conflicts:    {metrics['claim_conflicts']}")
    if metrics["processing_errors"]:
        print(f"Processing Errors:        {metrics['processing_errors']}")
    print(f"Final Match Rate:         {metrics['match_rate']}%")
    print(f"Ground-Truth Accuracy:    {metrics['ground_truth_accuracy']}%")
    if metrics["ground_truth_disagreements"]:
        # Printed, not hidden behind a percentage: a disagreement names a record
        # whose classification a human should look at.
        print("\nRecords that did not reach their expected state:")
        for item in metrics["ground_truth_disagreements"]:
            print(
                f"  {item['settlement_id']} ({item['scenario']}): "
                f"expected {item['expected_state']}, got {item['actual_state']}"
            )
    print()

    target_manifest.write_text(json.dumps(exceptions, indent=4))
    print(f"Exceptions manifest written to {target_manifest}")

    return metrics


if __name__ == "__main__":
    asyncio.run(run_evaluation())



