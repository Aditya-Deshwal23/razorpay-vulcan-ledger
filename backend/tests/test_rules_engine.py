"""Tests for backend.core.rules_engine.DeterministicRulesEngine."""
from decimal import Decimal

import pytest

from core.rules_engine import DeterministicRulesEngine, ReconciliationResult


def _d(x) -> Decimal:
    """Test-only helper: build a Decimal the same way production code must."""
    return Decimal(str(x))


def test_perfect_match():
    result = DeterministicRulesEngine.evaluate_match(
        gross=_d(1000.00), fees=_d(20.00), taxes=_d(3.60),
        refunds=_d(0.00), adjustments=_d(0.00), bank_credit=_d(976.40),
    )
    assert isinstance(result, ReconciliationResult)
    assert result.status == "MATCHED"
    assert result.is_resolved is True
    assert result.variance == Decimal("0.00")
    assert result.expected_net == Decimal("976.40")


def test_variance_exactly_at_tolerance_boundary_is_matched():
    result = DeterministicRulesEngine.evaluate_match(
        gross=_d(1000.00), fees=_d(20.00), taxes=_d(3.60),
        refunds=_d(0.00), adjustments=_d(0.00), bank_credit=_d(976.38),
    )
    assert result.variance == Decimal("0.02")
    assert result.status == "MATCHED"


def test_variance_just_outside_tolerance_is_unresolved():
    result = DeterministicRulesEngine.evaluate_match(
        gross=_d(1000.00), fees=_d(20.00), taxes=_d(3.60),
        refunds=_d(0.00), adjustments=_d(0.00), bank_credit=_d(976.37),
    )
    assert result.variance == Decimal("0.03")
    assert result.status == "UNRESOLVED_DISCREPANCY"
    assert result.is_resolved is False


def test_cross_settlement_adjustment_scenario_matches():
    # doc2's "previous cycle chargeback" scenario: a 500 INR adjustment
    # correctly brings expected_net down to match a reduced bank credit.
    result = DeterministicRulesEngine.evaluate_match(
        gross=_d(1000.00), fees=_d(20.00), taxes=_d(3.60),
        refunds=_d(0.00), adjustments=_d(500.00), bank_credit=_d(476.40),
    )
    assert result.status == "MATCHED"
    assert result.expected_net == Decimal("476.40")


def test_unresolvable_anomaly_scenario_is_unresolved():
    result = DeterministicRulesEngine.evaluate_match(
        gross=_d(1000.00), fees=_d(20.00), taxes=_d(3.60),
        refunds=_d(0.00), adjustments=_d(0.00), bank_credit=_d(100.00),
    )
    assert result.status == "UNRESOLVED_DISCREPANCY"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(gross=1000.0, fees=_d(20), taxes=_d(0), refunds=_d(0), adjustments=_d(0), bank_credit=_d(980)),
        dict(gross=_d(1000), fees=20.0, taxes=_d(0), refunds=_d(0), adjustments=_d(0), bank_credit=_d(980)),
        dict(gross=_d(1000), fees=_d(20), taxes=3.6, refunds=_d(0), adjustments=_d(0), bank_credit=_d(976.4)),
        dict(gross=_d(1000), fees=_d(20), taxes=_d(0), refunds=0.0, adjustments=_d(0), bank_credit=_d(980)),
        dict(gross=_d(1000), fees=_d(20), taxes=_d(0), refunds=_d(0), adjustments=0.0, bank_credit=_d(980)),
        dict(gross=_d(1000), fees=_d(20), taxes=_d(0), refunds=_d(0), adjustments=_d(0), bank_credit=980.0),
    ],
)
def test_a_float_in_any_position_raises_typeerror(kwargs):
    with pytest.raises(TypeError, match="float"):
        DeterministicRulesEngine.evaluate_match(**kwargs)


def test_a_bare_int_also_raises_typeerror():
    # Strict on purpose: even an int that "looks safe" must be an explicit
    # Decimal, so there's exactly one accepted way to construct these values.
    with pytest.raises(TypeError):
        DeterministicRulesEngine.evaluate_match(
            gross=1000, fees=_d(20), taxes=_d(0), refunds=_d(0), adjustments=_d(0), bank_credit=_d(980)
        )