# 用量(额度)数据来源调研(0.0.2)

调研日期:**2026-10-08**。标注和 `agent-hooks-notes.md` 一致:

- **[文档]** 官方文档明确写的。
- **[实测]** 在本机真实环境里看到的(附版本)。
- **[第三方]** 社区工具或文章说的,没有官方文档。
- **[推断]** 根据以上内容推出来的,没有实测。

目标:在面板里显示 Claude 的 5 小时 / 7 天额度,以及 Codex 的 5 小时 / 每周额度。

---

## 1. 结论

| agent | 推荐来源 | 可信度 |
|---|---|---|
| Claude Code | statusLine 的 stdin JSON 里的 `rate_limits` | [文档] |
| Codex | 会话 jsonl 里 `token_count` 事件的 `rate_limits` | [实测] + [第三方] |

两条路径都**不需要读取任何登录凭据**(`~/.claude/.credentials.json`、`~/.codex/auth.json`)。Agentail 不应该去读这两个文件。

---

## 2. Claude Code

来源:<https://code.claude.com/docs/en/statusline>(2026-10-08 阅读)。Claude Code 版本 2.1.290。

### 2.1 statusLine 的 `rate_limits` [文档]

statusLine 命令的 stdin 会收到 JSON,其中:

| 字段 | 含义 |
|---|---|
| `rate_limits.five_hour.used_percentage` | 5 小时窗口已用百分比,0–100 |
| `rate_limits.five_hour.resets_at` | 窗口重置时间,Unix 秒 |
| `rate_limits.seven_day.used_percentage` | 7 天窗口已用百分比 |
| `rate_limits.seven_day.resets_at` | 同上 |
| `rate_limits.spend_limit.*` | 仅在 Claude apps gateway 后面出现(v2.1.251+),不在本次范围 |

文档写明的限制:

- 只对 claude.ai 的 Pro / Max 订阅用户出现(或 gateway 有额度限制时)。API key 用户没有。
- 只在会话里**第一次 API 响应之后**才出现。
- 每个窗口可能各自缺失,脚本必须容忍。
- 窗口过了 `resets_at` 后,文档说明 statusLine 会重新触发;显示时不能继续用旧百分比。

### 2.2 hook payload 里没有额度 [推断]

`agent-hooks-notes.md` 里列出的 hook 事件字段不含 `rate_limits`。所以现有的 `agentail-hook.py` 拿不到这个数据,需要 statusLine 这个新入口。

### 2.3 对安装器的影响 [推断]

`statusLine` 在 `settings.json` 里是**单个命令**,不是列表。用户可能已经配了自己的。所以 Agentail 不能覆盖它,只能:

1. 记录原命令;
2. 写入一个包装命令:把 stdin 先转发给原命令并原样输出它的 stdout,同时把 `rate_limits` 摘出来发给 daemon;
3. 卸载时还原原命令。

这和 PLAN §2 不变量 5(只合并不覆盖、可还原)一致。包装器同样要遵守不变量 1:任何失败都不影响原 statusLine 的输出。

### 2.4 不推荐:`/api/oauth/usage` [第三方]

第三方工具(如 [ccusage on PyPI](https://pypi.org/project/ccusage/))调用 `GET https://api.anthropic.com/api/oauth/usage`,带本地 OAuth token 和 `anthropic-beta: oauth-2025-04-20`。问题:

- 要读取 OAuth token,违背上面"不碰凭据"的原则。
- 没有官方文档;同一来源称响应结构已经变过(旧的扁平 `seven_day_*` 键为 null,新数据在 limits 数组里)。

---

## 3. Codex

### 3.1 会话 jsonl 里的 `rate_limits` [实测]

codex-cli 0.160.1。路径 `~/.codex/sessions/YYYY/MM/DD/*.jsonl`,事件 `payload.type == "token_count"`,`payload.rate_limits` 的真实内容(脱敏前后一致,不含凭据):

```json
{
  "limit_id": "codex",
  "limit_name": null,
  "primary":   {"used_percent": 10.0, "window_minutes": 300,   "resets_at": 1791453381},
  "secondary": {"used_percent": 4.0,  "window_minutes": 10080, "resets_at": 1791953908},
  "credits": {"has_credits": false, "unlimited": false, "balance": "0"},
  "individual_limit": null,
  "spend_control_reached": null,
  "plan_type": "plus",
  "rate_limit_reached_type": null
}
```

- `resets_at` 是 Unix **秒**(1791453381 对应 2026-10-08)。
- 窗口长度来自 `window_minutes`:这里 primary = 300(5 小时),secondary = 10080(7 天)。**不要写死**,第三方资料里有说 primary 是 2 小时的,不同套餐可能不同。
- `primary` / `secondary` 都可能是 null [推断]。

### 3.2 注意事项

- **可能过期 [推断]**:只在 Codex 有响应时才写入。没有会话时数值会变旧,所以要显示"多久前更新",且窗口过了 `resets_at` 就按已重置处理。
- **找最新文件要按 mtime [实测]**:`sessions/*/*/*/*.jsonl` 按路径排序,最后一个不一定是最新的。
- **压缩格式 [第三方]**:有资料称某些版本的 rollout 文件是 `.jsonl.zst`。0.160.1 上是 `.jsonl`;解析器要在遇到其他后缀时静默跳过,不报错。
- **大文件**:只需要最后一个 `token_count`,从文件尾部往前读即可,不要整文件解析。
- **格式不稳定**:没有官方文档,字段可能随版本变,所有字段访问都要容忍缺失和类型错误(同 `adapters/claude.py` 的做法)。

### 3.3 备选:`codex app-server` [实测类型定义]

`codex app-server generate-ts`(0.160.1)生成的类型里有 `RateLimitSnapshot`、`RateLimitWindow`:

```ts
RateLimitWindow = { usedPercent: number, windowDurationMins: number | null, resetsAt: number | null }
RateLimitSnapshot = { ..., primary: RateLimitWindow | null, secondary: RateLimitWindow | null,
                      credits, individualLimit, spendControlReached, planType, rateLimitReachedType, ... }
```

请求 `account/rateLimits/read`,推送 `account/rateLimits/updated`(来自 [openai/codex PR #5302](https://github.com/openai/codex/pull/5302) 和第三方镜像)。含义和 jsonl 一致,字段是 camelCase。

不选它的原因:官方 README 说 app-server 协议目前不稳定;需要额外起一个子进程并带登录态;而 jsonl 已经足够。留作以后 jsonl 失效时的备选。

---

## 4. 对架构的影响 [推断]

- **额度是账号级的**:本机读一份就够。服务器上的会话如果用同一个账号,不必在每台服务器上重复读取。但 Claude 的 statusLine 在服务器上运行时,数据会走现有的 socket 隧道回传,所以协议里要标注来源主机(来源仍只由 socket 决定,不变量 2)。
- **协议**:`docs/protocol.md` 需要新增一个用量消息类型,作为 v1 的向后兼容扩展;UI 侧的快照和增量里加用量字段。具体设计另写。
- **UI**:每个窗口显示百分比 + 重置倒计时;缺失的窗口不显示;过期数据置灰。
- **隐私**:只传百分比、窗口长度、重置时间和套餐名,不传 token 计数、对话内容或凭据。

---

## 5. 待验证

- 👤 Claude statusLine:在真实订阅账号上确认 `rate_limits` 的实际出现时机、包装器不改变原 statusLine 的显示。
- 👤 Codex:`primary` / `secondary` 为 null 的情形;长时间没有会话后,jsonl 里的数据多久过期;服务器上的 Codex 是否同账号。
- 窗口刚过 `resets_at` 时两家各自的行为。
