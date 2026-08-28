"""
Deterministic reconciliation math for Razorpay Vulcan Ledger.

This module is the purely-mathematical pass of the reconciliation
pipeline: it decides, using only exact Decimal arithmetic and a fixed
tolerance window, whether a Razorpay settlement's expected net payout
matches what actually landed in the bank ledger. Anything this module
can't resolve gets handed to the LangGraph agent (Phase 4) -- this module
itself never calls an LLM and never touches the database; it is a pure
function over Decimal inputs, deliberately kept unit-testable in isolation.

Currency handling:
    Every parameter here MUST already be a decimal.Decimal constructed via
    Decimal(str(x)) at the point the value first entered the system (the
    ORM layer returns Decimal automatically; anything read from a JSON
    payload or a webhook must be run through Decimal(str(x)) before it
    reaches this module). Passing a float raises TypeError immediately --
    see _ensure_decimal -- rather than silently reintroducing binary
    floating-point error into a financial calculation.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from core.money import money_to_str, quantize_money

MATCH_TOLERANCE = Decimal("0.02")


class ReconciliationResult(BaseModel):
    """
    Outcome of running the deterministic rules engine against one
    settlement/bank-credit pair.

    Attributes:
        status: "MATCHED" if the variance is within MATCH_TOLERANCE,
            otherwise "UNRESOLVED_DISCREPANCY".
        expected_net: Gross - Fees - Taxes - Refunds - Adjustments, computed
            entirely in Decimal and normalized to exactly two decimal places.
        variance: expected_net - bank_credit. Positive means the bank
            received less than expected; negative means more.
        is_resolved: True only when status == "MATCHED" -- callers (the
            evaluation runner, the LangGraph fetch node) branch on this
            instead of string-comparing status.
        explanation: a plain-language account of the arithmetic behind this
            verdict. Not decoration: it is what a finance operator reads
            instead of the backend logs, it is what gets embedded in the
            sanitized context handed to the agent, and it means every
            deterministic decision can be justified without re-deriving it.
    """

    status: Literal["MATCHED", "UNRESOLVED_DISCREPANCY"]
    expected_net: Decimal
    variance: Decimal
    is_resolved: bool
    explanation: str


def _ensure_decimal(value: object, name: str) -> Decimal:
    """
    Validate and normalize one currency input.

    Delegates to core.money.quantize_money so the rules engine, the ORM layer,
    the bank parsers, and the agent's output validation cannot drift apart on
    what counts as a valid amount.

    Args:
        value: the value to check.
        name: the parameter name, used only to make the error actionable.

    Returns:
        The amount as a finite Decimal with exactly two decimal places.

    Raises:
        TypeError: if value is a float (even one that "looks exact", e.g.
            1000.0) or any other non-Decimal type. This is deliberately a
            hard failure, not a silent Decimal(str(value)) rescue -- the
            fix belongs at the point the float was created, not here.
        ValueError: if value is non-finite (NaN/Infinity), carries more than
            two decimal places, or overflows DECIMAL(15,2).
    """
    return quantize_money(value, name)


class DeterministicRulesEngine:
    """
    Stateless deterministic matcher. Every method is a staticmethod: this
    class exists purely as a namespace, not to hold state, since a class
    instance would otherwise misleadingly suggest per-settlement state that
    doesn't exist.
    """

    @staticmethod
    def evaluate_match(
        gross: Decimal,
        fees: Decimal,
        taxes: Decimal,
        refunds: Decimal,
        adjustments: Decimal,
        bank_credit: Decimal,
    ) -> ReconciliationResult:
        """
        Apply Net = Gross - Fees - Taxes - Refunds - Adjustments and compare
        against what the bank actually credited, within MATCH_TOLERANCE
        (0.02 INR).

        Args:
            gross: gross settlement amount before any deductions.
            fees: Razorpay gateway fees.
            taxes: GST/tax withheld on fees.
            refunds: refunds issued against this settlement.
            adjustments: cross-settlement / chargeback adjustments (the
                "Cross_Settlement_Adjustments" term in the accounting
                equation).
            bank_credit: the actual credit_amount from t_bank_ledger.

            All six arguments must already be decimal.Decimal -- see
            _ensure_decimal.

        Returns:
            A ReconciliationResult. status is "MATCHED" when
            abs(variance) <= 0.02 INR, otherwise "UNRESOLVED_DISCREPANCY"
            and the caller should route to the LangGraph agent (Phase 4).

        Exception vectors handled:
            TypeError: raised by _ensure_decimal if any argument is a float
                or otherwise not a Decimal -- fails loudly at the boundary
                rather than producing a silently wrong reconciliation.
        """
        gross = _ensure_decimal(gross, "gross")
        fees = _ensure_decimal(fees, "fees")
        taxes = _ensure_decimal(taxes, "taxes")
        refunds = _ensure_decimal(refunds, "refunds")
        adjustments = _ensure_decimal(adjustments, "adjustments")
        bank_credit = _ensure_decimal(bank_credit, "bank_credit")

        expected_net = quantize_money(
            gross - fees - taxes - refunds - adjustments, "expected_net"
        )
        variance = quantize_money(expected_net - bank_credit, "variance")

        status: Literal["MATCHED", "UNRESOLVED_DISCREPANCY"]
        if abs(variance) <= MATCH_TOLERANCE:
            status = "MATCHED"
        else:
            status = "UNRESOLVED_DISCREPANCY"

        explanation = DeterministicRulesEngine._explain(
            gross=gross,
            fees=fees,
            taxes=taxes,
            refunds=refunds,
            adjustments=adjustments,
            bank_credit=bank_credit,
            expected_net=expected_net,
            variance=variance,
            status=status,
        )

        return ReconciliationResult(
            status=status,
            expected_net=expected_net,
            variance=variance,
            is_resolved=(status == "MATCHED"),
            explanation=explanation,
        )

    @staticmethod
    def _explain(
        *,
        gross: Decimal,
        fees: Decimal,
        taxes: Decimal,
        refunds: Decimal,
        adjustments: Decimal,
        bank_credit: Decimal,
        expected_net: Decimal,
        variance: Decimal,
        status: str,
    ) -> str:
        """
        Render the arithmetic behind one verdict as a single readable line.

        Every amount goes through money_to_str, so the explanation an operator
        reads, the string embedded in the agent's sanitized context, and the
        value stored in the database are all the same canonical two-decimal
        text -- there is no second formatting path that could disagree.

        Args:
            gross, fees, taxes, refunds, adjustments, bank_credit: the
                validated inputs, already normalized to two decimal places.
            expected_net: the computed net payout.
            variance: expected_net - bank_credit.
            status: "MATCHED" or "UNRESOLVED_DISCREPANCY".

        Returns:
            A plain-language sentence naming every term, the tolerance it was
            judged against, and which side of that tolerance it fell on.
        """
        equation = (
            f"expected_net = gross {money_to_str(gross)} "
            f"- fees {money_to_str(fees)} "
            f"- taxes {money_to_str(taxes)} "
            f"- refunds {money_to_str(refunds)} "
            f"- adjustments {money_to_str(adjustments)} "
            f"= {money_to_str(expected_net)} INR"
        )
        comparison = (
            f"bank credited {money_to_str(bank_credit)} INR, "
            f"variance {money_to_str(variance)} INR "
            f"(tolerance +/-{money_to_str(MATCH_TOLERANCE)} INR)"
        )
        if status == "MATCHED":
            verdict = "within tolerance, so this settlement is deterministically MATCHED"
        else:
            verdict = (
                "outside tolerance, so the deterministic pass cannot explain the "
                "difference and the settlement is routed to the AI agent"
            )
        return f"{equation}; {comparison}; {verdict}."