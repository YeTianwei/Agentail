// Clicks through the Agentail panel and saves a full-screen PNG of every state, with the
// rectangles needed to crop and to draw a pointer, into $GIF_OUT.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const OUT = GLib.getenv('GIF_OUT');
const SCALE = Number(GLib.getenv('GIF_SCALE') || '2');
const UUID = 'agentail@yetianwei.github.io';

const sleep = ms => new Promise(r => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    r();
    return GLib.SOURCE_REMOVE;
}));

function rect(a) {
    const [x, y] = a.get_transformed_position();
    const [w, h] = a.get_transformed_size();
    return [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
}

function find(root, names) {
    const stack = [root];
    while (stack.length) {
        const a = stack.pop();
        if (a instanceof St.Button && names.includes(a.accessible_name) && a.mapped)
            return a;
        stack.push(...a.get_children());
    }
    throw new Error(`no button ${names}`);
}

function shot(path) {
    return new Promise((resolve, reject) => {
        const file = Gio.File.new_for_path(path);
        const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
        new Shell.Screenshot().screenshot(false, stream, (o, res) => {
            try {
                o.screenshot_finish(res);
                stream.close(null);
                resolve();
            } catch (e) {
                reject(e);
            }
        });
    });
}

// Like Settings > Displays > Scale: the virtual monitor at SCALE, for a HiDPI recording.
async function setMonitorScale(scale) {
    const call = (method, args) => new Promise((resolve, reject) => {
        Gio.DBus.session.call('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
            'org.gnome.Mutter.DisplayConfig', method, args, null, Gio.DBusCallFlags.NONE, -1, null,
            (c, res) => {
                try {
                    resolve(c.call_finish(res));
                } catch (e) {
                    reject(e);
                }
            });
    });
    const [serial, monitors] = (await call('GetCurrentState', null)).deepUnpack();
    const [[connector], modes] = monitors[0];
    const mode = modes.find(m => m[6]['is-current']?.deepUnpack()) ?? modes[0];
    console.log(`demodriver: ${connector} mode ${mode[0]} scales ${mode[5]}`);
    await call('ApplyMonitorsConfig', new GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',
        [serial, 1, [[0, 0, scale, 0, true, [[connector, mode[0], {}]]]], {}]));
}

export default class DemoDriver extends Extension {
    enable() {
        console.log('demodriver: enabled');
        this._run().catch(e => console.error(`demodriver: ${e}\n${e.stack}`));
    }

    disable() {}

    async _run() {
        await setMonitorScale(SCALE);
        await sleep(2500);
        Main.overview.hide();
        await sleep(3000);
        const ind = Main.panel.statusArea[UUID];
        const steps = [];
        let n = 0;
        const menuRect = () => ind.menu.isOpen ? rect(ind.menu.actor) : null;
        const menu = () => ind.menu.actor;
        // The panel redraws itself every second (session ages), so a button is looked up again
        // right before each use: one kept from earlier may have been destroyed by then.
        const button = async names => {
            for (let i = 0; ; i++) {
                try {
                    const b = find(menu(), names);
                    const [, , w] = rect(b);
                    if (w > 0 && w < 2000)
                        return b;
                } catch (e) {
                    if (i > 12)
                        throw e;
                }
                await sleep(300);
            }
        };
        const shotName = () => `${String(n++).padStart(2, '0')}.png`;
        // A state: its picture; then, with `names`, the button hovered, pictured and clicked.
        const state = async (hold, names) => {
            await sleep(700);
            const s = {shot: shotName(), hold, capsule: rect(ind), menu: menuRect()};
            await shot(`${OUT}/${s.shot}`);
            if (names) {
                let b = names === 'capsule' ? ind : await button(names);
                s.click = rect(b);
                b.add_style_pseudo_class('hover');
                await sleep(100);
                s.hover = shotName();
                await shot(`${OUT}/${s.hover}`);
                b.remove_style_pseudo_class('hover');
                if (names === 'capsule') {
                    ind.menu.open(0);
                } else {
                    b = await button(names);
                    b.emit('clicked', 1);
                }
            }
            steps.push(s);
        };

        await state(1300, 'capsule');
        await state(2800, ['Yes']);
        await state(1500, ['Token counts']);
        await state(1700, ['7 days']);
        await state(1700, ['Model']);
        await state(1400, ['30 days']);
        await state(1400, ['Lifetime']);
        await state(1900, ['Back']);
        await state(900, ['Use light colours']);
        await state(1500, ['Settings']);
        await state(1300, ['中文']);
        await state(1500, ['返回']);
        await state(3200, null);

        GLib.file_set_contents(`${OUT}/steps.json`, JSON.stringify(steps, null, 1));
        console.log('demodriver: done');
    }
}
