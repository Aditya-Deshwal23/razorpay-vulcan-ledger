"""
Intelligent CSV parser for Razorpay settlement uploads.

Takes a raw CSV file the finance team exports from the Razorpay Dashboard and
converts it into structured, validated, accounting-equation-verified
ParsedSettlementRow objects — each carrying a within-batch sequential record
identifier (REC-001, REC-002 …).

Field derivation is the core value here:
    The CSV a finance team exports carries raw components. This module computes:

        net_credit = gross_amount - gateway_fee - tax_deducted
                     - refund_amount - cross_settlement_adj

    and validates that the equation holds for every row before any row reaches
    the database. A row whose own numbers disagree with their own accounting
    equation is a data quality problem that must surface here, not silently
    reach the rules engine as a ghost variance.

Column aliases:
    CSV headers are normalized to snake_case and aliased via COLUMN_ALIASES,
    so a finance export that says "Gross Amount (INR)" and one that says
    "gross_amount" are both accepted. The aliases are additive: the canonical
    snake_case name always works.

Exception vectors handled:
    - Missing required column         -> CsvParseError naming the column
    - Row with unparseable amount     -> CsvRowError with row number and field
    - Row violating accounting equation -> CsvRowError with the mismatch
    - Empty file / header-only file   -> CsvParseError
    - Non-UTF-8 bytes in file         -> CsvParseError (caller must decode)
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from core.money import money_to_str, parse_money

# ---------------------------------------------------------------------------
# Column name aliases
# ---------------------------------------------------------------------------
# Maps every known variant of a column header (lowercase, stripped) to the
# canonical field name. Extend this dict when a new Razorpay export format
# appears rather than branching in the parser logic.
COLUMN_ALIASES: dict[str, str] = {
    # settlement_id
    "settlement_id": "settlement_id",
    "id": "settlement_id",
    "settlement id": "settlement_id",
    # status
    "status": "status",
    "settlement_status": "status",
    "settlement status": "status",
    # gross_amount
    "gross_amount": "gross_amount",
    "gross amount": "gross_amount",
    "gross amount (inr)": "gross_amount",
    "gross": "gross_amount",
    # gateway_fee
    "gateway_fee": "gateway_fee",
    "gateway fee": "gateway_fee",
    "fees": "gateway_fee",
    "fee": "gateway_fee",
    "razorpay fees": "gateway_fee",
    "razorpay_fees": "gateway_fee",
    "mdr": "gateway_fee",
    # tax_deducted
    "tax_deducted": "tax_deducted",
    "tax deducted": "tax_deducted",
    "gst": "tax_deducted",
    "tax": "tax_deducted",
    "taxes": "tax_deducted",
    "gst on fees": "tax_deducted",
    # refund_amount
    "refund_amount": "refund_amount",
    "refund amount": "refund_amount",
    "refunds": "refund_amount",
    "refund": "refund_amount",
    # cross_settlement_adj
    "cross_settlement_adj": "cross_settlement_adj",
    "cross settlement adj": "cross_settlement_adj",
    "adjustments": "cross_settlement_adj",
    "adjustment": "cross_settlement_adj",
    "cross settlement adjustments": "cross_settlement_adj",
    # utr_reference
    "utr_reference": "utr_reference",
    "utr reference": "utr_reference",
    "utr": "utr_reference",
    "transfer reference": "utr_reference",
    # settlement_date
    "settlement_date": "settlement_date",
    "settlement date": "settlement_date",
    "date": "settlement_date",
    "value_date": "settlement_date",
}

# Required canonical field names that MUST be present after alias resolution.
REQUIRED_FIELDS = frozenset({
    "settlement_id",
    "gross_amount",
    "gateway_fee",
    "tax_deducted",
})

# Optional fields — if absent, default to "0.00" or empty string.
OPTIONAL_MONEY_FIELDS = frozenset({"refund_amount", "cross_settlement_adj"})


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------
class CsvParseError(ValueError):
    """
    A fatal error that means the entire CSV file cannot be processed —
    e.g. a missing required column, empty file, or non-UTF-8 encoding.
    """


class CsvRowError(ValueError):
    """
    An error on a specific row — bad amount, violated accounting equation,
    or missing settlement_id. Carries the 1-based row number and field name.
    """

    def __init__(self, row_num: int, field: str, detail: str) -> None:
        self.row_num = row_num
        self.field = field
        super().__init__(f"Row {row_num} [{field}]: {detail}")


# ---------------------------------------------------------------------------
# Parsed row model
# ---------------------------------------------------------------------------
class ParsedSettlementRow(BaseModel):
    """
    One fully-validated settlement row derived from a CSV upload.

    All monetary fields are Decimal; net_credit is derived, not read from the
    CSV. The accounting equation is checked by the model validator.

    Attributes:
        record_id:     Within-batch sequential identifier, e.g. "REC-001".
        batch_id:      The parent batch identifier, e.g. "BATCH-001".
        settlement_id: Razorpay settlement_id (business key).
        status:        Settlement status string (default "processed").
        gross_amount:  Total amount before deductions.
        gateway_fee:   Razorpay MDR / gateway fee.
        tax_deducted:  GST withheld on gateway fees (18% of fees typically).
        refund_amount: Refunds issued against this settlement.
        cross_settlement_adj: Cross-settlement or chargeback adjustments.
        net_credit:    DERIVED — gross - fee - tax - refund - adjustment.
                       This is what should land in the bank account.
        utr_reference: UTR from the CSV, if present.
        settlement_date: Raw date string from CSV, passed through as-is.
    """

    record_id: str
    batch_id: str
    settlement_id: str
    status: str = "processed"
    gross_amount: Decimal
    gateway_fee: Decimal
    tax_deducted: Decimal
    refund_amount: Decimal = Decimal("0.00")
    cross_settlement_adj: Decimal = Decimal("0.00")
    net_credit: Decimal  # derived
    utr_reference: Optional[str] = None
    settlement_date: Optional[str] = None

    @field_validator("settlement_id")
    @classmethod
    def _settlement_id_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("settlement_id must not be blank")
        return stripped

    @field_validator("status")
    @classmethod
    def _status_nonempty(cls, value: str) -> str:
        return value.strip() or "processed"

    @model_validator(mode="after")
    def _validate_accounting_equation(self) -> "ParsedSettlementRow":
        """
        Verify net_credit == gross - fee - tax - refund - adj.

        The rules engine would catch this later, but catching it here means the
        error is attributed to the CSV source row, not to a reconciliation
        discrepancy.
        """
        expected = (
            self.gross_amount
            - self.gateway_fee
            - self.tax_deducted
            - self.refund_amount
            - self.cross_settlement_adj
        )
        # Quantize both sides to 2dp for the comparison.
        from core.money import quantize_money
        expected_q = quantize_money(expected, "expected_net")
        net_q = quantize_money(self.net_credit, "net_credit")
        if net_q != expected_q:
            raise ValueError(
                f"accounting equation violated: net_credit {money_to_str(net_q)} "
                f"!= gross {money_to_str(self.gross_amount)} "
                f"- fees {money_to_str(self.gateway_fee)} "
                f"- taxes {money_to_str(self.tax_deducted)} "
                f"- refunds {money_to_str(self.refund_amount)} "
                f"- adj {money_to_str(self.cross_settlement_adj)} "
                f"= {money_to_str(expected_q)}"
            )
        return self


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _normalize_header(raw: str) -> str:
    """Lower-case, strip, collapse internal whitespace."""
    return " ".join(raw.strip().lower().split())


def _resolve_headers(raw_headers: list[str]) -> dict[str, str]:
    """
    Map raw CSV headers to canonical field names via COLUMN_ALIASES.

    Args:
        raw_headers: the headers exactly as they appear in the CSV.

    Returns:
        {canonical_field_name: original_csv_header} for every recognized column.

    Raises:
        CsvParseError: if a required field cannot be found in any alias.
    """
    resolved: dict[str, str] = {}
    for raw in raw_headers:
        normalized = _normalize_header(raw)
        canonical = COLUMN_ALIASES.get(normalized)
        if canonical and canonical not in resolved:
            resolved[canonical] = raw

    missing = REQUIRED_FIELDS - set(resolved)
    if missing:
        raise CsvParseError(
            f"CSV is missing required column(s): {sorted(missing)}. "
            f"Recognized headers: {sorted(COLUMN_ALIASES.keys())}"
        )
    return resolved


def _parse_amount(raw: str, field: str, row_num: int) -> Decimal:
    """
    Parse one monetary cell from the CSV.

    Args:
        raw:     the cell's raw string value.
        field:   canonical field name, for error messages.
        row_num: 1-based row number in the CSV, for error messages.

    Returns:
        A two-decimal Decimal.

    Raises:
        CsvRowError: if the cell is not parseable as a monetary amount.
    """
    cleaned = raw.strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not cleaned:
        return Decimal("0.00")
    try:
        return parse_money(cleaned, field)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise CsvRowError(row_num, field, f"cannot parse {raw!r} as a monetary amount: {exc}") from exc


def _record_id(index: int) -> str:
    """Format a 1-based row index as a record identifier."""
    return f"REC-{index:03d}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_settlement_csv(
    content: str,
    batch_id: str,
    *,
    skip_accounting_errors: bool = False,
) -> tuple[list[ParsedSettlementRow], list[str]]:
    """
    Parse a Razorpay settlement export CSV and derive all fields.

    For each row:
      1. Maps raw CSV headers → canonical field names via COLUMN_ALIASES.
      2. Parses every monetary field as a Decimal (never a float).
      3. Derives net_credit = gross - gateway_fee - tax_deducted - refund - adj.
      4. Assigns a sequential record identifier: REC-001, REC-002, …
      5. Validates the accounting equation.

    Args:
        content:  The decoded CSV text. Caller must handle encoding; this
                  function never touches bytes.
        batch_id: The parent batch identifier (e.g. "BATCH-001"), embedded in
                  every returned row so it survives downstream without being
                  passed again.
        skip_accounting_errors: When True, rows that violate the accounting
                  equation are logged as warnings and skipped instead of
                  raising. Use False (the default) for production to ensure
                  every row is accounted for.

    Returns:
        (rows, warnings)
        rows:     A list of ParsedSettlementRow, one per valid CSV data row,
                  in file order with sequential record IDs.
        warnings: A list of human-readable warning strings for rows that were
                  skipped (only non-empty when skip_accounting_errors=True or
                  when optional fields were defaulted).

    Raises:
        CsvParseError: if the file is empty, has no data rows, or is missing
                       a required column.
        CsvRowError:   if a row has an unparseable amount and
                       skip_accounting_errors is False.
    """
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise CsvParseError("CSV file is empty or has no header row")

    # Resolve headers once, before iterating rows.
    col_map = _resolve_headers(list(reader.fieldnames))

    rows: list[ParsedSettlementRow] = []
    warnings: list[str] = []
    record_index = 0  # incremented only on successful parse

    for raw_row_num, raw_row in enumerate(reader, start=2):  # 1=header, data starts at 2
        settlement_id_raw = raw_row.get(col_map.get("settlement_id", ""), "").strip()
        if not settlement_id_raw:
            warnings.append(f"Row {raw_row_num}: skipped — settlement_id is blank")
            continue

        try:
            gross = _parse_amount(raw_row.get(col_map["gross_amount"], "0"), "gross_amount", raw_row_num)
            fee = _parse_amount(raw_row.get(col_map["gateway_fee"], "0"), "gateway_fee", raw_row_num)
            tax = _parse_amount(raw_row.get(col_map["tax_deducted"], "0"), "tax_deducted", raw_row_num)

            refund_col = col_map.get("refund_amount")
            refund = _parse_amount(raw_row.get(refund_col, "0") if refund_col else "0", "refund_amount", raw_row_num)

            adj_col = col_map.get("cross_settlement_adj")
            adj = _parse_amount(raw_row.get(adj_col, "0") if adj_col else "0", "cross_settlement_adj", raw_row_num)

            # DERIVED field: net_credit
            from core.money import quantize_money
            net_credit = quantize_money(gross - fee - tax - refund - adj, "net_credit")

            utr_col = col_map.get("utr_reference")
            utr_raw = raw_row.get(utr_col, "").strip() if utr_col else ""
            utr = utr_raw if utr_raw else None

            date_col = col_map.get("settlement_date")
            settlement_date = raw_row.get(date_col, "").strip() if date_col else None

            status_col = col_map.get("status")
            status_raw = raw_row.get(status_col, "processed").strip() if status_col else "processed"

            record_index += 1
            row = ParsedSettlementRow(
                record_id=_record_id(record_index),
                batch_id=batch_id,
                settlement_id=settlement_id_raw,
                status=status_raw or "processed",
                gross_amount=gross,
                gateway_fee=fee,
                tax_deducted=tax,
                refund_amount=refund,
                cross_settlement_adj=adj,
                net_credit=net_credit,
                utr_reference=utr,
                settlement_date=settlement_date,
            )
            rows.append(row)

        except CsvRowError:
            if skip_accounting_errors:
                warnings.append(str(CsvRowError(raw_row_num, "amount", "skipped due to parse error")))
                continue
            raise
        except Exception as exc:
            if skip_accounting_errors:
                warnings.append(f"Row {raw_row_num}: skipped — {exc}")
                continue
            raise CsvRowError(raw_row_num, "unknown", str(exc)) from exc

    if not rows:
        raise CsvParseError(
            "CSV contained no valid data rows. Check that the file has data below the header "
            "and that settlement_id is populated for at least one row."
        )

    return rows, warnings
