"""Value-investing deep-dive informational fields (16 additive fields).

These fields are INFORMATIONAL only: no new cross-field hard rules, none locked
by default. Tests cover per-field coercion (enum / price / pct / str / json) plus
a server round-trip asserting persistence, an intact hash chain, and that none of
the new fields are auto-locked.
"""
import asyncio

import pytest

from discipline_mcp import schema, server, store
from discipline_mcp.schema import ValidationError


# --------------------------------------------------------------------- #
# enum fields (generalized ENUM_VALUES table)
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("field,good,bad", [
    ("stock_type", "QUALITY", "JUNK"),
    ("moat_rating", "WIDE", "HUGE"),
    ("value_trap", "WATCH", "MAYBE"),
    ("dividend_sustainable", "RISK", "PROBABLY"),
    ("confidence", "MED", "MEDIUM"),
])
def test_new_enum_valid_and_invalid(field, good, bad):
    assert schema.coerce_value(field, good) == good
    with pytest.raises(ValidationError):
        schema.coerce_value(field, bad)


@pytest.mark.parametrize("field", [
    "stock_type", "moat_rating", "value_trap", "dividend_sustainable", "confidence",
])
def test_new_enum_none_clears(field):
    # None clears an optional enum field
    assert schema.coerce_value(field, None) is None


def test_every_enum_field_has_value_set():
    # Fail-closed guard: every FIELD_TYPES enum field MUST have an ENUM_VALUES
    # entry, so coerce_value can never silently fall back to STATUSES for a
    # misconfigured field.
    enum_fields = {f for f, t in schema.FIELD_TYPES.items() if t == "enum"}
    missing = enum_fields - set(schema.ENUM_VALUES)
    assert not missing, f"enum 字段缺少 ENUM_VALUES 配置:{sorted(missing)}"


def test_new_enum_case_sensitive_exact_match():
    # consistent with the existing status enum: no auto-uppercasing
    with pytest.raises(ValidationError):
        schema.coerce_value("stock_type", "quality")
    with pytest.raises(ValidationError):
        schema.coerce_value("confidence", "high")


def test_status_enum_still_strict_regression():
    # regression: status keeps its existing strictness incl. None rejection
    assert schema.coerce_value("status", "HOLD") == "HOLD"
    with pytest.raises(ValidationError):
        schema.coerce_value("status", "MAYBE")
    with pytest.raises(ValidationError):
        schema.coerce_value("status", None)


def test_each_enum_validates_own_set_not_statuses():
    # a value valid for status must NOT leak into the new enums
    with pytest.raises(ValidationError):
        schema.coerce_value("stock_type", "HOLD")
    # and a new-enum value must NOT be accepted as status
    with pytest.raises(ValidationError):
        schema.coerce_value("status", "WIDE")


# --------------------------------------------------------------------- #
# numeric fields (price / pct) keep the NaN/Inf + bounds discipline
# --------------------------------------------------------------------- #
def test_normalized_eps_price_semantics():
    assert schema.coerce_value("normalized_eps", "1.25") == 1.25
    assert schema.coerce_value("normalized_eps", None) is None
    with pytest.raises(ValidationError):           # negative rejected by "price"
        schema.coerce_value("normalized_eps", -0.5)


def test_normalized_eps_rejects_nan_inf_regression():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            schema.coerce_value("normalized_eps", bad)


def test_dividend_yield_pct_bounds():
    assert schema.coerce_value("dividend_yield", 0) == 0.0
    assert schema.coerce_value("dividend_yield", 4.2) == 4.2
    with pytest.raises(ValidationError):           # >100 rejected by "pct"
        schema.coerce_value("dividend_yield", 150)
    with pytest.raises(ValidationError):           # NaN regression
        schema.coerce_value("dividend_yield", float("nan"))


# --------------------------------------------------------------------- #
# str fields
# --------------------------------------------------------------------- #
def test_str_fields_accept_string_reject_nonstring():
    assert schema.coerce_value("moat", "品牌+渠道+成本") == "品牌+渠道+成本"
    assert schema.coerce_value("moat", None) is None
    with pytest.raises(ValidationError):
        schema.coerce_value("moat", 123)
    with pytest.raises(ValidationError):
        schema.coerce_value("evidence", ["not", "a", "string"])


# --------------------------------------------------------------------- #
# catalysts (json_catalysts)
# --------------------------------------------------------------------- #
def test_catalysts_wellformed_and_defaults():
    out = schema.coerce_value("catalysts", [
        {"event": "抽蓄项目投产", "date": "2026Q4", "note": "现金流拐点"},
        {"event": "分红率提升"},  # date/note default to ""
    ])
    assert out[0] == {"event": "抽蓄项目投产", "date": "2026Q4", "note": "现金流拐点"}
    assert out[1] == {"event": "分红率提升", "date": "", "note": ""}
    assert schema.coerce_value("catalysts", None) is None


def test_catalysts_missing_event_raises():
    with pytest.raises(ValidationError):
        schema.coerce_value("catalysts", [{"date": "2026Q4"}])
    with pytest.raises(ValidationError):           # blank/whitespace event
        schema.coerce_value("catalysts", [{"event": "   "}])


def test_catalysts_non_list_and_bad_item_raise():
    with pytest.raises(ValidationError):
        schema.coerce_value("catalysts", {"event": "x"})  # not a list
    with pytest.raises(ValidationError):
        schema.coerce_value("catalysts", ["not a dict"])


# --------------------------------------------------------------------- #
# tracking_metrics (json_metrics)
# --------------------------------------------------------------------- #
def test_tracking_metrics_wellformed_preserves_threshold_str():
    out = schema.coerce_value("tracking_metrics", [
        {"metric": "股息率", "threshold": "≥1.2%", "note": "低于则复核"},
        {"metric": "ROE", "threshold": 12},  # coerced to str, note default ""
    ])
    assert out[0] == {"metric": "股息率", "threshold": "≥1.2%", "note": "低于则复核"}
    assert out[1] == {"metric": "ROE", "threshold": "12", "note": ""}
    assert schema.coerce_value("tracking_metrics", None) is None


def test_tracking_metrics_missing_keys_raise():
    with pytest.raises(ValidationError):           # missing threshold
        schema.coerce_value("tracking_metrics", [{"metric": "ROE"}])
    with pytest.raises(ValidationError):           # missing metric
        schema.coerce_value("tracking_metrics", [{"threshold": "≥1.2%"}])
    with pytest.raises(ValidationError):           # blank metric
        schema.coerce_value("tracking_metrics", [{"metric": " ", "threshold": "x"}])


def test_tracking_metrics_non_list_raises():
    with pytest.raises(ValidationError):
        schema.coerce_value("tracking_metrics", {"metric": "x", "threshold": "y"})


# --------------------------------------------------------------------- #
# no new hard rules: an informational-only candidate validates clean
# --------------------------------------------------------------------- #
def test_no_new_hard_rules_on_validate_rule():
    rule = {
        "stock_type": "QUALITY", "moat_rating": "WIDE", "value_trap": "NO",
        "normalized_eps": 1.0, "dividend_yield": 3.0, "confidence": "HIGH",
        "catalysts": [{"event": "x", "date": "", "note": ""}],
        "tracking_metrics": [{"metric": "m", "threshold": "t", "note": ""}],
    }
    # must not raise and must not invent new warnings about these fields
    warnings = schema.validate_rule(rule)
    assert all("stock_type" not in w and "moat" not in w for w in warnings)


# --------------------------------------------------------------------- #
# server round-trip: persist several new fields atomically, chain intact,
# none auto-locked
# --------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


def test_bulk_set_new_fields_roundtrip_chain_clean_no_autolock(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCIPLINE_MCP_DB", str(f := tmp_path / "rules.jsonl"))
    r = _run(server.set_rule_bulk("sh.600483", {
        "stock_type": "QUALITY",
        "moat": "风电+抽蓄+核电参股",
        "moat_rating": "NARROW",
        "normalized_eps": 1.2,
        "value_trap": "NO",
        "dividend_yield": 3.5,
        "dividend_sustainable": "YES",
        "catalysts": [{"event": "抽蓄投产", "date": "2026Q4"}],
        "tracking_metrics": [{"metric": "股息率", "threshold": "≥1.2%"}],
        "confidence": "MED",
        "evidence": "2026Q1 扣非 +12%",
        "vs_portfolio": "电力配置, 与福能互补",
    }, "价值深挖录入", confirm=True))
    assert "已原子写入" in r

    events = store.read_events(f)
    assert len(events) == 1 and events[0]["op"] == "BULK_SET"
    # chain verifies clean
    store.verify_chain(events)

    rule = store.replay(events)["sh.600483"]
    assert rule["stock_type"] == "QUALITY"
    assert rule["moat_rating"] == "NARROW"
    assert rule["normalized_eps"] == 1.2
    assert rule["dividend_yield"] == 3.5
    assert rule["catalysts"] == [{"event": "抽蓄投产", "date": "2026Q4", "note": ""}]
    assert rule["tracking_metrics"] == [
        {"metric": "股息率", "threshold": "≥1.2%", "note": ""}]
    assert rule["confidence"] == "MED"

    # NONE of the new fields are auto-locked (only the default sell defences are)
    locked = set(rule.get("locked_fields", []))
    assert locked == {"stop_loss", "clear_line"}
    new_fields = {
        "stock_type", "moat", "moat_rating", "normalized_eps", "normalized_basis",
        "earnings_quality", "value_trap", "cheap_reason", "dividend_yield",
        "dividend_sustainable", "catalysts", "tracking_metrics", "confidence",
        "disagreement", "evidence", "vs_portfolio",
    }
    assert not (locked & new_fields)


def test_set_rule_single_new_enum_field_via_server(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCIPLINE_MCP_DB", str(f := tmp_path / "rules.jsonl"))
    r = _run(server.set_rule("sh.600519", "stock_type", "QUALITY", "定性", confirm=True))
    assert "已写入" in r
    assert store.replay(store.read_events(f))["sh.600519"]["stock_type"] == "QUALITY"
    # an invalid enum value is rejected at the tool surface
    bad = _run(server.set_rule("sh.600519", "moat_rating", "HUGE", "错值", confirm=True))
    assert "校验未通过" in bad
