"""Rule field schema and write-time validation (stdlib only, no pydantic).

Validation is split into:
  * field-level coercion/typing  -> coerce_value()
  * cross-field hard rules       -> validate_rule()  (reject on violation)
  * price-relative + soft rules  -> validate_rule()  (warnings; reject only the
                                    current-price hard checks when price given)

Keeping validation as plain, explicit functions (vs a framework) makes every
rule auditable and unit-testable.
"""
from __future__ import annotations

import math
import re

STATUSES = {"HOLD", "BUILDING", "TRIMMING", "EXITING", "WATCH", "RETIRED"}
# status values that only the retire/reinstate lifecycle tools may set
LIFECYCLE_STATUSES = {"RETIRED"}
_SYMBOL_RE = re.compile(r"^(sh|sz|bj)\.\d{6}$")

# field -> ("price"|"pct"|"str"|"enum"|"json_tranches"|"json_triggers"
#           |"json_catalysts"|"json_metrics")
FIELD_TYPES = {
    "name": "str",
    "sector": "str",
    "status": "enum",
    "rationale": "str",
    "intrinsic_low": "price",
    "intrinsic_high": "price",
    "graham_number": "price",
    "add_zone_high": "price",
    "add_tranches": "json_tranches",
    "no_chase_above": "price",
    "trim_zone_low": "price",
    "trim_tranches": "json_tranches",
    "stop_loss": "price",
    "clear_line": "price",
    "hard_triggers": "json_triggers",
    "target_position_pct": "pct",
    "max_position_pct": "pct",
    # --- value-investing deep-dive informational fields (additive, no new hard rules) ---
    "stock_type": "enum",
    "moat": "str",
    "moat_rating": "enum",
    "normalized_eps": "price",
    "normalized_basis": "str",
    "earnings_quality": "str",
    "value_trap": "enum",
    "cheap_reason": "str",
    "dividend_yield": "pct",
    "dividend_sustainable": "enum",
    "catalysts": "json_catalysts",
    "tracking_metrics": "json_metrics",
    "confidence": "enum",
    "disagreement": "str",
    "evidence": "str",
    "vs_portfolio": "str",
}

# Per-enum-field allowed value-sets. `status` keeps its existing strictness
# (and its own None-rejection); the new enums allow None to CLEAR the field.
ENUM_VALUES = {
    "status": STATUSES,
    "stock_type": {"CYCLICAL", "GROWTH", "QUALITY", "VALUE", "VALUE_TRAP",
                   "SPECIAL_SITUATION", "DEFENSIVE"},
    "moat_rating": {"WIDE", "NARROW", "NONE"},
    "value_trap": {"YES", "NO", "WATCH"},
    "dividend_sustainable": {"YES", "NO", "RISK"},
    "confidence": {"LOW", "MED", "HIGH"},
}

class ValidationError(ValueError):
    """Raised when a write violates a hard rule."""


def known_field(field: str) -> bool:
    return field in FIELD_TYPES


def valid_symbol(symbol: str) -> str:
    """Canonicalize + validate a symbol. Raises ValidationError.

    Required form: '<sh|sz|bj>.<6 digits>' (lowercased). This also rules out the
    empty string (which means 'all' in get_rules) and any path-ish characters.
    """
    s = (symbol or "").strip().lower()
    if not _SYMBOL_RE.match(s):
        raise ValidationError(f"非法 symbol:{symbol!r},需形如 'sh.600519'/'sz.002049'/'bj.xxxxxx'")
    return s


def _num(field, value, *, allow_negative=False):
    """Coerce to a finite float; reject bool, NaN, Inf, (optionally) negatives."""
    if isinstance(value, bool):
        raise ValidationError(f"{field} 不能是布尔值")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是数字,得到 {value!r}")
    if not math.isfinite(v):
        raise ValidationError(f"{field} 必须是有限数值(不能是 NaN/Inf)")
    if not allow_negative and v < 0:
        raise ValidationError(f"{field} 不能为负:{v}")
    return v


def valid_current_price(value):
    """None passes through; otherwise must be a finite price > 0."""
    if value is None:
        return None
    v = _num("current_price", value)
    if v <= 0:
        raise ValidationError(f"current_price 必须 >0:{v}")
    return v


def coerce_value(field: str, value):
    """Type-coerce/validate a single field value. Raises ValidationError.

    ``None`` is allowed for optional price fields (e.g. clearing stop_loss).
    """
    t = FIELD_TYPES.get(field)
    if t is None:
        raise ValidationError(f"未知字段:{field}(允许字段见 schema.FIELD_TYPES)")

    if t in ("price", "pct") and value is not None:
        value = _num(field, value)
        if t == "pct" and value > 100:
            raise ValidationError(f"{field} 是百分比,不能 >100:{value}")

    elif t == "enum":
        # Fail CLOSED: an enum field with no ENUM_VALUES entry is a config bug, not
        # a license to fall back to STATUSES (which would let status-like values
        # persist under the wrong field). Refuse rather than guess.
        allowed = ENUM_VALUES.get(field)
        if allowed is None:
            raise ValidationError(f"{field} 是 enum 但缺少 ENUM_VALUES 配置(内部错误)")
        # status must stay a valid lifecycle value (None still raises); the new
        # optional enums allow None to CLEAR the field.
        if value is None and field != "status":
            return None
        if value not in allowed:
            raise ValidationError(f"{field} 非法:{value!r},允许 {sorted(allowed)}")

    elif t == "str":
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"{field} 必须是字符串")

    elif t == "json_tranches":
        value = _coerce_tranches(field, value)

    elif t == "json_triggers":
        value = _coerce_triggers(field, value)

    elif t == "json_catalysts":
        value = _coerce_catalysts(field, value)

    elif t == "json_metrics":
        value = _coerce_tracking_metrics(field, value)

    return value


def _coerce_tranches(field, value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(f"{field} 必须是数组 [{{price, shares, note}}]")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or "price" not in item or "shares" not in item:
            raise ValidationError(f"{field}[{i}] 需含 price 和 shares")
        price = _num(f"{field}[{i}].price", item["price"])
        shares = _num(f"{field}[{i}].shares", item["shares"])
        if price <= 0 or shares <= 0:
            raise ValidationError(f"{field}[{i}] 的 price/shares 必须 >0")
        out.append({"price": price, "shares": shares, "note": item.get("note", "")})
    return out


def _coerce_triggers(field, value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(f"{field} 必须是数组 [{{condition, action}}]")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or "condition" not in item or "action" not in item:
            raise ValidationError(f"{field}[{i}] 需含 condition 和 action")
        out.append({"condition": str(item["condition"]), "action": str(item["action"]),
                    "note": item.get("note", "")})
    return out


def _coerce_catalysts(field, value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(f"{field} 必须是数组 [{{event, date, note}}]")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError(f"{field}[{i}] 必须是对象 {{event, date?, note?}}")
        event = str(item.get("event", "")).strip()
        if not event:
            raise ValidationError(f"{field}[{i}] 需含非空 event")
        out.append({"event": event,
                    "date": str(item.get("date", "")),
                    "note": str(item.get("note", ""))})
    return out


def _coerce_tracking_metrics(field, value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(f"{field} 必须是数组 [{{metric, threshold, note}}]")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or "metric" not in item or "threshold" not in item:
            raise ValidationError(f"{field}[{i}] 需含 metric 和 threshold")
        metric = str(item["metric"]).strip()
        if not metric:
            raise ValidationError(f"{field}[{i}] 需含 metric 和 threshold")
        out.append({"metric": metric,
                    "threshold": str(item["threshold"]),
                    "note": str(item.get("note", ""))})
    return out


def _g(rule, field):
    """Get a finite numeric field or None (bool excluded)."""
    v = rule.get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def validate_rule(rule: dict, current_price=None) -> list[str]:
    """Validate a (post-change) rule dict. Returns warnings list.

    Raises ValidationError on any HARD rule violation. Price-relative HARD checks
    only run when ``current_price`` is supplied; otherwise a warning notes they
    were skipped.
    """
    warnings: list[str] = []
    lo, hi = _g(rule, "intrinsic_low"), _g(rule, "intrinsic_high")
    add_hi = _g(rule, "add_zone_high")
    trim_lo = _g(rule, "trim_zone_low")
    clear = _g(rule, "clear_line")
    stop = _g(rule, "stop_loss")

    # --- HARD: structural ---
    if lo is not None and hi is not None and lo > hi:
        raise ValidationError(f"intrinsic_low({lo}) 不能 > intrinsic_high({hi})")

    # HARD: Graham margin-of-safety — buy zone must sit at/below intrinsic floor
    if add_hi is not None and lo is not None and add_hi > lo:
        raise ValidationError(
            f"add_zone_high({add_hi}) 不能高于 intrinsic_low({lo}):加仓必须有安全边际"
        )

    if clear is not None and trim_lo is not None and clear < trim_lo:
        raise ValidationError(f"clear_line({clear}) 应 ≥ trim_zone_low({trim_lo})")

    if stop is not None and clear is not None and stop >= clear:
        raise ValidationError(f"stop_loss({stop}) 应 < clear_line({clear})")

    # tranche monotonicity
    _check_tranche_monotonic(rule.get("add_tranches"), "add_tranches", ascending=False)
    _check_tranche_monotonic(rule.get("trim_tranches"), "trim_tranches", ascending=True)

    tp, mp = _g(rule, "target_position_pct"), _g(rule, "max_position_pct")
    if tp is not None and mp is not None and mp < tp:
        raise ValidationError(f"max_position_pct({mp}) 不能 < target_position_pct({tp})")

    # --- price-relative checks ---
    if current_price is not None:
        cp = float(current_price)
        if stop is not None and stop >= cp:
            raise ValidationError(f"stop_loss({stop}) 应 < 现价({cp})")
        if clear is not None and clear <= cp:
            warnings.append(f"clear_line({clear}) ≤ 现价({cp}):清仓线已被触及")
        if add_hi is not None and add_hi > cp:
            warnings.append(f"add_zone_high({add_hi}) > 现价({cp}):等于现价即可加,注意是否追高")
    else:
        if stop is not None or add_hi is not None:
            warnings.append("未提供 current_price,跳过止损/追高的现价校验")

    # --- SOFT ---
    if trim_lo is not None and hi is not None and trim_lo < hi:
        warnings.append(f"trim_zone_low({trim_lo}) < intrinsic_high({hi}):减仓区低于价值上沿(可能偏早,如防御特殊情况除外)")
    if add_hi is not None and lo is not None and lo > 0:
        disc = (lo - add_hi) / lo
        if disc < 0.15:
            warnings.append(f"加仓折扣仅 {disc:.0%}(<15%),安全边际偏薄")

    return warnings


def _check_tranche_monotonic(tranches, field, ascending):
    if not tranches:
        return
    prices = [t["price"] for t in tranches]
    ok = all(prices[i] < prices[i+1] for i in range(len(prices)-1)) if ascending \
        else all(prices[i] > prices[i+1] for i in range(len(prices)-1))
    if not ok:
        order = "递增" if ascending else "递减"
        raise ValidationError(f"{field} 价格必须严格{order}:{prices}")
