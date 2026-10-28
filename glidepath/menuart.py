"""The band's bars as pictures for the macOS menu bar (SwiftBar `image=`).

The same language as the Desktop band (mods/glidepath/hooks/register.tsx,
barSvg): orange for what is used, a pale orange forecast to the reset after
a hairline gap (fading while the estimate is rough), red into a wall when it
runs out, green dots over quota that will go unused, all red when reached,
grey when idle. Drawn at twice the point size; SwiftBar is told the size in
points (width=, height=), so they are sharp on a Retina screen.

A window's chart (chart.py) as the Desktop draws it in Details (chartSvg):
what was used, the forecast to the reset, forecasts made earlier, the grid.
A picture has no text here (no fonts), so its hours or days are a line of
Menlo under it, each label under its grid line (`labels`).
"""

import base64

from .png import Canvas, hex_color

S = 2  # pixels per point
PALETTE = {
    False: {"t": "#8a867c", "a": "#d97757", "r": "#d0564c", "g": "#5b9c64", "y": "#c08a1e", "k": "#141413"},  # light
    True: {"t": "#b4afa4", "a": "#d97757", "r": "#e5695e", "g": "#74b97c", "y": "#e0b252", "k": "#ecebe5"},  # dark
}


def _draw(c, w, x, y, width, h, dark, dots=True):
    """One bar of the window `w` (band.window) in the box (x, y, width, h) in points."""
    p = PALETTE[dark]
    r = h / 2.0
    X = lambda pct: x + max(0.0, min(100.0, pct)) / 100.0 * width  # noqa: E731
    box = lambda x0, x1, color, fade=None: c.round_rect(  # noqa: E731
        x0 * S, y * S, x1 * S, (y + h) * S, r * S, color, fade)
    box(x, x + width, hex_color(p["t"], 0.24))
    kind = w["kind"]
    if kind == "wait" or w.get("pct") is None:
        return
    if kind == "maxed":
        box(x, x + width, hex_color(p["r"]))
        return
    u = X(w["used"])
    gap = max(u, x + h) + 1.0 if w["used"] > 0 else x
    if kind == "dry":
        if x + width - gap >= h:
            fade = None
            if w.get("soft"):
                lo, hi = gap * S, (x + width) * S
                fade = lambda px: 1.0 - 0.8 * (px - lo) / max(1.0, hi - lo)  # noqa: E731
            box(gap, x + width, hex_color(p["r"], 0.45), fade)
        c.round_rect((x + width - 1.2) * S, (y - 2) * S, (x + width) * S, (y + h + 2) * S, 0.6 * S,
                     hex_color(p["r"], 0.5 if w.get("soft") else 1.0))
    elif kind != "idle" and w.get("projected") is not None:
        end = X(w["projected"])
        if end - gap >= h:
            fade = None
            if kind == "rough":
                lo, hi = gap * S, end * S
                fade = lambda px: 1.0 - 0.9 * (px - lo) / max(1.0, hi - lo)  # noqa: E731
            box(gap, end, hex_color(p["a"], 0.36), fade)
        if kind == "expire" and dots:
            step = 3.5
            cx = end + 3.0
            while cx < x + width - 1.5:
                c.dot(cx * S, (y + h / 2.0) * S, 0.9 * S, hex_color(p["g"]))
                cx += step
    if w["used"] > 0:
        box(x, max(u, x + h), hex_color(p["t"], 0.7) if kind == "idle" else hex_color(p["a"]))


def bar(w, width, height, dark, pad=0):
    """A window's bar, `width` x `height` points, as base64 PNG; `pad` points
    on the right stay clear, room before the text beside it."""
    bar_h = 5.0
    c = Canvas(width * S, height * S)
    _draw(c, w, 0.5, (height - bar_h) / 2.0, width - 1.5 - pad, bar_h, dark)
    return base64.b64encode(c.png()).decode("ascii")


def icon(ws, dark, width=24, height=18):
    """Both windows stacked, 5 hours on top, for the menu bar title."""
    c = Canvas(width * S, height * S)
    bar_h, gap = 4.0, 3.0
    top = (height - 2 * bar_h - gap) / 2.0
    for i, w in enumerate(ws[:2]):
        _draw(c, w, 0.5, top + i * (bar_h + gap), width - 1.5, bar_h, dark, dots=False)
    return base64.b64encode(c.png()).decode("ascii")


# The chart's margins in points: room for the end ring on the right and the
# line at 100 on top.
PAD_L, PAD_R, PAD_T, PAD_B = 1.0, 4.0, 4.0, 4.5  # the bottom holds the nights' strip


def _x(c, t, width):
    return PAD_L + (t - c["t0"]) / float(c["t1"] - c["t0"]) * (width - PAD_L - PAD_R)


def chart(c, width, height, dark, tone=None):
    """The chart `c` (chart.build), `width` x `height` points, as base64 PNG.
    `tone`: the pace's ("error", "warning"), the forecast's color then."""
    p = PALETTE[dark]
    cv = Canvas(width * S, height * S)
    X = lambda t: _x(c, t, width) * S  # noqa: E731
    Y = lambda u: (PAD_T + (1 - max(0.0, min(100.0, u)) / 100.0) * (height - PAD_T - PAD_B)) * S  # noqa: E731
    pts = lambda series: [(X(t), Y(u)) for t, u in series]  # noqa: E731
    base, top = Y(0), Y(100)
    for a, b in c.get("nights") or []:  # a faint tint behind the rest, a thin strip under the axis
        cv.fill_between(X(a), X(b), lambda x: top, lambda x: base, hex_color(p["t"], 0.06))
        cv.round_rect(X(a), base + 1.5 * S, X(b), base + 3.0 * S, 0.75 * S, hex_color(p["t"], 0.45))
    for t in c["grid"]:
        cv.polyline([(X(t), top), (X(t), base)], 1.0, hex_color(p["t"], 0.22))
    cv.polyline([(PAD_L * S, top), ((width - PAD_R) * S, top)], 1.2, hex_color(p["t"], 0.6), dash=(2.5, 4))
    for g in c["ghosts"]:
        cv.polyline(pts(g), 1.6, hex_color(p["t"], 0.6), dash=(5, 5))
    a = pts(c["actual"])
    if len(a) > 1:
        cv.fill_between(a[0][0], a[-1][0], lambda x: _at(a, x), lambda x: base, hex_color(p["a"], 0.14))
    f = pts(c["forecast"])
    if c.get("fan"):  # between the slower and the faster line (the faster on top)
        lo, hi = pts(c["fan"][0]), pts(c["fan"][1])
        cv.fill_between(lo[0][0], lo[-1][0], lambda x: _at(hi, x), lambda x: _at(lo, x), hex_color(p["a"], 0.12))
    now = X(c["now"])
    cv.polyline([(now, top), (now, base)], 1.0, hex_color(p["k"], 0.35))
    if len(a) > 1:
        cv.polyline(a, 3.0, hex_color(p["a"]))
    if len(a) <= 30:
        for x, y in a[1:]:
            cv.dot(x, y, 2.2, hex_color(p["a"]))
    if c.get("out"):  # runs out before the reset: the rest tinted, a dashed mark, a dot at 100
        hue = p["y"] if tone == "warning" else p["r"]
        x = X(c["out"])
        cv.fill_between(x, (width - PAD_R) * S, lambda _: top, lambda _: base, hex_color(hue, 0.1))
        cv.polyline([(x, top), (x, base)], 1.6, hex_color(hue, 0.9), dash=(4, 3))
        cv.dot(x, top, 3.2, hex_color(hue))
    if len(f) > 1:
        color = hex_color(p["r"]) if tone == "error" else hex_color(p["y"]) if tone == "warning" else hex_color(p["a"], 0.8)
        cv.polyline(f, 2.6, color, dash=(7, 5))
        cv.ring(f[-1][0] - 3, f[-1][1], 3.6, 2.0, color)
    cv.dot(a[-1][0], a[-1][1], 4.0, hex_color(p["a"]))
    return base64.b64encode(cv.png()).decode("ascii")


def _at(pts, x):
    """The line through pts (sorted by x) at x."""
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 if x1 <= x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def labels(c, width, advance):
    """The chart's labels as one line of a monospaced font, `advance` points a
    character: each centered under its x (at an edge, inside it), in the
    order listed; one that would touch a label placed before it (a space
    apart) is left out, not moved."""
    placed, limit = [], int(width // advance)
    for t, text in c["labels"]:
        col = max(0, min(int(round(_x(c, t, width) / advance - len(text) / 2.0)), limit - len(text)))
        if all(col + len(text) < c0 or c0 + len(t0) < col for c0, t0 in placed):
            placed.append((col, text))
    out = ""
    for col, text in sorted(placed):
        out = out.ljust(col) + text
    return out
