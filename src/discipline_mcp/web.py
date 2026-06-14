"""Read-only web viewer for the discipline ledger (stdlib http.server).

Boots in a daemon thread from ``server.main()`` before ``mcp.run()`` and serves
a single self-contained HTML page (overview table + click-to-expand detail) on
127.0.0.1. It is strictly READ-ONLY: GET only, no write path, so all edits keep
going through the confirm-gated MCP tools and cannot bypass validation, locks,
or the hash chain.

Safety:
  * binds loopback only (127.0.0.1) — host is not configurable;
  * GET only; other methods -> 405; unknown path -> 404;
  * every dynamic value is HTML-escaped (free-text fields -> no XSS);
  * a broken hash chain renders a red tamper banner instead of data;
  * best-effort: any startup error is logged to stderr and the MCP keeps running;
  * all logging -> stderr (stdout is the MCP stdio protocol channel).

Reads go through ``store.read_events`` (shared file lock), so they are safe
against concurrent MCP writes; every request re-reads the ledger fresh.
"""
from __future__ import annotations

import html
import json
import logging
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import schema, store

logger = logging.getLogger("discipline-mcp.web")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


# --------------------------------------------------------------------- #
# config (env)
# --------------------------------------------------------------------- #
def web_enabled() -> bool:
    """Default ON; disable with DISCIPLINE_MCP_WEB in {0,false,no,off,''}."""
    v = os.environ.get("DISCIPLINE_MCP_WEB")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def web_port() -> int:
    try:
        return int(os.environ.get("DISCIPLINE_MCP_WEB_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


# --------------------------------------------------------------------- #
# tiny escaping / formatting helpers
# --------------------------------------------------------------------- #
def _esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _cell(x) -> str:
    """Format one table cell value: numbers compact, everything else escaped."""
    if isinstance(x, bool):
        return _esc(str(x))
    if isinstance(x, (int, float)):
        return f"{x:g}"
    return _esc("" if x is None else str(x))


def _num(rule, field):
    v = rule.get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _present(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def _is_json_field(f) -> bool:
    return str(schema.FIELD_TYPES.get(f, "")).startswith("json")


# --------------------------------------------------------------------- #
# field labels + detail-panel grouping
# --------------------------------------------------------------------- #
LABELS = {
    "name": "名称", "sector": "行业", "status": "状态", "rationale": "投资逻辑",
    "intrinsic_low": "内在价值下沿", "intrinsic_high": "内在价值上沿",
    "graham_number": "格雷厄姆数",
    "add_zone_high": "加仓上限", "add_tranches": "加仓批次", "no_chase_above": "追高禁线",
    "trim_zone_low": "减仓下限", "trim_tranches": "减仓批次",
    "stop_loss": "止损线", "clear_line": "清仓线",
    "hard_triggers": "硬触发", "target_position_pct": "目标仓位",
    "max_position_pct": "最大仓位",
    "stock_type": "类型", "moat": "护城河", "moat_rating": "护城河评级",
    "normalized_eps": "正常化 EPS", "normalized_basis": "正常化依据",
    "earnings_quality": "盈利质量",
    "value_trap": "价值陷阱", "cheap_reason": "便宜原因",
    "dividend_yield": "股息率", "dividend_sustainable": "股息可持续",
    "catalysts": "催化剂", "tracking_metrics": "跟踪指标",
    "confidence": "信心", "disagreement": "反方观点", "evidence": "证据",
    "vs_portfolio": "组合定位", "source_docs": "证据链",
    "locked_fields": "锁定字段", "updated_at": "更新时间",
}

SECTIONS = [
    ("价格纪律", ["intrinsic_low", "intrinsic_high", "graham_number",
                  "add_zone_high", "add_tranches", "no_chase_above",
                  "trim_zone_low", "trim_tranches", "stop_loss", "clear_line",
                  "target_position_pct", "max_position_pct"]),
    ("基本面", ["stock_type", "moat", "moat_rating",
                "normalized_eps", "normalized_basis", "earnings_quality",
                "value_trap", "cheap_reason", "dividend_yield",
                "dividend_sustainable"]),
    ("触发与催化", ["hard_triggers", "catalysts", "tracking_metrics"]),
    ("证据链", ["source_docs"]),
    ("论点", ["rationale", "disagreement", "evidence", "vs_portfolio",
              "confidence"]),
]

STATUS_ORDER = ["HOLD", "BUILDING", "TRIMMING", "EXITING", "WATCH", "RETIRED"]


# --------------------------------------------------------------------- #
# render: small pieces
# --------------------------------------------------------------------- #
def _badge(status) -> str:
    s = status or "WATCH"
    return '<span class="badge st-%s">%s</span>' % (_esc(s), _esc(s))


def _scalar(f, v) -> str:
    t = schema.FIELD_TYPES.get(f)
    if isinstance(v, bool):
        return _esc(str(v))
    if isinstance(v, (int, float)):
        s = f"{v:g}"
        return s + "%" if t == "pct" else s
    return _esc(str(v))


def _mini(headers, rows) -> str:
    head = "".join("<th>%s</th>" % _esc(h) for h in headers)
    body = "".join(
        "<tr>" + "".join("<td>%s</td>" % _cell(c) for c in r) + "</tr>"
        for r in rows
    )
    return ('<table class="mini"><thead><tr>%s</tr></thead><tbody>%s</tbody>'
            '</table>' % (head, body))


def _source_docs_table(items) -> str:
    head = "".join("<th>%s</th>" % _esc(h)
                   for h in ["凭证", "art_code", "sha256", "备注"])
    body = ""
    for it in items:
        sha = str(it.get("sha256") or "")
        if sha:
            sha_cell = ('<span class="mono" title="%s">%s…</span>'
                        % (_esc(sha), _esc(sha[:10])))
        else:
            sha_cell = '<span class="muted">—</span>'
        cells = [
            "<td>%s</td>" % _cell(it.get("doc", "")),
            "<td>%s</td>" % _cell(it.get("art_code") or "—"),
            "<td>%s</td>" % sha_cell,
            "<td>%s</td>" % _cell(it.get("note", "")),
        ]
        body += "<tr>" + "".join(cells) + "</tr>"
    return ('<table class="mini"><thead><tr>%s</tr></thead><tbody>%s</tbody>'
            '</table>' % (head, body))


def _json_block(f, v) -> str:
    label = _esc(LABELS.get(f, f))
    if f in ("add_tranches", "trim_tranches"):
        tbl = _mini(["价格", "数量", "备注"],
                    [[i.get("price"), i.get("shares"), i.get("note", "")]
                     for i in v])
    elif f == "hard_triggers":
        tbl = _mini(["条件", "动作", "备注"],
                    [[i.get("condition", ""), i.get("action", ""),
                      i.get("note", "")] for i in v])
    elif f == "catalysts":
        tbl = _mini(["事件", "日期", "备注"],
                    [[i.get("event", ""), i.get("date", ""),
                      i.get("note", "")] for i in v])
    elif f == "tracking_metrics":
        tbl = _mini(["指标", "阈值", "备注"],
                    [[i.get("metric", ""), i.get("threshold", ""),
                      i.get("note", "")] for i in v])
    elif f == "source_docs":
        tbl = _source_docs_table(v)
    else:
        tbl = "<pre>%s</pre>" % _esc(json.dumps(v, ensure_ascii=False))
    return '<div class="subblock"><h4>%s</h4>%s</div>' % (label, tbl)


def _section(title, fields, rule) -> str:
    scalars, blocks = [], []
    for f in fields:
        v = rule.get(f)
        if not _present(v):
            continue
        if _is_json_field(f):
            blocks.append(_json_block(f, v))
        else:
            scalars.append("<dt>%s</dt><dd>%s</dd>"
                           % (_esc(LABELS.get(f, f)), _scalar(f, v)))
    if not scalars and not blocks:
        return ""
    out = ['<section class="grp"><h3>%s</h3>' % _esc(title)]
    if scalars:
        out.append('<dl class="kv">' + "".join(scalars) + "</dl>")
    out += blocks
    out.append("</section>")
    return "".join(out)


def _history_section(events) -> str:
    head = "".join("<th>%s</th>" % _esc(h)
                   for h in ["#", "时间", "操作", "字段", "变更", "发起者", "理由"])
    rows = ""
    for e in events:
        op = e.get("op")
        if op == "SET":
            chg = "%s → %s" % (_cell(e.get("old_value")), _cell(e.get("new_value")))
        elif op == "BULK_SET":
            chg = _esc(", ".join((e.get("new_value") or {}).keys()))
        elif op in ("LOCK", "UNLOCK"):
            chg = _esc(e.get("field") or "")
        elif op in ("RETIRE", "REINSTATE"):
            chg = _esc(str(e.get("new_value") or "RETIRED"))
        else:
            chg = ""
        cells = [
            "<td class='mono'>#%s</td>" % _cell(e.get("seq")),
            "<td class='mono'>%s</td>" % _cell(e.get("ts")),
            "<td>%s</td>" % _esc(op or ""),
            "<td>%s</td>" % _esc(e.get("field") or "—"),
            "<td>%s</td>" % chg,
            "<td>%s</td>" % _esc(e.get("actor") or ""),
            "<td>%s</td>" % _esc(e.get("rationale") or ""),
        ]
        rows += "<tr>" + "".join(cells) + "</tr>"
    return ('<section class="grp"><h3>变更历史</h3>'
            '<table class="mini hist"><thead><tr>%s</tr></thead><tbody>%s'
            '</tbody></table></section>' % (head, rows))


def _detail(rule, history) -> str:
    parts = [_section(t, fs, rule) for t, fs in SECTIONS]
    locked = rule.get("locked_fields") or []
    if locked:
        chips = "".join('<span class="lock">🔒 %s</span>' % _esc(LABELS.get(f, f))
                        for f in locked)
        parts.append('<section class="grp"><h3>锁定字段</h3>'
                     '<div class="locks">%s</div></section>' % chips)
    parts.append(_history_section(history))
    return '<div class="detail">%s</div>' % "".join(p for p in parts if p)


# --------------------------------------------------------------------- #
# render: overview rows + page
# --------------------------------------------------------------------- #
def _row(sym, rule, history) -> str:
    name = rule.get("name") or ""
    sector = rule.get("sector") or ""
    status = rule.get("status") or "WATCH"
    lo, hi = _num(rule, "intrinsic_low"), _num(rule, "intrinsic_high")
    if lo is not None and hi is not None:
        intr = f"{lo:g}–{hi:g}"
    elif lo is not None:
        intr = f"≥{lo:g}"
    elif hi is not None:
        intr = f"≤{hi:g}"
    else:
        intr = "—"
    add = _num(rule, "add_zone_high")
    stop = _num(rule, "stop_loss")
    clear = _num(rule, "clear_line")
    tgt = _num(rule, "target_position_pct")
    moat = rule.get("moat_rating") or ""
    conf = rule.get("confidence") or ""
    upd = rule.get("updated_at") or ""
    search = " ".join([sym, name, sector]).lower()

    data = ('data-symbol="%s" data-search="%s" data-status="%s" '
            'data-intrinsic="%s" data-stop="%s" data-clear="%s" '
            'data-target="%s" data-updated="%s"' % (
                _esc(sym), _esc(search), _esc(status),
                "" if lo is None else lo,
                "" if stop is None else stop,
                "" if clear is None else clear,
                "" if tgt is None else tgt,
                _esc(upd)))
    dash = '<span class="muted">—</span>'
    cells = [
        '<td class="sym">%s</td>' % _esc(sym),
        '<td>%s</td>' % (_esc(name) if name else dash),
        '<td>%s</td>' % _badge(status),
        '<td class="num">%s</td>' % intr,
        '<td class="num">%s</td>' % ("—" if add is None else f"{add:g}"),
        '<td class="num">%s</td>' % ("—" if stop is None else f"{stop:g}"),
        '<td class="num">%s</td>' % ("—" if clear is None else f"{clear:g}"),
        '<td class="num">%s</td>' % ("—" if tgt is None else f"{tgt:g}%"),
        '<td>%s</td>' % (_esc(moat) if moat else dash),
        '<td>%s</td>' % (_esc(conf) if conf else dash),
        '<td class="muted nowrap mono">%s</td>' % _esc(upd),
    ]
    retired = " retired" if status == "RETIRED" else ""
    return ('<tbody class="sym%s" %s><tr class="main">%s</tr>'
            '<tr class="detail-row"><td colspan="11">%s</td></tr></tbody>'
            % (retired, data, "".join(cells), _detail(rule, history)))


def _history_by_symbol(events):
    out = {}
    for e in events:
        out.setdefault(e.get("symbol"), []).append(e)
    return out


def _overview_html(state, events, db_path) -> str:
    n_sym, n_ev = len(state), len(events)
    head = store.last_hash(events)
    head12 = _esc(head[:12]) if head and head != store.GENESIS else "genesis"
    dbp = _esc(str(db_path if db_path is not None else store.db_path()))
    gen = _esc(datetime.now().strftime("%Y-%m-%d %H:%M"))
    header = (
        '<header class="top"><div>'
        '<h1>纪律账本<span class="en">Discipline Ledger</span></h1>'
        '<div class="subtitle mono">%s</div></div>'
        '<div class="meta">'
        '<span>%d 标的 · %d 事件</span>'
        '<span class="badge chain-ok">● 链完整 · %s</span>'
        '<span class="muted">%s</span>'
        '</div></header>' % (dbp, n_sym, n_ev, head12, gen)
    )
    if not state:
        return (header + '<div class="empty">账本为空 · 用 set_rule / '
                'set_rule_bulk 录入纪律</div>')

    hist = _history_by_symbol(events)
    opts = "".join("<option>%s</option>" % s for s in STATUS_ORDER)
    controls = (
        '<div class="controls">'
        '<input id="filter" class="filter" type="search" '
        'placeholder="筛选 代码 / 名称 / 行业…" autocomplete="off">'
        '<select id="statusFilter"><option value="">全部状态</option>%s</select>'
        '<span id="count" class="muted"></span>'
        '</div>' % opts
    )
    thead = (
        '<thead><tr>'
        '<th data-key="symbol" data-type="str">代码</th>'
        '<th>名称</th>'
        '<th data-key="status" data-type="str">状态</th>'
        '<th class="num" data-key="intrinsic" data-type="num">内在价值</th>'
        '<th class="num">加仓≤</th>'
        '<th class="num" data-key="stop" data-type="num">止损</th>'
        '<th class="num" data-key="clear" data-type="num">清仓</th>'
        '<th class="num" data-key="target" data-type="num">目标仓位</th>'
        '<th>护城河</th>'
        '<th>信心</th>'
        '<th data-key="updated" data-type="str">更新</th>'
        '</tr></thead>'
    )
    bodies = "".join(_row(s, state[s], hist.get(s, []))
                     for s in sorted(state))
    return (header + controls + '<table id="rules">' + thead + bodies
            + "</table>")


def _tamper_html(err) -> str:
    return (
        '<header class="top"><div>'
        '<h1>纪律账本<span class="en">Discipline Ledger</span></h1></div></header>'
        '<div class="tamper"><h2>⚠ 账本校验失败</h2>'
        '<p>哈希链断裂或被篡改,已停止展示纪律数据以免呈现不可信内容。'
        '请用 verify_chain 排查,并从备份恢复。</p>'
        '<pre>%s</pre></div>' % _esc(str(err))
    )


def render_page(events, db_path=None) -> str:
    """Full HTML page from an event list. Never raises on a broken chain —
    a ChainError renders a tamper banner instead of data."""
    try:
        state = store.replay(events)
    except store.ChainError as e:
        return _document(_tamper_html(e))
    return _document(_overview_html(state, events, db_path))


def _document(inner, title="纪律账本") -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + _esc(title) + '</title><style>' + _CSS + '</style></head>'
        '<body><div class="wrap">' + inner + '</div>'
        '<script>' + _JS + '</script></body></html>'
    )


# --------------------------------------------------------------------- #
# HTTP server (read-only)
# --------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    db_path = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "discipline-mcp-web"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # -> stderr, never stdout
        logger.info("web %s %s", self.address_string(), fmt % args)

    def _drain(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path != "/":
            self._send(404, _document('<div class="empty">404 · 未找到</div>'))
            return
        try:
            db = getattr(self.server, "db_path", None)
            events = store.read_events(db)
            self._send(200, render_page(events, db))
        except Exception as e:  # never let the request thread die
            logger.exception("web render failed")
            self._send(500, _document(
                '<div class="tamper"><h2>500</h2><pre>%s</pre></div>'
                % _esc(str(e))))

    def _reject(self):
        self._drain()
        self._send(405, _document(
            '<div class="empty">405 · 只读视图,不支持写操作</div>'))

    do_POST = do_PUT = do_DELETE = do_PATCH = _reject


def make_httpd(host=HOST, port=DEFAULT_PORT, db_path=None) -> _Server:
    """Build (but do not start) the loopback server. port=0 -> ephemeral."""
    httpd = _Server((host, port), _Handler)
    httpd.db_path = db_path
    return httpd


def start_web_server():
    """Best-effort: start the read-only viewer in a daemon thread. Returns the
    server (or None if disabled / port busy). Never raises — the MCP must keep
    running even if the viewer can't bind."""
    if not web_enabled():
        logger.info("web viewer disabled (DISCIPLINE_MCP_WEB)")
        return None
    port = web_port()
    try:
        httpd = make_httpd(HOST, port)
    except OSError as e:
        logger.warning("web viewer not started (port %s in use?): %s", port, e)
        return None
    except Exception:
        logger.exception("web viewer failed to start")
        return None
    threading.Thread(target=httpd.serve_forever, name="discipline-web",
                     daemon=True).start()
    logger.info("web viewer on http://%s:%s (read-only)", HOST, port)
    return httpd


# --------------------------------------------------------------------- #
# assets (kept as plain strings so braces don't collide with f-strings)
# --------------------------------------------------------------------- #
_CSS = """
:root{
  --bg:#fbfbfa; --panel:#fff; --ink:#18181b; --muted:#71717a;
  --line:#ededee; --line-strong:#e2e2e4; --accent:#0f766e; --accent-soft:#0f766e14;
  --hover:#0000000a; --shadow:0 1px 2px #0000000a,0 10px 30px #0000000a;
  --radius:14px;
  --mono:"SF Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0a0a0b; --panel:#141416; --ink:#ededed; --muted:#8e8e98;
  --line:#212124; --line-strong:#2b2b30; --accent:#2dd4bf; --accent-soft:#2dd4bf18;
  --hover:#ffffff08; --shadow:0 1px 2px #0006,0 14px 36px #0007;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.mono{font-family:var(--mono)}
.muted{color:var(--muted)}
.nowrap{white-space:nowrap}
.wrap{max-width:1200px;margin:0 auto;padding:52px 30px 120px}

header.top{display:flex;align-items:flex-end;justify-content:space-between;
  gap:24px;margin-bottom:32px}
h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-.012em;
  display:flex;align-items:baseline}
h1 .en{font-weight:400;font-size:12px;letter-spacing:.04em;color:var(--muted);
  margin-left:10px;text-transform:uppercase}
.subtitle{margin-top:7px;font-size:12px;color:var(--muted);word-break:break-all}
.meta{display:flex;flex-direction:column;align-items:flex-end;gap:7px;
  font-size:12px;color:var(--muted)}

.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
  border-radius:999px;font-size:11.5px;font-weight:500;line-height:1.4;
  font-variant-numeric:tabular-nums}
.chain-ok{color:var(--accent);background:var(--accent-soft)}
.st-HOLD{color:#0f766e;background:#0f766e14}
.st-BUILDING{color:#1d4ed8;background:#1d4ed814}
.st-TRIMMING{color:#b45309;background:#b4530914}
.st-EXITING{color:#c2410c;background:#c2410c14}
.st-WATCH{color:#6b7280;background:#6b728014}
.st-RETIRED{color:#9ca3af;background:#9ca3af14}
@media (prefers-color-scheme:dark){
  .st-HOLD{color:#2dd4bf;background:#2dd4bf18}
  .st-BUILDING{color:#7aa2ff;background:#7aa2ff18}
  .st-TRIMMING{color:#e0a85a;background:#e0a85a18}
  .st-EXITING{color:#f0915e;background:#f0915e18}
  .st-WATCH{color:#9aa0aa;background:#9aa0aa18}
  .st-RETIRED{color:#7c7c85;background:#7c7c8518}
}

.controls{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.filter,select{appearance:none;font:inherit;font-size:13px;color:var(--ink);
  background:var(--panel);border:1px solid var(--line-strong);border-radius:9px;
  padding:8px 12px;outline:none;transition:border-color .15s,box-shadow .15s}
.filter{flex:1;min-width:0}
.filter:focus,select:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
.filter::placeholder{color:var(--muted)}
#count{margin-left:auto;font-size:12px}

table#rules{width:100%;border-collapse:collapse;font-size:13px}
table#rules thead th{position:sticky;top:0;z-index:1;background:var(--bg);
  text-align:left;font-weight:500;color:var(--muted);font-size:11px;
  letter-spacing:.05em;text-transform:uppercase;padding:0 14px 11px;
  border-bottom:1px solid var(--line-strong);user-select:none;white-space:nowrap}
th[data-key]{cursor:pointer}
th[data-key]:hover{color:var(--ink)}
th.sorted-asc::after{content:" ↑";color:var(--accent)}
th.sorted-desc::after{content:" ↓";color:var(--accent)}
.num{text-align:right;font-variant-numeric:tabular-nums}
td.num{font-family:var(--mono);font-size:12.5px}

tbody.sym>tr.main>td{padding:13px 14px;border-bottom:1px solid var(--line);
  vertical-align:middle}
tbody.sym>tr.main{cursor:pointer;transition:background .12s}
tbody.sym:hover>tr.main>td{background:var(--hover)}
tbody.sym.open>tr.main>td{background:var(--hover);border-bottom-color:transparent}
td.sym{font-family:var(--mono);font-size:12.5px;letter-spacing:-.01em}
tbody.sym.retired td.sym{text-decoration:line-through;color:var(--muted)}

tr.detail-row{display:none}
tbody.sym.open tr.detail-row{display:table-row}
tr.detail-row>td{padding:2px 14px 20px}
.detail{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow);padding:24px 26px}

.grp{margin-bottom:22px}
.grp:last-child{margin-bottom:0}
.grp>h3{margin:0 0 13px;padding-bottom:9px;font-size:11px;font-weight:600;
  letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
  border-bottom:1px solid var(--line)}
dl.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);
  gap:9px 26px;margin:0;align-items:baseline}
dl.kv dt{color:var(--muted);font-size:12.5px}
dl.kv dd{margin:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.subblock{margin-top:14px}
.subblock>h4{margin:0 0 6px;font-size:12px;font-weight:500;color:var(--muted)}

table.mini{width:100%;border-collapse:collapse;font-size:12.5px}
table.mini th{text-align:left;font-weight:500;color:var(--muted);font-size:10.5px;
  text-transform:uppercase;letter-spacing:.04em;padding:5px 11px;
  border-bottom:1px solid var(--line)}
table.mini td{padding:6px 11px;border-bottom:1px solid var(--line);
  overflow-wrap:anywhere;vertical-align:top}
table.mini tbody tr:last-child td{border-bottom:none}
table.mini.hist td:first-child,table.mini.hist td:nth-child(2){white-space:nowrap}

.locks{display:flex;flex-wrap:wrap;gap:8px}
.lock{font-size:12px;color:var(--muted);background:var(--hover);
  border:1px solid var(--line);border-radius:8px;padding:3px 10px}

.empty{text-align:center;color:var(--muted);padding:96px 0;font-size:13.5px}
.tamper{border:1px solid #dc262644;background:#dc26260d;border-radius:var(--radius);
  padding:24px 26px;color:#b91c1c}
.tamper h2{margin:0 0 10px;font-size:16px}
.tamper p{margin:0 0 14px;color:var(--ink)}
.tamper pre{margin:0;white-space:pre-wrap;font-family:var(--mono);font-size:12.5px;
  color:var(--ink)}
"""

_JS = """
(function(){
  var tbl=document.getElementById('rules');
  if(!tbl)return;
  function bodies(){return Array.prototype.slice.call(
    tbl.querySelectorAll('tbody.sym'));}
  var f=document.getElementById('filter');
  var sf=document.getElementById('statusFilter');
  var counter=document.getElementById('count');
  function apply(){
    var q=(f&&f.value||'').trim().toLowerCase();
    var st=sf&&sf.value||'';
    var shown=0,all=bodies();
    all.forEach(function(b){
      var okT=!q||(b.getAttribute('data-search')||'').indexOf(q)>=0;
      var okS=!st||b.getAttribute('data-status')===st;
      var vis=okT&&okS;
      b.style.display=vis?'':'none';
      if(vis)shown++;
    });
    if(counter)counter.textContent=shown+' / '+all.length;
  }
  if(f)f.addEventListener('input',apply);
  if(sf)sf.addEventListener('change',apply);
  apply();
  tbl.addEventListener('click',function(e){
    var tr=e.target.closest&&e.target.closest('tr.main');
    if(!tr)return;
    tr.parentNode.classList.toggle('open');
  });
  var ths=Array.prototype.slice.call(tbl.querySelectorAll('th[data-key]'));
  var cur={key:null,dir:1};
  ths.forEach(function(h){
    h.addEventListener('click',function(){
      var key=h.getAttribute('data-key'),type=h.getAttribute('data-type');
      cur.dir=(cur.key===key)?-cur.dir:1; cur.key=key;
      var rows=bodies();
      rows.sort(function(a,b){
        var av=a.getAttribute('data-'+key)||'',bv=b.getAttribute('data-'+key)||'';
        if(type==='num'){
          var an=parseFloat(av),bn=parseFloat(bv),ae=isNaN(an),be=isNaN(bn);
          if(ae&&be)return 0; if(ae)return 1; if(be)return -1;
          return (an-bn)*cur.dir;
        }
        return av.localeCompare(bv)*cur.dir;
      });
      rows.forEach(function(r){tbl.appendChild(r);});
      ths.forEach(function(x){x.classList.remove('sorted-asc','sorted-desc');});
      h.classList.add(cur.dir>0?'sorted-asc':'sorted-desc');
    });
  });
})();
"""
