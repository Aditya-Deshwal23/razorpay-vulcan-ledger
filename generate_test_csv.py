"""Build a mixed settlement CSV: ~60% exact matches, remainder LangGraph exceptions."""
from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

TWO = Decimal("0.01")


def money(value: Decimal) -> str:
    return str(value.quantize(TWO, rounding=ROUND_HALF_UP))


def generate_varied_csv(path: Path, matches: int = 6, exceptions: int = 4) -> None:
    rows: list[dict[str, str]] = []
    index = 0

    def components(gross: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        fee = (gross * Decimal("0.02")).quantize(TWO, rounding=ROUND_HALF_UP)
        tax = (fee * Decimal("0.18")).quantize(TWO, rounding=ROUND_HALF_UP)
        net = (gross - fee - tax).quantize(TWO, rounding=ROUND_HALF_UP)
        return fee, tax, net

    for _ in range(matches):
        index += 1
        gross = Decimal("2500.00") + Decimal(index) * Decimal("10.00")
        fee, tax, net = components(gross)
        rows.append(
            {
                "settlement_id": f"setl_match_{index:03d}",
                "status": "processed",
                "gross_amount": money(gross),
                "gateway_fee": money(fee),
                "tax_deducted": money(tax),
                "refund_amount": "0.00",
                "cross_settlement_adj": "0.00",
                "net_settlement": money(net),
                "bank_credit": money(net),
                "bank_utr": f"HDFC{index:012d}",
                "utr_reference": f"HDFC{index:012d}",
                "bank_name": "HDFC",
                "narration": f"NEFT CR HDFC{index:012d} RAZORPAY SETTLEMENT",
                "settlement_date": "2026-08-15",
            }
        )

    for offset in range(exceptions):
        index += 1
        gross = Decimal("4100.00") + Decimal(offset) * Decimal("25.00")
        fee, tax, net = components(gross)
        missing_utr = offset >= exceptions // 2
        bank_credit = net if missing_utr else (net - Decimal("150.00"))
        rows.append(
            {
                "settlement_id": f"setl_excp_{offset + 1:03d}",
                "status": "processed",
                "gross_amount": money(gross),
                "gateway_fee": money(fee),
                "tax_deducted": money(tax),
                "refund_amount": "0.00",
                "cross_settlement_adj": "0.00",
                "net_settlement": money(net),
                "bank_credit": money(bank_credit),
                "bank_utr": "" if missing_utr else f"ICIC{index:012d}",
                "utr_reference": f"ICIC{index:012d}",
                "bank_name": "ICICI",
                "narration": (
                    "IMPS CR UNPARSEABLE NARRATION RAZORPAY"
                    if missing_utr
                    else f"RTGS CR ICIC{index:012d} SHORT CREDIT"
                ),
                "settlement_date": "2026-08-16",
            }
        )

    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "test_batch_varied.csv"
    generate_varied_csv(target)
    print(f"{target.name} created.")
