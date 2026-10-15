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
    error: 'error',
    stopped: 'off',
};

const HOST_STATE_TEXT = {
    local: 'local',
    connected: 'connected',
    connecting: 'connecting',
    backoff: 'reconnecting',
    auth_failed: 'login failed',
    error: 'error',
    stopped: 'not set up',
};

// Same names and order as agentail.agents.AGENTS (tests/test_gnome_extension.py checks it).
// Every agent here needs icons/agent-<name>.png and .agentail-chip-<name> in stylesheet.css.
export const AGENT_NAME = {
    claude: 'Claude Code',
    codex: 'Codex',
    qoder: 'Qoder',
    qoder_cn: 'Qoder CN',
};

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

// The key exactly as the daemon sent it, to send back in a request (dismiss_session);
// null unless its three fields are strings. Never displayed: keyOf() is for that.
export function wireKey(key) {
    if (!key || typeof key !== 'object')
        return null;
    const {host, agent, session_id: sid} = key;
    if (typeof host !== 'string' || typeof agent !== 'string' || typeof sid !== 'string')
        return null;
    return {host, agent, session_id: sid};
}

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
        this.usageApi = new Map(); // agent -> {state, servers, okTs} of the daemon's usage query
        this.settings = {usage: null}; // the daemon's SETTINGS; null: not reported
    }

    disconnect() {
        this.connected = false;
        this.sessions.clear();
        this.hosts.clear();
        this.usage.clear();
        this.usageApi.clear();
        this.settings = {usage: null};
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
            this.usageApi.clear();
            if (msg.usage_api && typeof msg.usage_api === 'object') {
                for (const [agent, status] of Object.entries(msg.usage_api))
                    this._setUsageApi(agent, status);
            }
            this._setSettings(msg.settings);
            break;
        case 'settings':
            this._setSettings(msg.settings);
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
        case 'usage_api_status':
            this._setUsageApi(msg.agent, msg.status);
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

    _setSettings(settings) {
        const usage = settings && typeof settings === 'object' ? settings.usage : null;
        this.settings = {usage: typeof usage === 'boolean' ? usage : null};
    }

    _setUsageApi(agent, status) {
        if (status && typeof status === 'object') {
            this.usageApi.set(clean(agent), {
                state: clean(status.state, 20),
                servers: status.servers === true,
                okTs: typeof status.ok_ts === 'number' ? status.ok_ts : null,
            });
        }
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
        key: wireKey(s.key),
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

// "max_5x" -> "Max 5x", "plus" -> "Plus".
export function planText(plan) {
    const words = clean(plan, 16).split(/[_-]+/).filter(x => x);
    if (!words.length)
        return '';
    words[0] = words[0][0].toUpperCase() + words[0].slice(1);
    return words.join(' ');
}

// The label column of a usage card: wide enough for its longest label ("Week · Fable"),
// estimated from the character count since St cannot measure text before it is shown.
export function usageLabelWidth(labels, charWidth = 6.6, min = 34, max = 130) {
    const longest = Math.max(0, ...labels.map(l => Array.from(l).length));
    return Math.min(max, Math.max(min, Math.ceil(longest * charWidth + 4)));
}

function usageRow(w, now) {
    const scope = clean(w.label, 24);
    const label = scope ? `${windowLabel(w.window_minutes)} · ${scope}` : windowLabel(w.window_minutes);
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

// "$8.61", "12.5 EUR", "40" (credits): an amount from the daemon, never trusted text.
export function amountText(amount, currency) {
    const money = typeof currency === 'string' && /^[A-Z]{3}$/.test(currency);
    const text = money ? amount.toFixed(2) : String(Number(amount.toFixed(2)));
    if (!money)
        return text;
    return currency === 'USD' ? `$${text}` : `${text} ${currency}`;
}

const amountOk = v => typeof v === 'number' && Number.isFinite(v) && v >= 0;

// Why an extra that is switched on cannot be used (agentail.usage.UsageExtra.inactive):
// only known codes are put into words. Keep in sync with client.INACTIVE_TEXT.
export const INACTIVE_TEXT = {out_of_credits: 'out of credits'};
const inactiveText = code => INACTIVE_TEXT[code] ?? 'not active';

// A row for an extra (agentail.usage.UsageExtra): money spent beyond the plan, with a bar
// when it has a cap, or a balance left, without one.
export function extraRow(e) {
    if (!e || typeof e !== 'object')
        return null;
    const label = clean(e.label, 24);
    if (!label)
        return null;
    const amount = amountOk(e.amount) ? e.amount : null;
    const off = {label, bar: false, percent: null, fraction: 0, level: 'off'};
    if (e.kind === 'balance') {
        if (e.unlimited === true)
            return {...off, resetText: 'unlimited'};
        return amount === null ? null : {...off, resetText: `${amountText(amount, e.currency)} left`};
    }
    if (e.kind !== 'spent' || amount === null)
        return null;
    const capped = amountOk(e.limit) && e.limit > 0;
    if (typeof e.inactive === 'string' && e.inactive) {
        // Switched on but not usable: grey, with the reason.
        const amounts = capped
            ? `${amountText(amount, e.currency)} of ${amountText(e.limit, e.currency)}`
            : `${amountText(amount, e.currency)} spent`;
        return {...off, bar: capped, resetText: `${amounts} · ${inactiveText(e.inactive)}`};
    }
    if (!capped)
        return {...off, resetText: `${amountText(amount, e.currency)} spent`};
    const percent = Math.min(100, amount / e.limit * 100);
    return {
        label,
        bar: true,
        percent: Math.round(percent),
        fraction: percent / 100,
        level: percent >= USAGE_CRIT ? 'crit' : percent >= USAGE_WARN ? 'warn' : 'ok',
        resetText: `${amountText(amount, e.currency)} of ${amountText(e.limit, e.currency)}`,
    };
}

// Accounts, as agentail.usage.group_by_account (Python) tells them apart: same agent and
// weekly windows that reset within SAME_ACCOUNT_S of each other. Keep the two in sync.
export const SAME_ACCOUNT_S = 600;
const WEEK_MINUTES = 10080;

const usableWindow = w => w && typeof w === 'object' &&
    typeof w.used_percent === 'number' && Number.isFinite(w.used_percent);

function weekReset(u) {
    for (const w of u.windows) {
        if (w.window_minutes === WEEK_MINUTES && !w.label &&
            typeof w.resets_at === 'number' && w.resets_at > 0)
            return w.resets_at;
    }
    return null;
}

// USAGE records grouped by agent and account, freshest record first in each group and
// freshest group first. A record without a usable weekly window joins its own host's group
// when there is exactly one, else its agent's only group; one whose week has already reset
// is left out when its agent has a current group. Records without a usable window are left out.
export function usageGroups(records, now) {
    const items = [];
    for (const u of records) {
        if (!u || typeof u !== 'object' || !Array.isArray(u.windows))
            continue;
        const windows = u.windows.filter(usableWindow);
        if (!windows.length)
            continue;
        const r = {u, agent: clean(u.agent), host: clean(u.host), windows,
            ts: typeof u.updated_ts === 'number' && Number.isFinite(u.updated_ts) ? u.updated_ts : 0};
        r.reset = weekReset(r);
        items.push(r);
    }
    items.sort((a, b) => b.ts - a.ts);
    const groups = [];
    const later = [];
    for (const r of items) {
        const fp = r.reset !== null && r.reset > now ? r.reset : null;
        if (fp === null) {
            later.push(r);
            continue;
        }
        const g = groups.find(x => x.agent === r.agent && x.rep !== null &&
            Math.abs(x.rep - fp) <= SAME_ACCOUNT_S);
        if (g)
            g.members.push(r);
        else
            groups.push({agent: r.agent, rep: fp, members: [r]});
    }
    const current = new Set(groups.map(g => g.agent));
    for (const r of later) {
        if (r.reset !== null && current.has(r.agent))
            continue; // its week is over: outdated
        const theirs = groups.filter(g => g.agent === r.agent);
        const mine = theirs.filter(g => g.members.some(m => m.host === r.host));
        if (mine.length === 1)
            mine[0].members.push(r);
        else if (theirs.length === 1)
            theirs[0].members.push(r);
        else
            groups.push({agent: r.agent, rep: null, members: [r]});
    }
    for (const g of groups)
        g.members.sort((a, b) => b.ts - a.ts);
    groups.sort((a, b) => b.members[0].ts - a.members[0].ts);
    return groups.map(g => g.members);
}

// One card per agent and account, showing the freshest record of that account. With one
// account the card names the host only when the record came from a server ("via gpu8");
// with several, every card lists the hosts it is seen on. Windows without a usable
// percentage are skipped; an agent without any is not listed.
// Where a card's numbers come from (USAGE "source"), as one line at its foot. The host is
// named in the card's head ("via gpu7"), except for a server's own login, which is the point.
const SOURCE_TEXT = {
    api: () => "From Anthropic, with this computer's Claude login",
    server_api: host => `From Anthropic, with the Claude login on ${host}`,
    status_line: () => 'From a Claude session in a terminal',
    app_server: () => 'From Codex',
    session_files: () => "From Codex's session files",
};

export function sourceText(u, state) {
    const text = SOURCE_TEXT[clean(u?.source)];
    return text ? text(state.hostTitle(clean(u.host))) : '';
}

export function usageCards(state, now) {
    const byAgent = new Map();
    for (const members of usageGroups(state.usage.values(), now)) {
        const agent = members[0].agent;
        if (!byAgent.has(agent))
            byAgent.set(agent, []);
        byAgent.get(agent).push(members);
    }
    const cards = [];
    for (const agent of sortAgents(byAgent.keys())) {
        const groups = byAgent.get(agent);
        // This computer's account first, then the freshest.
        groups.sort((a, b) => Number(b.some(m => m.host === 'local')) - Number(a.some(m => m.host === 'local')));
        for (const members of groups) {
            const {u, ts, host, windows} = members[0];
            const extras = Array.isArray(u.extras) ? u.extras : [];
            const rows = [...windows.map(w => usageRow(w, now)), ...extras.map(extraRow).filter(r => r)];
            const hosts = [...new Set(members.map(m => m.host))]
                .sort((a, b) => Number(b === 'local') - Number(a === 'local'));
            const local = hosts.includes('local');
            cards.push({
                agent,
                name: AGENT_NAME[agent] ?? agent,
                plan: planText(members.map(m => clean(m.u.plan, 16)).find(p => p)),
                via: host && host !== 'local' ? state.hostTitle(host) : '',
                sources: groups.length > 1
                    ? hosts.map(h => h === 'local' ? 'this computer' : state.hostTitle(h)).join(' · ')
                    : '',
                updatedText: local && state.usageApi.get(agent)?.state === 'needs_login'
                    ? `sign-in expired · run ${agent} once`
                    : ts > 0 ? `updated ${ageText(ts, now)} ago` : '',
                stale: !(ts > 0) || now - ts > USAGE_STALE_S,
                sourceText: sourceText(u, state),
                rows,
                labelWidth: usageLabelWidth(rows.map(r => r.label)),
            });
        }
    }
    return cards;
}

// ---- settings: the two switches of the Claude usage query ------------------------------
//
// Both are off until the user turns them on (docs/usage-api-design.md section 4). The panel
// writes them with `agentail usage-api`; the daemon reports them back in usage_api_status.

const claudeSeen = state =>
    [...state.sessions.values()].some(s => keyOf(s?.key)[1] === 'claude') ||
    [...state.usage.values()].some(u => clean(u?.agent) === 'claude');

// The switches as the daemon reports them; null for one it does not report (not connected,
// or an older daemon).
function switchValues(state) {
    const api = state.usageApi.get('claude');
    const usage = state.settings.usage;
    return {
        usage: typeof usage === 'boolean' ? usage : null,
        local: api ? api.state !== 'off' : null,
        servers: api ? api.servers : null,
    };
}

const LOCAL_STATUS = {
    pending: 'Checking…',
    needs_login: 'Sign-in expired: run claude once',
    error: 'The last query failed: see agentail doctor',
};

// The settings page: the Usage switch, then (while it is on) Claude's two. `pending`: values
// the user just chose that the daemon has not reported yet; they are shown (as "saving")
// instead of the daemon's.
export function usageSettings(state, pending = {}, now = 0) {
    const actual = switchValues(state);
    const hasServers = [...state.hosts.keys()].some(alias => alias !== 'local');
    const row = (id, title, notes, enabled) => {
        const want = typeof pending[id] === 'boolean' ? pending[id] : null;
        const known = actual[id] !== null;
        return {
            id, title, notes,
            on: want ?? actual[id] ?? false,
            enabled: known && enabled,
            saving: known && want !== null && want !== actual[id],
            status: '',
        };
    };
    const usage = row('usage', 'Show usage limits',
        ["Claude's and Codex's limits in this panel. Off: Agentail collects none."], true);
    const api = state.usageApi.get('claude');
    const local = row('local', "Ask Anthropic with this computer's login",
        ['Checks after every Claude turn and when you open this panel.'], true);
    if (local.on && api) {
        const ago = api.state === 'ok' ? ageText(api.okTs, now) : '';
        local.status = ago ? `Last checked ${ago} ago` : LOCAL_STATUS[api.state] ?? '';
    }
    const servers = row('servers', "Ask Anthropic with each server's login",
        [hasServers
            ? 'For servers with a different Claude account. The token stays on the server.'
            : 'For servers with a different Claude account. Add a server first.'],
        hasServers || actual.servers === true);
    return {
        available: actual.usage !== null,
        usage,
        // Claude's switches only matter while usage is shown at all.
        showClaude: usage.on && actual.local !== null,
        intro: 'Claude reports its limits only in terminal sessions. Agentail can also ask ' +
            'Anthropic for them.',
        rows: [local, servers],
    };
}

// Drops the pending choices the daemon now reports (or that no longer differ).
export function settlePending(state, pending) {
    const actual = switchValues(state);
    const out = {};
    for (const [id, want] of Object.entries(pending)) {
        if (typeof want === 'boolean' && actual[id] !== want)
            out[id] = want;
    }
    return out;
}

// One line in the Usage section while this computer's switch is off and Claude is used here:
// its limits then come only from terminal sessions. Null otherwise.
export function usageHint(state) {
    const api = state.usageApi.get('claude');
    if (state.settings.usage === false || !api || api.state !== 'off' || !claudeSeen(state))
        return null;
    return 'Claude: terminal sessions only. More in Settings (⚙, top right).';
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
    const text = String(output ?? '');
    if (text.includes('shares its $HOME'))
        return `Removed ${alias}. The hooks stay for the server that shares its home.`;
    if (text.includes('other receivers still use the hooks'))
        return `Removed ${alias}. The hooks stay for the other desktops that watch it.`;
    return `Removed ${alias}.`;
}

// ---- notifications ------------------------------------------------------------------

// Banners show on the lock screen: by default a notice says only what and where (project,
// host). The prompt and the command are added only when the daemon sends "details": true
// ([notifications] details = true in config.toml); daemons before 0.0.4 send no such field.
export function notice(msg, state) {
    const kind = msg?.kind;
    if (kind !== 'turn_done' && kind !== 'attention')
        return null;
    const key = keyOf(msg.key);
    const known = state.sessions.get(keyString(key));
    const c = card(known ?? {key: msg.key}, 0);
    const where = key[0] === 'local' ? 'this computer' : key[0];
    let detail = '';
    if (msg.details === true) {
        detail = known ? c.subtitle : '';
        if (kind === 'attention' && c.tool && c.toolDetail)
            detail = `${c.agentName} wants to run ${c.tool}: ${c.toolDetail}`;
    }
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

// ---- error containment ---------------------------------------------------------------

// Run fn() and report what it throws to onError instead of the caller. The shell's main
// loop callbacks (socket reads, idle renders) use this: an exception escaping one of them
// would stop the panel from updating until GNOME Shell restarts.
export function guard(fn, onError) {
    try {
        fn();
        return true;
    } catch (e) {
        try {
            onError(e);
        } catch {
            // nothing left to report to
        }
        return false;
    }
}

// Hand one line read from ui.sock to `handle`. A line that is not JSON is skipped (the
// next snapshot repairs any gap) and an exception from `handle` goes to onError, so the
// read loop always goes on to read the next line. Returns whether `handle` succeeded.
export function dispatchLine(line, handle, onError) {
    let msg = null;
    try {
        msg = JSON.parse(line);
    } catch {
        return false;
    }
    if (!msg)
        return false;
    return guard(() => handle(msg), onError);
}
