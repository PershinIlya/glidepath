"""Tiny anti-aliased drawings as PNG, standard library only: what the menu
bar needs to draw the band's bars (rounded bars, dots, a wall) and charts
(lines, dashed or not, rings, the area under a line).

Coordinates are in pixels of the canvas; colors are (r, g, b, a) with
a in 0..1. Edges are sampled 3x3 per pixel, interiors filled at once.
"""

import math
import struct
import zlib

SUB = 3  # samples per pixel side on edges


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = int(w), int(h)
        self.px = [[0.0, 0.0, 0.0, 0.0] for _ in range(self.w * self.h)]  # straight r, g, b (0..255), a (0..1)

    def _blend(self, x, y, color, cover):
        r, g, b, a = color
        a *= cover
        if a <= 0:
            return
        p = self.px[y * self.w + x]
        out_a = a + p[3] * (1 - a)
        if out_a <= 0:
            return
        for i, c in enumerate((r, g, b)):
            p[i] = (c * a + p[i] * p[3] * (1 - a)) / out_a
        p[3] = out_a

    def round_rect(self, x0, y0, x1, y1, r, color, fade=None):
        """Fill a rounded rectangle; fade(x) -> 0..1 scales the alpha along x."""
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            return
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))

        def inside(x, y):
            dx = max(x0 + r - x, 0.0, x - (x1 - r))
            dy = max(y0 + r - y, 0.0, y - (y1 - r))
            return x0 <= x <= x1 and y0 <= y <= y1 and dx * dx + dy * dy <= r * r

        for py in range(max(0, int(y0)), min(self.h, int(y1) + 1)):
            for px in range(max(0, int(x0)), min(self.w, int(x1) + 1)):
                cx, cy = px + 0.5, py + 0.5
                core = (x0 + r + 1 <= cx <= x1 - r - 1 and y0 + 1 <= cy <= y1 - 1) or \
                       (x0 + 1 <= cx <= x1 - 1 and y0 + r + 1 <= cy <= y1 - r - 1)
                if core:
                    cover = 1.0
                else:
                    hits = sum(inside(px + (i + 0.5) / SUB, py + (j + 0.5) / SUB)
                               for i in range(SUB) for j in range(SUB))
                    cover = hits / float(SUB * SUB)
                if cover > 0:
                    self._blend(px, py, color, cover * (fade(cx) if fade else 1.0))

    def dot(self, cx, cy, r, color):
        self.round_rect(cx - r, cy - r, cx + r, cy + r, r, color)

    def polyline(self, pts, width, color, dash=None):
        """A line through pts [(x, y)], `width` wide with round joins; dash
        (on, off) in pixels along it. Each pixel takes its largest cover once,
        so a joint is no darker than the line."""
        cov, hw, run = {}, width / 2.0, 0.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            dx, dy = x1 - x0, y1 - y0
            l2 = dx * dx + dy * dy
            seg = math.sqrt(l2)
            for py in range(max(0, int(min(y0, y1) - hw - 1)), min(self.h, int(max(y0, y1) + hw + 2))):
                cy = py + 0.5
                for px in range(max(0, int(min(x0, x1) - hw - 1)), min(self.w, int(max(x0, x1) + hw + 2))):
                    cx = px + 0.5
                    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((cx - x0) * dx + (cy - y0) * dy) / l2))
                    c = hw + 0.5 - math.hypot(cx - x0 - t * dx, cy - y0 - t * dy)
                    if c <= 0 or (dash and (run + t * seg) % (dash[0] + dash[1]) > dash[0]):
                        continue
                    k = py * self.w + px
                    if c > cov.get(k, 0.0):
                        cov[k] = min(1.0, c)
            run += seg
        for k, c in cov.items():
            self._blend(k % self.w, k // self.w, color, c)

    def ring(self, cx, cy, r, width, color):
        """A circle's outline, `width` wide."""
        for py in range(max(0, int(cy - r - width)), min(self.h, int(cy + r + width) + 1)):
            for px in range(max(0, int(cx - r - width)), min(self.w, int(cx + r + width) + 1)):
                c = width / 2.0 + 0.5 - abs(math.hypot(px + 0.5 - cx, py + 0.5 - cy) - r)
                if c > 0:
                    self._blend(px, py, color, min(1.0, c))

    def fill_between(self, x0, x1, top, bottom, color):
        """Fill each column from top(x) down to bottom(x), x the column's
        middle, the edge pixels in part."""
        for px in range(max(0, int(x0)), min(self.w, int(math.ceil(x1)))):
            cx = px + 0.5
            if not x0 <= cx <= x1:
                continue
            a, b = top(cx), bottom(cx)
            for py in range(max(0, int(a)), min(self.h, int(math.ceil(b)))):
                c = min(py + 1.0, b) - max(float(py), a)
                if c > 0:
                    self._blend(px, py, color, min(1.0, c))

    def png(self, dpi=144):
        """PNG bytes; dpi 144 says the pixels are at twice the point size."""
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)
            for x in range(self.w):
                r, g, b, a = self.px[y * self.w + x]
                raw += bytes((int(round(r)), int(round(g)), int(round(b)), int(round(a * 255))))

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        ppm = int(round(dpi / 0.0254))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
                + chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))


def hex_color(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)
