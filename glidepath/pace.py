"""Pace math for the 5-hour window, plus helpers shared with the weekly view.

All rates are in percentage points per hour. Times are epoch seconds.
"""

from . import config
from .config import FIVE_HOUR
from .store import FIRST, FRESH

IDLE_RATE = 0.05  # %/h; below this a fitted slope counts as idle
# The rate's relative uncertainty: an early estimate (fewer than
# rate_min_samples readings) or a fitted one (D-37).
SPREAD = {"interpolated": 0.4, "lsq": 0.25}
SURE_MARGIN_S = 10 * 60  # band.ON_PACE_MARGIN_S for the 5 hours


def lsq_slope(points):
    """Ordinary least squares slope of (t, y) points, per hour. None if degenerate."""
    n = len(points)
    if n < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in points]
    ys = [y for _, y in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def running_series(rows, start_value=None):
    """(ts, used, flag) rows -> (ts, running max of FRESH values).

    Heartbeats contribute their timestamp only: they prove time passed, and
    their value may be an idle session's stale reading.
    """
    out = []
    cur = start_value
    for ts, used, flag in rows:
        if flag == FRESH:
            cur = used if cur is None else max(cur, used)
        elif cur is None:
            cur = used
        out.append((ts, cur))
    return out


def level_path(fresh, start_ts, used, now, first=()):
    """[(ts, level, synthetic)] of the window's level, from fresh readings.

    Timed points are the first time each new level (running max) was read;
    a repeat of the same value says nothing about when the next one comes.
    The path starts at 0 at the window start. A session's first reading
    (`first`) may be old (every open session re-renders when the status line
    is installed): it only says the level was at least that by then, so it
    becomes a point only where it lifts the path. After the last step the
    path keeps that step's slope for one reading quantum (readings are whole
    percents, so "no new value yet" is what steady work looks like between
    steps; only for a step between two readings), then goes flat: a finished
    burst fades out and idle reads 0.
    """
    path, run, steps = [], None, []
    for ts, u in fresh:
        if run is None or u > run:
            if run is not None:
                steps.append(u - run)
            run = u
            path.append((ts, run, False))
    if not path or path[0][0] > start_ts + 1:
        path.insert(0, (start_ts, 0.0, True))
    if path[-1][1] < used:  # the level is known now (a first reading, or the write failed)
        path.append((now, used, True))
    for ts, u in first:
        if start_ts < ts < now and u > level_at(path, ts) + 1e-9:
            path = [p for p in path if p[0] < ts] + [(ts, u, False)] + [p for p in path if p[0] > ts and p[1] > u]
    if path[-1][2]:
        return path
    if len(path) >= 2 and not path[-2][2]:  # a step between two readings, not the ramp from 0
        (t0, y0, _), (t1, y1, _) = path[-2], path[-1]
        quantum = min(1.0, max(0.1, min(steps))) if steps else 1.0
        if t1 > t0 and y1 > y0 and now > t1:
            slope = (y1 - y0) / (t1 - t0)
            t_ext = t1 + quantum / slope
            if t_ext < now:
                path.append((t_ext, y1 + quantum, True))
            else:
                path.append((now, y1 + slope * (now - t1), True))
    return path


def level_at(path, t):
    """Piecewise-linear level at t; flat after the last point and before the first."""
    if t <= path[0][0]:
        return path[0][1]
    for (t0, y0, _), (t1, y1, _) in zip(path, path[1:]):
        if t <= t1:
            return y0 if t1 <= t0 else y0 + (y1 - y0) * (t - t0) / (t1 - t0)
    return path[-1][1]


def early(v, rough, cfg):
    """D-37: an early estimate warns only of what holds at its low edge. A
    run-out that does not is `shaky` (no "Runs out", no color); slow down
    becomes on pace unless it overshoots at the low edge too."""
    out = v.get("cap_ts") if v["window"] == FIVE_HOUR else v.get("dry_ts")
    v["shaky"] = bool(rough and out and not v.get("sure_out"))
    m, spread = v.get("multiplier"), v.get("spread") or 0.0
    if rough and v["verdict"] == "slow_down" and m is not None and spread < 1:
        if m / (1 - spread) >= float(cfg["verdict"]["slow_down"]):
            v["verdict"] = "on_pace"


def verdict_for(mult, cfg):
    th = cfg["verdict"]
    if mult is None:
        return "unknown"
    if mult >= float(th["speed_up"]):
        return "speed_up"
    if mult < float(th["slow_down"]):
        return "slow_down"
    return "on_pace"


def current_used(store, inst, reading):
    """The window's level: the stored running max, or this payload's reading
    if the write was skipped and it is newer and higher."""
    used = inst["max_used"]
    if reading and abs(reading[1] - inst["reset_ts"]) <= 300:
        used = max(used, reading[0])
    return used


def five_hour(store, now, cfg, reading=None):
    """Everything known about the current 5-hour window, as a dict."""
    v = {"window": FIVE_HOUR, "present": False}
    inst = store.open_instance(FIVE_HOUR)
    if inst is None or inst["reset_ts"] <= now:
        v["verdict"] = "fresh"
        v["severity"] = "dim"
        if reading:  # the write failed before it opened this window: show its level, no math
            v["raw"] = reading
        elif inst is not None:  # it ended, and no new one is seen yet: that opens with the next message
            v["ended_ts"] = inst["reset_ts"]
        return v
    tgt = config.target(cfg, FIVE_HOUR)
    used = current_used(store, inst, reading)
    reset = inst["reset_ts"]
    rem_h = max(0.0, (reset - now) / 3600.0)
    v.update(present=True, used=used, reset_ts=reset, start_ts=inst["start_ts"], remaining_h=rem_h,
             target=tgt, window_id=inst["id"])

    # The level path: fresh readings only (a heartbeat repeats a session's
    # last API response and says nothing about the account's level), linear
    # between readings (spend in an unobserved gap is spread over it, not
    # squeezed into the moment we looked); see level_path for both ends. The
    # rate is the least-squares slope of that path over the trailing window,
    # which never reaches back past the window's first real reading once a
    # second one follows it (the 0-at-start ramp would dilute it; there is no
    # rate until min_span follows the first). A single reading keeps reaching
    # back: the window average, which fades out over the fit window.
    win_s = float(cfg["rate_window_min"]) * 60.0
    min_span = min(float(cfg["rate_min_span_min"]) * 60, win_s)
    fresh = store.fresh_levels(inst["id"])
    first = store.fresh_levels(inst["id"], FIRST)
    path = level_path(fresh, inst["start_ts"], used, now, first)
    real_pts = [p for p in path if not p[2]]
    since = max(now - win_s, inst["start_ts"])
    if len(real_pts) >= 2 and since < real_pts[0][0]:
        since = real_pts[0][0]
    rate, source, inside = None, None, []
    if now - since >= min_span:
        inside = [p for p in path if since < p[0] < now]
        points = [(since, level_at(path, since))] + [(t, y) for t, y, _ in inside] + [(now, level_at(path, now))]
        s = lsq_slope(points)
        if s is not None:
            # A fit carried by fewer than rate_min_samples readings is marked ~.
            # A FIRST reading counts only where it is a point of the path.
            on_path = [(ts, u) for ts, u in first if (ts, u, False) in path]
            real = sorted(ts for ts, _ in fresh + on_path if since <= ts <= now)
            enough = len(real) >= int(cfg["rate_min_samples"]) and now - real[0] >= min_span
            rate, source = max(0.0, s), "lsq" if enough else "interpolated"
    v["rate"] = rate
    v["rate_source"] = source
    v["spread"] = SPREAD.get(source)
    # When the level was last read at what it is now: the start of a nowcast
    # (band.level). A FIRST reading's timing may be stale (level_path), so not those.
    at = [ts for ts, u in fresh if u >= used - 1e-9 and ts <= now]
    v["known_ts"] = max(at) if at else None
    v["last_seen"] = inst["last_seen"]
    v["rate_points"] = len(inside) + 2
    v["rate_since"] = since  # the fit start: `glidepath now` names why there is no rate

    allowed = max(0.0, tgt - used) / rem_h if rem_h > 0 else 0.0
    v["allowed"] = allowed
    v["idle"] = rate is not None and rate < IDLE_RATE

    if rate is None:
        v.update(multiplier=None, projected=None, projected_raw=None, cap_ts=None)
        v["verdict"] = "unknown"
    elif v["idle"]:
        v.update(multiplier=None, projected=used, projected_raw=used, cap_ts=None)
        v["verdict"] = "idle"
    else:
        raw = used + rate * rem_h
        cap_ts = None
        if raw >= 100.0 and used < 100.0:
            cap_ts = now + (100.0 - used) / rate * 3600.0
            # Runs out even at the low edge of the rate: a firm "Runs out".
            low = rate * (1 - SPREAD[source])
            v["sure_out"] = low > 0 and reset - (now + (100.0 - used) / low * 3600.0) > SURE_MARGIN_S
        mult = allowed / rate if rate > 0 else None
        v.update(multiplier=mult, projected=min(100.0, raw), projected_raw=raw, cap_ts=cap_ts)
        # When it would reach 100 at this rate, past the reset too (Details' "Runs out").
        if used < 100.0 and rate > 0:
            v["empty_ts"] = now + (100.0 - used) / rate * 3600.0
        v["verdict"] = verdict_for(mult, cfg)
        early(v, source == "interpolated", cfg)
    if used >= 100.0:
        v["verdict"] = "maxed"
    if v["verdict"] == "unknown":
        # No rate yet: the burn is unknown, not zero, and so is the leftover
        # (100 - used would assume nothing more is spent).
        v["unused_at_reset"] = None
    else:
        proj = v["projected"] if v["projected"] is not None else used
        v["unused_at_reset"] = max(0.0, 100.0 - proj)
    v["in_tail"] = (reset - now) <= float(cfg["tail"]["five_hour_min"]) * 60
    v["severity"] = severity(v, cfg)
    return v


def over_target(v):
    """At or above a target set below 100: nothing left to allow."""
    return v.get("target", 100.0) < 100.0 and v.get("used", 0.0) >= v["target"]


def spare(points, min_free):
    """At least min_free points, and some: with min_free 0 a leftover of 0
    (maxed, or dry before the reset) is nothing to harvest. None (unknown)
    is never spare."""
    return points is not None and points > 0 and points >= float(min_free)


def below_target(points, target):
    """Of `points` free out of 100 (now, or projected at the reset), the part
    below the window's target: max(0, target - level). A target below 100
    keeps a reserve, and the reserve is never spare. None stays None."""
    return None if points is None else max(0.0, points - (100.0 - target))


def severity(v, cfg):
    """dim (fine), opportunity (spare quota about to expire), warn, alert.

    alert: maxed, or it runs dry before the reset with a slow down verdict,
    also at the low edge of the estimate (`sure_out`).
    warn: a milder overshoot (dry, but on pace), slow down without a dry
    time, or at or above a target below 100 whatever the verdict (idle
    included). At target 100 slow down always runs dry.
    opportunity: in the tail with at least min_free points projected to
    expire below the target (the leftover above it is the reserve the
    target keeps); never while the leftover is unknown (no rate yet),
    whatever min_free (taken as 0, it passed min_free 0).
    """
    if not v.get("present"):
        return "dim"
    dry = v.get("cap_ts") is not None and not v.get("shaky")
    if v["verdict"] == "maxed" or (dry and v["verdict"] == "slow_down" and v.get("sure_out", True)):
        return "alert"
    if dry or v["verdict"] == "slow_down" or over_target(v):
        return "warn"
    if v.get("in_tail") and spare(below_target(v.get("unused_at_reset"), v.get("target", 100.0)), cfg["min_free"]):
        return "opportunity"
    return "dim"
