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
    }

    disconnect() {
        this.connected = false;
        this.sessions.clear();
        this.hosts.clear();
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
        case 'notify':
            return msg;
        }
        return null;
    }

    _setHost(h) {
        if (h && typeof h === 'object')
            this.hosts.set(clean(h.alias), h);
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
            text: 'Not connected', hostsDown: [],
        };
    }
    const counts = {attention: 0, running: 0, waiting: 0};
    for (const s of state.sessions.values()) {
        if (s.status === 'needs_attention')
            counts.attention++;
        else if (s.status === 'running')
            counts.running++;
        else if (s.status === 'waiting_input')
            counts.waiting++;
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
    return {level, counts, text: parts.join(' · ') || 'No active sessions', hostsDown};
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

// Sessions grouped by host: this computer first, then hosts in daemon order, then
// hosts that only appear in sessions. Ended sessions are only counted.
export function groups(state, now) {
    const byHost = new Map();
    for (const s of state.sessions.values()) {
        const host = keyOf(s.key)[0];
        if (!byHost.has(host))
            byHost.set(host, []);
        byHost.get(host).push(s);
    }
    const aliases = [];
    if (state.hosts.has('local') || byHost.has('local'))
        aliases.push('local');
    for (const a of state.hosts.keys()) {
        if (a !== 'local')
            aliases.push(a);
    }
    for (const a of [...byHost.keys()].sort()) {
        if (!aliases.includes(a))
            aliases.push(a);
    }

    const out = [];
    for (const alias of aliases) {
        const sessions = byHost.get(alias) ?? [];
        const live = sessions
            .filter(s => s.status !== 'ended')
            .sort((a, b) =>
                STATUS[statusOf(a)].order - STATUS[statusOf(b)].order ||
                (b.last_ts || 0) - (a.last_ts || 0));
        const h = state.hosts.get(alias);
        const hostState = h ? clean(h.state) : 'unknown';
        const level = HOST_LEVEL[hostState] ?? 'off';
        out.push({
            alias,
            title: state.hostTitle(alias),
            local: alias === 'local',
            level,
            stateText: HOST_STATE_TEXT[hostState] ?? (hostState || 'unknown'),
            problem: level === 'pending' || level === 'error'
                ? clean(h?.detail, LIMITS.hostDetail) : '',
            cards: live.map(s => card(s, now)),
            ended: sessions.length - live.length,
        });
    }
    return out;
}

export function footer(state) {
    let ended = 0;
    for (const s of state.sessions.values()) {
        if (s.status === 'ended')
            ended++;
    }
    const remote = [...state.hosts].filter(([a]) => a !== 'local');
    const down = summary(state).hostsDown.length;
    const connected = remote.filter(([, h]) => HOST_LEVEL[clean(h.state)] === 'ok').length;
    let hosts = '';
    if (remote.length) {
        hosts = `${remote.length} host${remote.length > 1 ? 's' : ''}`;
        if (down)
            hosts += ` · ${down} unreachable`;
        else if (connected === remote.length)
            hosts += ' · all connected';
        else
            hosts += ` · ${connected} connected`;
    }
    return {ended: ended ? `${ended} ended in the last 10 minutes` : '', hosts};
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
