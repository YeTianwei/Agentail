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

**2026-10-12 更新 [实测]**:statusLine 在 VS Code 扩展和 Claude Desktop(`stream-json` 模式)里不运行,只用 VS Code 的用户永远拿不到 Claude 额度。实测这个接口可用(HTTP 200,同时有 `limits[]` 和 `five_hour` / `seven_day`),因此改为在用户开启后只读查询,设计见 [usage-api-design.md](usage-api-design.md)。

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

### 3.3 `codex app-server` 主动查询 [实测]

codex-cli 0.160.1(2026-10-08)。启动 `codex -s read-only -a never app-server`(与 CodexBar 的文档一致,
<https://github.com/steipete/CodexBar> 的 `docs/codex.md`,只读了文档),通过 stdin/stdout 发 JSON-RPC:

1. `{"id":1,"method":"initialize","params":{"clientInfo":{"name":..,"title":null,"version":..},"capabilities":null}}`
2. `{"method":"initialized"}`
3. `{"id":2,"method":"account/rateLimits/read"}`

结果里的 `rateLimits.primary` / `secondary` 是 `{usedPercent, windowDurationMins, resetsAt}`,另有 `planType`、
`accountId`(账号标识,可以用来区分账号)和按 `limitId` 分组的 `rateLimitsByLimitId`。整个过程约 1 秒,关闭 stdin 后进程
正常退出。它不发消息,Codex 用自己的登录态查询,Agentail 不读 `auth.json`。

实测时它返回的 5 小时用量(11%)比 jsonl 里的(10%)新:jsonl 只在 Codex 回答时才更新。上游标明 app-server 协议不稳定,
所以失败时回退到读 jsonl。

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

---

## 6. 其他项目的做法(2026-10-12)

读了两个同类 macOS 项目的最新 `main`:

- **CodeIsland**(<https://github.com/wxtsky/CodeIsland>,MIT):读了源码和 CHANGELOG。
- **open-vibe-island**(<https://github.com/Octane0411/open-vibe-island>,GPL-3.0):读了 README 和额度相关源码,下面只记做法,**不复制任何代码**(不变量 6)。

### 6.1 Claude

| 项目 | 来源 | 刷新 |
|---|---|---|
| open-vibe-island | statusLine 包装器把 `rate_limits` 写进 `/tmp/open-island-rl.json`,应用读这个文件 | 每 5 秒读一次文件;README 承认 stream-json 模式(Desktop)不更新 |
| CodeIsland | 默认关闭的开关;用 Claude Code 的 OAuth 登录查询 `/api/oauth/usage`(macOS 读 Keychain,否则读 `.credentials.json`) | Stop 驱动,见 [usage-api-design.md](usage-api-design.md) |
| Agentail | 0.0.2:statusLine;0.0.3 起再加 CodeIsland 式的接口查询 | 同上 |

### 6.2 Codex:我们的方案已经更完整

| | open-vibe-island | CodeIsland | Agentail(0.0.2) |
|---|---|---|---|
| 数据来源 | `~/.codex/sessions` 下的 `rollout-*.jsonl`,按 mtime 取最新文件,找最后一个 `token_count` 的 `rate_limits`(`primary` / `secondary`、`plan_type`、`limit_id`) | **没有做**(它的调研文档把 Codex 额度列为 P2 计划;并记下免费账号没有 5h / 7d 窗口) | 同样的 jsonl 来源;另在打开面板时用 `codex app-server` 的 `account/rateLimits/read` 实时查询 |
| 读取方式 | 整个文件读入后逐行解析 | — | 只读尾部 256 KiB |
| 刷新 | 开关打开时每 120 秒一次 | — | 启动时;本机 Codex 事件后 5 秒;每 60 秒兜底;打开面板时实时查询(每分钟最多一次) |
| 开关 | 有,`~/.codex` 存在时默认开 | — | 无,一直开(不读凭据) |

结论:不需要替换,我们的方案在新鲜度(app-server 实时查询)和读取开销(只读尾部)上都更好。可以借鉴三点:

1. **字段位置的容错**:open-vibe-island 同时接受 `payload.rate_limits` 和 `payload.info.rate_limits`。codex-cli 0.160.1 实测在 `payload` 这一层(`payload` 的键是 `type`、`info`、`rate_limits`,`info` 里只有 token 计数),所以我们现在没问题;改成两处都找的代价很小,可以在下次动 `usage_codex.py` 时顺手加上。
2. **"已触顶"状态**:open-vibe-island 看到 `rate_limit_reached_type` 不为空,或回合进行中 `primary.used_percent >= 100` 时,把这个**会话**标为被限流。我们可以在会话卡上显示"已触顶"(而不只是额度卡变红)。这是新功能,以后单独设计。
3. **多账号**:CodeIsland 允许登记额外的配置目录(第二个 `CLAUDE_CONFIG_DIR` / `CODEX_HOME`),每个目录单独装 hook、单独统计。这正是 [usage-design.md](usage-design.md) §8 暂不做的多账号问题的一种解法,留作参考。

### 6.3 其他 agent(Qoder、Qwen Code、Gemini CLI 等):两边都没有做

- 两个项目都支持这些 agent 的**状态**(open-vibe-island 还有 Factory、CodeBuddy、Cursor、Kimi、Grok、OpenCode、Pi 等),但**都没有额度显示**。open-vibe-island 把 Qoder / Qwen 写成"Claude Code 的 fork,hook 格式相同";它们是否也有 statusLine、有没有 `rate_limits`,两边都没有提到。
- CodeIsland 另有一个"用量统计",但统计的是 **token 数量**而不是额度:增量扫描本地 Claude transcript(`message.usage`,按 `message.id` 去重同一次 API 响应的多行,只读上次读到的字节之后的部分),画出近 5 小时 / 今日的 token 和每小时柱状图,完全不联网。对没有额度来源的 agent 来说,这是一种可选的替代显示;但 Agentail 0.0.2 定过"不存 token 计数"(`usage.py` 文档字符串),要做得先改这个原则。

以后接入其他 agent 的额度时,按下面的线索调研(**都未验证**):

- **Qoder / Qwen Code**:作为 Claude 格式的 fork,先查它们是否支持 `statusLine`,以及 stdin JSON 里有没有类似 `rate_limits` 的字段;有的话可以直接复用 Claude 的包装器和解析。
- **Gemini CLI**:查 `/stats` 等命令显示的额度从哪个接口或文件来,以及 hook payload 或会话文件里有没有配额字段。
- 任何需要读凭据的方案,都按 [usage-api-design.md](usage-api-design.md) 的规则:可以关闭、用户知情、只读不刷新、token 不落地。
