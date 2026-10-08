#!/bin/sh
# Build dist/agentail_<version>.deb from the source tree. No debhelper needed:
# the package is pure Python plus a GNOME Shell extension and a systemd user unit.
#
#   packaging/build-deb.sh [output-dir]
#
# Layout of the package:
#   /usr/lib/python3/dist-packages/agentail/        the Python package (incl. resources)
#   /usr/bin/agentail                               launcher
#   /usr/share/gnome-shell/extensions/<uuid>        symlink into resources/gnome-extension
#   /usr/lib/systemd/user/agentail.service          daemon unit, enabled globally in postinst
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
out=${1:-"$root/dist"}
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/src/agentail/__init__.py")
[ -n "$version" ] || { echo "cannot read __version__" >&2; exit 1; }
uuid=agentail@yetianwei.github.io
name=agentail_${version}

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
pkg=$stage/$name
lib=$pkg/usr/lib/python3/dist-packages

install -d -m 755 "$lib" "$pkg/usr/bin" "$pkg/usr/share/gnome-shell/extensions" \
    "$pkg/usr/lib/systemd/user" "$pkg/usr/share/doc/agentail" "$pkg/DEBIAN"

# Python package without caches (py3compile builds them in postinst).
cp -r "$root/src/agentail" "$lib/agentail"
find "$lib" -name __pycache__ -type d -prune -exec rm -rf {} +
# Stamp the extension so a panel still running older code can tell it was upgraded.
ext="$lib/agentail/resources/gnome-extension/$uuid"
build="$version+$(date +%s)"
python3 - "$ext/metadata.json" "$build" <<'PY'
import json, sys
path, build = sys.argv[1], sys.argv[2]
meta = json.load(open(path))
meta["version-name"] = build
open(path, "w").write(json.dumps(meta, indent=2) + "\n")
PY
sed -i "s/^const BUILD = 'dev';$/const BUILD = '$build';/" "$ext/extension.js"
grep -q "^const BUILD = '$build';$" "$ext/extension.js" || { echo "cannot stamp extension.js" >&2; exit 1; }

find "$lib" -type d -exec chmod 755 {} +
find "$lib" -type f -exec chmod 644 {} +

cat > "$pkg/usr/bin/agentail" <<'LAUNCHER'
#!/usr/bin/python3
from agentail.cli import main

raise SystemExit(main())
LAUNCHER
chmod 755 "$pkg/usr/bin/agentail"

ln -s "../../../lib/python3/dist-packages/agentail/resources/gnome-extension/$uuid" \
    "$pkg/usr/share/gnome-shell/extensions/$uuid"
install -m 644 "$root/packaging/agentail.service" "$pkg/usr/lib/systemd/user/agentail.service"

install -m 644 "$root/README.md" "$pkg/usr/share/doc/agentail/README.md"
gzip -9n -c "$root/CHANGELOG.md" > "$pkg/usr/share/doc/agentail/changelog.gz"
chmod 644 "$pkg/usr/share/doc/agentail/changelog.gz"
install -m 644 "$root/packaging/copyright" "$pkg/usr/share/doc/agentail/copyright"

for script in postinst prerm postrm; do
    install -m 755 "$root/packaging/$script" "$pkg/DEBIAN/$script"
done
size=$(du -sk --exclude=DEBIAN "$pkg" | cut -f1)
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" "$root/packaging/control.in" > "$pkg/DEBIAN/control"

mkdir -p "$out"
dpkg-deb --root-owner-group --build "$pkg" "$out/$name.deb" >/dev/null
echo "$out/$name.deb"
