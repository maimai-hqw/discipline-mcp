# discipline-mcp

Local single-user MCP server: a **protected, schema-strict, auditable** store for
per-symbol investment discipline (intrinsic-value ranges, add/trim/stop/clear
price thresholds, tranches, fundamental hard-triggers, target position, status)
plus a value-investing deep-dive layer (moat, stock type, value-trap flag,
dividend sustainability, catalysts, tracking metrics, conviction, evidence).

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

## Value-investing deep-dive fields
Informational, additive fields (no new hard rules, none locked by default). Set
them via the same `set_rule` / `set_rule_bulk` tools.

| field | type | values / shape |
|---|---|---|
| `stock_type` | enum | `CYCLICAL` `GROWTH` `QUALITY` `VALUE` `VALUE_TRAP` `SPECIAL_SITUATION` `DEFENSIVE` |
| `moat` | str | free text — source(s) of the competitive moat |
| `moat_rating` | enum | `WIDE` `NARROW` `NONE` |
| `normalized_eps` | price | normalized / mid-cycle EPS (元/股, ≥0) |
| `normalized_basis` | str | how normalized_eps was derived |
| `earnings_quality` | str | cash conversion / accruals notes |
| `value_trap` | enum | `YES` `NO` `WATCH` |
| `cheap_reason` | str | why the market prices it cheaply |
| `dividend_yield` | pct | 0..100 |
| `dividend_sustainable` | enum | `YES` `NO` `RISK` |
| `catalysts` | json | `[{event (required), date?, note?}]` |
| `tracking_metrics` | json | `[{metric (required), threshold (required, str), note?}]` |
| `confidence` | enum | `LOW` `MED` `HIGH` |
| `disagreement` | str | bear case / where you might be wrong |
| `evidence` | str | supporting evidence for the thesis |
| `vs_portfolio` | str | role vs the rest of the portfolio |

Enum matching is case-sensitive/exact. For the new enums, setting `None` clears
the field; `status` keeps its existing strictness (no `None`).

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
