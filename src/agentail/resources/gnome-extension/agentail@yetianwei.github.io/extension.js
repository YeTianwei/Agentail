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
const STATUS_ICON = {
    running: 'status-running.svg',
    waiting: 'status-waiting.svg',
    attention: 'status-attention.svg',
    stale: 'status-stale.svg',
};

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
        this._state = new M.UiState();
        this._limiter = new M.RateLimiter();
        this._renderId = 0;
        this._ageTimer = 0;
        this._source = null;
        this._pulsing = false;

        const box = hbox('agentail-button-box', {y_align: Clutter.ActorAlign.CENTER});
        this._mark = new St.Icon({
            gicon: this._gicon('agentail-mark.svg'),
            style_class: 'system-status-icon agentail-mark',
        });
        this._downDot = new St.Widget({
            style_class: 'agentail-host-down-dot',
            visible: false,
            y_align: Clutter.ActorAlign.END,
        });
        this._badges = hbox('agentail-badges', {y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._mark);
        box.add_child(this._downDot);
        box.add_child(this._badges);
        this.add_child(box);

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

    _renderTopBar() {
        const sum = M.summary(this._state);
        const offline = sum.level === 'offline';
        const mark = {offline: 'agentail-mark-offline.svg', attention: 'agentail-mark-attention.svg'};
        this._mark.gicon = this._gicon(mark[sum.level] ?? 'agentail-mark.svg');
        this._downDot.visible = sum.hostsDown.length > 0;
        this._badges.destroy_all_children();
        for (const [kind, n] of [['attention', sum.counts.attention],
            ['running', sum.counts.running], ['waiting', sum.counts.waiting]]) {
            if (n)
                this._badges.add_child(label(String(n), `agentail-badge agentail-badge-${kind}`));
        }
        this.accessible_name = `Agentail: ${sum.text}`;
        for (const cls of ['agentail-attention', 'agentail-offline'])
            this.remove_style_class_name(cls);
        if (offline)
            this.add_style_class_name('agentail-offline');
        this._setPulse(sum.level === 'attention');
    }

    _setPulse(on) {
        if (on)
            this.add_style_class_name('agentail-attention');
        if (on === this._pulsing)
            return;
        this._pulsing = on;
        this._badges.remove_all_transitions();
        if (on) {
            this._badges.ease({
                opacity: 110,
                duration: 900,
                mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1,
                autoReverse: true,
            });
        } else {
            this._badges.opacity = 255;
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
            this._panel.add_child(label('Agentail could not draw this panel. See the GNOME Shell log.', 'agentail-summary'));
        }
    }

    _buildPanel() {
        const now = Date.now() / 1000;
        const st = this._state;
        const sum = M.summary(st);
        this._panel.destroy_all_children();
        this._panel.add_child(this._header(sum));

        if (!st.connected) {
            this._panel.add_child(this._offline());
            return;
        }
        // Remote hosts are always listed (an idle one is a single header row); this
        // computer only when it has sessions.
        const groups = M.groups(st, now).filter(g => g.cards.length || g.problem || !g.local);
        const content = vbox('agentail-groups', {x_expand: true});
        if (!groups.some(g => g.cards.length))
            content.add_child(this._empty());
        for (const g of groups)
            content.add_child(this._group(g));
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
        if (foot.ended || foot.hosts) {
            this._panel.add_child(new St.Widget({style_class: 'agentail-divider', x_expand: true}));
            const row = hbox('agentail-footer', {x_expand: true});
            row.add_child(label(foot.ended, 'agentail-footer-left', {xExpand: true}));
            row.add_child(label(foot.hosts, 'agentail-footer-right'));
            this._panel.add_child(row);
        }
    }

    _header(sum) {
        const row = hbox('agentail-header', {x_expand: true});
        const titles = vbox('', {x_expand: true});
        titles.add_child(label('Agents', 'agentail-title'));
        titles.add_child(label(sum.text, 'agentail-summary', {ellipsize: true}));
        row.add_child(titles);
        const pills = hbox('agentail-pills', {y_align: Clutter.ActorAlign.CENTER});
        for (const [kind, n] of [['attention', sum.counts.attention],
            ['running', sum.counts.running], ['waiting', sum.counts.waiting]]) {
            if (!n)
                continue;
            const pill = hbox(`agentail-pill agentail-pill-${kind}`);
            pill.add_child(new St.Widget({style_class: 'agentail-pill-dot', y_align: Clutter.ActorAlign.CENTER}));
            pill.add_child(label(String(n), ''));
            pills.add_child(pill);
        }
        row.add_child(pills);
        return row;
    }

    _group(g) {
        const box = vbox('agentail-group', {x_expand: true});
        const head = hbox('agentail-host-row', {x_expand: true});
        head.add_child(new St.Icon({
            icon_name: g.local ? 'computer-symbolic' : 'network-server-symbolic',
            style_class: 'agentail-host-icon',
        }));
        head.add_child(label(g.title.toUpperCase(), 'agentail-host-name', {ellipsize: true, xExpand: true}));
        const state = hbox(`agentail-host-state agentail-host-${g.local ? 'local' : g.level}`,
            {y_align: Clutter.ActorAlign.CENTER});
        if (!g.local)
            state.add_child(new St.Widget({style_class: 'agentail-host-dot', y_align: Clutter.ActorAlign.CENTER}));
        const idle = !g.cards.length && !g.problem;
        state.add_child(label(idle ? `${g.stateText} · idle` : g.stateText, ''));
        head.add_child(state);
        box.add_child(head);
        if (g.problem) {
            const problem = label(`${g.problem}. Sessions below may be out of date.`, 'agentail-host-problem');
            problem.clutter_text.line_wrap = true;
            box.add_child(problem);
        }
        for (const c of g.cards)
            box.add_child(this._card(c, g));
        return box;
    }

    _card(c, g) {
        const row = hbox(`agentail-card agentail-card-${c.style}`, {x_expand: true});

        const left = vbox('agentail-card-left');
        left.add_child(new St.Bin({
            style_class: `agentail-tile agentail-tile-${c.style}`,
            child: new St.Icon({gicon: this._gicon(STATUS_ICON[c.style] ?? STATUS_ICON.stale), icon_size: 16}),
        }));
        if (KNOWN_AGENTS.includes(c.agent)) {
            left.add_child(new St.Icon({
                gicon: this._gicon(`agent-${c.agent}.png`),
                icon_size: 22,
                accessible_name: c.agentName,
                x_align: Clutter.ActorAlign.CENTER,
            }));
        } else {
            const letter = label(Array.from(c.agentName)[0]?.toUpperCase() ?? '?', 'agentail-agent-letter');
            letter.x_align = Clutter.ActorAlign.CENTER;
            left.add_child(letter);
        }
        row.add_child(left);

        const body = vbox('agentail-card-body', {x_expand: true});
        const top = hbox('', {x_expand: true});
        top.add_child(label(c.project, 'agentail-project', {ellipsize: true, xExpand: true}));
        const age = c.style === 'attention' && c.age ? `waiting ${c.age}` : c.age;
        top.add_child(label(age, 'agentail-age'));
        body.add_child(top);
        if (c.subtitle)
            body.add_child(label(c.subtitle, 'agentail-subtitle', {ellipsize: true}));

        if (c.style === 'attention' && c.toolDetail)
            body.add_child(label(c.toolDetail, 'agentail-command', {ellipsize: true}));

        const meta = hbox('agentail-meta');
        meta.add_child(label(c.statusLabel, `agentail-status agentail-status-${c.style}`));
        if (c.tool && c.style !== 'stale') {
            meta.add_child(label('·', 'agentail-sep'));
            meta.add_child(label(c.tool, 'agentail-tool'));
        }
        body.add_child(meta);
        if (c.style === 'attention') {
            const where = g.local ? 'Answer it in the terminal' : `Answer it in the terminal on ${g.alias}`;
            body.add_child(label(where, 'agentail-hint', {ellipsize: true}));
        }
        row.add_child(body);
        return row;
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
