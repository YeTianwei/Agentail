# Token 统计设计

数据来源见 [token-stats-notes.md](token-stats-notes.md)。本文只讲怎么接进 Agentail,不含实现。落地时 §6 并入 `docs/protocol.md`。

目标版本:0.0.6(审批)之后,暂定 0.0.7。

---

## 1. 范围(2026-10-23 你决定)

- **做**:Claude Code 和 Codex,本机和 SSH 服务器一起做;按对话、按天、按周统计;**只显示分类 token**(输入 / 缓存写 / 缓存读 / 输出)。
- **不做**:费用(不维护价格表);Qoder、Qwen Code、Gemini CLI(没有可用的 token 来源,见 notes §1);图表。
- **改一条原则**:0.0.2 定的「不存 token 计数」(`usage.py` 文档字符串)改为「token 计数只存在本机,不上传到任何第三方」。服务器上的计数经 ssh 回到桌面,和现有的额度查询走同一条路。
- **默认值**:本机和服务器的统计都**默认开启**(只读本地文件,不联网,不读凭据);首次启动**自动回填**现有历史;周从**周一**开始;记录保留 **400 天**。

---

## 2. 设计原则

1. **hook 脚本和线协议 v1 不变**(不变量 1)。token 数据不经过 hook,daemon 自己读会话文件。
2. **一份扫描器,本机和服务器共用。** 新增资源文件 `resources/agentail-tokens.py`,Python 3.6、只用标准库(和 hook 脚本同样的约束,测试用 `feature_version=(3, 6)` 解析)。本机由 daemon 在线程里调用;服务器上用 `python3 -c <脚本内容>` 经 ssh 运行(同 `usage_claude_remote.py`)。两边行为完全一致,测试只写一套。
3. **服务器只回传数字。** 每条记录只有 id、模型名、时间、项目目录的末级名和 token 计数。对话内容、文件路径、提示词一律不出服务器。
4. **来源主机由 daemon 决定,不由内容决定**(不变量 2):本机扫描的记录属于 `local`,ssh 到别名 `gpu7` 得到的记录属于 `gpu7` 所在的 HOME 组(§3.3)。
5. **扫描结果不可信**(不变量 4):daemon 对每个字段强转、夹紧、白名单校验后才入库;界面纯文本渲染。远程命令是固定 argv,不拼任何数据;daemon 给脚本的参数走 stdin,而且是 daemon 自己生成的游标。
6. **不读凭据、不联网**(除 ssh 到用户自己的服务器)。
7. **只读 agent 的文件**:只读 `~/.claude/projects`、`~/.codex/sessions`、`~/.codex/archived_sessions`,绝不写入。

---

## 3. 数据模型

### 3.1 一次响应 = 一条记录

```
TokenRecord = {
  agent:       "claude" | "codex",
  response_id: str,          # 去重键
  session_id:  str,          # 与 hook 的 session_id 相同,用来对上面板里的会话卡片
  ts:          int,          # UTC 秒
  model:       str | null,
  project:     str | null,   # cwd 的末级目录名,只用于 CLI 显示
  input, cache_write, cache_read, output, reasoning: int
}
```

四个类别的口径统一成**互不重叠**(`reasoning` 是 `output` 的一部分,单独列出但不重复相加):

| 字段 | Claude(`message.usage`) | Codex(`usage`) |
|---|---|---|
| `input`(未命中缓存的输入) | `input_tokens` | `input_tokens − cached_input_tokens − cache_write_input_tokens`,小于 0 时取 0 |
| `cache_write` | `cache_creation_input_tokens` | `cache_write_input_tokens` |
| `cache_read` | `cache_read_input_tokens` | `cached_input_tokens` |
| `output` | `output_tokens` | `output_tokens` |
| `reasoning`(`output` 中的思考部分) | `output_tokens_details.thinking_tokens` | `reasoning_output_tokens` |

- Codex 的 `input_tokens` 包含缓存命中([实测]:`total_tokens = input_tokens + output_tokens`)。`cache_write_input_tokens` 是否也包含在内,本机样本里一直是 0,无法确认:`TODO(M0)`。
- Claude 的 `output_tokens` 包含 thinking,Codex 的 `output_tokens` 包含 reasoning:都是 [推断],依据是两家 API 的计费口径。

### 3.2 各 agent 的解析规则

**Claude**:`~/.claude/projects/**/*.jsonl`(`CLAUDE_CONFIG_DIR` 覆盖,和 `AgentSpec.home()` 一致),包括 `<session>/subagents/*.jsonl`。

- 只看 `type == "assistant"` 且 `message.usage` 是对象的行。为了快,先按子串 `"usage"` 过滤再解析 JSON。
- `response_id = message.id`;没有 id 的行跳过。
- 同一个 id 出现多行时(多个 content block、流式中途的值、resume 或 fork 复制进新文件),**每个字段取最大值**。实测里 output 会在后面的行里变大,其他字段不变。
- `session_id = sessionId`。子代理的行本来就是父会话的 id,所以自动计入父对话。
- resume / fork 复制进新文件的行会改成**新会话**的 `sessionId`([实测]:本机 29 个跨文件的 id 全是如此)。同一个响应只记一次,归到先读到的那个会话;每天、每周的合计不受影响。
- 所有计数都是 0 的行(如 `<synthetic>` 消息)跳过。
- `project` 取 `cwd` 的末级目录名。

**Codex**:`~/.codex/sessions/**/rollout-*.jsonl` 和 `~/.codex/archived_sessions/`(`CODEX_HOME` 覆盖)。

- 0.153 起:`type == "token_usage_record"` 的行,`response_id = payload.response_id`,`session_id = payload.session_id`。
- 更早的版本只有 `event_msg/token_count`:用 `info.last_token_usage`,`response_id = "<session_id>#<total_tokens 累计值>"`。累计值不变的重复行因此自然去重。这条规则只在回填旧文件时用到:`TODO(M0)`。
- `model` 取同一文件中最近一行 `turn_context.model`,`project` 取 `session_meta.cwd` 的末级名。
- `.jsonl.zst`:如果系统有 `zstd`,就用 `zstd -dc` 解压后读(作为子进程,argv 固定为 `["zstd", "-dc", "--", path]`);没有就跳过,并把跳过的文件数报告给 daemon。这些旧文件只在回填时才会遇到,因为 Codex 压缩之前,我们已经读过它的 `.jsonl` 原文件。

两边都一样:解析失败的行跳过,不认识的字段忽略,单个文件出错不影响其他文件。

### 3.3 存储:`$XDG_STATE_HOME/agentail/tokens.sqlite`

用标准库的 `sqlite3`,WAL 模式(CLI 在 daemon 运行时也能只读打开)。

```sql
responses(store TEXT, agent TEXT, response_id TEXT,
          session_id TEXT, ts INTEGER, model TEXT, project TEXT,
          input INTEGER, cache_write INTEGER, cache_read INTEGER, output INTEGER, reasoning INTEGER,
          PRIMARY KEY (store, agent, response_id))
cursors(store TEXT PRIMARY KEY, state TEXT)   -- 扫描器的游标,见 §4.3
```

- 写入用 upsert:主键冲突时每个计数字段取 `MAX(旧, 新)`。
- **`store`**:本机是 `local`;服务器是 `home:<home_id>`。`add-host` 已经给每台服务器记录了 `home_id`,共享同一个 NFS HOME 的服务器(gpu7、gpu8)`home_id` 相同,所以同一份会话文件只存一次。没有 `home_id` 的旧记录退回用别名。
- **保留期**:默认保留 400 天(`[tokens] keep_days`),每天清理一次更早的行。Claude 默认 30 天后就会删掉自己的 transcript([文档],见 notes §2.3),所以超过 30 天的历史只存在于这里。
- 体量:本机两周约 4800 行,一年几十万行,文件几十 MB,不需要预先聚合。

### 3.4 天和周

- 按**桌面的本地时区**计算;记录里只存 UTC 秒,查询时再换算。
- 周从**周一**开始(ISO 周)。
- 「今天」「本周」都是日历意义上的,不是滚动的 24 小时或 7 天。

---

## 4. 扫描

### 4.1 扫描器接口(`agentail-tokens.py`)

从 stdin 读一个 JSON 请求,向 stdout 写一个 JSON 回答:

```json
请求  {"v": 1, "agents": ["claude", "codex"], "cursor": <上次回答里的 cursor 或 null>,
      "limit": 20000, "budget_s": 20}
回答  {"v": 1, "records": [[...], ...], "cursor": {...}, "more": false, "skipped_zst": 0}
```

- `records` 用数组而不是对象,字段顺序固定,以减小体积。
- 一次最多回 `limit` 条,最多花 `budget_s` 秒;`more: true` 时 daemon 立刻用新游标再要一次(首次回填时会遇到)。文件按 mtime 从旧到新读,所以分页回填也是按时间顺序。
- 同一次回答里同一个响应只出现一次(各计数取最大值)。
- 任何异常都只输出 `{"v": 1, "error": "<异常类名>"}`,不带路径、不带内容。

### 4.2 本机

- **触发**:本机 Claude 或 Codex 的 `Stop` / `SessionEnd` 事件之后 5 秒(去抖);打开面板时;每 60 秒兜底一次。
- 在 `asyncio.to_thread` 里直接调用扫描器的函数,不另起进程。
- **首次启动**:游标为 null 时,扫描所有现存文件,即自动回填(Claude 最多 30 天,Codex 全部)。

### 4.3 游标

游标是扫描器自己生成、daemon 原样保存的对象:`{"files": {文件标识: [inode, size, offset, context]}}`,其中文件标识是**路径的 sha256 前 16 位**,所以服务器上的路径不会到桌面。

- 文件变大:从 `offset` 接着读到最后一个完整的行。
- inode 变了或文件变小:从头重读(重复的记录由主键去重)。
- inode 和大小都没变的文件不打开。
- 游标里的文件消失了就删掉对应项。
- `context` 保存读到一半的 Codex 文件需要的状态:会话 id、项目名、当前模型、是否已经见过 `token_usage_record`。
- `.jsonl.zst` 一次读完,之后大小不变就不再打开。

### 4.4 服务器

- **命令**:`ssh_argv(ssh, alias, [python, "-c", TOKENS_SCRIPT])`,`python` 用 `add-host` 记录的解释器。请求经 stdin 发送,回答最多 16 MiB,超时 60 秒。
- **时机**:沿用 `RemoteSchedule`:那台服务器上的 Claude / Codex `Stop` 之后 15 秒、打开面板时、同一个 HOME 组每分钟最多一次;只查隧道已连接、装了对应 agent hook 的服务器;失败按 5 分钟起、最多 1 小时退避。没有定时轮询。
- **共享 HOME**:同一个 `home_id` 的服务器只查其中一台已连接的。
- **首次 `add-host` 之后**:第一次查询即回填那台服务器上的全部历史。
- `remove-host`:删掉最后一台属于某个 `home_id` 的服务器时,**保留**它的历史记录(CLI 仍能看到),只删游标。

---

## 5. 和会话卡片对应

面板上每张会话卡片的键是 `(host, agent, session_id)`。daemon 把 `host` 换算成 `store`(`local` 或那台服务器的 `home:<id>`),再按 `(store, agent, session_id)` 汇总。Claude 的 hook `session_id` 就是 transcript 的 `sessionId`,Codex 的 hook `session_id` 就是 rollout 文件名里的 id([实测],见 `tests/fixtures/codex/recorded-0.160.0.jsonl`)。

`/clear` 之后 Claude 开启新的 session id,卡片上的数字也从零开始。这和 Claude 自己的 `cost.total_cost_usd` 一致。

---

## 6. UI 协议改动(加入 `docs/protocol.md` §2)

快照新增 `tokens`,新增一种增量消息:

```json
{"type": "snapshot", ..., "tokens": TOKENS}
{"type": "tokens_update", "tokens": TOKENS}
```

```
TOKENS   = {"today": [ROW...], "week": [ROW...],
            "sessions": [{"key": {"host", "agent", "session_id"}, "totals": COUNTS}...],
            "day_start": ts, "week_start": ts, "updated_ts": ts}
ROW      = {"agent": "claude", "counts": COUNTS}
COUNTS   = {"input", "cache_write", "cache_read", "output", "reasoning"}
```

- `today` / `week` 是所有主机的合计,按 agent 分行;没有数据的 agent 不出现。
- `sessions` 只包含当前状态表里存在的会话(也就是面板上有卡片的那些),不是全部历史。
- `TOKENS` 有变化时才发,最多每 5 秒一次。
- 每次都发完整的 `TOKENS`,不发差量:体积很小(几十张卡片也只有几 KB),客户端也不用维护状态。
- 跨过午夜或周一零点时,daemon 主动重算并发送一次。
- 旧客户端会忽略未知的消息类型和快照字段(现有约定)。
- 历史查询不走 ui.sock:CLI 直接只读打开 `tokens.sqlite`,daemon 不在运行时也能用。

---

## 7. 界面

### 7.1 面板:Tokens 区

放在 Usage(额度)区之后。默认展开,折叠状态存进 `panel.json` 的 `tokens_open`。今日和本周用一个切换按钮,所选范围存进 `tokens_range`。

```
 Tokens                                   [ Today | Week ]
            Input   Cache write   Cache read   Output
 Claude      3.1k       9.5M         331M        2.3M
 Codex        13k          0          52k         175
```

- 数字用 k / M / B,保留三位有效数字;中文界面也用 k / M(和额度区的写法一致)。
- 四个类别的列标题都要经过 `_()`,中文分别是「输入」「缓存写」「缓存读」「输出」。
- `reasoning` 不单独占一列,在 Output 之后加一个括号「其中思考 x」,太窄时省略。
- 没有任何数据时整个区隐藏。

### 7.2 会话卡片

卡片上加一行小字,只显示两个数,不显示四个类别:

```
In 13.3M (93% cached) · Out 48k
```

`In` 是输入、缓存写、缓存读三项之和,`cached` 是缓存读占 `In` 的比例。四个类别的明细在 Tokens 区和 CLI 里看。会话还没有记录时不显示这一行。

### 7.3 CLI

```bash
agentail tokens                       # 最近 7 天,按天、按 agent
agentail tokens --by week --weeks 8
agentail tokens --by session --days 7 # 最耗 token 的 20 个对话:项目、主机、agent、时间、四项
agentail tokens --by model
agentail tokens --by host             # local / gpu7·gpu8(共享 HOME 的服务器合并显示)
agentail tokens --agent codex --json
agentail tokens on|off|status [--servers]   # [tokens] enabled / servers
agentail tokens forget [--yes]              # 删掉全部记录(要确认)
```

报表直接只读打开 `tokens.sqlite`,daemon 没运行也能用。表格列:输入、缓存写、缓存读、输出、其中思考;数字右对齐,格式和面板相同(k / M / B,三位有效数字)。每种分组末尾按 agent 加一行 `total`(按对话分组除外);按对话分组按总量从大到小,默认只列 20 个。

`agentail status` 末尾加一节 TOKENS TODAY(今日合计,按 agent),`agentail tail` 在收到 `tokens_update` 时打印一行今日输出量。AppIndicator 菜单加一行「Tokens today: Claude out 2.3M · Codex out 175」。`doctor` 加一项:本机最近一次扫描的时间,以及每台服务器最近一次成功的时间。

### 7.4 设置页

在设置页加一个「Token 统计」组,有两个开关:「统计 token」和「包括服务器」。两个开关对应 `config.toml`:

```toml
[tokens]
enabled = true     # false:不扫描、不显示;已有记录保留
servers = true     # false:不 ssh 去服务器扫描
keep_days = 400
```

关闭 `enabled` 不删除 `tokens.sqlite`。另外提供 `agentail tokens forget`(要确认)用来删掉全部记录:它保留扫描游标,所以已经读过的会话文件不会被重新计入,统计从此刻重新开始。

---

## 8. 测试

- `tests/test_tokens_scanner.py` 在临时 HOME 里现场生成合成数据:Claude transcript(同一个 id 出现多行且 output 递增、跨文件重复、子代理文件、坏行、无 usage 的行)和 Codex rollout(`token_usage_record`、旧版 `token_count`、`.zst`、坏行)。不放任何真实对话。
- 扫描器:`feature_version=(3, 6)` 解析;游标的续读、截断、inode 变化、文件消失;`limit` 和 `more` 分页;异常时只输出类名。
- 存储:upsert 取最大值、天和周的边界(用固定时区跑一遍)、保留期清理。
- 服务器:`tests/fakessh.py` 在临时 HOME 里运行同一个脚本;共享 `home_id` 的两台只查一次;超时、输出过大、坏 JSON。
- 面板:`tests/js/test_model.js` 覆盖数字格式、今日 / 本周切换、卡片那一行;i18n 测试会检查新字符串都有中文。
- 所有测试都用临时 HOME,不碰真实的 `~/.claude`、`~/.codex`,也不跑真 ssh。

---

## 9. 实现分解(每步一个 PR)

1. ✅ `resources/agentail-tokens.py` 扫描器 + 测试(2026-10-23,`tests/test_tokens_scanner.py`;本机真实数据只读试跑:5335 次响应,首次全量 0.88 秒含解压,增量 2 毫秒,回答约 800 KB)。
2. ✅ `daemon/tokens.py`:`TokenStore`(sqlite、upsert、汇总、天和周、清理)(2026-10-23,`tests/test_tokens_store.py`)。入库前校验:agent 只认 claude / codex,id 和模型名走白名单(`<synthetic>` 之类变成空),项目名去掉控制字符和双向控制符,时间早于 2020 年或超前一天以上、计数为负或超过 10¹⁰、思考多于输出的记录整条丢弃。文件坏了就改名为 `tokens.sqlite.broken-<时间>` 重新开始;新版本写的文件(schema 更高)不动,token 统计关闭。本机真实数据试跑:5347 条全部通过校验,写入 0.04 秒。
3. ✅ daemon 接入:本机调度、服务器调度(`RemoteSchedule`、`home_id` 分组)、`tokens` 快照与 `tokens_update`、`config.load_token_switches` / `set_token_switches`(2026-10-23,`daemon/token_stats.py`,`tests/test_token_stats.py`;协议已写进 `protocol.md`「Token counts」)。`SETTINGS` 新增 `tokens: {enabled, servers}`。没有 `home_id` 的服务器用 `alias:<别名>` 作 store。daemon 打不开文件(权限、新版本的 schema)时 `TOKENS` 为 null,原因记在日志里(doctor 在第 4 步显示)。本机冒烟测试:启动约 1 秒扫完 5378 条。
4. ✅ CLI `agentail tokens`、`status`、`doctor`(2026-10-23,`tokens_cli.py`,`tests/test_tokens_cli.py`)。doctor 需要的扫描状态放在快照的 `tokens_status` 里(只在快照中,不随增量发送,见 `protocol.md`):本机最近一次成功扫描的时间、保留的记录数、每个 HOME 组最近一次成功的时间和连续失败次数、因为没有 `zstd` 跳过的压缩文件数。
5. 面板:`model.js` 纯函数 + `extension.js` 的 Tokens 区、卡片行、设置组 + i18n。
6. AppIndicator 一行。
7. 文档:`protocol.md`、README、CHANGELOG,以及 `usage.py` 文档字符串里的原则。

不变量检查:hook 脚本不改;远程命令是固定 argv;不读凭据;不写 agent 文件;不涉及审批。

---

## 10. 已知限制

- resume / fork 之后,复制过来的旧响应可能算在新对话上(§3.2)。
- 共享 NFS HOME 的服务器分不出是哪一台用的,统计按 HOME 组合并显示(如 `gpu7·gpu8`)。会话卡片不受影响,因为卡片本身带主机名。
- 回填只能拿到 Claude 还没删的部分(默认 30 天)。在 Claude Desktop 里开始或继续过的会话不受这个限制([文档])。
- 一台服务器连不上超过 30 天,这期间的 Claude 记录会被 Claude 自己删掉,之后也拿不回来。
- 服务器上的 Claude 如果靠交互式 shell 里设置的 `CLAUDE_CONFIG_DIR` 指定配置目录,非交互 ssh 里看不到这个变量。这和 `usage_claude_remote.py` 的限制相同。
- Codex 0.153 之前的旧文件只能按 `token_count` 估算。
- 服务器上没有 `zstd` 时,压缩过的旧文件跳过(doctor 里显示跳过的数量)。
- 数字是 agent 写进本地文件的值,不是账单。
