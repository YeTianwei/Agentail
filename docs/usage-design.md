# 用量(Usage)消息与面板设计(0.0.2)

数据来源见 [usage-notes.md](usage-notes.md)。本文只讲怎么接进 Agentail,不含实现。落地时 §3 并入 `docs/protocol.md`。

---

## 1. 设计原则

1. **线协议不变(仍是 v1)。** 用量走现有的 hook 信封:`{"agent": "claude", "event": "StatusLine", "stdin": "<statusLine JSON>"}`。远程服务器因此直接复用现有隧道,hook 脚本不需要任何改动(不变量 1:哑管道,没有 agent 逻辑)。
2. **daemon 只保留规范化后的数字。** 适配器从载荷里挑出百分比、窗口长度、重置时间、套餐名,其余全部丢弃。载荷不可信(不变量 4),所以每个数字都要强转类型并夹紧范围。
3. **来源主机仍由 socket 决定**(不变量 2)。用量按 `(host, agent)` 存。
4. **缺数据就不显示,不猜。** 没有就留空;过期就置灰;窗口已重置就不再显示旧百分比。
5. **不碰凭据。** 两家都不读 `.credentials.json` / `auth.json`。
6. **旧客户端不受影响。** 新增的消息类型和快照字段对旧扩展是未知字段/类型,按现有约定忽略。

---

## 2. 数据流

```
Claude (本机/远程)  statusLine ──包装命令──▶ agentail-hook.py --agent claude --event StatusLine
                                              │  (现有 socket / 隧道)
                                              ▼
                                         adapters/claude.py ──▶ UsageReading ─┐
                                                                              ├▶ UsageStore ─▶ ui.sock ─▶ 扩展
Codex (仅本机)  ~/.codex/sessions/**.jsonl ─▶ daemon/usage_codex.py ─▶ UsageReading ┘
```

### 2.1 Claude:statusLine 包装器

`install-local` / `add-host` 在 `settings.json` 里设置 `statusLine`。因为它是单个命令(不是列表),采用**包装**而不是覆盖:

- 用户原来的 `statusLine` 命令保存进安装 manifest,卸载时逐字节还原(不变量 5)。
- 新命令是一段固定的 `sh -c`:先读 stdin 到变量;把它送给 `agentail-hook.py … --event StatusLine`(输出丢弃、失败忽略、不阻塞);再把同一份 stdin 交给原命令,**原样输出原命令的 stdout**。没有原命令时输出空。
- 任何失败都不能影响原状态栏的显示。

两个风险(第一个已实测,见 PLAN.md 0.0.2:每次约 19 ms,不需要去重):
- statusLine 触发很频繁,每次都起一次 Python 发送。信封很小,但仍要量一下开销;如果明显,可以在包装脚本里用 `rate_limits` 子串变化才发送的办法去重,**不在 hook 脚本里加逻辑**。
- 订阅用户之外(API key)没有 `rate_limits`,适配器直接返回 None,什么都不更新。

### 2.2 Codex:daemon 读取本机 jsonl

Codex 的 hook 载荷里没有额度,只能读文件。因此只覆盖**本机**的 Codex。服务器上的 Codex 见 §2.4(2026-10-15 加入)。

`daemon/usage_codex.py`:
- 触发:daemon 启动时一次;收到本机 `codex` 事件后(去抖 5 秒);另有每 60 秒一次按 mtime 的兜底检查。
- 找最新文件:按 mtime 取 `~/.codex/sessions/**/*.jsonl` 最新的几个,不依赖路径排序。
- 只读文件**尾部**(例如最后 256 KiB),从后往前找最后一个 `payload.type == "token_count"` 且带 `rate_limits` 的行。读不到就保持原值。
- 遇到不认识的后缀(如 `.jsonl.zst`)或解析失败时静默跳过。
- 文件读取放进线程(`asyncio.to_thread`),不阻塞事件循环。
- 只读 `sessions/`,绝不打开 `auth.json`。

### 2.4 Codex:服务器(2026-10-15)

只在本机读 Codex 时,只在服务器上用 Codex(比如 VS Code Remote)的人看不到用量。daemon 在服务器上通过 ssh 启动 `codex app-server`(`codex_appserver.remote_argv`),问法和本机完全一样(`account/rateLimits/read`),读数存为该服务器的别名:

- 远程命令是固定的 shell 脚本(`REMOTE_SCRIPT`,不带任何参数),按顺序找 codex:PATH、`~/.local/bin` 等常见目录、VS Code Remote 扩展自带的那一份。非交互 ssh 的 PATH 往往不含 `~/.local/bin`(gpu8 实测如此)。有 `timeout` 时用它包一层,确保 app-server 不会在 ssh 断开后长期残留。
- 用的是服务器上 Codex 自己的登录,token 不离开服务器,只有 JSON-RPC 的回答经 ssh 回来。不读服务器上的任何文件。
- 何时查询(`RemoteSchedule`):那台服务器上的 Codex Stop 之后 15 秒(去抖),或打开面板时;同一台服务器每分钟最多一次;只查 `hosts.toml` 里装了 Codex hook、隧道已连接、Codex 出现过的服务器;失败后按 5 分钟起、最多 1 小时退避。没有定时轮询。
- 实测(gpu8,codex-cli 0.160.0,2026-10-15):一次约 1.5 秒,结束后服务器上没有残留进程;读数的重置时间和本机完全相同,所以按账号分组时和本机合成一张卡。
- 它是完整来源(`_on_usage(full=True)`),和本机 app-server 一样。

### 2.3 规范化

适配器输出 `UsageReading`:

```
UsageReading = {host, agent, plan, windows: [Window...], ts}
Window       = {used_percent: float 0..100, window_minutes: int | null, resets_at: int | null,
                label: str | null}   # 0.0.4 起
```

规范化规则:

| 来源字段 | 输出 |
|---|---|
| Claude `rate_limits.five_hour` | `window_minutes = 300` |
| Claude `rate_limits.seven_day` | `window_minutes = 10080` |
| Claude `used_percentage` / Codex `used_percent` | `used_percent`,夹紧到 0..100 |
| Codex `window_minutes` | 原样(强转 int,范围 1..525600,否则 null) |
| `resets_at` | 强转 int 秒,不合理的值(≤0 或远在未来)置 null |
| Codex `primary` / `secondary` | 按顺序放进 `windows`,为 null 的跳过 |
| `plan_type` | 只保留匹配 `^[a-z0-9_-]{1,16}$` 的字符串,否则 null |
| `spend_limit`(gateway) | 0.0.2 不处理,忽略 |
| Claude `spend`(`enabled` 时;没有则 `extra_usage`,`is_enabled` 时)、Codex `credits`(有余额或无限额度时,0.0.4 起) | `extras`:已花金额(可带上限)或余额;两者在 owner 的账号上都是关的/空的,开启后的单位按字段名推断,标了 `TODO(M0)`。面板上有上限的画进度条,否则只有文字 |
| 只针对某个模型的额度(0.0.4 起) | 单独的窗口,`label` 为模型或额度名,只保留匹配 `^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,23}$` 的字符串;取不到名字的跳过 |

`windows` 按 `window_minutes` 升序排列,同样长度的窗口不带 `label` 的(总额度)在前、其余按 `label` 排,UI 不依赖字段名。

这套结构对所有 agent 通用(2026-10-14):

- **Claude**:`plan` 来自 profile 接口,启动时和凭据文件变化时各查一次;`weekly_scoped` 带模型名标签。见 `usage-api-design.md` §2.1、§6。
- **Codex**:`plan` 随每次读数一起来(`plan_type` / `planType`),换套餐后下一次读数就会更新,不需要额外查询。其他额度:app-server 的 `rateLimitsByLimitId` 里 `limitId` 不是主额度的项,和会话文件里 `limit_id` 不是 `codex` 的 `token_count` 事件,都变成带 `limitName` 标签的窗口(没有名字就用 id)。实测(codex-cli 0.160.1,Plus 账号)只见过主额度 `codex`,其他额度的字段含义按字段名推断,代码里标了 `TODO(M0)`。会话文件只合并同一个文件里的其他额度。
- **Qoder 及以后的 agent**:目前没有用量来源(本机 `~/.qoder` 里没有额度数据,qodercli 也没有公开接口)。以后找到来源时,适配器的 `usage()` 或 daemon 里的读取器返回同样的 `UsageReading`(`plan` + 带或不带 `label` 的窗口),面板、`status`、AppIndicator 都不用改。
`ts` 是 daemon 收到的时间(Codex 用文件里该事件的时间戳,取不到就用 mtime),不是载荷里的字段。

---

## 3. UI 协议改动(加入 `docs/protocol.md` §2)

快照新增 `usage` 数组,新增增量消息:

```json
{"type": "snapshot", "sessions": [...], "hosts": [...], "usage": [USAGE...]}
{"type": "usage_update", "usage": USAGE}
{"type": "usage_remove", "host": "gpu1", "agent": "claude"}
```

```
USAGE = {"host", "agent", "plan": "plus" | null,
         "windows": [{"used_percent", "window_minutes", "resets_at"}],
         "updated_ts"}
```

- `host` 来自 socket(本机为 `local`)。
- 主机从 `hosts.toml` 删除或隧道断开时:删除时发 `usage_remove`;断开时**保留**,让界面靠 `updated_ts` 判断过期,不像会话那样标 stale。
- 值没有变化的读数最多每分钟广播一次(`UsageStore.update` 比较后再发),避免 statusLine 高频触发把客户端队列刷满。
- 字符串字段只有 `host`、`agent`、`plan`,且 `plan` 经过白名单校验,界面仍是纯文本渲染。

---

## 4. 面板显示

### 4.1 位置与结构

放在 **按 agent 分组的会话列表之后、Servers 之前**,是一块固定的「Usage」区域,和 macOS 版面板底部的用量区对应。每个有数据的 agent 一行组:

```
 Usage
 ┌──────────────────────────────────────────────┐
 │ [Claude]  Max                  updated 2m ago │
 │ 5h    ████████░░░░░░░░░░░░   41%   resets 2h 10m │
 │ Week  ███░░░░░░░░░░░░░░░░░   15%   resets 4d 3h  │
 ├──────────────────────────────────────────────┤
 │ [Codex]   Plus                 updated 1h ago │
 │ 5h    ██░░░░░░░░░░░░░░░░░░   10%   resets 3h 5m  │
 │ Week  █░░░░░░░░░░░░░░░░░░░    4%   resets 2d 9h  │
 └──────────────────────────────────────────────┘
```

- 一块用量区,每个 agent 一张小卡,卡头沿用现有的 agent 图标 + 名字;右侧是套餐名(有才显示)。
- 每个窗口一行:标签、进度条、百分比、重置倒计时。标签由 `window_minutes` 得出:300 → `5h`,10080 → `Week`,其他按小时或天显示(如 `2h`、`30d`),为 null 时显示 `Limit`。
- 没有任何用量数据的 agent 不占位。两家都没有数据时整块用量区隐藏,不显示空框。
- 默认**展开**。需要折叠时与 Servers 一样,折叠状态存进 `panel.json` 的新字段 `usage_open`。

### 4.2 颜色与状态

进度条沿用现有状态色,按百分比分级,同时保留数字,不只靠颜色:

| 已用 | 颜色 |
|---|---|
| < 70% | 蓝(中性) |
| 70–89% | 橙(接近) |
| ≥ 90% | 红(快用完) |

- **多来源的选择**:同一个 agent 有多个 `(host, agent)` 读数时,面板显示 `updated_ts` 最新的那一条。卡头的更新时间旁边,若有来源不是 `local`,标注主机名(如 `via gpu7`)。不同账号混用时这里会有误导,已知限制,见 §6。
- **过期**:`now - updated_ts > 30 分钟` → 整张卡变灰,显示 `updated 1h ago`;不影响百分比数字的显示。
- **已重置**:某个窗口 `now > resets_at` → 这一行不显示旧百分比,改为 `reset · waiting for next update`,条置空;不能继续显示"41%"。
- **缺失**:`resets_at` 为 null 时不显示倒计时;`window_minutes` 为 null 时标签为 `Limit`。

### 4.3 顶栏胶囊

0.0.2 **不在胶囊里显示用量**:胶囊现在是状态指示(空闲 / 运行 / 需要你 / 主机断线),加数字会挤。用量区的标题行也不加提示红点(已决定)。

### 4.4 通知

0.0.2 **不做**用量通知(没有"快用完了"弹窗)。理由:数据延迟(Codex 只在有响应时更新、Claude 要等第一次响应),过早打扰;可以在 0.0.3 观察数据质量后再决定。

---

### 4.5 多个账号

额度属于账号,不属于主机。界面(面板 `model.js` 的 `usageGroups`、`status` 和 AppIndicator 用的 `usage.group_by_account`,两份实现保持一致,容差常量由测试同步)把读数按"agent + 账号"分组,每组一张卡,显示组内最新的读数:

- 同一 agent、总周额度的重置时间相差不超过 10 分钟的读数是同一账号。
- 没有周窗口的读数:并入自己主机所在的那一组;该 agent 只有一组时并入那一组;都没有指纹时合成一组(和以前一样);否则单独成卡。
- 周额度已经重置过的读数(比自己账号的任何新读数都旧),在该 agent 有当前账号时不显示;`status` 里仍列出,ACCOUNT 列为 `-`。
- 只有一个账号时界面和以前一样(服务器来源显示 "via gpu8");有多个账号时每张卡列出它出现在哪些主机("this computer · gpu8"),本机的账号排在前面。套餐名取组内任一条读数的。"sign-in expired" 只出现在本机所在的那张卡上。

在界面侧分组,协议和 daemon 都不用改:客户端每次从完整状态重新分组,不会因为周重置时间变化而拿着过期的账号编号。

## 5. 实现分解(给后续 PR 的任务清单)

1. `adapters/base.py`:新增 `UsageReading` 与规范化辅助函数(夹紧、强转、白名单);测试覆盖畸形输入。
2. `adapters/claude.py`:识别 `event == "StatusLine"`,抽取 `rate_limits`;`tests/fixtures/claude/` 加合成样例。
3. `daemon/usage.py`:`UsageStore`(按 `(host, agent)` 存,比较后才广播,主机删除时清理);`daemon/usage_codex.py`(§2.2,注入文件系统路径,测试用临时 HOME)。
4. `daemon/uiapi.py` / `main.py`:快照与增量消息;`agentail status` 打印用量。
5. `install/claude_config.py`:statusLine 包装与还原(§2.1),含"用户原本没有 statusLine"、"已经被包装"、"用户之后改过 statusLine"三种情形的测试;`doctor` 检查项。
6. `model.js`:纯函数 `usageCards(state, now)`,输出每张卡的 agent、套餐、窗口行(标签、百分比、颜色级别、重置文字)、过期与已重置状态;`tests/js/test_model.js` 覆盖 §4.2 全部分支。
7. `extension.js` / `stylesheet.css`:用量区;`panel.json` 的 `usage_open`。
8. `ui/model.py`(AppIndicator 退路):菜单里加两行文字用量,例如 `Claude 5h 41% · Week 15%`。
9. 文档:`protocol.md`、README、CHANGELOG。

不变量检查:没有新的远程命令、不读凭据、不改 hook 脚本、不实现审批。

---

## 6. 已知限制

- 服务器上的 Codex 只在那台服务器上的 Codex 出现过(事件或读数)、隧道已连接时才查询。
- 额度是账号级的,但读数按 `(host, agent)` 存;如果不同机器用不同账号,面板只显示最新的一个账号。
- Claude 要等会话里的第一次响应后才有数据;API key 用户没有。
- Codex 的 jsonl 格式没有官方文档,字段随版本可能变;解析必须容错,失败时显示旧值并变灰,不能报错。

## 7. 已决定(2026-10-08)

1. 用量区默认展开。
2. 过期阈值 30 分钟。
3. 不加 ≥ 90% 的标题红点。
4. 颜色分级 70% / 90%。

## 8. 暂不做(2026-10-08 决定)

- **多账号**:读数按 `(host, agent)` 存,面板对每个 agent 只显示最新的一条。本机和服务器用同一个账号时没有问题;不同账号会互相覆盖。想做时的办法:用周窗口的 `resets_at` 当账号指纹,指纹相同合并成一张卡,不同则分卡显示来源。指纹已经用于补全同一账号的读数(`usage.same_account`、`fill_in`,2026-10-15,见 `usage-api-design.md` §7)。**分卡显示已做(2026-10-15)**:见 §4.5。
- ~~服务器上的 Codex 用量~~:已做(2026-10-15),改用 ssh 上的 `codex app-server`(§2.4),不读服务器上的文件。
