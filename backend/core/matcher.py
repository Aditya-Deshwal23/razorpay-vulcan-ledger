"""
Deterministic bank-credit matching for Razorpay Vulcan Ledger.

The rules engine (backend/core/rules_engine.py) answers "does this settlement's
arithmetic agree with this bank credit?". It cannot answer the question that comes
first: WHICH bank credit belongs to this settlement. That is this module's job,
and it is deliberately a separate one -- pairing rows and checking arithmetic fail
in different ways, and conflating them produces a matcher that quietly explains
away its own mis-pairings as variances.

The matching rules, in strict priority order:

1. UTR equality. A UTR is the bank's own unique reference for one NEFT/RTGS
   transfer, so an exact match is proof of identity and outranks everything else.
   If the amounts then disagree, that is a real variance for the rules engine to
   report -- not a reason to go looking for a different credit that fits better.
   Choosing the better-fitting credit instead is how a reconciliation system
   manufactures a clean match rate out of genuinely broken data.
2. Amount within MATCH_TOLERANCE AND transaction date within a configured window
   of the settlement date. Used only when no UTR is available on either side, and
   only within one bank.
3. Anything else is refused, explicitly: AMBIGUOUS when more than one credit fits
   the rule that was applied, NO_CANDIDATE when none does. Neither is an error --
   both are honest outcomes that route the settlement to review.

This module never guesses between equally good candidates. Two credits of the
same amount on the same day are indistinguishable from here, and picking the
first would decide a real financial question on row order.

Currency handling:
    Every amount must already be a decimal.Decimal; they are re-validated through
    core.money.quantize_money at the boundary, so a float cannot reach the
    comparison that decides whether two amounts are "the same".
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import BankLedgerEntry
from core.money import money_to_str, quantize_money
from core.rules_engine import MATCH_TOLERANCE

MatchStatus = Literal[
    "MATCHED_BY_UTR",
    "MATCHED_BY_AMOUNT_AND_DATE",
    "AMBIGUOUS",
    "NO_CANDIDATE",
]

@dataclass(frozen=True)
class MatchCandidate:
    """
    The only facts about a bank credit that matching is allowed to use.

    A plain value object rather than an ORM row, so select_bank_credit() is a pure
    function testable with no database, and so no lazy-loaded relationship can
    smuggle extra state into a matching decision.

    Attributes:
        entry_id: t_bank_ledger.entry_id.
        bank_name: which bank the credit landed at.
        transaction_date: the credit's value date.
        credit_amount: the amount credited, as a two-decimal Decimal.
        extracted_utr: the UTR parsed from the narration, or None if the narration
            had no unambiguous one.
    """

    entry_id: uuid.UUID
    bank_name: str
    transaction_date: date
    credit_amount: Decimal
    extracted_utr: Optional[str]

    @classmethod
    def from_row(cls, row: BankLedgerEntry) -> "MatchCandidate":
        """
        Project an ORM bank-ledger row down to the matchable facts.

        Args:
            row: a loaded BankLedgerEntry.

        Returns:
            The corresponding MatchCandidate.
        """
        return cls(
            entry_id=row.entry_id,
            bank_name=row.bank_name,
            transaction_date=row.transaction_date,
            credit_amount=row.credit_amount,
            extracted_utr=row.extracted_utr,
        )


class MatchOutcome(BaseModel):
    """
    The result of trying to pair one settlement with one bank credit.

    Attributes:
        status: which rule fired, or why none could.
        entry_id: the matched bank credit, or None for AMBIGUOUS/NO_CANDIDATE. A
            caller must branch on is_matched rather than assuming this is set.
        considered: every candidate the deciding rule found admissible. For an
            AMBIGUOUS outcome this is the list a human needs in order to choose;
            it is retained precisely so "ambiguous" is actionable rather than a
            dead end.
        explanation: a plain-language account of the decision, in the same spirit
            as ReconciliationResult.explanation -- what an operator reads instead
            of the logs.
    """

    model_config = ConfigDict(frozen=True)

    status: MatchStatus
    entry_id: Optional[uuid.UUID]
    considered: tuple[uuid.UUID, ...]
    explanation: str

    @property
    def is_matched(self) -> bool:
        """True only when exactly one bank credit was identified."""
        return self.entry_id is not None

def select_bank_credit(
    *,
    expected_net: Decimal,
    settlement_date: date,
    settlement_utr: Optional[str],
    candidates: Sequence[MatchCandidate],
    window_days: int,
    tolerance: Decimal = MATCH_TOLERANCE,
) -> MatchOutcome:
    """
    Decide which of several bank credits belongs to one settlement.

    Args:
        expected_net: the settlement's expected net payout (the rules engine's
            Gross - Fees - Taxes - Refunds - Adjustments).
        settlement_date: the date the settlement was made, used as the centre of
            the date window when matching by amount.
        settlement_utr: the UTR Razorpay reported for this settlement, or None.
        candidates: the bank credits worth considering. The caller is responsible
            for having already excluded credits that are reconciled against
            another settlement -- this function judges suitability, not
            availability.
        window_days: how many days either side of settlement_date a credit may
            fall and still be considered the same payout when no UTR proves it.
            0 means same-day only.
        tolerance: the amount window, defaulting to the system-wide
            MATCH_TOLERANCE of 0.02 INR. Exposed as a parameter for testing a
            stricter rule, never to loosen the production one.

    Returns:
        A MatchOutcome. status is MATCHED_BY_UTR or MATCHED_BY_AMOUNT_AND_DATE
        with entry_id set; AMBIGUOUS when the rule that fired admitted more than
        one credit; NO_CANDIDATE when nothing was admissible.

    Raises:
        TypeError: if expected_net or any candidate amount is a float rather than
            a Decimal (via core.money).
        ValueError: if window_days is negative, or an amount is non-finite or
            carries more than two decimals.
    """
    if window_days < 0:
        raise ValueError(f"window_days must not be negative, got {window_days}")

    expected = quantize_money(expected_net, "expected_net")
    limit = quantize_money(tolerance, "tolerance")

    if settlement_utr:
        utr_hits = [c for c in candidates if c.extracted_utr == settlement_utr]
        if len(utr_hits) == 1:
            hit = utr_hits[0]
            amount = quantize_money(hit.credit_amount, "credit_amount")
            note = (
                "amount agrees"
                if abs(expected - amount) <= limit
                else (
                    f"amount differs by {money_to_str(expected - amount)} INR, which the "
                    "rules engine will report as the variance -- the UTR still proves "
                    "this is the right credit"
                )
            )
            return MatchOutcome(
                status="MATCHED_BY_UTR",
                entry_id=hit.entry_id,
                considered=(hit.entry_id,),
                explanation=(
                    f"UTR {settlement_utr} matched bank credit {hit.entry_id} at "
                    f"{hit.bank_name} on {hit.transaction_date.isoformat()} for "
                    f"{money_to_str(amount)} INR; {note}."
                ),
            )
        if len(utr_hits) > 1:
            return MatchOutcome(
                status="AMBIGUOUS",
                entry_id=None,
                considered=tuple(c.entry_id for c in utr_hits),
                explanation=(
                    f"{len(utr_hits)} bank credits carry UTR {settlement_utr}. One UTR is "
                    "one transfer, so this is a data conflict a human must resolve; "
                    "matching either one could double-count a payout."
                ),
            )

    window = timedelta(days=window_days)
    fits = [
        c
        for c in candidates
        if abs(expected - quantize_money(c.credit_amount, "credit_amount")) <= limit
        and abs(c.transaction_date - settlement_date) <= window
    ]

    if len(fits) == 1:
        hit = fits[0]
        return MatchOutcome(
            status="MATCHED_BY_AMOUNT_AND_DATE",
            entry_id=hit.entry_id,
            considered=(hit.entry_id,),
            explanation=(
                f"no UTR was available to prove identity, so matching fell back to "
                f"amount and date: exactly one credit ({hit.entry_id}) at {hit.bank_name} "
                f"is within +/-{money_to_str(limit)} INR of the expected "
                f"{money_to_str(expected)} INR and within {window_days} day(s) of "
                f"{settlement_date.isoformat()} (it is dated "
                f"{hit.transaction_date.isoformat()})."
            ),
        )

    if len(fits) > 1:
        return MatchOutcome(
            status="AMBIGUOUS",
            entry_id=None,
            considered=tuple(c.entry_id for c in fits),
            explanation=(
                f"{len(fits)} bank credits are each within +/-{money_to_str(limit)} INR of "
                f"{money_to_str(expected)} INR and within {window_days} day(s) of "
                f"{settlement_date.isoformat()}, and no UTR distinguishes them. Choosing "
                "one would decide a financial question on row order, so this needs a human."
            ),
        )

    return MatchOutcome(
        status="NO_CANDIDATE",
        entry_id=None,
        considered=(),
        explanation=(
            f"none of the {len(candidates)} candidate credit(s) matched: no UTR equality"
            + (f" for {settlement_utr}" if settlement_utr else " (settlement has no UTR)")
            + f", and none within +/-{money_to_str(limit)} INR of "
            f"{money_to_str(expected)} INR and {window_days} day(s) of "
            f"{settlement_date.isoformat()}."
        ),
    )

async def find_bank_credit(
    session: AsyncSession,
    *,
    bank_name: str,
    expected_net: Decimal,
    settlement_date: date,
    settlement_utr: Optional[str],
    window_days: int,
    include_reconciled: bool = False,
) -> MatchOutcome:
    """
    Load the plausible bank credits for one settlement and pick between them.

    The database query is deliberately narrow -- one bank, and either the exact UTR
    or the date window -- so the pure decision function receives a handful of rows
    rather than the whole ledger, and so a growing bank ledger does not slow
    matching down. Reconciled credits are excluded by default: a credit already
    backing another settlement is not available, and offering it as a candidate is
    how double-counting starts.

    Args:
        session: an open AsyncSession.
        bank_name: only credits at this bank are considered.
        expected_net: the settlement's expected net payout.
        settlement_date: the settlement date, centre of the date window.
        settlement_utr: the settlement's UTR, or None.
        window_days: half-width of the date window, in days.
        include_reconciled: when True, already-matched credits are also loaded.
            Only for diagnostics (answering "which settlement took this credit?");
            never for live matching.

    Returns:
        A MatchOutcome from select_bank_credit over whatever the query found. An
        empty result set yields NO_CANDIDATE, not an exception -- a settlement whose
        money has not arrived yet is a normal state of the world.

    Raises:
        TypeError / ValueError: as select_bank_credit, for invalid amounts or a
            negative window.
    """
    if window_days < 0:
        raise ValueError(f"window_days must not be negative, got {window_days}")

    window = timedelta(days=window_days)
    date_window = (BankLedgerEntry.transaction_date >= settlement_date - window) & (
        BankLedgerEntry.transaction_date <= settlement_date + window
    )

    stmt = select(BankLedgerEntry).where(BankLedgerEntry.bank_name == bank_name)
    if settlement_utr:
        stmt = stmt.where(or_(BankLedgerEntry.extracted_utr == settlement_utr, date_window))
    else:
        stmt = stmt.where(date_window)
    if not include_reconciled:
        stmt = stmt.where(BankLedgerEntry.is_reconciled.is_(False))

    rows = (await session.scalars(stmt)).all()

    return select_bank_credit(
        expected_net=expected_net,
        settlement_date=settlement_date,
        settlement_utr=settlement_utr,
        candidates=[MatchCandidate.from_row(row) for row in rows],
        window_days=window_days,
    )



