"""Server-level (async) tool behavior: confirm gate, default-lock semantics,
status lifecycle, atomic bulk, symbol validation."""
import asyncio

import pytest

from discipline_mcp import server, store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCIPLINE_MCP_DB", str(tmp_path / "rules.jsonl"))
    return tmp_path / "rules.jsonl"


def run(coro):
    return asyncio.run(coro)


def test_dry_run_does_not_write(db):
    r = run(server.set_rule("sh.600483", "intrinsic_low", 15, "下沿", confirm=False))
    assert "预览" in r
    assert store.read_events(db) == []


def test_margin_of_safety_reject(db):
    run(server.set_rule("sh.600483", "intrinsic_low", 15, "下沿", confirm=True))
    r = run(server.set_rule("sh.600483", "add_zone_high", 16, "想加", confirm=True))
    assert "安全边际" in r and "校验未通过" in r


def test_default_lock_allows_first_set_blocks_change(db):
    # first set of stop_loss (None -> value) is allowed despite default lock
    r1 = run(server.set_rule("sh.600483", "stop_loss", 8, "首设止损", confirm=True,
                             current_price=11))
    assert "已写入" in r1
    # changing it is blocked until unlock
    r2 = run(server.set_rule("sh.600483", "stop_loss", 9, "想动止损", confirm=True,
                             current_price=11))
    assert "已锁定" in r2
    # unlock -> change works
    run(server.unlock_rule("sh.600483", "stop_loss", "复审", confirm=True))
    r3 = run(server.set_rule("sh.600483", "stop_loss", 9, "放宽", confirm=True,
                             current_price=11))
    assert "已写入" in r3


def test_status_retired_only_via_tool(db):
    run(server.set_rule("sh.600483", "intrinsic_low", 15, "x", confirm=True))
    r = run(server.set_rule("sh.600483", "status", "RETIRED", "想退役", confirm=True))
    assert "retire_symbol" in r


def test_retire_blocks_writes_then_reinstate(db):
    run(server.set_rule("sz.000027", "intrinsic_low", 5, "x", confirm=True))
    run(server.retire_symbol("sz.000027", "价值陷阱", confirm=True))
    blocked = run(server.set_rule("sz.000027", "intrinsic_low", 6, "改", confirm=True))
    assert "RETIRED" in blocked
    run(server.reinstate_symbol("sz.000027", "WATCH", "重新观察", confirm=True))
    ok = run(server.set_rule("sz.000027", "intrinsic_low", 6, "改", confirm=True))
    assert "已写入" in ok


def test_bulk_atomic_single_event(db):
    run(server.set_rule_bulk("sh.600483",
                             {"intrinsic_low": 15, "intrinsic_high": 21, "add_zone_high": 11},
                             "初始化", confirm=True))
    events = store.read_events(db)
    assert len(events) == 1 and events[0]["op"] == "BULK_SET"


def test_bulk_rejects_bad_field_atomically(db):
    r = run(server.set_rule_bulk("sh.600483",
                                 {"intrinsic_low": 20, "intrinsic_high": 10},  # low>high
                                 "坏数据", confirm=True))
    assert "校验未通过" in r
    assert store.read_events(db) == []  # nothing written


def test_invalid_symbol_rejected(db):
    r = run(server.set_rule("600483", "intrinsic_low", 15, "x", confirm=True))
    assert "非法 symbol" in r


def test_rationale_required(db):
    r = run(server.set_rule("sh.600483", "intrinsic_low", 15, "  ", confirm=True))
    assert "rationale" in r


def test_stop_above_price_rejected(db):
    r = run(server.set_rule("sh.600483", "stop_loss", 50, "错误止损", confirm=True,
                            current_price=44))
    assert "校验未通过" in r and "现价" in r
