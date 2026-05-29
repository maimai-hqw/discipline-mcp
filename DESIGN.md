# discipline-mcp · 设计稿

> 投资纪律的**受保护、严格 schema、可审计**存储与访问层。
> 独立 stdio MCP。append-only JSONL + hash chain。无数据库、无 git 依赖、零运维。
> 设计日:2026-05-28。配套:[[ashare-mcp]](行情/财务)、[[portfolio-mcp]](持仓/成本)。

---

## 1. 定位与边界

**它管什么**:每只标的的**纪律数据**——内在价值区间、加/减/止损/清仓的价格阈值与分批、目标仓位、基本面硬触发、状态。即"我打算怎么做"(意图/计划)。

**它不管什么**:
- 不存行情/财务 → 找 `ashare`;
- 不存持仓/成本/现金 → 找 `portfolio`;
- 不存分析叙述/研报/三方辩论 → 留在 md(自由文本);
- 不做"需要现价的组合层校验"(如"加后不破组合25%")→ 那是对话层(Claude)叠加 ashare+portfolio 现算的。

**一句话**:portfolio 记**已发生的事实**,discipline 记**未发生的意图**,两个独立状态机,独立 DB 文件,独立 MCP。

---

## 2. 存储:append-only JSONL + hash chain

- 单文件 `rule_events.jsonl`,**只追加,不改写**。
- 当前纪律 = **读时重放(replay)** 所有 events,从不单独落地(纯 event-sourcing,和 portfolio 同哲学)。
- 每条 event 带 hash chain:`hash = sha256(canonical_json(本条除hash外字段) + prev_hash)`;`get_rules`/`verify_chain` 时逐条验链,**任何对历史的篡改 → 链断 → 报错**。

### event schema(append-only,每行一条)
```jsonc
{
  "seq": 12,                    // 自增序号(=已有行数+1),验链顺序
  "ts": "2026-05-28T20:31:05",  // server 系统时间(ISO)
  "symbol": "sh.600483",        // 标的;特殊 op 可为空
  "op": "SET",                  // SET|DELETE_FIELD|LOCK|UNLOCK|RETIRE|REINSTATE
  "field": "add_zone_high",     // 改哪个字段(SET/LOCK/UNLOCK 用)
  "old_value": 10.85,           // 改前(便于审计/回滚,replay 不依赖它)
  "new_value": 10.85,
  "rationale": "三方第3轮重估上调内在价值",  // 必填,为什么改
  "actor": "claude",            // claude|user;谁发起
  "confirm": true,              // 必须 true 才写入(见 §5)
  "prev_hash": "a1b2c3...",     // 上一条的 hash;首条 = "genesis"
  "hash": "d4e5f6..."           // 本条 hash
}
```

### rule(派生状态,reduce(events) 得到,不落地)
```jsonc
{
  "symbol": "sh.600483",
  "name": "福能股份",
  "sector": "电力",
  "status": "BUILDING",         // HOLD|BUILDING|TRIMMING|EXITING|WATCH|RETIRED
  // —— 价值锚 ——
  "intrinsic_low": 15.0,
  "intrinsic_high": 21.0,
  "graham_number": 15.49,       // 参考,可空
  // —— 加仓 ——
  "add_zone_high": 11.5,        // 低于此才考虑加(安全边际门槛)
  "add_tranches": [             // 倒金字塔:价越低买越多
    {"price": 11.5, "shares": 1000, "note": "底仓✅已建"},
    {"price": 10.85, "shares": 1000, "note": "第2档"},
    {"price": 10.0, "shares": 1500, "note": "重仓档"}
  ],
  "no_chase_above": 13.5,       // 高于此不追
  // —— 减仓 ——
  "trim_zone_low": 18.0,        // 高于此才减
  "trim_tranches": [{"price": 18.0, "shares": 0, "note": "中枢以上才止盈"}],
  // —— 止损/清仓 ——
  "stop_loss": null,            // 价格止损线
  "clear_line": null,           // 清仓价格线
  // —— 基本面硬触发(与价格无关,任一触发即按 action) ——
  "hard_triggers": [
    {"condition": "2026Q2归母同比<-15% 或 抽蓄项目重大延期", "action": "暂停剩余加仓档,只持底仓观察"}
  ],
  // —— 仓位 ——
  "target_position_pct": 11.0,  // 目标占组合%(总目标)
  "max_position_pct": 15.0,     // 单股上限%
  // —— 元 ——
  "locked_fields": ["stop_loss", "clear_line"],  // 锁定字段(改需先 unlock)
  "rationale": "风电主58%+抽蓄¥255亿在建+核电参股;三方第3轮内在价值¥15-21",
  "updated_at": "2026-05-28T20:31:05"
}
```

---

## 3. 字段清单与单位约定

| 字段 | 类型 | 单位 | 说明 |
|---|---|---|---|
| intrinsic_low / high | float | 元/股 | 内在价值区间(已证实下沿 → 成长上沿) |
| graham_number | float? | 元/股 | √(22.5×EPS×BVPS),参考 |
| add_zone_high | float | 元/股 | 加仓触发上限(价 ≤ 它才考虑加) |
| add_tranches[] | json | — | `{price, shares, note}`;**价格须单调递减** |
| no_chase_above | float? | 元/股 | 涨过此价不追加 |
| trim_zone_low | float | 元/股 | 减仓触发下限(价 ≥ 它才减) |
| trim_tranches[] | json | — | `{price, shares, note}`;**价格须单调递增** |
| stop_loss | float? | 元/股 | 跌破清(可空) |
| clear_line | float? | 元/股 | 涨到此清仓止盈(可空) |
| hard_triggers[] | json | — | `{condition, action}`;基本面触发,与价格无关 |
| target_position_pct | float? | % | 目标占组合 |
| max_position_pct | float? | % | 单股上限 |
| status | enum | — | HOLD/BUILDING/TRIMMING/EXITING/WATCH/RETIRED |
| rationale | str | — | 当前一句话理由(SET 时随 event 更新) |

> **单位约定**:价格阈值一律"元/股";tranche 动作量用"股数"(实操单位);仓位约束用"占组合%"。三者不混。

---

## 4. 校验规则(写入时,pydantic)

**硬校验(违反 → 拒写,返回错误)**:
1. `intrinsic_low ≤ intrinsic_high`
2. **`add_zone_high ≤ intrinsic_low`** ← 格雷厄姆铁律:加仓必须有安全边际(买点不高于内在价值下沿)
3. `clear_line ≥ trim_zone_low`(清仓在减仓区之上)
4. `add_tranches` 价格单调递减;`trim_tranches` 价格单调递增;所有 shares > 0
5. `*_pct` ∈ [0, 100];`max_position_pct ≥ target_position_pct`
6. 目标字段在 `locked_fields` 里 → 拒写(需先 `unlock_rule`)
7. enum 字段值合法

**需现价的校验(可选参数 `current_price`)**:
- 传了 `current_price` → 硬校验:`stop_loss < current_price < clear_line`、新设 `add_zone_high` 应 ≤ current_price(否则是"追高",warn 或按 strict 拒);
- 没传 → 跳过这些,仅返回 warning 提示"未校验现价相关约束"。

**软校验(warning,不拒)**:
- `trim_zone_low < intrinsic_high`(减仓区低于价值上沿——可能过早减,如 ST人福场景,允许但提示);
- `add_zone_high` 距 `intrinsic_low` 折扣 < 15%(安全边际偏薄,提示)。

---

## 5. 工具接口

| 工具 | 读/写 | 签名 | 说明 |
|---|---|---|---|
| `get_rules` | 读 | `(symbol="")` | 当前纪律(重放+验链);空=全部。**默认入口,随便调** |
| `get_rule_field` | 读 | `(symbol, field)` | 单字段当前值 |
| `get_rule_history` | 读 | `(symbol="", field="")` | event 审计流水(升序) |
| `verify_chain` | 读 | `()` | 校验 hash chain 完整性,返回 OK / 在哪条断裂 |
| `set_rule` | **写** | `(symbol, field, value, rationale, confirm=False, current_price=None)` | 改一个字段。见下方 confirm 流程 |
| `set_rule_bulk` | **写** | `(symbol, fields:dict, rationale, confirm=False, current_price=None)` | 一次设一只多字段(初始化用),内部展开成多条 event,**整批校验通过才逐条 append** |
| `lock_rule` | **写** | `(symbol, field, rationale, confirm=False)` | 锁定字段 |
| `unlock_rule` | **写** | `(symbol, field, rationale, confirm=False)` | 解锁 |
| `retire_symbol` | **写** | `(symbol, rationale, confirm=False)` | 标记 RETIRED(如深圳能源),保留历史 |

### confirm 流程(默认只读姿态的实现)
所有**写**工具默认 `confirm=False`:
- `confirm=False` → **dry-run**:执行全部校验,返回"将把 `<symbol>.<field>` 从 `<old>` 改成 `<new>`;校验结果:<pass/warnings/errors>;如无误请用 confirm=True 重发",**不写文件**;
- `confirm=True` → 校验通过 + 字段未锁定 → append 一条带 hash 的 event。

→ 效果:**改纪律永远是"先看一眼 diff 再确认"的两步动作**,我(Claude)不可能一步顺手改掉;`rationale` 必填,逼出理由;全部留痕。

---

## 6. 重放(replay)逻辑

```python
def load_state():
    events = [json.loads(l) for l in open(JSONL)]
    verify_chain(events)                 # 验链:逐条重算 hash + prev 连续;断裂→raise
    state = {}                           # symbol -> rule dict
    for e in sorted(events, key=lambda x: x["seq"]):
        s = state.setdefault(e["symbol"], {"symbol": e["symbol"], "locked_fields": []})
        if   e["op"] == "SET":          s[e["field"]] = e["new_value"]; s["rationale"]=e["rationale"]
        elif e["op"] == "DELETE_FIELD": s.pop(e["field"], None)
        elif e["op"] == "LOCK":         s["locked_fields"].append(e["field"])
        elif e["op"] == "UNLOCK":       s["locked_fields"].remove(e["field"])
        elif e["op"] == "RETIRE":       s["status"] = "RETIRED"
        elif e["op"] == "REINSTATE":    s["status"] = e["new_value"]
        s["updated_at"] = e["ts"]
    return state
```
数据量(几百行)→ 每次 replay 微秒级,无需缓存/物化。

---

## 7. 技术栈与项目结构(镜像 ashare/portfolio)

- Python ≥3.10;`mcp[cli]`;`pydantic`(校验);标准库 `hashlib`/`json`/`fcntl`。**无 DB、无 git、无网络依赖**。
- stdio transport;工具调用用 `asyncio.Lock` 串行化(同 portfolio),server 内无并发。
- append 写:`open(f,'a')` 单行;可选 `fcntl.flock` 加固。
- 数据文件:`~/.discipline-mcp/rule_events.jsonl`,env `DISCIPLINE_MCP_DB` 覆盖;`~/.discipline-mcp` symlink 到 `/Volumes/T9/.discipline-mcp`(仿 portfolio,数据落 T9)。
```
discipline-mcp/
  pyproject.toml          # ashare-mcp = "discipline_mcp.server:main"
  src/discipline_mcp/
    server.py             # FastMCP + 工具注册
    store.py              # JSONL append + hash chain + replay
    schema.py             # pydantic models + 校验规则
    __init__.py
  tests/
    test_chain.py         # hash chain 验证/篡改检测
    test_validation.py    # 各校验规则
    test_replay.py        # 重放正确性
  DESIGN.md
```
- 注册:`claude mcp add discipline -- uv --directory /Volumes/T9/workspace/discipline-mcp run discipline-mcp`

---

## 8. security 总结(三层,各管一段)

| 层 | 机制 | 防什么 |
|---|---|---|
| 写入时 | `confirm` 两步 + pydantic 校验 + `locked_fields` | 防"误写/不合规写/顺手改锁定项" |
| 事后 | hash chain(replay 验链) | 防"偷偷改历史"(改了链断,报错暴露) |
| 约定 | 只通过 MCP 工具访问,不用 Edit/bash 碰 .jsonl | 这是纪律,非技术强制(但违规会被验链/文件mtime发现) |

**诚实边界**:挡不住"现在故意调 set_rule(confirm=True) 写坏值"——那靠校验+你看 dry-run 时把关。技术防的是"误改/漂移/事后篡改",不是"故意的合规写入"。

---

## 9. 迁移与未来

- **首次迁移**:把作战手册 md 里现有 6 只(紫光/乖宝/中宠/ST人福/福能 + 观察:国投/依依/深圳能源RETIRED)的纪律,用 `set_rule_bulk` 逐只灌入 → 生成初始 events。
- **md 的去留**:结构化纪律迁入后,作战手册 md 退化为"叙述层"(为什么/三方结论),阈值以 discipline-mcp 为唯一真相;速查表可由 `get_rules` 动态生成。
- **未来可选**:`cron` 定期 `git commit` 那个 jsonl 做异地备份(非机制核心);`diff_rules(symbol, since_ts)` 看某只纪律演变。

---

## 待你拍板的决策点(默认值已给,认可即按此实现)

1. **tranche 动作量单位** → 默认"股数"(实操单位)。是否改用"占预算%"?(默认:股数)
2. **现价校验** → `set_rule` 接受可选 `current_price`,传了硬校验 stop_loss/追高,不传跳过+warn。(默认:可选)
3. **默认锁定哪些字段** → 默认锁 `stop_loss` + `clear_line`(最关键的两条防线),其余不锁。(默认:锁这两个)
4. **数据文件位置** → `~/.discipline-mcp/` symlink 到 T9(仿 portfolio)。是否直接放项目目录?(默认:仿 portfolio)
5. **repo** → 在 `/Volumes/T9/workspace/discipline-mcp/` init,push 到你的 GitHub(类似 ashare/portfolio)?(默认:是)
