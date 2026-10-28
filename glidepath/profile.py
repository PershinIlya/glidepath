"""Hour-of-week usage profile: 168 bins, Monday 00:00 local = bin 0.

w(h) is the expected share of a week's spend that falls in hour-of-week h
(sum 1). It combines:

  prior p(h)  from transcripts (cost-weighted tokens per hour of week, see
              transcripts.py; it fades toward uniform with the half-life)
              or from a manual override, else uniform;
  observed    weekly-percent spend per completed local hour, learned
              incrementally as a decay-weighted mean per bin (14-day half
              life, i.e. an EWMA in continuous time);

as a shrinkage estimate r(h) = (n(h) m(h) + k M p(h)) / (n(h) + k), where
n(h) is the decayed observation weight, m(h) the observed mean, k the prior
strength and M the weekly total implied by the observations. Then a floor
keeps off-hours at `floor` times the typical awake-hour rate, and the
result is normalized. docs/design-decisions.md D-10..D-15 explain each choice.
"""

import json
import math

from . import clock
from .config import SEVEN_DAY
from .store import FIRST, FRESH

BINS = 168
HOUR = 3600.0
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PROFILE_KEY = "profile"
LOW_CONFIDENCE = 0.25  # weight of an hour learned from a long gap between readings
TYPICAL_SEEN = 112  # hours of the week observed before the profile's scale stands for a typical week
AWAKE_RATIO = 0.25  # an hour is awake when its rate is at least this share of typical


def bin_of(ts):
    lt = clock.localtime(ts)
    return lt.tm_wday * 24 + lt.tm_hour


def hour_start(ts):
    """Start of the local clock hour containing ts (DST and half-hour zones safe)."""
    lt = clock.localtime(ts)
    return math.floor(ts) - lt.tm_min * 60 - lt.tm_sec


def segments(t0, t1):
    """Split [t0, t1) at local hour boundaries: yields (start, end, hour_start, bin)."""
    t = t0
    while t < t1:
        hs = hour_start(t)
        nxt = min(t1, hs + HOUR)
        if nxt <= t:  # paranoia against a clock that does not advance
            nxt = t + HOUR
        yield t, nxt, hs, bin_of(t)
        t = nxt


def uniform():
    return [1.0 / BINS] * BINS


def normalize(v):
    s = sum(v)
    if s <= 0 or not math.isfinite(s):
        return uniform()
    return [x / s for x in v]


def smooth(v, side=0.15):
    """Light circular 3-tap smoothing along the hour axis."""
    n = len(v)
    mid = 1.0 - 2 * side
    return [side * v[(i - 1) % n] + mid * v[i] + side * v[(i + 1) % n] for i in range(n)]


def override_shape(ov):
    """Shape from {"work_days": [0..6], "wake_hours": [h0, h1], "off_day_factor": f}."""
    days = set(int(d) for d in ov.get("work_days", [0, 1, 2, 3, 4]))
    h0, h1 = ov.get("wake_hours", [8, 23])
    h0, h1 = int(h0), int(h1)
    off = float(ov.get("off_day_factor", 0.5))
    v = []
    for b in range(BINS):
        d, h = divmod(b, 24)
        awake = (h0 <= h < h1) if h0 <= h1 else (h >= h0 or h < h1)
        if not awake:
            v.append(0.0)
        else:
            v.append(1.0 if d in days else off)
    return v


class Profile:
    """Persistent profile state, stored as one JSON blob in the meta table."""

    def __init__(self, data=None):
        d = data or {}
        self.prior = d.get("prior")  # list[168] shape, or None
        self.prior_meta = d.get("prior_meta") or {}
        self.S = d.get("S") or [0.0] * BINS
        self.W = d.get("W") or [0.0] * BINS
        self.T = d.get("T") or [0.0] * BINS  # time of each bin's last update
        self.processed_until = d.get("processed_until")
        self.learned_hours = d.get("learned_hours", 0)

    def to_json(self):
        return {
            "version": 1,
            "prior": self.prior,
            "prior_meta": self.prior_meta,
            "S": self.S,
            "W": self.W,
            "T": self.T,
            "processed_until": self.processed_until,
            "learned_hours": self.learned_hours,
        }

    # -- learning --------------------------------------------------------------
    def observe(self, hstart, value, conf, half_life_s):
        b = bin_of(hstart)
        if self.W[b] > 0 and hstart > self.T[b]:
            decay = 0.5 ** ((hstart - self.T[b]) / half_life_s)
            self.S[b] *= decay
            self.W[b] *= decay
        self.S[b] += conf * value
        self.W[b] += conf
        self.T[b] = max(self.T[b], hstart)
        self.learned_hours += 1

    def observed(self, now, half_life_s):
        """Per bin (mean %/hour, effective weight now)."""
        out = []
        for b in range(BINS):
            if self.W[b] <= 0:
                out.append((None, 0.0))
                continue
            n = self.W[b] * 0.5 ** (max(0.0, now - self.T[b]) / half_life_s)
            out.append((self.S[b] / self.W[b], n))
        return out

    # -- the shape ---------------------------------------------------------------
    def prior_fade(self, now, half_life_s):
        """How much of the transcript prior is left: 0.5 ** (age / half-life)
        from its build (D-14)."""
        return 0.5 ** (max(0.0, now - float(self.prior_meta.get("built_at", now))) / half_life_s)

    def rates(self, now, cfg, weekly_total=None):
        """Unnormalized expected rate per bin and the weekly total M.

        M is the scale of the prior: the peak of recent closed weekly
        windows when known (`weekly_total`), else the ratio estimate
        sum(m) / sum(p) over the observed bins.
        """
        pc = cfg["profile"]
        ov = pc.get("override")
        if isinstance(ov, dict) and ov.get("mode") == "replace":
            return override_shape(ov), weekly_total, "override"
        k = float(pc["prior_strength"])
        hl = float(pc["half_life_days"]) * 86400
        if isinstance(ov, dict):
            prior, src = normalize(override_shape(ov)), "override+observed"
        elif self.prior:
            # The transcripts describe the weeks before they were read: shape
            # and strength fade toward uniform with the half-life, like an
            # observation. Without it, hours only seen through long gaps
            # (learned at low confidence, spread by this very shape) kept the
            # transcripts' rate for good.
            a = self.prior_fade(now, hl)
            prior = normalize([a * x + (1 - a) / BINS for x in normalize(self.prior)])
            src = "transcripts+observed"
            # A prior built from a few days is mostly uniform: weigh it so.
            trust = float(self.prior_meta.get("trust", 1.0)) * a
            ku = float(pc.get("prior_strength_uniform", 0.25))
            k = ku + trust * (k - ku)
        else:
            # A flat prior knows nothing: let a few observations outweigh it.
            prior, src = uniform(), "uniform+observed"
            k = float(pc.get("prior_strength_uniform", 0.25))
        obs = self.observed(now, hl)
        # Unweighted: busy hours are observed with more confidence (short gaps
        # between readings), so weighting by n would inflate M.
        num = sum(m for m, n in obs if m is not None)
        den = sum(prior[b] for b, (m, n) in enumerate(obs) if m is not None)
        if den <= 0 or num <= 0:
            return prior, weekly_total, src.replace("+observed", "")
        M = weekly_total if weekly_total else num / den
        r = []
        for b, (m, n) in enumerate(obs):
            if m is None:
                r.append(M * prior[b])
            else:
                r.append((n * m + k * M * prior[b]) / (n + k))
        return r, M, src

    def weights(self, now, cfg, weekly_total=None):
        """(w[168] summing to 1, awake[168] bools, info dict)."""
        r, M, src = self.rates(now, cfg, weekly_total)
        typical = typical_rate(r)
        floor = float(cfg["profile"]["floor"]) * typical
        awake = [x >= AWAKE_RATIO * typical for x in r] if typical > 0 else [True] * BINS
        r = [max(x, floor) for x in r]
        w = normalize(r)
        # typical: what a week comes to, for the weekly forecast's prior (D-37):
        # the closed weeks' own peak (typical_week); else M once most of the
        # week's hours were observed (before that, M is the busy hours' rate
        # stretched over all 168); else None.
        hl = float(cfg["profile"]["half_life_days"]) * 86400
        seen = sum(1 for m, _ in self.observed(now, hl) if m is not None)
        typical = weekly_total or (M if seen >= TYPICAL_SEEN else None)
        info = {"source": src, "weekly_total": M, "awake_hours": sum(awake), "typical": typical}
        return w, awake, info


def typical_rate(r):
    """Mean of the busiest third of the bins: a typical awake-hour rate."""
    top = sorted(r, reverse=True)[: BINS // 3]
    return sum(top) / len(top) if top else 0.0


def typical_week(store, now, half_life_s, weeks=8):
    """Decay-weighted mean peak of recent closed weekly windows that were
    observed during their last day (an unobserved tail understates the peak)
    and lasted a whole week (one opened by a drop reset, or cut short by an
    early reset, holds part of one)."""
    rows = store.db.execute(
        "SELECT reset_ts, max_used, (SELECT MAX(ts) FROM samples s WHERE s.window_id=w.id AND s.flag IN (?, ?)) "
        "FROM windows w WHERE window=? AND closed_at IS NOT NULL "
        "AND close_reason IN ('expired', 'rollover') AND reset_ts > ? AND reset_ts <= ? "
        "AND reset_ts - start_ts >= ? AND closed_at >= reset_ts - 3600",
        (FRESH, FIRST, SEVEN_DAY, now - weeks * 7 * 86400, now, 7 * 86400 - 1),
    ).fetchall()
    num = den = 0.0
    for reset, peak, seen in rows:
        if seen is None or seen < reset - 86400:
            continue
        a = 0.5 ** ((now - reset) / half_life_s)
        num += a * peak
        den += a
    return num / den if den > 0 else None


def current_weights(store, prof, now, cfg):
    """prof.weights scaled by the typical week (D-13): what the line, `glidepath
    profile` and learning all use."""
    hl = float(cfg["profile"]["half_life_days"]) * 86400
    return prof.weights(now, cfg, typical_week(store, now, hl))


def load(store):
    try:
        return Profile(store.meta_get(PROFILE_KEY))
    except Exception:
        return Profile()


def save(store, prof):
    store.meta_set(PROFILE_KEY, prof.to_json())


# -- walking the week ------------------------------------------------------------
def share_between(w, t0, t1):
    """Expected share of weekly spend in [t0, t1)."""
    if t1 <= t0:
        return 0.0
    return sum(w[b] * (e - s) / HOUR for s, e, _, b in segments(t0, t1))


def active_lookback(awake, now, hours, limit_s):
    """Start time of the span ending now that holds `hours` awake hours."""
    need = hours * HOUR
    t = now
    floor_t = now - limit_s
    while t > floor_t and need > 0:
        hs = hour_start(t - 1e-6)
        seg = t - hs
        if awake[bin_of(hs)]:
            if seg >= need:
                return t - need
            need -= seg
        t = hs
    return max(t, floor_t)


def walk_to(w, t0, t1, rate_pts_per_share, budget):
    """Time when cumulative spend from t0 reaches `budget` points, or None.

    Spend accrues at rate_pts_per_share * w(h) per hour.
    """
    if budget <= 0:
        return t0
    if rate_pts_per_share <= 0:
        return None
    cum = 0.0
    for s, e, _, b in segments(t0, t1):
        add = rate_pts_per_share * w[b] * (e - s) / HOUR
        if cum + add >= budget and add > 0:
            return s + (budget - cum) / add * (e - s)
        cum += add
    return None


# -- incremental learning from weekly-percent readings --------------------------
def learn(store, prof, now, cfg, w_current=None):
    """Fold completed hours since prof.processed_until into the profile.

    Spend between consecutive fresh readings is spread over the gap: evenly
    in time when the gap is short (it is well localized), by the current
    profile when it is long (only the total is known) and then learned with
    low confidence. At a reset, a long tail of the old window after its last
    reading is learned as no spend, with low confidence, unless it was
    capped. An hour is final once a fresh reading after it exists.
    Returns the number of hours learned.
    """
    pc = cfg["profile"]
    hl = float(pc["half_life_days"]) * 86400
    max_gap = float(pc["max_gap_hours"]) * HOUR
    drop = float(cfg["reset"]["drop_points"])
    pu = prof.processed_until
    first = store.db.execute(
        "SELECT MIN(ts) FROM samples WHERE window=? AND flag=?", (SEVEN_DAY, FRESH)
    ).fetchone()[0]
    if first is None:
        return 0
    if pu is None:
        pu = hour_start(first)
    last = store.db.execute(
        "SELECT MAX(ts) FROM samples WHERE window=? AND flag=?", (SEVEN_DAY, FRESH)
    ).fetchone()[0]
    end = hour_start(last)
    if end <= pu:
        prof.processed_until = pu
        return 0
    anchor = store.db.execute(
        "SELECT ts, used_pct, window_id FROM samples WHERE window=? AND flag=? AND ts<? "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (SEVEN_DAY, FRESH, pu),
    ).fetchone()
    rows = store.fresh_series(SEVEN_DAY, pu - 1e-6 if anchor is None else anchor[0], end + 2 * max_gap + 1)
    if anchor is not None:
        rows = [anchor] + [r for r in rows if r[0] > anchor[0]]
    if w_current is None:
        w_current = uniform()
    spend, cover = {}, {}
    prev, run_max, starts = None, None, {}
    for ts, used, wid in rows:
        if prev is None:
            # Seed with the window's running max, not the anchor's own value:
            # a slightly lower reading from a parallel session would
            # otherwise count the difference again.
            m = store.db.execute(
                "SELECT MAX(used_pct) FROM samples WHERE window_id=? AND flag=? AND ts<=?", (wid, FRESH, ts)
            ).fetchone()[0]
            prev, run_max = (ts, used, wid), max(used, m if m is not None else used)
            continue
        if wid != prev[2]:
            if wid not in starts:
                inst = store.instance(wid)
                starts[wid] = inst["start_ts"] if inst else ts
            if starts[wid] < prev[0] and used > run_max - min(drop, max(1.0, 0.5 * run_max)):
                # A window that began before the previous reading at about the
                # same level (a moved or bogus reset, the real window
                # reopened): a continuation, not new spend. "About the same"
                # is closer to the old level than to 0 (a light user's early
                # reset drops less than drop_points), within a reading step.
                a, delta = prev[0], max(0.0, used - run_max)
                run_max = max(run_max, used)
            else:
                # A real reset (an early one too, whose start is rounded down
                # to the hour): the new window counts from 0.
                if starts[wid] - prev[0] > max_gap and run_max < 100.0 and not _capped(store, prev[2]):
                    # The old window's tail after its last reading: no
                    # reading, no news, no spend (D-11), at low confidence.
                    # Unlearned, those hours kept the prior's rate for good.
                    # Not after a cap: blocked hours say nothing of the habit.
                    _spread(spend, cover, prev[0], starts[wid], 0.0, False, w_current, pu, end)
                a, delta, run_max = max(prev[0], starts[wid]), used, used
        else:
            a, delta = prev[0], max(0.0, used - run_max)
            run_max = max(run_max, used)
        b = ts
        if b > a:
            _spread(spend, cover, a, b, delta, b - a <= max_gap, w_current, pu, end)
        prev = (ts, used, wid)
        if ts >= end:
            break
    n = 0
    for hs in sorted(cover):
        if pu <= hs < end:
            secs, weighted = cover[hs]
            if secs <= 0:
                continue
            conf = weighted / HOUR
            prof.observe(hs, spend.get(hs, 0.0) * HOUR / secs, conf, hl)
            n += 1
    prof.processed_until = end
    return n


def _capped(store, wid):
    """Whether a window reached 100%. A FIRST reading counts (D-06, as for
    the lookback, D-15): when the last points went on claude.ai, a new
    session's first reading may be the only one at 100."""
    m = store.db.execute(
        "SELECT MAX(used_pct) FROM samples WHERE window_id=? AND flag IN (?, ?)", (wid, FRESH, FIRST)
    ).fetchone()[0]
    return m is not None and m >= 100.0


def _spread(spend, cover, a, b, delta, short, w, lo, hi):
    segs = list(segments(a, b))
    if short:
        total = b - a
        shares = [(e - s) / total for s, e, _, _ in segs]
        conf = 1.0
    else:
        weights = [w[bn] * (e - s) for s, e, _, bn in segs]
        tot = sum(weights) or 1.0
        shares = [x / tot for x in weights]
        conf = LOW_CONFIDENCE
    for (s, e, hs, _), sh in zip(segs, shares):
        if hs < lo or hs >= hi:
            continue
        spend[hs] = spend.get(hs, 0.0) + delta * sh
        secs, weighted = cover.get(hs, (0.0, 0.0))
        cover[hs] = (secs + (e - s), weighted + conf * (e - s))


def heatmap_rows(w):
    """7 rows of 24 floats (Mon..Sun), each bin's weight times 168 (1.0 = uniform)."""
    return [[w[d * 24 + h] * BINS for h in range(24)] for d in range(7)]


def dumps(w):
    return json.dumps([round(x, 6) for x in w])
