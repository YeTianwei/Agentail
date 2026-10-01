# Agentail 实施方案

本文件是给**执行者**(Cloud session 或你自己)的操作手册。设计背景和取舍见 [research-and-design.md](research-and-design.md)(调研时项目名还叫 "island",文中的 `island-*` 对应现在的 `agentail-*`,以本文件和代码为准)。

---

## 0. 当前状态(M2 之后)

| 模块 | 状态 |
|---|---|
| `resources/agentail-hook.py` 哑管道 hook 脚本 | ✅ 完成并有测试(fire 模式、ping、截断、环境变量白名单、所有失败路径静默退出 0) |
| `protocol.py` 线协议 v1 | ✅ 完成并有测试 |
| `daemon/ingest.py` socket 监听 | ✅ 完成并有测试(目录 0700、socket 0600、坏消息不影响服务) |
| `daemon/state.py` 状态机 | ✅ 完成并有测试(按 (主机, agent, session) 区分、乱序容忍、离线、清理) |
| `adapters/claude.py` | ✅ 字段名已按官方文档核对(M0-b);M1 真实录制(2.1.285)已加入测试,未知字段一律忽略、字段类型不对也忽略 |
| `adapters/codex.py` | ✅ M2:按 `agent-hooks-notes.md` §3.2 实现(8 个事件,PermissionRequest → 需要授权,Interrupt → 回合结束);待 👤 真机验证 |
| `install/claude_config.py` 合并/移除 | ✅ 完成并有测试(幂等、保留用户条目、可完全还原) |
| `daemon/main.py` + `agentail daemon --print-events --record` | ✅ 可运行;`--record` 不再录制 ping;同一 ui.sock 上拒绝启动第二个 daemon |
| `install/local.py`、`install/codex_config.py` | ✅ M2:`install-local` / `uninstall-local`(`--dry-run`、备份、原子写入、manifest 还原原文件);Codex 写 `~/.codex/hooks.json` |
| `install/remote.py` | ⬜ 只有接口说明(M3) |
| `daemon/tunnels.py` | ⬜ 常量与 argv 构造已写,`run()` 待实现 |
| `daemon/uiapi.py` + `agentail tail` / `status` | ✅ M2:快照 + 增量,每客户端有界队列,慢客户端直接断开 |
| `ui/*` | ⬜ 只有接口说明(M4) |
| `scripts/m0-check-host.sh` | ✅ 服务器实测脚本(只读 + 一个临时 socket) |

本地验证:`pip install -e '.[dev]' && python -m pytest && ruff check . && ruff format --check .`

---

## 1. 执行方式

- **每个里程碑开一个 Cloud session**,在分支 `m<N>-<简述>` 上工作,完成后开 PR,由你审阅合并。
- **Cloud session 做不到的事**(标 👤):它连不到你的服务器、你的桌面、你的 GNOME 界面。凡是需要真实服务器、真实 agent 会话、肉眼看界面的步骤,都由你在本机完成,然后把结果贴回仓库(`docs/m0-verification.md` 或 PR 评论)。
- 每个 session 开始前读 `CLAUDE.md`(不变量和约定)。
- 下面每个里程碑末尾都有一段**可直接粘贴给 Cloud session 的提示词**。

---

## 2. 不变量(任何改动都不能破坏)

1. **hook 脚本:fail-open = 不干预。** 任何失败都不输出、退出码 0、最多阻塞 `--timeout` 秒。不含 agent 逻辑;只用标准库;兼容 Python 3.6。
2. **来源主机只由 socket 决定**,不信任 payload 里的任何主机信息。
3. **环境变量白名单**(`protocol.ENV_WHITELIST`,hook 脚本里有同步副本,测试会检查)。
4. **远程载荷不可信**:UI 只用纯文本渲染;载荷字段绝不拼进 shell 命令;远程命令一律 argv 列表 + `shlex.quote`。
5. **配置文件只合并不覆盖**:识别自己条目靠 `agentail-hook` 标记;写回前备份;卸载后能恢复原样。
6. **不复制 GPL 代码**(open-vibe-island)和 vibe-island 的任何代码。项目是 MIT。
7. **v1 不做审批**(不实现 `--mode wait`)。

---

## 3. 里程碑

### M0 实测(先做,两部分可并行)

**M0-a 👤 你在本机做**

```bash
git clone https://github.com/YeTianwei/Agentail && cd Agentail
pip install -e '.[dev]' && pytest            # 确认本机环境
claude --version; codex --version            # 记录版本
./scripts/m0-check-host.sh <服务器别名>       # 每台服务器跑一次,包括 SoC 集群的具体登录节点
```

把每台服务器的输出贴进 `docs/m0-verification.md`。重点看:`OK: remote -> local unix socket forwarding works` 是否出现;`run_user` 是 yes 还是 no;`home_fs` 是否是 nfs;python3 版本是否 ≥ 3.6。

**M0-b ☁️ Cloud session 做:查文档,写 `docs/agent-hooks-notes.md`**

内容:Claude Code 和 Codex 当前的 hook 机制,每条都附官方文档链接和查阅日期。

- Claude Code:settings.json 中 hooks 的结构;每个事件的 stdin 字段(`session_id`、`cwd`、`hook_event_name`、`prompt`、`tool_name`、Notification 是否有类型字段);hook 默认 timeout;空输出退出码 0 的语义。
- Codex:hook 在哪里配置(config.toml / hooks 文件 / 插件)、有哪些事件、payload 字段、是否需要 `/hooks` 手动信任、`notify` 的参数格式。
- 结论:`adapters/claude.py` 需要改什么;`adapters/codex.py` 和 `install/codex_config.py` 应该怎么实现。

> 提示词(M0-b):
> 阅读 CLAUDE.md 和 docs/PLAN.md。完成 PLAN.md 中 M0-b:查阅 Claude Code 和 OpenAI Codex CLI 当前的官方 hook 文档,写 docs/agent-hooks-notes.md,每条结论附链接和日期,标明哪些是文档明确写的、哪些是推断。然后按文档修正 adapters/claude.py 的字段名和 tests/fixtures/claude/synthetic.jsonl,并更新测试。不要实现 Codex 适配器,只写出实现建议。在分支 m0-hook-notes 上提交并开 PR。

**完成标准**:`docs/m0-verification.md` 有每台服务器的结果;`docs/agent-hooks-notes.md` 合并。

### M1 本地链路 👤(脚手架已完成代码部分)

你在本机手动验证一次:

```bash
agentail daemon --print-events --record recordings/     # 终端 A
agentail paths                                           # 看 local.sock 路径
```

在 `~/.claude/settings.json` 里手工加一个 Stop 和 UserPromptSubmit hook(命令用 `agentail hook-path` 给出的路径,`--sock` 用 `local.sock`),然后跑一次 Claude Code。终端 A 应打印事件和状态变化。`recordings/` 里的内容就是真实载荷(已在 .gitignore 中,**提交前先脱敏**,放进 `tests/fixtures/claude/`)。

**完成标准**:终端 A 看到 `prompt_submit` → `running` → `stop` → `waiting_input`。

### M2 本地安装器 + Codex + tail/status ☁️

任务:

1. `install/local.py`:`agentail install-local [--dry-run]` / `uninstall-local`。复制 hook 到 `~/.agentail/`(0700),备份 settings.json,原子写入(tmp + rename)。
2. `daemon/uiapi.py`:`UiServer`,协议见 `docs/protocol.md`;慢客户端丢弃而不是阻塞 daemon。
3. `agentail tail`(实时打印事件/状态变化)和 `agentail status`(当前会话表 + 主机状态),都是 ui.sock 的客户端。
4. 按 M0-b 结论实现 `adapters/codex.py`、`install/codex_config.py`(tomlkit;顶层键插在第一个表之前;`notify` 已存在时串联)。
   *实际实现(M2)*:按 `agent-hooks-notes.md` §3.3 的结论改为写 `~/.codex/hooks.json`,`config.toml` 只读不写(只用来提示 `features.hooks = false` 或内联 `[hooks]`),所以没有用到 tomlkit;`notify` 串联按 §3.3 建议暂不做。
5. 测试:安装/卸载往返(用临时 HOME)、UiServer 快照 + 增量、Codex 配置合并。

> 提示词(M2):
> 阅读 CLAUDE.md、docs/PLAN.md、docs/agent-hooks-notes.md。完成 PLAN.md 中 M2 的 1–5 项。所有文件操作要在测试里用临时 HOME 验证,不得触碰真实的 ~/.claude 或 ~/.codex。保持 pytest 和 ruff 全绿。在分支 m2-local-install 上提交并开 PR,PR 描述里写清哪些需要我在本机验证。

👤 合并后你在本机:`agentail install-local`,开 `agentail daemon` 和 `agentail tail`,分别跑一次 Claude Code 和 Codex,确认状态正确;再 `uninstall-local`,确认配置文件恢复原样。

### M3 多主机 ☁️ + 👤

任务:

1. `daemon/tunnels.py`:`TunnelSupervisor.run/stop`(asyncio 子进程;先 cleanup 再 `-R`;状态机 CONNECTING/CONNECTED/BACKOFF/AUTH_FAILED;连上后等端到端 ping 才算 CONNECTED;断开时回调让 Store 标 stale)。ssh 可执行文件可注入,测试用假 ssh 脚本模拟:正常、立即退出、认证失败、运行中断开。
2. `install/remote.py`:`agentail add-host <alias>` / `remove-host`,流程见该文件文档字符串。所有远程调用走一个"argv 列表 → ssh"的辅助函数,测试里替换成假实现。
3. daemon 启动时为 hosts.toml 里每台主机建 listener + supervisor;`agentail status` 显示每台主机的隧道状态。
4. 测试覆盖:退避计算、认证失败不重试、两台主机相同 session_id 不冲突、hosts.toml 写入保留注释。

> 提示词(M3):
> 阅读 CLAUDE.md、docs/PLAN.md、docs/research-and-design.md 第 7 节、docs/m0-verification.md。完成 PLAN.md 中 M3 的 1–4 项。你无法连接真实服务器:所有 ssh 调用必须可注入并用假 ssh 脚本测试。远程命令只能用 argv 列表构造,任何来自载荷或配置的字符串都要 shlex.quote。在分支 m3-multi-host 上提交并开 PR,PR 描述里列出我需要在真实服务器上做的验证步骤。

👤 合并后:对两台服务器 `agentail add-host`;两台 + 本地同时跑 agent,`agentail status` 能正确区分;拔网线 / 休眠唤醒 / `kill` ssh 进程后能自动恢复;`remove-host` 后远程配置恢复原样。

### M4 界面 ☁️ + 👤

任务:`agentail ui`,GTK3 胶囊 + 按主机分组的列表 + 主机连接灯 + 系统通知,细节见 `ui/panel.py`、`ui/notify.py` 的文档字符串和设计文档第 9 节。把"UI 状态"(显示什么)写成纯函数并测试;GTK 部分保持薄。

> 提示词(M4):
> 阅读 CLAUDE.md、docs/PLAN.md、docs/research-and-design.md 第 9 节。完成 M4:实现 agentail ui(PyGObject + Gtk 3.0)和桌面通知。把从快照计算显示内容的逻辑写成纯函数并测试;GTK 代码保持薄,无法在云端显示的部分写清手动验证步骤。所有来自载荷的字符串只能用 set_text 渲染。在分支 m4-ui 上提交并开 PR。

👤 合并后:在 GNOME + X11 上看位置、置顶、不抢焦点、通知是否正常;截图贴到 PR。

### M5 打磨 ☁️

systemd user service(`agentail install-service`,daemon 与 ui 各一个 unit)、README 的安装与使用说明、`agentail doctor`(检查 socket、hook、隧道)、版本号与 CHANGELOG。

---

## 4. v1 之后(不在当前范围)

Claude Code 审批(`--mode wait`,约束见设计文档 6.4)→ Claude 兼容 fork(Qoder、Qwen)→ Gemini CLI → Cursor → 插件类 agent;GNOME Shell 扩展前端(Wayland);集群计算节点的 JSONL 回退方案。
