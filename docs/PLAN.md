# Agentail 实施方案

本文件是给**执行者**(Cloud session 或你自己)的操作手册。设计背景和取舍见 [research-and-design.md](research-and-design.md)(调研时项目名还叫 "island",文中的 `island-*` 对应现在的 `agentail-*`,以本文件和代码为准)。

---

## 0. 当前状态(M4 完成,待 👤 确认菜单外观)

| 模块 | 状态 |
|---|---|
| `resources/agentail-hook.py` 哑管道 hook 脚本 | ✅ 完成并有测试(fire 模式、ping、截断、环境变量白名单、所有失败路径静默退出 0) |
| `protocol.py` 线协议 v1 | ✅ 完成并有测试 |
| `daemon/ingest.py` socket 监听 | ✅ 完成并有测试(目录 0700、socket 0600、坏消息不影响服务) |
| `daemon/state.py` 状态机 | ✅ 完成并有测试(按 (主机, agent, session) 区分、乱序容忍、离线、清理) |
| `adapters/claude.py` | ✅ 字段名已按官方文档核对(M0-b);M1 真实录制(2.1.285)已加入测试,未知字段一律忽略、字段类型不对也忽略 |
| `adapters/codex.py` | ✅ M2:按 `agent-hooks-notes.md` §3.2 实现(8 个事件,PermissionRequest → 需要授权,Interrupt → 回合结束);已真机验证(codex-cli 0.160.0 录制已加入测试) |
| `install/claude_config.py` 合并/移除 | ✅ 完成并有测试(幂等、保留用户条目、可完全还原) |
| `daemon/main.py` + `agentail daemon --print-events --record` | ✅ 可运行;`--record` 不再录制 ping;同一 ui.sock 上拒绝启动第二个 daemon |
| `install/local.py`、`install/codex_config.py` | ✅ M2:`install-local` / `uninstall-local`(`--dry-run`、备份、原子写入、manifest 还原原文件);Codex 写 `~/.codex/hooks.json`;hook 命令带 `2>/dev/null \|\| true`,脚本被删后旧会话也不会被阻塞 |
| `install/remote.py` + `sshexec.py` | ✅ M3:`add-host` / `remove-host`(`--dry-run`、`--local-only`);一次 ssh 探测;本地合并、远程备份 + 原子写入、服务器端 manifest 逐字节还原;共享 NFS HOME 用 `~/.agentail/home-id` 识别,只装一次、删最后一台时才卸;所有远程命令是固定 `sh -c` 脚本 + quote 过的参数 |
| `daemon/tunnels.py` | ✅ M3:`TunnelSupervisor`(先 `rm -f` 再 `-R`;CONNECTING → 端到端 ping 到达才 CONNECTED → 断线 BACKOFF 1s..60s,稳定 60s 重置;认证失败 AUTH_FAILED 不重试;断线回调把该主机会话标 stale) |
| `daemon/uiapi.py` + `agentail tail` / `status` | ✅ M2:快照 + 增量,每客户端有界队列,慢客户端直接断开 |
| `daemon/main.py` 多主机 | ✅ M3:每台主机一个 listener + 隧道;每 2 秒检查 hosts.toml,`add-host` / `remove-host` 不用重启 daemon;新增 UI 消息 `host_remove` |
| 测试用假 ssh(`tests/fakessh.py`) | ✅ 在本机临时目录里"远程"执行命令,`-R` 用真实 socket 中转;场景:正常、认证失败、立即退出、运行中断开 |
| `ui/*` + `agentail ui` | ✅ M4:顶栏指示器(AppIndicator,Ubuntu 默认启用的扩展显示):图标按状态变化(idle / busy / 橙色 attention / offline),旁边紧凑文字如 `⚠1 ▶2 ⏸1 ✕1`,下拉菜单按主机列出会话;`ui/model.py` 纯函数有测试;`ui/notify.py` libnotify,退路 `notify-send`,正文先转义。最初做的顶部胶囊窗口已按你的意见删除(像 macOS 刘海,在 GNOME 上会盖住窗口、和通知抢位置、Wayland 下也用不了)。venv 需 `include-system-site-packages = true` |
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

✅ 已完成(2026-10-05,Claude Code 2.1.289、codex-cli 0.160.0):两边状态流转正确;`uninstall-local` 后 `settings.json` 逐字节恢复;Codex 的 `PermissionRequest` 在授权提示时触发。发现并修复:卸载后旧会话调用已删除脚本退出码 2 → Claude 当作阻塞。细节见 `agent-hooks-notes.md` §3.5。注意 Codex 必须先在 `/hooks` 里信任 agentail 条目才会执行;信任条目写在 `config.toml`,卸载后会留下。

### M3 多主机 ☁️ + 👤

任务:

1. `daemon/tunnels.py`:`TunnelSupervisor.run/stop`(asyncio 子进程;先 cleanup 再 `-R`;状态机 CONNECTING/CONNECTED/BACKOFF/AUTH_FAILED;连上后等端到端 ping 才算 CONNECTED;断开时回调让 Store 标 stale)。ssh 可执行文件可注入,测试用假 ssh 脚本模拟:正常、立即退出、认证失败、运行中断开。
2. `install/remote.py`:`agentail add-host <alias>` / `remove-host`,流程见该文件文档字符串。所有远程调用走一个"argv 列表 → ssh"的辅助函数,测试里替换成假实现。
3. daemon 启动时为 hosts.toml 里每台主机建 listener + supervisor;`agentail status` 显示每台主机的隧道状态。
4. 测试覆盖:退避计算、认证失败不重试、两台主机相同 session_id 不冲突、hosts.toml 写入保留注释。

> 提示词(M3):
> 阅读 CLAUDE.md、docs/PLAN.md、docs/research-and-design.md 第 7 节、docs/m0-verification.md。完成 PLAN.md 中 M3 的 1–4 项。你无法连接真实服务器:所有 ssh 调用必须可注入并用假 ssh 脚本测试。远程命令只能用 argv 列表构造,任何来自载荷或配置的字符串都要 shlex.quote。在分支 m3-multi-host 上提交并开 PR,PR 描述里列出我需要在真实服务器上做的验证步骤。

👤 合并后在真实服务器上验证(gpu0–gpu12 共享 NFS HOME `/data/twye`,uid 10006):

```bash
agentail daemon --print-events                  # 终端 A,一直开着
agentail tail                                    # 终端 B

# 1. 先只读预览(探测 + 读取配置,不写任何东西)
agentail add-host gpu7 --dry-run
# 2. 真正安装;daemon 在跑时会等到 "gpu7: connected (end-to-end ping received)"
agentail add-host gpu7
agentail add-host gpu8                           # 共享 HOME:应显示 "already up to date" 和 "shares its $HOME"
agentail status                                  # gpu7 / gpu8 都是 connected
# 3. 在 gpu7、gpu8 和本机各跑一次 claude(gpu7 再跑一次 codex,先 /hooks 信任),
#    status 里三处会话分开显示
# 4. 恢复测试:在服务器上 `pkill -f 'agentail.sock'` 或本机 kill 对应 ssh 进程;
#    拔网线 / 休眠唤醒。应看到 backoff → connected,断开期间该主机会话变 stale
# 5. remove-host gpu8(应提示共享 HOME、不动远程配置),再 remove-host gpu7,
#    确认 ~/.claude/settings.json、~/.codex/hooks.json 逐字节恢复、~/.agentail 被删
```

重点看:`add-host` 探测出的 python 路径(应为 `/usr/bin/python3`);远程 hook 命令在 Claude 的 `sh -c` 和 Codex 的 `$SHELL -lc` 下都能找到 `~/.agentail/agentail-hook.py`;已知限制:登录 shell 是 csh/tcsh 的服务器上多行远程脚本可能失败(M0 的 13 台都是 bash)。

✅ 已验证(2026-10-07,gpu7 + gpu8,共享 NFS HOME `/data/twye`,Claude Code 远程版本见服务器):

- `add-host gpu7`:探测到 `/usr/bin/python3` 3.10.12、`/run/user/10006` 可用;安装后约 4 秒 `connected (end-to-end ping received)`。
- `add-host gpu8`:识别出共享 HOME(`already up to date` + `shares its $HOME with gpu7`),同样 connected。
- 在 gpu7、gpu8 各跑一次 `claude -p`:事件分别归到 `gpu7/claude/…`、`gpu8/claude/…`,状态 waiting_input → running → [Bash] → waiting_input → ended。gpu7 上已经在跑的 Claude 会话(热加载设置后)也被识别。
- 本机 `kill -9` gpu7 的隧道 ssh:1 秒内 backoff,2 秒内重新 connected;远程残留 socket 被自动清理;断线期间 gpu7 会话为 stale。
- `remove-host gpu8`(提示共享 HOME、不动配置)→ `remove-host gpu7`:`settings.json` sha256 与安装前一致,`hooks.json`、`~/.agentail`、远程 socket 均已删除,隧道停止。
- 发现并修复:共享 HOME 时 `remove-host` 没删该节点自己的 `/run/user` socket。
- 未验证:远程 Codex(需要在服务器上交互式 `/hooks` 信任);拔网线 / 休眠唤醒(`ServerAliveInterval=15 × 3`,理论上约 45 秒内发现断线并重连)。

### M4 界面 ☁️ + 👤

任务:`agentail ui`。*实际实现*:顶栏指示器(AyatanaAppIndicator3)+ 系统通知,而不是原计划的置顶胶囊窗口。显示逻辑全部在 `ui/model.py`(纯函数、有测试);`ui/indicator.py` 只做渲染;所有来自载荷的文字都是纯文本。

✅ 已验证(2026-10-07,Ubuntu 24.04 GNOME 46 X11):图标和文字出现在顶栏右侧;本地会话运行时显示 `▶1`;模拟授权请求时图标变橙、显示 `⚠1`,并弹出 "claude needs you — this computer" 通知;回合结束弹出 "claude finished";daemon 停止时图标变虚线圈,重启后自动重连;通过 dbusmenu 读出的菜单内容正确。
发现并修复:菜单最初点不开(启动时交给扩展的菜单是空的,之后每秒删掉重建菜单项),改为启动时建好固定菜单项、之后只原地改文字,用 XTest 模拟点击确认能打开;结束的会话合并成一行 "✓ N ended recently"。
发现并绕过:GNOME 的 AppIndicator 扩展只在 label 变化时刷新,启动时设置的第一个值会丢,所以连接后会再发两次 label。已知外观问题:扩展只还原第一个下划线,菜单里 `a_b_c` 会显示成 `a_b__c`(上游 bug)。

👤 仍需你确认:点开菜单看排版是否可读;"needs you" 通知(critical,停留到手动关闭)是否合适;长时间运行是否稳定。

v1 之后:GNOME Shell 扩展前端(可做自定义下拉面板,也支持 Wayland),直接读 `ui.sock`。

### M5 打磨 ☁️

systemd user service(`agentail install-service`,daemon 与 ui 各一个 unit)、README 的安装与使用说明、`agentail doctor`(检查 socket、hook、隧道)、版本号与 CHANGELOG。

---

## 4. v1 之后(不在当前范围)

Claude Code 审批(`--mode wait`,约束见设计文档 6.4)→ Claude 兼容 fork(Qoder、Qwen)→ Gemini CLI → Cursor → 插件类 agent;GNOME Shell 扩展前端(Wayland);集群计算节点的 JSONL 回退方案。
