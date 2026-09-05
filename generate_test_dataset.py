"""Generate a deterministic 60-row reconciliation test dataset."""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

FIELDNAMES = [
    "transaction_id",
    "source",
    "amount",
    "status",
    "payment_ref",
    "customer_ref",
    "date",
]
TARGET = Path(__file__).resolve().parent / "test_batch_60_comprehensive.csv"


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _row(
    index: int,
    *,
    source: str,
    amount: Decimal,
    status: str = "captured",
    payment_ref: str | None = None,
) -> dict[str, str]:
    return {
        "transaction_id": f"TXN-{index:04d}",
        "source": source,
        "amount": _money(amount),
        "status": status,
        "payment_ref": payment_ref if payment_ref is not None else f"PAY-{index:04d}",
        "customer_ref": f"CUS-{((index - 1) % 20) + 1:03d}",
        "date": f"2026-08-{((index - 1) % 28) + 1:02d}",
    }


def generate_dataset(path: Path = TARGET) -> list[dict[str, str]]:
    """Write exactly 60 rows covering the controller's principal scenarios."""
    rows: list[dict[str, str]] = []

    # 36 exact matches: alternating Razorpay and Stripe sources, stable references.
    for index in range(1, 37):
        rows.append(
            _row(
                index,
                source="razorpay_exact" if index % 2 else "stripe_exact",
                amount=Decimal("1200.00") + Decimal(index * 175),
            )
        )

    # 9 monetary variances: amount represents the bank-side amount; source carries
    # the gateway-side comparison amount for a reviewer/test harness.
    variance_cases = (
        (Decimal("2425.00"), Decimal("2450.50")),
        (Decimal("7050.00"), Decimal("7100.00")),
        (Decimal("3890.00"), Decimal("3925.75")),
        (Decimal("5180.00"), Decimal("5200.00")),
        (Decimal("6475.25"), Decimal("6500.00")),
        (Decimal("2750.00"), Decimal("2775.50")),
        (Decimal("8110.00"), Decimal("8150.00")),
        (Decimal("9345.00"), Decimal("9400.00")),
        (Decimal("1560.00"), Decimal("1585.25")),
    )
    for offset, (bank_amount, gateway_amount) in enumerate(variance_cases, start=37):
        rows.append(
            _row(
                offset,
                source=f"razorpay_variance_gateway_{_money(gateway_amount)}",
                amount=bank_amount,
            )
        )

    # 6 missing references: blank payment_ref forces amount/customer association.
    for index in range(46, 52):
        rows.append(
            _row(
                index,
                source="stripe_missing_reference",
                amount=Decimal("2100.00") + Decimal((index - 46) * 225),
                payment_ref="",
            )
        )

    # 6 refund clashes: gateway says refunded although a captured bank settlement exists.
    for index in range(52, 58):
        rows.append(
            _row(
                index,
                source="razorpay_refund_clash_bank_captured",
                amount=Decimal("3300.00") + Decimal((index - 52) * 310),
                status="refunded",
            )
        )

    # 3 failed/anomalous transactions: explicit failed status for exception logging.
    for index in range(58, 61):
        rows.append(
            _row(
                index,
                source="stripe_failed_anomaly",
                amount=Decimal("875.00") + Decimal((index - 58) * 450),
                status="failed",
            )
        )

    if len(rows) != 60:
        raise AssertionError(f"Expected 60 rows, generated {len(rows)}")

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return rows


if __name__ == "__main__":
    generated = generate_dataset()
    print(f"Created {TARGET.name} with {len(generated)} rows.")
