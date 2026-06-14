# Read-only web viewer for the discipline ledger

**Date:** 2026-06-14
**Status:** Approved (brainstorming → implementation)

## Goal
Let the user view all disciplines in the database through a web page in the
browser. Today `get_rules()` already returns everything, but as a full-field
dump (a wall of text) that is unusable as an overview for ~32 symbols. This adds
a clean, scannable web view.

## Decisions (locked with the user)
- **Read-only.** The page only displays disciplines + history + chain-integrity
  status. All edits stay on the confirm-gated MCP tools. No write path exists in
  the web layer — it cannot bypass validation, locks, or the hash chain.
- **Always-on, in-process.** The web server boots inside the MCP process (a
  daemon thread) on startup, before `mcp.run()`. No separate command. It is up
  for the lifetime of the MCP session.
- **Overview → detail layout.** A compact sortable table of all symbols; click a
  row to expand an inline detail panel (full deep-dive + that symbol's history).
- **Aesthetics:** clean and 高级 (premium/refined). Minimal palette, generous
  whitespace, tabular numerals, subtle hairlines, tasteful dark-mode support.

## Architecture
New module `src/discipline_mcp/web.py`, stdlib only (`http.server`,
`html`, `json`), no new dependencies.

- `start_web_server()` — called from `server.main()` before `mcp.run()`. If
  disabled via env, logs and returns. Otherwise builds a `ThreadingHTTPServer`
  bound to `127.0.0.1:<port>` and serves in a **daemon thread**. Best-effort:
  `OSError` (port busy) or any exception is logged to **stderr** and the MCP
  keeps running. The web viewer can never take down the discipline server.
- `make_httpd(host, port, db_path=None)` — constructs the server (testable;
  port 0 → ephemeral). The chosen `db_path` is stored on the server instance so
  the handler reads the right ledger (`None` → `store.db_path()` from env).
- `_Handler(BaseHTTPRequestHandler)`:
  - `do_GET`: path `/` → overview page (HTTP 200); other paths → 404.
  - `do_POST/PUT/DELETE/PATCH` → 405 (read-only).
  - `log_message` overridden → stderr logger (never stdout, which is the MCP
    stdio protocol channel).
  - any render exception → 500 with an escaped message; the thread never dies.
- Pure render layer (no sockets, unit-testable):
  - `render_page(events) -> str`: replays the events; on `store.ChainError`
    returns a red **tamper banner** page; otherwise the overview page.
  - helpers: header/chain-status, status badge, the sortable table, the
    per-symbol detail panel, and small-table renderers for the JSON fields
    (`add_tranches`, `trim_tranches`, `hard_triggers`, `catalysts`,
    `tracking_metrics`, `source_docs`).

## Data flow (per request)
`store.read_events(db_path)` → `store.replay()` (runs `verify_chain`) → render
HTML. Reads use the store's existing **shared file lock**, so they are safe
against concurrent MCP writes; each request re-reads fresh (no cached state).
Per-symbol history for the detail panels is filtered from the same `events`
list — one read serves the whole page.

## Layout
- **Header**: title, DB path, event + symbol counts, **chain status** badge
  (✅ 链完整 head=`abcd…` / ❌ 校验失败 + location), generated-at timestamp.
- **Controls**: text filter + status filter (client-side vanilla JS).
- **Overview table**, one row per symbol, sortable:
  `symbol · name · status(badge) · 内在值(low–high) · add≤ · stop · clear ·
  目标% · moat · confidence · updated_at`. Numbers tabular & right-aligned.
- **Detail panel** (server-rendered, hidden via `display:none`; JS toggles):
  grouped sections — 价格纪律 / 基本面 / 触发与催化 (hard_triggers, catalysts,
  tracking_metrics) / 证据链 (source_docs, art_code + short sha256) / 论点
  (rationale, confidence, disagreement, evidence, vs_portfolio) / 🔒 锁定字段 /
  该标的变更历史. Detail is server-rendered (not JS-templated) so escaping is
  centralized and testable; JS stays trivial (toggle + filter + sort). No CDN,
  no build step, works offline.

## Security
- Binds **127.0.0.1 only**; host is not configurable (can't accidentally expose
  on 0.0.0.0).
- **Read-only**: no write handlers; non-GET → 405.
- `html.escape` on **every** dynamic value (rationale/moat/evidence are free
  text → XSS defense even on loopback). `X-Content-Type-Options: nosniff`.
- No auth (single-user localhost), no HTTPS (loopback only) — deliberate.
- A broken hash chain renders a visible tamper banner, not a crash.

## Config (env)
- `DISCIPLINE_MCP_WEB=0` disables the viewer (default **on**; lets headless/cron
  MCP runs opt out).
- `DISCIPLINE_MCP_WEB_PORT` (default **8765**).
- Reuses `DISCIPLINE_MCP_DB` for the ledger path.

## Shared improvement
Extract the field display order (currently a local `keys_order` list inside
`server.py:_fmt_rule`) into `schema.DISPLAY_ORDER`, used by both the `get_rules`
tool and the web viewer — one source of truth so the two never drift. No
behavior change to `get_rules`.

## Testing (pytest, matching existing style)
- **Unit (pure render fns)**: overview lists all symbols/statuses/key numbers;
  a `<script>` in a free-text field is escaped (not raw); a tampered chain →
  tamper banner (not a crash); empty ledger → empty-state page.
- **Integration**: `make_httpd("127.0.0.1", 0, db)` served in a thread;
  `http.client` GET `/` → 200 + expected content; unknown path → 404; POST →
  405. Uses a temp DB.
- **Regression**: existing `get_rules` tests stay green after the
  `DISPLAY_ORDER` extraction.

## After implementation
- **codex review** (GPT-5.5) for correctness/security.
- **Designer-perspective Claude pass** for clean/premium aesthetics; apply.
- Re-run the full suite; verify the server actually serves `/`.

## Out of scope (YAGNI)
No editing from web, no auth, no HTTPS, no charts, no auto-open browser, no
pagination, no separate standalone command.
