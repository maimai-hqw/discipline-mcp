"""discipline-mcp FastMCP server (stdio) — protected investment-discipline store.

Stores per-symbol trading discipline (intrinsic value, add/trim/stop/clear price
thresholds, tranches, fundamental hard-triggers, target position, status) as an
append-only hash-chained JSONL log; current rules are derived by REPLAY.

Security posture:
  * all WRITE tools default confirm=False -> dry-run preview, nothing written;
  * cross-field validation rejects unsafe writes (e.g. add zone above intrinsic
    floor) before they land;
  * the sell defences (stop_loss, clear_line) are locked once set — changing them
    needs an explicit unlock_rule first;
  * status RETIRED only via retire_symbol/reinstate_symbol (not raw set_rule),
    and retired symbols reject ordinary writes;
  * hash chain makes any after-the-fact tampering detectable on read.

It does NOT store prices/holdings — combine with `ashare` / `portfolio` at the
conversation layer. In stdio transport stdout is the protocol channel; all logs
go to stderr, never print to stdout.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from . import store, schema, web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("discipline-mcp")

mcp = FastMCP("discipline-mcp")
_lock = asyncio.Lock()  # serialize tool calls within this process


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def _load_state():
    """Read + verify + replay. Raises store.ChainError if tampered."""
    return store.replay(store.read_events())


def _blank_rule(symbol):
    return {"symbol": symbol, "locked_fields": list(store.DEFAULT_LOCKED), "status": "WATCH"}


def _change_blocked(rule, field):
    """A field is change-blocked if it's locked AND already holds a value.
    First-time set (value is None) is allowed so initial setup / migration work;
    moving an established defence requires unlock_rule."""
    return field in rule.get("locked_fields", []) and rule.get(field) is not None


def _fmt_rule(rule: dict) -> str:
    lines = []
    for k in schema.DISPLAY_ORDER:
        if k in rule and rule[k] not in (None, [], ""):
            lines.append(f"  {k}: {rule[k]}")
    return "\n".join(lines)


def _preview(action_desc, warnings, errors=None):
    lines = [f"🔎 预览(未写入):{action_desc}"]
    if errors:
        lines.append("❌ 校验未通过,无法写入:")
        lines += [f"   - {w}" for w in errors]
        return "\n".join(lines)
    if warnings:
        lines.append("⚠️ 警告(不阻止写入):")
        lines += [f"   - {w}" for w in warnings]
    lines.append("如无误,用 confirm=true 重新调用以写入。")
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# READ tools (always allowed)
# --------------------------------------------------------------------- #
@mcp.tool()
async def get_rules(symbol: str = "") -> str:
    """当前纪律(重放 + 验链)。symbol 留空 = 全部标的。
    返回每只的内在价值区间、加/减/止损/清仓阈值、硬触发、目标仓位、锁定字段、状态。
    默认只读入口,可随时调用。数据源:本地纪律账本(非行情/持仓)。"""
    async with _lock:
        try:
            state = _load_state()
        except store.ChainError as e:
            return f"❌ 纪律账本校验失败(可能被篡改):{e}"
    if not state:
        return "📋 纪律账本为空。用 set_rule / set_rule_bulk 录入。"
    if symbol:
        try:
            sym = schema.valid_symbol(symbol)
        except schema.ValidationError as e:
            return f"❌ {e}"
        rule = state.get(sym)
        if not rule:
            return f"未找到 {sym} 的纪律。"
        return f"📋 {sym} 纪律\n" + _fmt_rule(rule)
    out = [f"📋 纪律总览({len(state)} 只)"]
    for sym, rule in sorted(state.items()):
        out.append(f"\n— {sym} [{rule.get('status')}]")
        out.append(_fmt_rule(rule))
    return "\n".join(out)


@mcp.tool()
async def get_rule_field(symbol: str, field: str) -> str:
    """读单只标的的单个字段当前值。"""
    try:
        sym = schema.valid_symbol(symbol)
    except schema.ValidationError as e:
        return f"❌ {e}"
    async with _lock:
        rule = _load_state().get(sym)
    if not rule:
        return f"未找到 {sym}。"
    if field not in rule:
        return f"{sym} 未设置字段 {field}。"
    locked = " 🔒(已锁定)" if field in rule.get("locked_fields", []) else ""
    return f"{sym}.{field} = {rule[field]}{locked}"


@mcp.tool()
async def get_rule_history(symbol: str = "", field: str = "") -> str:
    """审计:按时间升序列出 event 流水(可按 symbol / field 过滤)。
    每条含 seq/时间/操作/字段/旧值→新值/理由/发起者,用于追溯纪律何时被谁如何改动。"""
    sym = ""
    if symbol:
        try:
            sym = schema.valid_symbol(symbol)
        except schema.ValidationError as e:
            return f"❌ {e}"
    async with _lock:
        events = store.read_events()
    rows = [e for e in events
            if (not sym or e.get("symbol") == sym)
            and (not field or e.get("field") == field)]
    if not rows:
        return "无匹配的历史记录。"
    lines = [f"🧾 纪律变更历史({len(rows)} 条)"]
    for e in rows:
        if e["op"] == "SET":
            chg = f" {e.get('field')}: {e.get('old_value')} → {e.get('new_value')}"
        elif e["op"] == "BULK_SET":
            chg = f" {list((e.get('new_value') or {}).keys())}"
        elif e["op"] in ("LOCK", "UNLOCK", "DELETE_FIELD"):
            chg = f" {e.get('field')}"
        elif e["op"] in ("RETIRE", "REINSTATE"):
            chg = f" → {e.get('new_value') or 'RETIRED'}"
        else:
            chg = ""
        lines.append(f"  #{e['seq']} {e['ts']} [{e['actor']}] {e['op']}{chg}")
        lines.append(f"        理由: {e.get('rationale')}")
    return "\n".join(lines)


@mcp.tool()
async def verify_chain() -> str:
    """校验纪律账本的 hash chain 完整性。返回 OK(含 head hash 供外部钉)或断裂位置。"""
    async with _lock:
        events = store.read_events()
        try:
            store.verify_chain(events)
        except store.ChainError as e:
            return f"❌ 链校验失败:{e}"
        head = store.last_hash(events)
    return f"✅ 链完整,共 {len(events)} 条事件,未发现篡改。head_hash={head}"


# --------------------------------------------------------------------- #
# WRITE tools (default dry-run; confirm=True to commit)
# --------------------------------------------------------------------- #
def _require_rationale(rationale):
    return bool(rationale and rationale.strip())


@mcp.tool()
async def set_rule(symbol: str, field: str, value, rationale: str,
                   confirm: bool = False, current_price: float = None,
                   actor: str = "claude") -> str:
    """设置/修改单只标的的一个纪律字段(默认 dry-run)。

    symbol 如 'sh.600483';field 见 schema(intrinsic_low/high、add_zone_high、
    add_tranches、trim_zone_low、stop_loss、clear_line、hard_triggers、status…)。
    rationale 必填。confirm=False 仅预览校验结果不写;confirm=True 才追加事件。
    锁定且已有值的字段(默认 stop_loss/clear_line)会被拒(需先 unlock_rule)。
    status 不可直接设为 RETIRED(请用 retire_symbol)。可选 current_price 触发止损/追高现价校验。
    """
    if not _require_rationale(rationale):
        return "❌ rationale(修改理由)必填。"
    try:
        sym = schema.valid_symbol(symbol)
        cp = schema.valid_current_price(current_price)
        if not schema.known_field(field):
            raise schema.ValidationError(f"未知字段 {field}。允许:{sorted(schema.FIELD_TYPES)}")
        if field == "status" and value in schema.LIFECYCLE_STATUSES:
            raise schema.ValidationError("status 不能直接设为 RETIRED,请用 retire_symbol")
    except schema.ValidationError as e:
        return f"❌ {e}"

    async with _lock:
        try:
            state, head = store.load()
        except store.ChainError as e:
            return f"❌ 纪律账本校验失败,拒绝写入:{e}"
        rule = state.get(sym, _blank_rule(sym))
        if rule.get("status") == "RETIRED":
            return f"❌ {sym} 已 RETIRED,拒绝写入。如需恢复请先 reinstate_symbol。"
        if _change_blocked(rule, field):
            return (f"❌ {sym}.{field} 已锁定 🔒(且已有值)。如确需修改,先调用 "
                    f"unlock_rule('{sym}', '{field}', <理由>, confirm=true)。")
        try:
            new = schema.coerce_value(field, value)
            candidate = dict(rule); candidate[field] = new
            warnings = schema.validate_rule(candidate, current_price=cp)
        except schema.ValidationError as e:
            return _preview(f"{sym}.{field}: {rule.get(field)} → {value}", [], errors=[str(e)])

        if not confirm:
            return _preview(f"{sym}.{field}: {rule.get(field)} → {new}", warnings)
        try:
            ev = store.append_event(symbol=sym, op=store.OP_SET, field=field,
                                    old_value=rule.get(field), new_value=new,
                                    rationale=rationale, actor=actor, expected_head=head)
        except store.ChainError as e:
            return f"❌ 写入失败:{e}"
    msg = [f"✅ 已写入 #{ev['seq']}:{sym}.{field} = {new}"]
    msg += [f"⚠️ {w}" for w in warnings]
    return "\n".join(msg)


@mcp.tool()
async def set_rule_bulk(symbol: str, fields: dict, rationale: str,
                        confirm: bool = False, current_price: float = None,
                        actor: str = "claude") -> str:
    """一次设置一只标的的多个字段(初始化/迁移用,默认 dry-run)。
    **原子写入**:整批校验通过后,作为单条 BULK_SET 事件落盘(全有或全无)。
    任一字段非法/未知/锁定(已有值)则整批拒绝。status 不可在此设为 RETIRED。"""
    if not _require_rationale(rationale):
        return "❌ rationale 必填。"
    if not isinstance(fields, dict) or not fields:
        return "❌ fields 需为非空字典 {字段: 值}。"
    try:
        sym = schema.valid_symbol(symbol)
        cp = schema.valid_current_price(current_price)
    except schema.ValidationError as e:
        return f"❌ {e}"

    async with _lock:
        try:
            state, head = store.load()
        except store.ChainError as e:
            return f"❌ 账本校验失败,拒绝写入:{e}"
        rule = dict(state.get(sym, _blank_rule(sym)))
        if rule.get("status") == "RETIRED":
            return f"❌ {sym} 已 RETIRED,拒绝写入。如需恢复请先 reinstate_symbol。"

        coerced = {}
        try:
            for f, v in fields.items():
                if not schema.known_field(f):
                    raise schema.ValidationError(f"未知字段 {f}")
                if f == "status":
                    raise schema.ValidationError("status 不能在批量中设置,请用 set_rule 或 retire_symbol/reinstate_symbol")
                if _change_blocked(rule, f):
                    raise schema.ValidationError(f"{f} 已锁定(且已有值),需先 unlock_rule")
                coerced[f] = schema.coerce_value(f, v)
            candidate = dict(rule); candidate.update(coerced)
            warnings = schema.validate_rule(candidate, current_price=cp)
        except schema.ValidationError as e:
            return _preview(f"{sym} 批量设 {list(fields)}", [], errors=[str(e)])

        old = {f: rule.get(f) for f in coerced}
        if not confirm:
            chg = "\n".join(f"   {f}: {old[f]} → {coerced[f]}" for f in coerced)
            return _preview(f"{sym} 批量设 {len(coerced)} 字段(单条 BULK_SET 原子写入)\n{chg}", warnings)
        try:
            ev = store.append_event(symbol=sym, op=store.OP_BULK_SET, field=None,
                                    old_value=old, new_value=coerced,
                                    rationale=rationale, actor=actor, expected_head=head)
        except store.ChainError as e:
            return f"❌ 写入失败:{e}"
    msg = [f"✅ 已原子写入 #{ev['seq']}:{sym} 的 {len(coerced)} 个字段 {list(coerced)}"]
    msg += [f"⚠️ {w}" for w in warnings]
    return "\n".join(msg)


async def _toggle_lock(symbol, field, rationale, confirm, actor, *, lock):
    if not _require_rationale(rationale):
        return "❌ rationale 必填。"
    try:
        sym = schema.valid_symbol(symbol)
        if not schema.known_field(field):
            raise schema.ValidationError(f"未知字段 {field}")
    except schema.ValidationError as e:
        return f"❌ {e}"
    async with _lock:
        try:
            state, head = store.load()
        except store.ChainError as e:
            return f"❌ 账本校验失败,拒绝写入:{e}"
        rule = state.get(sym)
        if not rule:
            return f"未找到 {sym}。"
        if rule.get("status") == "RETIRED":
            return f"❌ {sym} 已 RETIRED,拒绝写入。"
        locked = field in rule.get("locked_fields", [])
        if lock and locked:
            return f"{sym}.{field} 已处于锁定状态。"
        if not lock and not locked:
            return f"{sym}.{field} 当前未锁定。"
        if not confirm:
            verb = "锁定" if lock else "解锁(解锁后该字段可被修改)"
            return _preview(f"{verb} {sym}.{field}", [])
        op = store.OP_LOCK if lock else store.OP_UNLOCK
        try:
            ev = store.append_event(symbol=sym, op=op, field=field,
                                    rationale=rationale, actor=actor, expected_head=head)
        except store.ChainError as e:
            return f"❌ 写入失败:{e}"
    if lock:
        return f"🔒 已锁定 #{ev['seq']}:{sym}.{field}"
    return f"🔓 已解锁 #{ev['seq']}:{sym}.{field}(记得改完再 lock_rule 锁回)"


@mcp.tool()
async def lock_rule(symbol: str, field: str, rationale: str, confirm: bool = False,
                    actor: str = "claude") -> str:
    """锁定某字段(锁定且字段已有值时 set_rule 拒改,需先 unlock)。stop_loss/clear_line 默认锁定。"""
    return await _toggle_lock(symbol, field, rationale, confirm, actor, lock=True)


@mcp.tool()
async def unlock_rule(symbol: str, field: str, rationale: str, confirm: bool = False,
                      actor: str = "claude") -> str:
    """解锁字段(解锁后才能 set_rule 修改)。这是有意的额外摩擦——动止损/清仓线前先过这一步。"""
    return await _toggle_lock(symbol, field, rationale, confirm, actor, lock=False)


@mcp.tool()
async def retire_symbol(symbol: str, rationale: str, confirm: bool = False,
                        actor: str = "claude") -> str:
    """将标的标记为 RETIRED(剔除,如价值陷阱),保留全部历史。恢复用 reinstate_symbol。"""
    if not _require_rationale(rationale):
        return "❌ rationale 必填。"
    try:
        sym = schema.valid_symbol(symbol)
    except schema.ValidationError as e:
        return f"❌ {e}"
    async with _lock:
        try:
            state, head = store.load()
        except store.ChainError as e:
            return f"❌ 账本校验失败,拒绝写入:{e}"
        rule = state.get(sym)
        if not rule:
            return f"未找到 {sym}。"
        if rule.get("status") == "RETIRED":
            return f"{sym} 已是 RETIRED。"
        if not confirm:
            return _preview(f"将 {sym} 标记为 RETIRED", [])
        try:
            ev = store.append_event(symbol=sym, op=store.OP_RETIRE, rationale=rationale,
                                    actor=actor, expected_head=head)
        except store.ChainError as e:
            return f"❌ 写入失败:{e}"
    return f"🗑 已剔除 #{ev['seq']}:{sym} → RETIRED"


@mcp.tool()
async def reinstate_symbol(symbol: str, status: str = "WATCH", rationale: str = "",
                           confirm: bool = False, actor: str = "claude") -> str:
    """将 RETIRED 标的恢复为某非 RETIRED 状态(默认 WATCH)。这是退出 RETIRED 的唯一入口。"""
    if not _require_rationale(rationale):
        return "❌ rationale 必填。"
    try:
        sym = schema.valid_symbol(symbol)
        if status not in schema.STATUSES or status in schema.LIFECYCLE_STATUSES:
            raise schema.ValidationError(f"恢复目标状态非法:{status}(不能是 RETIRED)")
    except schema.ValidationError as e:
        return f"❌ {e}"
    async with _lock:
        try:
            state, head = store.load()
        except store.ChainError as e:
            return f"❌ 账本校验失败,拒绝写入:{e}"
        rule = state.get(sym)
        if not rule:
            return f"未找到 {sym}。"
        if rule.get("status") != "RETIRED":
            return f"{sym} 当前不是 RETIRED(status={rule.get('status')}),无需 reinstate。"
        if not confirm:
            return _preview(f"将 {sym} 从 RETIRED 恢复为 {status}", [])
        try:
            ev = store.append_event(symbol=sym, op=store.OP_REINSTATE, new_value=status,
                                    rationale=rationale, actor=actor, expected_head=head)
        except store.ChainError as e:
            return f"❌ 写入失败:{e}"
    return f"♻️ 已恢复 #{ev['seq']}:{sym} → {status}"


def main() -> None:
    logger.info("starting discipline-mcp (stdio); db=%s", store.db_path())
    if store.fcntl is None:  # non-POSIX: no cross-process file lock
        logger.warning("fcntl 不可用(非 POSIX 平台):跨进程文件锁未启用,"
                       "请勿多实例并发写同一账本(单实例使用不受影响)。")
    web.start_web_server()  # best-effort read-only viewer; never blocks the MCP
    mcp.run()


if __name__ == "__main__":
    main()
