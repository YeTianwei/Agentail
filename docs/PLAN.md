# Agentail 实施方案

本文件是给**执行者**(Cloud session 或你自己)的操作手册。设计背景和取舍见 [research-and-design.md](research-and-design.md)(调研时项目名还叫 "island",文中的 `island-*` 对应现在的 `agentail-*`,以本文件和代码为准)。

---

## 0. 当前状态(0.0.1 候选:M0–M5 完成,界面重做和 deb 打包完成,待发布)

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
| GNOME Shell 扩展(主界面) | ✅ M4:`resources/gnome-extension/agentail@yetianwei.github.io/`,`agentail install-gnome-extension [--link]` / `uninstall-gnome-extension`。顶栏 A 字标 + 彩色数字徽标(橙=需要你、蓝=运行中、绿=轮到你,有主机断线时加红点,需要你时橙色描边 + 徽标呼吸);下拉面板按主机分组的会话卡片(状态图标 + 官方 agent 图标、项目名、prompt、授权时显示具体命令);GNOME 原生通知(带 agent 图标)。显示逻辑在 `model.js`(纯函数,`tests/js/test_model.js` 用 gjs 跑)。官方图标在 `icons/`,附 `NOTICE.md`,不在 MIT 范围内 |
| `ui/*` + `agentail ui` | ✅ AppIndicator 退路(非 GNOME 桌面或没装扩展时用):图标 + `⚠1 ▶2 ⏸1` 文字 + 菜单;`ui/model.py` 纯函数有测试 |
| `scripts/m0-check-host.sh` | ✅ 服务器实测脚本(只读 + 一个临时 socket) |
| `install/service.py` + `agentail install-service` | ✅ M5:systemd 用户服务,绑定 `graphical-session.target`(登录后启动,拿到 SSH_AUTH_SOCK),失败自动重启,已有 daemon 时退出码 3 不重启 |
| `doctor.py` + `agentail doctor` | ✅ M5:daemon、开机自启、hook 脚本版本、Claude/Codex hooks、Codex 信任、每台主机隧道、GNOME 扩展,逐项给出修复命令 |
| README / CHANGELOG / 版本 | ✅ 版本号 0.0.1(早先的 1.0.0 作废:功能齐了但没人能一键安装) |
| 顶栏灵动岛胶囊 + 按 agent 分组的面板 | ✅ 0.0.1:`extension.js` / `stylesheet.css` 重写;`model.js` 新增 `agentGroups` / `hostRows`;嵌套 GNOME Shell 46 截图核对 |
| `.deb` 打包 | ✅ 0.0.1:`packaging/build-deb.sh`(只依赖 dpkg-deb);装上就用:daemon 首次启动自动装 hook、启用扩展(`firstrun.py`,`--auto-setup`),只需重新登录一次;`agentail setup` 作手动版本 |
| 面板「Servers → ＋ Add server」 | ✅ 0.0.1:从 `~/.ssh/config` 选或手输别名,扩展用 argv 列表调 `agentail add-host`,结果显示在面板里(`list-ssh-hosts`、`sshconfig.py`);每台服务器一行有 ✕(先确认,连不上时可「Forget it anyway」= `--local-only`) |

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

*实际实现*:经过两轮调整(置顶胶囊窗口 → AppIndicator → GNOME Shell 扩展),主界面是 **GNOME Shell 扩展**,视觉稿见 https://claude.ai/artifact/Fpwsrtf2Yykw6J8hiEN1G7 。AppIndicator 版 `agentail ui` 保留作退路。扩展只读 `ui.sock`,daemon 和协议不变(只新增 `SESSION.tool_detail`)。

✅ 已验证(2026-10-07,嵌套 GNOME Shell 46,`dbus-run-session -- gnome-shell --nested --wayland`,独立 dconf 配置):扩展加载无报错;顶栏字标 + 徽标随状态变化;面板按主机分组、需要授权的卡片显示具体命令(Claude 取自之前的 PreToolUse,Codex 取自 PermissionRequest);通知横幅带 Codex/Claude 官方图标;daemon 重启后自动重连。

过程中发现并修复:symbolic SVG 会被 GNOME 强制填充(字标改用普通彩色 SVG);St 的 `min-width` 不含内边距(徽标过宽);叠加式滚动条压住文字。

👤 需要你:`agentail install-gnome-extension` 后按 Alt+F2 → `r` → 回车重载 Shell(X11),确认顶栏和面板;不要同时运行 `agentail ui`(会重复发通知)。

### M5 打磨 ☁️

✅ 已完成(2026-10-07):`agentail install-service` / `uninstall-service`(systemd 用户服务)、`agentail doctor`、README、CHANGELOG;面板里没有会话的远程主机也显示为一行("connected · idle")。只有 daemon 一个服务:界面是 GNOME 扩展,由 Shell 加载。

已在本机落地:`agentail.service` 运行中(两条隧道是它的子进程),`agentail doctor` 全部通过。注意 service 的 ExecStart 指向当前 venv 的 python,移动仓库目录后要重新 `agentail install-service`。

👤 仍未验证:远程 Codex(在服务器上 `/hooks` 信任一次)、拔网线 / 休眠唤醒后的重连、重启电脑后 daemon 自动启动。

### 0.0.1 发布 ☁️ + 👤

范围:Claude Code + Codex 的状态显示(本机和 SSH 服务器)、灵动岛胶囊顶栏、按 agent 分组的面板、`.deb`。**用量监控放到 0.0.2,Claude / Codex 以外的 agent 放到 0.0.3。**

☁️ 已完成:界面重做(见上表)、`.deb`(`packaging/build-deb.sh`、`agentail setup`、系统级 systemd 用户单元、GNOME 扩展装进 `/usr/share/gnome-shell/extensions`)、对应测试。

👤 发布前你要做的:

```bash
git pull && packaging/build-deb.sh              # 生成 dist/agentail_0.0.1.deb
sudo apt install ./dist/agentail_0.0.1.deb  # 在干净的用户或另一台 Ubuntu 24.04 上最好
# 不需要别的命令;注销再登录一次让 Shell 加载面板;agentail doctor 可随时检查
```

确认:顶栏胶囊(空闲 / 运行中 / 需要你 / 主机断线)、面板、通知;`sudo apt remove agentail` 后 `agentail uninstall-local` 能还原配置。满意后:

```bash
git tag v0.0.1 && git push origin v0.0.1        # .github/workflows/release.yml 构建并附到 Release
```

(工作流没在 GitHub 上跑过;失败时用 `gh release create v0.0.1 dist/agentail_0.0.1.deb` 手动发。)

仍未验证(沿用 M5):远程 Codex 信任、拔网线 / 休眠唤醒后重连、重启电脑后自启动。

### 0.0.2(下一步)

只做用量监控(Claude 5 小时 / 7 天额度、Codex 每周额度,参考 macOS 版面板底部的"用量"区)。需要先调研各家额度信息从哪里取、是否有稳定的本地来源。**不加新 agent**,全部放到 0.0.3。

✅ 已完成(2026-10-08,Claude Code 2.1.290、codex-cli 0.160.1),调研见 `docs/usage-notes.md`,设计见 `docs/usage-design.md`:

- Claude:安装器包装 `statusLine`(保留用户原命令,卸载还原),额度经现有 hook 信封和隧道送达,服务器上的 Claude 也能上报。
- Codex:daemon 读本机 `~/.codex/sessions` 的 `token_count`;面板打开时再用 `codex app-server` 的 `account/rateLimits/read` 主动查一次(每分钟最多一次)。都不读登录凭据。
- 面板 Usage 区(默认展开,70% / 90% 变色,30 分钟置灰,窗口重置后不显示旧值);`agentail status` / `tail`、AppIndicator 菜单、`doctor` 检查项;用量跨 daemon 重启保存。
- 顺带:`[sessions] ignore_cwd`,默认隐藏 CodexBar 的探测会话。
- 已在本机验证:Claude 与 Codex 两张卡、打开面板约 1 秒刷新 Codex。
- 服务器上的 Claude 用量(2026-10-08,gpu7,共享 NFS HOME 所以 gpu8 同时生效):`add-host gpu7` 重装后包装器写入服务器 `settings.json`,服务器上交互式 Claude 回答后 daemon 收到 `usage claude/gpu7`,服务器状态栏显示正常。
- statusLine 开销实测:包装器每次约 19 ms(用户命令本身约 1 ms,多出的几乎都是 Python 启动和 import);daemon 没在运行时也一样,不会卡住。Claude 只在新消息、`/compact` 等时触发并有 300 ms 防抖,所以不需要去重。

暂不做(见 `usage-design.md` §8):多账号区分、服务器上的 Codex 用量。

✅ 亮色主题下的 Usage 区已由你确认(2026-10-08)。0.0.2 的用量功能全部验证完毕。

### 0.0.2 发布 ☁️ + 👤

☁️ 已完成:版本号 0.0.2、CHANGELOG、README(Usage limits 一节、路线图)、`.github/release-notes/v0.0.2.md`、`dist/agentail_0.0.2.deb`。
升级时 `firstrun.SETUP_VERSION = 2` 让已装 hook 的 Claude 配置自动补上 statusLine 包装(只一次;卸载过的 agent 不动)。

👤 你要做的:用 `.deb` 替换开发安装并确认,再推送和打 tag:

```bash
rm ~/.local/share/gnome-shell/extensions/agentail@yetianwei.github.io   # 开发时的符号链接,会盖住系统里的扩展
sudo apt install ./dist/agentail_0.0.2.deb
# 注销再登录一次(X11:Alt+F2,r,回车);agentail doctor 全绿,面板 Usage 区正常
git push origin main && git tag v0.0.2 && git push origin v0.0.2
```

### 0.0.3(下一步):更多 agent

**范围**:Qoder、Qwen Code、Gemini CLI(只显示状态,和 Claude / Codex 一样)。**不做**:Cursor(IDE 远程位置未核实,事件模型也不同,推迟到 0.0.4 或之后)、OpenCode 等插件类、新 agent 的用量监控(`adapter.usage()` 返回 None,用量区只列有读数的 agent)、审批(不变量 7)。

**现状**:agent 列表硬编码在多处:`config.py` 默认值,`install/local.py` 与 `install/remote.py` 的 `AGENTS`、配置路径、`claude_config if agent == "claude" else codex_config` 二选一和远程探测脚本,`doctor.py`,`firstrun.py`,`model.js` 的 `AGENT_NAME`,`ui/model.py`,图标。hook 脚本与 `Adapter` 协议不用改。

**A0 ☁️ agent 注册表(纯重构,不加功能)**
- 抽出 `AgentSpec`:名称、显示名、配置目录(含环境变量覆盖)、配置文件名、合并/移除模块、信任或配置警告、远程探测项。
- 本机安装、远程安装、doctor、firstrun、配置默认值、两个 UI 的显示名都从注册表读取。
- 完成标准:Claude / Codex 的行为不变,现有测试全绿(`pytest`、`ruff`)。之后新增一个 agent = 适配器 + 配置模块 + 注册一行 + 图标。

**A1 ☁️ 调研(可与 A0 并行)**:在 `docs/agent-hooks-notes.md` 新增章节,每个 agent 查清 hook 配置文件位置、事件名、payload 字段、有没有"需要用户确认"的信号、SSH 下 hook 在哪一侧执行;每条结论附链接和日期,标明文档明确写的与推断的。重点核实:
- Qwen Code 是 Gemini CLI 的 fork,格式可能更接近 Gemini 而不是 Claude;若如此,A2 与 A3 合并处理。
- Qoder 是否真的兼容 Claude 的 `settings.json` 格式。
- 没有稳定 hook 来源的 agent 不硬做,写进结论后顺延。

✅ **A0 完成**(2026-10-08):`src/agentail/agents.py`(`AgentSpec` + `AGENTS`);本机 / 远程安装、doctor、firstrun、CLI、AppIndicator 都从注册表读取;远程探测改成一个固定脚本、agent 作参数传入;`model.js` 的 `AGENT_NAME` 由测试与注册表对齐。Claude / Codex 行为不变,本机 `doctor` 与 `install-local --dry-run` 结果与重构前一致。

✅ **A1 完成**(2026-10-08):`docs/agent-hooks-notes.md` §4。要点:
- **Qwen Code 是 Claude 格式**(不是 Gemini 格式),和 Qoder 一起放 A2;两者都可复用 Claude 的解码与合并(不带 statusLine 包装),`timeout` 秒;配置 `~/.qwen/settings.json`(`QWEN_HOME`)、`~/.qoder/settings.json`;用户级 hook 都不需要信任。
- **Gemini CLI**:自有事件名(`BeforeAgent`/`BeforeTool`/`AfterTool`/`AfterAgent`/`Notification`…),`timeout` 是**毫秒**,没有空闲通知;`GEMINI_CLI_HOME` 是 `.gemini` 的父目录(`AgentSpec` 要扩展);按源码,folder trust 默认开启时**未信任目录里可能连用户级 hook 都不跑**,A4 必须实测。
- 你已决定(2026-10-12):Qoder 国际版和国内版都支持,先做 Qoder,再做 Qwen Code。

**A2~A3 ☁️ 逐个接入(每个 agent 一个 PR)**:先 Claude 兼容的一组(Qoder、Qwen Code),再 Gemini CLI。

✅ **A2-Qoder 完成**(2026-10-12,未经真机验证):注册表里两个 agent,`qoder`(`~/.qoder/settings.json`,显示 "Qoder")和 `qoder_cn`(`~/.qoder-cn/settings.json`,显示 "Qoder CN");`adapters/claude_compat.py` 复用 Claude 的解码(`claude.decode_claude_style`),`install/claude_compat_config.py` 复用合并逻辑但不包装 statusLine;图标取自 qoder.com 的 app 图标(`icons/agent-qoder.png`,`agent-qoder_cn.png` 是同一张);合成测试数据 `tests/fixtures/qoder/synthetic.jsonl`;安装 / 卸载 / doctor / add-host 测试在 `tests/test_qoder.py`。升级后 daemon 首次启动会给已存在配置目录的 Qoder 版本自动装 hook(`firstrun` 里没处理过的 agent)。

✅ **A2-Qwen Code 完成**(2026-10-12,未经真机验证):注册项 `qwen`(`~/.qwen/settings.json`,`QWEN_HOME` 覆盖,开头的 `~` 本机与远程探测都会展开;相对路径的 `QWEN_HOME` 不支持),显示 "Qwen Code",二进制 `qwen`;同样用 `ClaudeCompatAdapter`;图标取自 QwenLM/qwen-code 仓库的桌面版 app 图标;合成数据 `tests/fixtures/qwen/synthetic.jsonl` 用 Qwen 自己的工具名(`run_shell_command`、`read_file`),测试在 `tests/test_qwen.py`。A2 全部完成。

✅ **A3-Gemini CLI 完成**(2026-10-12,未经真机验证):`adapters/gemini.py`(`SessionStart`、`BeforeAgent`→提交 prompt、`BeforeTool`/`AfterTool`、`Notification`(`ToolPermission`,命令取自 `details.command` / `details.filePath`)→需要你、`AfterAgent`→回合结束、`SessionEnd`;每次模型调用都触发的 `BeforeModel` 等不装);`install/gemini_config.py`(`timeout` 5000 毫秒,`matcher: ""`;`hooksConfig.enabled = false` 时安装和 doctor 都警告;带注释的 settings.json 不改并报错);注册表新增 `dir_env_is_parent`,`GEMINI_CLI_HOME` 本机和远程都按"替换 $HOME"处理;doctor 的配置警告改为对所有 agent 通用;图标取自 geminicli.com;测试 `tests/test_gemini.py`。安装后会提示 folder trust 可能让未信任目录里的会话不显示。

**A3b ☁️ Claude 额度主动查询**(2026-10-12 加入 0.0.3):statusLine 在 VS Code 扩展和 Claude Desktop 里不运行,只用 VS Code 时 Claude 额度永远不更新。改为用 Claude Code 自己的 OAuth 登录只读查询 `api.anthropic.com/api/oauth/usage`(已实测可用),由 Stop 和打开面板触发;默认开启(`config.toml` 的 `[usage] claude_api = false` 或 `agentail usage-api off` 关闭),只查本机账号。设计见 `docs/usage-api-design.md`,调研与其他项目的对比见 `docs/usage-notes.md` §2.4、§6。

**下一步**:A3b → A4(你的真机验证)→ A5 发布。

👤 Qoder 真机验证(A4 的一部分):

```bash
git pull && pip install -e '.[dev]' && agentail install-gnome-extension   # 然后重新登录或 Alt+F2 r
agentail install-local --dry-run      # 看 ~/.qoder/settings.json(和 ~/.qoder-cn/)的改动
agentail install-local
agentail daemon --record recordings/  # 另开终端;已有 systemd daemon 时先 systemctl --user stop agentail
qodercli                              # 若命令名不同请告诉我;问一个要跑命令的问题,批准一次,等它回答完,再按 Esc 中断一次,然后退出
agentail doctor                       # qoder hooks 一行应为 ✓
```

Qwen Code 同样:`install-local` 之后运行 `qwen`,做同样的操作(要跑命令的问题、批准一次、回答完、Esc 中断一次、退出),`doctor` 里 `qwen hooks` 应为 ✓,录制在 `recordings/qwen/`,版本号用 `qwen --version`。

Gemini CLI 同样:运行 `gemini`,做同样的操作;**另外**在一个从没信任过的新目录(如 `mkdir /tmp/g && cd /tmp/g && gemini`,出现 folder trust 提示时先选"不信任")里再问一次,看面板里会不会出现这个会话;`doctor` 里 `gemini hooks` 应为 ✓,录制在 `recordings/gemini/`,版本号 `gemini --version`。还请注意 Gemini 界面里每次 hook 执行时是否出现空白提示行(我们的 hook 什么都不输出)。

把 `recordings/qoder/`(或 `qoder_cn/`、`qwen/`、`gemini/`)下的 jsonl 去掉隐私后发给我,我替换掉合成数据;同时记下 Qoder 版本号(`qodercli --version`)和二进制名。面板里应出现 Qoder 分组和图标,授权时为橙色"需要你"。每个 agent 的统一清单:
1. `adapters/<x>.py`:未知字段一律忽略、字段类型不对也忽略;未验证的假设标 `TODO(M0)`。
2. `install/<x>_config.py`:合并而非覆盖,按 `agentail-hook` 识别,先备份,卸载还原(不变量 5)。
3. 注册表登记;`install-local`、`add-host`、`doctor` 自动覆盖(含测试,用临时 HOME,不碰真实配置)。
4. 官方图标放进 `icons/`,在 `icons/NOTICE.md` 登记(第三方商标,不在 MIT 范围内,同现有 Claude / Codex)。
5. 测试数据:合成的 `tests/fixtures/<agent>/synthetic.jsonl`,真实录制由 A4 补。
6. 文档:README、`docs/protocol.md` 如有变化。

**A4 👤 你在本机验证**:每个 agent 录一份真实会话放进 `tests/fixtures/<agent>/recorded-<版本>.jsonl`;在一台真实服务器上跑 `add-host`;确认面板里的显示(图标、分组、需要授权时的状态)。PR 描述会写出确切步骤。

**A5 发布 ☁️ + 👤**:CHANGELOG、版本号 0.0.3、`.deb`;同时整理"新增 agent 的清单"文档(A0 完成后的实际步骤)。升级时若安装器写入了新内容,按需提高 `firstrun.SETUP_VERSION`。

---

## 4. v1 之后(不在当前范围)

Claude Code 审批(`--mode wait`,约束见设计文档 6.4)→ Cursor → 插件类 agent(OpenCode 等);Qoder、Qwen、Gemini CLI 见 0.0.3;GNOME Shell 扩展前端(Wayland);集群计算节点的 JSONL 回退方案。
