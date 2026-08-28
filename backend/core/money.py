"""
Monetary primitives for Razorpay Vulcan Ledger.

Single home for every rule about how money is represented in this system, so
the rules engine, the matcher, the bank parsers, the ORM layer, the agent's
output validation, and the evaluation runner all agree byte-for-byte.

The rules, in order of importance:

1. Money is decimal.Decimal. A float is never accepted, not even one that
   looks exact like 1000.0 -- the fix belongs where the float was created.
2. Money is finite. Decimal("NaN") and Decimal("Infinity") are perfectly valid
   Decimals and would sail through a naive isinstance check, then poison every
   comparison downstream (NaN compares False to everything, including itself)
   and blow up on INSERT. They are rejected here.
3. Money has exactly two decimal places. More precision is not silently
   rounded away -- silent rounding of money is a defect, not a convenience --
   it is rejected with an error naming the offending value.
4. Money fits DECIMAL(15,2). Overflow is caught in Python with an actionable
   message instead of aborting a transaction halfway through with a database
   numeric-overflow error.
5. Money has one canonical string form (money_to_str), used for hashing and
   for anything that crosses a JSON boundary, so a hash computed in Python
   always equals the equivalent hash computed in SQL.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: The scale every monetary value is normalized to. Matches DECIMAL(15,2).
MONEY_SCALE = Decimal("0.01")

#: Largest magnitude DECIMAL(15,2) can hold: 13 integer digits + 2 decimals.
MAX_MONEY_MAGNITUDE = Decimal("9999999999999.99")

def ensure_decimal(value: object, name: str) -> Decimal:
    """
    Guard a currency input against float contamination and non-finite values.

    Args:
        value: the value to check.
        name: the parameter name, used only to make the error actionable.

    Returns:
        value, unchanged, if it is a finite Decimal.

    Raises:
        TypeError: if value is a float (even one that "looks exact", e.g.
            1000.0) or any other non-Decimal type. Deliberately a hard failure
            rather than a silent Decimal(str(value)) rescue.
        ValueError: if value is a Decimal but not finite -- NaN, sNaN, or
            +/-Infinity. These are real Decimals that would otherwise reach a
            comparison (where NaN is False against everything, so a variance
            check would quietly report "within tolerance") or an INSERT.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name}={value!r} was passed as a float. Construct it as "
            f"Decimal(str({value!r})) at the point of origin -- "
            "this system never accepts float for currency values."
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a decimal.Decimal, got {type(value).__name__}: {value!r}")
    if not value.is_finite():
        raise ValueError(
            f"{name}={value!r} is not a finite amount. NaN and Infinity are valid "
            "Decimals but never valid money -- reject them at the boundary."
        )
    return value


def quantize_money(value: object, name: str) -> Decimal:
    """
    Normalize a currency value to exactly two decimal places, refusing to round.

    Args:
        value: a finite Decimal amount.
        name: the parameter name, for error messages.

    Returns:
        The same amount with scale exactly 2, e.g. Decimal("976.4") ->
        Decimal("976.40"). Lossless by construction: anything that would
        actually need rounding is rejected first.

    Raises:
        TypeError: via ensure_decimal, if value is a float or not a Decimal.
        ValueError: if value is non-finite, carries more than two decimal
            places (e.g. "10.005" -- a third decimal in a currency amount is a
            data-quality bug upstream, not something to silently round), or
            exceeds what DECIMAL(15,2) can store.
    """
    amount = ensure_decimal(value, name)

    quantized = amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    if quantized != amount:
        raise ValueError(
            f"{name}={amount} has more than two decimal places. Currency in this "
            "system is exact to paise; round explicitly upstream if that is "
            "genuinely intended."
        )
    if abs(quantized) > MAX_MONEY_MAGNITUDE:
        raise ValueError(
            f"{name}={amount} exceeds DECIMAL(15,2) range "
            f"(max magnitude {MAX_MONEY_MAGNITUDE})."
        )
    return quantized


def parse_money(text: object, name: str) -> Decimal:
    """
    Parse a monetary amount out of an untrusted string.

    The only sanctioned way for money to enter this system from a bank
    statement line, a JSON payload, a webhook body, or an LLM's structured
    output. Never `Decimal(float(...))`, never `float(...)`.

    Args:
        text: the raw string, e.g. "976.40", "1,234.56", "-12.30". Commas
            (Indian bank statements use them freely) and surrounding
            whitespace are stripped. A leading "+" is accepted.
        name: the field name, for error messages.

    Returns:
        A finite Decimal normalized to exactly two decimal places.

    Raises:
        TypeError: if text is not a str -- including if it is a float, which is
            the exact mistake this function exists to prevent.
        ValueError: if the string is empty, is not parseable as a decimal
            number, is non-finite ("NaN", "Infinity" -- Decimal accepts both
            and they must not become money), is in scientific notation, or
            carries more than two decimal places.
    """
    if isinstance(text, float):
        raise TypeError(
            f"{name}={text!r} was passed as a float. Pass the original string "
            "instead -- converting through float has already lost precision."
        )
    if not isinstance(text, str):
        raise TypeError(f"{name} must be a str, got {type(text).__name__}: {text!r}")

    cleaned = text.strip().replace(",", "").replace(" ", "")
    if not cleaned:
        raise ValueError(f"{name} is empty -- no amount to parse")
    if "e" in cleaned.lower():
        raise ValueError(
            f"{name}={text!r} looks like scientific notation. Currency must be "
            "written in plain decimal form."
        )

    try:
        amount = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{name}={text!r} is not a valid decimal amount") from exc

    return quantize_money(amount, name)


def money_to_str(value: object, name: str = "amount") -> str:
    """
    The one canonical string form of a monetary value.

    Used for the bank-entry dedupe hash and the reconciliation state hash, both
    of which must be reproducible from SQL: PostgreSQL renders a NUMERIC(15,2)
    with exactly two decimals when cast to text, and this produces the same
    text for the same amount.

    Args:
        value: a finite Decimal amount.
        name: field name for error messages.

    Returns:
        e.g. "976.40", "0.00", "-12.30".

    Raises:
        TypeError / ValueError: as quantize_money.
    """
    return str(quantize_money(value, name))
