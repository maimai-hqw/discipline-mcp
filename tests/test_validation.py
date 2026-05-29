"""Schema coercion + cross-field validation rules."""
import pytest

from discipline_mcp import schema
from discipline_mcp.schema import ValidationError


# ---- coercion ----
def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        schema.coerce_value("bogus", 1)


def test_price_negative_rejected():
    with pytest.raises(ValidationError):
        schema.coerce_value("intrinsic_low", -1)


def test_pct_over_100_rejected():
    with pytest.raises(ValidationError):
        schema.coerce_value("target_position_pct", 150)


def test_status_enum():
    assert schema.coerce_value("status", "HOLD") == "HOLD"
    with pytest.raises(ValidationError):
        schema.coerce_value("status", "MAYBE")


def test_tranches_shape_and_positivity():
    ok = schema.coerce_value("add_tranches", [{"price": 11, "shares": 1000, "note": "x"}])
    assert ok[0]["price"] == 11.0
    with pytest.raises(ValidationError):
        schema.coerce_value("add_tranches", [{"price": 0, "shares": 100}])
    with pytest.raises(ValidationError):
        schema.coerce_value("add_tranches", [{"price": 11}])  # missing shares


def test_triggers_shape():
    ok = schema.coerce_value("hard_triggers", [{"condition": "c", "action": "a"}])
    assert ok[0]["condition"] == "c"
    with pytest.raises(ValidationError):
        schema.coerce_value("hard_triggers", [{"condition": "only"}])


# ---- cross-field HARD rules ----
def test_intrinsic_low_le_high():
    with pytest.raises(ValidationError):
        schema.validate_rule({"intrinsic_low": 20, "intrinsic_high": 10})


def test_margin_of_safety_add_zone_must_be_below_floor():
    # add_zone_high above intrinsic_low -> reject (Graham rule)
    with pytest.raises(ValidationError):
        schema.validate_rule({"intrinsic_low": 15, "intrinsic_high": 21, "add_zone_high": 16})
    # at/below floor -> ok
    schema.validate_rule({"intrinsic_low": 15, "intrinsic_high": 21, "add_zone_high": 12})


def test_clear_ge_trim():
    with pytest.raises(ValidationError):
        schema.validate_rule({"trim_zone_low": 25, "clear_line": 20})


def test_stop_lt_clear():
    with pytest.raises(ValidationError):
        schema.validate_rule({"stop_loss": 30, "clear_line": 28})


def test_tranche_monotonic():
    with pytest.raises(ValidationError):
        schema.validate_rule({"add_tranches": [{"price": 10, "shares": 1},
                                               {"price": 11, "shares": 1}]})  # should descend
    # descending add is fine
    schema.validate_rule({"add_tranches": [{"price": 11, "shares": 1},
                                           {"price": 10, "shares": 1}]})


# ---- price-relative ----
def test_stop_must_be_below_current_price():
    with pytest.raises(ValidationError):
        schema.validate_rule({"stop_loss": 30}, current_price=29)
    schema.validate_rule({"stop_loss": 30}, current_price=35)  # ok


def test_skip_price_checks_warns_when_no_price():
    w = schema.validate_rule({"stop_loss": 30})
    assert any("current_price" in x for x in w)


def test_soft_thin_margin_warning():
    w = schema.validate_rule({"intrinsic_low": 100, "add_zone_high": 95})  # 5% discount
    assert any("安全边际" in x for x in w)
