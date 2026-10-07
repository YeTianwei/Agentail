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
    const st = make([s('local', 'a', 'running'), s('gpu1', 'b', 'running'),
        s('gpu1', 'c', 'waiting_input'), s('gpu1', 'd', 'ended')]);
    let sum = M.summary(st);
    eq([sum.level, sum.counts, sum.text], ['busy', {attention: 0, running: 2, waiting: 1}, '2 running · 1 waiting']);
    st.apply({type: 'session_update', session: s('local', 'a', 'needs_attention')});
    sum = M.summary(st);
    eq([sum.level, sum.text], ['attention', '1 needs you · 1 running · 1 waiting']);
    eq(M.summary(make([])).text, 'No active sessions');
    eq(M.summary(make([s('local', 'w', 'waiting_input')])).level, 'waiting');
});

test('hosts down', () => {
    const st = make([]);
    for (const [state, down] of [['connected', false], ['connecting', true], ['backoff', true],
        ['auth_failed', true], ['stopped', false], ['weird\x1b[31m', false]]) {
        st.apply({type: 'host_status', host: host('gpu1', state, '', 'why')});
        eq(M.summary(st).hostsDown.length, down ? 1 : 0, state);
    }
    st.apply({type: 'host_status', host: host('gpu1', 'backoff', '', 'ssh exited (255)')});
    const g = M.groups(st, NOW).find(x => x.alias === 'gpu1');
    eq([g.level, g.stateText, g.problem], ['pending', 'reconnecting', 'ssh exited (255)']);
    eq(M.summary(st).text, '1 host unreachable');
    st.apply({type: 'host_remove', alias: 'gpu1'});
    eq(M.summary(st).hostsDown, []);
});

test('groups order, cards and ended count', () => {
    const st = make([
        s('gpu1', 'w', 'waiting_input', {last: NOW - 120}),
        s('gpu1', 'n', 'needs_attention', {tool: 'Bash', detail: 'rm -rf build', message: 'Claude needs your permission to use Bash', last: NOW - 30}),
        s('gpu1', 'r', 'running', {agent: 'codex', cwd: '/data/x/openpi/', last: NOW - 1}),
        s('gpu1', 'e', 'ended'),
        s('local', 'l', 'running'),
        s('gpu9', 'x', 'stale', {prompt: ''}),
    ]);
    const gs = M.groups(st, NOW);
    eq(gs.map(g => g.alias), ['local', 'gpu1', 'gpu9']);
    eq(gs[0].title, 'This computer');
    eq(gs[1].title, 'GPU one (gpu1)');
    eq(gs[1].cards.map(c => c.id), ['n', 'w', 'r']);
    eq(gs[1].ended, 1);
    const n = gs[1].cards[0];
    eq([n.project, n.subtitle, n.statusLabel, n.tool, n.toolDetail, n.age, n.agentName],
        ['app', 'Claude needs your permission to use Bash', 'Needs you', 'Bash', 'rm -rf build', '30s', 'Claude Code']);
    const r = gs[1].cards[2];
    eq([r.project, r.agent, r.agentName, r.style], ['openpi', 'codex', 'Codex', 'running']);
    eq([gs[2].level, gs[2].stateText, gs[2].cards[0].subtitle], ['off', 'unknown', 'Last seen running']);
    const f = M.footer(st);
    eq(f, {ended: '1 ended in the last 10 minutes', hosts: '1 host · all connected'});
});

test('footer host summary', () => {
    eq(M.footer(make([], [host('local', 'local'), host('a', 'connected'), host('b', 'stopped')])).hosts, '2 hosts · 1 connected');
    eq(M.footer(make([], [host('a', 'backoff'), host('b', 'connected')])).hosts, '2 hosts · 1 unreachable');
    eq(M.footer(make([], [host('local', 'local')])), {ended: '', hosts: ''});
});

test('remote host without sessions is listed', () => {
    eq(M.groups(make([]), NOW).map(g => [g.alias, g.cards.length]), [['local', 0], ['gpu1', 0]]);
});

test('untrusted strings are cleaned and bounded', () => {
    const evil = '\x1b]8;;http://x\x07click\x1b]8;;\x07 <b>bold</b>\n‮' + 'y'.repeat(500);
    const st = make([s('gpu1', 'e', 'running', {prompt: evil, cwd: '/a/\x1b[2Jb/' + 'c'.repeat(100)})]);
    const c = M.groups(st, NOW).find(g => g.alias === 'gpu1').cards[0];
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
    eq([M.summary(st).level, M.groups(st, NOW)], ['offline', []]);
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
