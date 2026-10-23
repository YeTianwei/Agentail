# Token 消耗统计调研

调研日期:**2026-10-23**。本机 Claude Code 2.1.309、codex-cli 0.164.0。标注同 `usage-notes.md`:**[文档]**、**[实测]**、**[推断]**。

目标:按对话、按天、按周统计 token 消耗。和 0.0.2 的「额度」(百分比)是两件事:额度是服务端给的账号级读数,token 是本地会话文件里的逐次响应计数。

---

## 1. 结论

| agent | 来源 | 粒度 | 可信度 |
|---|---|---|---|
| Claude Code | 会话 transcript `~/.claude/projects/<项目>/<session>.jsonl`,`type == "assistant"` 行的 `message.usage` | 每次 API 响应 | [实测] |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl[.zst]` 的 `token_usage_record` 行 | 每次响应,带 `response_id` | [实测] |
| Qoder | `~/.qoder/logs/sessions/**/segments/*.jsonl` 有 `usage` 字段,但抽样全是 0 | — | [实测],不可用 |
| Qwen Code / Gemini CLI | 未调研 | — | — |

hook 载荷里没有 token 计数;Claude 的 statusLine 有 `cost.total_cost_usd`(会话累计),但 `context_window.*` 是**当前上下文**而不是累计消耗 [文档](<https://code.claude.com/docs/en/statusline>),且 statusLine 在 VS Code / Desktop 里不运行。所以只能读会话文件。

和额度功能一样**不碰凭据**,也不联网。

---

## 2. Claude Code [实测]

### 2.1 字段

每个 `assistant` 行的 `message.usage`:

```json
{"input_tokens": 2, "cache_creation_input_tokens": 25759, "cache_read_input_tokens": 41811,
 "output_tokens": 526, "output_tokens_details": {"thinking_tokens": 122},
 "cache_creation": {"ephemeral_1h_input_tokens": 25759, "ephemeral_5m_input_tokens": 0},
 "service_tier": "standard", "speed": "standard", ...}
```

同一行还有 `message.id`、`requestId`、`message.model`、`sessionId`、`timestamp`(UTC ISO)、`cwd`、`isSidechain`。`input_tokens` **不含**缓存。

### 2.2 去重(必须做)

- 一次响应的多个 content block 各占一行,`usage` 重复出现:本机 8867 行只有 4263 次响应。按 `(message.id, requestId)` 去重。
- 其中 45 次响应的各行 `output_tokens` 不同(流式中途的值):取最大值。
- 29 个 `message.id` 出现在不止一个文件里(resume / fork 会把历史复制进新文件):去重必须跨文件。
- 子代理在 `<项目>/<session>/subagents/*.jsonl`,`sessionId` 是父会话(3051/3051 行),所以直接计入父对话。
- 文件里另有 `type == "cost-state"` 行:Claude Code 自己算的会话累计 `totalCostUSD` 和按模型的 `modelUsage`(含 `costUSD`)。不是每个会话都有,无时间分布,只能作参考。

### 2.3 保留期 [文档]

<https://code.claude.com/docs/en/data-usage>(2026-10-23 阅读):transcript 默认在 `~/.claude/projects/` 保留 **30 天**,用 `cleanupPeriodDays` 调整;在 Claude Desktop 或 Cowork 里开始或最近继续过的会话默认不受这个限制。要看「上个月」就必须由 Agentail 自己存记录,不能每次现算。

---

## 3. Codex [实测]

- 新版本每次响应写一行 `token_usage_record`,`payload` 有 `response_id`(去重键)、`thread_id` / `session_id`、`turn_id`、`usage`(`input_tokens`、`cached_input_tokens`、`cache_write_input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens`),另有 `turn_token_usage`、`thread_token_usage` 累计值。模型在 `turn_context.model`。
- Codex 的 `input_tokens` **包含** `cached_input_tokens`,和 Claude 口径不同,入库前要换算成同一口径。
- 旧文件会被压缩成 `.jsonl.zst`(本机 42 个文件里 30 个,10-08 及以前的都是),`~/.codex/archived_sessions/` 下也是 `.zst`。Python 3.12 标准库没有 zstd:可调用系统 `zstd -dc`(本机有),或只统计开始监控之后的新文件,旧的作一次性回填。
- 更早的版本只有 `token_count`(`info.total_token_usage` 是累计值,`info.last_token_usage` 是这一次)。本机文件按 `session_meta.cli_version` 统计:0.147–0.150 只有 `token_count`,**0.153 起**两者都有,数量基本一一对应。

---

## 4. 本机样本 [实测]

全量扫描(不做增量):Claude 212 MB 用 0.3 秒(先按子串 `"usage"` 过滤再解析),Codex 93 MB(含解压)0.6 秒。增量读取后开销可以忽略。

2026-10-23 一天的 Claude:fresh input 3 千、cache write 946 万、**cache read 3.3 亿**、output 228 万。cache read 占总数约 97%。

**直接显示「总 token」会严重误导**:必须分类显示,或者换算成估算费用(cache read 的单价约为输入的十分之一)。

---

## 5. 方案草图

### 5.1 数据流

```
本机:  Stop / SessionEnd 事件 (去抖) + 60 秒兜底
        └▶ daemon/tokens_scan.py  增量读 transcript(记住每个文件读到的字节偏移)
            └▶ 规范化 + 按响应 id 去重
                └▶ ~/.local/state/agentail/tokens.sqlite  (标准库 sqlite3)
                    └▶ ui.sock:tokens_summary(今日 / 本周 / 当前会话)
                    └▶ agentail tokens --by day|week|session|model
服务器:ssh 上运行固定的 Python 3.6 脚本(同 usage_claude_remote 的做法),在服务器上聚合,只回传数字
```

### 5.2 存储

每次响应一行:`host, agent, session_id, response_id, ts(UTC), model, input, cache_write, cache_read, output, reasoning`,主键 `(host, agent, response_id)`。按天 / 周 / 会话都是 SQL 查询;日界按桌面本地时区。另存 `cwd` 的末级目录名用于显示项目名(和会话卡片一致)。

体量估计:本机两周约 4800 行,一年几十万行,sqlite 毫无压力。

### 5.3 界面

- 面板:会话卡片上显示本对话消耗(如 `↑1.2M ↓48k`);Usage 区下加「Tokens」:今日 / 本周,按 agent 分行。
- CLI:`agentail tokens`(默认最近 7 天按天),`--by session` 列出最耗的对话。
- 柱状图之类留到后面。

### 5.4 服务器

transcript 在服务器上,daemon 读不到。沿用 `usage_claude_remote.py` 的模式:argv + 常量脚本,脚本在服务器上读 `~/.claude/projects` / `~/.codex/sessions` 并聚合,只返回数字和 session id,不返回任何对话内容或路径。共享 NFS HOME 的服务器(gpu7/gpu8)用 `home-id` 只扫一次。hook 脚本不改(不变量 1)。

---

## 6. 决定(2026-10-23)

第一版本机和服务器一起做,只显示分类 token,不算费用。「不存 token 计数」的原则改为「只存在本机」。设计见 [token-stats-design.md](token-stats-design.md),默认值(默认开启、自动回填、周一开始、保留 400 天)见该文 §1。

## 7. 待核实

- `cleanupPeriodDays` 是否也删除 `subagents/` 目录。
- 旧版 `token_count` 在 resume、compact 时的边界情况。
- Codex 的 `cache_write_input_tokens` 是否包含在 `input_tokens` 里(本机样本一直是 0)。
- Qwen Code、Gemini CLI 的会话文件里有没有 token 计数;Qoder 的全 0 是不是因为没有上报。
