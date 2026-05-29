"""Replay: folding events into current rule state."""
from discipline_mcp import store


def _ap(f, **kw):
    kw.setdefault("rationale", "t")
    return store.append_event(path=f, **kw)


def test_set_overrides_and_tracks_updated_at(tmp_path):
    f = tmp_path / "e.jsonl"
    _ap(f, symbol="sh.600483", op=store.OP_SET, field="intrinsic_low", new_value=12)
    _ap(f, symbol="sh.600483", op=store.OP_SET, field="intrinsic_low", new_value=15)
    state = store.replay(store.read_events(f))
    assert state["sh.600483"]["intrinsic_low"] == 15
    assert state["sh.600483"]["updated_at"] is not None


def test_lock_unlock(tmp_path):
    f = tmp_path / "e.jsonl"
    _ap(f, symbol="x", op=store.OP_SET, field="stop_loss", new_value=30)
    _ap(f, symbol="x", op=store.OP_LOCK, field="stop_loss")
    assert "stop_loss" in store.replay(store.read_events(f))["x"]["locked_fields"]
    _ap(f, symbol="x", op=store.OP_UNLOCK, field="stop_loss")
    assert "stop_loss" not in store.replay(store.read_events(f))["x"]["locked_fields"]


def test_retire_and_reinstate(tmp_path):
    f = tmp_path / "e.jsonl"
    _ap(f, symbol="x", op=store.OP_SET, field="intrinsic_low", new_value=10)
    _ap(f, symbol="x", op=store.OP_RETIRE)
    assert store.replay(store.read_events(f))["x"]["status"] == "RETIRED"
    _ap(f, symbol="x", op=store.OP_REINSTATE, new_value="WATCH")
    assert store.replay(store.read_events(f))["x"]["status"] == "WATCH"


def test_multi_symbol_isolation(tmp_path):
    f = tmp_path / "e.jsonl"
    _ap(f, symbol="a", op=store.OP_SET, field="intrinsic_low", new_value=1)
    _ap(f, symbol="b", op=store.OP_SET, field="intrinsic_low", new_value=2)
    state = store.replay(store.read_events(f))
    assert state["a"]["intrinsic_low"] == 1 and state["b"]["intrinsic_low"] == 2
