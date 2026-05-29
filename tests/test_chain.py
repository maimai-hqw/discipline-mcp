"""Hash chain: integrity, tamper detection, append linkage."""
import json

import pytest

from discipline_mcp import store


def _append(tmp, **kw):
    kw.setdefault("rationale", "test")
    return store.append_event(path=tmp, **kw)


def test_append_links_and_verifies(tmp_path):
    f = tmp_path / "e.jsonl"
    _append(f, symbol="sh.600483", op=store.OP_SET, field="intrinsic_low", new_value=15)
    _append(f, symbol="sh.600483", op=store.OP_SET, field="intrinsic_high", new_value=21)
    events = store.read_events(f)
    assert [e["seq"] for e in events] == [1, 2]
    assert events[0]["prev_hash"] == store.GENESIS
    assert events[1]["prev_hash"] == events[0]["hash"]
    store.verify_chain(events)  # no raise


def test_hash_is_deterministic():
    e = {"seq": 1, "ts": "t", "symbol": "x", "op": "SET", "field": "f",
         "old_value": None, "new_value": 1, "rationale": "r", "actor": "a",
         "prev_hash": store.GENESIS}
    assert store.compute_hash(e) == store.compute_hash(dict(e))


def test_tamper_value_breaks_chain(tmp_path):
    f = tmp_path / "e.jsonl"
    _append(f, symbol="x", op=store.OP_SET, field="stop_loss", new_value=30)
    _append(f, symbol="x", op=store.OP_SET, field="stop_loss", new_value=31)
    # tamper: edit the first line's value, leave its hash as-is
    lines = f.read_text().splitlines()
    obj = json.loads(lines[0]); obj["new_value"] = 999
    lines[0] = json.dumps(obj, ensure_ascii=False)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_delete_middle_line_breaks_seq(tmp_path):
    f = tmp_path / "e.jsonl"
    for i in range(3):
        _append(f, symbol="x", op=store.OP_SET, field="intrinsic_low", new_value=i + 1)
    lines = f.read_text().splitlines()
    f.write_text("\n".join([lines[0], lines[2]]) + "\n")  # drop middle
    with pytest.raises(store.ChainError):
        store.verify_chain(store.read_events(f))


def test_append_refuses_to_extend_tampered_chain(tmp_path):
    f = tmp_path / "e.jsonl"
    _append(f, symbol="x", op=store.OP_SET, field="intrinsic_low", new_value=10)
    lines = f.read_text().splitlines()
    obj = json.loads(lines[0]); obj["new_value"] = 999
    lines[0] = json.dumps(obj, ensure_ascii=False)
    f.write_text("\n".join(lines) + "\n")
    with pytest.raises(store.ChainError):
        _append(f, symbol="x", op=store.OP_SET, field="intrinsic_high", new_value=20)


def test_empty_and_missing_file(tmp_path):
    assert store.read_events(tmp_path / "nope.jsonl") == []
    store.verify_chain([])  # no raise
