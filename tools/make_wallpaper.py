"""Draw `packaging/icon/wallpaper.svg` and `wallpaper.png`, a 3840x2160 desktop wallpaper
for the instrument PCs grown from the icon.

Run:  uv run tools/make_wallpaper.py
      uv run tools/make_wallpaper.py --open    # and show the PNG

The icon's two gears sit on the left, drawn as clock wheels with curved crossings, and a
timing diagram runs out of them to the right: a gate pulse, a trap-and-release, a 7-bit
pseudo-random multiplexing sequence, a fast clock and a trigger, under a trace of
arrival-time peaks. No wordmark: the picture is the mark.

The gears are drawn from a module and a tooth count rather than scaled up from
`clockwork.svg`, whose teeth are too coarse to hold at this size, and the second gear is
phased so its teeth sit in the first one's gaps. `_meshes_cleanly` refuses to write a
drawing whose teeth overlap, which a change of tooth shape can do silently.

The SVG uses blur filters and masks, which Qt's SVG Tiny renderer ignores, so the PNG is
rendered by headless Microsoft Edge, present on every Windows 11 machine; without it the
SVG is still written and the PNG step is reported as skipped. The palette is the icon's
eight-stop viridis ramp.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TARGET_SVG = os.path.join(ROOT, "packaging", "icon", "wallpaper.svg")
TARGET_PNG = os.path.join(ROOT, "packaging", "icon", "wallpaper.png")

WIDTH, HEIGHT = 3840, 2160

VIRIDIS = ["#440154", "#46327e", "#365c8d", "#277f8e", "#1fa187", "#4ac16d", "#a0da39", "#fde725"]

# Tooth half-widths at root and tip, as fractions of the pitch angle, and the addendum and
# dedendum in modules. Flat-topped trapezoids like the icon's, kept short: these are not
# involute teeth, and taller ones collide with their neighbours at the mesh.
ROOT_HALF, TIP_HALF = 0.27, 0.15
ADDENDUM, DEDENDUM = 0.75, 1.0
BACKLASH = 0.06

# The timing diagram: horizontal extent, frame period, and the baseline of each lane.
LANE_X0, LANE_X1 = 1350, 3800
FRAME = 400
LANE_Y = [1560, 1420, 1280, 1140, 1000]
LANE_HEIGHT = 70
PEAKS_Y = 860
# Arrival-time peaks within each frame: (position as a fraction of the frame, height, width).
PEAKS = [(0.35, 0.55, 18), (0.52, 1.0, 14), (0.61, 0.4, 12), (0.8, 0.7, 20)]


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _polar(cx: float, cy: float, r: float, a: float) -> tuple[float, float]:
    return cx + r * math.cos(a), cy + r * math.sin(a)


class Gear:
    def __init__(self, cx: float, cy: float, z: int, m: float, phase: float = 0.0):
        self.cx, self.cy, self.z, self.m, self.phase = cx, cy, z, m, phase
        self.rp = m * z / 2
        self.rt = self.rp + ADDENDUM * m
        self.rr = self.rp - DEDENDUM * m

    def corners(self) -> list[tuple[float, float]]:
        """The outline's vertices, root-tip-tip-root per tooth."""
        pitch = 2 * math.pi / self.z
        pts = []
        for i in range(self.z):
            th = self.phase + i * pitch
            for half, r in ((-ROOT_HALF, self.rr), (-TIP_HALF, self.rt),
                            (TIP_HALF, self.rt), (ROOT_HALF, self.rr)):
                pts.append(_polar(self.cx, self.cy, r, th + half * pitch))
        return pts

    def outline(self) -> str:
        """The corners joined by arcs along the tip and root circles."""
        pts = self.corners()
        d = [f"M{_n(pts[0][0])} {_n(pts[0][1])}"]
        for i in range(0, len(pts), 4):
            root_in, tip_a, tip_b, root_out = pts[i:i + 4]
            if i:
                d.append(f"A{_n(self.rr)} {_n(self.rr)} 0 0 1 {_n(root_in[0])} {_n(root_in[1])}")
            d.append(f"L{_n(tip_a[0])} {_n(tip_a[1])}")
            d.append(f"A{_n(self.rt)} {_n(self.rt)} 0 0 1 {_n(tip_b[0])} {_n(tip_b[1])}")
            d.append(f"L{_n(root_out[0])} {_n(root_out[1])}")
        d.append(f"A{_n(self.rr)} {_n(self.rr)} 0 0 1 {_n(pts[0][0])} {_n(pts[0][1])}Z")
        return "".join(d)

    def circle(self, r: float) -> str:
        cx, cy = self.cx, self.cy
        return (f"M{_n(cx - r)} {_n(cy)}A{_n(r)} {_n(r)} 0 1 1 {_n(cx + r)} {_n(cy)}"
                f"A{_n(r)} {_n(r)} 0 1 1 {_n(cx - r)} {_n(cy)}Z")

    def windows(self, n: int, ri: float, rh: float, spoke: float, rot: float,
                twist: float = 0.35) -> str:
        """The cut-outs between n curved crossings, from hub radius rh to rim radius ri."""
        cx, cy = self.cx, self.cy
        d = []
        for k in range(n):
            a1 = rot + k * 2 * math.pi / n
            a2 = a1 + 2 * math.pi / n
            do, dh = spoke / ri, spoke * 1.3 / rh
            o1 = _polar(cx, cy, ri, a1 + do + twist)
            o2 = _polar(cx, cy, ri, a2 - do + twist)
            h1 = _polar(cx, cy, rh, a1 + dh)
            h2 = _polar(cx, cy, rh, a2 - dh)
            rm = (ri + rh) / 2
            c1 = _polar(cx, cy, rm, a1 + (do + dh) / 2 + twist * 0.25)
            c2 = _polar(cx, cy, rm, a2 - (do + dh) / 2 + twist * 0.25)
            d.append(
                f"M{_n(h1[0])} {_n(h1[1])}Q{_n(c1[0])} {_n(c1[1])} {_n(o1[0])} {_n(o1[1])}"
                f"A{_n(ri)} {_n(ri)} 0 0 1 {_n(o2[0])} {_n(o2[1])}"
                f"Q{_n(c2[0])} {_n(c2[1])} {_n(h2[0])} {_n(h2[1])}"
                f"A{_n(rh)} {_n(rh)} 0 0 0 {_n(h1[0])} {_n(h1[1])}Z")
        return "".join(d)


def meshed_pair(cx: float, cy: float, z1: int, z2: int, m: float, angle: float,
                phase1: float = 0.0) -> tuple[Gear, Gear]:
    """A gear at (cx, cy) and a second one placed along `angle`, its teeth in the first's gaps.

    The first gear's nearest tooth sits `off` pitches clockwise of the line of centres;
    mirrored across the contact, the matching point on the second gear is the same pitch
    fraction counter-clockwise of its own end of that line, and a gap belongs there.
    """
    g1 = Gear(cx, cy, z1, m, phase1)
    sx, sy = _polar(cx, cy, g1.rp + m * z2 / 2 + BACKLASH * m, angle)
    p1, p2 = 2 * math.pi / z1, 2 * math.pi / z2
    off = ((angle - phase1) / p1) % 1.0
    return g1, Gear(sx, sy, z2, m, angle + math.pi + (off + 0.5) * p2)


def _inside(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    x, y = pt
    hit, j = False, len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def _meshes_cleanly(g1: Gear, g2: Gear) -> bool:
    """No sample of g2's tooth band facing g1 falls inside both outlines."""
    p1, p2 = g1.corners(), g2.corners()
    facing = math.atan2(g1.cy - g2.cy, g1.cx - g2.cx)
    for i in range(80):
        for j in range(21):
            pt = _polar(g2.cx, g2.cy, g2.rr + (g2.rt - g2.rr) * j / 20,
                        facing + (i - 40) / 40 * 0.9)
            if _inside(pt, p1) and _inside(pt, p2):
                return False
    return True


def _lfsr7(n: int) -> list[int]:
    """n bits of the maximal-length sequence from x^7 + x^6 + 1."""
    s, out = 0b1111111, []
    for _ in range(n):
        out.append(s & 1)
        s = ((s << 1) | (((s >> 6) ^ (s >> 5)) & 1)) & 0x7F
    return out


def _square(y: float, edges: list[float]) -> str:
    """A square wave along baseline y, starting low and toggling at each edge."""
    level = 0
    d = [f"M{LANE_X0} {_n(y)}"]
    for e in edges:
        if LANE_X0 < e < LANE_X1:
            level ^= 1
            d.append(f"H{_n(e)}V{_n(y - LANE_HEIGHT * level)}")
    d.append(f"H{LANE_X1}")
    return "".join(d)


def _lanes() -> list[list[float]]:
    frames = [1400 + k * FRAME for k in range(-1, 8)]
    gate = [e for s in frames for e in (s, s + 40)]
    trap = [e for s in frames for e in (s + 40, s + 300)]
    step = (LANE_X1 - LANE_X0) / 90
    prbs, prev = [], 0
    for i, b in enumerate(_lfsr7(90)):
        if b != prev:
            prbs.append(LANE_X0 + i * step)
            prev = b
    clock = [LANE_X0 + i * 50 for i in range(1, 49)]
    trigger = [e for s in frames for e in (s + 310, s + 330)]
    return [gate, trap, prbs, clock, trigger]


def _peaks() -> str:
    pts = []
    for x in range(LANE_X0, LANE_X1 + 1, 4):
        v = sum(amp * math.exp(-(((x - (s + pos * FRAME)) / wd) ** 2) / 2)
                for s in (1400 + k * FRAME for k in range(-1, 8))
                for pos, amp, wd in PEAKS)
        pts.append(f"{_n(x)} {_n(PEAKS_Y - 200 * v)}")
    return "M" + "L".join(pts)


def _gradient(gid: str, x1: float, y1: float, x2: float, y2: float) -> str:
    last = len(VIRIDIS) - 1
    stops = "".join(f'<stop offset="{100 * i / last:.1f}%" stop-color="{c}"/>'
                    for i, c in enumerate(VIRIDIS))
    return (f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'gradientUnits="userSpaceOnUse">{stops}</linearGradient>')


def draw() -> str:
    g1, g2 = meshed_pair(820, 1180, 16, 10, 44, math.radians(-42), phase1=0.15)
    if not _meshes_cleanly(g1, g2):
        raise SystemExit("the two gears' teeth overlap at the mesh; shorten or narrow them")

    defs = [
        _gradient("gears", 360, 1650, 1400, 520),
        _gradient("lanes", 1450, 0, 3700, 0),
        '<radialGradient id="backdrop" cx="900" cy="1100" r="2400" gradientUnits="userSpaceOnUse">'
        '<stop offset="0" stop-color="#24103a"/><stop offset="0.5" stop-color="#110b1c"/>'
        '<stop offset="1" stop-color="#0a0b10" stop-opacity="0"/></radialGradient>',
        # The lanes fade in out of the gears and out at the right edge.
        '<linearGradient id="fadein" x1="1350" y1="0" x2="1900" y2="0" gradientUnits="userSpaceOnUse">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff"/></linearGradient>',
        '<linearGradient id="fadeout" x1="3350" y1="0" x2="3800" y2="0" gradientUnits="userSpaceOnUse">'
        '<stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>',
        f'<mask id="lanefade" maskUnits="userSpaceOnUse" x="0" y="0" width="{WIDTH}" height="{HEIGHT}">'
        f'<rect x="1350" y="0" width="2000" height="{HEIGHT}" fill="url(#fadein)"/>'
        f'<rect x="3349" y="0" width="451" height="{HEIGHT}" fill="url(#fadeout)"/></mask>',
        '<filter id="glow" x="-10%" y="-50%" width="120%" height="200%">'
        '<feGaussianBlur stdDeviation="10" result="b"/><feMerge><feMergeNode in="b"/>'
        '<feMergeNode in="SourceGraphic"/></feMerge></filter>',
        '<filter id="bloom" x="-30%" y="-30%" width="160%" height="160%">'
        '<feGaussianBlur stdDeviation="50"/></filter>',
        '<radialGradient id="sheen" cx="0.35" cy="0.25" r="0.9">'
        '<stop offset="0" stop-color="#ffffff" stop-opacity="0.25"/>'
        '<stop offset="0.5" stop-color="#ffffff" stop-opacity="0.03"/>'
        '<stop offset="1" stop-color="#000000" stop-opacity="0.3"/></radialGradient>',
    ]

    body = []
    grid = "".join(f"M{x} 560V1640" for x in range(1400, LANE_X1, 100))
    body.append(f'<path d="{grid}" stroke="#ffffff" stroke-opacity="0.035" stroke-width="2" '
                f'mask="url(#lanefade)"/>')
    for y, edges in zip(LANE_Y, _lanes()):
        body.append(f'<path d="{_square(y, edges)}" fill="none" stroke="url(#lanes)" '
                    f'stroke-width="6" filter="url(#glow)" mask="url(#lanefade)"/>')
        body.append(f'<path d="M{LANE_X0} {y}H{LANE_X1}" stroke="#ffffff" stroke-opacity="0.05" '
                    f'stroke-width="2" mask="url(#lanefade)"/>')
    peaks = _peaks()
    body.append(f'<path d="{peaks}V{PEAKS_Y}H{LANE_X0}Z" fill="url(#lanes)" opacity="0.18" '
                f'mask="url(#lanefade)"/>')
    body.append(f'<path d="{peaks}" fill="none" stroke="url(#lanes)" stroke-width="5" '
                f'filter="url(#glow)" mask="url(#lanefade)"/>')

    wheels = [g1.outline() + g1.windows(5, g1.rr - 52, 110, 20, rot=0.3) + g1.circle(40),
              g2.outline() + g2.windows(4, g2.rr - 36, 64, 14, rot=0.1) + g2.circle(24)]
    for d in wheels:
        body.append(f'<path d="{d}" fill="url(#gears)" fill-rule="evenodd" filter="url(#bloom)" '
                    f'opacity="0.5"/>')
    for d in wheels:
        body.append(f'<path d="{d}" fill="url(#gears)" fill-rule="evenodd"/>')
        body.append(f'<path d="{d}" fill="url(#sheen)" fill-rule="evenodd"/>')
        body.append(f'<path d="{d}" fill="none" stroke="#fff" stroke-opacity="0.16" stroke-width="3"/>')

    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<!-- Generated by tools/make_wallpaper.py; edit that and re-run, not this file. -->\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
            f'width="{WIDTH}" height="{HEIGHT}">\n'
            f'<defs>{"".join(defs)}</defs>\n'
            '<rect width="100%" height="100%" fill="#0a0b10"/>\n'
            '<rect width="100%" height="100%" fill="url(#backdrop)"/>\n'
            + "\n".join(body) + "\n</svg>\n")


def _edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(path):
                return path
    return shutil.which("msedge")


def render_png() -> bool:
    edge = _edge()
    if edge is None:
        print("PNG skipped: Microsoft Edge not found to render the SVG's filters")
        return False
    if os.path.exists(TARGET_PNG):
        os.remove(TARGET_PNG)
    url = "file:///" + TARGET_SVG.replace(os.sep, "/")
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", f"--window-size={WIDTH},{HEIGHT}",
                    f"--screenshot={TARGET_PNG}", url],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if not os.path.isfile(TARGET_PNG):
        raise SystemExit(f"Edge wrote no screenshot to {TARGET_PNG}")
    print(f"{os.path.relpath(TARGET_PNG, ROOT)}  {WIDTH}x{HEIGHT}  "
          f"{os.path.getsize(TARGET_PNG) / 1024:.0f} KiB")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open", action="store_true",
                    help="open the written PNG in the default viewer")
    args = ap.parse_args()

    with open(TARGET_SVG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(draw())
    print(f"{os.path.relpath(TARGET_SVG, ROOT)}  {os.path.getsize(TARGET_SVG) / 1024:.0f} KiB")
    if render_png() and args.open:
        os.startfile(TARGET_PNG)  # noqa: S606 - Windows, and the path is this module's own
    return 0


if __name__ == "__main__":
    sys.exit(main())
