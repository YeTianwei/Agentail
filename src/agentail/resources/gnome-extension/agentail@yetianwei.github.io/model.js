// What the panel shows, computed from UI protocol messages (docs/protocol.md).
//
// Pure JavaScript, no GNOME imports: extension.js only renders what these
// functions return, and tests/js/test_model.js runs them under plain gjs.
// Mirrors src/agentail/ui/model.py (the AppIndicator fallback).
//
// Every string that came from the daemon is passed through clean() (no control
// characters, bounded length) before it reaches a view object, and the panel
// renders it as plain text only (St.Label text, never markup).

export const STATUS = {
    needs_attention: {label: 'Needs you', order: 0, style: 'attention'},
    waiting_input: {label: 'Your turn', order: 1, style: 'waiting'},
    running: {label: 'Running', order: 2, style: 'running'},
    stale: {label: 'Out of date', order: 3, style: 'stale'},
    ended: {label: 'Ended', order: 4, style: 'ended'},
};

// Tunnel state -> level: ok | pending | error | off.
const HOST_LEVEL = {
    local: 'ok',
    connected: 'ok',
    connecting: 'pending',
    backoff: 'pending',
    auth_failed: 'error',
    stopped: 'off',
};

const HOST_STATE_TEXT = {
    local: 'local',
    connected: 'connected',
    connecting: 'connecting',
    backoff: 'reconnecting',
    auth_failed: 'login failed',
    stopped: 'not set up',
};

export const AGENT_NAME = {claude: 'Claude Code', codex: 'Codex'};

export const LIMITS = {project: 40, subtitle: 140, detail: 160, hostDetail: 160};
export const NOTIFY_INTERVAL_S = 10;
export const USAGE_STALE_S = 30 * 60;
export const USAGE_WARN = 70;
export const USAGE_CRIT = 90;

// Cc and Cf categories: control and format characters (terminal escapes,
// bidi overrides). Whitespace controls become spaces.
const CONTROL = /[\p{Cc}\p{Cf}]/gu;

export function clean(value, limit = 0) {
    let text;
    if (typeof value === 'string')
        text = value;
    else if (value === null || value === undefined)
        text = '';
    else
        text = String(value);
    text = text.replace(/[\t\n\r]/g, ' ').replace(CONTROL, '?');
    const chars = Array.from(text);
    if (limit && chars.length > limit)
        text = `${chars.slice(0, limit - 1).join('')}…`;
    return text;
}

export function keyOf(key) {
    if (!key || typeof key !== 'object')
        return ['?', '?', '?'];
    return [clean(key.host), clean(key.agent), clean(key.session_id)];
}

const keyString = k => JSON.stringify(k);

export function ageText(ts, now) {
    if (typeof ts !== 'number' || !(ts > 0))
        return '';
    const secs = Math.max(0, Math.floor(now - ts));
    if (secs < 60)
        return `${secs}s`;
    if (secs < 3600)
        return `${Math.floor(secs / 60)}m`;
    if (secs < 86400)
        return `${Math.floor(secs / 3600)}h`;
    return `${Math.floor(secs / 86400)}d`;
}

export function basename(path) {
    const parts = clean(path).split('/').filter(p => p);
    return parts.length ? parts[parts.length - 1] : clean(path);
}

export class UiState {
    constructor() {
        this.connected = false;
        this.sessions = new Map(); // keyString -> session
        this.hosts = new Map(); // alias -> host, in daemon order
        this.usage = new Map(); // host/agent -> subscription usage reading
    }

    disconnect() {
        this.connected = false;
        this.sessions.clear();
        this.hosts.clear();
        this.usage.clear();
    }

    // Update from one message. Returns the message if it asks for a notification.
    apply(msg) {
        if (!msg || typeof msg !== 'object')
            return null;
        switch (msg.type) {
        case 'snapshot':
            this.connected = true;
            this.sessions.clear();
            for (const s of Array.isArray(msg.sessions) ? msg.sessions : []) {
                if (s && typeof s === 'object')
                    this.sessions.set(keyString(keyOf(s.key)), s);
            }
            this.hosts.clear();
            for (const h of Array.isArray(msg.hosts) ? msg.hosts : [])
                this._setHost(h);
            this.usage.clear();
            for (const u of Array.isArray(msg.usage) ? msg.usage : [])
                this._setUsage(u);
            break;
        case 'session_update':
            if (msg.session && typeof msg.session === 'object')
                this.sessions.set(keyString(keyOf(msg.session.key)), msg.session);
            break;
        case 'session_remove':
            this.sessions.delete(keyString(keyOf(msg.key)));
            break;
        case 'host_status':
            this._setHost(msg.host);
            break;
        case 'host_remove':
            this.hosts.delete(clean(msg.alias));
            break;
        case 'usage_update':
            this._setUsage(msg.usage);
            break;
        case 'usage_remove':
            this.usage.delete(keyString([clean(msg.host), clean(msg.agent)]));
            break;
        case 'notify':
            return msg;
        }
        return null;
    }

    _setHost(h) {
        if (h && typeof h === 'object')
            this.hosts.set(clean(h.alias), h);
    }

    _setUsage(u) {
        if (u && typeof u === 'object')
            this.usage.set(keyString([clean(u.host), clean(u.agent)]), u);
    }

    hostTitle(alias) {
        if (alias === 'local')
            return 'This computer';
        const h = this.hosts.get(alias);
        const name = h ? clean(h.name, 30) : '';
        return name && name !== alias ? `${name} (${alias})` : alias;
    }
}

function statusOf(s) {
    return Object.hasOwn(STATUS, s.status) ? s.status : 'stale';
}

// ---- top bar --------------------------------------------------------------------

// Counts for the badges, the panel's one-line summary and the hosts that are down.
export function summary(state) {
    if (!state.connected) {
        return {
            level: 'offline', counts: {attention: 0, running: 0, waiting: 0},
            agents: [], total: 0, lead: '', text: 'Not connected', hostsDown: [],
        };
    }
    const counts = {attention: 0, running: 0, waiting: 0};
    const agents = new Set();
    let lead = '';
    for (const s of state.sessions.values()) {
        if (s.status === 'needs_attention') {
            counts.attention++;
            lead = lead || clean(basename(s.cwd), LIMITS.project);
        } else if (s.status === 'running') {
            counts.running++;
        } else if (s.status === 'waiting_input') {
            counts.waiting++;
        } else {
            continue;
        }
        agents.add(keyOf(s.key)[1]);
    }
    const hostsDown = [];
    for (const [alias, h] of state.hosts) {
        const level = HOST_LEVEL[clean(h.state)];
        if (alias !== 'local' && (level === 'pending' || level === 'error'))
            hostsDown.push(alias);
    }
    const parts = [];
    if (counts.attention)
        parts.push(`${counts.attention} needs you`);
    if (counts.running)
        parts.push(`${counts.running} running`);
    if (counts.waiting)
        parts.push(`${counts.waiting} waiting`);
    if (hostsDown.length)
        parts.push(`${hostsDown.length} host${hostsDown.length > 1 ? 's' : ''} unreachable`);
    let level = 'idle';
    if (counts.attention)
        level = 'attention';
    else if (counts.running)
        level = 'busy';
    else if (counts.waiting)
        level = 'waiting';
    return {
        level, counts, agents: sortAgents(agents), lead,
        total: counts.attention + counts.running + counts.waiting,
        text: parts.join(' · ') || 'No active sessions', hostsDown,
    };
}

// ---- panel -----------------------------------------------------------------------

export function card(s, now) {
    const [, agent, sid] = keyOf(s.key);
    const status = statusOf(s);
    const prompt = clean(s.prompt_preview, LIMITS.subtitle);
    const message = clean(s.message, LIMITS.subtitle);
    let subtitle = prompt;
    if (status === 'needs_attention' && message)
        subtitle = message;
    else if (status === 'stale')
        subtitle = prompt || 'Last seen running';
    return {
        id: sid,
        agent,
        agentName: AGENT_NAME[agent] ?? agent,
        host: keyOf(s.key)[0] === 'local' ? 'This computer' : keyOf(s.key)[0],
        project: clean(basename(s.cwd), LIMITS.project) || '?',
        cwd: clean(s.cwd),
        subtitle,
        status,
        statusLabel: STATUS[status].label,
        style: STATUS[status].style,
        tool: clean(s.tool, 30),
        toolDetail: clean(s.tool_detail, LIMITS.detail),
        age: ageText(s.last_ts, now),
    };
}

// Known agents first (in AGENT_NAME order), then any other agent by name.
function sortAgents(agents) {
    const known = Object.keys(AGENT_NAME);
    return [...agents].sort((a, b) => {
        const ia = known.indexOf(a), ib = known.indexOf(b);
        if (ia !== -1 || ib !== -1)
            return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
        return a < b ? -1 : a > b ? 1 : 0;
    });
}

// Sessions grouped by agent (Claude Code, Codex, ...), most urgent first inside a group.
// Ended sessions are not listed; see footer().
export function agentGroups(state, now) {
    const byAgent = new Map();
    for (const s of state.sessions.values()) {
        if (s.status === 'ended')
            continue;
        const agent = keyOf(s.key)[1];
        if (!byAgent.has(agent))
            byAgent.set(agent, []);
        byAgent.get(agent).push(s);
    }
    return sortAgents(byAgent.keys()).map(agent => {
        const cards = byAgent.get(agent)
            .sort((a, b) =>
                STATUS[statusOf(a)].order - STATUS[statusOf(b)].order ||
                (b.last_ts || 0) - (a.last_ts || 0))
            .map(s => card(s, now));
        return {agent, name: AGENT_NAME[agent] ?? agent, count: cards.length, cards};
    });
}

// Remote hosts in daemon order, with how many live sessions each has. This computer
// is only listed when it has a problem (it never does today).
export function hostRows(state) {
    const live = new Map();
    for (const s of state.sessions.values()) {
        if (s.status !== 'ended') {
            const host = keyOf(s.key)[0];
            live.set(host, (live.get(host) ?? 0) + 1);
        }
    }
    const rows = [];
    for (const [alias, h] of state.hosts) {
        if (alias === 'local')
            continue;
        const hostState = clean(h.state);
        const level = HOST_LEVEL[hostState] ?? 'off';
        rows.push({
            alias,
            title: state.hostTitle(alias),
            level,
            stateText: HOST_STATE_TEXT[hostState] ?? (hostState || 'unknown'),
            problem: level === 'pending' || level === 'error'
                ? clean(h.detail, LIMITS.hostDetail) : '',
            sessions: live.get(alias) ?? 0,
        });
    }
    return rows;
}

// ---- subscription usage ---------------------------------------------------------------

// "5h", "Week", "2d" ... for a window length in minutes.
export function windowLabel(minutes) {
    if (typeof minutes !== 'number' || !(minutes > 0))
        return 'Limit';
    if (minutes === 10080)
        return 'Week';
    if (minutes % 1440 === 0)
        return `${minutes / 1440}d`;
    if (minutes % 60 === 0)
        return `${minutes / 60}h`;
    return `${Math.floor(minutes)}m`;
}

// "2h 10m", "4d 3h", "45m": the two largest units.
export function durationText(secs) {
    secs = Math.max(0, Math.floor(secs));
    const days = Math.floor(secs / 86400);
    const hours = Math.floor((secs % 86400) / 3600);
    const minutes = Math.floor((secs % 3600) / 60);
    if (days)
        return `${days}d ${hours}h`;
    if (hours)
        return `${hours}h ${minutes}m`;
    return `${Math.max(minutes, 1)}m`;
}

function usageRow(w, now) {
    const label = windowLabel(w.window_minutes);
    const resetsAt = typeof w.resets_at === 'number' && w.resets_at > 0 ? w.resets_at : 0;
    if (resetsAt && resetsAt <= now) {
        // The window has reset since the last reading: the old percentage means nothing.
        return {label, percent: null, fraction: 0, level: 'off', resetText: 'reset, waiting for the next update'};
    }
    const percent = Math.min(100, Math.max(0, w.used_percent));
    return {
        label,
        percent: Math.round(percent),
        fraction: percent / 100,
        level: percent >= USAGE_CRIT ? 'crit' : percent >= USAGE_WARN ? 'warn' : 'ok',
        resetText: resetsAt ? `resets in ${durationText(resetsAt - now)}` : '',
    };
}

// One card per agent that has a reading: the freshest one when several hosts report
// (the account is the same in the common case). Windows without a usable percentage are
// skipped; an agent without any is not listed.
export function usageCards(state, now) {
    const best = new Map();
    for (const u of state.usage.values()) {
        const agent = clean(u.agent);
        const ts = typeof u.updated_ts === 'number' ? u.updated_ts : 0;
        const have = best.get(agent);
        if (!have || ts > have.ts)
            best.set(agent, {u, ts});
    }
    const cards = [];
    for (const agent of sortAgents(best.keys())) {
        const {u, ts} = best.get(agent);
        const rows = (Array.isArray(u.windows) ? u.windows : [])
            .filter(w => w && typeof w === 'object' &&
                typeof w.used_percent === 'number' && Number.isFinite(w.used_percent))
            .map(w => usageRow(w, now));
        if (!rows.length)
            continue;
        const host = clean(u.host);
        const plan = clean(u.plan, 16);
        cards.push({
            agent,
            name: AGENT_NAME[agent] ?? agent,
            plan: plan ? plan[0].toUpperCase() + plan.slice(1) : '',
            via: host && host !== 'local' ? state.hostTitle(host) : '',
            updatedText: ts > 0 ? `updated ${ageText(ts, now)} ago` : '',
            stale: !(ts > 0) || now - ts > USAGE_STALE_S,
            rows,
        });
    }
    return cards;
}

// Host chips wrapped into rows that fit the panel. Clutter's FlowLayout under-reports its
// height inside a vertical box (the last rows overlap what follows), so rows are fixed here,
// from an estimate of each chip's width (monospace text plus padding).
export function chipRows(aliases, maxWidth = 350, charWidth = 7.5, padding = 22, gap = 6) {
    const rows = [];
    let row = [], used = 0;
    for (const alias of aliases) {
        const w = Array.from(alias).length * charWidth + padding;
        if (row.length && used + gap + w > maxWidth) {
            rows.push(row);
            row = [];
            used = 0;
        }
        used += (row.length ? gap : 0) + w;
        row.push(alias);
    }
    if (row.length)
        rows.push(row);
    return rows;
}

// One line for the collapsed Servers header: "2 connected", "1 unreachable", ...
export function serversSummary(rows) {
    if (!rows.length)
        return {text: 'none yet', level: 'off'};
    const down = rows.filter(r => r.level === 'pending' || r.level === 'error').length;
    if (down)
        return {text: `${down} unreachable`, level: 'error'};
    const ok = rows.filter(r => r.level === 'ok').length;
    if (ok === rows.length)
        return {text: `${ok} connected`, level: 'ok'};
    return {text: `${ok} of ${rows.length} connected`, level: 'off'};
}

// Panel colours: the user's choice, else the desktop's colour scheme. GNOME Shell's own
// menus are dark unless the desktop asks for light, so dark is the default.
export function resolveTheme(choice, colorScheme) {
    if (choice === 'light' || choice === 'dark')
        return choice;
    return colorScheme === 'prefer-light' ? 'light' : 'dark';
}

export function footer(state) {
    let ended = 0;
    for (const s of state.sessions.values()) {
        if (s.status === 'ended')
            ended++;
    }
    return {ended: ended ? `${ended} ended recently` : ''};
}

// ---- adding a server -----------------------------------------------------------------

// Same rule as agentail.sshconfig.valid_alias: a name ssh cannot take for an option.
const ALIAS = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$/;

export function validAlias(alias) {
    return typeof alias === 'string' && ALIAS.test(alias);
}

// Output of `agentail list-ssh-hosts`: one alias per line; anything odd is dropped.
export function parseAliases(stdout) {
    const seen = new Set();
    for (const line of String(stdout ?? '').split('\n')) {
        const alias = line.trim();
        if (validAlias(alias))
            seen.add(alias);
    }
    // Natural order: gpu2 before gpu10.
    return [...seen].sort((a, b) => a.localeCompare(b, 'en', {numeric: true}));
}

// What to tell the user after `agentail add-host` ran: the last line it printed
// (preferring an "error:" line), cleaned and bounded.
export function addHostMessage(output, ok) {
    const lines = String(output ?? '').split('\n').map(l => l.trim()).filter(l => l);
    const error = lines.filter(l => l.startsWith('error:')).pop();
    const text = ok ? lines[lines.length - 1] : (error ?? lines[lines.length - 1]);
    return clean(text ?? (ok ? 'Done' : 'Failed'), 240);
}

// "Added gpu7. connected (end-to-end ping received)": the tool's last line, minus its own
// "gpu7:" prefix.
export function addedMessage(alias, output) {
    const last = addHostMessage(output, true);
    const tail = last.startsWith(`${alias}: `) ? last.slice(alias.length + 2) : last;
    return `Added ${alias}.${tail && tail !== 'Done' ? ` ${tail}` : ''}`;
}

export function removedMessage(alias, output) {
    const shared = String(output ?? '').includes('shares its $HOME');
    return `Removed ${alias}.${shared ? ' Its settings stay: another server shares the same home.' : ''}`;
}

// ---- notifications ------------------------------------------------------------------

export function notice(msg, state) {
    const kind = msg?.kind;
    if (kind !== 'turn_done' && kind !== 'attention')
        return null;
    const key = keyOf(msg.key);
    const known = state.sessions.get(keyString(key));
    const c = card(known ?? {key: msg.key}, 0);
    const where = key[0] === 'local' ? 'this computer' : key[0];
    let detail = known ? c.subtitle : '';
    if (kind === 'attention' && c.tool && c.toolDetail)
        detail = `${c.agentName} wants to run ${c.tool}: ${c.toolDetail}`;
    return {
        key: keyString(key),
        kind,
        agent: key[1],
        title: kind === 'attention' ? `${c.project} needs you` : `${c.project} is done`,
        body: [where, detail].filter(x => x).join(' · '),
        urgent: kind === 'attention',
    };
}

export class RateLimiter {
    constructor(interval = NOTIFY_INTERVAL_S) {
        this.interval = interval;
        this._last = new Map();
    }

    allow(n, now) {
        const k = `${n.key}|${n.kind}`;
        const last = this._last.get(k);
        if (last !== undefined && now - last < this.interval)
            return false;
        this._last.set(k, now);
        if (this._last.size > 1000) {
            for (const [key, t] of this._last) {
                if (now - t >= this.interval)
                    this._last.delete(key);
            }
        }
        return true;
    }
}
