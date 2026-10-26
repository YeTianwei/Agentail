"""The recorded frames (steps.json and PNGs) -> one looping GIF with a drawn pointer.

python3 make_gif.py FRAMES_DIR OUT.gif [OUT_WIDTH]
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

FR = Path(sys.argv[1])
steps = json.loads((FR / "steps.json").read_text())
first = Image.open(FR / steps[0]["shot"])
S = first.width / 1440  # device pixels per logical pixel

# The crop: the capsule and the panel, a margin of wallpaper around the panel.
mx = min(s["menu"][0] for s in steps if s["menu"])
X0 = int(mx - 20 * S)
X1 = first.width
Y0, Y1 = 0, int(740 * S)
OUT_W = int(sys.argv[3]) if len(sys.argv) > 3 else X1 - X0
K = OUT_W / (X1 - X0)

# A classic arrow pointer, in logical pixels, tip at (0, 0).
ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]


def pointer(img, pos, ring=0.0):
    d = ImageDraw.Draw(img, "RGBA")
    x, y = pos
    if ring:
        r = (10 + 14 * ring) * S * K
        d.ellipse(
            [x - r, y - r, x + r, y + r],
            outline=(255, 255, 255, int(200 * (1 - ring))),
            width=max(2, int(2.5 * S * K)),
            fill=(255, 255, 255, int(60 * (1 - ring))),
        )
    pts = [(x + px * S * K * 1.15, y + py * S * K * 1.15) for px, py in ARROW]
    d.polygon(
        pts, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=max(1, int(1.2 * S * K))
    )


def base(name):
    im = Image.open(FR / name).convert("RGB").crop((X0, Y0, X1, Y1))
    if K != 1:
        im = im.resize((OUT_W, int(im.height * K)), Image.LANCZOS)
    return im


def center(rect):
    x, y, w, h = rect
    return ((x + w * 0.55 - X0) * K, (y + h * 0.6 - Y0) * K)


frames, durations = [], []


def add(img, ms):
    frames.append(img)
    durations.append(int(ms))


pos = ((X1 - X0) * K * 0.55, (Y1 - Y0) * K * 0.55)
for s in steps:
    shot = base(s["shot"])
    im = shot.copy()
    pointer(im, pos)
    add(im, s["hold"])
    if "click" not in s:
        continue
    target = center(s["click"])
    n = 9
    for i in range(1, n + 1):
        t = i / n
        e = t * t * (3 - 2 * t)  # ease in and out
        p = (pos[0] + (target[0] - pos[0]) * e, pos[1] + (target[1] - pos[1]) * e)
        im = shot.copy()
        pointer(im, p)
        add(im, 30)
    pos = target
    hover = base(s["hover"])
    im = hover.copy()
    pointer(im, pos)
    add(im, 260)
    for ring in (0.25, 0.6, 0.95):
        im = hover.copy()
        pointer(im, pos, ring)
        add(im, 60)

out = sys.argv[2]
pal = [
    f.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    for f in frames
]
pal[0].save(
    out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True, disposal=1
)
print(
    out,
    len(frames),
    "frames,",
    sum(durations) / 1000,
    "s,",
    Path(out).stat().st_size // 1024,
    "KB,",
    frames[0].size,
)
