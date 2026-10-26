// The panel's languages: English (the strings in the code) and Chinese.
//
// Pure JavaScript, no GNOME imports: tests/js/test_model.js runs it under gjs. Not gettext,
// which follows GNOME Shell's locale: the panel switches language while it runs.
//
// _('English text', {name: value}) returns the text in the current language with each {name}
// replaced. N_('English text') only marks a string kept in a table; it is translated with _()
// where it is shown. Keys are literal strings (no `${}`), so tests/js/test_model.js can find
// them all and check that each has a Chinese entry with the same placeholders.
//
// Text from the daemon (payloads, host details, the agentail tool's output) is never a key:
// it stays as it came, cleaned by model.js.

export const LANGUAGES = ['en', 'zh'];

let current = 'en';

export function setLanguage(lang) {
    current = LANGUAGES.includes(lang) ? lang : 'en';
}

export function language() {
    return current;
}

// The user's choice ('en' or 'zh'), else the desktop's: the first of `names`
// (GLib.get_language_names(), most preferred first) that is English or Chinese; else English.
export function resolveLanguage(choice, names) {
    if (LANGUAGES.includes(choice))
        return choice;
    for (const name of Array.isArray(names) ? names : []) {
        const base = String(name).split(/[_.@-]/)[0].toLowerCase();
        if (LANGUAGES.includes(base))
            return base;
    }
    return 'en';
}

export const N_ = msg => msg;

export function _(msg, params) {
    const text = current === 'zh' && Object.hasOwn(ZH, msg) ? ZH[msg] : msg;
    if (!params)
        return text;
    // One pass: a value that contains "{x}" is not expanded again.
    return text.replace(/\{(\w+)\}/g, (m, k) => Object.hasOwn(params, k) ? String(params[k]) : m);
}

// Units: compact in English ("5m", "1h 22m"), words in Chinese.
export const ZH = {
    // statuses
    'Needs you': '需要你',
    'Your turn': '轮到你',
    'Running': '运行中',
    'Out of date': '已过时',
    'Ended': '已结束',
    // tunnel states
    'local': '本机',
    'connected': '已连接',
    'connecting': '连接中',
    'reconnecting': '重新连接中',
    'login failed': '登录失败',
    'error': '出错',
    'not set up': '未设置',
    'unknown': '未知',
    // times
    '{n}s': '{n}秒',
    '{n}m': '{n}分钟',
    '{n}h': '{n}小时',
    '{n}d': '{n}天',
    '{d}d {h}h': '{d}天{h}小时',
    '{h}h {m}m': '{h}小时{m}分',
    'Limit': '限额',
    'Week': '每周',
    // top bar and summary
    'Not connected': '未连接',
    'No active sessions': '没有进行中的会话',
    '{n} needs you': '{n} 个需要你',
    '{n} need you': '{n} 个需要你',
    '{project} needs you': '{project} 需要你',
    'Agent': 'Agent',
    '{n} running': '{n} 个运行中',
    '{n} waiting': '{n} 个轮到你',
    '{n} host unreachable': '{n} 台主机连不上',
    '{n} hosts unreachable': '{n} 台主机连不上',
    // session cards
    'This computer': '本机',
    'this computer': '本机',
    'Last seen running': '最后一次看到时在运行',
    'for {age}': '已等{age}',
    'Remove from the list': '从列表中移除',
    'Answer it in the terminal': '请在终端里处理',
    'Answer it in the terminal on {host}': '请在 {host} 的终端里处理',
    '{n} ended recently': '最近结束了 {n} 个',
    // usage
    'Usage': '用量',
    'Hide usage': '收起用量',
    'Show usage': '展开用量',
    'reset, waiting for the next update': '已重置，等待下次更新',
    'resets in {time}': '{time}后重置',
    'unlimited': '不限',
    '{amount} left': '剩余 {amount}',
    '{amount} of {limit}': '{amount} / {limit}',
    '{amount} spent': '已用 {amount}',
    'out of credits': '额度已用完',
    'not active': '未生效',
    'Extra usage': '额外用量',
    'Credits': '额度',
    'sign-in expired · run {agent} once': '登录已过期 · 运行一次 {agent}',
    'updated {age} ago': '{age}前更新',
    'via {host}': '来自 {host}',
    'Claude: terminal sessions only. More in Settings (⚙, top right).':
        'Claude：仅来自终端会话。更多选项见设置（右上角 ⚙）。',
    // settings
    'Settings': '设置',
    'Back': '返回',
    'Use dark colours': '使用深色',
    'Use light colours': '使用浅色',
    'Language': '语言',
    'Notifications': '通知',
    "When it's your turn": '轮到你时',
    'When an agent needs your approval': '需要你批准时',
    'Show the prompt and the command': '显示 prompt 和命令',
    'Also on the lock screen.': '锁屏上也会显示。',
    'The agentail daemon is not running, or is too old for this setting.':
        'agentail daemon 没有运行，或者版本太旧，不支持这项设置。',
    'System': '跟随系统',
    'Usage limits': '用量限额',
    'The agentail daemon is not running, or is too old for these settings.':
        'agentail daemon 没有运行，或者版本太旧，不支持这些设置。',
    'Saving…': '正在保存…',
    'Checking…': '正在检查…',
    'Sign-in expired: run claude once': '登录已过期：运行一次 claude',
    'The last query failed: see agentail doctor': '上次查询失败：见 agentail doctor',
    'Last checked {age} ago': '{age}前查询过',
    'Show usage limits': '显示用量限额',
    "Ask Anthropic for Claude's limits": '向 Anthropic 查询 Claude 限额',
    'After every Claude turn and when you open this panel.': '每轮对话结束和打开面板时查询。',
    'For servers signed in to another Claude account.': '用于登录了其他 Claude 账号的服务器。',
    'Add a server first.': '请先添加服务器。',
    'Could not save that. Run agentail {args} in a terminal.':
        '保存失败。请在终端里运行 agentail {args}。',
    // servers
    'Servers': '服务器',
    '{host} is unreachable': '{host} 连不上',
    'Sessions may be out of date.': '会话状态可能已过时。',
    'Agents on SSH servers too? Add them in Settings (⚙).': '在 SSH 服务器上也跑 agent？在设置（⚙）里添加服务器。',
    '{state} · {n} active': '{state} · {n} 个进行中',
    'Remove {alias}': '移除 {alias}',
    '{problem}. Sessions may be out of date.': '{problem}。会话状态可能已过时。',
    '＋  Add server': '＋  添加服务器',
    'Pick a server from your SSH config': '从 SSH 配置中选择服务器',
    'Looking…': '正在查找…',
    'and {n} more: type the name below': '还有 {n} 个：请在下方输入名称',
    'No unused hosts in ~/.ssh/config. Type its ssh name below.':
        '~/.ssh/config 里没有可添加的主机。请在下方输入它的 ssh 名称。',
    'or type a name, then Enter': '或输入名称后按回车',
    'Cancel': '取消',
    "Remove {alias}? Agentail stops watching it and takes this desktop out of the server's Claude and Codex settings (they go back to the original once nothing else uses them). The server must be reachable.":
        '移除 {alias}？Agentail 会停止监视它，并把本桌面从该服务器的 Claude 和 Codex 设置中移除（没有其他使用者后，设置会恢复原样）。服务器必须能连上。',
    'Remove': '移除',
    'Adding {alias}…': '正在添加 {alias}…',
    'Removing {alias}…': '正在移除 {alias}…',
    'This talks to the server over SSH and can take a few seconds.':
        '需要通过 SSH 连接服务器，可能要几秒钟。',
    'Forget it anyway': '仍然移除',
    'Try again': '重试',
    'Close': '关闭',
    'That is not a valid ssh host name.': '这不是有效的 ssh 主机名。',
    'The agentail command was not found. Run `agentail {command}` in a terminal.':
        '找不到 agentail 命令。请在终端里运行 `agentail {command}`。',
    'Done': '完成',
    'Failed': '失败',
    'Added {alias}.': '已添加 {alias}。',
    'Added {alias}. {detail}': '已添加 {alias}。{detail}',
    'Removed {alias}.': '已移除 {alias}。',
    'Removed {alias}. The hooks stay for the server that shares its home.':
        '已移除 {alias}。与它共用 home 目录的服务器还在用这些 hook，所以保留。',
    'Removed {alias}. The hooks stay for the other desktops that watch it.':
        '已移除 {alias}。还有其他桌面在监视它，hook 保留。',
    // whole panel
    'Agentail could not draw this panel. See the GNOME Shell log.':
        'Agentail 无法绘制面板，请查看 GNOME Shell 日志。',
    'Agentail was updated. Press Alt+F2, type r, Enter (X11), or log out and back in to load the new panel.':
        'Agentail 已更新。按 Alt+F2，输入 r 后回车（X11），或者注销后重新登录，以加载新面板。',
    'All quiet': '一切安静',
    'Start Claude Code or Codex in a terminal and it shows up here.':
        '在终端里启动 Claude Code 或 Codex，它就会出现在这里。',
    'The agentail daemon is not running': 'agentail daemon 没有运行',
    'Retrying every few seconds. Start it with:': '每隔几秒重试一次。启动命令：',
    // notifications
    '{project} is done': '{project} 已完成',
    '{agent} wants to run {tool}: {detail}': '{agent} 想运行 {tool}：{detail}',
    // approvals
    'Yes': '是',
    'No': '否',
    'Sending…': '正在发送…',
    '{n}s left': '还剩 {n} 秒',
    'Approvals': '审批',
    'Answer permission prompts here': '在这里处理授权请求',
    // token counts
    'Tokens': 'Token 消耗',
    'Nothing yet today.': '今天还没有。',
    'Nothing yet this week.': '本周还没有。',
    'Nothing counted yet.': '还没有统计。',
    'Daily': '今日',
    'Weekly': '本周',
    'Lifetime': '累计',
    'All agents': '全部 agent',
    'Input': '输入',
    'Cache write': '缓存写',
    'Cache read': '缓存读',
    'Output': '输出',
    'Since {date}': '自 {date} 起',
    'Token counts are off. Turn them on in Settings.': 'Token 统计已关闭，可在设置中打开。',
    'Token counts': 'Token 统计',
    'Count tokens': '统计 token',
};
