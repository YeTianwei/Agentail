# Agent hook 机制笔记(M0-b)

查阅日期:**2026-09-30**。每条结论后面标注来源和可信度:

- **[文档]** 官方文档明确写的。
- **[源码]** 官方开源仓库源码里能直接看到的(比文档更具体,但可能随版本变)。
- **[推断]** 根据文档/源码推出来的,没有实测,需要 M1/M2 在本机用 `agentail daemon --record` 验证。

来源:

- Claude Code:<https://code.claude.com/docs/en/hooks>(hooks reference 页,2026-09-30 抓取)。
- Codex:<https://developers.openai.com/codex/hooks> 与 `/codex/config-reference` 是官方文档,但本次 Cloud 环境的出网代理拦截了 `developers.openai.com`,**没能直接读到**。改为阅读官方仓库 <https://github.com/openai/codex>(Apache-2.0)`main` 分支,commit `bcd6d9ab6b9f26f85d76d0c680b3f88b367bffa0`(2026-09-30)。只读、只记录接口事实,没有复制代码。
  相关文件:`codex-rs/hooks/schema/generated/*.command.input.schema.json`(每个事件 stdin 的 JSON Schema)、`codex-rs/config/src/hook_config.rs`、`codex-rs/hooks/src/engine/discovery.rs`、`codex-rs/hooks/src/legacy_notify.rs`、`codex-rs/features/src/lib.rs`、`codex-rs/utils/cli/src/shared_options.rs`、`codex-rs/core/src/hook_runtime.rs`。
  👤 建议你在本机打开 developers.openai.com/codex/hooks 对照一遍,有出入以文档为准并在这里更正。

---

## 1. Claude Code

### 1.1 配置结构 [文档]

`settings.json`(用户级 `~/.claude/settings.json`):

```json
{"hooks": {"<EventName>": [
  {"matcher": "<可选>", "hooks": [
    {"type": "command", "command": "...", "timeout": 5, "async": false}
  ]}
]}}
```

- 与 `install/claude_config.py` 现有结构**一致**,无需修改。
- `matcher` 为 `""`、`"*"` 或省略时匹配全部。对 `Notification` 匹配 `notification_type`,对 `SessionStart` 匹配 `source`,对工具事件匹配 `tool_name`。
- `type` 还可以是 `http` / `mcp_tool` / `prompt` / `agent`,我们只用 `command`。
- 命令默认用 `bash` 执行;工作目录是会话当前目录(不存在时依次退回启动目录、项目根、HOME、临时目录)。环境会继承父进程(去掉 `OTEL_*`),另加 `CLAUDE_PROJECT_DIR` 等。
- 直接编辑 settings 文件会被文件监视器**自动加载**;`/hooks` 菜单只读,**没有信任/审核步骤**。

### 1.2 超时和退出码 [文档]

- `command` 默认 timeout **600 秒**;`UserPromptSubmit` 为 30 秒;所有 `SessionEnd` hook 合计只有 **1.5 秒**预算(可随单个 hook 的 timeout 提高,上限 60 秒)。我们 fire 模式约 1 秒,够用,但 SessionEnd 比较紧(见 §3.1)。
- 退出码 0 且 stdout 为空:成功,不产生任何效果(`UserPromptSubmit`/`SessionStart` 的纯文本 stdout 会被加进上下文,所以 hook 脚本**必须什么都不打印**——与不变量 1 一致)。
- 退出码 2:在可阻断的事件上阻断操作。其他非零码:非阻断错误,stderr 会显示为 `Failed with non-blocking status code`。我们始终退出 0。
- `async: true`:后台运行、不阻塞 Claude,**timeout 不再生效**。

### 1.3 公共 stdin 字段 [文档]

每个事件都有:`session_id`、`transcript_path`、`cwd`、`permission_mode`、`hook_event_name`。
另有 `prompt_id`(首个用户输入前没有)、`scratchpad_dir`、`effort.level`、`agent_id` / `agent_type`(只在子 agent 内部或 `--agent` 时出现)。

### 1.4 我们关心的事件 [文档]

| 事件 | 额外字段 | 说明 |
|---|---|---|
| `SessionStart` | `source`: `startup`/`resume`/`clear`/`compact`/`fork`;`model`;`session_title` | 会话开始或恢复 |
| `UserPromptSubmit` | `prompt` | |
| `PreToolUse` | `tool_name`、`tool_input`、`tool_use_id` | |
| `PostToolUse` | 同上 + `tool_response` | |
| `PostToolUseFailure` | 同 PostToolUse | 工具失败时触发,**不会**再触发 PostToolUse [推断:文档把两者列为不同事件] |
| `PermissionRequest` | `tool_name`、`tool_input`、`tool_use_id` | "When a tool call needs a permission decision" |
| `Notification` | `message`、`title`、`notification_type` | 见 1.5 |
| `Stop` | `stop_hook_active`、`last_assistant_message` | "When Claude finishes responding" |
| `StopFailure` | `error_type`: `rate_limit`/`overloaded`/`server_error`/... | 回合因 API 错误结束 |
| `SubagentStop` | 同 Stop | |
| `SessionEnd` | `reason`: `clear`/`resume`/`logout`/`prompt_input_exit`/`other` | |
| `PreCompact` | `compaction_type`: `manual`/`auto` | |

完整事件列表还包括 `Setup`、`UserPromptExpansion`、`PermissionDenied`、`PostToolBatch`、`SubagentStart`、`TeammateIdle`、`TaskCreated`、`TaskCompleted`、`FileChanged`、`CwdChanged`、`DirectoryAdded`、`ConfigChange`、`InstructionsLoaded`、`PostCompact`、`PreModelSwitch`、`PostModelSwitch`、`MessageDisplay`、`Elicitation`、`ElicitationResult`、`WorktreeCreate`、`WorktreeRemove`——v1 不需要。

### 1.5 Notification 的类型字段 [文档]

`notification_type` 是**明确的字段**(不用再解析 `message` 文本),取值:

| 值 | 含义 | Agentail 处理 |
|---|---|---|
| `permission_prompt` | 需要用户授权 | ATTENTION / PERMISSION → `needs_attention` |
| `idle_prompt` | 空闲、等输入(触发时长文档没写) | ATTENTION / IDLE → `waiting_input` |
| `elicitation_dialog`、`elicitation_url_dialog` | MCP 表单/链接对话框等用户填 | ATTENTION / OTHER → `needs_attention` |
| `agent_needs_input` | 子 agent 需要输入 | ATTENTION / OTHER → `needs_attention` |
| `auth_success`、`elicitation_complete`、`elicitation_response`、`agent_completed`、`quota_auto_resume_*` | 信息类 | `OTHER`,不改变状态 |

文档没有给出引入 `notification_type` 的版本号。为兼容更老的版本,缺少该字段时仍退回解析 `message`(旧逻辑)[推断]。

### 1.6 未确认 / 需要实测

- 用户按 Esc 中断回合时是否触发 `Stop`:文档没有提到。[推断] 大概率**不触发**,会话会停在 `running`,直到下一次 prompt 或 `idle_prompt` 通知。M1 需实测。
- `idle_prompt` 在空闲多久后发出:文档未写。

---

## 2. Codex CLI

### 2.1 启用方式 [源码]

- 特性开关 `hooks`(旧名 `codex_hooks` 仍可用),**Stable,默认开启**(`features/src/lib.rs`)。网上较早的文章说"实验性、默认关闭",已过时。
- 管理员可以在 `requirements.toml` 设 `allow_managed_hooks_only = true`,此时用户级 hook 全部被忽略(`docs/config.md`)。

### 2.2 配置位置 [源码]

两种写法,事件结构完全相同(与 Claude 一致:事件名 → matcher 组 → `hooks` 列表):

1. `hooks.json`,放在各配置层的配置目录里。用户级是 `$CODEX_HOME/hooks.json`,默认 `~/.codex/hooks.json`:
   ```json
   {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "...", "timeout": 5}]}]}}
   ```
   顶层只允许 `description` 和 `hooks`(`deny_unknown_fields`)。
2. `config.toml` 里内联,**顶层** `[hooks]` 表(放进 `[features]` 会类型错误):
   ```toml
   [[hooks.Stop]]
   [[hooks.Stop.hooks]]
   type = "command"
   command = "..."
   timeout = 5
   ```

同一层两种都写时会给出警告 "prefer a single representation"。

handler 字段:`type = "command"`、`command`(字符串,整行交给 shell)、`timeout`(秒)、`async`、`statusMessage`、`commandWindows`。未设 timeout 时默认 **600 秒**;`SessionEnd` 和 `Interrupt` 默认 1 秒、上限 3 秒。

命令执行方式:`$SHELL -lc "<command>"`(**登录 shell**,会读 `.profile` 之类,启动可能较慢)[源码 `command_runner.rs`]。

### 2.3 信任机制(重要)[源码]

- 非托管(用户/项目/插件)的 hook 需要被**信任**才会执行。信任状态存在 `config.toml` 的 `[hooks.state."<key>"]` 里:`trusted_hash = "sha256:..."`、`enabled = true/false`。
- key 形如 `<来源文件路径>:<事件名 snake_case>:<组序号>:<handler 序号>`,例如 `/home/u/.codex/hooks.json:stop:0:0`。
- hash 是对规范化后的 hook 定义(事件名 + matcher + handler)做 SHA-256。**hook 命令一改,hash 就变**,状态变成 `Modified`,需要重新信任。
- 状态为 `Untrusted` / `Modified` 的 hook **静默跳过**。TUI 启动时会弹出"启动 hook 审核"界面,`/hooks` 里也能审核。
- 命令行 `--dangerously-bypass-hook-trust` 可以对单次调用跳过信任(文档注明"DANGEROUS,仅供已审核 hook 来源的自动化使用")。

### 2.4 事件与 stdin 字段 [源码:schema 文件]

事件共 12 个:`PreToolUse`、`PermissionRequest`、`PostToolUse`、`PreCompact`、`PostCompact`、`SessionStart`、`SessionEnd`、`UserPromptSubmit`、`SubagentStart`、`SubagentStop`、`Stop`、`Interrupt`。**没有 `Notification`**。

公共字段:`session_id`(即 thread id)、`cwd`、`hook_event_name`、`transcript_path`(可为 null)、`model`、`permission_mode`;回合内事件还有 `turn_id`。

| 事件 | 额外字段 |
|---|---|
| `SessionStart` | `source`: `startup`/`resume`/`clear`/`compact`/`fork` |
| `UserPromptSubmit` | `prompt`、`turn_id` |
| `PreToolUse` | `tool_name`、`tool_input`、`tool_use_id` |
| `PostToolUse` | 同上 + `tool_response` |
| `PermissionRequest` | `tool_name`、`tool_input` |
| `Stop` | `stop_hook_active`、`last_assistant_message` |
| `Interrupt` | 只有公共字段 + `turn_id`;用户中断当前回合时触发(子 agent 不触发) |
| `SessionEnd` | `reason`(目前只有 `"other"`) |

退出码 0 且 stdout 为空 → 无效果;`Stop` 退出码 2 会要求继续(需要 stderr 写续写提示)。我们始终退出 0,不打印。

### 2.5 `notify`(旧机制,作为后备)[源码]

- `config.toml` 顶层 `notify = ["程序", "参数1", ...]`(字符串数组,只能一个程序)。
- 每个回合结束时启动该程序,**最后一个 argv 参数**是 JSON:
  `{"type": "agent-turn-complete", "thread-id": ..., "turn-id": ..., "cwd": ..., "client": ..., "input-messages": [...], "last-assistant-message": ...}`。
- stdin/stdout/stderr 都是 null,不等待退出。
- 源码注释写着 legacy,将来会移除。**不需要信任**。

---

## 3. 结论与实现建议

### 3.1 `adapters/claude.py`(本 PR 已改)

- 字段名 `session_id`、`cwd`、`hook_event_name`、`prompt`、`tool_name`、`message` 均与文档一致,不需要改。
- Notification 改为优先读 `notification_type`,按 §1.5 的表分类;信息类通知解码为 `OTHER`,不再把会话误标成"等输入"。缺字段时退回解析 `message`。
- 新增 `StopFailure` → `STOP`(否则 API 出错结束的回合会一直显示 running),并加入 `hook_events()` 让安装器注册它。
- 新增 `PostToolUseFailure` → `TOOL_END`(否则失败的工具调用会让 `active_tools` 计数一直不归零),也加入 `hook_events()`。
- 状态机相应调整:`Attention.OTHER`(MCP 对话框、子 agent 要输入)也算 `needs_attention`。
- `install/claude_config.py` 的配置结构已核对无误。**建议(M2)**:对 fire 模式的 hook 加 `"async": true`,彻底不阻塞 Claude(代价:timeout 不生效,但脚本自己有 `--timeout`)。SessionEnd 的 1.5 秒合计预算要留意,不要为 SessionEnd 注册多个 agentail 条目。
- `PermissionRequest` 暂不注册:`Notification/permission_prompt` 已经覆盖"需要授权";等 v1 之后做审批时再用。

### 3.2 `adapters/codex.py`(M2 实现,本 PR 不改代码)

- 注册事件:`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`PermissionRequest`、`Stop`、`Interrupt`、`SessionEnd`。
- 映射:`SessionStart`→SESSION_START;`UserPromptSubmit`→PROMPT_SUBMIT(`prompt`);`PreToolUse`/`PostToolUse`→TOOL_START/TOOL_END(`tool_name`);`PermissionRequest`→ATTENTION/PERMISSION(Codex 没有 Notification,这是唯一的"等授权"信号)[推断:它在弹出授权前触发];`Stop` 和 `Interrupt`→STOP;`SessionEnd`→SESSION_END。
- 会话 id 只用 `session_id`(hooks)和 `thread-id`(notify)。现有代码里的 `thread_id` / `conversation_id` 猜测可以删掉。
- notify 后备:payload 在**最后一个 argv**,不是 stdin。hook 脚本目前只读 stdin,M2 若要支持 notify,需要给 hook 脚本加一个"把最后一个参数当 payload"的选项(仍保持无 agent 逻辑)。事件名 `agent-turn-complete` → STOP;会话 id 取 `thread-id`;prompt 取 `input-messages` 最后一条。

### 3.3 `install/codex_config.py`(M2)

- **优先写 `~/.codex/hooks.json`**,而不是往 `config.toml` 里内联:JSON 文件独立、结构与 Claude 相同,可以直接复用 `claude_config.py` 的合并/移除逻辑,也不用处理 TOML 注释保留。已存在时照样合并(按 `agentail-hook` 标记识别),先备份。
- **信任**:安装器**不应**自己计算并写入 `trusted_hash`(那等于替用户绕过 Codex 的安全审核,而且 hash 算法是内部实现,随时会变)。安装后提示用户:下次启动 Codex 时在"hook 审核"界面(或 `/hooks`)信任 agentail 的条目。命令字符串要保持稳定(相同路径、相同参数),否则每次重装都需要重新信任。`agentail status` / `doctor` 可以提示"Codex 已安装但尚未收到任何事件,可能还没信任"。
- 远程主机:同样的问题——用户需要在每台服务器上首次运行 Codex 时信任一次。写进 M3 的手动步骤。
- `notify` 串联只作为可选后备(比如服务器上的 Codex 版本太旧、没有 hooks):`notify` 只能有一个程序,已有值时要把原命令包起来串联,卸载时还原。建议 M2 先不做,除非 M0-a 发现服务器上的 Codex 太旧。
- `$SHELL -lc` 是登录 shell:hook 命令里用绝对路径,不要依赖 `~` 以外的 shell 特性。

### 3.4 需要 M1/M2 实测确认(👤)

1. Claude:Esc 中断时是否有 Stop;`idle_prompt` 的触发时长;真实 payload 录下来替换手写 fixture。
2. Codex:hooks.json 安装后首次启动是否弹出审核;信任后 `SessionStart`/`UserPromptSubmit`/`Stop`/`Interrupt` 是否都能收到;`PermissionRequest` 的触发时机。
3. 两边的版本号记到 `docs/m0-verification.md`。

### 3.5 M2 本机实测结果(2026-10-05,Claude Code 2.1.289、codex-cli 0.160.0)

- **Codex 信任**:装好 `hooks.json` 后,`codex exec` 一个事件都不发,也没有任何提示(未信任即静默跳过,与 2.3 一致)。在 TUI 里用 `/hooks` 批准后,Codex 往 `config.toml` 写入 `[hooks.state."/home/<user>/.codex/hooks.json:<event>:0:0"] trusted_hash = ...`,每个事件一条。
  - `uninstall-local` 不会删这些条目(`config.toml` 只读不写)。条目指向的 hook 已不存在,不影响 Codex 运行,但 `config.toml` 不会逐字节恢复;这是 Codex 写的状态,不是我们写的。
  - hash 覆盖命令字符串:安装器改了命令格式(比如本次加 `2>/dev/null || true`),重装后需要重新信任。
- **Codex `PermissionRequest` 时机**:在授权提示出现时触发,用户回答之前(录制中 `PermissionRequest` 到同一工具的 `PostToolUse` 相隔约 6.4 秒,即批准耗时)。payload 没有 `tool_use_id`。录制见 `tests/fixtures/codex/recorded-0.160.0.jsonl`。
- **Codex 其他观察**:所有事件都有 `model`、`permission_mode`,回合内事件有 `turn_id`;`SessionStart` 有 `source: "startup"`;`Stop` 有 `last_assistant_message`。失败的 `apply_patch` 只有 `PreToolUse` 没有 `PostToolUse`。
- **Claude 设置热加载**:正在运行的 Claude 会话会热加载 `settings.json`,安装后无需重启即开始发事件;卸载后到重新加载之前,还会用旧命令调用已删除的脚本。`python3 missing.py` 退出码 2 被 Claude 当作阻塞错误(实测出现 `PostToolUse:Bash hook blocking error`)。修复:hook 命令末尾加 `2>/dev/null || true`(`install/hookjson.py`)。
- 未测:Claude 的 Esc 中断与 `idle_prompt`;Codex 的 `Interrupt`、`SessionEnd`。
