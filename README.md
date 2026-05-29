# discipline-mcp

Local single-user MCP server: a **protected, schema-strict, auditable** store for
per-symbol investment discipline (intrinsic-value ranges, add/trim/stop/clear
price thresholds, tranches, fundamental hard-triggers, target position, status).

Event-sourced: every change appends one immutable event to an append-only JSONL
log carrying a **hash chain**; current rules are derived by **replay**. No
database, no git dependency, no network.

## Why
Discipline (intent) is different from a trade ledger (facts) and from analysis
(prose). It must resist careless edits, enforce a strict schema, and stay
auditable. Markdown can be silently rewritten; this server confines writes to a
few validated, confirm-gated tools and makes any after-the-fact tampering
detectable via the hash chain.

## Security posture
- **confirm two-step**: all write tools default `confirm=False` → dry-run preview
  with validation results, nothing written; `confirm=True` commits.
- **cross-field validation**: rejects unsafe writes before they land — e.g. the
  Graham rule `add_zone_high ≤ intrinsic_low` (no buying without a margin of
  safety), `stop_loss < clear_line`, tranche price monotonicity.
- **locked fields** (default `stop_loss`, `clear_line`): require an explicit
  `unlock_rule` first — deliberate friction against quietly moving sell defences.
- **hash chain**: `verify_chain` / `replay` detect any edited or deleted history.

Honest limit: this defends against *mistakes, drift, and after-the-fact
tampering*. It cannot stop a deliberate, in-spec `set_rule(confirm=True)` writing
a bad value — that is what the dry-run preview and validation are for.

## Tools
Read: `get_rules`, `get_rule_field`, `get_rule_history`, `verify_chain`.
Write (confirm-gated): `set_rule`, `set_rule_bulk`, `lock_rule`, `unlock_rule`,
`retire_symbol`.

It does NOT store prices/holdings — combine with the `ashare` (quotes/financials)
and `portfolio` (positions/cost) MCP servers at the conversation layer.

## Run
```
uv sync
uv run discipline-mcp            # stdio
```
Data file: `~/.discipline-mcp/rule_events.jsonl` (override via `DISCIPLINE_MCP_DB`).

Register:
```
claude mcp add discipline -- uv --directory /Volumes/T9/workspace/discipline-mcp run discipline-mcp
```

See `DESIGN.md` for the full schema, validation rules, and rationale.
