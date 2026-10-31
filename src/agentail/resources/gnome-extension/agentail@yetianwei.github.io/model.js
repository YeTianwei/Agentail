// What the panel shows, computed from UI protocol messages (docs/protocol.md).
//
// Pure JavaScript, no GNOME imports: extension.js only renders what these
// functions return, and tests/js/test_model.js runs them under plain gjs.
// Mirrors src/agentail/ui/model.py (the AppIndicator fallback).
//
// Every string that came from the daemon is passed through clean() (no control
// characters, bounded length) before it reaches a view object, and the panel
// renders it as plain text only (St.Label text, never markup).
//
// Text of our own goes through _() (i18n.js): in the language set with setLanguage().

import {_, N_, language} from './i18n.js';

export const STATUS = {
    needs_attention: {label: N_('Needs you'), order: 0, style: 'attention'},
    waiting_input: {label: N_('Your turn'), order: 1, style: 'waiting'},
    running: {label: N_('Running'), order: 2, style: 'running'},
    stale: {label: N_('Out of date'), order: 3, style: 'stale'},
    ended: {label: N_('Ended'), order: 4, style: 'ended'},
};

// Tunnel state -> level: ok | pending | error | off. Connecting is not an outage by itself
// (every host connects after a daemon start or a resume); see UiState.troubled.
const HOST_LEVEL = {
    local: 'ok',
    connected: 'ok',
    connecting: 'off',
    backoff: 'pending',
    auth_failed: 'error',
    error: 'error',
    stopped: 'off',
};

// Tunnel states that mean a remote host is unreachable (agentail.ui.model.DOWN_STATES).
const DOWN_STATES = ['backoff', 'auth_failed', 'error'];

const HOST_STATE_TEXT = {
    local: N_('local'),
    connected: N_('connected'),
    connecting: N_('connecting'),
    backoff: N_('reconnecting'),
    auth_failed: N_('login failed'),
    error: N_('error'),
    stopped: N_('not set up'),
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
// Approvals (0.0.6): a request's command as the daemon sends it, in full (it holds none longer).
export const APPROVAL_DETAIL_CHARS = 4096;
// After this long without input the panel tells the daemon nobody can answer here.
export const READY_IDLE_S = 300;
const APPROVAL_ID = /^[0-9a-f]{32}$/;
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

// clean() that keeps line breaks: a command shown for approval keeps its lines (a heredoc, a
// script), every other control character still becomes "?".
export function cleanLines(value) {
    const text = typeof value === 'string' ? value : '';
    return text.replace(/\r\n?/g, '\n').split('\n').map(line => clean(line)).join('\n');
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
        return _('{n}s', {n: secs});
    if (secs < 3600)
        return _('{n}m', {n: Math.floor(secs / 60)});
    if (secs < 86400)
        return _('{n}h', {n: Math.floor(secs / 3600)});
    return _('{n}d', {n: Math.floor(secs / 86400)});
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
        this.settings = {usage: null, notifications: null, approvals: null, tokens: null}; // the daemon's SETTINGS; null: not reported
        this.approvals = new Map(); // id -> held permission request (docs/protocol.md, "Approvals")
        this.tokens = null; // the daemon's TOKENS record, checked (tokensOf); null: off or not reported
        // Remote hosts that failed and have not connected since -> the last reason. A host
        // retrying goes backoff -> connecting -> backoff ...: it stays down while connecting.
        this.troubled = new Map();
    }

    disconnect() {
        this.connected = false;
        this.sessions.clear();
        this.hosts.clear();
        this.troubled.clear();
        this.usage.clear();
        this.usageApi.clear();
        this.approvals.clear();
        this.settings = {usage: null, notifications: null, approvals: null, tokens: null};
        this.tokens = null;
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
            this.troubled.clear();
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
            this.approvals.clear();
            for (const a of Array.isArray(msg.approvals) ? msg.approvals : [])
                this._setApproval(a);
            this.tokens = tokensOf(msg.tokens);
            break;
        case 'tokens_update':
            this.tokens = tokensOf(msg.tokens);
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
            this.troubled.delete(clean(msg.alias));
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
        case 'approval':
            this._setApproval(msg.approval);
            break;
        case 'approval_remove':
            this.approvals.delete(clean(msg.id));
            break;
        case 'notify':
            return msg;
        }
        return null;
    }

    _setHost(h) {
        if (!h || typeof h !== 'object')
            return;
        const alias = clean(h.alias), state = clean(h.state);
        this.hosts.set(alias, h);
        if (DOWN_STATES.includes(state))
            this.troubled.set(alias, clean(h.detail, LIMITS.hostDetail) || this.troubled.get(alias) || '');
        else if (state !== 'connecting')
            this.troubled.delete(alias);
    }

    _setUsage(u) {
        if (u && typeof u === 'object')
            this.usage.set(keyString([clean(u.host), clean(u.agent)]), u);
    }

    _setSettings(settings) {
        const s = settings && typeof settings === 'object' ? settings : {};
        const n = s.notifications && typeof s.notifications === 'object' ? s.notifications : null;
        const flag = v => typeof v === 'boolean' ? v : null;
        this.settings = {
            usage: flag(s.usage),
            // Since 0.0.5; null from an older daemon.
            notifications: n && {turn_done: flag(n.turn_done), attention: flag(n.attention), details: flag(n.details)},
            // Since 0.0.6.
            approvals: flag(s.approvals),
            tokens: s.tokens && typeof s.tokens === 'object'
                ? {enabled: flag(s.tokens.enabled), servers: flag(s.tokens.servers)} : null,
        };
    }

    // A held request, kept only if it is well formed. `detail` is kept whole (the user must
    // see all of what they allow); a longer one than the daemon ever sends is not kept.
    _setApproval(a) {
        if (!a || typeof a !== 'object' || typeof a.id !== 'string' || !APPROVAL_ID.test(a.id))
            return;
        if (typeof a.detail !== 'string' || !a.detail || Array.from(a.detail).length > APPROVAL_DETAIL_CHARS)
            return;
        this.approvals.set(a.id, {
            id: a.id,
            session: keyString(keyOf(a.key)),
            tool: clean(a.tool, 30),
            detail: cleanLines(a.detail),
            description: clean(a.description, 200),
            expiresTs: typeof a.expires_ts === 'number' ? a.expires_ts : 0,
            sent: null,
        });
    }

    // The user clicked Allow or Deny: the buttons go until the daemon removes the request.
    markSent(id, decision) {
        const a = this.approvals.get(id);
        if (a)
            a.sent = decision;
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
            return _('This computer');
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
            agents: [], total: 0, lead: '', text: _('Not connected'), hostsDown: [],
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
    const hostsDown = [...state.hosts.keys()].filter(a => a !== 'local' && state.troubled.has(a));
    const parts = [];
    if (counts.attention)
        parts.push(_('{n} needs you', {n: counts.attention}));
    if (counts.running)
        parts.push(_('{n} running', {n: counts.running}));
    if (counts.waiting)
        parts.push(_('{n} waiting', {n: counts.waiting}));
    if (hostsDown.length) {
        const n = hostsDown.length;
        parts.push(n > 1 ? _('{n} hosts unreachable', {n}) : _('{n} host unreachable', {n}));
    }
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
        text: parts.join(' · ') || _('No active sessions'), hostsDown,
    };
}

// ---- panel -----------------------------------------------------------------------

// The request held for a session, the one that expires last; null if there is none.
export function approvalOf(state, s, now) {
    const sk = keyString(keyOf(s.key));
    let found = null;
    for (const a of state.approvals.values()) {
        if (a.session === sk && (!found || a.expiresTs > found.expiresTs))
            found = a;
    }
    if (!found)
        return null;
    const left = Math.max(0, Math.ceil(found.expiresTs - now));
    return {
        id: found.id,
        tool: found.tool,
        detail: found.detail,
        description: found.description,
        sent: found.sent,
        leftText: found.sent ? _('Sending…') : _('{n}s left', {n: left}),
    };
}

export function card(s, now) {
    const [, agent, sid] = keyOf(s.key);
    const status = statusOf(s);
    const prompt = clean(s.prompt_preview, LIMITS.subtitle);
    const message = clean(s.message, LIMITS.subtitle);
    let subtitle = prompt;
    if (status === 'needs_attention' && message)
        subtitle = message;
    else if (status === 'stale')
        subtitle = prompt || _('Last seen running');
    const local = keyOf(s.key)[0] === 'local';
    return {
        id: sid,
        key: wireKey(s.key),
        agent,
        agentName: AGENT_NAME[agent] ?? agent,
        local,
        host: local ? _('This computer') : keyOf(s.key)[0],
        project: clean(basename(s.cwd), LIMITS.project) || '?',
        cwd: clean(s.cwd),
        subtitle,
        status,
        statusLabel: _(STATUS[status].label),
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
// Ended sessions are not listed: the daemon keeps them for a few minutes, and a count of
// them told nobody anything.
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
            .map(s => ({...card(s, now), approval: approvalOf(state, s, now)}));
        return {agent, name: AGENT_NAME[agent] ?? agent, count: cards.length, cards};
    });
}

// Remote hosts in daemon order, with how many live sessions each has; `down` while a host is
// unreachable (UiState.troubled), with the reason in `problem`. This computer is not listed.
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
        const down = state.troubled.has(alias);
        let level = HOST_LEVEL[hostState] ?? 'off';
        if (down && level === 'off')
            level = 'pending';  // connecting again after a failure
        rows.push({
            alias,
            title: state.hostTitle(alias),
            level,
            down,
            stateText: Object.hasOwn(HOST_STATE_TEXT, hostState)
                ? _(HOST_STATE_TEXT[hostState]) : hostState || _('unknown'),
            problem: down ? state.troubled.get(alias) : '',
            sessions: live.get(alias) ?? 0,
        });
    }
    return rows;
}

// ---- subscription usage ---------------------------------------------------------------

// "5h", "Week", "2d" ... for a window length in minutes.
export function windowLabel(minutes) {
    if (typeof minutes !== 'number' || !(minutes > 0))
        return _('Limit');
    if (minutes === 10080)
        return _('Week');
    if (minutes % 1440 === 0)
        return _('{n}d', {n: minutes / 1440});
    if (minutes % 60 === 0)
        return _('{n}h', {n: minutes / 60});
    return _('{n}m', {n: Math.floor(minutes)});
}

// "2h 10m", "4d 3h", "45m": the two largest units.
export function durationText(secs) {
    secs = Math.max(0, Math.floor(secs));
    const days = Math.floor(secs / 86400);
    const hours = Math.floor((secs % 86400) / 3600);
    const minutes = Math.floor((secs % 3600) / 60);
    if (days)
        return _('{d}d {h}h', {d: days, h: hours});
    if (hours)
        return _('{h}h {m}m', {h: hours, m: minutes});
    return _('{n}m', {n: Math.max(minutes, 1)});
}

// "max_5x" -> "Max 5x", "plus" -> "Plus".
export function planText(plan) {
    const words = clean(plan, 16).split(/[_-]+/).filter(x => x);
    if (!words.length)
        return '';
    words[0] = words[0][0].toUpperCase() + words[0].slice(1);
    return words.join(' ');
}

// Wide characters (CJK) take about two columns.
const WIDE = /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/u;
const columns = text => Array.from(text).reduce((n, ch) => n + (WIDE.test(ch) ? 2 : 1), 0);

// The label column of a usage card: wide enough for its longest label ("Week · Fable"),
// estimated from the character count since St cannot measure text before it is shown.
export function usageLabelWidth(labels, charWidth = 6.6, min = 34, max = 130) {
    const longest = Math.max(0, ...labels.map(columns));
    return Math.min(max, Math.max(min, Math.ceil(longest * charWidth + 4)));
}

function usageRow(w, now) {
    const scope = clean(w.label, 24);
    const label = scope ? `${windowLabel(w.window_minutes)} · ${scope}` : windowLabel(w.window_minutes);
    const resetsAt = typeof w.resets_at === 'number' && w.resets_at > 0 ? w.resets_at : 0;
    if (resetsAt && resetsAt <= now) {
        // The window has reset since the last reading: the old percentage means nothing.
        return {label, percent: null, fraction: 0, level: 'off', resetText: _('reset, waiting for the next update')};
    }
    const percent = Math.min(100, Math.max(0, w.used_percent));
    return {
        label,
        percent: Math.round(percent),
        fraction: percent / 100,
        level: percent >= USAGE_CRIT ? 'crit' : percent >= USAGE_WARN ? 'warn' : 'ok',
        resetText: resetsAt ? _('resets in {time}', {time: durationText(resetsAt - now)}) : '',
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
export const INACTIVE_TEXT = {out_of_credits: N_('out of credits')};
const inactiveText = code => Object.hasOwn(INACTIVE_TEXT, code) ? _(INACTIVE_TEXT[code]) : _('not active');

// Extra labels the daemon writes (agentail.daemon.usage_claude_api.EXTRA_USAGE,
// usage_codex.CREDITS): translated when known, shown as sent otherwise.
const EXTRA_LABELS = [N_('Extra usage'), N_('Credits')];

const spent = (amount, e) => _('{amount} spent', {amount: amountText(amount, e.currency)});
const ofLimit = (amount, e) =>
    _('{amount} of {limit}', {amount: amountText(amount, e.currency), limit: amountText(e.limit, e.currency)});

// A row for an extra (agentail.usage.UsageExtra): money spent beyond the plan, with a bar
// when it has a cap, or a balance left, without one.
export function extraRow(e) {
    if (!e || typeof e !== 'object')
        return null;
    const raw = clean(e.label, 24);
    if (!raw)
        return null;
    const label = EXTRA_LABELS.includes(raw) ? _(raw) : raw;
    const amount = amountOk(e.amount) ? e.amount : null;
    const off = {label, bar: false, percent: null, fraction: 0, level: 'off'};
    if (e.kind === 'balance') {
        if (e.unlimited === true)
            return {...off, resetText: _('unlimited')};
        return amount === null ? null
            : {...off, resetText: _('{amount} left', {amount: amountText(amount, e.currency)})};
    }
    if (e.kind !== 'spent' || amount === null)
        return null;
    const capped = amountOk(e.limit) && e.limit > 0;
    if (typeof e.inactive === 'string' && e.inactive) {
        // Switched on but not usable: grey, with the reason.
        const amounts = capped ? ofLimit(amount, e) : spent(amount, e);
        return {...off, bar: capped, resetText: `${amounts} · ${inactiveText(e.inactive)}`};
    }
    if (!capped)
        return {...off, resetText: spent(amount, e)};
    const percent = Math.min(100, amount / e.limit * 100);
    return {
        label,
        bar: true,
        percent: Math.round(percent),
        fraction: percent / 100,
        level: percent >= USAGE_CRIT ? 'crit' : percent >= USAGE_WARN ? 'warn' : 'ok',
        resetText: ofLimit(amount, e),
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
                    ? hosts.map(h => h === 'local' ? _('this computer') : state.hostTitle(h)).join(' · ')
                    : '',
                updatedText: local && state.usageApi.get(agent)?.state === 'needs_login'
                    ? _('sign-in expired · run {agent} once', {agent})
                    : ts > 0 ? _('updated {age} ago', {age: ageText(ts, now)}) : '',
                stale: !(ts > 0) || now - ts > USAGE_STALE_S,
                rows,
                labelWidth: usageLabelWidth(rows.map(r => r.label)),
            });
        }
    }
    return cards;
}

// ---- token counts (docs/token-stats-design.md section 7) ----------------------------------

const COUNT_FIELDS = ['input', 'cache_write', 'cache_read', 'output', 'reasoning'];
const countOk = v => Number.isSafeInteger(v) && v >= 0;

// A COUNTS record with five non-negative integers, else null.
function countsOf(c) {
    if (!c || typeof c !== 'object' || !COUNT_FIELDS.every(f => countOk(c[f])))
        return null;
    return Object.fromEntries(COUNT_FIELDS.map(f => [f, c[f]]));
}

// The agent rows of a 0.0.6 daemon's today and total.
function agentRows(rows) {
    const out = [];
    for (const r of Array.isArray(rows) ? rows : []) {
        const counts = countsOf(r?.counts);
        if (counts && typeof r.agent === 'string')
            out.push({agent: clean(r.agent, 20), counts});
    }
    return out;
}

// A name in a SLICE's list: a string (cut), or null for "the rest"; undefined if neither.
const nameOf = v => (v === null ? null : typeof v === 'string' && v ? clean(v, 60) : undefined);

// One of a SLICE's lists, as {agent, key, total}; null when the daemon sends none.
function listOf(rows, parse) {
    if (!Array.isArray(rows))
        return null;
    return rows.slice(0, 20).map(r => (Array.isArray(r) ? parse(r) : null)).filter(r => r);
}

const LIST_PARSERS = {
    agent: r => (typeof r[0] === 'string' && countOk(r[1])
        ? {agent: clean(r[0], 20), key: clean(r[0], 20), total: r[1]} : null),
    model: r => (nameOf(r[0]) !== undefined && nameOf(r[1]) !== undefined && countOk(r[2])
        ? {agent: nameOf(r[0]), key: nameOf(r[1]), total: r[2]} : null),
    host: r => (nameOf(r[0]) !== undefined && countOk(r[1]) ? {agent: null, key: nameOf(r[0]), total: r[1]} : null),
    project: r => (nameOf(r[0]) !== undefined && countOk(r[1]) ? {agent: null, key: nameOf(r[0]), total: r[1]} : null),
};

export const TOKEN_GROUPS = ['agent', 'model', 'host', 'project'];

// A SLICE (a range, day or hour): its counts, responses and four lists; null if malformed.
function sliceOf(x) {
    const counts = countsOf(x?.counts);
    if (!counts)
        return null;
    const slice = {counts, requests: countOk(x.requests) ? x.requests : null};
    for (const g of TOKEN_GROUPS)
        slice[g] = listOf(x[g], LIST_PARSERS[g]);
    return slice;
}

// A 0.0.6 daemon's agent rows as a SLICE with only the agent list.
function sliceOfRows(rows) {
    const found = agentRows(rows);
    const slice = {counts: addCounts(found.map(r => r.counts)), requests: null, model: null, host: null, project: null};
    slice.agent = found.map(r => ({agent: r.agent, key: r.agent, total: totalOf(r.counts)}))
        .sort((a, b) => b.total - a.total);
    return slice;
}

// The chart's days or hours, in order; null when there are none or they are malformed.
function timeSlices(list, most) {
    if (!Array.isArray(list) || !list.length || list.length > most)
        return null;
    const out = [];
    for (const x of list) {
        const slice = sliceOf(x);
        if (!slice || !countOk(x.start) || (out.length && x.start <= out[out.length - 1].start))
            return null;
        out.push({start: x.start, slice});
    }
    return out;
}

// The parts of a TOKENS record the panel uses, checked; null for anything else (the feature
// is off). A 0.0.6 daemon has only today and lifetime, by agent, and no days or hours.
export function tokensOf(t) {
    if (!t || typeof t !== 'object')
        return null;
    const r = t.ranges && typeof t.ranges === 'object' ? t.ranges : null;
    const p = t.previous && typeof t.previous === 'object' ? t.previous : {};
    return {
        ranges: r
            ? Object.fromEntries(TOKEN_RANGES.map(id => [id, sliceOf(r[id])]))
            : {today: sliceOfRows(t.today), last7: null, last30: null, total: sliceOfRows(t.total)},
        days: r ? timeSlices(t.days, 60) : null,
        hours: r ? timeSlices(t.hours, 25) : null,
        previous: Object.fromEntries(['today', 'last7', 'last30'].map(id => [id, countOk(p[id]) ? p[id] : null])),
        dayStart: countOk(t.day_start) && t.day_start > 0 ? t.day_start : null,
        since: countOk(t.since) && t.since > 0 ? t.since : null,
    };
}

// 1234 -> "1.23k", 360461887 -> "360M": three significant digits, as `agentail tokens`
// (agentail.tokens_cli.fmt_count; tests/js/test_model.js uses the same cases).
export function fmtCount(n) {
    if (!countOk(n))
        return '?';
    if (n < 1000)
        return String(n);
    const units = [['k', 1e3], ['M', 1e6], ['B', 1e9], ['T', 1e12]];
    for (const [unit, size] of units) {
        const v = n / size;
        if (v < 999.5 || unit === 'T') {
            if (v < 9.995)
                return `${v.toFixed(2)}${unit}`;
            if (v < 99.95)
                return `${v.toFixed(1)}${unit}`;
            return `${v.toFixed(0)}${unit}`;
        }
    }
    return '?';
}

// The Tokens page's ranges: today, the last 7 and 30 calendar days, and everything kept.
export const TOKEN_RANGES = ['today', 'last7', 'last30', 'total'];

// Whether the panel offers its Tokens page: the daemon counts tokens (0.0.6 and later, on).
export function tokensAvailable(state) {
    return state.tokens !== null;
}

const pad2 = n => String(n).padStart(2, '0');

// The local date of a time stamp, as 2026-10-05.
function dateText(ts) {
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

// The chart's short date, as 10/05, and hour, as 14:00.
function shortDate(ts) {
    const d = new Date(ts * 1000);
    return `${pad2(d.getMonth() + 1)}/${pad2(d.getDate())}`;
}

function hourText(ts) {
    return `${pad2(new Date(ts * 1000).getHours())}:00`;
}

// Everything a model read and wrote: the four kinds added up (thinking is part of the output).
// Codex's own "lifetime tokens" counts the same way, cached input included.
const totalOf = c => c.input + c.cache_write + c.cache_read + c.output;

function addCounts(list) {
    const sum = Object.fromEntries(COUNT_FIELDS.map(f => [f, 0]));
    for (const c of list) {
        for (const f of COUNT_FIELDS)
            sum[f] += c[f];
    }
    return sum;
}

// 1602 -> "1602 requests", 12345 -> "12.3k requests" (in Chinese "1.23 万次请求"); '' when
// the daemon does not say.
function requestsText(n) {
    if (n === null)
        return '';
    if (n < 10000)
        return _('{count} requests', {count: String(n)});
    if (language() === 'zh')  // counted in 万 (ten thousands); the English text is never shown
        return _('{count}×10k requests', {count: String(Number((n / 1e4).toPrecision(3)))});
    return _('{count} requests', {count: fmtCount(n)});
}

// claude-opus-5-5 -> "Opus 5.5", claude-3-5-haiku-20241022 -> "Haiku 3.5"; any other name as
// the agent wrote it; null (not recorded, or the smaller models added up) -> "Other".
export function modelName(model) {
    if (model === null)
        return _('Other');
    const cap = w => w[0].toUpperCase() + w.slice(1);
    const version = (major, minor) => (minor ? `${major}.${minor}` : major);
    let m = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d))?(?:-\d{8})?$/.exec(model);
    if (m)
        return `${cap(m[1])} ${version(m[2], m[3])}`;
    m = /^claude-(\d+)(?:-(\d))?-(opus|sonnet|haiku)(?:-\d{8})?$/.exec(model);
    if (m)
        return `${cap(m[3])} ${version(m[1], m[2])}`;
    return model;
}

// "+18%" / "−5%": how `now` compares with `before`; '' when there was nothing before.
function changeText(now, before) {
    if (!before)
        return '';
    const pct = Math.round(100 * (now - before) / before);
    return pct < 0 ? `−${-pct}%` : `+${pct}%`;
}

// The chart's columns for a range: today's hours, the last 7 or 30 days; null for lifetime
// or when the daemon sends none. `label` names a picked column, `current` is where now is.
function chartColumns(t, which, now) {
    if (which === 'today') {
        if (!t.hours)
            return null;
        const current = t.hours.filter(h => h.start <= now).pop();
        return t.hours.map((h, i) => {
            const next = t.hours[i + 1]?.start ?? h.start + 3600;
            return {...h, short: hourText(h.start), label: `${hourText(h.start)}–${hourText(next).replace(/^00:/, '24:')}`,
                current: h === current};
        });
    }
    const n = {last7: 7, last30: 30}[which];
    if (!n || !t.days)
        return null;
    const days = t.days.slice(-n);
    return days.map((d, i) => {
        const today = i === days.length - 1;
        const date = today ? _('Today') : shortDate(d.start);
        return {...d, short: date, label: date, current: today};
    });
}

// The rows of one list: a name, an agent (for its icon and colour) or a symbolic icon, the
// total, and its share of the slice.
function listRows(slice, group, all) {
    return (slice[group] ?? []).map(r => {
        let name, icon = null;
        if (group === 'agent') {
            name = AGENT_NAME[r.key] ?? r.key;
        } else if (group === 'model') {
            name = modelName(r.key);
        } else if (group === 'host') {
            name = r.key === null ? _('Other') : r.key === 'local' ? _('This computer') : r.key;
            icon = r.key === 'local' ? 'computer-symbolic' : 'network-server-symbolic';
        } else {
            name = r.key ?? _('Other');
            icon = 'folder-symbolic';
        }
        const share = all ? r.total / all : 0;
        const pct = Math.round(100 * share);
        return {
            name,
            agent: r.agent,
            icon,
            total: fmtCount(r.total),
            percent: r.total && pct < 1 ? '<1%' : `${pct}%`,
            share,
        };
    });
}

// The Tokens page for a range: the total of every agent and its four kinds, a line of how it
// compares (a day's average, the same stretch of time before, the responses), a chart of
// today's hours or of the last 7 or 30 days, and the totals by agent, model, host or project.
// `pick` is the start of a column clicked in the chart: then everything but the chart is that
// hour's or day's. Null while the daemon reports nothing.
export function tokenPage(state, range, group, pick = null, now = Date.now() / 1000) {
    const t = state.tokens;
    if (!t)
        return null;
    const offered = TOKEN_RANGES.filter(id => t.ranges[id]);
    const which = offered.includes(range) ? range : 'today';
    const columns = chartColumns(t, which, now);
    const picked = columns?.find(c => c.start === pick) ?? null;
    const slice = picked ? picked.slice : t.ranges[which];
    const all = totalOf(slice.counts);
    const lifetime = t.ranges.total;
    const groups = TOKEN_GROUPS.filter(g => lifetime?.[g] && (g !== 'host' || lifetime.host.length > 1));
    const by = groups.includes(group) ? group : 'agent';
    const labels = {today: _('Today'), last7: _('7 days'), last30: _('30 days'), total: _('Lifetime')};
    const groupLabels = {agent: _('Agent'), model: _('Model'), host: _('Host'), project: _('Project')};
    const page = {
        range: which,
        ranges: offered.map(id => ({id, text: labels[id], on: id === which})),
        groups: groups.length > 1 ? groups.map(id => ({id, text: groupLabels[id], on: id === by})) : [],
        picked: picked !== null,
        title: picked ? picked.label : _('All agents'),
        total: fmtCount(all),
        parts: [
            {label: _('Input'), value: fmtCount(slice.counts.input)},
            {label: _('Cache write'), value: fmtCount(slice.counts.cache_write)},
            {label: _('Cache read'), value: fmtCount(slice.counts.cache_read)},
            {label: _('Output'), value: fmtCount(slice.counts.output)},
        ],
        summary: '',
        chart: null,
        rows: listRows(slice, by, all),
        empty: '',
        since: which === 'total' && t.since ? _('Since {date}', {date: dateText(t.since)}) : '',
    };
    if (!all && !picked) {
        page.empty = {
            today: _('Nothing yet today.'),
            last7: _('Nothing in the last 7 days.'),
            last30: _('Nothing in the last 30 days.'),
            total: _('Nothing counted yet.'),
        }[which];
        return page;
    }
    if (picked) {
        page.summary = requestsText(slice.requests);
    } else {
        let days = {last7: 7, last30: 30}[which] ?? 0;
        if (which === 'total' && t.since) {
            const first = new Date(t.since * 1000);
            first.setHours(0, 0, 0, 0);
            days = Math.max(1, Math.ceil((now * 1000 - first.getTime()) / 86400000));
        }
        // Compared with the same stretch of time before, when the counts go back that far:
        // Claude deletes its session files after 30 days, so the 30 days before the last 30
        // are mostly gone after a first backfill.
        const before = {today: 1, last7: 13, last30: 59}[which];
        let change = '';
        if (before && t.dayStart && t.since) {
            const start = new Date(t.dayStart * 1000);
            start.setDate(start.getDate() - before);
            if (t.since * 1000 <= start.getTime())
                change = changeText(all, t.previous[which]);
        }
        const compare = {
            today: _('{change} vs this time yesterday', {change}),
            last7: _('{change} vs prior 7 days', {change}),
            last30: _('{change} vs prior 30 days', {change}),
        };
        page.summary = [
            days ? _('{value}/day', {value: fmtCount(Math.round(all / days))}) : '',
            change ? compare[which] : '',
            requestsText(slice.requests),
        ].filter(x => x).join(' · ');
    }
    if (columns) {
        const totals = columns.map(c => totalOf(c.slice.counts));
        const most = Math.max(...totals);
        const on = picked ?? columns.find(c => c.current);
        const count = columns.length;
        page.chart = {
            bars: columns.map((c, i) => {
                const parts = new Map(c.slice.agent.map(r => [r.key, r.total]));
                return {
                    start: c.start,
                    label: c.label,
                    on: c === on,
                    picked: c === picked,
                    segments: sortAgents(parts.keys()).map(agent => ({
                        agent,
                        fraction: most ? parts.get(agent) / most : 0,
                    })),
                    title: `${c.label} · ${fmtCount(totals[i])}`,
                };
            }),
            // Under 7 columns, every date; otherwise the ends and the middle.
            axis: count <= 7
                ? columns.map(c => c.short)
                : [columns[0].short, columns[Math.floor(count / 2)].short,
                    which === 'today' ? '24:00' : columns[count - 1].short],
        };
    }
    return page;
}

// ---- settings: switches the daemon keeps in config.toml ----------------------------------
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
    const n = state.settings.notifications;
    return {
        usage: typeof usage === 'boolean' ? usage : null,
        local: api ? api.state !== 'off' : null,
        servers: api ? api.servers : null,
        turn_done: n?.turn_done ?? null,
        attention: n?.attention ?? null,
        details: n?.details ?? null,
        approvals: state.settings.approvals,
        tokens: state.settings.tokens?.enabled ?? null,
        tokens_servers: state.settings.tokens?.servers ?? null,
    };
}

// One switch row. `pending`: values the user just chose that the daemon has not reported
// yet; they are shown (as "saving") instead of the daemon's.
function switchRow(actual, pending, id, title, notes, enabled) {
    const want = typeof pending[id] === 'boolean' ? pending[id] : null;
    const known = actual[id] !== null;
    return {
        id, title, notes,
        on: want ?? actual[id] ?? false,
        enabled: known && enabled,
        saving: known && want !== null && want !== actual[id],
        status: '',
    };
}

// The Notifications group: one switch per kind, then whether they carry the prompt and the
// command (pointless while both kinds are off). `available` is false for a daemon before 0.0.5.
export function notificationSettings(state, pending = {}) {
    const actual = switchValues(state);
    const row = (...args) => switchRow(actual, pending, ...args);
    const turnDone = row('turn_done', _("When it's your turn"), [], true);
    const attention = row('attention', _('When an agent needs your approval'), [], true);
    const details = row('details', _('Show the prompt and the command'),
        [_('Also on the lock screen.')], turnDone.on || attention.on);
    return {available: actual.turn_done !== null, rows: [turnDone, attention, details]};
}

const LOCAL_STATUS = {
    pending: N_('Checking…'),
    needs_login: N_('Sign-in expired: run claude once'),
    error: N_('The last query failed: see agentail doctor'),
};

// The Usage limits group: the Usage switch, then (while it is on) Claude's two.
export function usageSettings(state, pending = {}, now = 0) {
    const actual = switchValues(state);
    const hasServers = [...state.hosts.keys()].some(alias => alias !== 'local');
    const row = (...args) => switchRow(actual, pending, ...args);
    const usage = row('usage', _('Show usage limits'), [], true);
    const api = state.usageApi.get('claude');
    const local = row('local', _('This computer'), [], true);
    if (local.on && api) {
        const ago = api.state === 'ok' ? ageText(api.okTs, now) : '';
        if (ago)
            local.status = _('Last checked {age} ago', {age: ago});
        else if (Object.hasOwn(LOCAL_STATUS, api.state))
            local.status = _(LOCAL_STATUS[api.state]);
    }
    const servers = row('servers', _('Servers'),
        [hasServers
            ? _('For servers signed in to another Claude account.')
            : _('Add a server first.')],
        hasServers || actual.servers === true);
    return {
        available: actual.usage !== null,
        usage,
        // Claude's switches only matter while usage is shown at all.
        showClaude: usage.on && actual.local !== null,
        // Under the group's title: what asking means, for both switches.
        intro: _('After every Claude turn and when you open this panel.'),
        rows: [local, servers],
    };
}

// The Approvals group (0.0.6): the switch. How long a request waits is set with
// `agentail approvals wait`, not here. `available` is false for an older daemon.
export function approvalSettings(state, pending = {}) {
    const actual = switchValues(state);
    const toggle = switchRow(actual, pending, 'approvals', _('Answer permission prompts here'), [], true);
    return {available: actual.approvals !== null, toggle};
}

// The Token counts group (0.0.6): counting at all, and on the servers too. `available` is false
// for an older daemon.
export function tokenSettings(state, pending = {}) {
    const actual = switchValues(state);
    const hasServers = [...state.hosts.keys()].some(alias => alias !== 'local');
    const row = (...args) => switchRow(actual, pending, ...args);
    const tokens = row('tokens', _('Count tokens'), [], true);
    const servers = row('tokens_servers', _('Servers'), [],
        tokens.on && (hasServers || actual.tokens_servers === true));
    return {available: actual.tokens !== null, rows: [tokens, servers]};
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
    return _('Claude: terminal sessions only. More in Settings (⚙, top right).');
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

// Panel colours: the user's choice, else the desktop's colour scheme. GNOME Shell's own
// menus are dark unless the desktop asks for light, so dark is the default.
export function resolveTheme(choice, colorScheme) {
    if (choice === 'light' || choice === 'dark')
        return choice;
    return colorScheme === 'prefer-light' ? 'light' : 'dark';
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
// (preferring an "error:" line), cleaned and bounded. The tool's text stays in English.
export function addHostMessage(output, ok) {
    const lines = String(output ?? '').split('\n').map(l => l.trim()).filter(l => l);
    const error = lines.filter(l => l.startsWith('error:')).pop();
    const text = ok ? lines[lines.length - 1] : (error ?? lines[lines.length - 1]);
    return text === undefined ? (ok ? _('Done') : _('Failed')) : clean(text, 240);
}

// "Added gpu7. connected (end-to-end ping received)": the tool's last line, minus its own
// "gpu7:" prefix.
export function addedMessage(alias, output) {
    const lines = String(output ?? '').split('\n').map(l => l.trim()).filter(l => l);
    if (!lines.length)
        return _('Added {alias}.', {alias});
    const last = addHostMessage(output, true);
    const detail = last.startsWith(`${alias}: `) ? last.slice(alias.length + 2) : last;
    return detail ? _('Added {alias}. {detail}', {alias, detail}) : _('Added {alias}.', {alias});
}

export function removedMessage(alias, output) {
    const text = String(output ?? '');
    if (text.includes('shares its $HOME'))
        return _('Removed {alias}. The hooks stay for the server that shares its home.', {alias});
    if (text.includes('other receivers still use the hooks'))
        return _('Removed {alias}. The hooks stay for the other desktops that watch it.', {alias});
    return _('Removed {alias}.', {alias});
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
    const where = key[0] === 'local' ? _('this computer') : key[0];
    let detail = '';
    if (msg.details === true) {
        detail = known ? c.subtitle : '';
        if (kind === 'attention' && c.tool && c.toolDetail)
            detail = _('{agent} wants to run {tool}: {detail}', {agent: c.agentName, tool: c.tool, detail: c.toolDetail});
    }
    return {
        key: keyString(key),
        kind,
        agent: key[1],
        title: kind === 'attention'
            ? _('{project} needs you', {project: c.project}) : _('{project} is done', {project: c.project}),
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
