// Tests for the GNOME extension's model.js. Run by tests/test_gnome_extension.py:
//   gjs -m tests/js/test_model.js <path to model.js>
// Prints one line per failure and exits 1 if any test failed.
import GLib from 'gi://GLib';
import System from 'system';

const [modelPath] = ARGV;
const M = await import(`file://${modelPath}`);
const extDir = GLib.path_get_dirname(modelPath);
const I = await import(`file://${extDir}/i18n.js`);
const source = name => new TextDecoder().decode(GLib.file_get_contents(`${extDir}/${name}`)[1]);

const NOW = 10000;
let failures = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const eq = (a, b, what = '') => {
    const ja = JSON.stringify(a), jb = JSON.stringify(b);
    if (ja !== jb)
        throw new Error(`${what} expected ${jb}, got ${ja}`);
};
const ok = (cond, what) => {
    if (!cond)
        throw new Error(`not true: ${what}`);
};

const s = (host, sid, status, kw = {}) => ({
    key: {host, agent: kw.agent ?? 'claude', session_id: sid},
    status,
    cwd: kw.cwd ?? '/home/u/proj/app',
    prompt_preview: kw.prompt ?? 'fix the bug',
    tool: kw.tool ?? '',
    tool_detail: kw.detail ?? '',
    message: kw.message ?? '',
    started_ts: kw.last ?? NOW - 5,
    last_ts: kw.last ?? NOW - 5,
});
const host = (alias, state, name = '', detail = '') => ({alias, name: name || alias, state, detail});
const make = (sessions, hosts) => {
    const st = new M.UiState();
    st.apply({type: 'snapshot', sessions,
        hosts: hosts ?? [host('local', 'local', 'desk'), host('gpu1', 'connected', 'GPU one')]});
    return st;
};

test('summary counts and levels', () => {
    eq(M.summary(new M.UiState()).level, 'offline');
    const st = make([s('local', 'a', 'running'), s('gpu1', 'b', 'running', {agent: 'codex'}),
        s('gpu1', 'c', 'waiting_input'), s('gpu1', 'd', 'ended', {agent: 'zed'})]);
    let sum = M.summary(st);
    eq([sum.level, sum.counts, sum.text], ['busy', {attention: 0, running: 2, waiting: 1}, '2 running · 1 waiting']);
    eq([sum.agents, sum.total, sum.lead], [['claude', 'codex'], 3, '']);
    st.apply({type: 'session_update', session: s('local', 'a', 'needs_attention', {cwd: '/x/SpatialVLA'})});
    sum = M.summary(st);
    eq([sum.level, sum.text, sum.lead], ['attention', '1 needs you · 1 running · 1 waiting', 'SpatialVLA']);
    eq(M.summary(make([])).text, 'No active sessions');
    eq(M.summary(make([])).agents, []);
    eq(M.summary(make([s('local', 'w', 'waiting_input')])).level, 'waiting');
    eq(M.summary(make([s('local', 'x', 'running', {agent: 'zed'}), s('local', 'y', 'running', {agent: 'aider'}),
        s('local', 'z', 'running', {agent: 'codex'})])).agents, ['codex', 'aider', 'zed']);
});

test('hosts down', () => {
    const st = make([]);
    for (const [state, down] of [['connected', false], ['connecting', false], ['backoff', true],
        ['auth_failed', true], ['error', true], ['stopped', false], ['weird\x1b[31m', false]]) {
        st.apply({type: 'host_status', host: host('gpu1', state, '', 'why')});
        eq(M.summary(st).hostsDown.length, down ? 1 : 0, state);
    }
    st.apply({type: 'host_status', host: host('gpu1', 'backoff', '', 'ssh exited (255)')});
    const h = M.hostRows(st).find(x => x.alias === 'gpu1');
    eq([h.level, h.down, h.stateText, h.problem], ['pending', true, 'reconnecting', 'ssh exited (255)']);
    eq(M.summary(st).text, '1 host unreachable');
    st.apply({type: 'host_remove', alias: 'gpu1'});
    eq(M.summary(st).hostsDown, []);
});

test('a host retrying stays down until it connects', () => {
    const st = make([], [host('local', 'local'), host('gpu1', 'connecting'), host('gpu2', 'connected')]);
    const row = alias => M.hostRows(st).find(x => x.alias === alias);
    eq([row('gpu1').down, row('gpu1').level], [false, 'off']);  // a start or a resume: not an outage
    st.apply({type: 'host_status', host: host('gpu1', 'backoff', '', 'ssh exited (255); retry in 4s')});
    st.apply({type: 'host_status', host: host('gpu1', 'connecting')});
    eq([row('gpu1').down, row('gpu1').level, row('gpu1').stateText], [true, 'pending', 'connecting']);
    eq(row('gpu1').problem, 'ssh exited (255); retry in 4s');  // the last reason
    eq(M.summary(st).hostsDown, ['gpu1']);
    st.apply({type: 'host_status', host: host('gpu1', 'connected')});
    eq([row('gpu1').down, row('gpu1').problem], [false, '']);
    st.apply({type: 'host_status', host: host('gpu2', 'auth_failed', '', 'Permission denied')});
    st.apply({type: 'snapshot', sessions: [], hosts: [host('gpu2', 'connecting')]});  // daemon restarted
    eq(row('gpu2').down, false);
});

test('agent groups, cards and ended count', () => {
    const st = make([
        s('gpu1', 'w', 'waiting_input', {last: NOW - 120}),
        s('gpu1', 'n', 'needs_attention', {tool: 'Bash', detail: 'rm -rf build', message: 'Claude needs your permission to use Bash', last: NOW - 30}),
        s('gpu1', 'r', 'running', {agent: 'codex', cwd: '/data/x/openpi/', last: NOW - 1}),
        s('gpu1', 'e', 'ended'),
        s('local', 'l', 'running'),
        s('gpu9', 'x', 'stale', {prompt: ''}),
    ]);
    const gs = M.agentGroups(st, NOW);
    eq(gs.map(g => [g.agent, g.name, g.count]), [['claude', 'Claude Code', 4], ['codex', 'Codex', 1]]);
    eq(gs[0].cards.map(c => c.id), ['n', 'w', 'l', 'x']);
    const n = gs[0].cards[0];
    eq([n.project, n.subtitle, n.statusLabel, n.tool, n.toolDetail, n.age, n.agentName, n.host],
        ['app', 'Claude needs your permission to use Bash', 'Needs you', 'Bash', 'rm -rf build', '30s', 'Claude Code', 'gpu1']);
    eq(gs[0].cards[2].host, 'This computer');
    const r = gs[1].cards[0];
    eq([r.project, r.agent, r.agentName, r.style], ['openpi', 'codex', 'Codex', 'running']);
    eq(gs[0].cards[3].subtitle, 'Last seen running');
    eq(gs[0].cards[3].key, {host: 'gpu9', agent: 'claude', session_id: 'x'});
    eq(M.footer(st), {ended: '1 ended recently'});
    eq(M.footer(make([])), {ended: ''});
});

test('host rows', () => {
    const st = make([s('gpu1', 'a', 'running'), s('gpu1', 'b', 'ended'), s('local', 'c', 'running')],
        [host('local', 'local'), host('gpu1', 'connected', 'GPU one'), host('b', 'stopped')]);
    eq(M.hostRows(st).map(h => [h.alias, h.title, h.level, h.stateText, h.sessions]),
        [['gpu1', 'GPU one (gpu1)', 'ok', 'connected', 1], ['b', 'b', 'off', 'not set up', 0]]);
    eq(M.hostRows(make([], [host('local', 'local')])), []);
});

test('untrusted strings are cleaned and bounded', () => {
    const evil = '\x1b]8;;http://x\x07click\x1b]8;;\x07 <b>bold</b>\n‮' + 'y'.repeat(500);
    const st = make([s('gpu1', 'e', 'running', {prompt: evil, cwd: '/a/\x1b[2Jb/' + 'c'.repeat(100)})]);
    const c = M.agentGroups(st, NOW)[0].cards[0];
    ok(!/[\x1b\x07\n‮]/.test(c.subtitle), 'no control chars in subtitle');
    ok(Array.from(c.subtitle).length <= M.LIMITS.subtitle, 'subtitle bounded');
    ok(Array.from(c.project).length <= M.LIMITS.project, 'project bounded');
    eq(M.card(s('local', 'q', '</style>'), NOW).status, 'stale');
    // The key goes back to the daemon as sent (it names the session), never displayed.
    eq(M.card(s('gpu1', 'a\x1bb', 'running'), NOW).key, {host: 'gpu1', agent: 'claude', session_id: 'a\x1bb'});
    eq(M.wireKey({host: 'gpu1', agent: 'claude'}), null);
    eq(M.wireKey({host: 'gpu1', agent: 'claude', session_id: 3}), null);
    eq(M.wireKey(null), null);
});

test('notices', () => {
    const st = make([s('gpu1', 'a', 'needs_attention', {tool: 'Bash', detail: 'make', cwd: '/x/SpatialVLA'})]);
    const msg = {type: 'notify', kind: 'attention', key: {host: 'gpu1', agent: 'claude', session_id: 'a'}};
    eq(st.apply(msg), msg);
    // Default (and daemons that predate the field): what and where only.
    const n = M.notice(msg, st);
    eq([n.title, n.body, n.urgent, n.agent], ['SpatialVLA needs you', 'gpu1', true, 'claude']);
    eq(M.notice({...msg, details: false}, st).body, 'gpu1');
    eq(M.notice({...msg, details: 'yes'}, st).body, 'gpu1', 'only true turns details on');
    const full = M.notice({...msg, details: true}, st);
    eq([full.title, full.body], ['SpatialVLA needs you', 'gpu1 · Claude Code wants to run Bash: make']);
    const done = M.notice({kind: 'turn_done', key: {host: 'local', agent: 'codex', session_id: 'zz'}}, st);
    eq([done.title, done.body, done.urgent], ['? is done', 'this computer', false]);
    eq(M.notice({kind: 'other', key: {}}, st), null);
});

test('notices never carry the prompt or the command unless asked', () => {
    const st = make([
        s('local', 'p', 'waiting_input', {prompt: 'deploy with token abc123', cwd: '/x/web'}),
        s('gpu1', 'q', 'needs_attention', {tool: 'Bash', detail: 'curl -H secret', cwd: '/x/api',
            message: 'Claude needs your permission'}),
    ]);
    const done = {kind: 'turn_done', key: {host: 'local', agent: 'claude', session_id: 'p'}};
    const att = {kind: 'attention', key: {host: 'gpu1', agent: 'claude', session_id: 'q'}};
    for (const m of [done, att]) {
        const n = M.notice(m, st);
        const text = `${n.title} ${n.body}`;
        ok(!text.includes('abc123') && !text.includes('curl') && !text.includes('permission'), text);
    }
    eq(M.notice(done, st).title, 'web is done');
    eq(M.notice(att, st).title, 'api needs you');
    eq(M.notice({...done, details: true}, st).body, 'this computer · deploy with token abc123');
    eq(M.notice({...att, details: true}, st).body, 'gpu1 · Claude Code wants to run Bash: curl -H secret');
});

test('add server helpers', () => {
    eq(['gpu7', 'me@h.example', 'a-b_c.d'].map(M.validAlias), [true, true, true]);
    eq(['', '-oProxyCommand=x', 'a b', 'a;b', '$(x)', 'x'.repeat(200), null, 5].map(M.validAlias),
        [false, false, false, false, false, false, false, false]);
    eq(M.parseAliases('gpu8\n  gpu7 \n-bad\ngpu7\n\x1b[31mx\n'), ['gpu7', 'gpu8']);
    eq(M.parseAliases(null), []);
    eq(M.addHostMessage('probing\ngpu7: connected (end-to-end ping received)\n', true),
        'gpu7: connected (end-to-end ping received)');
    eq(M.addHostMessage('gpu7: x\nerror: gpu7: no python3\nbye', false), 'error: gpu7: no python3');
    eq(M.addHostMessage('', false), 'Failed');
    eq(M.addedMessage('gpu7', 'x\ngpu7: connected (end-to-end ping received)\n'), 'Added gpu7. connected (end-to-end ping received)');
    eq(M.addedMessage('gpu7', ''), 'Added gpu7.');
    eq(M.removedMessage('gpu7', 'removed gpu7 from x'), 'Removed gpu7.');
    eq(M.removedMessage('gpu8', 'gpu8 shares its $HOME with gpu7: leaving'), 'Removed gpu8. The hooks stay for the server that shares its home.');
    eq(M.removedMessage('gpu8', 'kept gpu8:/h/.agentail: other receivers still use the hooks'), 'Removed gpu8. The hooks stay for the other desktops that watch it.');
    ok(Array.from(M.addHostMessage('y'.repeat(900), false)).length <= 240, 'bounded');
});

test('alias order, theme', () => {
    eq(M.parseAliases('gpu10\ngpu2\ngpu1\nbastion\ngpu0'), ['bastion', 'gpu0', 'gpu1', 'gpu2', 'gpu10']);
    eq([M.resolveTheme('light', 'prefer-dark'), M.resolveTheme('dark', 'prefer-light'),
        M.resolveTheme(null, 'prefer-light'), M.resolveTheme(undefined, 'default'),
        M.resolveTheme('weird', 'prefer-dark')], ['light', 'dark', 'light', 'dark', 'dark']);
});

test('chip rows', () => {
    eq(M.chipRows([]), []);
    eq(M.chipRows(['a', 'b', 'c'], 100, 10, 20, 5), [['a', 'b', 'c']]);  // 30+5+30+5+30
    eq(M.chipRows(['aaaa', 'bbbb', 'cccc'], 100, 10, 20, 5), [['aaaa'], ['bbbb'], ['cccc']]);
    eq(M.chipRows(['a', 'b', 'c', 'd'], 100, 10, 20, 5), [['a', 'b', 'c'], ['d']]);
    eq(M.chipRows(['x'.repeat(60)], 100), [['x'.repeat(60)]]);  // a long name still gets a row
});

test('rate limiter', () => {
    const rl = new M.RateLimiter(10);
    const a = {key: 'k', kind: 'turn_done'}, b = {key: 'k', kind: 'attention'};
    ok(rl.allow(a, 0) && !rl.allow(a, 5) && rl.allow(b, 5) && rl.allow(a, 10.5), 'limits per kind');
});

test('disconnect clears state', () => {
    const st = make([s('local', 'a', 'running')]);
    st.disconnect();
    eq([M.summary(st).level, M.agentGroups(st, NOW)], ['offline', []]);
});

test('helpers', () => {
    eq([M.ageText(NOW - 59, NOW), M.ageText(NOW - 61, NOW), M.ageText(NOW - 7200, NOW), M.ageText(0, NOW)],
        ['59s', '1m', '2h', '']);
    eq([M.basename('/data/user/Baselines/SpatialVLA/'), M.basename(''), M.basename('/')], ['SpatialVLA', '', '/']);
    eq(M.clean('a\tb\u0007c', 0), 'a b?c');
    eq(M.clean('abcdef', 4), 'abc…');
});

const usage = (host, agent, windows, kw = {}) => ({
    host, agent, plan: kw.plan ?? null, windows, updated_ts: kw.ts ?? NOW - 120,
});
const win = (pct, minutes, resets) => ({used_percent: pct, window_minutes: minutes, resets_at: resets});
const withUsage = (list, extra = []) => {
    const st = make([]);
    st.apply({type: 'snapshot', sessions: [], hosts: [host('local', 'local'), host('gpu1', 'connected')],
        usage: list});
    for (const m of extra)
        st.apply(m);
    return st;
};

test('usage labels and durations', () => {
    eq([300, 10080, 2880, 90, null, 0, '5'].map(M.windowLabel), ['5h', 'Week', '2d', '90m', 'Limit', 'Limit', 'Limit']);
    eq([10, 600, 7800, 90000, -5].map(M.durationText), ['1m', '10m', '2h 10m', '1d 1h', '1m']);
});

test('usage cards: rows, levels, reset text', () => {
    const st = withUsage([usage('local', 'claude', [win(41.2, 300, NOW + 7800), win(75, 10080, NOW + 90000)],
        {plan: 'max'})]);
    const [c] = M.usageCards(st, NOW);
    eq([c.agent, c.name, c.plan, c.via, c.updatedText, c.stale], ['claude', 'Claude Code', 'Max', '', 'updated 2m ago', false]);
    eq(c.rows.map(r => [r.label, r.percent, r.level, r.resetText]),
        [['5h', 41, 'ok', 'resets in 2h 10m'], ['Week', 75, 'warn', 'resets in 1d 1h']]);
    ok(Math.abs(c.rows[0].fraction - 0.412) < 1e-9, "fraction");
});

test('usage: plan names and per-model windows', () => {
    eq(['max_5x', 'max_20x', 'plus', 'pro', '', null, 'x__y', '__'].map(M.planText),
        ['Max 5x', 'Max 20x', 'Plus', 'Pro', '', '', 'X y', '']);
    const st = withUsage([usage('local', 'claude', [win(2, 300), win(0, 10080),
        {...win(3, 10080), label: 'Fable'}], {plan: 'max_5x'})]);
    const [c] = M.usageCards(st, NOW);
    eq(c.plan, 'Max 5x');
    eq(c.rows.map(r => r.label), ['5h', 'Week', 'Week · Fable']);
    eq(c.labelWidth, M.usageLabelWidth(['5h', 'Week', 'Week · Fable']));
    ok(c.labelWidth > 34, 'the label column grows for a labelled window');
    eq(M.usageLabelWidth(['5h', 'Week']), 34);
    eq(M.usageLabelWidth(['x'.repeat(80)]), 130);
    const junk = withUsage([usage('local', 'codex', [{...win(1, 300), label: 'a\nb'}, {...win(1, 300), label: 7}])]);
    eq(M.usageCards(junk, NOW)[0].rows.map(r => r.label), ['5h · a b', '5h · 7']);
});

test('usage: one card per account, hosts named only when there are several', () => {
    const week = NOW + 86400;
    const rec = (h, pct, ts, reset, plan) => usage(h, 'claude', [win(pct, 300), win(1, 10080, reset)], {ts, plan});
    const one = withUsage([rec('local', 10, NOW - 300, week, 'max_5x'), rec('gpu1', 20, NOW - 60, week + 300)]);
    const [c] = M.usageCards(one, NOW);
    eq(M.usageCards(one, NOW).length, 1);
    eq([c.via, c.sources, c.plan, c.rows[0].percent], ['gpu1', '', 'Max 5x', 20]);  // plan from the group

    const two = withUsage([rec('local', 10, NOW - 300, week, 'max_5x'), rec('gpu1', 20, NOW - 60, week + 300),
        rec('gpu7', 30, NOW - 30, week + 61 * 3600)]);
    const cards = M.usageCards(two, NOW);
    eq(cards.map(x => [x.sources, x.rows[0].percent, x.plan]),
        [['this computer · gpu1', 20, 'Max 5x'], ['gpu7', 30, '']]);  // this computer's account first

    // a week that is over is left out next to a current reading
    const old = withUsage([rec('local', 10, NOW - 60, week), rec('gpu7', 30, NOW - 6 * 86400, NOW - 3600)]);
    eq(M.usageCards(old, NOW).map(x => x.sources), ['']);
    // sign-in problems belong to this computer's account only
    const login = withUsage([rec('local', 10, NOW - 300, week), rec('gpu7', 30, NOW - 30, week + 61 * 3600)],
        [{type: 'usage_api_status', agent: 'claude', status: {state: 'needs_login', detail: ''}}]);
    eq(M.usageCards(login, NOW).map(x => x.updatedText), ['sign-in expired · run claude once', 'updated 30s ago']);
});

test('usage extras: extra usage and credits', () => {
    eq([M.amountText(8.61, 'USD'), M.amountText(3, 'EUR'), M.amountText(12.5, null), M.amountText(40, undefined)],
        ['$8.61', '3.00 EUR', '12.5', '40']);
    const capped = M.extraRow({label: 'Extra usage', kind: 'spent', amount: 40, limit: 50, currency: 'USD'});
    eq([capped.bar, capped.percent, capped.level, capped.resetText], [true, 80, 'warn', '$40.00 of $50.00']);
    eq(M.extraRow({label: 'Extra usage', kind: 'spent', amount: 3, currency: 'USD'}).resetText, '$3.00 spent');
    // the owner's account on 2026-10-15: switched on, $20 cap, no credits
    const idle = M.extraRow({label: 'Extra usage', kind: 'spent', amount: 0, limit: 20, currency: 'USD',
        inactive: 'out_of_credits'});
    eq([idle.bar, idle.percent, idle.level, idle.resetText], [true, null, 'off', '$0.00 of $20.00 · out of credits']);
    eq(M.extraRow({label: 'Extra usage', kind: 'spent', amount: 0, currency: 'USD', inactive: 'unknown'}).resetText,
        '$0.00 spent · not active');
    const credits = M.extraRow({label: 'Credits', kind: 'balance', amount: 12.5});
    eq([credits.bar, credits.percent, credits.resetText], [false, null, '12.5 left']);
    eq(M.extraRow({label: 'Credits', kind: 'balance', unlimited: true}).resetText, 'unlimited');
    for (const junk of [null, 'x', {kind: 'balance', amount: 1}, {label: 'C', kind: 'balance'},
        {label: 'C', kind: 'spent', amount: -1}, {label: 'C', kind: 'other', amount: 1}])
        eq(M.extraRow(junk), null);
    const st = withUsage([{...usage('local', 'codex', [win(5, 300)]),
        extras: [{label: 'Credits', kind: 'balance', amount: 40}, 'junk']}]);
    eq(M.usageCards(st, NOW)[0].rows.map(r => [r.label, r.resetText]), [['5h', ''], ['Credits', '40 left']]);
});

test('usage levels switch at 70 and 90 percent', () => {
    const st = withUsage([usage('local', 'codex', [win(69.9, 300), win(70, 300), win(89.9, 300), win(90, 300), win(120, 300)])]);
    eq(M.usageCards(st, NOW)[0].rows.map(r => [r.level, r.percent]),
        [['ok', 70], ['warn', 70], ['warn', 90], ['crit', 90], ['crit', 100]]);
    eq(M.usageCards(st, NOW)[0].rows[4].fraction, 1);
    eq(M.usageCards(st, NOW)[0].rows[0].resetText, '');
});

test('a window past its reset time shows no old percentage', () => {
    const st = withUsage([usage('local', 'claude', [win(95, 300, NOW - 1), win(10, 10080, NOW + 60)])]);
    const [r0, r1] = M.usageCards(st, NOW)[0].rows;
    eq([r0.percent, r0.level, r0.fraction, r0.resetText], [null, 'off', 0, 'reset, waiting for the next update']);
    eq([r1.percent, r1.resetText], [10, 'resets in 1m']);
});

test('usage goes stale after 30 minutes', () => {
    const fresh = withUsage([usage('local', 'claude', [win(1, 300)], {ts: NOW - 1800})]);
    const old = withUsage([usage('local', 'claude', [win(1, 300)], {ts: NOW - 1801})]);
    eq([M.usageCards(fresh, NOW)[0].stale, M.usageCards(old, NOW)[0].stale], [false, true]);
    eq(M.usageCards(old, NOW)[0].updatedText, 'updated 30m ago');
    eq(M.usageCards(withUsage([usage('local', 'claude', [win(1, 300)], {ts: 0})]), NOW)[0].stale, true);
});

test('usage: freshest host wins, remote source is named, agents ordered', () => {
    const st = withUsage([
        usage('local', 'codex', [win(4, 10080)], {ts: NOW - 50}),
        usage('local', 'claude', [win(10, 300)], {ts: NOW - 500}),
        usage('gpu1', 'claude', [win(20, 300)], {ts: NOW - 60}),
    ]);
    const cards = M.usageCards(st, NOW);
    eq(cards.map(c => [c.agent, c.rows[0].percent, c.via]), [['claude', 20, 'gpu1'], ['codex', 4, '']]);
});

test('usage ignores junk and agents without a usable window', () => {
    const st = withUsage([
        usage('local', 'claude', [null, 'x', {used_percent: 'a'}, {used_percent: NaN}, {}]),
        usage('local', 'codex', 'nope'),
        'junk',
        usage('local', 'future-agent', [win(5, 60)]),
    ]);
    eq(M.usageCards(st, NOW).map(c => c.agent), ['future-agent']);
    eq(M.usageCards(make([]), NOW), []);
});

test('usage: an expired Claude login is shown instead of the age', () => {
    const list = [usage('local', 'claude', [win(10, 300)]), usage('local', 'codex', [win(4, 300)])];
    const st = withUsage(list);
    eq(M.usageCards(st, NOW).map(c => c.updatedText), ['updated 2m ago', 'updated 2m ago']);
    st.apply({type: 'usage_api_status', agent: 'claude', status: {state: 'needs_login', detail: 'x'}});
    eq(M.usageCards(st, NOW).map(c => c.updatedText),
        ['sign-in expired · run claude once', 'updated 2m ago']);
    st.apply({type: 'usage_api_status', agent: 'claude', status: {state: 'ok'}});
    eq(M.usageCards(st, NOW)[0].updatedText, 'updated 2m ago');
    st.apply({type: 'usage_api_status', agent: 'claude', status: 'junk'});
    eq(M.usageCards(st, NOW)[0].updatedText, 'updated 2m ago');
    const snap = make([]);
    snap.apply({type: 'snapshot', sessions: [], hosts: [], usage: list,
        usage_api: {claude: {state: 'needs_login'}}});
    eq(M.usageCards(snap, NOW)[0].updatedText, 'sign-in expired · run claude once');
    snap.apply({type: 'snapshot', sessions: [], hosts: [], usage: list, usage_api: 'junk'});
    eq(M.usageCards(snap, NOW)[0].updatedText, 'updated 2m ago');
});

test('settings: the two switches of the Claude usage query', () => {
    const status = (state, servers = false) =>
        ({type: 'usage_api_status', agent: 'claude', status: {state, detail: '', servers}});
    const view = st => M.usageSettings(st).rows.map(r => [r.id, r.on, r.enabled, r.saving, r.status]);
    // not connected, or a daemon without the switches: shown off and disabled
    eq(M.usageSettings(new M.UiState()).available, false);
    eq(view(new M.UiState()), [['local', false, false, false, ''], ['servers', false, false, false, '']]);
    // no server: the servers' switch cannot be turned on
    const alone = make([], [host('local', 'local')]);
    alone.apply(status('off'));
    eq(view(alone), [['local', false, true, false, ''], ['servers', false, false, false, '']]);
    eq(M.usageSettings(alone).rows[1].notes, ['Add a server first.']);
    // with a server; this computer's switch reports the query's state
    const st = make([]);
    st.apply(status('needs_login'));
    eq(view(st), [['local', true, true, false, 'Sign-in expired: run claude once'],
        ['servers', false, true, false, '']]);
    st.apply(status('ok', true));
    eq(view(st), [['local', true, true, false, ''], ['servers', true, true, false, '']]);
    // once a query succeeded, the switch says when
    st.apply({type: 'usage_api_status', agent: 'claude', status: {state: 'ok', servers: true, ok_ts: NOW - 42}});
    eq(M.usageSettings(st, {}, NOW).rows[0].status, 'Last checked 42s ago');
    eq(M.usageSettings(st).usage.notes, []);
    eq(M.usageSettings(st).intro, 'After every Claude turn and when you open this panel.');
    // a choice the daemon has not reported yet shows as saving, until it does
    let pending = {servers: false};
    eq(M.usageSettings(st, pending).rows[1].on, false);
    eq(M.usageSettings(st, pending).rows[1].saving, true);
    eq(M.settlePending(st, pending), {servers: false});
    st.apply(status('ok', false));
    pending = M.settlePending(st, pending);
    eq(pending, {});
    eq(M.settlePending(new M.UiState(), {local: true}), {local: true});  // nothing to compare yet
    // switched on with the servers removed meanwhile: can still be switched off
    const gone = make([], [host('local', 'local')]);
    gone.apply(status('off', true));
    eq(view(gone)[1], ['servers', true, true, false, '']);
});

test('settings: notifications', () => {
    const view = (st, pending) => M.notificationSettings(st, pending).rows.map(r => [r.id, r.on, r.enabled, r.saving]);
    const old = make([]);
    old.apply({type: 'settings', settings: {usage: true}});  // a daemon before 0.0.5
    eq(M.notificationSettings(old).available, false);
    eq(view(old).map(r => r[2]), [false, false, false]);

    const st = make([]);
    st.apply({type: 'settings', settings: {usage: true, notifications: {turn_done: true, attention: true, details: false}}});
    eq(M.notificationSettings(st).available, true);
    eq(view(st), [['turn_done', true, true, false], ['attention', true, true, false], ['details', false, true, false]]);
    eq(M.notificationSettings(st).rows[2].notes, ['Also on the lock screen.']);

    const pending = {turn_done: false};
    eq(view(st, pending)[0], ['turn_done', false, true, true]);
    eq(M.settlePending(st, pending), pending);
    st.apply({type: 'settings', settings: {usage: true, notifications: {turn_done: false, attention: false, details: true}}});
    eq(M.settlePending(st, pending), {});
    eq(view(st)[2], ['details', true, false, false]);  // no notifications: nothing to show them in

    st.apply({type: 'settings', settings: {usage: true, notifications: {turn_done: 'yes', attention: false}}});
    eq(st.settings.notifications, {turn_done: null, attention: false, details: null});
});

test('settings: the Usage switch', () => {
    const st = make([s('local', 'a', 'running')]);
    st.apply({type: 'snapshot', sessions: [s('local', 'a', 'running')], hosts: [host('local', 'local')],
        usage_api: {claude: {state: 'off', servers: false}}, settings: {usage: true}});
    let v = M.usageSettings(st);
    eq([v.available, v.usage.on, v.usage.enabled, v.showClaude], [true, true, true, true]);
    ok(M.usageHint(st) !== null, 'hint while on');
    // switched off: Claude's switches and the hint go away
    st.apply({type: 'settings', settings: {usage: false}});
    v = M.usageSettings(st);
    eq([v.usage.on, v.showClaude], [false, false]);
    eq(M.usageHint(st), null);
    // flipped in the panel, not confirmed yet
    v = M.usageSettings(st, {usage: true});
    eq([v.usage.on, v.usage.saving, v.showClaude], [true, true, true]);
    eq(M.settlePending(st, {usage: true}), {usage: true});
    st.apply({type: 'settings', settings: {usage: true}});
    eq(M.settlePending(st, {usage: true}), {});
    // junk, or a daemon without settings: unknown, the switch is disabled
    st.apply({type: 'settings', settings: {usage: 'yes'}});
    eq([M.usageSettings(st).available, M.usageSettings(st).usage.enabled], [false, false]);
    st.disconnect();
    eq(st.settings, {usage: null, notifications: null, approvals: null, approvalWait: null});
});

test('usage: each card says where its numbers come from', () => {
    const rec = (h, source, ts) => ({...usage(h, 'claude', [win(10, 300, NOW + 86400)], {ts}), source});
    const st = withUsage([rec('local', 'api', NOW - 10)]);
    eq(M.usageCards(st, NOW)[0].sourceText, "From Anthropic, with this computer's Claude login");
    st.apply({type: 'usage_update', usage: rec('gpu1', 'status_line', NOW - 5)});  // fresher
    eq(M.usageCards(st, NOW)[0].sourceText, 'From a Claude session in a terminal');
    st.apply({type: 'usage_update', usage: rec('gpu1', 'server_api', NOW - 1)});
    eq(M.usageCards(st, NOW)[0].sourceText, 'From Anthropic, with the Claude login on gpu1');
    st.apply({type: 'usage_update', usage: rec('gpu1', 'whatever', NOW)});
    eq(M.usageCards(st, NOW)[0].sourceText, '');  // unknown or missing: nothing said
    const codex = withUsage([{...usage('local', 'codex', [win(4, 300)]), source: 'session_files'}]);
    eq(M.usageCards(codex, NOW)[0].sourceText, "From Codex's session files");
});

test('usage: a hint while the Claude query is off and Claude is used', () => {
    const off = {type: 'usage_api_status', agent: 'claude', status: {state: 'off', servers: false}};
    const quiet = make([], [host('local', 'local')]);
    quiet.apply(off);
    eq(M.usageHint(quiet), null);  // Claude not seen
    const here = make([s('local', 'a', 'running')], [host('local', 'local')]);
    here.apply(off);
    ok(M.usageHint(here).includes('terminal sessions only'), 'hint');
    const reading = withUsage([usage('local', 'claude', [win(10, 300)])], [off]);
    ok(M.usageHint(reading) !== null, 'a reading counts as seen');
    reading.apply({type: 'usage_api_status', agent: 'claude', status: {state: 'ok'}});
    eq(M.usageHint(reading), null);
    eq(M.usageHint(withUsage([usage('local', 'codex', [win(10, 300)])], [off])), null);
    eq(M.usageHint(withUsage([usage('local', 'claude', [win(10, 300)])])), null);  // old daemon
});

test('usage updates, removal, and disconnect', () => {
    const u = usage('gpu1', 'claude', [win(10, 300)]);
    const st = withUsage([], [{type: 'usage_update', usage: u}]);
    eq(M.usageCards(st, NOW).length, 1);
    st.apply({type: 'usage_update', usage: usage('gpu1', 'claude', [win(30, 300)])});
    eq(M.usageCards(st, NOW)[0].rows[0].percent, 30);
    st.apply({type: 'usage_remove', host: 'gpu1', agent: 'claude'});
    eq(M.usageCards(st, NOW), []);
    st.apply({type: 'usage_update', usage: u});
    st.apply({type: 'snapshot', sessions: [], hosts: []});
    eq(M.usageCards(st, NOW), []);  // a snapshot without usage replaces it
    st.apply({type: 'usage_update', usage: u});
    st.disconnect();
    eq(M.usageCards(st, NOW), []);
});

test('guard reports what throws and returns', () => {
    const errors = [];
    eq(M.guard(() => {}, e => errors.push(e)), true);
    eq(M.guard(() => {
        throw new Error('boom');
    }, e => errors.push(e.message)), false);
    eq(errors, ['boom']);
    // A throwing reporter is swallowed too: nothing escapes into the main loop.
    eq(M.guard(() => {
        throw new Error('a');
    }, () => {
        throw new Error('b');
    }), false);
});

test('the read loop goes on after a message handler throws', () => {
    // Mirrors UiClient._read: dispatch each line, then read the next one.
    const lines = [
        '{"type":"notify","kind":"attention"}',
        'not json',
        'null',
        '{"type":"session_update","boom":true}',
        JSON.stringify({type: 'session_update', session: s('local', 'a', 'running')}),
        '{"type":"snapshot","sessions":[],"hosts":[]}',
    ];
    const st = new M.UiState();
    const handled = [], errors = [];
    const handle = msg => {
        if (msg.boom || msg.type === 'notify')
            throw new TypeError(`no such API for ${msg.type}`);
        st.apply(msg);
        handled.push(msg.type);
    };
    const results = lines.map(line => M.dispatchLine(line, handle, e => errors.push(e.message)));
    eq(results, [false, false, false, false, true, true]);
    eq(handled, ['session_update', 'snapshot']);
    eq(errors, ['no such API for notify', 'no such API for session_update']);
    ok(st.connected, 'the snapshot after the failures was applied');
});

test('language: choice, then the desktop, then English', () => {
    eq(I.resolveLanguage('zh', ['en_US.UTF-8']), 'zh');
    eq(I.resolveLanguage('en', ['zh_CN.UTF-8']), 'en');
    eq(I.resolveLanguage(null, ['zh_CN.UTF-8', 'zh_CN', 'zh', 'C']), 'zh');
    eq(I.resolveLanguage(null, ['de_DE.UTF-8', 'de', 'zh_TW', 'C']), 'zh');
    eq(I.resolveLanguage(null, ['de_DE.UTF-8', 'de', 'C']), 'en');
    eq(I.resolveLanguage('fr', []), 'en');
    eq(I.resolveLanguage(null, undefined), 'en');
});

test('translation and placeholders', () => {
    try {
        I.setLanguage('zh');
        eq(I.language(), 'zh');
        eq(I._('Your turn'), '轮到你');
        eq(I._('{n} running', {n: 3}), '3 个运行中');
        eq(I._('not a key {x}', {x: '{y}', y: 1}), 'not a key {y}');  // one pass
        eq(I._('constructor'), 'constructor');
        I.setLanguage('xx');
        eq(I.language(), 'en');
        eq(I._('{n} running', {n: 3}), '3 running');
    } finally {
        I.setLanguage('en');
    }
});

test('the panel in Chinese', () => {
    try {
        I.setLanguage('zh');
        const st = make([
            s('local', 'w', 'waiting_input', {last: NOW - 360}),
            s('gpu1', 'x', 'stale', {prompt: ''}),
            s('gpu1', 'e', 'ended'),
        ]);
        const cards = M.agentGroups(st, NOW)[0].cards;
        eq(cards.map(c => [c.statusLabel, c.host, c.age]), [['轮到你', '本机', '6分钟'], ['已过时', 'gpu1', '5秒']]);
        eq(cards[1].subtitle, '最后一次看到时在运行');
        eq(M.summary(st).text, '1 个轮到你');
        eq(M.footer(st), {ended: '最近结束了 1 个'});
        eq([600, 7800, 90000].map(M.durationText), ['10分钟', '2小时10分', '1天1小时']);
        eq([300, 10080, 0].map(M.windowLabel), ['5小时', '每周', '限额']);
        eq(M.hostRows(st)[0].stateText, '已连接');
        eq(M.extraRow({label: 'Extra usage', kind: 'spent', amount: 2, limit: 10, currency: 'USD'}).label, '额外用量');
        eq(M.extraRow({label: 'Bonus', kind: 'spent', amount: 2, currency: 'USD'}).label, 'Bonus');
        eq(M.usageLabelWidth(['每周 · Fable']), M.usageLabelWidth(['xxxx · Fable']));  // wide: two columns
        const n = M.notice({type: 'notify', kind: 'turn_done', key: {host: 'local', agent: 'claude', session_id: 'w'}}, st);
        eq([n.title, n.body], ['app 已完成', '本机']);
        eq(M.addedMessage('gpu7', ''), '已添加 gpu7。');
        eq(M.addHostMessage('', false), '失败');
    } finally {
        I.setLanguage('en');
    }
});

test('every string marked for translation has a Chinese entry', () => {
    const code = ['model.js', 'extension.js'].map(source).join('\n');
    ok(!/(?<![\w$.])N?_\(\s*`/.test(code), 'no template literals as keys');
    const keys = new Set();
    const re = /(?<![\w$.])N?_\(\s*(?:'([^'\\\n]*)'|"([^"\\\n]*)")/g;
    for (const m of code.matchAll(re))
        keys.add(m[1] ?? m[2]);
    ok(keys.size > 120, `found only ${keys.size} strings`);
    const holes = text => [...text.matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort();
    for (const key of keys) {
        ok(Object.hasOwn(I.ZH, key), `no Chinese for "${key}"`);
        eq(holes(I.ZH[key]), holes(key), `placeholders of "${key}"`);
    }
    for (const key of Object.keys(I.ZH))
        ok(keys.has(key), `unused Chinese entry "${key}"`);
});


// ---- approvals (0.0.6) -------------------------------------------------------------------

const ID = 'a'.repeat(32);
const approval = (sid, kw = {}) => ({
    id: kw.id ?? ID,
    key: {host: kw.host ?? 'local', agent: kw.agent ?? 'claude', session_id: sid},
    tool: 'Bash',
    detail: kw.detail ?? 'rm -rf build',
    description: kw.description ?? 'Remove the build directory',
    created_ts: NOW - 5,
    expires_ts: kw.expires ?? NOW + 42.2,
});

test('approvals: kept from the snapshot and messages, malformed ones dropped', () => {
    const st = new M.UiState();
    st.apply({type: 'snapshot', sessions: [s('local', 'a', 'needs_attention')], hosts: [],
        approvals: [approval('a'), approval('a', {id: 'xyz'}), approval('a', {id: 'b'.repeat(32), detail: 5}),
            approval('a', {id: 'c'.repeat(32), detail: ''}),
            approval('a', {id: 'd'.repeat(32), detail: 'x'.repeat(M.APPROVAL_DETAIL_CHARS + 1)}), null, 'junk']});
    eq([...st.approvals.keys()], [ID]);
    st.apply({type: 'approval', approval: approval('a', {id: 'e'.repeat(32)})});
    eq(st.approvals.size, 2);
    st.apply({type: 'approval_remove', id: ID});
    eq([...st.approvals.keys()], ['e'.repeat(32)]);
    st.disconnect();
    eq(st.approvals.size, 0);
});

test('approvals: shown on their session card, in full', () => {
    const long = `echo ${'x'.repeat(300)}`;
    const st = make([s('local', 'a', 'needs_attention'), s('gpu1', 'b', 'needs_attention', {agent: 'codex'}),
        s('local', 'c', 'running')]);
    st.apply({type: 'approval', approval: approval('a', {detail: `cat <<EOF\n${long}\n\u001b[31mEOF`})});
    st.apply({type: 'approval', approval: approval('b', {id: 'f'.repeat(32), host: 'gpu1', agent: 'codex'})});
    const cards = M.agentGroups(st, NOW).flatMap(g => g.cards);
    const byId = Object.fromEntries(cards.map(c => [c.id, c]));
    const a = byId.a.approval;
    // Every line, nothing cut, escapes neutralised.
    eq(a.detail, `cat <<EOF\n${long}\n?[31mEOF`);
    eq([a.id, a.tool, a.description, a.leftText, a.sent], [ID, 'Bash', 'Remove the build directory', '43s left', null]);
    eq(a.after, 'You can also answer it in the terminal.');
    eq(byId.b.approval.after, 'Then Codex asks in its terminal.');
    eq(byId.c.approval, null);
    // Clicked: no buttons until the daemon removes it.
    st.markSent(ID, 'allow');
    eq(M.agentGroups(st, NOW)[0].cards.find(c => c.id === 'a').approval.leftText, 'Sending…');
    // Expired but not removed yet: no negative countdown.
    eq(M.approvalOf(st, s('gpu1', 'b', 'needs_attention', {agent: 'codex'}), NOW + 100).leftText, '0s left');
});

test('approvals: settings', () => {
    const old = make([]);
    old.apply({type: 'settings', settings: {usage: true, notifications: {turn_done: true, attention: true, details: false}}});
    eq(M.approvalSettings(old).available, false);  // a 0.0.5 daemon
    eq(M.approvalSettings(old).toggle.enabled, false);
    const st = make([]);
    st.apply({type: 'settings', settings: {usage: true, approvals: false, approval_wait_s: {claude: 120, codex: 30, x: 'no'}}});
    eq(st.settings.approvalWait, {claude: 120, codex: 30});
    let v = M.approvalSettings(st);
    eq([v.available, v.toggle.id, v.toggle.on, v.toggle.enabled, v.wait.enabled], [true, 'approvals', false, true, false]);
    // Switched on in the panel: shown at once as saving, the wait choices with it.
    let pending = {approvals: true};
    v = M.approvalSettings(st, pending);
    eq([v.toggle.on, v.toggle.saving, v.wait.enabled], [true, true, true]);
    eq(v.wait.choices.map(c => [c.value, c.on]), [[10, false], [20, false], [30, true], [60, false]]);
    st.apply({type: 'settings', settings: {usage: true, approvals: true, approval_wait_s: {claude: 120, codex: 30}}});
    pending = M.settlePending(st, pending);
    eq(pending, {});
    pending = {wait_codex: 10};
    v = M.approvalSettings(st, pending);
    eq([v.wait.saving, v.wait.choices.find(c => c.on).value], [true, 10]);
    eq(M.settlePending(st, pending), {wait_codex: 10});
    st.apply({type: 'settings', settings: {usage: true, approvals: true, approval_wait_s: {claude: 120, codex: 10}}});
    eq(M.settlePending(st, pending), {});
});

test('cleanLines keeps line breaks only', () => {
    eq(M.cleanLines('a\r\nb\rc\n\td‮'), 'a\nb\nc\n d?');
    eq(M.cleanLines(null), '');
});

for (const [name, fn] of tests) {
    try {
        fn();
    } catch (e) {
        failures++;
        print(`FAIL ${name}: ${e.message}`);
    }
}
print(`${tests.length - failures}/${tests.length} passed`);
System.exit(failures ? 1 : 0);
