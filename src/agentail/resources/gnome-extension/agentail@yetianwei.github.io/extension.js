// Agentail top bar panel for GNOME Shell (45+, tested on 46).
//
// A client of the agentail daemon's ui.sock (docs/protocol.md): it reads
// newline-delimited JSON, keeps no state of its own beyond the last snapshot,
// and reconnects with backoff when the daemon goes away.
//
// What to show is decided in model.js (pure, tested with gjs). Everything from
// the daemon is untrusted payload text: it is only ever set as plain St.Label
// text, and notification bodies are sent without markup.

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import * as M from './model.js';

const RECONNECT_MAX_S = 10;
const AGE_REFRESH_S = 5;
const KNOWN_AGENTS = ['claude', 'codex'];
const AGENT_ICON_SIZE = 16;
const ADD_HOST_TIMEOUT_S = 150;
// Replaced by packaging/build-deb.sh with the same stamp it writes into metadata.json's
// "version-name". GNOME Shell keeps an extension's code in memory until it restarts, so
// after an upgrade the disk stamp differs from this one and the panel says so.
const BUILD = 'dev';

function uiSocketPath() {
    return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'agentail', 'ui.sock']);
}

// ---- ui.sock client ----------------------------------------------------------------

class UiClient {
    constructor(path, onMessage, onDisconnect) {
        this._path = path;
        this._onMessage = onMessage;
        this._onDisconnect = onDisconnect;
        this._delay = 1;
        this._timer = 0;
        this._cancellable = null;
        this._conn = null;
        this._stream = null;
    }

    start() {
        this._connect();
    }

    stop() {
        this._cancellable?.cancel();
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        this._close();
    }

    _connect() {
        this._cancellable = new Gio.Cancellable();
        const client = new Gio.SocketClient();
        const address = Gio.UnixSocketAddress.new(this._path);
        client.connect_async(address, this._cancellable, (c, res) => {
            try {
                this._conn = c.connect_finish(res);
            } catch (e) {
                if (!e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    this._retry();
                return;
            }
            this._delay = 1;
            this._stream = new Gio.DataInputStream({
                base_stream: this._conn.get_input_stream(),
                close_base_stream: true,
            });
            this._read();
        });
    }

    _read() {
        this._stream.read_line_async(GLib.PRIORITY_DEFAULT, this._cancellable, (s, res) => {
            let line = null;
            try {
                [line] = s.read_line_finish_utf8(res);
            } catch (e) {
                if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
            }
            if (line === null) {
                this._close();
                this._onDisconnect();
                this._retry();
                return;
            }
            let msg = null;
            try {
                msg = JSON.parse(line);
            } catch {
                // ignore a malformed line; the next snapshot repairs any gap
            }
            if (msg)
                this._onMessage(msg);
            this._read();
        });
    }

    _retry() {
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._delay, () => {
            this._timer = 0;
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
        this._delay = Math.min(RECONNECT_MAX_S, this._delay * 2);
    }

    _close() {
        try {
            this._stream?.close(null);
            this._conn?.close(null);
        } catch {
            // already closed
        }
        this._stream = null;
        this._conn = null;
    }
}

// ---- widgets -----------------------------------------------------------------------

function label(text, styleClass, {ellipsize = false, xExpand = false} = {}) {
    const l = new St.Label({
        text, // plain text: never use_markup
        style_class: styleClass,
        x_expand: xExpand,
        y_align: Clutter.ActorAlign.CENTER,
    });
    l.clutter_text.ellipsize = ellipsize ? Pango.EllipsizeMode.END : Pango.EllipsizeMode.NONE;
    return l;
}

function hbox(styleClass, props = {}) {
    return new St.BoxLayout({style_class: styleClass, ...props});
}

function vbox(styleClass, props = {}) {
    return new St.BoxLayout({style_class: styleClass, vertical: true, ...props});
}

const AgentailIndicator = GObject.registerClass(
class AgentailIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Agentail', false);
        this._ext = extension;
        this.add_style_class_name('agentail-button');
        this._state = new M.UiState();
        this._limiter = new M.RateLimiter();
        this._renderId = 0;
        this._ageTimer = 0;
        this._source = null;
        // "Add server" flow: idle | picking | running | result (see _addServer)
        this._add = {mode: 'idle', candidates: null, alias: '', ok: false, message: ''};
        this._addProc = null;
        this._addCancel = null;
        this._addTimer = 0;

        // The "island": a dark capsule inside the panel button. Its content is rebuilt
        // by _renderTopBar() whenever the summary changes.
        this._capsule = hbox('agentail-capsule', {y_align: Clutter.ActorAlign.CENTER});
        this._topSig = '';
        this.add_child(this._capsule);

        this.menu.actor.add_style_class_name('agentail-menu');
        const item = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
            style_class: 'agentail-item',
        });
        this._panel = vbox('agentail-panel', {x_expand: true});
        item.add_child(this._panel);
        this.menu.addMenuItem(item);
        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open) {
                this._renderPanel();
                this._ageTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, AGE_REFRESH_S, () => {
                    if (this._add.mode !== 'picking')
                        this._renderPanel();
                    return GLib.SOURCE_CONTINUE;
                });
            } else {
                if (this._add.mode === 'picking' || this._add.mode === 'result')
                    this._add = {mode: 'idle', candidates: null, alias: '', ok: false, message: ''};
                if (this._ageTimer) {
                    GLib.source_remove(this._ageTimer);
                    this._ageTimer = 0;
                }
            }
        });

        this._client = new UiClient(
            uiSocketPath(),
            msg => this._onMessage(msg),
            () => {
                this._state.disconnect();
                this._scheduleRender();
            });
        this._renderTopBar();
        this._client.start();
    }

    _gicon(name) {
        return Gio.FileIcon.new(this._ext.dir.get_child('icons').get_child(name));
    }

    _onMessage(msg) {
        const notify = this._state.apply(msg);
        if (notify) {
            const n = M.notice(notify, this._state);
            if (n && this._limiter.allow(n, GLib.get_monotonic_time() / 1e6))
                this._notify(n);
        } else {
            this._scheduleRender();
        }
    }

    _scheduleRender() {
        if (this._renderId)
            return;
        this._renderId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._renderId = 0;
            this._renderTopBar();
            if (this.menu.isOpen && this._add.mode !== 'picking')
                this._renderPanel();  // a rebuild would drop what is being typed
            return GLib.SOURCE_REMOVE;
        });
    }

    // -- top bar -------------------------------------------------------------------------

    // Idle: a small dot. Otherwise: the agents that have live sessions, then one segment
    // per kind (needs you / running / your turn). "Needs you" turns the whole capsule
    // orange and names the project.
    _renderTopBar() {
        const sum = M.summary(this._state);
        const sig = JSON.stringify([sum.level, sum.counts, sum.agents, sum.lead,
            sum.hostsDown.length > 0]);
        this.accessible_name = `Agentail: ${sum.text}`;
        if (sig === this._topSig)
            return;
        this._topSig = sig;

        const c = this._capsule;
        c.remove_all_transitions();
        c.opacity = 255;
        c.destroy_all_children();
        for (const cls of ['agentail-capsule-idle', 'agentail-capsule-attention',
            'agentail-capsule-offline'])
            c.remove_style_class_name(cls);

        if (sum.level === 'offline' || sum.level === 'idle') {
            const offline = sum.level === 'offline';
            c.add_style_class_name(offline ? 'agentail-capsule-offline' : 'agentail-capsule-idle');
            // The resting island: just a small dot (hollow when the daemon is unreachable).
            c.add_child(new St.Widget({
                style_class: offline ? 'agentail-idle-dot agentail-idle-dot-offline' : 'agentail-idle-dot',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        } else {
            for (const agent of sum.agents) {
                c.add_child(KNOWN_AGENTS.includes(agent)
                    ? new St.Icon({
                        gicon: this._gicon(`agent-${agent}.png`),
                        icon_size: AGENT_ICON_SIZE,
                        style_class: 'agentail-capsule-agent',
                    })
                    : label(Array.from(agent)[0]?.toUpperCase() ?? '?', 'agentail-capsule-letter'));
            }
            const {attention, running, waiting} = sum.counts;
            if (attention) {
                // Orange capsule: the one thing that needs you, plus how many others run.
                c.add_style_class_name('agentail-capsule-attention');
                const glyph = new St.Icon({gicon: this._gicon('spinner-attention.svg'), icon_size: 14});
                c.add_child(glyph);
                const text = attention > 1 ? `${attention} need you` : `${sum.lead || 'Agent'} needs you`;
                c.add_child(label(text, 'agentail-capsule-text', {ellipsize: true}));
                const others = running + waiting;
                if (others)
                    c.add_child(label(`+${others}`, 'agentail-capsule-count'));
                glyph.ease({
                    opacity: 90,
                    duration: 800,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                    repeatCount: -1,
                    autoReverse: true,
                });
            } else {
                if (running) {
                    const spinner = new St.Icon({gicon: this._gicon('spinner.svg'), icon_size: 14});
                    spinner.set_pivot_point(0.5, 0.5);
                    spinner.ease({
                        rotation_angle_z: 360,
                        duration: 1000,
                        mode: Clutter.AnimationMode.LINEAR,
                        repeatCount: -1,
                    });
                    c.add_child(spinner);
                    c.add_child(label(String(running), 'agentail-capsule-count'));
                }
                if (waiting) {
                    c.add_child(new St.Icon({gicon: this._gicon('dot-waiting.svg'), icon_size: 14}));
                    c.add_child(label(String(waiting), 'agentail-capsule-count'));
                }
            }
        }
        if (sum.hostsDown.length) {
            c.add_child(new St.Widget({
                style_class: 'agentail-host-down-dot', y_align: Clutter.ActorAlign.START,
            }));
        }
    }

    // -- panel ------------------------------------------------------------------------------

    _renderPanel() {
        try {
            this._buildPanel();
        } catch (e) {
            // Never leave the menu unusable because one session could not be drawn.
            console.error(`agentail: cannot render the panel: ${e}\n${e.stack}`);
            this._panel.destroy_all_children();
            this._panel.add_child(label('Agentail could not draw this panel. See the GNOME Shell log.', 'agentail-empty-text'));
        }
    }

    _buildPanel() {
        const now = Date.now() / 1000;
        const st = this._state;
        this._panel.destroy_all_children();

        if (this._outdated())
            this._panel.add_child(this._updateBanner());
        if (!st.connected) {
            this._panel.add_child(this._offline());
            return;
        }
        const groups = M.agentGroups(st, now);
        const content = vbox('agentail-groups', {x_expand: true});
        if (!groups.length)
            content.add_child(this._empty());
        for (const g of groups)
            content.add_child(this._agentGroup(g));
        content.add_child(this._hosts(M.hostRows(st)));

        const scroll = new St.ScrollView({
            style_class: 'agentail-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: false,
            x_expand: true,
        });
        if (scroll.set_child)
            scroll.set_child(content);
        else
            scroll.add_actor(content);
        this._panel.add_child(scroll);

        const foot = M.footer(st);
        if (foot.ended)
            this._panel.add_child(label(foot.ended, 'agentail-footer'));
    }

    _agentGroup(g) {
        const box = vbox('agentail-group', {x_expand: true});
        const chip = hbox(`agentail-chip agentail-chip-${KNOWN_AGENTS.includes(g.agent) ? g.agent : 'other'}`,
            {x_align: Clutter.ActorAlign.START});
        if (KNOWN_AGENTS.includes(g.agent)) {
            chip.add_child(new St.Icon({
                gicon: this._gicon(`agent-${g.agent}.png`),
                icon_size: 16,
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        chip.add_child(label(`${g.name} · ${g.count}`, 'agentail-chip-text'));
        box.add_child(chip);
        for (const c of g.cards)
            box.add_child(this._card(c));
        return box;
    }

    _card(c) {
        const box = vbox(`agentail-card agentail-card-${c.style}`, {x_expand: true});

        const top = hbox('agentail-card-top', {x_expand: true});
        top.add_child(new St.Widget({
            style_class: `agentail-dot agentail-dot-${c.style}`,
            y_align: Clutter.ActorAlign.CENTER,
        }));
        top.add_child(label(c.project, 'agentail-project', {ellipsize: true, xExpand: true}));
        const age = c.style === 'attention' && c.age ? `for ${c.age}` : c.age;
        top.add_child(label(c.statusLabel, `agentail-status agentail-status-${c.style}`));
        if (age)
            top.add_child(label(age, 'agentail-age'));
        box.add_child(top);

        const second = hbox('agentail-card-second', {x_expand: true});
        const line = [c.host, c.subtitle].filter(x => x).join(' · ');
        second.add_child(label(line, 'agentail-subtitle', {ellipsize: true, xExpand: true}));
        if (c.tool && (c.style === 'running' || c.style === 'attention'))
            second.add_child(label(c.tool, 'agentail-tool'));
        box.add_child(second);

        if (c.style === 'attention') {
            if (c.toolDetail)
                box.add_child(label(c.toolDetail, 'agentail-command', {ellipsize: true}));
            const where = c.host === 'This computer' ? 'Answer it in the terminal' : `Answer it in the terminal on ${c.host}`;
            box.add_child(label(where, 'agentail-hint', {ellipsize: true}));
        }
        return box;
    }

    _hosts(rows) {
        const box = vbox('agentail-group', {x_expand: true});
        box.add_child(label('Servers', 'agentail-section'));
        const card = vbox('agentail-card agentail-hosts', {x_expand: true});
        for (const h of rows) {
            const row = hbox('agentail-host-row', {x_expand: true});
            row.add_child(new St.Widget({
                style_class: `agentail-dot agentail-host-${h.level}`,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            row.add_child(label(h.title, 'agentail-host-name', {ellipsize: true, xExpand: true}));
            const detail = h.level === 'ok' && h.sessions
                ? `${h.stateText} · ${h.sessions} active` : h.stateText;
            row.add_child(label(detail, `agentail-host-state agentail-host-text-${h.level}`));
            if (this._add.mode === 'idle') {
                const x = this._button('✕', 'agentail-btn-x', () => this._setAdd({mode: 'confirm', alias: h.alias}));
                x.accessible_name = `Remove ${h.alias}`;
                row.add_child(x);
            }
            card.add_child(row);
            if (h.problem) {
                const problem = label(`${h.problem}. Sessions may be out of date.`, 'agentail-host-problem');
                problem.clutter_text.line_wrap = true;
                card.add_child(problem);
            }
        }
        if (rows.length)
            box.add_child(card);
        box.add_child(this._addServer());
        return box;
    }

    // -- add server ---------------------------------------------------------------------

    _setAdd(state) {
        this._add = {mode: 'idle', candidates: null, alias: '', ok: false, message: '', ...state};
        this._renderPanel();
    }

    _button(text, styleClass, onClick) {
        const b = new St.Button({
            style_class: `agentail-btn ${styleClass}`,
            reactive: true,
            can_focus: true,
            track_hover: true,
            x_align: Clutter.ActorAlign.START,
            child: label(text, ''),
        });
        b.connect('clicked', onClick);
        return b;
    }

    _addServer() {
        const box = vbox('agentail-add', {x_expand: true});
        const a = this._add;
        if (a.mode === 'idle') {
            box.add_child(this._button('＋  Add server', 'agentail-btn-add', () => this._openPicker()));
        } else if (a.mode === 'picking') {
            box.add_child(label('Pick a server from your SSH config', 'agentail-add-title'));
            if (a.candidates === null) {
                box.add_child(label('Looking…', 'agentail-add-note'));
            } else {
                for (const alias of a.candidates.slice(0, 8))
                    box.add_child(this._button(alias, 'agentail-btn-pick', () => this._runAddHost(alias)));
                if (a.candidates.length > 8)
                    box.add_child(label(`and ${a.candidates.length - 8} more: type the name below`, 'agentail-add-note'));
                if (!a.candidates.length)
                    box.add_child(label('No unused hosts in ~/.ssh/config. Type its ssh name below.', 'agentail-add-note'));
            }
            const entry = new St.Entry({
                style_class: 'agentail-entry',
                hint_text: 'or type a name, then Enter',
                can_focus: true,
                x_expand: true,
            });
            entry.clutter_text.connect('activate', () => this._runAddHost(entry.get_text().trim()));
            box.add_child(entry);
            box.add_child(this._button('Cancel', 'agentail-btn-quiet', () => this._setAdd({})));
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                entry.grab_key_focus();
                return GLib.SOURCE_REMOVE;
            });
        } else if (a.mode === 'confirm') {
            const msg = label(`Remove ${a.alias}? Agentail puts the server's Claude and Codex settings back as they were and stops watching it.`, 'agentail-add-title');
            msg.clutter_text.line_wrap = true;
            box.add_child(msg);
            const row = hbox('agentail-add-row');
            row.add_child(this._button('Remove', 'agentail-btn-danger', () => this._runRemoveHost(a.alias, false)));
            row.add_child(this._button('Cancel', 'agentail-btn-quiet', () => this._setAdd({})));
            box.add_child(row);
        } else if (a.mode === 'running') {
            const row = hbox('agentail-add-row');
            const spinner = new St.Icon({gicon: this._gicon('spinner.svg'), icon_size: 16});
            spinner.set_pivot_point(0.5, 0.5);
            spinner.ease({rotation_angle_z: 360, duration: 1000, mode: Clutter.AnimationMode.LINEAR, repeatCount: -1});
            row.add_child(spinner);
            row.add_child(label(`${a.verb} ${a.alias}…`, 'agentail-add-title'));
            box.add_child(row);
            box.add_child(label('This talks to the server over SSH and can take a few seconds.', 'agentail-add-note'));
        } else {
            const msg = label(a.message, `agentail-add-result agentail-add-${a.ok ? 'ok' : 'fail'}`);
            msg.clutter_text.line_wrap = true;
            box.add_child(msg);
            const row = hbox('agentail-add-row');
            if (!a.ok && a.action === 'remove') {
                // The server could not be reached: forget it here without touching it.
                row.add_child(this._button('Forget it anyway', 'agentail-btn-danger', () => this._runRemoveHost(a.alias, true)));
            } else if (!a.ok) {
                row.add_child(this._button('Try again', 'agentail-btn-add', () => this._openPicker()));
            }
            row.add_child(this._button('Close', 'agentail-btn-quiet', () => this._setAdd({})));
            box.add_child(row);
        }
        return box;
    }

    // The agentail command-line tool does the work, so the panel and the terminal behave the
    // same. The alias is checked against the same rule the tool uses, and passed as one argv
    // element (never through a shell).
    _agentailBinary() {
        const packaged = '/usr/bin/agentail';
        if (GLib.file_test(packaged, GLib.FileTest.IS_EXECUTABLE))
            return packaged;
        return GLib.find_program_in_path('agentail');
    }

    _openPicker() {
        this._setAdd({mode: 'picking'});
        const bin = this._agentailBinary();
        if (!bin) {
            this._setAdd({mode: 'picking', candidates: []});
            return;
        }
        try {
            const proc = Gio.Subprocess.new([bin, 'list-ssh-hosts'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let aliases = [];
                try {
                    const [, out] = p.communicate_utf8_finish(res);
                    aliases = M.parseAliases(out);
                } catch {
                    // no list: the user can still type a name
                }
                if (this._add.mode === 'picking' && this._add.candidates === null)
                    this._setAdd({mode: 'picking', candidates: aliases});
            });
        } catch {
            this._setAdd({mode: 'picking', candidates: []});
        }
    }

    _runAddHost(alias) {
        this._runTool(alias, 'add', 'Adding', ['add-host', alias]);
    }

    _runRemoveHost(alias, localOnly) {
        this._runTool(alias, 'remove', 'Removing',
            localOnly ? ['remove-host', '--local-only', alias] : ['remove-host', alias]);
    }

    // Runs `agentail <args>` (argv list, no shell) and shows its last line in the panel.
    _runTool(alias, action, verb, args) {
        const fail = message => this._setAdd({mode: 'result', ok: false, action, alias, message});
        if (!M.validAlias(alias)) {
            fail('That is not a valid ssh host name.');
            return;
        }
        const bin = this._agentailBinary();
        if (!bin) {
            fail(`The agentail command was not found. Run \`agentail ${args[0]}\` in a terminal.`);
            return;
        }
        this._setAdd({mode: 'running', alias, verb});
        let proc;
        try {
            proc = Gio.Subprocess.new([bin, ...args],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE);
        } catch (e) {
            fail(M.clean(e.message, 240));
            return;
        }
        this._addProc = proc;
        this._addCancel = new Gio.Cancellable();
        this._addTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, ADD_HOST_TIMEOUT_S, () => {
            this._addTimer = 0;
            proc.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        proc.communicate_utf8_async(null, this._addCancel, (p, res) => {
            let out = '';
            let ok = false;
            try {
                [, out] = p.communicate_utf8_finish(res);
                ok = p.get_successful();
            } catch (e) {
                if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                out = `error: ${e.message}`;
            }
            this._addProc = null;
            if (this._addTimer) {
                GLib.source_remove(this._addTimer);
                this._addTimer = 0;
            }
            let message = M.addHostMessage(out, false);
            if (ok)
                message = action === 'add' ? M.addedMessage(alias, out) : M.removedMessage(alias, out);
            this._setAdd({mode: 'result', ok, action, alias, message});
        });
    }

    // True when the installed extension on disk is newer than the code running now.
    _outdated() {
        if (BUILD === 'dev')
            return false;
        try {
            const [, bytes] = GLib.file_get_contents(this._ext.dir.get_child('metadata.json').get_path());
            const onDisk = JSON.parse(new TextDecoder().decode(bytes))['version-name'];
            return typeof onDisk === 'string' && onDisk !== BUILD;
        } catch {
            return false;
        }
    }

    _updateBanner() {
        const text = label('Agentail was updated. Press Alt+F2, type r, Enter (X11), or log out and back in to load the new panel.',
            'agentail-update');
        text.clutter_text.line_wrap = true;
        return text;
    }

    _empty() {
        const box = vbox('agentail-empty', {x_expand: true});
        box.add_child(new St.Bin({
            style_class: 'agentail-empty-icon',
            x_align: Clutter.ActorAlign.CENTER,
            child: new St.Icon({gicon: this._gicon('agentail-mark.svg'), icon_size: 22}),
        }));
        box.add_child(label('All quiet', 'agentail-empty-title'));
        const text = label('Start Claude Code or Codex in a terminal and it shows up here.', 'agentail-empty-text');
        text.clutter_text.line_wrap = true;
        box.add_child(text);
        return box;
    }

    _offline() {
        const box = vbox('agentail-empty', {x_expand: true});
        box.add_child(new St.Bin({
            style_class: 'agentail-empty-icon',
            x_align: Clutter.ActorAlign.CENTER,
            child: new St.Icon({gicon: this._gicon('agentail-mark-offline.svg'), icon_size: 22}),
        }));
        box.add_child(label('The agentail daemon is not running', 'agentail-empty-title'));
        box.add_child(label('Retrying every few seconds. Start it with:', 'agentail-empty-text'));
        const cmd = label('agentail daemon', 'agentail-command');
        cmd.x_align = Clutter.ActorAlign.CENTER;
        box.add_child(cmd);
        return box;
    }

    // -- notifications -------------------------------------------------------------------------

    _notify(n) {
        if (!this._source) {
            this._source = new MessageTray.Source({
                title: 'Agentail',
                icon: this._gicon('agentail-mark.svg'),
            });
            this._source.connect('destroy', () => {
                this._source = null;
            });
            Main.messageTray.add(this._source);
        }
        const notification = new MessageTray.Notification({
            source: this._source,
            title: n.title,
            body: n.body, // use-body-markup stays false: payload text is never markup
            gicon: KNOWN_AGENTS.includes(n.agent) ? this._gicon(`agent-${n.agent}.png`) : null,
            urgency: n.urgent ? MessageTray.Urgency.HIGH : MessageTray.Urgency.NORMAL,
        });
        notification.connect('activated', () => this.menu.open());
        this._source.addNotification(notification);
    }

    destroy() {
        this._client.stop();
        this._addCancel?.cancel();
        if (this._addTimer)
            GLib.source_remove(this._addTimer);
        if (this._renderId)
            GLib.source_remove(this._renderId);
        if (this._ageTimer)
            GLib.source_remove(this._ageTimer);
        this._renderId = this._ageTimer = 0;
        this._source?.destroy();
        this._source = null;
        super.destroy();
    }
});

export default class AgentailExtension extends Extension {
    enable() {
        this._indicator = new AgentailIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
