"""A window's chart for Details (the Desktop band) and the menu bar: what
was used over the window, the forecast to its reset, and forecasts made
earlier, to see by eye how the model does.

  actual    the level path (pace.level_path): fresh readings, linear between
            them, from the window's start
  forecast  from now to the reset: the 5 hours at the fitted rate (a line),
            the week at its intensity along the hour-of-week profile (slower
            at night, down to the profile's floor, steeper in the hours you
            usually work); capped at 100
  fan       the forecast at its slope `spread` lower and higher, each capped
            at 100: how sure it is (pace.SPREAD; the week's narrows as the
            week brings evidence, D-37)
  ghosts    forecasts made earlier in this window (an hour ago for the 5
            hours, a day and 2 days ago for the week), drawn from snapshots
            `snapshot` keeps as the status line and the band run
  nights    the week's night hours, outside the waking hours (the profile
            override's, else 8 to 23): [[start, end]], drawn as pale bands
  out       when it runs out before the reset (as the band says it), and
            out_label its time: a mark on the chart, the rest of the window
            tinted
  grid      local hours (5 hours) or the starts of local days (week);
            labels: "17", "Fri", in the order to place them (whole days
            before the week's edge days), each where no earlier one is

Times are epoch seconds, levels percents; the series are thinned to at most
POINTS points, so the band's JSON stays small.
"""

from . import clock, profile
from .config import FIVE_HOUR, SEVEN_DAY
from .pace import level_path
from .store import FIRST

POINTS = 120
# One snapshot of the forecast per this many seconds, per window, the last KEEP kept.
SNAP_EVERY = {FIVE_HOUR: 15 * 60, SEVEN_DAY: 2 * 3600}
KEEP = 60
# The forecasts drawn as ghosts: made this long ago (within a third of it).
GHOSTS = {FIVE_HOUR: (3600,), SEVEN_DAY: (86400, 2 * 86400)}
HOUR = profile.HOUR
WEEK = 7 * 86400
# A day at the week's edge shorter than this goes unnamed: its name would
# crowd the next day's in a narrow chart.
NAMED_DAY = 16 * HOUR


def _key(window):
    return "forecasts:" + window


def _slope(v):
    """What a snapshot keeps to redraw its forecast: percent an hour (5 hours)
    or percent per share of the week's expected spend (the week)."""
    if v["window"] == FIVE_HOUR:
        return v.get("rate")
    return v.get("_path_k")


def snapshot(store, v, now):
    """Keep the window's forecast, at most one every SNAP_EVERY, for ghosts
    later: [time, level, slope, where it lands uncapped]. A new window
    instance starts the list over."""
    if not v.get("present") or v.get("verdict") in ("maxed", "unknown") or _slope(v) is None:
        return False
    key = _key(v["window"])
    kept = store.meta_get(key) or {}
    log = kept.get("log", []) if kept.get("wid") == v["window_id"] else []
    if log and now - log[-1][0] < SNAP_EVERY[v["window"]]:
        return False
    end = v.get("projected_raw")
    log.append([int(now), round(v["used"], 2), round(_slope(v), 6), round(v["used"] if end is None else end, 2)])
    store.meta_set(key, {"wid": v["window_id"], "log": log[-KEEP:]})
    return True


def _thin(points, t0, t1, n=POINTS):
    """At most n points, the last of each of n time bins, the first and last kept."""
    if len(points) <= n:
        return points
    step = (t1 - t0) / float(n) or 1.0
    out, last_bin = [], None
    for p in points:
        b = int((p[0] - t0) / step)
        if out and b == last_bin:
            out[-1] = p
        else:
            out.append(p)
        last_bin = b
    if out[0] != points[0]:
        out.insert(0, points[0])
    return out


def _line(used, rate, t0, t1):
    """The 5 hours' forecast: a line at `rate` percent an hour, flat at 100."""
    if rate is None or t1 <= t0:
        return []
    end = used + rate * (t1 - t0) / HOUR
    if end <= 100.0 or used >= 100.0:
        return [[t0, used], [t1, min(end, 100.0) if used < 100.0 else used]]
    cap = t0 + (100.0 - used) / rate * HOUR
    return [[t0, used], [cap, 100.0], [t1, 100.0]]


def _walk(w, used, k, t0, t1):
    """The week's forecast: hour by hour along the profile, k percent per share."""
    if k is None or t1 <= t0:
        return []
    out, u = [[t0, used]], used
    for s, e, _, b in profile.segments(t0, t1):
        u = min(100.0, u + k * w[b] * (e - s) / HOUR)
        out.append([e, u])
        if u >= 100.0:
            out.append([t1, 100.0])
            break
    return out


def _forecast(v, w, used, slope, t0, t1):
    if v["window"] == FIVE_HOUR:
        return _line(used, slope, t0, t1)
    return _walk(w, used, slope, t0, t1)


def _ghost(v, w, snap, t1):
    """A forecast made earlier, from its snapshot. The week's is walked along
    today's profile (it is learned as you go), its slope set so it lands
    where that forecast said; the 5 hours' line needs no profile."""
    ts, used, slope = snap[0], snap[1], snap[2]
    if v["window"] == SEVEN_DAY and len(snap) > 3:
        share = profile.share_between(w, ts, t1)
        if share > 0:
            slope = max(0.0, snap[3] - used) / share
    return _forecast(v, w, used, slope, ts, t1)


def _at(points, t):
    """The level of a path [[t, u]] at t: linear between points, flat past its ends."""
    if t <= points[0][0]:
        return points[0][1]
    for (t0, u0), (t1, u1) in zip(points, points[1:]):
        if t <= t1:
            return u0 if t1 <= t0 else u0 + (u1 - u0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def _fan(v, w, used, slope, t0, t1):
    """[low, high]: the forecast at its slope `spread` lower and higher
    (pace.SPREAD, weekly spread), each capped at 100, or None when they
    hardly differ (idle, or both at 100 at about the same time)."""
    spread = v.get("spread")
    if not slope or not spread:
        return None
    lo = _forecast(v, w, used, slope * (1 - spread), t0, t1)
    hi = _forecast(v, w, used, slope * (1 + spread), t0, t1)
    if not lo or max(_at(hi, t) - _at(lo, t) for t, _ in lo + hi) < 1.0:
        return None
    return [[[int(t), round(u, 2)] for t, u in _thin(p, t0, t1, 60)] for p in (lo, hi)]


def _days(t0, t1):
    """The starts of local days in (t0, t1): the first hour of each new date,
    midnight, or 01:00 where a DST change skips midnight. An hour at a time,
    so a 23 or 25 hour day is no special case."""
    out, h, day = [], int(profile.hour_start(t0)), clock.localtime(t0).tm_yday
    for _ in range(int((t1 - t0) / HOUR) + 3):
        h = int(profile.hour_start(h + HOUR + 60))  # the next hour's start, always ahead
        if h >= t1 - 60:
            break
        if clock.localtime(h).tm_yday != day:
            out.append(h)
            day = clock.localtime(h).tm_yday
    return out


def _ticks(window, t0, t1):
    """(grid, labels): local hours with their hour (5 hours), or local
    midnights with the day's name at the middle of each day (week)."""
    if window == FIVE_HOUR:
        grid, t = [], int(profile.hour_start(t0)) + HOUR
        for _ in range(12):
            if t >= t1 - 60:
                break
            grid.append(t)
            t = int(profile.hour_start(t + HOUR + 60))
        # The ends too when they fall on the hour (a window opens at its first message).
        ends = [t for t in (t0, t1) if clock.localtime(t).tm_min == 0]
        labels = [[t, clock.strftime("%H", t)] for t in sorted(grid + ends)]
        return grid, labels
    grid = _days(t0, t1)
    edges = [int(t0)] + grid + [int(t1)]
    days = [[(a + b) // 2, clock.strftime("%a", (a + b) // 2), b - a] for a, b in zip(edges, edges[1:])
            if b - a >= NAMED_DAY]
    # Whole days first: a label is drawn only where none listed before it is.
    labels = [d[:2] for d in days if d[2] >= 23 * HOUR - 60] + [d[:2] for d in days if d[2] < 23 * HOUR - 60]
    return grid, labels


def _when(ts, now):
    """A run-out's time for its mark, as Details says it (band.at): 15:39, or
    Thu 13:30 a day or more on."""
    from .band import at

    return at(ts, now)


def _nights(t0, t1, wake):
    """[[start, end]] of the local hours outside the waking hours [h0, h1)
    in [t0, t1], an hour at a time, so DST days need no care."""
    h0, h1 = wake
    out, h = [], int(profile.hour_start(t0))
    for _ in range(int((t1 - t0) / HOUR) + 3):
        if h >= t1:
            break
        nxt = int(profile.hour_start(h + HOUR + 60))
        hr = clock.localtime(h).tm_hour
        awake = h0 <= hr < h1 if h0 < h1 else (hr >= h0 or hr < h1)
        if not awake:
            a, b = max(h, int(t0)), min(nxt, int(t1))
            if out and out[-1][1] == a:
                out[-1][1] = b
            elif b > a:
                out.append([a, b])
        h = nxt
    return out


def build(store, v, now, wake=(8, 23)):
    """The window's chart, or None without a window to draw."""
    if not v.get("present"):
        return None
    window, wid = v["window"], v["window_id"]
    t1 = v["reset_ts"]
    t0 = t1 - (WEEK if window == SEVEN_DAY else 5 * HOUR)
    used = v["used"]
    fresh = store.fresh_levels(wid)
    first = store.fresh_levels(wid, FIRST)
    path = level_path(fresh, v["start_ts"], used, now, first)
    # The readings and the start; not the path's own guess past the last step.
    actual = [[int(t), round(u, 2)] for t, u, guess in path if t0 <= t <= now and (not guess or t == path[0][0])]
    if not actual or actual[-1][0] < now:
        actual.append([int(now), round(used, 2)])
    w = v.get("_weights")
    forecast = []
    if v["verdict"] not in ("maxed", "unknown"):
        forecast = _forecast(v, w, used, _slope(v), now, t1)
    fan = _fan(v, w, used, _slope(v), now, t1) if forecast else None
    # When it runs out before the reset, as the band says it (band._out):
    # not within the margin of the reset, nor an early estimate's guess.
    out = v.get("cap_ts") if window == FIVE_HOUR else v.get("dry_ts")
    margin = 10 * 60 if window == FIVE_HOUR else 6 * HOUR
    out = int(out) if forecast and out and not v.get("shaky") and t1 - out > margin else None
    ghosts = []
    kept = store.meta_get(_key(window)) or {}
    log = kept.get("log", []) if kept.get("wid") == wid else []
    for ago in GHOSTS[window]:
        # The one closest to `ago` before now, within a third of it.
        near = [s for s in log if t0 <= s[0] <= now - ago * 2 / 3 and abs(s[0] - (now - ago)) <= ago / 3]
        if near:
            snap = min(near, key=lambda s: abs(s[0] - (now - ago)))
            g = _ghost(v, w, snap, t1)
            if g:
                ghosts.append([[int(t), round(y, 2)] for t, y in _thin(g, snap[0], t1, 60)])
    grid, labels = _ticks(window, t0, t1)
    return {
        "t0": int(t0), "t1": int(t1), "now": int(now),
        "actual": _thin(actual, t0, now),
        "forecast": [[int(t), round(u, 2)] for t, u in _thin(forecast, now, t1, 60)],
        "fan": fan, "ghosts": ghosts, "grid": [int(g) for g in grid], "labels": labels,
        "nights": _nights(t0, t1, wake) if window == SEVEN_DAY else [],
        "out": out, "out_label": None if out is None else _when(out, now),
    }


def attach(store, v5, vw, now, cfg=None):
    """Build both windows' charts into their views (v["chart"])."""
    ov = ((cfg or {}).get("profile") or {}).get("override") or {}
    wake = ov.get("wake_hours") or (8, 23)
    for v in (v5, vw):
        try:
            v["chart"] = build(store, v, now, wake)
        except Exception:  # a chart is never worth the band
            v["chart"] = None
    c = v5.get("chart")
    if c and vw.get("verdict") == "maxed" and c["forecast"]:
        # A maxed week blocks the 5 hours: nothing more until the reset.
        u = round(v5["used"], 2)
        c["forecast"] = [[c["now"], u], [c["t1"], u]]
        c["fan"] = None
        c["out"] = c["out_label"] = None
