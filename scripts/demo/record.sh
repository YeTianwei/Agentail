#!/bin/bash
# Record docs/images/demo.gif, the README's animation: the real GNOME Shell extension from
# this checkout in a headless GNOME Shell (Ubuntu session mode, 2x), fed made-up sessions by
# fake_daemon.py and clicked through by the driver extension in driver/; make_gif.py then
# crops the frames and draws the pointer.
#
#   scripts/demo/record.sh [work-dir]     # default: ~/.cache/agentail-demo
#
# DEMO_MODE=user records with GNOME's own shell theme instead of Ubuntu's Yaru, and DEMO_GIF
# writes the GIF elsewhere (to check a change without replacing the README's).
#
# Needs gnome-shell 46, dbus-run-session and python3-pil. Nothing touches your session: the
# shell gets its own HOME, runtime dir and session bus, and keeps its settings in a keyfile.
set -eu
DEMO=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DEMO/../.." && pwd)
SCALE=2
# The isolated environment must exist before the session bus starts: D-Bus activated
# services (dconf above all) get the bus's environment, so a bus started from your own
# environment would write these settings into your real desktop.
if [ -z "${DEMO_INNER:-}" ]; then
    WORK=$(mkdir -p "${1:-$HOME/.cache/agentail-demo}" && cd "${1:-$HOME/.cache/agentail-demo}" && pwd)
    export HOME=$WORK/home XDG_CONFIG_HOME=$WORK/home/.config XDG_DATA_HOME=$WORK/home/.local/share \
           XDG_CACHE_HOME=$WORK/home/.cache XDG_STATE_HOME=$WORK/home/.local/state
    # AF_UNIX paths are short: the runtime dir stays under /run/user.
    export XDG_RUNTIME_DIR=/run/user/$(id -u)/agentail-demo
    export DEMO_WORK=$WORK GIF_OUT=$WORK/frames GIF_SCALE=$SCALE DEMO_INNER=1
    unset DBUS_SESSION_BUS_ADDRESS WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP \
          XDG_SESSION_TYPE GNOME_SHELL_SESSION_MODE DESKTOP_SESSION
    rm -rf "$WORK/home" "$GIF_OUT" "$XDG_RUNTIME_DIR"
    mkdir -p "$XDG_DATA_HOME/gnome-shell/extensions" "$XDG_CONFIG_HOME" "$GIF_OUT" "$HOME/Desktop"
    mkdir -m 700 -p "$XDG_RUNTIME_DIR"
    exec dbus-run-session -- "$0" "$@"
fi
[ "$HOME" = "$DEMO_WORK/home" ] || { echo "not isolated" >&2; exit 1; }
# Settings live in a keyfile inside the isolated HOME, read in-process: no dconf at all.
export GSETTINGS_BACKEND=keyfile LANG=en_US.UTF-8 LANGUAGE=en_US:en
ext=$XDG_DATA_HOME/gnome-shell/extensions
ln -s "$REPO/src/agentail/resources/gnome-extension/agentail@yetianwei.github.io" "$ext/"
ln -s "$DEMO/driver" "$ext/demodriver@agentail"
gsettings set org.gnome.shell enabled-extensions "['agentail@yetianwei.github.io', 'demodriver@agentail']"
gsettings set org.gnome.shell disable-extension-version-validation true
gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'
gsettings set org.gnome.desktop.background picture-uri ''
gsettings set org.gnome.desktop.background picture-uri-dark ''
gsettings set org.gnome.desktop.background picture-options 'none'
gsettings set org.gnome.desktop.background color-shading-type 'vertical'
gsettings set org.gnome.desktop.background primary-color '#3a3f4b'
gsettings set org.gnome.desktop.background secondary-color '#1f2229'

python3 -u "$DEMO/fake_daemon.py" > "$DEMO_WORK/daemon.log" 2>&1 &
dpid=$!
sleep 1
gnome-shell --mode="${DEMO_MODE:-ubuntu}" --headless --virtual-monitor $((1440 * SCALE))x$((880 * SCALE)) --no-x11 \
    > "$DEMO_WORK/shell.log" 2>&1 &
spid=$!
for _ in $(seq 1 120); do
    [ -f "$GIF_OUT/steps.json" ] && break
    sleep 1
done
kill "$spid" "$dpid" 2>/dev/null || true
wait "$spid" 2>/dev/null || true
rm -rf "$XDG_RUNTIME_DIR"
[ -f "$GIF_OUT/steps.json" ] || { echo "no recording; see $DEMO_WORK/shell.log" >&2; exit 1; }
python3 "$DEMO/make_gif.py" "$GIF_OUT" "${DEMO_GIF:-$REPO/docs/images/demo.gif}"
