# Claude 额度:主动查询 Anthropic 用量接口(设计)

日期:2026-10-12。前置:[usage-design.md](usage-design.md)(0.0.2 的用量消息与面板)、[usage-notes.md](usage-notes.md) §2.4(本次推翻了那里"不碰凭据"的结论)。本文只讲设计,不含实现。

---

## 1. 背景

0.0.2 的 Claude 额度只来自 statusLine 的 `rate_limits`,而 statusLine 只在**终端交互式** `claude` 里运行。VS Code 扩展、Claude Desktop 都以 `--output-format stream-json` 驱动 `claude`,没有状态栏,所以永远不会上报额度。

实际表现(2026-10-12,owner 的机器):本机和 gpu7 上的 Claude Code 会话都在 VS Code 里持续使用,但面板上的 Claude 卡显示的是 3 天前的读数(`via gpu7 · updated 3d ago`),而且是换账号之前那个账号的。在终端里运行一次 `claude` 后读数立刻更新,说明链路本身没坏,只是缺少数据来源。

参考实现:
- open-vibe-island(GPL,只读了 README):同样只用 statusLine,README 明确说 stream-json 模式下不更新,"不依赖 CLI 的来源"列为后续工作。
- **CodeIsland**(<https://github.com/wxtsky/CodeIsland>,MIT,2026-10-12 阅读 `ClaudeQuotaClient.swift`、`ClaudeQuotaScheduler.swift`、`ClaudeQuota.swift`):用 Claude Code 自己的 OAuth 登录查询 `/api/oauth/usage`,默认关闭。本设计的安全措施和刷新策略基本沿用它的思路,代码用 Python 自己写,不照搬。

## 2. 实测 [实测]

2026-10-12,Claude Code 2.1.294(VS Code 扩展),Pro 账号,Linux。

- 凭据文件 `~/.claude/.credentials.json`,权限 `0600`,结构 `{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt"(毫秒), "refreshTokenExpiresAt", "scopes", "subscriptionType": "pro", "rateLimitTier"}, "organizationUuid"}`。当时 access token 还剩约 6.9 小时有效。
- `GET https://api.anthropic.com/api/oauth/usage`,请求头 `Authorization: Bearer <accessToken>`、`anthropic-beta: oauth-2025-04-20`,本机直连(没有代理)返回 HTTP 200。响应里与我们相关的部分:

```json
{
  "five_hour":  {"utilization": 43.0, "resets_at": "2026-10-12T04:40:00.348344+00:00", ...},
  "seven_day":  {"utilization": 17.0, "resets_at": "2026-10-17T04:00:00.348390+00:00", ...},
  "limits": [
    {"kind": "session",    "group": "session", "percent": 43, "severity": "normal",
     "resets_at": "2026-10-12T04:40:00.348344+00:00", "scope": null, "is_active": true},
    {"kind": "weekly_all", "group": "weekly",  "percent": 17, "severity": "normal",
     "resets_at": "2026-10-17T04:00:00.348390+00:00", "scope": null, "is_active": false}
  ],
  ...
}
```

- 在 VS Code 里执行 `/usage` 时,结果会作为 `usageReport.rate_limits.limits` 写进会话 transcript,结构与上面的 `limits` 完全一致,所以 `/usage` 用的就是这个接口 [推断]。
- 响应里还有很多与额度无关、名字含义不明的字段(`seven_day_breakdown`、`spend`、各种代号),一律不读。

### 2.1 Max 账号与套餐名 [实测]

2026-10-14,同一台机器,账号从 Pro 升级到 Max 5x 之后:

- 凭据文件里仍是 `"subscriptionType": "pro"`、`"rateLimitTier": "default_claude_ai"`。当天 Claude Code 刷新过 token(文件 mtime 变了),这两个字段没变,所以它们大概只在 `/login` 时写入 [推断]。面板因此一直显示 `Claude Code · Pro`。
- `limits[]` 多了一项单个模型的周额度:

```json
{"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
 "resets_at": "2026-10-17T04:00:00+00:00",
 "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null}, "is_active": false}
```

  旧格式里对应的 `seven_day_opus` / `seven_day_sonnet` 为 `null`。`weekly_scoped_shares` 说明它占总周额度的 50%,不读。
- `GET https://api.anthropic.com/api/oauth/profile`(同样的请求头;凭据的 `scopes` 含 `user:profile`)返回 HTTP 200,与套餐有关的部分:

```json
{"account": {"has_claude_max": true, "has_claude_pro": false, ...},
 "organization": {"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x",
                  "subscription_status": "active", ...}}
```

  其余字段是姓名、邮箱、uuid 等,一律不读、不存、不记日志。

## 3. 设计原则

1. **默认开启,可以关闭**(§4)。关闭时不读凭据文件、不发任何请求。
2. **只读、不刷新。** 只读 `accessToken`、`expiresAt`、`subscriptionType` 三个字段;永不使用 `refreshToken`。刷新会换发新 refresh token,让 Claude Code 手里那份失效。token 过期就等 Claude Code 下次运行时自己刷新。
3. **token 不离开这一次请求。** 每次请求前重新读文件,不缓存在进程里,不写日志,不进 `usage.json`、`--print-events`、`--record`,不出现在异常信息里;只发往写死的 `https://api.anthropic.com/api/oauth/usage`。拒绝重定向,不用 cookie;TLS 证书校验使用默认值。
4. **只取规范化后的数字。** 响应同样当作不可信输入(不变量 4),走现有的 `make_window` 夹紧与校验。
5. **按需查询,不定时轮询。** 额度只在一轮对话结束时变化,所以由 Stop 事件和打开面板来触发(§5)。
6. **只查本机账号。** 远程服务器不读凭据;owner 已决定(2026-10-12)默认本机与服务器使用同一个账号,服务器上的 Claude 仍走 statusLine。
7. **不改线协议、不改 hook。** 读数仍是 `UsageReading(host="local", agent="claude")`,USAGE 记录不变;UI 协议只为查询状态加了向后兼容的字段与消息(§4.1)。

## 4. 开关:默认关闭,面板询问一次(owner 决定 2026-10-15)

> **2026-10-15 改为默认关闭**(推广前的安全评估,"安全 1"):默认开启意味着用户在不知情时,daemon 就拿 Claude Code 的 OAuth token 以 `User-Agent: agentail` 访问一个未公开的接口;是否允许第三方工具这样用由 Anthropic 决定,被限流或封号的风险落在用户身上;`.deb` 又会对机器上每个用户自动生效。现在:
>
> - `[usage] claude_api`(本机登录)和 `claude_api_servers`(各服务器自己的登录,经 ssh)两个开关,**都默认关闭**;只有布尔值算"已选择"。
> - 两个键都没写过时,UI 协议的 `USAGE_API.decided` 为 false;GNOME 面板在见过 Claude(会话或读数)后,在 Usage 区问一次:「Turn on」/「Also on servers」(有服务器时)/「No thanks」,答案通过 `agentail usage-api on|off [--servers]` 写进 `config.toml`。说明文字写明接口未公开、请求标明来自 Agentail。
> - `install-local` 在未选择时打印一行提示(`OFFER`),开启后才打印完整说明(`NOTE`);`doctor` 分别显示两个开关。
> - 从 0.0.3 升级:没写过这个键的用户变为关闭,面板会问一次;用 `usage-api on` 明确开过的保持开启,但服务器那一侧要另外打开。
>
> 下面是 2026-10-12 的原决定,保留作记录;"默认开启"和"面板上不放开关"两条已被上面取代。

### 4.0 原决定:默认开启,可关闭(2026-10-12,已取代)

会看 Usage 的用户本来就需要 Claude 额度,所以**默认开启,面板上不放开关**。关闭的办法保留在配置里:

```toml
# ~/.config/agentail/config.toml
[usage]
claude_api = false   # 不读 Claude Code 的登录、不查询 Anthropic 用量接口;默认 true
```

- daemon 是唯一读这个键的地方,在现有的 hosts 轮询循环里按 mtime 重新读取,改完不用重启。改为 `false` 后立即停止读凭据和查询;已有的 API 读数不删除,会自然变旧,Claude 卡回到 statusLine 的读数。
- CLI:`agentail usage-api on|off|status`,用 tomlkit 改写上面的键(保留注释和其他键,同 `hosts.toml` 的做法)。
- `doctor` 新增一行 `claude usage api`:关闭 / 上次成功于 N 分钟前 / token 已过期("运行一次 claude 即可") / 最近失败(HTTP 码或网络错误,不含响应正文)。
- **告知用户**:因为默认开启、用户不会主动确认,所以 README 的 Usage 与隐私说明、CHANGELOG(含"升级后自动生效")都要写清楚:读哪个文件、只读哪几个字段、发往哪里、何时查询、如何关闭。`install-local` 结束时打印一行同样的说明。

### 4.1 面板上的状态

不加开关,但让用户知道登录过期了:

- 开启且读数来自本机时,Claude 卡头不再标 `via <host>`。
- 状态为"需要登录"时,卡上的更新时间改为 `sign-in expired · run claude once`;其他错误不在面板上显示(旧读数按 30 分钟规则变灰),原因到 `doctor` 里看。

协议改动(`docs/protocol.md` §2,v1 的向后兼容扩展,旧扩展忽略未知字段):

```json
{"type": "snapshot", ..., "usage_api": {"claude": USAGE_API}}
{"type": "usage_api_status", "agent": "claude", "status": USAGE_API}
```

```
USAGE_API = {"state": "off|pending|ok|needs_login|error", "detail": "...", "ok_ts": 上次成功的时间}
```

扩展到 daemon 的方向不变(仍只有 `refresh_usage`)。

## 5. 何时查询

纯函数的调度器 `Schedule`(只记时间戳、算下次时间,不碰定时器,方便测试),参数参照 CodeIsland:

| 触发 | 规则 |
|---|---|
| daemon 启动 / 配置从关闭改为开启 | 立即查一次 |
| 任意主机上的 `claude` 会话发出 `STOP` | 等最后一个 Stop 之后 15 秒再查(合并同时结束的多个会话);两次查询至少间隔 60 秒,间隔内来的 Stop 记下来,窗口结束后补查一次 |
| 打开面板(现有的 `refresh_usage`) | 读数超过 60 秒就查 |
| 其他时间 | 不查。没有 10 分钟兜底轮询:我们的胶囊不显示用量,窗口重置后用户打开面板时自然会刷新 |

远程主机的 Stop 也算,因为同账号的假设下它同样消耗本机账号的额度。Qoder 等兼容 agent 的 Stop 不算。

失败处理:

| 结果 | 处理 |
|---|---|
| 凭据文件不存在 / 无法解析 / `expiresAt` 已过 | 不发请求;状态记为"需要登录" |
| 401 / 403 | 状态记为"需要登录",停止后台查询,直到凭据文件的 mtime 变化(Claude Code 刷新了 token)或用户打开面板 |
| 429 | 有 `Retry-After` 就按它,否则按下面的退避 |
| 其他 HTTP 码、网络错误、超时(15 秒)、解析失败 | 指数退避:60 秒起,翻倍,最多 15 分钟;成功后清零 |

任何失败都只写一行日志(状态码或异常类型),面板保留旧读数,按现有规则 30 分钟后变灰。查询放在线程里(`asyncio.to_thread`),不阻塞事件循环;同一时间最多一个请求。

## 6. 解析

1. 优先读 `limits[]`:`kind == "session"` → `window_minutes = 300`;`kind == "weekly_all"` → `10080`;`percent` → `used_percent`;`resets_at`(ISO 8601)→ Unix 秒。
2. `limits` 缺失或里面没有可用的窗口时,退回读 `five_hour.utilization` / `seven_day.utilization`。
3. `kind == "weekly_scoped"`(某个模型单独的周额度,Max 账号有,§2.1)→ `window_minutes = 10080`,窗口的 `label` 取 `scope.model.display_name`(经 `clean_label` 白名单校验),面板显示 "Week · Fable"。取不到可用名字的 scoped 窗口跳过,免得和总的周额度混在一起;其他未知 kind 也跳过。旧格式的 `seven_day_opus` / `seven_day_sonnet` 同样带标签 "Opus" / "Sonnet"。
4. `plan` 优先取 profile 接口(§2.1):`organization.rate_limit_tier` 为 `default_claude_max_<N>x` 时取 `max_<N>x`,否则 `organization_type` 去掉 `claude_` 前缀(`pro`、`max`、`team`…),再否则看 `account.has_claude_max` / `has_claude_pro`。profile 只在 daemon 启动后和凭据文件每次变化后各查一次(owner 决定 2026-10-14,不定时轮询),跟着下一次用量查询一起发;网络错误或 429 时随下一次用量查询重试,其他失败等下一次凭据变化。查不到时退回凭据文件里的 `subscriptionType`。查到的套餐也用于本机 statusLine 的读数。面板显示为 `Claude Code · Max 5x`。凭据文件变化本身也会触发一次用量查询(以前只在登录失效后才会)。
5. 一个窗口都解析不出来就算失败(进入退避),不发空读数。
6. `ts` 用收到响应的时间。

## 7. 与 statusLine 读数的关系

- 两种来源并存,不互斥。statusLine 读数仍按来源主机存放;API 读数存为 `host = "local"`,和本机 statusLine 那一份共用一个位置。
- statusLine 只知道 5h 和 Week,不知道套餐和单个模型的额度。为了不让它冲掉接口读数(2026-10-15 修正:以前本机 statusLine 一到,"Week · Fable" 那行就消失;服务器上的读数更新时,卡片会丢失套餐名),daemon 记住最近一次接口读数,把同一账号的 statusLine 读数(本机或任何服务器)补全:数字用 statusLine 自己的,套餐和它缺少的带标签窗口用接口的。"同一账号"的判据是总周额度的 `resets_at` 相差不超过 10 分钟(`usage.same_account`),对不上就原样保存。Codex 同理:app-server 读数是完整来源,会话文件读数用它补全。
- 面板沿用"同一 agent 显示最新那条"的规则。开启后本机这一份总是最新,所以换账号后残留的旧账号读数(正是这次的问题)也会自然被盖住,不需要额外的账号识别。
- 已知限制不变:如果服务器上用的是另一个账号,两边的读数会互相覆盖(usage-design.md §8)。

## 8. 模块与测试

- `src/agentail/daemon/usage_claude_api.py`:读凭据(注入路径)、构造请求、解析响应、调度器;HTTP 用标准库 `urllib`,base URL 可注入。不引入新依赖。
- `daemon/main.py`:读开关;在 `handle()` 里把 claude 的 `STOP` 交给调度器;`request_live_usage` 同时触发 Codex 和 Claude;主循环里检查调度时间。
- `config.py`:`load_usage_settings()` / `set_usage_setting()`(CLI 用);`cli.py`:`usage-api` 子命令;`doctor.py`:新检查项。
- `daemon/uiapi.py`:快照带 `usage_api`;广播 `usage_api_status`。
- `model.js`:Claude 卡的 `sign-in expired` 文案;`tests/js/test_model.js` 覆盖。
- 测试(不碰真实 `~/.claude`、不访问网络):临时 HOME 里的假凭据文件;本地 `http.server` 假接口返回 200 / 401 / 429 / 500 / 302 / 畸形 JSON / 只有旧字段;断言 token 不出现在日志、`usage.json` 和 `--print-events` 输出里;调度器的去抖、限频、补查、退避、"需要登录"后等 mtime 变化各一组用例;配置关闭时凭据文件从未被打开。
- 文档:`usage-notes.md` §2.4 改为本方案并注明理由;README 的 Usage limits 一节和隐私说明;CHANGELOG。

不变量检查:hook 脚本不改;没有新的远程命令;负载内容不进 shell;不实现审批;没有复制 GPL 代码(open-vibe-island 只读了 README)。

## 9. 风险

- **非公开接口**:字段和请求头随时可能改变。解析失败时保留旧值、进入退避,doctor 里能看到;statusLine 这条路仍然有效。
- **请求频率限制未知**:按上面的规则,一天的请求数大约等于对话轮数 ÷ 合并比例,最多每分钟一次。
- **token 过期**:Claude Code 运行时会自己刷新。长时间不用时读数会变灰,这和"没在用、额度也不会变"是一致的。

## 10. 已决定(2026-10-12)

1. 设置存 `config.toml`,另有 CLI 与 doctor 检查项。
2. 放进 0.0.3,在 A5 发布前完成(PLAN.md 的 A3b)。
3. **默认开启,面板上不放开关**;关闭靠 `config.toml` / `agentail usage-api off`。默认开启的代价是用户没有主动确认,所以 README、CHANGELOG 和 `install-local` 的输出必须说明(§4)。
4. 远程服务器不查询,默认本机与服务器使用同一个账号。

其他项目对 Codex 和其他 agent 的额度做法见 [usage-notes.md](usage-notes.md) §6。

## 11. 服务器上的 Claude 登录(2026-10-15,owner 同意读取服务器上的凭据)

本机的查询能覆盖所有用同一账号的服务器。覆盖不到的两种情况:服务器登录的是另一个账号;本机根本没有可用的 Claude 登录(只在服务器上通过 VS Code Remote 用 Claude,而 VS Code 里 statusLine 不运行)。这时 daemon 在那台服务器上查询(`daemon/usage_claude_remote.py`):

- **不在服务器上安装任何东西。** 通过 ssh 用 add-host 记录的 `python3` 运行一段固定的 Python 3.6 脚本(`python3 -c SCRIPT usage [profile]`,参数只有这两个常量)。脚本在服务器上读 `$CLAUDE_CONFIG_DIR/.credentials.json`(默认 `~/.claude`),规则和本机相同:只读 `accessToken`、`expiresAt`、`subscriptionType`,不刷新,拒绝重定向。
- **token 不离开服务器。** 输出只有接口的回答;profile 在服务器上就裁剪成套餐字段(没有姓名、邮箱、uuid);错误只保留 HTTP 状态码或异常类名。解析在本机用现有的 `parse_usage` / `parse_plan` 完成。
- **何时查询**(`daemon/schedule.RemoteSchedule`,和服务器上的 Codex 共用):那台服务器上的 Claude Stop 之后 15 秒,或打开面板时;每台服务器每分钟最多一次;失败后 5 分钟起退避;只查装了 Claude hook、隧道已连接、Claude 出现过的服务器。前提是"需要":`[usage] claude_api` 开着(同一个开关),并且本机查询的状态不是 ok,或者这台服务器最近 24 小时没被确认和本机同一账号。确认的方式:服务器的任何 Claude 读数(statusLine 或它自己的查询)和本机接口读数的周重置时间一致(`same_account`)。所以同一账号的服务器最多被单独查一次,之后由本机查询代表。
- 套餐:每台服务器首次查询时连 profile 一起问,之后每 24 小时一次。
- 实测(2026-10-15,gpu6、gpu8,Max 5x):一次约 1.2 秒,两台都和本机同一账号。
