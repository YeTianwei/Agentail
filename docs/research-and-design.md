# Linux 版 Agent Island:调研结论与开发方案(v2)

> **项目已命名为 Agentail。** 本文写于命名之前,文中的 `island`、`island-hook.py`、`~/.config/island/`、`island-hook.sock` 等分别对应 `agentail`、`agentail-hook.py`、`~/.config/agentail/`、`agentail.sock`。命名、目录与接口以 `docs/PLAN.md` 和代码为准;本文保留作设计依据。

> 整理日期:2026-09-30
> v2 变更:确定第一版范围(Claude Code + Codex,多台 SSH 远程从一开始就做,第一版不做审批);修正 fail-open 语义、socket 位置、审批超时等设计问题;新增第 8 节"安全";新增第 12 节"第一版实现方案";Swift 移植内容移到附录。
> 说明:文中"已核实"指读过代码或文档;"未验证"指推断或设计建议,尚未在真实环境中测试;"需实测"指实现前必须在你的机器/服务器上确认。对 vibe-island 只读了代码,没有编译、运行或做安全审查。

---

## 1. 需求与范围

**总体目标**

- 在本地 Linux 桌面(当前为 GNOME + X11)上,像 macOS 刘海/灵动岛那样显示 AI 编程 agent 的实时状态。
- 同时接入本地 agent 会话,和多台远程服务器(通过 SSH 访问,无桌面环境)上的 agent 会话。
- 架构按适配器设计,以后加新 agent 不需要重写核心。

**第一版(v1)范围——已确定**

| 包含 | 不包含(留到 v2 及以后) |
|---|---|
| Claude Code、Codex 两个适配器 | 其他 agent(Qoder、Qwen、Gemini、Cursor、OpenCode 等) |
| 本地 + **多台 SSH 远程服务器**(从一开始就做) | 在面板里审批权限请求 |
| 会话状态、"轮到我了"提醒、"终端里有待审批"提醒 | 跳回远程终端 |
| 每台主机的隧道连接状态 | 重连后从 transcript 同步状态 |

v1 不做审批的原因:审批是风险最集中的部分(fail-open 语义、超时、竞态、人不在桌前、面板误触,见第 6.3、8 节),先把状态链路做稳。

---

## 2. 调研结论

### 2.1 Agent Island(agent-island.dev)

- 官网称是 macOS + Windows 开源状态伴侣,支持 Claude Code、Codex、Antigravity、Grok、Cursor,MIT 协议。
- 官网与仓库对不上:官网写 v2.1.2,`tristan666666/agent-island` 仓库最新 v1.3.1,Swift 占 97.5%,只有 macOS 源码。README 说它 fork 自 [codex-island](https://github.com/ericjypark/codex-island)。
- 状态检测靠**轮询 transcript 文件**,不是 hook 事件驱动,做不了审批。
- **结论:不作为基础。**

### 2.2 三个 hook 事件驱动的 macOS 开源项目

均为 macOS 14+ 专属,SwiftUI + AppKit。

| | open-vibe-island | CodeIsland | ping-island |
|---|---|---|---|
| 协议 | **GPL-3.0** | **MIT** | **Apache-2.0** |
| 可移植核心 | `OpenIslandCore`,约 2 万行 SwiftPM 库 | `CodeIslandCore`,约 1.5 万行 | 仅 `Prototype/` 中约 5.7k 行 |
| 文档 | `architecture.md`、`hooks.md`、`ssh-setup.md` | 基本没有 | 若干专题文档 |
| 远程方案 | SSH `RemoteForward` + 远程 Python 脚本 | 未细看 | 远程装静态二进制和常驻服务 |

数据流相同:agent 触发 hook → 小脚本经 Unix socket 把 JSON 发给 App → 状态机更新界面。Unix socket、JSON、agent 配置在 Linux 上都能用,**只有界面需要重写**。Swift 核心的 Linux 移植清单见附录 A。

---

## 3. 现有工具调研与参考实现

### 3.1 结论

没有现成工具同时满足:Linux 桌面灵动岛形态 + 多 agent + 多台 SSH 远程聚合 + 审批。**决定自己实现,新建仓库,不 fork**(理由见第 4 节)。参考实现:

- **open-vibe-island**(macOS):参考线协议、hook 文档和 SSH 转发思路。
- **vibe-island**(Linux/Rust/Tauri):参考 Linux 窗口聚焦、hook 安装器覆盖的 agent 范围。只读不抄。

### 3.2 已找到的 Linux / 跨平台候选(据 README,除 vibe-island 外未深入验证)

| 项目 | 情况 | 为什么不直接用 |
|---|---|---|
| [vibe-island](https://github.com/NetVar1337/vibe-island)(镜像 [VoidChecksum](https://github.com/VoidChecksum/vibe-island)) | Rust + Tauri v2,跨平台,GPL-3.0 标注 | SSH 不可用、来路存疑、维护不活跃(见 3.3) |
| [claude-code-tray](https://github.com/riccardo-enr/claude-code-tray) | GNOME 顶栏托盘,MIT | 只支持 Claude Code,无审批,无 CI |
| [agent-notifier](https://github.com/SonNX24042005/agent-notifier) | 通知覆盖层 + GNOME Shell 扩展,MIT | 只做通知,无远程,只有一个提交 |
| [claude-code-monitor](https://github.com/bruceyxli/claude-code-monitor) | 网页面板,远程机器经 HTTP 发 hook 事件,有远程审批,MIT | 只支持 Claude Code,非灵动岛形态 |
| [ai-agent-session-center](https://github.com/coding-by-feng/ai-agent-session-center) | Claude + Codex,Electron/网页,MIT | 主要在 macOS 测试,远程仅限 localhost |
| [codenotch-linux](https://github.com/joinsplit/codenotch-linux)、[gonotch](https://github.com/Leock9/gonotch) | 用量额度显示 | 不是会话进度 |
| [zincnan/AgentBeacon](https://github.com/zincnan/AgentBeacon) | Claude Code + Codex 插件,远程机器单向上报状态到 Windows 接收端,不做审批 | 接收端只有 Windows;无 Linux 桌面形态。其 README 提到 Codex 默认跳过未受信任的 hook,需在 codex 中执行 `/hooks` 信任(待 M0 核实) |
| [Agent Deck](https://github.com/asheshgoplani/agent-deck)、[ccboard](https://github.com/FlorianBruniaux/ccboard) | 终端 TUI | 不能在本地聚合多台服务器 |

### 3.3 参考实现:vibe-island(已核实代码结构)

- Rust + Tauri v2 后端约 3000 行,React 前端约 1900 行;46 个提交,单一作者,最后提交 2026-04-28,无单元测试。
- **可借鉴**:后端模块划分(hooks / socket / sessions / platform / config);hook 脚本透传终端相关环境变量;Linux 窗口聚焦(X11 `xdotool` 按 PID、Hyprland `hyprctl`、Sway `swaymsg`、KDE `wmctrl`);安装器覆盖的 agent 及配置路径清单。
- **不照搬**:
  - SSH 模块用的是 `-L`(方向反了),前端没调用,远程不装 hook,会话没有主机字段,不重连——据读代码判断不可用。
  - 单一 socket、无主机区分;自有协议 `vi-e7c4`。
  - 提交信息 "Align open app with reverse-engineered desktop build" 表明可能参考了闭源商业版的逆向结果,GPL 授权是否有效无法判断。**不复制其代码,不论自己项目选什么协议。**
  - 不要运行其 README 的 `npx` 安装命令,除非先审查 `hooks/mod.rs`、`updater.rs`、`bin/vibe-island.js`。

### 3.4 其他可借鉴的想法

- claude-code-monitor:审批 2 分钟超时——v2 做审批时参考超时策略。
- claude-code-tray:长驻 daemon + Unix socket + tmux 聚焦。
- agent-notifier:GNOME Shell 扩展方案(Wayland 下的 UI 退路,见第 9 节)。

---

## 4. 选型:新建仓库,不 fork

| 候选 | 为什么不 fork |
|---|---|
| open-vibe-island / CodeIsland / ping-island | Swift + macOS UI,UI 要全部重写;Swift 核心在 Linux 未编译验证;都没有多主机模型 |
| vibe-island | 来路存疑;SSH、多主机、适配层都要重写,剩下能用的主要是 UI 骨架 |
| claude-code-monitor 等 | 只支持 Claude Code,架构(HTTP、网页面板)与本方案不同 |

v1 的核心代码(hook 脚本、daemon、状态机、两个适配器、隧道守护、安装器、小面板)估计只有几千行 Python。fork 带来的主要是不需要的 UI 和架构约束,以及协议和来路上的负担。

**项目协议——建议现在就定**(它决定哪些代码可以复用):

- **选 GPL-3.0**:可以把 open-vibe-island 的 hook 脚本、文档片段直接拷进来(保留版权声明)。但本方案的 hook 脚本是"哑管道"(第 6.3 节),和它的脚本差别很大,实际能复用的不多。
- **选 MIT / Apache-2.0**:只依据公开文档和自己实测来实现,不复制 GPL 代码。
- 无论哪种,都不复用 vibe-island 的代码。
- 以上不是法律意见。

**语言**:daemon、安装器、UI 第一版都用 Python(一种语言,少一套工具链);远程 hook 脚本用纯标准库 Python。协议稳定后再决定是否换 Rust/Go。

---

## 5. 目标架构

```
远程服务器 A/B/C(无桌面)                        本地 Linux 桌面 (GNOME/X11)
Claude Code / Codex                              ┌─ island-daemon(Python, asyncio)
  └ hook → island-hook.py(哑管道,标准库)        │    ├─ 适配器:claude / codex
       连接 /run/user/<uid>/island-hook.sock      │    ├─ 状态机 key=(主机, agent, session_id)
          ▲                                       │    ├─ 隧道守护:每台主机一个 ssh -N -R
          ║ ssh -R(每台主机一条隧道)             │    ├─ $XDG_RUNTIME_DIR/island/local.sock
          ╚═════════════════════════════════════════►  ├─ $XDG_RUNTIME_DIR/island/hosts/<别名>.sock
                                                  │    └─ ui.sock(给 UI 推状态)
本地 Claude Code / Codex                          │
  └ hook → island-hook.py → local.sock ─────────► │
                                                  └─ island-ui(独立进程):顶部胶囊 + 系统通知
```

要点:

- **来源主机由"事件从哪个 socket 进来"决定**,不信任 payload。
- **UI 是独立进程**,通过 `ui.sock` 订阅状态。将来换 Tauri 或 GNOME Shell 扩展时 daemon 不动。
- **agent 相关的知识全部在本地**(适配器 + 安装器);远程脚本不含 agent 逻辑。

---

## 6. 多 agent 适配层

### 6.1 各 agent 的接入方式(据 open-vibe-island、ping-island 文档,未逐一实测)

| 接入类型 | agent | 配置位置 | 备注 |
|---|---|---|---|
| Claude 兼容 hook | Claude Code(v1);Qoder、Qwen Code、Factory、CodeBuddy(以后) | `~/.claude/settings.json` 等 | 格式相同,只有路径不同 |
| 载荷兼容、配置不同 | Kimi CLI | `~/.kimi/config.toml` | 以后 |
| 独立格式 | **Codex(v1)** | `~/.codex/config.toml` | 据文档 hook 只有 SessionStart、UserPromptSubmit、Stop;**v1 只做状态**。审批需走 `codex app-server` JSON-RPC,等于要包一层 codex,是另一种架构。`notify` 配置可作为退路(需实测) |
| | Gemini CLI | `~/.gemini/settings.json` | 据记录是 fire-and-forget,不能审批 |
| | Cursor | `~/.cursor/hooks.json` | 事件名不同;要求 JSON 回复 |
| 插件机制 | OpenCode、Pi、Hermes | 各自插件目录 | 需生成插件代码 |

### 6.2 适配器职责

1. **安装 / 卸载 / 检查**:把 hook **合并**进配置文件,保留用户条目;我们的条目通过命令里的 `island-hook` 字样识别,保证幂等。
2. **解码**:原始载荷 → 统一事件:`session_start`、`prompt_submit`、`tool_start`、`tool_end`、`attention`(需要用户注意,含子类型 `idle` / `permission`)、`stop`(回合结束)、`session_end`、`subagent_*`。
3. **编码**:统一的"批准/拒绝/不表态" → agent 期望的 stdout/退出码(v2)。
4. **能力声明**:能否审批、能否回答提问。UI 按能力决定显示什么。`question`(回答 agent 的提问)能否经 hook 实现,需逐个实测。

### 6.3 hook 脚本 = 哑管道,agent 知识放在安装器写入的命令行参数里

安装器写入配置的 hook 命令形如:

```
/usr/bin/python3 ~/.island/island-hook.py --agent claude --event PreToolUse \
    --mode fire --sock /run/user/1234/island-hook.sock
```

- 脚本只把 `{agent, event, 白名单环境变量, stdin 原文, ppid, cwd}` 发出去。
- `--mode fire`:发完就走,不等回复。v1 所有事件都是 fire。
- `--mode wait --fallback <不表态输出>`(v2 审批):阻塞等 daemon 返回 `{stdout, exit}`;超时或失败时输出 fallback。
- 新增 agent 或改事件映射,只需在本地重跑安装器,远程脚本不用重新部署。
- Python 解释器用 `add-host` 时探测到的**绝对路径**,不用 `env python3`(conda/module 环境可能指向坏掉的解释器)。

**fail-open 的准确含义:不干预,不是放行。** 任何失败(socket 不存在、连不上、超时、异常)时,脚本都必须交还 agent 的原生行为:

- 对 Claude Code:不输出、退出码 0 = 不表态,终端照常询问。
- 对要求 JSON 回复的 agent(如 Cursor):fallback 必须是"ask",**绝不能是 allow**,否则隧道一断,所有命令就自动批准了。
- 每个 agent 的"不表态"写法要实测,并确认空输出不会被当成错误或拒绝。

### 6.4 审批(v2)的前置约束

- **审批超时 < agent 的 hook 超时**:安装器写入 hook 时显式设置 `timeout`(Claude Code 的默认值需查当前版本),daemon 在此之前主动返回"不表态"。
- **竞态**:面板和终端同时在等时,用户在终端先回答了会怎样,需实测。
- **人不在这台桌面前**:你也会从 Windows 机器 SSH 上服务器;只要 Linux 桌面的隧道开着,审批会弹到 Linux 桌面并卡到超时。对策:审批拦截默认关闭、按会话/主机开启;或桌面空闲/锁屏时直接返回"不表态"。
- 面板不抢焦点,不把 Enter 绑定为"批准"。

### 6.5 限制

- 各家 hook 接口在变,适配器需维护;录下真实载荷作为测试夹具。
- 没有 hook 的 agent 只能轮询 transcript,只有状态。
- Cursor / Qoder 的 IDE 版在 Remote-SSH 下 hook 在哪一侧执行,未核实。

### 6.6 实现顺序

1. Claude Code(v1)
2. Codex(v1,只做状态)
3. Qoder、Qwen 等 Claude 兼容 fork
4. Gemini CLI,然后 Cursor
5. OpenCode 等插件类

---

## 7. 多服务器设计

### 7.1 socket 位置

- **本地**:`$XDG_RUNTIME_DIR/island/`(目录 0700),内含 `local.sock`、`ui.sock`、`hosts/<别名>.sock`。
- **远程(首选)**:`/run/user/<uid>/island-hook.sock`。
  - 节点本地、tmpfs,不在 NFS 上。
  - 隧道本身是一个登录会话,隧道在时该目录一直存在。
  - 同一台服务器上其他用户无权访问。
  - `-R` 需要绝对路径,uid 在 `add-host` 时获取。
- **远程(退路)**:没有 `/run/user/<uid>`(无 systemd-logind)时,用 `~/.island/run/hook.sock`,并在 `add-host` 时检测家目录是否在 NFS 上、给出警告。
- **为什么不放共享家目录**:NFS 上的 Unix socket 在 A 节点创建后,B 节点看得见但连不上(静默失效);多个登录节点共用家目录时,两条隧道会抢同一个路径。

### 7.2 隧道守护

每台主机一个独立 asyncio 任务,监督一个 `ssh` 子进程:

```
ssh -o BatchMode=yes <别名> 'rm -f <远程socket>'          # 清残留,见 7.4
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -R <远程socket>:<本地 hosts/别名.sock> <别名>
```

- 直接用 `~/.ssh/config` 里的别名(密钥、端口、ProxyJump 不重复配置)。
- 状态:`connecting` / `connected` / `backoff` / `auth_failed`。stderr 出现 `Permission denied` 时进入 `auth_failed`,不再重试刷屏。
- 断线后指数退避(1s → 60s),连上稳定 60 秒后重置。
- 连上后做一次端到端检查:远程执行 `island-hook.py --ping`,本地收到才算 `connected ✓`。
- 需要交互输入密码或二次验证的服务器无法自动连(BatchMode)。
- 以后可用 ControlMaster 把"清残留"和"建隧道"合成一条连接。

### 7.3 配置与安装

- 配置文件 `~/.config/island/hosts.toml`:

  ```toml
  [[host]]
  alias = "gpu1"          # ~/.ssh/config 中的别名
  name  = "组里 GPU 服务器" # 显示名
  agents = ["claude", "codex"]
  ```

- `island add-host <别名>` 一次完成:
  1. 探测:`id -u`、`$HOME`、python3 绝对路径与版本、`/run/user/<uid>` 是否存在、sshd 是否允许 Unix socket 转发。
  2. 上传脚本:`ssh 别名 'mkdir -p ~/.island && cat > ~/.island/island-hook.py' < island-hook.py`(不依赖 scp),权限 0700。
  3. **合并配置在本地做**:把远程 `~/.claude/settings.json`、`~/.codex/config.toml` 拉下来,本地用 `json` / `tomlkit`(保留注释和格式)合并,先在远程备份原文件再写回。远程只需要 Python 3.6+ 标准库。
  4. 建隧道,触发一次测试事件,确认收到。
- `island remove-host <别名>`:从远程配置里删掉我们的条目,删脚本和 socket。

### 7.4 sshd 相关

- **重连时 socket 残留**:SSH 异常断开后远程 socket 文件不会被清理,重连时 sshd 默认拒绝在已有路径上转发,登录成功但转发悄悄失效。
  - 根治(需 root):sshd_config 加 `StreamLocalBindUnlink yes`,然后 `reload`。注意客户端同名选项只对 `-L` 生效,对 `-R` 无效。
  - 本方案:每次重连前 `rm -f` 远程旧 socket。socket 在节点本地、只由本守护进程创建,不会误伤。
- **转发被禁用**:服务器可能设置了 `AllowStreamLocalForwarding no` 或 `DisableForwarding yes`(学校服务器尤其可能)。`add-host` 要能识别并明确报错,而不是静默失败。
- **socket 权限**:sshd 按 `StreamLocalBindMask`(默认 0177)创建,即 0600。

### 7.5 集群(SoC 集群等)

- 有多个登录节点时,隧道只落在其中一个。在 `hosts.toml` 里写**具体的登录节点主机名**,并在该节点上跑 agent;在其他登录节点上跑的 agent 会静默收不到事件。
- 计算节点(Slurm 作业)连不到登录节点上的 socket。退路:hook 把事件追加写到共享家目录的 JSONL,本地 `ssh 主机 tail -F` 拉取;单向,只有状态。v1 不做。

---

## 8. 安全

| 风险 | 措施 |
|---|---|
| **远程载荷不可信**:审批卡片、会话标题会显示命令、路径、diff,被攻破的服务器可借此攻击本地 | UI 一律纯文本渲染(GTK 用 `set_text`,不用 markup;将来用 WebView 时开严格 CSP、禁用 innerHTML、Tauri 命令白名单);载荷字段绝不拼进 shell 命令 |
| **环境变量泄露**:服务器环境里常有 API key、token | 脚本只转发白名单:`TERM`、`TERM_PROGRAM`、`TMUX`、`TMUX_PANE`、`STY`、`SSH_TTY`、`COLORTERM` |
| **载荷过大**:Write 类工具的载荷可能包含整个文件 | 脚本和 daemon 都设上限(如 256 KB),超出截断并标记 |
| **本地 socket 被注入** | `$XDG_RUNTIME_DIR/island/` 目录 0700;同用户进程本就可信 |
| **日志** | 默认不落盘原始载荷;`--record` 调试模式才写,且写到 0700 目录 |
| **面板误触**(v2) | 面板不抢焦点,Enter 不绑定"批准" |
| **hook 拖慢 agent** | fire 模式 connect/send 超时 1 秒;脚本捕获所有异常,永远退出码 0、不输出 |

---

## 9. 界面方案

### 9.1 v1:Python + GTK3(PyGObject)独立进程

- **为什么不是 GTK4**:GTK4 去掉了窗口定位和 keep-above 接口,做不了"固定在顶部居中、置顶"。GTK3 在 X11 下可以。
- **为什么不是 Tauri**:v1 只有一个小胶囊和一个列表,不值得再引入 Rust + JS 工具链;UI 与 daemon 分进程,以后要换随时换。
- 窗口:无边框、keep-above、不进任务栏、`set_accept_focus(False)`、顶部居中。平时是细胶囊(显示"运行中 N / 等你 M"和主机连接灯),点开是按主机分组的会话列表。
- **需实测**:Mutter 通常把普通窗口限制在工作区内,不让盖住顶栏;要贴在顶栏正中可能需要 `DOCK` / `NOTIFICATION` 窗口类型。v1 先放在顶栏正下方。
- 通知:`Gio.Notification` 或 libnotify,在"回合结束""需要你操作""终端里有待审批"时发。声音用 libcanberra(可选)。
- 本地会话点击可跳回终端(X11 `xdotool` 按 PID,可参考 vibe-island 思路);远程会话 v1 不做跳转。

### 9.2 X11 的寿命

据我所知,GNOME 上游在逐步移除 X11 会话,各发行版在跟进(**需按你的发行版版本核实**)。只剩 Wayland 后,GNOME 上无法让普通窗口置顶并固定位置,也不支持 layer-shell,唯一路径是 GNOME Shell 扩展。因此 daemon ↔ UI 的 `ui.sock` 协议要保持稳定、UI 保持薄,届时写一个 GNOME Shell 扩展作为新前端即可。

---

## 10. 已知限制与风险

- **断线期间事件丢失**:hook 连不上时不干预,agent 照常跑,本地收不到。主机断线时,其会话标"主机离线(状态可能过期)";重连后状态同步留到以后。
- **会话残留**:`running` 状态超过 30 分钟无事件,标"可能已停止";`ended` 若干分钟后移除。
- **事件乱序**:并行工具调用和 subagent 的 hook 是独立连接,到达顺序不保证,状态机要容忍。
- **隧道在、daemon 卡死**:远程连接会挂住,靠脚本的短超时兜底。
- **Python 启动开销**:每次工具调用多几十毫秒;以后可换静态二进制。
- **认证**:需要密钥或 ssh-agent 免交互登录。
- **集群**:见 7.5。
- **X11**:见 9.2。
- **原项目版本变动**:调研基于 2026-09-30 的仓库快照。

---

## 11. 待决策事项

- **项目协议**(建议现在定,见第 4 节)。
- v2 审批的开启策略:默认关闭、按会话开启,还是按桌面空闲自动判断。
- Cursor / Qoder 的 IDE 版本是否纳入范围。
- 是否把 daemon 换成 Rust/Go(v1 之后)。
- 是否对 vibe-island 做安全审查(仅当想在本机运行它时需要)。

---

## 12. 第一版实现方案

### 12.1 仓库结构

```
agent-island-linux/
├─ pyproject.toml            # Python ≥3.11(本地);依赖:tomlkit、PyGObject
├─ island/
│  ├─ cli.py                 # island daemon | ui | add-host | remove-host | install-local | status | tail
│  ├─ daemon/
│  │  ├─ main.py             # asyncio 入口,装配各组件
│  │  ├─ ingest.py           # 监听 local.sock 和 hosts/*.sock,解析一行 JSON
│  │  ├─ state.py            # 状态机(纯函数 reducer,便于测试)
│  │  ├─ tunnels.py          # 每主机一个隧道守护任务
│  │  └─ uiapi.py            # ui.sock:推快照 + 增量
│  ├─ adapters/
│  │  ├─ base.py             # Adapter 接口 + 统一事件 dataclass
│  │  ├─ claude.py
│  │  └─ codex.py
│  ├─ install/
│  │  ├─ remote.py           # 探测、上传、拉配置 → 本地合并 → 备份 → 写回
│  │  ├─ claude_config.py    # settings.json 合并/移除
│  │  └─ codex_config.py     # config.toml 合并/移除(tomlkit)
│  └─ ui/
│     ├─ indicator.py        # 顶栏指示器(M4 实现时由胶囊改为指示器)
│     └─ notify.py
├─ hook/island-hook.py       # 远程/本地共用;纯标准库,兼容 Python 3.6
├─ tests/
│  ├─ fixtures/{claude,codex}/*.jsonl   # 录制的真实载荷
│  └─ test_state.py, test_adapters.py, test_config_merge.py
└─ docs/protocol.md          # 线协议和 UI 协议
```

### 12.2 线协议 v1(hook → daemon)

Unix socket,一个连接一行 JSON:

```json
{"v": 1, "agent": "claude", "event": "PreToolUse", "mode": "fire",
 "ts": 1759212345.12, "ppid": 4321, "cwd": "/home/u/proj",
 "env": {"TMUX": "...", "TERM_PROGRAM": "..."},
 "stdin": "<hook 原始 stdin 字符串,可能被截断>", "truncated": false}
```

- `fire`:脚本发完即关闭。
- `wait`(v2):daemon 回一行 `{"stdout": "...", "exit": 0}`。
- `ping`:`{"v":1,"event":"__ping__"}`,用于隧道端到端检查。

### 12.3 UI 协议(daemon → UI)

UI 连 `ui.sock` 后先收到一行 `{"type":"snapshot", ...}`,之后是 `{"type":"session_update"|"session_remove"|"host_status", ...}`。UI 只显示,不持有业务状态;重连后重新拿快照。

### 12.4 状态模型

- 键:`(host, agent, session_id)`。
- 字段:状态、cwd、最近一次 prompt 的前若干字、当前工具、最后事件时间、主机连接状态。
- 状态:`running` → `waiting_input`(回合结束 / 空闲提醒)/ `needs_attention`(终端里有待审批)→ `ended`;另有 `stale`(主机离线或长时间无事件)。
- Claude Code 事件映射(需实测当前版本字段):SessionStart、UserPromptSubmit、PreToolUse/PostToolUse、Notification(区分"等输入"与"要权限";若没有类型字段就退回解析 message)、Stop、SubagentStop、SessionEnd。
- Codex:SessionStart、UserPromptSubmit、Stop;若 hook 不可用则用 `notify`。注意 `notify` 只能配一个命令,用户已有时要串联调用原命令。

### 12.5 里程碑

| 里程碑 | 内容 | 完成标准 |
|---|---|---|
| **M0 实测**(先做) | 在本机和每台服务器上确认:Claude Code 当前 hook 事件、字段和 timeout;Codex hook 的真实配置格式与事件(或 notify);sshd 是否允许 Unix socket 转发;`/run/user/<uid>` 是否存在;python3 路径 | 一页实测记录,写进 `docs/` |
| **M1 链路** | `island-hook.py` + daemon 的 `ingest` + `island tail`(打印事件);本地 Claude Code 手工配 hook | 本地跑 Claude,终端里实时看到事件 |
| **M2 适配器与状态机** | `adapters/claude.py`、`codex.py`、`state.py`;`--record` 录制夹具;单元测试 | 用夹具回放,状态序列正确 |
| **M3 多主机** | `tunnels.py`、`add-host` / `remove-host`、配置合并、ping 检查;至少接两台服务器 | 两台服务器 + 本地同时跑 agent,`island status` 正确区分;拔网线/休眠后能自动恢复 |
| **M4 界面** | 顶栏指示器 + 下拉会话列表 + 系统通知(原计划为 GTK3 胶囊) | 日常可用 |
| **M5 打磨** | systemd user service 自启、`install-local`、stale 处理、README | 重启电脑后无需手动操作 |

### 12.6 测试清单

- **fail-open**:daemon 未启动、隧道断开、daemon 卡死(`kill -STOP`)三种情况下,Claude Code 和 Codex 都照常工作,每次 hook 的额外延迟可接受。
- **隧道**:杀掉 ssh 进程、断网、笔记本休眠唤醒后自动重连,且远程 socket 残留被清理。
- **多主机**:两台服务器上 session_id 相同时不冲突。
- **配置合并**:已有用户 hook、已有注释的 TOML、重复安装、卸载后恢复原样。
- **载荷**:超大载荷被截断;含控制字符、伪造 markup 的字符串在 UI 中按纯文本显示。

### 12.7 v2 预告

Claude Code 审批(`--mode wait` + fallback、超时、竞态、开启策略,见 6.4)→ Qoder/Qwen 等 Claude 兼容 fork → Gemini CLI → Cursor → 插件类。

---

## 13. 参考链接

**Agent Island 及上游**
- https://agent-island.dev/ 、https://agent-island.dev/capabilities/
- https://github.com/tristan666666/agent-island
- https://github.com/ericjypark/codex-island

**参考实现**
- open-vibe-island(GPL-3.0,macOS):https://github.com/Octane0411/open-vibe-island(`docs/hooks.md`、`docs/ssh-setup.md`、`scripts/open-island-hooks.py`)
- vibe-island(GPL-3.0 标注,Rust/Tauri):https://github.com/NetVar1337/vibe-island(镜像 https://github.com/VoidChecksum/vibe-island)

**其他 macOS 同类**
- CodeIsland(MIT):https://github.com/wxtsky/CodeIsland
- ping-island(Apache-2.0):https://github.com/erha19/ping-island
- Vibe Island(闭源):https://github.com/vibeislandapp/vibe-island;Claude Island:https://github.com/farouqaldori/claude-island

**Linux / 跨平台候选**
- https://github.com/riccardo-enr/claude-code-tray
- https://github.com/SonNX24042005/agent-notifier
- https://github.com/bruceyxli/claude-code-monitor
- https://github.com/coding-by-feng/ai-agent-session-center
- https://github.com/joinsplit/codenotch-linux 、https://github.com/Leock9/gonotch
- https://github.com/asheshgoplani/agent-deck 、https://github.com/FlorianBruniaux/ccboard
- 综合列表:https://github.com/arach/awesome-agent-clients

---

## 附录 A:open-vibe-island 核心库移植到 Linux 的改动(未编译验证,不 fork 后仅供参考)

- `BridgeTransport.swift`:`sockaddr_un.sun_len` 是 BSD 专有;`SO_NOSIGPIPE` 换 `MSG_NOSIGNAL`。
- `Darwin.connect` 等显式限定换 Glibc。
- `os.Logger` 换 swift-log。
- 删除 `WatchHTTPEndpoint`(Network.framework)和 Warp 终端相关代码。
- CodeIsland 对应问题:`PushChannel.swift` 用 CryptoKit(可换 swift-crypto),bridge 直接调 Darwin socket,`AppState` 约 3950 行单体。
