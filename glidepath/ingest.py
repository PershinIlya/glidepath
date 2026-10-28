"""Turn a statusline (or mod) payload into stored samples.

Rules, in order (see docs/design-decisions.md D-05..D-09):
  * sanitize: reject used% outside [0, 105] (the #52326 epoch leak lands in
    used_percentage), clamp to 100, reject resets_at that already passed or
    lies further ahead than the window length plus slack, accept ms epochs.
  * freshness: a reading is FRESH when it differs from what the same session
    reported last time; a session that has not talked to the API since only
    re-reports its old reading (HEARTBEAT), which proves time passed but
    carries no news and must never move the series. A session id can be
    shared by 2 processes: a changed reading that goes back to one the
    session already gave (its reading before last, or one it gave a window
    no longer open) is the idle one's replay, a heartbeat too.
  * window instance: resets_at within 300 s of the open instance is the same
    window (the epoch wobbles a few seconds between renders); a later
    resets_at rolls the window over (when the new window began before the
    last reading, at about the same level, the reset moved: the old
    instance closes as a shift).
  * reset detection: a fresh reading at least `drop_points` below the
    instance's max, with an earlier resets_at, or `drop_points` above the max
    of an instance a drop opened, is an ANOMALY. Only when `drop_samples`
    consecutive fresh readings agree, the last of them with its own
    session's previous reading too, is it a reset (or a rise); one is
    enough to undo a moved reset (a shift) from the same session, unless
    that reading sits below the session's reading of the moved reset or its
    own last one, or `drop_points` or more above the max of the instance it
    opened, and 1 is enough for a reset moved earlier, at about the same
    level, to a resets_at no instance had.
  * write throttle: a sample identical to the last stored one within
    `write_min_interval_s` is not stored again; heartbeats are stored at most
    once per interval across all sessions.
"""

import datetime
import math

from . import config
from .config import DURATION, WINDOWS
from .store import ANOMALY, FIRST, FRESH, HEARTBEAT

RESET_JITTER = 300  # seconds; ccpool's resetJitter
MAX_PCT = 105.0
FUTURE_SLACK = {config.FIVE_HOUR: 900, config.SEVEN_DAY: 3600}
MIN_EPOCH = 1.5e9  # 2017; anything earlier is not a real reset time


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def to_epoch(v):
    """Epoch seconds from seconds, milliseconds, a numeric string or ISO 8601."""
    f = _num(v)
    if f is None and isinstance(v, str) and v:
        try:
            s = v.strip().replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    if f is not None and f > 1e11:  # milliseconds
        f /= 1000.0
    return f


def sanitize(window, used, resets_at, now):
    """Return ((used, resets_at), None) or (None, reason)."""
    u = _num(used)
    if u is None:
        return None, "pct_missing"
    if u < 0 or u > MAX_PCT:
        return None, "pct_out_of_range"
    r = to_epoch(resets_at)
    if r is None:
        return None, "reset_missing"
    if r < MIN_EPOCH:
        return None, "reset_bad_epoch"
    if r <= now:  # Claude Code drops a window once resets_at passes; so do we
        return None, "expired"
    if r > now + DURATION[window] + FUTURE_SLACK[window]:
        return None, "reset_too_far"
    return (min(u, 100.0), int(round(r))), None


def parse_payload(obj, now):
    """Extract readings from a statusline-shaped payload.

    Returns (readings, rejects, kinds): readings maps window -> (used, reset),
    rejects maps window -> reason, kinds lists every rate-limit kind seen
    (statusline keys plus the mod's `kinds`, so unknown ones get logged).
    """
    readings, rejects, kinds = {}, {}, []
    if not isinstance(obj, dict):
        return readings, rejects, kinds
    rl = obj.get("rate_limits")
    if isinstance(rl, dict):
        kinds.extend(str(k) for k in rl.keys())
        for w in WINDOWS:
            entry = rl.get(w)
            if not isinstance(entry, dict):
                continue
            ok, why = sanitize(w, entry.get("used_percentage"), entry.get("resets_at"), now)
            if ok:
                readings[w] = ok
            else:
                rejects[w] = why
    extra = obj.get("kinds")
    if isinstance(extra, list):
        kinds.extend(str(k) for k in extra if isinstance(k, (str, int, float)))
    seen, uniq = set(), []
    for k in kinds:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return readings, rejects, uniq


def record(store, readings, now, source, session_id, cfg, rejects=None, kinds=None):
    """Store readings in one IMMEDIATE transaction. Returns per-window actions."""
    actions = {}
    store.begin()
    try:
        store.close_expired(now)
        for w in WINDOWS:
            if w in readings:
                used, reset = readings[w]
                actions[w] = _record_one(store, w, used, reset, now, source, session_id, cfg)
        for w, why in (rejects or {}).items():
            store.meta_incr("rejected:%s:%s" % (w, why))
            actions[w] = "rejected:" + why
        for k in kinds or ():
            _note_kind(store, k, source, now)
        last_prune = store.meta_get("last_prune", 0) or 0
        if now - last_prune > 86400:
            store.prune(now, float(cfg.get("retention_days", 365)))
            store.meta_set("last_prune", now)
        store.commit()
    except Exception:
        store.rollback()
        raise
    return actions


def _note_kind(store, kind, source, now):
    row = store.db.execute("SELECT last_seen FROM kinds WHERE kind=?", (kind,)).fetchone()
    if row is None or now - (row[0] or 0) > 3600:
        store.note_kind(kind, source, now)


def _same(a_used, a_reset, b_used, b_reset):
    return abs(a_used - b_used) < 1e-9 and abs(a_reset - b_reset) <= RESET_JITTER


def _gave(store, w, inst, reader, prev, before, used, reset):
    """Whether this session already gave this reading: as its reading before
    last, or to a window that is no longer open (superseded by a moved or an
    early reset)."""
    if before is not None and _same(before[0], before[1], used, reset):
        return True
    if inst is not None and abs(inst["reset_ts"] - reset) <= RESET_JITTER:
        return False  # the open window: a real drop reset climbs through old levels again
    return (
        store.db.execute(
            "SELECT 1 FROM samples s JOIN windows i ON i.id = s.window_id WHERE s.window=? AND s.ts>=? "
            "AND i.closed_at IS NOT NULL AND COALESCE(s.session_id, '~' || s.source)=? "
            "AND ABS(s.used_pct - ?) < 1e-9 AND ABS(s.resets_at - ?) <= ? LIMIT 1",
            (w, reset - DURATION[w] - FUTURE_SLACK[w] - RESET_JITTER, reader, used, reset, RESET_JITTER),
        ).fetchone()
        is not None
    )


def _left_later(prev, before, reset):
    """Whether the session's last reading, or its reading before last (the
    busy process's, when an idle one rendered in between), had a later
    reset than this one."""
    return prev[1] > reset + RESET_JITTER or (before is not None and before[1] > reset + RESET_JITTER)


def _replay(store, w, inst, reader, prev, before, used, reset):
    """Whether a changed reading goes back to one this session already gave.

    A single process only moves forward: within a window its level only
    rises, a moved reset keeps the level, and it returns to a resets_at it
    left only to undo its own bogus later one. A session that goes back is 2
    processes sharing the session id (`claude -c` or `--resume` without
    `--fork-session`): the idle one keeps re-rendering its last API response,
    and each of its renders differs from the reader row the busy one wrote
    (D-06)."""
    if _left_later(prev, before, reset):
        return False  # back to an earlier reset: _anomaly judges it (D-10)
    if prev[1] < reset - RESET_JITTER and abs(used - prev[0]) < 1e-9:
        return False  # back to a later reset at the level it left: the reset moved back
    if inst is not None and abs(inst["reset_ts"] - reset) <= RESET_JITTER:
        # The open window's reset: only A, B, A within it. Another reset in
        # between (the stale one) leaves this reading's render as news that
        # breaks the stale one's run.
        return (
            before is not None
            and abs(prev[1] - reset) <= RESET_JITTER
            and _same(before[0], before[1], used, reset)
        )
    return _gave(store, w, inst, reader, prev, before, used, reset)


def _record_one(store, w, used, reset, now, source, session_id, cfg):
    reader = session_id or ("~" + source)
    prev = store.reader_get(reader, w)
    first_seen = prev is None
    changed = first_seen or not _same(prev[0], prev[1], used, reset)
    inst = store.open_instance(w)
    fresh, before = changed, None
    if changed and not first_seen:
        before = store.reader_get(reader + store.BEFORE_LAST, w)
        store.reader_set(reader + store.BEFORE_LAST, w, prev[0], prev[1], now)
        # A replay is a heartbeat; the rows still move on, so the other
        # process's next render is known too.
        fresh = not _replay(store, w, inst, reader, prev, before, used, reset)
        if not fresh and inst is not None and abs(inst["reset_ts"] - reset) > RESET_JITTER:
            # Its first render was no news either: if it is pending in a
            # run, a later real reset must not sweep it in. At the open
            # window's reset A, B, A is symmetric (the busy process's
            # re-render of its own new reading matches it too): a drop or a
            # rise is guarded where its run completes instead.
            store.withdraw_anomalies(inst["id"], reader, used, reset, RESET_JITTER)
    if changed or now - prev[2] > 86400:
        store.reader_set(reader, w, used, reset, now)

    drop_pts = float(cfg["reset"]["drop_points"])
    if inst is None:
        if not fresh:
            return "stale"  # its window was already judged and closed
        # resets_at wobbles: a reading of a window that just expired can carry
        # a reset a few seconds later than the one stored. Not a new window.
        old = store.db.execute(
            "SELECT id FROM windows WHERE window=? AND close_reason='expired' AND ABS(reset_ts - ?) <= ? LIMIT 1",
            (w, reset, RESET_JITTER),
        ).fetchone()
        if old is not None:
            return "expired"
        inst = store.new_instance(w, reset - DURATION[w], reset, now, used)
        store.add_sample(now, w, used, reset, source, session_id, FIRST if first_seen else FRESH, inst["id"])
        return "new_window"

    if reset > inst["reset_ts"] + RESET_JITTER:
        if not fresh:
            # A heartbeat repeats a reading already judged; after a bogus
            # later reset was undone, it must not redo the rollover.
            return "stale"
        # The window rolled over (or reset early): close and open the next one.
        # Begun before the last reading, at about the same level (closer to
        # the old level than to 0), the reset moved later: that window never
        # ended. The continuation test learning uses (D-13): one that began
        # after the last reading is a real reset, whatever its level.
        m = inst["max_used"]
        moved = reset - DURATION[w] < inst["last_seen"] and used > m - min(drop_pts, max(1.0, 0.5 * m))
        store.close_instance(inst["id"], min(now, inst["reset_ts"]), "shift" if moved else "rollover")
        inst = store.new_instance(w, reset - DURATION[w], reset, now, used)
        store.add_sample(now, w, used, reset, source, session_id, FIRST if first_seen else FRESH, inst["id"])
        return "rollover"

    shifted = reset < inst["reset_ts"] - RESET_JITTER
    dropped = used <= inst["max_used"] - drop_pts
    if shifted or dropped:
        if not fresh or first_seen:
            # An idle session re-reporting an old window, or one glidepath has
            # never heard from (e.g. every open session re-renders when the
            # statusLine is installed): its first word cannot count as news.
            return "stale"
        kind = "shift" if shifted else "drop"
        # Back to an earlier reset from a session that last read a later one:
        # an idle twin's replay when the session already gave this reading,
        # or perhaps when it is the session's first change (its reading
        # before last unknown, as at install).
        replay = shifted and _left_later(prev, before, reset)
        replay = replay and (before is None or _gave(store, w, inst, reader, prev, before, used, reset))
        return _anomaly(store, w, inst, used, reset, now, source, session_id, cfg, kind, prev, replay)

    if not fresh and used > inst["max_used"]:
        # A heartbeat never raises the max: after a drop-reset an idle
        # session may still carry the pre-reset value.
        return "stale"
    opened_by_drop = inst["reset_ts"] - inst["start_ts"] < DURATION[w] - 1
    if fresh and not first_seen and used >= inst["max_used"] + drop_pts and opened_by_drop:
        # A drop reset opened this window (it starts at the drop): a jump of
        # drop_points over it may be a twin's pre-reset reading, which takes
        # drop_samples readings to agree, like the drop itself (D-10).
        return _anomaly(store, w, inst, used, reset, now, source, session_id, cfg, "rise", prev, False)

    last = store.last_sample(w)
    if fresh:
        dup = (
            last is not None
            and last[4] == inst["id"]
            and _same(last[1], last[2], used, reset)
            and now - last[0] < cfg["write_min_interval_s"]
        )
        if not dup:
            # A session's first word may be an old reading (every open session
            # re-renders when the statusLine is installed): keep its level,
            # but not as a timed point.
            store.add_sample(now, w, used, reset, source, session_id, FIRST if first_seen else FRESH, inst["id"])
        # last_used follows the running max: a slightly lower fresh reading
        # from another session is eventual-consistency noise, not a refund.
        store.touch_instance(inst["id"], now, max(used, inst["max_used"]))
        return "dup" if dup else "fresh"
    if last is None or now - last[0] >= cfg["write_min_interval_s"]:
        # last_seen is not touched: a heartbeat says nothing about the level.
        store.add_sample(now, w, used, reset, source, session_id, HEARTBEAT, inst["id"])
        return "heartbeat"
    return "throttled"


def _undoes_rollover(store, w, inst, used, reset, source, session_id):
    """Whether the open instance came from this session's own moved resets_at,
    and this reading is from the window that the instance's rollover closed."""
    # A rollover closes the old window at the moment it opens the new one, as
    # a shift when the reset moved. One closed as a rollover is what a real
    # early reset looks like when the new window shows a lower level; when the
    # bogus reading sat at or above the closed window's max (a maxed session
    # reads no fresh value, so its bogus reset can begin after its last one),
    # it is undone all the same.
    # Walk back through the instances this session's own rollovers opened (a
    # maxed session can read several bogus later resets before its real one).
    me = session_id or "~" + source
    cur, top = inst, 0.0
    for _ in range(16):
        opener = store.db.execute(
            "SELECT session_id, source, used_pct FROM samples WHERE window_id=? ORDER BY ts, id LIMIT 1", (cur["id"],)
        ).fetchone()
        # A session's real reading is never below its own reading of the moved
        # reset; an idle twin's stale one (two processes can share a session
        # id) can be.
        if opener is None or (opener[0] or "~" + opener[1]) != me:
            return False
        top = max(top, opener[2])
        if used < top:
            return False
        prev = store.db.execute(
            "SELECT id, close_reason, max_used, reset_ts FROM windows WHERE window=? AND id<? "
            "AND close_reason IN ('rollover', 'shift') AND closed_at >= ? ORDER BY id DESC LIMIT 1",
            (w, cur["id"], cur["first_seen"]),
        ).fetchone()
        if prev is None:
            return False
        # Every link, not only the last: a rollover below its window's max is
        # what a real early reset looks like, and the chain stops there.
        if not (prev[1] == "shift" or opener[2] >= (prev[2] or 0.0)):
            return False
        if abs(prev[3] - reset) <= RESET_JITTER:
            return True
        cur = store.instance(prev[0])
    return False


def _anomaly(store, w, inst, used, reset, now, source, session_id, cfg, kind, prev, replay):
    """A fresh reading at odds with the open window: an earlier resets_at
    ("shift"), drop_points below its max ("drop"), or drop_points above the
    max of a window a drop opened ("rise"). Pending until drop_samples
    consecutive fresh readings agree."""
    shifted = kind == "shift"
    m = inst["max_used"]
    drop_pts = float(cfg["reset"]["drop_points"])
    need = max(1, int(cfg["reset"]["drop_samples"]))
    look = need - 1  # other sessions' pending readings are gathered either way
    # A reset moved earlier keeps the level (D-08's test, and less than
    # drop_points above), and a process's level never goes below its own
    # last reading of the open window.
    floor = prev[0] if abs(prev[1] - inst["reset_ts"]) <= RESET_JITTER else 0.0
    moved = floor <= used and m - min(drop_pts, max(1.0, 0.5 * m)) < used < m + drop_pts
    if shifted and floor <= used < m + drop_pts and _undoes_rollover(store, w, inst, used, reset, source, session_id):
        # One fresh reading did the bogus rollover, so one undoes it: a
        # session that is maxed or idle repeats its real reading only as
        # heartbeats, and the second fresh one never came (D-10). Another
        # session still needs drop_samples (it may lag a real early reset),
        # and so does a window closed as a rollover (a real early reset looks
        # like that), or a reading below the moved reset's, below the
        # session's own last reading of it or drop_points above the
        # instance: two processes can share a session id, and the idle one
        # re-renders the old window.
        need = 1
    elif (
        shifted
        and moved
        and store.db.execute(
            "SELECT 1 FROM windows WHERE window=? AND ABS(reset_ts - ?) <= ? LIMIT 1", (w, reset, RESET_JITTER)
        ).fetchone()
        is None
    ):
        # The reset moved earlier, to a resets_at no window ever had: a stale
        # reading (a lagging session's, an idle twin's) carries a known one.
        # It takes 1 reading, as a move later does: a maxed or idle session
        # would never send the second.
        need = 1
    # The previous fresh-or-anomaly readings of this window, newest first.
    rows = store.db.execute(
        "SELECT id, ts, used_pct, resets_at, flag, window_id FROM samples "
        "WHERE window=? AND flag IN (?, ?, ?) ORDER BY ts DESC, id DESC LIMIT ?",
        (w, FRESH, FIRST, ANOMALY, look),
    ).fetchall()
    run = []
    for r in rows:
        if r[4] != ANOMALY or r[5] != inst["id"] or abs(r[3] - reset) > RESET_JITTER:
            break
        if (kind == "drop" and r[2] > m - drop_pts) or (kind == "rise" and r[2] < m + drop_pts):
            break  # a pending reading of the other kind
        run.append(r)
    # The reading that completes a run must agree with its own session's
    # last one: 2 processes can share a session id, and an idle one's
    # re-render goes straight from the busy one's reading of the open window
    # to the anomaly. Its word joins a run but cannot complete it. A drop or
    # a rise agrees when the session's last reading was one too. An earlier
    # resets_at that may be a replay agrees only when it looks like that
    # reset moved earlier.
    if shifted:
        agrees = moved or not replay
    else:
        alike = prev[0] <= m - drop_pts if kind == "drop" else prev[0] >= m + drop_pts
        # ... and moved forward from it, as a single process does: a twin's
        # stale reading in between is not the completing process's own.
        agrees = abs(prev[1] - reset) <= RESET_JITTER and alike and used > prev[0]
    if len(run) + 1 < need or (need > 1 and not agrees):
        store.add_sample(now, w, used, reset, source, session_id, ANOMALY, inst["id"])
        return "anomaly"
    if kind == "rise":
        store.move_samples([r[0] for r in run], inst["id"], FRESH)
        store.add_sample(now, w, used, reset, source, session_id, FRESH, inst["id"])
        store.touch_instance(inst["id"], now, max(used, m))
        store.recompute_max(inst["id"])
        return "rise"
    first_ts = min([r[1] for r in run] + [now])
    store.close_instance(inst["id"], first_ts, kind)
    if shifted:
        # The open window came from a bogus later resets_at. If the real
        # window was closed by it, reopen that one instead of starting over.
        old = store.db.execute(
            "SELECT id FROM windows WHERE window=? AND id!=? AND closed_at IS NOT NULL "
            "AND ABS(reset_ts - ?) <= ? AND closed_at >= ? ORDER BY id DESC LIMIT 1",
            (w, inst["id"], reset, RESET_JITTER, now - DURATION[w]),
        ).fetchone()
        if old is not None:
            store.reopen_instance(old[0])
            # Every bogus instance between the real window and this one never ended.
            store.db.execute(
                "UPDATE windows SET close_reason='shift' WHERE window=? AND id>? AND id<? AND close_reason='rollover'",
                (w, old[0], inst["id"]),
            )
            store.move_samples([r[0] for r in run], old[0], FRESH)
            store.add_sample(now, w, used, reset, source, session_id, FRESH, old[0])
            store.recompute_max(old[0])
            return "reset_reopen"
    start = reset - DURATION[w] if shifted else first_ts
    new = store.new_instance(w, start, reset, now, used)
    store.move_samples([r[0] for r in run], new["id"], FRESH)
    store.add_sample(now, w, used, reset, source, session_id, FRESH, new["id"])
    store.recompute_max(new["id"])
    store.set_first_seen(new["id"], first_ts)
    return "reset_" + kind
