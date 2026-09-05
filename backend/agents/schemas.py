"""
Pydantic v2 schemas for the sandboxed LangGraph reasoning node's structured
output.

These are the ONLY shapes of data the LLM is allowed to produce. The LLM
never sees a database row and never emits SQL -- it receives a sanitized
string (built in backend/agents/graph.py) and must return exactly one
ExceptionClassification, enforced by langchain_google_genai's structured
output (translated from this class into the model's function-calling
schema).

Currency handling:
    detected variance is intentionally typed as a plain string on the wire
    (variance_str) -- JSON/function-calling schemas have no native Decimal
    type, and asking the model for a "number" risks the underlying API
    representing it as a float before it ever reaches our code. Asking for
    a string instead, then parsing it ourselves via Decimal(variance_str),
    keeps the float boundary entirely outside our process. See
    ExceptionClassification.variance for the parsed value.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.money import money_to_str, parse_money


class ExceptionClassification(BaseModel):
    """
    Structured verdict the LLM must produce for one unresolved
    reconciliation exception.

    Attributes:
        discrepancy_reason: the LLM's best classification of *why* the
            deterministic rules engine (backend/core/rules_engine.py)
            couldn't match this settlement.
        variance_str: the monetary variance as a plain decimal string, e.g.
            "500.00" or "-12.30" -- never a bare JSON number. Validated on
            construction; see the field_validator below.
        suggested_action: whether this can be auto-approved or must go to
            a human reviewer.
        confidence_score: the model's self-reported confidence, 0.0-1.0.
            NOT a currency value -- a probability, so float is the correct
            type here, unlike every monetary field elsewhere in this
            system.
    """

    discrepancy_reason: Literal[
        "TEMPORAL_CROSS_SETTLEMENT",
        "INCORRECT_FEE_LOGGING",
        "DELAYED_WEBHOOK_DELIVERY",
        "UNKNOWN_UNRESOLVABLE",
        "AI_UNAVAILABLE",
    ] = Field(description="Best-fit category for why the deterministic engine could not match this settlement.")

    variance_str: str = Field(
        description=(
            "The monetary variance as a plain decimal string with exactly two "
            "decimal places, e.g. '500.00' or '-12.30'. Never scientific "
            "notation, never a bare number, never a currency symbol."
        )
    )

    suggested_action: Literal["AUTO_APPROVE_ADJUSTMENT", "ROUTE_TO_HITL_PANEL"] = Field(
        description=(
            "AUTO_APPROVE_ADJUSTMENT only when confidence_score is high AND the "
            "reason is routine; otherwise ROUTE_TO_HITL_PANEL."
        )
    )

    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Self-reported confidence, 0.0-1.0. Not a currency value."
    )

    @field_validator("variance_str")
    @classmethod
    def _variance_str_must_be_decimal_parseable(cls, value: str) -> str:
        """
        Reject a variance_str that isn't actually a clean monetary string, and
        normalize the ones that are, at construction time -- before this object
        can be treated as a valid classification anywhere downstream.

        Validation is delegated to core.money.parse_money, the same function the
        bank parsers and the ORM layer use, so an LLM-supplied amount is held to
        exactly the standard every other amount in the system is held to. A
        hand-rolled `Decimal(value)` check is not equivalent and was the defect
        here: Decimal happily accepts "NaN", "Infinity", and "5e2", so all three
        used to pass validation. NaN in particular is the dangerous one -- it
        compares False against everything, including itself, so a variance
        cross-check would quietly conclude "within tolerance".

        Returns:
            The amount in canonical two-decimal form (e.g. "500.0" -> "500.00",
            "1,234.56" -> "1234.56"), so variance_str and the .variance property
            can never disagree and the persisted string matches what PostgreSQL
            renders for the same NUMERIC(15,2).

        Raises:
            ValueError: if value is not a plain decimal amount -- "approximately
                500", "$500.00", "5e2", "NaN", "Infinity", more than two decimal
                places, or beyond DECIMAL(15,2) range. Pydantic wraps this into a
                ValidationError, which is exactly the malformed-response case
                backend/agents/graph.py's self-correction block is built to catch
                and quote back to the model.
        """
        return money_to_str(parse_money(value, "variance_str"), "variance_str")

    @property
    def variance(self) -> Decimal:
        """
        The variance as a real Decimal, safely parsed from variance_str.
        Guaranteed to succeed -- the field_validator above already proved
        variance_str is a finite, in-range, two-decimal amount before this object
        could exist.
        """
        return Decimal(self.variance_str)


class BatchExceptionClassifications(BaseModel):
    """Ordered classifications returned for one bounded batch prompt."""

    classifications: list[ExceptionClassification]


class IngestionSanitizer(BaseModel):
    """
    Strict validation and sanitization for untrusted CSV/Bank inputs.

    Prevents indirect prompt injections from altering LLM behavior by stripping
    adversarial instruction patterns, limiting character sets to safe financial
    characters, and capping the maximum string length. Any text extracted from
    SWIFT MT940 narrations, CSV remarks, or bank references must pass through
    this sanitizer before being composed into an LLM prompt string.
    """

    raw_text: str

    @field_validator("raw_text")
    @classmethod
    def sanitize_input(cls, v: str) -> str:
        """Strip adversarial instruction patterns and limit character sets."""
        suspicious_patterns = (
            r"(?i)(ignore previous|system prompt|instruction|bypass|override|forget|you are a)"
        )
        sanitized = re.sub(suspicious_patterns, "[REDACTED]", v)
        # Restrict to safe financial/alphanumeric characters only.
        sanitized = re.sub(r"[^\w\s\-\/\:\.,#@]", "", sanitized)
        return sanitized[:250].strip()
