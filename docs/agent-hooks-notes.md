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
- `PermissionRequest` 暂不注册:`Notification/permission_prompt` 已经覆盖"需要授权";0.0.6 做审批时再用(调研见 §5)。

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

---

## 4. 0.0.3 新 agent(A1 调研,2026-10-08)

查阅日期 **2026-10-08**,标注同上([文档] / [源码] / [推断])。只读、只记录接口事实,没有复制代码。来源:

- Qwen Code:<https://github.com/QwenLM/qwen-code>(Apache-2.0)`main` commit `2e962b6`,`docs/users/features/hooks.md`、`docs/users/configuration/settings.md`、`packages/core/src/hooks/{hookRunner,types}.ts`、`packages/core/src/utils/sanitize-child-env.ts`。
- Qoder CLI:<https://docs.qoder.com/cli/hooks>、<https://docs.qoder.com/zh/cli/hooks.md>(闭源,只有文档)。
- Gemini CLI:<https://geminicli.com/docs/hooks/reference/>;<https://github.com/google-gemini/gemini-cli>(Apache-2.0)`main` commit `44d764e`,`docs/hooks/index.md`、`docs/reference/configuration.md`、`packages/core/src/hooks/{hookRunner,hookRegistry,trustedHooks,types}.ts`、`packages/core/src/services/environmentSanitization.ts`、`packages/core/src/utils/shell-utils.ts`。

### 4.1 总结

| | Qwen Code | Qoder CLI | Gemini CLI |
|---|---|---|---|
| 格式 | **Claude 兼容**(事件名、结构、字段都是 Claude 那套) | **Claude 兼容** | **自有格式**(BeforeTool 等) |
| 用户级配置 | `~/.qwen/settings.json`;`QWEN_HOME` 覆盖目录 | `~/.qoder/settings.json`(国内版 `~/.qoder-cn/`) | `~/.gemini/settings.json`;`GEMINI_CLI_HOME` 覆盖的是 **$HOME**(目录变成 `$GEMINI_CLI_HOME/.gemini`) |
| 默认启用 | 是(`disableAllHooks: true` 关闭) | 文档未写 | 是(`hooksConfig.enabled` 默认 true) |
| 用户 hook 需信任 | 否 | 文档未写 | 用户 hook 不需要;但见 4.4 folder trust |
| 执行方式 | `bash -c` | `bash -c` | `bash -c` |
| `timeout` 单位 | 秒(≥1000 仍按毫秒,兼容旧配置) | 秒 | **毫秒** |
| 授权信号 | `Notification`(`permission_prompt`)、`PermissionRequest` | 同左 | `Notification`(`ToolPermission`) |
| 空闲信号 | `Notification`(`idle_prompt`) | 同左 | 无 |
| 回合结束 | `Stop` | `Stop` | `AfterAgent` |

**最重要的修正**:PLAN 原先担心 Qwen 是 Gemini fork 所以格式更接近 Gemini。实际上 Qwen Code 的 hooks 是 Claude 那一套 [文档+源码],可以和 Qoder 一起放在 A2。

### 4.2 Qwen Code [文档 + 源码]

- 配置:`settings.json` 顶层 `hooks`,结构与 Claude 相同(`{"<Event>": [{"matcher": "...", "hooks": [{"type": "command", "command": "...", "timeout": 10}]}]}`)。用户级 `~/.qwen/settings.json`;环境变量 `QWEN_HOME` 改全局配置目录(空串视为未设)[文档:settings.md]。
- 默认启用;顶层 `disableAllHooks: true` 全部关闭;`--safe-mode`、`--bare` 下也不跑 [文档]。项目级 hook 只在受信任目录加载,**用户级不受影响**,没有逐条审核 [文档]。
- 执行:`bash -c <command>`,`shell: false` 的 spawn [源码:hookRunner.ts]。环境变量只删掉内部令牌(`QWEN_SERVER_TOKEN` 等),其他原样继承 [源码:sanitize-child-env.ts],所以我们白名单里的变量能到 hook。另设 `QWEN_PROJECT_DIR`、`CLAUDE_PROJECT_DIR`、`GEMINI_PROJECT_DIR`。
- 事件(22 个)与 Claude 同名:`SessionStart`、`SessionEnd`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`Notification`、`PermissionRequest`、`Stop`、`StopFailure`、`SubagentStop` 等 [文档]。
- 公共字段:`session_id`、`transcript_path`、`cwd`、`hook_event_name`、`timestamp`、`permission_mode` [文档]。`UserPromptSubmit.prompt`;`PreToolUse` 有 `tool_name`、`tool_input`、`tool_use_id`;`PermissionRequest` 有 `tool_name`、`tool_input`。
- `notification_type`:`permission_prompt`、`idle_prompt`、`auth_success`;`elicitation_dialog` 已定义但未实现 [文档 + 源码 types.ts]。
- 工具名是 Qwen 自己的(`run_shell_command`、`write_file` 等,继承自 Gemini),但文档说 Claude 的 matcher 如 `Bash`、`Write|Edit` 也能用 [文档]。我们 `matcher: ""` 不受影响;`tool_input` 里的键名(`command`、`file_path`/`path`)需要录制确认 `TODO(M0)`。
- 退出码:0 时 stdout 若是 JSON 则解析,否则当作普通文本(`SessionStart`/`UserPromptSubmit` 时注入上下文)[文档]。我们的 hook 什么都不输出,无影响。
- **结论**:可直接复用 `ClaudeAdapter` 的解码逻辑和 `claude_config.merge_hooks`(不带 statusLine 包装),`timeout` 秒。

### 4.3 Qoder CLI [文档]

- 配置:用户级 `~/.qoder/settings.json`,另有项目级 `.qoder/settings.json`、`.qoder/settings.local.json`,合并加载,同事件的 hook 不互相覆盖。改文件**立即生效**,无需重启。结构与 Claude 相同。国内版用 `~/.qoder-cn/`。文档没有写改配置目录的环境变量。
- 执行:shell 形式走 `bash -c "<command>"`;`timeout` 单位秒,command 默认 600。
- 事件(23 个)与 Claude 同名,含 `Notification`(`notification_type`:`permission_prompt`、`idle_prompt`、`auth_success`、`elicitation_*`)、`PermissionRequest`、`Stop`。公共字段 `session_id`、`transcript_path`、`cwd`、`hook_event_name`。
- 文档没有提到 hook 信任或审核,也没有提到 Claude 兼容或读取 `~/.claude`。
- 环境变量:设 `QODER_PROJECT_DIR`;是否净化父进程环境,文档未写 `TODO(M0)`。
- 二进制名文档里没写(社区常见 `qodercli`)`TODO(M0)`;只影响 add-host 的"未找到"提示。
- **结论**:与 Qwen 同样处理。另外要决定是否同时支持 `~/.qoder-cn`(见 4.6)。
- **实测(2026-10-12,qodercli 1.1.68,本机)**,录制见 `tests/fixtures/qoder/recorded-1.1.68.jsonl`:
  - 二进制名 `qodercli`;`matcher: ""` 对所有事件都生效;hook 正常执行,面板显示与预期一致。
  - **没有 `SessionStart`**:启动 `qodercli` 不发事件,会话在第一个 `UserPromptSubmit` 时才出现。
  - `permission_prompt` 在授权提示出现时触发(与 `PreToolUse` 同时),payload 带 `details`:`toolName`、`toolUseId`、`input`(工具参数)、`message`("Allow Bash to run: …?")。适配器在没有 `tool_input` 时从 `details.input` 取命令。
  - **Esc 中断不发任何事件**(没有 `Stop`、没有 `idle_prompt`):会话停在"运行中",直到下一个 prompt 或退出(`SessionEnd`,`reason: "other"`)。与 Claude 的已知限制相同(1.6)。
  - **Qoder IDE(1.1.66)也读 `~/.qoder/settings.json` 的 hooks**,事件以 `--agent qoder` 进入同一个分组。与 CLI 的差别:IDE **会发 `SessionStart`**(`source: "startup"`);没打开文件夹时 `cwd` 是 IDE 为每个对话建的临时目录 `~/Documents/Qoder/<日期>/<会话 id 前 8 位>`,所以卡片标题是这串 id;`parent_business_info.product` 是 `"app"`(CLI 是 `"cli"`)。IDE 的 `permission_mode: "auto"` 时没有授权提示。
  - 未文档化的字段:`request_set_id`、`model`(如 `"Efficient"`)、`Stop` 的 `parent_request_set_id` / `parent_business_info`;`PostToolUse.tool_response` 带 `stdout`、`exitCode`、`interrupted` 等。

### 4.4 Gemini CLI [文档 + 源码]

- 配置:`~/.gemini/settings.json` 顶层 `hooks`,结构 `{"<Event>": [{"matcher": "...", "sequential": false, "hooks": [{"type": "command", "command": "...", "name": "...", "timeout": 60000}]}]}`。`timeout` **毫秒**,默认 60000 [文档]。工具事件的 matcher 是正则,生命周期事件是精确字符串,`""` / `"*"` 匹配全部 [文档]。
- 启用:`hooksConfig.enabled` 默认 true [文档:configuration.md]。旧文档提到的 `tools.enableHooks` / `hooks.enabled` 是实验期的开关,已不需要。
- 执行:Linux 上 `bash -c <command>`,同步执行(Gemini 等所有匹配的 hook 跑完才继续)[源码 + 文档]。命令里的 `$GEMINI_PROJECT_DIR`、`$GEMINI_SESSION_ID`、`$CLAUDE_PROJECT_DIR` 等会被**文本替换**[源码:expandCommand],我们的命令里没有这些字样,不受影响。
- 环境:默认原样继承;只有开启 `enableEnvironmentVariableRedaction` 或在 GitHub Actions 里才净化 [源码:environmentSanitization.ts]。净化时白名单外的变量(如 `TMUX`)会丢,只影响终端定位,不影响状态。
- 输出:exit 0 时 stdout 不是 JSON 就当作 `systemMessage`,决定为 allow [源码:convertPlainTextToHookOutput]。我们 stdout 为空,`TODO(M0)` 确认空 `systemMessage` 不会在界面上显示空行。退出码 1 是非阻塞警告;**2 及其他非零码是阻塞**,所以命令末尾的 `|| true` 对 Gemini 同样必要。
- 信任 / folder trust [源码:hookRegistry.ts]:**项目级** hook 有指纹(名称+命令),变化后会警告。另外 `security.folderTrust.enabled` 默认 true;从 settings 合并来的 hook(含用户级)在 `processHooksFromConfig` 里一律按 `ConfigSource.Project` 注册,并且**只有在受信任目录下才注册**。[推断] 在未信任的目录里启动 Gemini 时,我们的用户级 hook 可能一个都不跑,会话就不会显示。`TODO(M0)` A4 必须实测。
- 事件:`SessionStart`(`source`)、`SessionEnd`(`reason`)、`BeforeAgent`(`prompt`)、`AfterAgent`(`prompt`、`prompt_response`)、`BeforeTool` / `AfterTool`(`tool_name`、`tool_input`、`tool_response`)、`Notification`、`BeforeModel`、`AfterModel`、`BeforeToolSelection`、`PreCompress`。公共字段 `session_id`、`transcript_path`、`cwd`、`hook_event_name`、`timestamp` [文档]。
- `Notification.notification_type` 只有 `ToolPermission`,另有 `message`、`details` [文档 + 源码 types.ts]。**没有空闲通知**。
- 映射建议:`SessionStart`→SESSION_START,`BeforeAgent`→PROMPT_SUBMIT,`BeforeTool`/`AfterTool`→TOOL_START/TOOL_END,`Notification`(ToolPermission)→ATTENTION/PERMISSION,`AfterAgent`→STOP,`SessionEnd`→SESSION_END。不装 `BeforeModel`/`AfterModel`/`BeforeToolSelection`(每次模型调用都触发,同步执行会拖慢 Gemini)。
- 中断:用户 Esc 时是否触发 `AfterAgent` 未知 `TODO(M0)`。
- settings.json 允许注释(Gemini 用 `strip-json-comments` 解析 [源码:packages/cli/src/config/settings.ts])。带注释的文件我们按"不是合法 JSON"拒绝修改,不丢用户的注释。
- **结论**:需要独立的 `GeminiAdapter` 和 `gemini_config`;`hookjson.merge_hooks` 能复用,但 timeout 传毫秒,matcher 用 `""`(或省略)。`AgentSpec` 需要表达"`GEMINI_CLI_HOME` 是 `.gemini` 的父目录"(A0 的 `dir_env` 目前假设环境变量就是配置目录本身)。

### 4.5 远程(SSH)

三者都是 CLI,在哪台机器运行,hook 就在哪台机器执行(与 Claude / Codex 相同)[推断:都是本进程 spawn `bash -c`]。远程安装复用 A0 的通用探测脚本即可;Gemini 需要探测脚本支持"父目录型"环境变量。

### 4.6 待你决定 / 待实测

1. Qoder 国内版 `~/.qoder-cn/`:作为单独的 agent(显示名相同),还是只支持国际版?建议先只支持 `~/.qoder`,有需要再加。
2. 三个 agent 的真实录制(A4):每个 agent 一个会话,至少包括一次工具调用、一次授权提示、正常结束和 Esc 中断;Gemini 另外在一个未信任目录里启动一次。
3. 版本号记到 `docs/m0-verification.md`。

---

## 5. 0.0.6 审批(`PermissionRequest`)调研,2026-10-21

查阅日期 **2026-10-21**,本机 Claude Code 2.1.306、codex-cli 0.160.1。标注同上;另加 **[二进制]**:本机 Claude Code 可执行文件(`~/.local/share/claude/versions/2.1.306`,Bun 打包的 JS)里直接读到的逻辑,只记录行为,没有复制代码。来源:

- Claude Code:<https://code.claude.com/docs/en/hooks>("PermissionRequest"、"PreToolUse"、"Run hooks in the background" 各节)。
- Codex:<https://developers.openai.com/codex/hooks>(现跳转到 <https://learn.chatgpt.com/docs/hooks>);仓库 <https://github.com/openai/codex> `main` commit `c7234c1`(2026-10-21):`codex-rs/hooks/schema/generated/permission-request.command.{input,output}.schema.json`、`codex-rs/hooks/src/events/permission_request.rs`、`codex-rs/hooks/src/engine/command_runner.rs`、`codex-rs/core/src/hook_runtime.rs`。

### 5.1 结论一览

| | Claude Code | Codex |
|---|---|---|
| 拦截事件 | `PermissionRequest`(目前未注册,见 3.1) | `PermissionRequest`(已注册,现在只用来显示"需要你") |
| 批准 / 拒绝 | `hookSpecificOutput.decision.behavior`:`allow` / `deny` | 同一格式 [文档 + schema] |
| 不表态 | 退出 0、stdout 为空 → 走正常授权流程 [文档] | 同左 [源码:`parse_completed`,空 stdout 时 `decision = None`] |
| 超时 | 默认 600 秒(未对此事件另设)[文档];超时 = 取消 = 无决定 [二进制:`hook_cancelled`] | 默认 600 秒 [文档];超时 → `error: "hook timed out"` → `Failed`,无决定 → 正常流程 [源码] |
| **终端提示与 hook 的关系** | **并行**:提示立即显示,hook 在后台跑,谁先决定听谁的;人先答则 hook 结果作废 [二进制 + 文档(`canUseTool` 一节)] | **串行**:hook 在"审批界面显示之前"运行,hook 返回(或超时)前终端**不出现**提示 [源码:`permission_request.rs` 文件头注释、`hook_runtime.rs`] |
| 退出码 2 | 不生效,流程不变 [文档] | **stderr 非空时 = 拒绝** [源码];我们始终退出 0,不受影响 |
| 多个 hook | 文档未写合并规则(`PreToolUse` 是 deny > defer > ask > allow) | 任一 deny 即 deny,否则有 allow 即 allow [源码] |
| 载荷里有 `tool_use_id` | **没有**(文档明说 "without `tool_use_id`"),有 `permission_suggestions` | 没有;有 `turn_id` |

两边**都满足不变量 7**:失败、超时、空输出都是"不表态",不会放行。差别在用户体验:Claude 可以"面板和终端同时等",Codex 做不到。

### 5.2 Claude Code

- **输出格式** [文档]:
  ```json
  {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
    "decision": {"behavior": "allow"}}}
  {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
    "decision": {"behavior": "deny", "message": "原因", "interrupt": false}}}
  ```
  `allow` 还可带 `updatedInput`(整体替换工具输入)和 `updatedPermissions`(写入"总是允许"规则);`deny` 的 `message` 给模型看,`interrupt: true` 直接中止回合。**0.0.6 只用裸 `allow` / `deny` + `message`**,不改输入、不写规则。`allow` 之后 deny/ask 规则仍会评估 [文档],所以我们的 allow 不会越过用户自己配的 deny 规则。
- **不表态** [文档]:"A hook that exits 2 without a `decision` object leaves the permission flow unchanged";退出 0 且无输出 = 无决定。`PreToolUse` 一节的原话 "staying silent doesn't approve it" 对 `PermissionRequest` 同样成立 [推断 + 二进制:无 `permissionRequestResult` 的结果被 `continue` 跳过]。
- **何时触发** [文档]:"Runs when Claude Code is about to ask you for permission to use a tool"。不需要授权的调用(规则已允许、`bypassPermissions`、auto 模式放行)不触发。沙箱命令的网络访问请求不触发。
- **竞态(关键)** [二进制]:交互式终端里,权限提示组件建立时就启动一个异步任务跑 `PermissionRequest` hook(`runHooks(...)`,不 await),提示**同时**显示;hook 结果回来时先检查该请求是否还在等待,已被人回答的就丢弃(常量文案 "The request this approval was for had already been withdrawn; the answer applied to nothing.")。传给 hook 执行器的取消信号是**整个回合**的 `abortController`,不是这一次提示的,所以 [推断] 人先在终端回答后,hook 进程不会被杀,会一直跑到自己退出或超时,结果被忽略;只有 Esc 中断回合才取消 hook(`hook_cancelled`)。**待实测**:`scripts/permission-hook-probe.sh`(见 5.4)。
  - 对我们的意义:`--mode wait` 的 hook 在终端已回答后仍可能挂着,占一个 Python 进程直到 `--timeout`;daemon 收不到"终端已回答"的通知,面板卡片要靠后续事件(`PostToolUse` / `PostToolUseFailure` / `Stop`)自己消失,并且这时回复 hook 什么都不影响。
  - 文档在 `--permission-prompt-tool` / `canUseTool`(SDK、IDE 宿主)场景下明说了同样的规则:"the hooks run alongside your host, and whichever decides first applies"。
  - 例外 [二进制]:当"问人"这一决定来自插件(`hooksMayRefuse` 路径),hook 在提示之前先跑一遍,**只认 deny**,allow 被忽略;之后提示照常显示。不影响普通配置。
- **无法显示提示的会话**(`claude -p` 等)[文档]:hook 照样运行;没有 hook 给出决定就拒绝。也就是说 0.0.6 的面板审批对 `-p` 会话也生效(hook 没开或超时则维持现状:拒绝)。
- **超时**:`PermissionRequest` 没有单独默认值,沿用 600 秒 [文档];超时在二进制里走 `hook_cancelled`,与无输出相同。安装器要显式写 `timeout`(秒),hook 自己的 `--timeout` 要比它**小**,这样永远是我们先退出、由我们保证"不表态"。
- `async: true` 的 hook 不能决定任何事 [文档],`PermissionRequest` 条目必须是同步的(现有 fire 模式条目不受影响)。
- `agent` 类型 hook 不支持此事件 [二进制:报错文案],与我们无关。
- 输入 [文档]:`tool_name`、`tool_input`、`permission_suggestions`(可选,与界面选项不完全对应),**没有 `tool_use_id`**;MCP 工具另有 `mcp_server`。面板卡片与这次请求的对应只能靠 `session_id` + `tool_name` + `tool_input`。
- `hook_events()` 里加 `PermissionRequest` 后,该条目需要与 fire 模式条目分开(`--mode wait`、同步、显式 `timeout`);功能关闭时不写入这一条(或 daemon 一律立刻回"不表态")。

### 5.3 Codex

- **能返回决定** [文档 + schema]:输出与 Claude 同形;`behavior` 只有 `allow` / `deny`,`message` 可选。`updatedInput`、`updatedPermissions`、`interrupt: true` 目前 **fail closed**(schema 注释:"PermissionRequest hooks currently fail closed if this field is present")——不要发。
- **触发时机** [源码]:`permission_request.rs` 文件头:"This event runs in the approval path, before guardian or user approval UI is shown";`hook_runtime.rs::run_permission_request_hooks` 先 `emit_hook_started_events`,然后 `await hooks.run_permission_request(...)`,拿到 `Option<decision>` 才继续。即 **hook 跑完之前终端不弹授权提示**;TUI 期间显示 hook 运行中的行 [源码:preview + started 事件,具体界面待实测]。
  - 这与 3.5 里的实测一致:当时录到 `PermissionRequest` 在提示出现时触发(fire 模式约 1 秒内返回,看起来像同时)。
  - 对我们的意义:Codex 上 `--mode wait` 等多久,终端就黑多久。只能二选一:(a)Codex 不做面板审批,保持 fire 模式;(b)短超时(如 10–30 秒)内面板可答,超时后终端再问——用户在终端前时会多等这段时间。你已决定(2026-10-21)**Codex 一起做**,走 (b):每个 agent 一个等待上限,Codex 的短(建议 30 秒,可调),Claude 的长;到时回"不表态"。
- **不表态** [源码:`parse_completed`]:退出 0 且 stdout 空白 → 无决定;stdout 像 JSON 但解析失败 → `Failed`,无决定;退出 0 的纯文本 stdout 被忽略 [文档]。
- **退出码 2 + 非空 stderr = 拒绝** [源码]。我们的 hook 任何情况下都退出 0 且不写 stderr(安装器还加了 `2>/dev/null || true`),所以不会误拒绝;但这是 Codex 与 Claude 的一个差异,改 hook 脚本时要记得。
- **超时** [源码:`command_runner.rs`]:`tokio::time::timeout(handler.timeout_sec)` 到期 → `error: "hook timed out after Ns"` → `Failed` → 无决定 → 正常审批流程。默认 600 秒 [文档]。
- **多个 hook** [源码:`resolve_permission_request_decision`]:任一 deny 即 deny;否则有 allow 即 allow;都没决定 → `None`。
- **信任**:改 hooks.json 里 `PermissionRequest` 条目的命令(加 `--mode wait`)会改变 hash,需要在 `/hooks` 里**重新信任**(2.3、3.5)。
- 输入 [schema]:`session_id`、`turn_id`、`tool_name`、`tool_input`(`Bash` / `apply_patch` 用 `tool_input.command`;另有 `tool_input.description` = 审批原因)、`model`、`permission_mode`、`cwd`、`transcript_path`;没有 `tool_use_id`。
- 适用范围 [源码]:在 core 里执行,TUI、`codex exec`、app-server(IDE)都走同一条路。

### 5.4 待你实测(👤,PR 里会再列一遍)

1. **Claude 竞态**:在一个临时项目目录的 `.claude/settings.local.json` 里注册 `scripts/permission-hook-probe.sh`(脚本头部有配置片段;不要动 `~/.claude`),`claude` 里问一个要跑命令的问题。看:提示是否在 hook 结束前就出现(应该是);在终端批准后,`/tmp/permission-hook-probe.log` 里是 `signal TERM`(hook 被杀)还是 40 秒后的 `end`(推断是后者);Esc 中断时是否 `signal TERM`。
2. **Codex 串行**:同一脚本放进 `~/.codex/hooks.json` 的 `PermissionRequest`,`/hooks` 信任后触发一次授权。确认 40 秒内终端**不**出现授权提示、TUI 上有没有"hook 运行中"的提示;然后决定 Codex 走 5.3 的 (a) 还是 (b)。
3. 两边都确认:hook 睡满 40 秒不输出退出后,终端照常询问(不表态 ≠ 放行)。
4. Claude 的 `permission_suggestions` 和 Codex 的 `tool_input.description` 实际长什么样(录一份,给面板用)。

### 5.5 对 0.0.6 设计的直接约束(补充 PLAN.md §3 的列表)

- hook `--timeout` < 安装器写入的 `timeout`,两者都由安装器显式写;面板审批超时后终端照常询问。
- daemon 的 `wait` 回复只需三种:`allow`、`deny`(+ 给模型的 `message`)、无(hook 自己超时退出)。协议里把 `deny` 的 `message` 固定为纯文本,不回传任何载荷内容。
- 用户在终端先回答时 daemon 不会得到通知(Claude、Codex 都没有"提示已关闭"事件):面板卡片用 `PostToolUse` / `PostToolUseFailure` / `Stop` / 下一个 `PermissionRequest` 来收起;等待中的 hook 让它自然超时。
- 一个会话同一时刻只会有一个等待中的授权(Claude 工具按顺序请求授权;Codex 串行)[推断,实测 5.4 时顺带看并行工具调用的情况]。
- 等待上限按 agent 区分(5.3):Claude 长、Codex 短;hook 的 `--timeout` 和安装器写的 `timeout` 都从注册表的同一个值派生。
