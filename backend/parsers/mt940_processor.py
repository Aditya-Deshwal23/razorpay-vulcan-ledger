"""
Legacy bank-statement parsing for Razorpay Vulcan Ledger.

Two jobs, both concerned with turning what a bank actually sends into
something the reconciliation pipeline can use:

1. parse_mt940_statement(): read a real SWIFT MT940 statement -- the format
   every Indian bank's "download statement (MT940)" button produces -- into
   structured BankStatementLine records. Each :61: transaction line and its
   following :86: narration become one record with a Decimal amount, a value
   date, and the UTR extracted from the narration.
2. LegacyBankParser.extract_utr(): the narration-level UTR extraction used both
   by the statement parser and directly by callers that already have a narration
   string (the Phase 5 evaluation runner, webhook payloads).

Real MT940/BAI2/CSV exports from Indian banks embed the NEFT/RTGS UTR (Unique
Transaction Reference) inside a free-text narration field using a different,
bank-specific convention -- HDFC prefixes with "/INF/", ICICI with "/TXT/" and a
hyphen before the UTR, and other banks vary further. Neither function touches the
database and neither does currency math (see backend/core/rules_engine.py for
that); amounts are parsed through core.money so a statement can never introduce a
float into the system.

Ambiguity is not resolved by guessing:
    When the generic fallback finds two or more DIFFERENT UTR-shaped tokens in one
    narration, extract_utr returns None rather than picking the first. Picking one
    would silently attach a bank credit to whichever settlement happened to be
    named first in the text -- a wrong match that reads as a confident match.
    None routes the row to review, which is the honest outcome.

Exception vectors handled:
    - narration is None or empty -> returns None, does not raise (an empty
      narration is a data-quality fact to log upstream, not a parser bug).
    - bank_name not in the registry (unsupported bank) -> falls through to the
      generic fallback pattern rather than raising, since a new bank showing up in
      a statement feed should degrade gracefully, not crash the ingestion batch.
    - narration contains no UTR-shaped token, or several conflicting ones ->
      returns None; the caller is responsible for treating a None UTR as "needs
      human/AI review", not this module.
    - a malformed MT940 :61: line, an unparseable MT940 amount, or a :86:
      narration with no preceding :61: -> MT940ParseError naming the offending
      line. A statement file that cannot be parsed is NOT silently truncated to
      the transactions that happened to parse: that would under-report a bank's
      credits and make an unreconciled payout look like it never arrived.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Pattern

from core.money import parse_money

# One compiled pattern per bank, matched in the order the narration is
# expected to actually appear for that bank. Adding a new bank is a single
# new dict entry -- no branching logic to touch.
_BANK_PATTERNS: dict[str, Pattern[str]] = {
    "HDFC": re.compile(r"/INF/(?:NEFT|RTGS)\s*(?:CR|DR)[:\s]+([A-Z0-9]{16,22})"),
    "ICICI": re.compile(r"/TXT/(?:NEFT|RTGS)-([A-Z0-9]{16,22})"),
    "AXIS": re.compile(r"(?:NEFT|RTGS)[-\s]+([A-Z0-9]{16,22})"),
}

# Last-resort pattern for banks not in _BANK_PATTERNS, or for a
# bank-specific pattern that didn't match: any bare 16-22 char uppercase
# alphanumeric token, which is what a UTR looks like regardless of bank.
_FALLBACK_PATTERN: Pattern[str] = re.compile(r"\b([A-Z0-9]{16,22})\b")

# A real UTR is 16-22 uppercase alphanumerics AND contains at least one digit.
# Without the digit requirement the fallback happily returned words -- a
# 16-letter customer name in a narration ("ACMECORPORATION") is not a transaction
# reference, but it is [A-Z]{16} and used to be accepted as one.
_UTR_SHAPE: Pattern[str] = re.compile(r"^(?=.*\d)[A-Z0-9]{16,22}$")

# An MT940 tag at the start of a line, e.g. ":61:" or ":86:" or ":62F:".
_TAG_PATTERN: Pattern[str] = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")

# The :61: statement line. Field 61 in SWIFT terms:
#   6!n  value date YYMMDD
#   [4!n] entry date MMDD (optional)
#   2a   debit/credit mark: C, D, RC (reversal of credit) or RD
#   [1a] funds code (currency's 3rd letter) -- optional
#   15d  amount, comma as the decimal separator ("976,40")
#   4!c  transaction type identification code, e.g. NTRF, NMSC
#   16x  reference for the account owner, then optionally //bank reference
# The optional 4-digit entry date and the amount cannot be confused because the
# debit/credit mark between them is alphabetic: the regex backtracks off the
# optional group when the next character is C/D/R.
_MT940_61_PATTERN: Pattern[str] = re.compile(
    r"^(?P<value_date>\d{6})"
    r"(?P<entry_date>\d{4})?"
    r"(?P<mark>R?[CD])"
    r"(?P<funds_code>[A-Z])?"
    r"(?P<amount>[\d,]+)"
    r"(?P<type_code>[A-Z][A-Z0-9]{3})"
    r"(?P<reference>.*)$"
)


class MT940ParseError(ValueError):
    """
    Raised when an MT940 statement cannot be parsed unambiguously.

    A ValueError subclass so a caller that treats bad input generically still
    catches it, while a caller that wants to distinguish "this file is not valid
    MT940" from "this amount is not valid money" can.
    """


@dataclass(frozen=True)
class BankStatementLine:
    """
    One transaction from an MT940 statement, normalized.

    Attributes:
        value_date: the :61: value date, as a real date object.
        entry_date: the :61: entry (booking) date if the bank supplied one. MT940
            gives only MMDD here, so the year is taken from value_date, with a
            year-boundary correction (a January booking date on a December value
            date belongs to the following year).
        is_credit: True for C/RC, False for D/RD. Reconciliation only cares about
            credits, but debits are parsed rather than dropped so a caller can see
            the whole statement.
        is_reversal: True for the RC/RD reversal marks. A reversal is not a fresh
            payout and must never be matched as one.
        amount: the transaction amount as a positive Decimal with two decimal
            places. Sign lives in is_credit, not in the number, matching
            t_bank_ledger.credit_amount's CHECK (credit_amount >= 0).
        transaction_type: the 4-character MT940 type code, e.g. "NTRF".
        reference: the raw reference field from :61:.
        narration: the concatenated :86: information lines, or "" if the bank sent
            none for this transaction.
        extracted_utr: the UTR found in the narration, or None.
    """

    value_date: date
    entry_date: Optional[date]
    is_credit: bool
    is_reversal: bool
    amount: Decimal
    transaction_type: str
    reference: str
    narration: str
    extracted_utr: Optional[str]

def _mt940_date(yymmdd: str, raw_line: str) -> date:
    """
    Convert an MT940 6-digit value date to a real date.

    Args:
        yymmdd: exactly six digits, e.g. "260815".
        raw_line: the line it came from, quoted in the error if it is invalid.

    Returns:
        The date, with the two-digit year read as 20YY. MT940 has no century
        field; every statement this system ingests is from the 2000s.

    Raises:
        MT940ParseError: if the digits are not a real calendar date (e.g. month
            13, or 30 February).
    """
    try:
        return date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError as exc:
        raise MT940ParseError(f"invalid MT940 date {yymmdd!r} in line {raw_line!r}: {exc}") from exc


def _mt940_entry_date(mmdd: str, value_date: date, raw_line: str) -> date:
    """
    Convert an MT940 4-digit entry date (MMDD, no year) to a real date.

    Args:
        mmdd: exactly four digits.
        value_date: the same transaction's value date, which supplies the year.
        raw_line: the line it came from, for error messages.

    Returns:
        The entry date. If the resulting month is far ahead of the value date's
        month, the entry date belongs to the previous year (a December booking
        with a January value date); if it is far behind, to the next year. Without
        this correction a year-boundary statement produces entry dates eleven
        months away from their own value date.

    Raises:
        MT940ParseError: if the digits are not a real calendar date.
    """
    month, day = int(mmdd[0:2]), int(mmdd[2:4])
    for year in (value_date.year, value_date.year - 1, value_date.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if abs((candidate - value_date).days) <= 183:
            return candidate
    raise MT940ParseError(f"invalid MT940 entry date {mmdd!r} in line {raw_line!r}")


def _mt940_amount(raw_amount: str, raw_line: str) -> Decimal:
    """
    Parse an MT940 amount, which uses a comma as its decimal separator.

    Args:
        raw_amount: the amount as it appears in :61:, e.g. "976,40" or "1000,".
        raw_line: the line it came from, for error messages.

    Returns:
        A Decimal with exactly two decimal places, via core.money.parse_money --
        the same validation every other amount in the system passes through.

    Raises:
        MT940ParseError: if the amount is not a valid two-decimal monetary value.
            Note the comma is translated to a period FIRST: core.money.parse_money
            treats commas as Indian thousands separators, so handing it "976,40"
            unchanged would silently yield 97640.00 -- a hundredfold error. The
            LAST comma is the decimal separator, so the split is from the right.
    """
    integer_part, _, fraction = raw_amount.rpartition(",")
    if not integer_part:
        # No comma at all: MT940 requires one, but a bank omitting it on a whole
        # number of rupees is a benign deviation, not a reason to reject the file.
        normalized = fraction
    else:
        normalized = f"{integer_part.replace(',', '')}.{fraction or '00'}"
    try:
        return parse_money(normalized, "mt940_amount")
    except (TypeError, ValueError) as exc:
        raise MT940ParseError(
            f"invalid MT940 amount {raw_amount!r} in line {raw_line!r}: {exc}"
        ) from exc

def parse_mt940_statement(content: str, bank_name: str) -> list[BankStatementLine]:
    """
    Parse a full MT940 statement into structured transaction records.

    MT940 is line-oriented: a `:61:` line opens a transaction, an optional `:86:`
    line (which may continue over several unprefixed lines) carries its narration,
    and any other tag (`:60F:` opening balance, `:62F:` closing balance, `:20:` a
    new statement block) closes the current transaction. This function walks the
    file in that order, so a statement containing several :20: blocks is handled as
    one continuous sequence of transactions.

    Args:
        content: the raw statement text, as downloaded. CRLF and lone-CR line
            endings are handled; a trailing "-" record separator is ignored.
        bank_name: which bank sent it, used to pick the narration pattern for UTR
            extraction (see LegacyBankParser.extract_utr).

    Returns:
        One BankStatementLine per :61: line, in file order. An empty list if the
        statement legitimately contains no transactions (a nil statement, which
        banks do send) -- that is a fact, not an error.

    Raises:
        MT940ParseError: if a :61: line does not match the field-61 grammar, an
            amount or date inside one is invalid, or a :86: narration appears with
            no transaction to attach it to. Deliberately fatal: a partially-parsed
            bank statement silently under-reports credits, which in a
            reconciliation system means real payouts look like they never arrived.
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    records: list[BankStatementLine] = []
    pending: Optional[re.Match[str]] = None
    pending_raw = ""
    narration_parts: list[str] = []

    def flush() -> None:
        """Turn the transaction currently being accumulated into a record."""
        if pending is None:
            return
        narration = " ".join(part.strip() for part in narration_parts if part.strip())
        mark = pending.group("mark")
        value_date = _mt940_date(pending.group("value_date"), pending_raw)
        raw_entry_date = pending.group("entry_date")
        records.append(
            BankStatementLine(
                value_date=value_date,
                entry_date=(
                    _mt940_entry_date(raw_entry_date, value_date, pending_raw)
                    if raw_entry_date
                    else None
                ),
                is_credit=mark.endswith("C"),
                is_reversal=mark.startswith("R"),
                amount=_mt940_amount(pending.group("amount"), pending_raw),
                transaction_type=pending.group("type_code"),
                reference=pending.group("reference").strip(),
                narration=narration,
                extracted_utr=LegacyBankParser.extract_utr(bank_name, narration or None),
            )
        )

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line or line == "-":
            continue

        tag_match = _TAG_PATTERN.match(line)
        if tag_match is None:
            # An unprefixed line continues the previous :86: narration. Outside a
            # narration it is not something to guess about.
            if pending is not None and narration_parts:
                narration_parts.append(line)
                continue
            raise MT940ParseError(
                f"line {line!r} has no MT940 tag and does not continue a :86: narration"
            )

        tag, value = tag_match.group(1), tag_match.group(2)

        if tag == "61":
            flush()
            pending = _MT940_61_PATTERN.match(value.strip())
            if pending is None:
                raise MT940ParseError(f"malformed MT940 :61: statement line: {line!r}")
            pending_raw = line
            narration_parts = []
        elif tag == "86":
            if pending is None:
                raise MT940ParseError(
                    f"MT940 :86: narration with no preceding :61: transaction: {line!r}"
                )
            narration_parts = [value]
        else:
            flush()
            pending = None
            pending_raw = ""
            narration_parts = []

    flush()
    return records

class LegacyBankParser:
    """
    Stateless namespace for narration-parsing static methods -- there is no
    per-bank state to hold, so instantiating this class would be misleading.
    """

    @staticmethod
    def extract_utr(bank_name: str, narration: Optional[str]) -> Optional[str]:
        """
        Extract a 16-22 character UTR from a raw bank statement narration.

        Args:
            bank_name: bank identifier, e.g. "HDFC", "ICICI", "AXIS".
                Case-insensitive; leading/trailing whitespace is stripped.
            narration: the raw, untouched narration string from the bank
                statement. May be None or empty for malformed rows.

        Returns:
            The extracted UTR, or None if no UTR-shaped token could be found or
            the narration contains several conflicting candidates -- callers must
            treat None as "route this row for human/AI review", not as an error.

        Exception vectors handled:
            narration is None/empty, bank_name is unrecognized, the bank-specific
            pattern doesn't match this particular narration, or the fallback finds
            more than one distinct candidate -- all fall through to None rather
            than raising, since a parsing miss is an expected, routine outcome for
            chaotic legacy data, not an exceptional one.
        """
        if not narration:
            return None

        bank_key = bank_name.strip().upper() if bank_name else ""
        pattern = _BANK_PATTERNS.get(bank_key)

        if pattern is not None:
            match = pattern.search(narration)
            if match and _UTR_SHAPE.match(match.group(1)):
                # The bank's own convention named this token as the reference, so
                # a second UTR-shaped token elsewhere in the narration (a
                # counterparty's own reference, say) does not make it ambiguous.
                return match.group(1)

        candidates = {
            token for token in _FALLBACK_PATTERN.findall(narration) if _UTR_SHAPE.match(token)
        }
        if len(candidates) == 1:
            return candidates.pop()

        # Zero candidates, or several that disagree. Neither can be turned into a
        # confident answer, and inventing one would attach a bank credit to a
        # settlement on the strength of token order.
        return None

    @staticmethod
    def parse_statement(content: str, bank_name: str) -> list[BankStatementLine]:
        """
        Parse a full MT940 statement. Thin alias for parse_mt940_statement, kept so
        callers that already hold a LegacyBankParser reference have both halves of
        the parsing API in one place.

        Args:
            content: raw MT940 statement text.
            bank_name: which bank sent it.

        Returns:
            One BankStatementLine per transaction, in file order.

        Raises:
            MT940ParseError: as parse_mt940_statement.
        """
        return parse_mt940_statement(content, bank_name)




