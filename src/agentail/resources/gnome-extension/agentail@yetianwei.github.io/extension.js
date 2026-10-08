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
                    this._renderPanel();
                    return GLib.SOURCE_CONTINUE;
                });
            } else if (this._ageTimer) {
                GLib.source_remove(this._ageTimer);
                this._ageTimer = 0;
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
            if (this.menu.isOpen)
                this._renderPanel();
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
        const hosts = M.hostRows(st);
        if (hosts.length)
            content.add_child(this._hosts(hosts));

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
            card.add_child(row);
            if (h.problem) {
                const problem = label(`${h.problem}. Sessions may be out of date.`, 'agentail-host-problem');
                problem.clutter_text.line_wrap = true;
                card.add_child(problem);
            }
        }
        box.add_child(card);
        return box;
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
