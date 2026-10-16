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

import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {_, LANGUAGES, resolveLanguage, setLanguage} from './i18n.js';
import * as M from './model.js';

const RECONNECT_MAX_S = 10;
const AGE_REFRESH_S = 5;
// Agents with an icon and a chip colour; tests keep this in sync with agentail.agents.
const KNOWN_AGENTS = Object.keys(M.AGENT_NAME);
const AGENT_ICON_SIZE = 16;
const ADD_HOST_TIMEOUT_S = 150;
const MAX_CHIPS = 40;
const USAGE_BAR_WIDTH = 120;
// Replaced by packaging/build-deb.sh with the same stamp it writes into metadata.json's
// "version-name". GNOME Shell keeps an extension's code in memory until it restarts, so
// after an upgrade the disk stamp differs from this one and the panel says so.
const BUILD = 'dev';
const SHELL_MAJOR = Number.parseInt(Config.PACKAGE_VERSION, 10);
// GNOME 48 added St.BoxLayout:orientation and deprecated :vertical; 45-47 only have :vertical.
const VERTICAL = St.BoxLayout.find_property('orientation')
    ? {orientation: Clutter.Orientation.VERTICAL}
    : {vertical: true};

// An onError callback for M.guard: log with the stack and carry on.
function logFailure(what) {
    return e => console.error(`agentail: ${what}: ${e}\n${e?.stack ?? ''}`);
}

// $XDG_RUNTIME_DIR/agentail/ui.sock. Without the variable the daemon uses /run/user/<uid>
// when logind made it (agentail.paths.runtime_dir); GLib would fall back to ~/.cache instead.
function uiSocketPath() {
    let base = GLib.getenv('XDG_RUNTIME_DIR');
    if (!base) {
        const runUser = `/run/user/${Gio.Credentials.new().get_unix_user()}`;
        base = GLib.file_test(runUser, GLib.FileTest.IS_DIR) ? runUser : GLib.get_user_runtime_dir();
    }
    return GLib.build_filenamev([base, 'agentail', 'ui.sock']);
}

// Panel preferences (theme, language, Usage expanded), kept in ~/.config/agentail/panel.json.
// Only this extension writes the file; anything unreadable falls back to the defaults. A null
// theme or language follows the desktop.
const PREFS_DEFAULT = {theme: null, language: null, usage_open: true};

function prefsPath() {
    return GLib.build_filenamev([GLib.get_user_config_dir(), 'agentail', 'panel.json']);
}

function loadPrefs() {
    try {
        const [, bytes] = GLib.file_get_contents(prefsPath());
        const data = JSON.parse(new TextDecoder().decode(bytes));
        return {
            theme: data.theme === 'light' || data.theme === 'dark' ? data.theme : null,
            language: LANGUAGES.includes(data.language) ? data.language : null,
            usage_open: data.usage_open !== false,
        };
    } catch {
        return {...PREFS_DEFAULT};
    }
}

function savePrefs(prefs) {
    try {
        GLib.mkdir_with_parents(GLib.path_get_dirname(prefsPath()), 0o700);
        GLib.file_set_contents(prefsPath(), JSON.stringify(prefs));
    } catch (e) {
        console.warn(`agentail: cannot save ${prefsPath()}: ${e.message}`);
    }
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
        if (!this._stream)
            return; // stopped while a message was being handled
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
                M.guard(this._onDisconnect, logFailure('cannot show the disconnect'));
                this._retry();
                return;
            }
            // Never lets an exception out: one bad message must not end the read loop.
            M.dispatchLine(line, this._onMessage, logFailure('cannot handle a daemon message'));
            this._read();
        });
    }

    // Tell the daemon something: {type: 'refresh_usage'} (the panel was opened) or
    // {type: 'dismiss_session', key} (the user removed a card).
    send(obj) {
        try {
            this._conn?.get_output_stream().write_all(`${JSON.stringify(obj)}\n`, null);
        } catch (e) {
            console.warn(`agentail: cannot send to the daemon: ${e.message}`);
        }
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
    return new St.BoxLayout({style_class: styleClass, ...VERTICAL, ...props});
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
        // 'main' or 'settings' (the gear, top right); back to 'main' when the menu closes.
        this._view = 'main';
        this._settingsError = '';
        // Switches just flipped in the settings that the daemon has not reported yet.
        this._pending = {};

        // The "island": a dark capsule inside the panel button. Its content is rebuilt
        // by _renderTopBar() whenever the summary changes.
        this._capsule = hbox('agentail-capsule', {y_align: Clutter.ActorAlign.CENTER});
        this._topSig = '';
        this.add_child(this._capsule);

        this.menu.actor.add_style_class_name('agentail-menu');
        this._prefs = loadPrefs();
        setLanguage(resolveLanguage(this._prefs.language, GLib.get_language_names()));
        this._interface = new Gio.Settings({schema_id: 'org.gnome.desktop.interface'});
        this._schemeId = this._interface.connect('changed::color-scheme', () => this._applyTheme());
        this._applyTheme();
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
                // The daemon asks Codex for its current limits only when someone looks.
                this._client.send({type: 'refresh_usage'});
                this._renderPanel();
                this._ageTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, AGE_REFRESH_S, () => {
                    if (this._add.mode !== 'picking')
                        this._renderPanel();
                    return GLib.SOURCE_CONTINUE;
                });
            } else {
                // Adding or removing a server goes on while the menu is closed: reopening shows
                // its progress, or its result, in the settings where it started.
                if (this._add.mode !== 'running')
                    this._view = 'main';
                this._settingsError = '';
                if (['picking', 'confirm', 'result'].includes(this._add.mode))
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
        this._pending = M.settlePending(this._state, this._pending);
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
            M.guard(() => {
                this._renderTopBar();
                if (this.menu.isOpen && this._add.mode !== 'picking')
                    this._renderPanel();  // a rebuild would drop what is being typed
            }, logFailure('cannot render the top bar'));
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
                const text = attention > 1 ? _('{n} need you', {n: attention})
                    : _('{project} needs you', {project: sum.lead || _('Agent')});
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
            this._panel.add_child(label(_('Agentail could not draw this panel. See the GNOME Shell log.'), 'agentail-empty-text'));
        }
    }

    _buildPanel() {
        const now = Date.now() / 1000;
        const st = this._state;
        this._panel.destroy_all_children();

        if (this._outdated())
            this._panel.add_child(this._updateBanner());
        if (this._view === 'settings') {
            this._panel.add_child(this._settingsPage());
            return;
        }
        if (!st.connected) {
            this._panel.add_child(this._titleRow());
            this._panel.add_child(this._offline());
            return;
        }
        const groups = M.agentGroups(st, now);
        const content = vbox('agentail-groups', {x_expand: true});
        if (!groups.length) {
            content.add_child(this._titleRow());
            content.add_child(this._empty());
        }
        // Settings and the light/dark switch sit top right, on the first agent's row.
        groups.forEach((g, i) => content.add_child(this._agentGroup(g, i === 0)));
        // Servers are managed in the settings; here only the ones that cannot be reached.
        const down = M.hostRows(st).filter(h => h.down);
        if (down.length)
            content.add_child(this._serverProblems(down));
        const usage = M.usageCards(st, now);
        const hint = M.usageHint(st);
        if (usage.length || hint)
            content.add_child(this._usage(usage, hint));

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

    // A top row with the buttons, for when there is no agent row to carry them.
    _titleRow() {
        const row = hbox('agentail-head', {x_expand: true});
        row.add_child(label('Agentail', 'agentail-section', {xExpand: true}));
        row.add_child(this._headButtons());
        return row;
    }

    // Top right of the panel: settings, then the light/dark switch.
    _headButtons() {
        const box = hbox('agentail-head-buttons', {y_align: Clutter.ActorAlign.CENTER});
        box.add_child(this._iconButton('emblem-system-symbolic', _('Settings'), () => {
            this._view = 'settings';
            this._renderPanel();
        }));
        box.add_child(this._themeButton());
        return box;
    }

    // `icon`: a themed icon name, or a Gio.Icon of our own.
    _iconButton(icon, accessibleName, onClick) {
        const b = new St.Button({
            style_class: 'agentail-btn agentail-btn-icon',
            reactive: true,
            can_focus: true,
            track_hover: true,
            accessible_name: accessibleName,
            child: new St.Icon(typeof icon === 'string'
                ? {icon_name: icon, icon_size: 14} : {gicon: icon, icon_size: 14}),
        });
        b.connect('clicked', onClick);
        return b;
    }

    // -- theme -------------------------------------------------------------------------------

    _theme() {
        return M.resolveTheme(this._prefs.theme, this._interface.get_string('color-scheme'));
    }

    _applyTheme() {
        if (this._theme() === 'light')
            this.menu.actor.add_style_class_name('agentail-light');
        else
            this.menu.actor.remove_style_class_name('agentail-light');
    }

    _themeButton() {
        const light = this._theme() === 'light';
        // A half-filled circle in both themes: a sun next to the settings gear looked alike.
        return this._iconButton(
            this._gicon('theme-symbolic.svg'),
            light ? _('Use dark colours') : _('Use light colours'),
            () => {
                this._prefs.theme = light ? 'dark' : 'light';
                savePrefs(this._prefs);
                this._applyTheme();
                this._renderPanel();
            });
    }

    // -- settings ------------------------------------------------------------------------------

    _settingsPage() {
        const page = vbox('agentail-groups', {x_expand: true});
        const head = hbox('agentail-head', {x_expand: true});
        head.add_child(this._iconButton('go-previous-symbolic', _('Back'), () => {
            this._view = 'main';
            this._settingsError = '';
            this._renderPanel();
        }));
        head.add_child(label(_('Settings'), 'agentail-section', {xExpand: true}));
        head.add_child(this._themeButton());
        page.add_child(head);
        page.add_child(this._languageGroup());
        page.add_child(this._notificationGroup());

        const settings = M.usageSettings(this._state, this._pending, Date.now() / 1000);
        // One group: the Usage switch, and under a divider (while it is on) Claude's two,
        // aligned with it.
        const usage = vbox('agentail-group', {x_expand: true});
        usage.add_child(label(_('Usage limits'), 'agentail-section'));
        const card = vbox('agentail-card agentail-settings', {x_expand: true});
        if (!settings.available) {
            const off = label(_('The agentail daemon is not running, or is too old for these settings.'),
                'agentail-add-note');
            off.clutter_text.line_wrap = true;
            card.add_child(off);
        }
        card.add_child(this._switchRow(settings.usage));
        if (settings.showClaude) {
            card.add_child(new St.Widget({style_class: 'agentail-settings-divider', x_expand: true}));
            const claude = vbox('agentail-settings-sub', {x_expand: true});
            claude.add_child(label(_("Ask Anthropic for Claude's limits"), 'agentail-settings-subtitle'));
            const intro = label(settings.intro, 'agentail-add-note agentail-settings-intro');
            intro.clutter_text.line_wrap = true;
            claude.add_child(intro);
            for (const r of settings.rows)
                claude.add_child(this._switchRow(r));
            card.add_child(claude);
        }
        usage.add_child(card);
        page.add_child(usage);
        if (this._settingsError) {
            const err = label(this._settingsError, 'agentail-add-result agentail-add-fail');
            err.clutter_text.line_wrap = true;
            page.add_child(err);
        }
        page.add_child(this._serversGroup(M.hostRows(this._state)));
        return page;
    }

    // System / English / 中文: the panel and its notifications. Each language is named in
    // itself, so the way back is readable whatever is shown.
    _languageGroup() {
        const group = vbox('agentail-group', {x_expand: true});
        group.add_child(label(_('Language'), 'agentail-section'));
        const row = hbox('agentail-lang-row', {x_expand: true});
        for (const [choice, name] of [[null, _('System')], ['en', 'English'], ['zh', '中文']]) {
            const on = this._prefs.language === choice;
            const text = label(name, '');
            text.x_align = Clutter.ActorAlign.CENTER;
            const b = new St.Button({
                style_class: `agentail-btn agentail-btn-lang${on ? ' agentail-btn-lang-on' : ''}`,
                reactive: true,
                can_focus: true,
                track_hover: true,
                x_expand: true,
                accessible_name: name,
                child: text,
            });
            b.connect('clicked', () => this._setLanguage(choice));
            row.add_child(b);
        }
        const card = vbox('agentail-card agentail-settings', {x_expand: true});
        card.add_child(row);
        group.add_child(card);
        return group;
    }

    // When it's your turn / when an agent needs you / with the prompt and the command.
    _notificationGroup() {
        const settings = M.notificationSettings(this._state, this._pending);
        const group = vbox('agentail-group', {x_expand: true});
        group.add_child(label(_('Notifications'), 'agentail-section'));
        const card = vbox('agentail-card agentail-settings', {x_expand: true});
        if (!settings.available) {
            const off = label(_('The agentail daemon is not running, or is too old for this setting.'),
                'agentail-add-note');
            off.clutter_text.line_wrap = true;
            card.add_child(off);
        }
        for (const r of settings.rows)
            card.add_child(this._switchRow(r));
        group.add_child(card);
        return group;
    }

    _setLanguage(choice) {
        this._prefs.language = choice;
        savePrefs(this._prefs);
        setLanguage(resolveLanguage(choice, GLib.get_language_names()));
        this._topSig = '';  // the capsule's text is in the old language
        this._renderTopBar();
        this._renderPanel();
    }

    // One switch: a title and what it does, the switch on the right; the whole row toggles.
    _switchRow(r) {
        const row = hbox('agentail-switch-row', {x_expand: true});
        const text = vbox('agentail-switch-text', {x_expand: true});
        text.add_child(label(r.title, 'agentail-switch-title'));
        for (const note of r.notes) {
            const l = label(note, 'agentail-add-note');
            l.clutter_text.line_wrap = true;
            text.add_child(l);
        }
        const status = r.saving ? _('Saving…') : r.status;
        if (status)
            text.add_child(label(status, 'agentail-switch-status'));
        row.add_child(text);
        const sw = new PopupMenu.Switch(r.on);
        sw.y_align = Clutter.ActorAlign.START;
        row.add_child(sw);
        const button = new St.Button({
            style_class: 'agentail-switch-button',
            reactive: r.enabled,
            can_focus: r.enabled,
            track_hover: r.enabled,
            x_expand: true,
            accessible_name: r.title,
            child: row,
        });
        if (!r.enabled)
            button.opacity = 128;
        button.connect('clicked', () => this._setSwitch(r.id, !r.on));
        return button;
    }

    _agentGroup(g, withHeadButtons = false) {
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
        if (withHeadButtons) {
            const head = hbox('agentail-head', {x_expand: true});
            head.add_child(chip);
            head.add_child(new St.Widget({x_expand: true}));
            head.add_child(this._headButtons());
            box.add_child(head);
        } else {
            box.add_child(chip);
        }
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
        const age = c.style === 'attention' && c.age ? _('for {age}', {age: c.age}) : c.age;
        top.add_child(label(c.statusLabel, `agentail-status agentail-status-${c.style}`));
        if (age)
            top.add_child(label(age, 'agentail-age'));
        box.add_child(top);

        const second = hbox('agentail-card-second', {x_expand: true});
        const line = [c.host, c.subtitle].filter(x => x).join(' · ');
        second.add_child(label(line, 'agentail-subtitle', {ellipsize: true, xExpand: true}));
        if (c.tool && (c.style === 'running' || c.style === 'attention'))
            second.add_child(label(c.tool, 'agentail-tool'));
        // The daemon forgets the session until its next event.
        if (c.key) {
            const key = c.key;
            second.add_child(this._trashButton(_('Remove from the list'),
                () => this._client.send({type: 'dismiss_session', key})));
        }
        box.add_child(second);

        if (c.style === 'attention') {
            if (c.toolDetail)
                box.add_child(label(c.toolDetail, 'agentail-command', {ellipsize: true}));
            const where = c.local ? _('Answer it in the terminal')
                : _('Answer it in the terminal on {host}', {host: c.host});
            box.add_child(label(where, 'agentail-hint', {ellipsize: true}));
        }
        return box;
    }

    // A small trash can: removes a session card (under its age) or a server (end of its row).
    _trashButton(accessibleName, onClick) {
        const b = new St.Button({
            style_class: 'agentail-btn-trash',
            reactive: true,
            can_focus: true,
            track_hover: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: accessibleName,
            // Our own: themes' trash cans are drawn with 1px lines and blur at this size.
            child: new St.Icon({gicon: this._gicon('trash-symbolic.svg'), icon_size: 14}),
        });
        b.connect('clicked', onClick);
        return b;
    }

    _usage(cards, hint) {
        const box = vbox('agentail-group', {x_expand: true});
        const open = this._prefs.usage_open;
        const head = hbox('agentail-servers-head', {x_expand: true});
        head.add_child(label(_('Usage'), 'agentail-section', {xExpand: true}));
        head.add_child(new St.Icon({
            icon_name: open ? 'pan-down-symbolic' : 'pan-end-symbolic',
            icon_size: 14,
            style_class: 'agentail-chevron',
        }));
        const toggle = new St.Button({
            style_class: 'agentail-servers-toggle',
            reactive: true,
            can_focus: true,
            track_hover: true,
            x_expand: true,
            accessible_name: open ? _('Hide usage') : _('Show usage'),
            child: head,
        });
        toggle.connect('clicked', () => {
            this._prefs.usage_open = !open;
            savePrefs(this._prefs);
            this._renderPanel();
        });
        box.add_child(toggle);
        if (open && hint) {
            const l = label(hint, 'agentail-usage-hint');
            l.clutter_text.line_wrap = true;
            box.add_child(l);
        }
        if (open)
            cards.forEach(c => box.add_child(this._usageCard(c)));
        return box;
    }

    _usageCard(c) {
        const box = vbox(`agentail-card agentail-usage${c.stale ? ' agentail-card-stale' : ''}`,
            {x_expand: true});
        const top = hbox('agentail-card-top', {x_expand: true});
        if (KNOWN_AGENTS.includes(c.agent)) {
            top.add_child(new St.Icon({
                gicon: this._gicon(`agent-${c.agent}.png`),
                icon_size: 16,
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        top.add_child(label(c.plan ? `${c.name} · ${c.plan}` : c.name, 'agentail-project',
            {ellipsize: true, xExpand: true}));
        const where = c.sources || (c.via ? _('via {host}', {host: c.via}) : '');
        const when = [where, c.updatedText].filter(x => x).join(' · ');
        if (when)
            top.add_child(label(when, 'agentail-age'));
        box.add_child(top);
        for (const r of c.rows) {
            const row = hbox('agentail-usage-row', {x_expand: true});
            // A minimum, not a width: the estimate must never cut a label short.
            const name = label(r.label, 'agentail-usage-label');
            name.set_style(`min-width: ${c.labelWidth}px;`);
            row.add_child(name);
            if (r.bar !== false) {  // a balance has no bar: label, then its text
                const track = hbox('agentail-usage-track', {y_align: Clutter.ActorAlign.CENTER});
                track.set_width(USAGE_BAR_WIDTH);
                if (r.fraction > 0) {
                    track.add_child(new St.Widget({
                        style_class: `agentail-usage-fill agentail-usage-${r.level}`,
                        width: Math.max(3, Math.round(USAGE_BAR_WIDTH * r.fraction)),
                        y_expand: true,
                    }));
                }
                row.add_child(track);
            }
            if (r.percent !== null)
                row.add_child(label(`${r.percent}%`, `agentail-usage-percent agentail-usage-text-${r.level}`));
            row.add_child(label(r.resetText, 'agentail-usage-reset', {ellipsize: true, xExpand: true}));
            box.add_child(row);
        }
        if (c.sourceText)
            box.add_child(label(c.sourceText, 'agentail-usage-source', {ellipsize: true}));
        return box;
    }

    // Runs `agentail usage on|off`, `agentail usage-api on|off [--servers]` or
    // `agentail notifications on|off --<kind>` (argv list, no shell). The switch shows the new
    // value at once; the daemon confirms it within seconds (M.settlePending).
    _setSwitch(id, on) {
        const value = on ? 'on' : 'off';
        const argv = {
            usage: ['usage', value],
            local: ['usage-api', value],
            servers: ['usage-api', value, '--servers'],
            turn_done: ['notifications', value, '--turn-done'],
            attention: ['notifications', value, '--attention'],
            details: ['notifications', value, '--details'],
        }[id];
        if (!argv)
            return;
        const fail = () => {
            delete this._pending[id];
            this._settingsError = _('Could not save that. Run agentail {args} in a terminal.', {args: argv.join(' ')});
            this._renderPanel();
        };
        this._settingsError = '';
        this._pending = {...this._pending, [id]: on};
        this._renderPanel();
        const bin = this._agentailBinary();
        if (!bin) {
            fail();
            return;
        }
        try {
            const proc = Gio.Subprocess.new([bin, ...argv],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            proc.wait_check_async(null, (p, res) => {
                try {
                    p.wait_check_finish(res);
                } catch {
                    fail();
                }
            });
        } catch {
            fail();
        }
    }

    // On the main page, one card per server that cannot be reached, with the reason.
    _serverProblems(rows) {
        const box = vbox('agentail-group', {x_expand: true});
        for (const h of rows) {
            const card = vbox('agentail-host-problem', {x_expand: true});
            card.add_child(label(_('{host} is unreachable', {host: h.title}), 'agentail-host-problem-title',
                {ellipsize: true}));
            const why = label(h.problem ? _('{problem}. Sessions may be out of date.', {problem: h.problem})
                : _('Sessions may be out of date.'), '');
            why.clutter_text.line_wrap = true;
            card.add_child(why);
            box.add_child(card);
        }
        return box;
    }

    // In the settings: every server with its state, a trash can each, and Add server.
    _serversGroup(rows) {
        const box = vbox('agentail-group', {x_expand: true});
        box.add_child(label(_('Servers'), 'agentail-section'));
        const card = vbox('agentail-card agentail-hosts', {x_expand: true});
        for (const h of rows) {
            const row = hbox('agentail-host-row', {x_expand: true});
            row.add_child(new St.Widget({
                style_class: `agentail-dot agentail-host-${h.level}`,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            row.add_child(label(h.title, 'agentail-host-name', {ellipsize: true, xExpand: true}));
            const detail = h.level === 'ok' && h.sessions
                ? _('{state} · {n} active', {state: h.stateText, n: h.sessions}) : h.stateText;
            row.add_child(label(detail, `agentail-host-state agentail-host-text-${h.level}`));
            if (this._add.mode === 'idle') {
                row.add_child(this._trashButton(_('Remove {alias}', {alias: h.alias}),
                    () => this._setAdd({mode: 'confirm', alias: h.alias})));
            }
            card.add_child(row);
            if (h.problem) {
                const problem = label(_('{problem}. Sessions may be out of date.', {problem: h.problem}),
                    'agentail-host-problem');
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
            box.add_child(this._button(_('＋  Add server'), 'agentail-btn-add', () => this._openPicker()));
        } else if (a.mode === 'picking') {
            box.add_child(label(_('Pick a server from your SSH config'), 'agentail-add-title'));
            if (a.candidates === null) {
                box.add_child(label(_('Looking…'), 'agentail-add-note'));
            } else {
                const chips = vbox('agentail-chips', {x_expand: true});
                for (const names of M.chipRows(a.candidates.slice(0, MAX_CHIPS))) {
                    const line = hbox('agentail-chip-row');
                    for (const alias of names)
                        line.add_child(this._button(alias, 'agentail-btn-pick', () => this._runAddHost(alias)));
                    chips.add_child(line);
                }
                if (a.candidates.length)
                    box.add_child(chips);
                if (a.candidates.length > MAX_CHIPS)
                    box.add_child(label(_('and {n} more: type the name below', {n: a.candidates.length - MAX_CHIPS}), 'agentail-add-note'));
                if (!a.candidates.length)
                    box.add_child(label(_('No unused hosts in ~/.ssh/config. Type its ssh name below.'), 'agentail-add-note'));
            }
            const entry = new St.Entry({
                style_class: 'agentail-entry',
                hint_text: _('or type a name, then Enter'),
                can_focus: true,
                x_expand: true,
            });
            entry.clutter_text.connect('activate', () => this._runAddHost(entry.get_text().trim()));
            box.add_child(entry);
            box.add_child(this._button(_('Cancel'), 'agentail-btn-quiet', () => this._setAdd({})));
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                entry.grab_key_focus();
                return GLib.SOURCE_REMOVE;
            });
        } else if (a.mode === 'confirm') {
            const msg = label(_("Remove {alias}? Agentail stops watching it and takes this desktop out of the server's Claude and Codex settings (they go back to the original once nothing else uses them). The server must be reachable.", {alias: a.alias}), 'agentail-add-title');
            msg.clutter_text.line_wrap = true;
            box.add_child(msg);
            const row = hbox('agentail-add-row');
            row.add_child(this._button(_('Remove'), 'agentail-btn-danger', () => this._runRemoveHost(a.alias, false)));
            row.add_child(this._button(_('Cancel'), 'agentail-btn-quiet', () => this._setAdd({})));
            box.add_child(row);
        } else if (a.mode === 'running') {
            const row = hbox('agentail-add-row');
            const spinner = new St.Icon({gicon: this._gicon('spinner.svg'), icon_size: 16});
            spinner.set_pivot_point(0.5, 0.5);
            spinner.ease({rotation_angle_z: 360, duration: 1000, mode: Clutter.AnimationMode.LINEAR, repeatCount: -1});
            row.add_child(spinner);
            const doing = a.action === 'add' ? _('Adding {alias}…', {alias: a.alias})
                : _('Removing {alias}…', {alias: a.alias});
            row.add_child(label(doing, 'agentail-add-title'));
            box.add_child(row);
            box.add_child(label(_('This talks to the server over SSH and can take a few seconds.'), 'agentail-add-note'));
        } else {
            const msg = label(a.message, `agentail-add-result agentail-add-${a.ok ? 'ok' : 'fail'}`);
            msg.clutter_text.line_wrap = true;
            box.add_child(msg);
            const row = hbox('agentail-add-row');
            if (!a.ok && a.action === 'remove') {
                // The server could not be reached: forget it here without touching it.
                row.add_child(this._button(_('Forget it anyway'), 'agentail-btn-danger', () => this._runRemoveHost(a.alias, true)));
            } else if (!a.ok) {
                row.add_child(this._button(_('Try again'), 'agentail-btn-add', () => this._openPicker()));
            }
            row.add_child(this._button(_('Close'), 'agentail-btn-quiet', () => this._setAdd({})));
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
        this._runTool(alias, 'add', ['add-host', alias]);
    }

    _runRemoveHost(alias, localOnly) {
        this._runTool(alias, 'remove',
            localOnly ? ['remove-host', '--local-only', alias] : ['remove-host', alias]);
    }

    // Runs `agentail <args>` (argv list, no shell) and shows its last line in the panel.
    _runTool(alias, action, args) {
        const fail = message => this._setAdd({mode: 'result', ok: false, action, alias, message});
        if (!M.validAlias(alias)) {
            fail(_('That is not a valid ssh host name.'));
            return;
        }
        const bin = this._agentailBinary();
        if (!bin) {
            fail(_('The agentail command was not found. Run `agentail {command}` in a terminal.', {command: args[0]}));
            return;
        }
        this._setAdd({mode: 'running', alias, action});
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
        const text = label(_('Agentail was updated. Press Alt+F2, type r, Enter (X11), or log out and back in to load the new panel.'),
            'agentail-update');
        text.clutter_text.line_wrap = true;
        return text;
    }

    // The mark (a shooting star; grey while the daemon is away) over a centred title.
    _mark(icon) {
        return new St.Icon({
            gicon: this._gicon(icon),
            icon_size: 40,
            style_class: 'agentail-empty-icon',
            x_align: Clutter.ActorAlign.CENTER,
        });
    }

    _emptyTitle(text) {
        // text-align only centres wrapped lines: a one-line label is centred as an actor.
        const l = label(text, 'agentail-empty-title');
        l.x_align = Clutter.ActorAlign.CENTER;
        return l;
    }

    _empty() {
        const box = vbox('agentail-empty', {x_expand: true});
        box.add_child(this._mark('agentail-mark.svg'));
        box.add_child(this._emptyTitle(_('All quiet')));
        const text = label(_('Start Claude Code or Codex in a terminal and it shows up here.'), 'agentail-empty-text');
        text.clutter_text.line_wrap = true;
        box.add_child(text);
        // Servers live in the settings now: say where, until there is one.
        if (![...this._state.hosts.keys()].some(alias => alias !== 'local')) {
            const servers = label(_('Agents on SSH servers too? Add them in Settings (⚙).'), 'agentail-empty-text');
            servers.clutter_text.line_wrap = true;
            box.add_child(servers);
        }
        return box;
    }

    _offline() {
        const box = vbox('agentail-empty', {x_expand: true});
        box.add_child(this._mark('agentail-mark-offline.svg'));
        box.add_child(this._emptyTitle(_('The agentail daemon is not running')));
        const retry = label(_('Retrying every few seconds. Start it with:'), 'agentail-empty-text');
        retry.x_align = Clutter.ActorAlign.CENTER;
        box.add_child(retry);
        const cmd = label('agentail daemon', 'agentail-command');
        cmd.x_align = Clutter.ActorAlign.CENTER;
        box.add_child(cmd);
        return box;
    }

    // -- notifications -------------------------------------------------------------------------

    // A notification API that differs on this shell must not break the client: log and go on.
    _notify(n) {
        M.guard(() => this._showNotification(n), logFailure('cannot show a notification'));
    }

    // GNOME 46 made MessageTray's Source and Notification take one params object (and
    // replaced showNotification with addNotification); 45 takes positional arguments.
    _showNotification(n) {
        if (!this._source) {
            const icon = this._gicon('agentail-mark-small.svg');  // no sparkles at 16px
            let source;
            if (SHELL_MAJOR >= 46) {
                source = new MessageTray.Source({title: 'Agentail', icon});
            } else {
                source = new MessageTray.Source('Agentail', 'dialog-information-symbolic');
                source.getIcon = () => icon; // 45 draws the source icon from getIcon()
            }
            source.connect('destroy', () => {
                this._source = null;
            });
            Main.messageTray.add(source);
            this._source = source;
        }
        const gicon = KNOWN_AGENTS.includes(n.agent) ? this._gicon(`agent-${n.agent}.png`) : null;
        const urgency = n.urgent ? MessageTray.Urgency.HIGH : MessageTray.Urgency.NORMAL;
        let notification;
        if (SHELL_MAJOR >= 46) {
            notification = new MessageTray.Notification({
                source: this._source,
                title: n.title,
                body: n.body, // use-body-markup stays false: payload text is never markup
                gicon,
                urgency,
            });
        } else {
            // The 45 banner is plain text unless the params ask for markup; they never do.
            notification = new MessageTray.Notification(this._source, n.title, n.body, {gicon});
            notification.setUrgency(urgency);
        }
        notification.connect('activated', () => this.menu.open());
        if (SHELL_MAJOR >= 46)
            this._source.addNotification(notification);
        else
            this._source.showNotification(notification);
    }

    destroy() {
        this._client.stop();
        if (this._schemeId)
            this._interface.disconnect(this._schemeId);
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
