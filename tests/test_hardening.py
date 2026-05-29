"""Tests for review-round-1 hardening: NaN/Inf, default locks, atomic bulk,
unknown-op rejection, symbol/current_price validation, file perms."""
import json
import math
import os
import stat

import pytest

from discipline_mcp import store, schema
from discipline_mcp.schema import ValidationError


# ---- NaN / Inf ----
def test_coerce_rejects_nan_inf():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            schema.coerce_value("intrinsic_low", bad)


def test_store_rejects_nonfinite_event(tmp_path):
    f = tmp_path / "e.jsonl"
    with pytest.raises(ValueError):
        store.append_event(path=f, symbol="x", op=store.OP_SET, field="stop_loss",
                           new_value=float("inf"), rationale="t")
    assert store.read_events(f) == []  # nothing written


def test_bool_rejected():
    with pytest.raises(ValidationError):
        schema.coerce_value("intrinsic_low", True)


def test_read_rejects_nan_token(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('{"seq":1,"new_value":NaN}\n')
    with pytest.raises(store.ChainError):
        store.read_events(f)


# ---- default locks ----
def test_default_locked_seeded(tmp_path):
    f = tmp_path / "e.jsonl"
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_low",
                       new_value=10, rationale="t")
    rule = store.replay(store.read_events(f))["x"]
    assert "stop_loss" in rule["locked_fields"]
    assert "clear_line" in rule["locked_fields"]


def test_no_delete_op_exists():
    # delete-field was intentionally removed to close the lock-bypass vector
    assert not hasattr(store, "OP_DELETE_FIELD")
    assert "DELETE_FIELD" not in store.OPS


def test_forged_delete_event_rejected(tmp_path):
    # a hand-crafted DELETE_FIELD event is now an unknown op -> chain rejects it
    f = tmp_path / "e.jsonl"
    ev = {"seq": 1, "ts": "t", "symbol": "x", "op": "DELETE_FIELD", "field": "stop_loss",
          "old_value": None, "new_value": None, "rationale": "r", "actor": "a",
          "prev_hash": store.GENESIS}
    ev["hash"] = store.compute_hash(ev)
    f.write_text(json.dumps(ev, ensure_ascii=False) + "\n")
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_clearing_locked_field_to_none_blocked_at_server(tmp_path, monkeypatch):
    # the only "reset" path (SET None) on a locked field that already holds a
    # value is itself blocked until unlock — so there is no lock bypass.
    import asyncio
    from discipline_mcp import server
    monkeypatch.setenv("DISCIPLINE_MCP_DB", str(f := tmp_path / "s.jsonl"))
    asyncio.run(server.set_rule("sh.600483", "stop_loss", 8, "首设", confirm=True,
                                current_price=11))
    r = asyncio.run(server.set_rule("sh.600483", "stop_loss", None, "想清空绕过",
                                    confirm=True))
    assert "已锁定" in r


# ---- atomic bulk ----
def test_bulk_set_is_single_event_and_replays(tmp_path):
    f = tmp_path / "e.jsonl"
    store.append_event(path=f, symbol="x", op=store.OP_BULK_SET, field=None,
                       old_value={"intrinsic_low": None, "intrinsic_high": None},
                       new_value={"intrinsic_low": 15, "intrinsic_high": 21},
                       rationale="init")
    events = store.read_events(f)
    assert len(events) == 1  # atomic: one line
    rule = store.replay(events)["x"]
    assert rule["intrinsic_low"] == 15 and rule["intrinsic_high"] == 21


def test_append_rejects_bad_op_before_write(tmp_path):
    # store must not persist an event that would fail its own verify grammar
    f = tmp_path / "e.jsonl"
    with pytest.raises(store.ChainError):
        store.append_event(path=f, symbol="x", op="HACK", field="intrinsic_low",
                           new_value=1, rationale="t")
    assert store.read_events(f) == []  # nothing written


def test_append_rejects_reserved_field_before_write(tmp_path):
    f = tmp_path / "e.jsonl"
    with pytest.raises(store.ChainError):
        store.append_event(path=f, symbol="x", op=store.OP_SET, field="locked_fields",
                           new_value=[], rationale="t")
    assert store.read_events(f) == []


# ---- unknown op rejected by verify ----
def test_unknown_op_breaks_verify(tmp_path):
    f = tmp_path / "e.jsonl"
    ev = {"seq": 1, "ts": "t", "symbol": "x", "op": "HACK", "field": None,
          "old_value": None, "new_value": 1, "rationale": "r", "actor": "a",
          "prev_hash": store.GENESIS}
    ev["hash"] = store.compute_hash(ev)  # valid hash, bogus op
    f.write_text(json.dumps(ev, ensure_ascii=False) + "\n")
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_field_op_missing_field_breaks_verify(tmp_path):
    f = tmp_path / "e.jsonl"
    ev = {"seq": 1, "ts": "t", "symbol": "x", "op": "SET", "field": None,
          "old_value": None, "new_value": 1, "rationale": "r", "actor": "a",
          "prev_hash": store.GENESIS}
    ev["hash"] = store.compute_hash(ev)
    f.write_text(json.dumps(ev, ensure_ascii=False) + "\n")
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


# ---- symbol / current_price validation ----
@pytest.mark.parametrize("good", ["sh.600519", "sz.002049", "bj.430047", "SH.600519"])
def test_valid_symbol_ok(good):
    assert schema.valid_symbol(good) == good.lower()


@pytest.mark.parametrize("bad", ["", " ", "600519", "sh600519", "../etc", "sh.60051", "x"])
def test_valid_symbol_rejects(bad):
    with pytest.raises(ValidationError):
        schema.valid_symbol(bad)


def test_current_price_validation():
    assert schema.valid_current_price(None) is None
    assert schema.valid_current_price("11.05") == 11.05
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            schema.valid_current_price(bad)


# ---- file perms ----
@pytest.mark.skipif(not hasattr(os, "umask"), reason="POSIX only")
def test_new_db_file_is_0600(tmp_path):
    f = tmp_path / "perm.jsonl"
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_low",
                       new_value=1, rationale="t")
    mode = stat.S_IMODE(os.stat(f).st_mode)
    assert mode & 0o077 == 0  # no group/other access


# ---- round-2: semantic verify guards ----
def _forge(f, ev):
    ev = dict(ev); ev["hash"] = store.compute_hash(ev)
    f.write_text(json.dumps(ev, ensure_ascii=False) + "\n")


def test_verify_rejects_reserved_field_set(tmp_path):
    f = tmp_path / "e.jsonl"
    _forge(f, {"seq": 1, "ts": "t", "symbol": "x", "op": "SET", "field": "locked_fields",
               "old_value": None, "new_value": [], "rationale": "r", "actor": "a",
               "prev_hash": store.GENESIS})
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_verify_rejects_set_status_retired(tmp_path):
    f = tmp_path / "e.jsonl"
    _forge(f, {"seq": 1, "ts": "t", "symbol": "x", "op": "SET", "field": "status",
               "old_value": None, "new_value": "RETIRED", "rationale": "r", "actor": "a",
               "prev_hash": store.GENESIS})
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_verify_rejects_reinstate_to_retired(tmp_path):
    f = tmp_path / "e.jsonl"
    _forge(f, {"seq": 1, "ts": "t", "symbol": "x", "op": "REINSTATE", "field": None,
               "old_value": None, "new_value": "RETIRED", "rationale": "r", "actor": "a",
               "prev_hash": store.GENESIS})
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_verify_rejects_bulk_reserved_key(tmp_path):
    f = tmp_path / "e.jsonl"
    _forge(f, {"seq": 1, "ts": "t", "symbol": "x", "op": "BULK_SET", "field": None,
               "old_value": None, "new_value": {"symbol": "evil"}, "rationale": "r",
               "actor": "a", "prev_hash": store.GENESIS})
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_read_rejects_overflow_float(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text('{"seq":1,"new_value":1e9999}\n')  # parses to inf via parse_float
    with pytest.raises(store.ChainError):
        store.read_events(f)


# ---- round-2: optimistic concurrency ----
def test_expected_head_aborts_on_stale(tmp_path):
    f = tmp_path / "e.jsonl"
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_low",
                       new_value=10, rationale="t")
    stale = store.GENESIS  # pretend caller loaded an empty log
    with pytest.raises(store.ChainError):
        store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_high",
                           new_value=20, rationale="t", expected_head=stale)


def test_expected_head_ok_when_fresh(tmp_path):
    f = tmp_path / "e.jsonl"
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_low",
                       new_value=10, rationale="t")
    _, head = store.load(f)
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_high",
                       new_value=20, rationale="t", expected_head=head)
    assert store.replay(store.read_events(f))["x"]["intrinsic_high"] == 20


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX only")
def test_existing_broad_file_tightened(tmp_path):
    f = tmp_path / "e.jsonl"
    f.write_text("")  # pre-create
    os.chmod(f, 0o644)
    store.append_event(path=f, symbol="x", op=store.OP_SET, field="intrinsic_low",
                       new_value=1, rationale="t")
    assert stat.S_IMODE(os.stat(f).st_mode) & 0o077 == 0
