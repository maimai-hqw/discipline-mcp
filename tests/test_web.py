"""Read-only web viewer: pure HTML rendering + a live loopback-server check.

The render layer is a pure function of the event list, so most behavior is
tested without binding a socket; one integration test starts the real
ThreadingHTTPServer on an ephemeral port to confirm routing/methods.
"""
import http.client
import threading

import pytest

from discipline_mcp import store, web


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "rules.jsonl"
    monkeypatch.setenv("DISCIPLINE_MCP_DB", str(p))
    return p


def _seed(db):
    """Build a small VALID ledger via the real store; return its events."""
    store.append_event(
        symbol="sh.600483", op=store.OP_BULK_SET,
        new_value={"name": "福能股份", "sector": "电力",
                   "intrinsic_low": 18.0, "intrinsic_high": 25.0,
                   "add_zone_high": 16.0, "target_position_pct": 8.0},
        rationale="seed", path=db)
    store.append_event(
        symbol="sh.600483", op=store.OP_SET, field="stop_loss",
        old_value=None, new_value=12.0, rationale="止损", path=db)
    store.append_event(
        symbol="sz.000651", op=store.OP_SET, field="name",
        old_value=None, new_value="格力电器", rationale="seed", path=db)
    return store.read_events(db)


# ----- pure render layer ---------------------------------------------------
def test_render_empty_is_valid_page_with_empty_state():
    html = web.render_page([])
    assert "<html" in html.lower()
    assert "账本为空" in html


def test_render_lists_every_symbol_and_key_numbers(db):
    html = web.render_page(_seed(db))
    assert "sh.600483" in html
    assert "sz.000651" in html
    assert "福能股份" in html and "格力电器" in html
    assert "WATCH" in html          # default status from replay
    assert "18" in html and "25" in html   # intrinsic range
    assert "12" in html             # stop_loss


def test_free_text_is_html_escaped_not_raw(db):
    _seed(db)
    payload = "<script>alert('XSS_PROBE_42')</script>"
    store.append_event(symbol="sh.600483", op=store.OP_SET, field="moat",
                       old_value=None, new_value=payload,
                       rationale="probe", path=db)
    html = web.render_page(store.read_events(db))
    assert payload not in html               # never rendered raw
    assert "XSS_PROBE_42" in html            # but the value IS shown (escaped)
    assert "&lt;script&gt;" in html


def test_tampered_chain_renders_banner_not_crash(db):
    events = _seed(db)
    events[0] = {**events[0], "rationale": "TAMPERED"}  # breaks the stored hash
    html = web.render_page(events)           # must not raise
    assert "账本校验失败" in html


# ----- env config ----------------------------------------------------------
def test_defaults_enabled_on_port_8765(monkeypatch):
    monkeypatch.delenv("DISCIPLINE_MCP_WEB", raising=False)
    monkeypatch.delenv("DISCIPLINE_MCP_WEB_PORT", raising=False)
    assert web.web_enabled() is True
    assert web.web_port() == 8765


@pytest.mark.parametrize("val,expected", [("0", False), ("false", False),
                                          ("no", False), ("1", True), ("", False)])
def test_web_enabled_env(monkeypatch, val, expected):
    monkeypatch.setenv("DISCIPLINE_MCP_WEB", val)
    assert web.web_enabled() is expected


def test_disabled_start_returns_none(monkeypatch):
    monkeypatch.setenv("DISCIPLINE_MCP_WEB", "0")
    assert web.start_web_server() is None


# ----- integration: a real loopback server ---------------------------------
def _serve(db):
    httpd = web.make_httpd("127.0.0.1", 0, str(db))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def test_corrupt_ledger_renders_tamper_banner_not_500(db):
    _seed(db)
    with open(db, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")   # read_events() raises ChainError early
    httpd, port = _serve(db)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        r = conn.getresponse(); body = r.read().decode("utf-8")
        assert r.status == 200            # not a bare 500
        assert "账本校验失败" in body
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_malformed_content_length_on_write_method(db):
    _seed(db)
    httpd, port = _serve(db)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/", skip_accept_encoding=True)
        conn.putheader("Content-Length", "nope")  # malformed
        conn.endheaders()
        r = conn.getresponse(); r.read()
        assert r.status == 405            # clean rejection, thread survived
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_get_with_body_does_not_desync_keepalive(db):
    _seed(db)
    httpd, port = _serve(db)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/", skip_accept_encoding=True)
        conn.putheader("Content-Length", "4")
        conn.endheaders()
        conn.send(b"junk")               # body the server must drain
        r = conn.getresponse(); r.read()
        assert r.status == 200
        conn.request("GET", "/")         # same connection must stay framed
        r2 = conn.getresponse(); b2 = r2.read().decode("utf-8")
        assert r2.status == 200 and "sh.600483" in b2
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_server_routes_and_methods(db):
    _seed(db)
    httpd = web.make_httpd("127.0.0.1", 0, str(db))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

        conn.request("GET", "/")
        r = conn.getresponse(); body = r.read().decode("utf-8")
        assert r.status == 200
        assert "sh.600483" in body

        conn.request("GET", "/does-not-exist")
        r = conn.getresponse(); r.read()
        assert r.status == 404

        conn.request("POST", "/")
        r = conn.getresponse(); r.read()
        assert r.status == 405          # read-only
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
