// Tests for the GNOME extension's model.js. Run by tests/test_gnome_extension.py:
//   gjs -m tests/js/test_model.js <path to model.js>
// Prints one line per failure and exits 1 if any test failed.
import System from 'system';

const [modelPath] = ARGV;
const M = await import(`file://${modelPath}`);

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
    for (const [state, down] of [['connected', false], ['connecting', true], ['backoff', true],
        ['auth_failed', true], ['stopped', false], ['weird\x1b[31m', false]]) {
        st.apply({type: 'host_status', host: host('gpu1', state, '', 'why')});
        eq(M.summary(st).hostsDown.length, down ? 1 : 0, state);
    }
    st.apply({type: 'host_status', host: host('gpu1', 'backoff', '', 'ssh exited (255)')});
    const h = M.hostRows(st).find(x => x.alias === 'gpu1');
    eq([h.level, h.stateText, h.problem], ['pending', 'reconnecting', 'ssh exited (255)']);
    eq(M.summary(st).text, '1 host unreachable');
    st.apply({type: 'host_remove', alias: 'gpu1'});
    eq(M.summary(st).hostsDown, []);
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
});

test('notices', () => {
    const st = make([s('gpu1', 'a', 'needs_attention', {tool: 'Bash', detail: 'make', cwd: '/x/SpatialVLA'})]);
    const msg = {type: 'notify', kind: 'attention', key: {host: 'gpu1', agent: 'claude', session_id: 'a'}};
    eq(st.apply(msg), msg);
    const n = M.notice(msg, st);
    eq([n.title, n.body, n.urgent, n.agent],
        ['SpatialVLA needs you', 'gpu1 · Claude Code wants to run Bash: make', true, 'claude']);
    const done = M.notice({kind: 'turn_done', key: {host: 'local', agent: 'codex', session_id: 'zz'}}, st);
    eq([done.title, done.body, done.urgent], ['? is done', 'this computer', false]);
    eq(M.notice({kind: 'other', key: {}}, st), null);
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
    eq([M.basename('/data/twye/Baselines/SpatialVLA/'), M.basename(''), M.basename('/')], ['SpatialVLA', '', '/']);
    eq(M.clean('a\tb\u0007c', 0), 'a b?c');
    eq(M.clean('abcdef', 4), 'abc…');
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
