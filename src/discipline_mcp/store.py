"""Append-only JSONL event store with a hash chain.

Discipline rules are event-sourced: every change appends one immutable event to
``rule_events.jsonl``. Current rule state is derived by REPLAYING the log — it is
never stored separately. Each event carries ``prev_hash`` + ``hash`` forming a
chain; tampering with any historical line breaks the chain and ``replay`` raises.

Stdlib only (no DB, no git, no network). Writes take an OS file lock and fsync;
within the single stdio process the server also serializes calls behind one
asyncio.Lock. NaN/Infinity are rejected on the way in so the log can never become
unparseable JSON.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

try:
    import fcntl  # POSIX file locking (macOS/Linux)
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

DEFAULT_DB = Path.home() / ".discipline-mcp" / "rule_events.jsonl"
GENESIS = "genesis"

# fields locked by default once they hold a value (the two sell defences)
DEFAULT_LOCKED = ("stop_loss", "clear_line")

# op codes. NOTE: there is intentionally NO delete-field op — clearing a field
# is done with SET <field>=None/[] (which, for a locked field that already holds
# a value, is itself blocked until unlock). Omitting delete removes the only
# path that could reset a locked field back to a "never set" state and thus
# bypass the unlock requirement.
OP_SET = "SET"
OP_BULK_SET = "BULK_SET"
OP_LOCK = "LOCK"
OP_UNLOCK = "UNLOCK"
OP_RETIRE = "RETIRE"
OP_REINSTATE = "REINSTATE"
OPS = {OP_SET, OP_BULK_SET, OP_LOCK, OP_UNLOCK, OP_RETIRE, OP_REINSTATE}
# ops that name a single field
FIELD_OPS = {OP_SET, OP_LOCK, OP_UNLOCK}
# replay-managed keys that no SET/DELETE/LOCK/BULK_SET event may target directly
RESERVED_KEYS = {"symbol", "locked_fields", "updated_at"}
RETIRED = "RETIRED"


class ChainError(RuntimeError):
    """Raised when the hash chain is broken (tampering / corruption)."""


def db_path() -> Path:
    return Path(os.environ.get("DISCIPLINE_MCP_DB") or DEFAULT_DB)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _reject_nonfinite(obj):
    """Recursively assert no NaN/Inf float hides in an event value."""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError(f"非有限数值(NaN/Inf)不允许写入:{obj}")
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_nonfinite(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _reject_nonfinite(v)


def _canonical(event: dict) -> str:
    """Deterministic JSON of an event EXCLUDING its own ``hash`` field.

    allow_nan=False guarantees a NaN/Inf can never be serialized into the chain
    (it would otherwise become unparseable on read and brick the whole log).
    """
    payload = {k: v for k, v in event.items() if k != "hash"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def compute_hash(event: dict) -> str:
    """sha256 over the canonical payload (which already includes prev_hash)."""
    return hashlib.sha256(_canonical(event).encode("utf-8")).hexdigest()


def _strict_constant(s):  # parse_constant hook: refuse NaN/Infinity tokens
    raise ChainError(f"日志含非法 JSON 常量({s});文件可能被手工编辑或损坏")


def read_events(path=None) -> list[dict]:
    """Load all events in file order under a SHARED file lock (so a reader never
    sees a half-written line while a writer holds LOCK_EX). Does NOT verify."""
    p = Path(path) if path is not None else db_path()
    if not p.exists():
        return []
    fd = os.open(str(p), os.O_RDONLY)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_SH)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            try:
                text = fh.read()
            except UnicodeDecodeError as e:
                raise ChainError(f"日志非 UTF-8 文本(可能损坏):{e}") from e
            return _parse_lines(text)
    finally:
        if fd >= 0:
            os.close(fd)


def verify_chain(events) -> None:
    """Raise ChainError on the first broken/invalid link; None if intact.

    Per event: seq strictly 1..N, prev_hash links to the previous hash (GENESIS
    for the first), stored hash recomputes, op is known, and field-naming ops
    actually carry a field. This makes a hash-valid but semantically bogus event
    fail verification rather than be silently ignored at replay.
    """
    prev = GENESIS
    for i, e in enumerate(events):
        seq = i + 1
        if e.get("seq") != seq:
            raise ChainError(f"seq 不连续:第 {seq} 条应为 {seq},实际 {e.get('seq')}")
        if e.get("prev_hash") != prev:
            raise ChainError(
                f"链断裂(seq={seq}):prev_hash 应为 {prev[:12]}…,"
                f"实际 {str(e.get('prev_hash'))[:12]}… —— 历史可能被篡改"
            )
        if e.get("hash") != compute_hash(e):
            raise ChainError(f"哈希不匹配(seq={seq}):该事件内容被改动过")
        op = e.get("op")
        if op not in OPS:
            raise ChainError(f"未知操作(seq={seq}):{op!r}")
        if op in FIELD_OPS:
            f = e.get("field")
            if not f:
                raise ChainError(f"操作 {op} 缺少 field(seq={seq})")
            if f in RESERVED_KEYS:
                raise ChainError(f"事件不得直接操作受管字段 {f}(seq={seq})")
            if op == OP_SET and f == "status" and e.get("new_value") == RETIRED:
                raise ChainError(f"status=RETIRED 只能经 RETIRE 操作(seq={seq})")
        if op == OP_BULK_SET:
            nv = e.get("new_value")
            if not isinstance(nv, dict) or not nv:
                raise ChainError(f"BULK_SET 的 new_value 必须是非空字典(seq={seq})")
            for k in nv:
                if k in RESERVED_KEYS:
                    raise ChainError(f"BULK_SET 不得操作受管字段 {k}(seq={seq})")
                if k == "status":
                    raise ChainError(f"BULK_SET 不得设 status(请用 set_rule/retire/reinstate)(seq={seq})")
        if op == OP_REINSTATE:
            nv = e.get("new_value")
            if not isinstance(nv, str) or not nv or nv == RETIRED:
                raise ChainError(f"REINSTATE 目标状态非法(seq={seq}):{nv!r}")
        prev = e["hash"]


def head_hash(path=None) -> str:
    """Latest event hash (or GENESIS). External callers can pin this to detect
    later rewrites of the whole file."""
    return last_hash(read_events(path))


def last_hash(events) -> str:
    return events[-1]["hash"] if events else GENESIS


def _parse_lines(text):
    """Parse already-read file text into events (same checks as read_events)."""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line, parse_constant=_strict_constant)
        except json.JSONDecodeError as e:
            raise ChainError(f"第 {lineno} 行不是合法 JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ChainError(f"第 {lineno} 行不是 JSON 对象")
        try:
            _reject_nonfinite(obj)
        except ValueError as e:
            raise ChainError(f"第 {lineno} 行含非有限数值: {e}") from e
        out.append(obj)
    return out


def _append_events(p, events_to_write, expected_head=None) -> None:
    """Append all events atomically under an exclusive file lock.

    Opens the file once (0600), takes flock, then reads+verifies the head FROM
    THE SAME fd (no reopen-by-path, so a symlink/path swap can't slip in), checks
    optimistic-concurrency token, writes all lines, fsyncs. All-or-nothing vs
    other locked writers.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)  # tighten perms even if file pre-existed broader
        with os.fdopen(fd, "r+", encoding="utf-8") as fh:
            fd = -1  # ownership passed to fh
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0)
            existing = _parse_lines(fh.read())
            verify_chain(existing)
            head = last_hash(existing)
            if expected_head is not None and head != expected_head:
                raise ChainError("并发修改:账本头已变化(期望 "
                                 f"{expected_head[:12]}…,实际 {head[:12]}…),请重读后重试")
            prev, seq, buf = head, len(existing) + 1, []
            for ev in events_to_write:
                ev["seq"] = seq
                ev["prev_hash"] = prev
                ev["hash"] = compute_hash(ev)
                buf.append(json.dumps(ev, ensure_ascii=False, allow_nan=False))
                prev, seq = ev["hash"], seq + 1
            # self-check: never persist events that would fail our own grammar
            verify_chain(existing + list(events_to_write))
            fh.seek(0, os.SEEK_END)
            fh.write("\n".join(buf) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _make_event(symbol, op, field, old_value, new_value, rationale, actor) -> dict:
    ev = {
        "ts": _now(), "symbol": symbol, "op": op, "field": field,
        "old_value": old_value, "new_value": new_value,
        "rationale": rationale, "actor": actor,
    }
    _reject_nonfinite(ev)
    return ev


def append_event(*, symbol, op, field=None, old_value=None, new_value=None,
                 rationale, actor="claude", expected_head=None, path=None) -> dict:
    """Append one linked event under an exclusive file lock.

    The whole operation is one fsynced write of a single line; tools never write
    more than one event per call (a bulk change is one BULK_SET event), so there
    is no multi-line transaction to tear. A crash mid-write can at worst leave a
    torn final line, which the next read rejects via verify_chain (remove that
    last line to recover). If ``expected_head`` is given, the write aborts
    (ChainError) when the on-disk head moved since the caller loaded state —
    optimistic concurrency so validation done against a snapshot can't land on a
    changed log. The proposed chain is self-verified before it is persisted."""
    p = Path(path) if path is not None else db_path()
    ev = _make_event(symbol, op, field, old_value, new_value, rationale, actor)
    _append_events(p, [ev], expected_head=expected_head)
    return ev


def replay(events) -> dict:
    """Fold events into ``{symbol: rule_dict}``. Verifies the chain first.

    A new symbol's rule is seeded with ``locked_fields = DEFAULT_LOCKED`` so the
    sell defences are protected from birth (see server's change-block rule). The
    event-level ``rationale`` is NOT copied into the rule; the rule has its own
    ``rationale`` field set only by an explicit SET rationale.
    """
    verify_chain(events)
    state: dict[str, dict] = {}
    for e in events:
        sym = e["symbol"]
        rule = state.setdefault(sym, {
            "symbol": sym, "locked_fields": list(DEFAULT_LOCKED),
            "status": "WATCH", "updated_at": None,
        })
        op = e["op"]
        if op == OP_SET:
            rule[e["field"]] = e["new_value"]
        elif op == OP_BULK_SET:
            for f, v in e["new_value"].items():
                rule[f] = v
        elif op == OP_LOCK:
            if e["field"] not in rule["locked_fields"]:
                rule["locked_fields"].append(e["field"])
        elif op == OP_UNLOCK:
            if e["field"] in rule["locked_fields"]:
                rule["locked_fields"].remove(e["field"])
        elif op == OP_RETIRE:
            rule["status"] = "RETIRED"
        elif op == OP_REINSTATE:
            rule["status"] = e.get("new_value") or "WATCH"
        rule["updated_at"] = e["ts"]
    return state


def load(path=None):
    """Return (state, head_hash) from one read — head pins the snapshot so write
    tools can pass it as expected_head for optimistic concurrency."""
    events = read_events(path)
    state = replay(events)
    return state, last_hash(events)
