import contextlib
import datetime
import json
import math
import os
import shutil
import tempfile
import time
import unittest

from glidepath import clock, config, ingest
from glidepath.store import Store

MON = 1791763200  # Monday 2026-10-12 00:00 UTC
H = 3600.0
D = 86400.0
# UTC as a TZ value in the POSIX form, which Windows' C runtime documents too
# (tzn[+|-]hh[:mm[:ss]][dzn]): a child process started with it runs in UTC.
TZ_UTC = "UTC0"


def utc():
    """Local time is UTC, on every OS: in this process through glidepath.clock;
    TZ goes to child processes (and to the time module where time.tzset
    exists), which read it at startup, Windows' C runtime included."""
    os.environ["TZ"] = TZ_UTC
    if hasattr(time, "tzset"):
        time.tzset()
    clock.localtime = time.gmtime


utc()  # the suite runs in UTC whatever the machine's zone; zone() steps out of it


def _zone_info(name):
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            from backports.zoneinfo import ZoneInfo  # Python 3.8
        except ImportError:
            return None
    try:
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError: no zone database (Windows without tzdata)
        return None


@contextlib.contextmanager
def zone(name):
    """Local time in the IANA zone `name` inside the block (DST tests).

    zoneinfo reads the system's zone database or the tzdata package (CI
    installs it on Windows, which has none); without zoneinfo (Python 3.8),
    TZ and time.tzset do it on POSIX. With neither, the test is skipped."""
    tz = _zone_info(name)
    saved = clock.localtime, os.environ.get("TZ")
    try:
        if tz is not None:
            # Floored to the second, as time.localtime does: fromtimestamp rounds.
            clock.localtime = lambda ts: datetime.datetime.fromtimestamp(math.floor(ts), tz).timetuple()
        elif hasattr(time, "tzset"):
            os.environ["TZ"] = name
            time.tzset()
            if time.timezone == 0 and not time.daylight:  # a name the system does not know reads as UTC
                raise unittest.SkipTest("no zone database has %s" % name)
            clock.localtime = time.localtime
        else:
            raise unittest.SkipTest("no zone database for %s: needs zoneinfo (Python 3.9+, or backports.zoneinfo) "
                                    "with the system's database or the tzdata package, or time.tzset" % name)
        yield
    finally:
        clock.localtime = saved[0]
        if saved[1] is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved[1]
        if hasattr(time, "tzset"):
            time.tzset()


def payload(five=None, week=None, sid="s1"):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if week is not None:
        rl["seven_day"] = {"used_percentage": week[0], "resets_at": week[1]}
    p = {"session_id": sid}
    if rl:
        p["rate_limits"] = rl
    return p


class TempHome(unittest.TestCase):
    """Each test gets its own GLIDEPATH_HOME and a fresh store."""

    def setUp(self):
        utc()
        self.dir = tempfile.mkdtemp(prefix="glidepath-test-")
        self.old_home = os.environ.get("GLIDEPATH_HOME")
        os.environ["GLIDEPATH_HOME"] = self.dir
        self.old_envs = {k: os.environ.pop(k) for k in config.OLD_ENVS if k in os.environ}  # never an old name's
        self.old_bg = os.environ.get("GLIDEPATH_NO_BACKGROUND")
        os.environ["GLIDEPATH_NO_BACKGROUND"] = "1"  # no real background bootstrap from a test's band run
        self.old_ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.dir, "claude")  # never the real ~/.claude
        self.cfg = config.load()
        self.st = Store(os.path.join(self.dir, "tempo.db"))

    def tearDown(self):
        self.st.close()
        if self.old_home is None:
            os.environ.pop("GLIDEPATH_HOME", None)
        else:
            os.environ["GLIDEPATH_HOME"] = self.old_home
        os.environ.update(self.old_envs)
        if self.old_bg is None:
            os.environ.pop("GLIDEPATH_NO_BACKGROUND", None)
        if self.old_ccd is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old_ccd
        shutil.rmtree(self.dir, ignore_errors=True)

    def use_home(self, name):
        """A fresh store in its own GLIDEPATH_HOME under the test's directory."""
        self.st.close()
        home = os.path.join(self.dir, name)
        os.makedirs(home)
        os.environ["GLIDEPATH_HOME"] = home
        self.st = Store(os.path.join(home, "tempo.db"))

    def feed(self, now, five=None, week=None, sid="s1", source="test"):
        readings, rejects, kinds = ingest.parse_payload(payload(five, week, sid), now)
        return ingest.record(self.st, readings, now, source, sid, self.cfg, rejects, kinds)

    def known(self, *sids):
        """Mark sessions as already heard from, so their next reading is timed (not FIRST).

        The row is dated far ahead: the first ingest prunes reader rows older
        than 7 days, which dropped a ts of 0 for every session not yet heard."""
        for sid in sids:
            for w in (config.FIVE_HOUR, config.SEVEN_DAY):
                self.st.reader_set(sid, w, -1.0, 0, 1e12)

    def late_start(self, t0, first, step, every, r5, week=None, renders=40):
        """Claude Code first reads the 5h window at `first`% at t0 (it was spent
        elsewhere before: claude.ai, another machine), then `step` points more
        every `every` seconds, with a render every 20 s. Yields each render's
        time once it is fed."""
        for k in range(renders):
            ts = t0 + 20 * k
            self.feed(ts, five=(min(100, first + step * int(20 * k // every)), r5), week=week)
            yield ts

    def work(self, t0, t1, R, rate, hours=(9, 18), days=(0, 1, 2, 3, 4), every=900, learn=False):
        """A user who spends `rate` weekly points an hour in local hours
        [hours[0], hours[1]) of `days` and nothing otherwise, from t0 (a
        window start, at 0%) to t1. A reading every `every` seconds while
        working, in whole percents, capped at 100; the weekly window resets
        at R and every 7 days after. With learn, the statusline's profile
        learning runs after each reading. Returns the next reset after t1."""
        from glidepath import profile, statusline

        prof = profile.load(self.st)
        level, t = 0.0, t0
        while t < t1:
            if t >= R:
                R += int(7 * D)
                level = 0.0
            lt = clock.localtime(t)
            if lt.tm_wday in days and hours[0] <= lt.tm_hour < hours[1]:
                level = min(100.0, level + rate * every / H)
                self.feed(t + every, week=(math.floor(level), R))
                if learn:
                    prof = statusline.maybe_learn(self.st, prof, t + every, self.cfg)
            t += every
        return R

    def rows(self, window):
        return self.st.db.execute(
            "SELECT ts, used_pct, resets_at, flag, window_id, session_id FROM samples WHERE window=? ORDER BY ts, id",
            (window,),
        ).fetchall()

    def windows(self, window):
        return self.st.instances(window)


def write_json(path, obj, indent=2, newline=True):
    with open(path, "w", encoding="utf-8", newline="") as f:  # LF on Windows too
        f.write(json.dumps(obj, indent=indent) + ("\n" if newline else ""))
