#!/usr/bin/env python3
"""Time `glidepath statusline` the way Claude Code runs it: a fresh process per
render, payload on stdin, against a database with weeks of history.

    python3 scripts/bench_statusline.py [--runs 50] [--python /usr/bin/python3]
                                        [--home DIR]   # default: a seeded temp home

--home DIR records the bench's synthetic readings in DIR: give it a copy.

Prints p50 / p95 / max wall time in milliseconds for the interpreter's start
alone, a render that writes nothing (--no-record), and a render that records a
fresh reading (the budget: p95 under 100 ms); then, in this process, where a
render's time goes.
"""

import argparse
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from glidepath import clock, config, ingest, profile, winshell  # noqa: E402
from glidepath.store import Store  # noqa: E402


def seed(home, now, days=45):
    """Synthetic history: weekday work hours, a reading a minute while active."""
    os.environ["GLIDEPATH_HOME"] = home
    cfg = config.load()
    st = Store(os.path.join(home, "tempo.db"))
    rnd = random.Random(7)
    t = now - days * 86400
    week_reset = t + 3.5 * 86400  # not a whole number of weeks before now
    five_reset = None
    week_used = five_used = 0.0
    n = 0
    while t < now - 60:
        lt = clock.localtime(t)
        if t >= week_reset:
            week_reset += 7 * 86400
            week_used = 0.0
        active = lt.tm_wday < 5 and 9 <= lt.tm_hour < 19
        if active:
            if five_reset is None or t >= five_reset:
                five_reset, five_used = t + 5 * 3600, 0.0
            five_used = min(100.0, five_used + rnd.uniform(0, 0.6))
            week_used = min(100.0, week_used + rnd.uniform(0, 0.05))
            payload = {"session_id": "s%d" % (int(t) // 7200), "rate_limits": {
                "five_hour": {"used_percentage": round(five_used, 1), "resets_at": int(five_reset)},
                "seven_day": {"used_percentage": round(week_used, 1), "resets_at": int(week_reset)}}}
            r, rej, kinds = ingest.parse_payload(payload, t)
            ingest.record(st, r, t, "statusline", payload["session_id"], cfg, rej, kinds)
            n += 1
        t += 60
    prof = profile.load(st)
    st.begin()
    profile.learn(st, prof, now, cfg)
    profile.save(st, prof)
    st.meta_set("profile_checked_at", now)
    st.commit()
    rows = st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    st.close()
    return n, rows, five_reset, five_used, week_reset, week_used


STEP = 0.1  # the 5-hour level a recording render adds: a fresh reading each time


def pct(times, q):
    times = sorted(times)
    return times[min(len(times) - 1, int(round(q * (len(times) - 1))))]


def report(label, times):
    print("%-28s p50 %6.1f ms  p95 %6.1f ms  max %6.1f ms  (%d)" % (
        label, pct(times, 0.5), pct(times, 0.95), max(times), len(times)))


class _Pipe:
    """stdin or stdout of an in-process render: bytes in .buffer, never a tty."""

    def __init__(self, data=b""):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False

    def write(self, s):
        self.buffer.write(s.encode("utf-8"))

    def flush(self):
        pass


def phases(payload, runs):
    """Where a render's time goes, in this process (no interpreter start): each
    phase over `runs` renders that each record a fresh reading."""
    from glidepath import ingest, pace, profile, statusline, store, weekly

    spent = {}

    def timed(owner, name, label):
        f = getattr(owner, name)

        def run(*a, **kw):
            t = time.perf_counter()
            try:
                return f(*a, **kw)
            finally:
                spent.setdefault(label, []).append((time.perf_counter() - t) * 1000)

        setattr(owner, name, run)

    for owner, name, label in ((store.Store, "__init__", "  open the database"), (ingest, "record", "  record the reading"),
                               (profile, "load", "  load the profile"), (statusline, "maybe_learn", "  learn"),
                               (pace, "five_hour", "  5-hour math"), (weekly, "weekly", "  weekly math"),
                               (statusline, "note_render", "  note the line"), (store.Store, "close", "  close")):
        timed(owner, name, label)
    report("render in this process", renders(payload, runs))
    for label, ms in sorted(spent.items(), key=lambda kv: -sum(kv[1])):
        report(label, ms)
    # Closing the last connection checkpoints the WAL into the database and deletes it,
    # which costs most on Windows: the same renders with the store set up otherwise (on
    # Windows with Python 3.12+ the store already skips that checkpoint: D-05).
    import sqlite3

    init = store.Store.__init__
    tries = [("synchronous=OFF", lambda db: db.execute("PRAGMA synchronous=OFF"))]
    if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):  # Python 3.12+
        tries.append(("no checkpoint on close", lambda db: db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)))
    for label, tweak in tries:
        def tweaked(self, *a, **kw):
            init(self, *a, **kw)
            tweak(self.db)

        store.Store.__init__ = tweaked
        spent.clear()
        report("render, " + label, renders(payload, runs))
        for part in ("  close", "  record the reading"):
            report(part, spent[part])
    store.Store.__init__ = init


def renders(payload, runs):
    """`runs` renders in this process, each with a fresh reading: their times."""
    from glidepath import statusline

    total, saved = [], (sys.stdin, sys.stdout)
    try:
        for _ in range(runs):
            payload["rate_limits"]["five_hour"]["used_percentage"] += STEP
            sys.stdin, sys.stdout = _Pipe(json.dumps(payload).encode()), _Pipe()
            t = time.perf_counter()
            statusline.main([])
            total.append((time.perf_counter() - t) * 1000)
    finally:
        sys.stdin, sys.stdout = saved
    return total


def main():
    try:  # a pipe or a file on Windows gets the ANSI code page, which has no ▲ or →
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--home", default=None)
    a = ap.parse_args()
    now = time.time()
    tmp = None
    if a.home:
        home = a.home
        payload = {"session_id": "bench", "rate_limits": {
            "five_hour": {"used_percentage": 40, "resets_at": int(now + 7200)},
            "seven_day": {"used_percentage": 30, "resets_at": int(now + 3 * 86400)}}}
    else:
        tmp = home = tempfile.mkdtemp(prefix="glidepath-bench-")
        t = time.time()
        n, rows, fr, fu, wr, wu = seed(home, now)
        print("seeded %d readings -> %d sample rows in %.1fs" % (n, rows, time.time() - t))
        if fr is None or fr <= now:
            fr, fu = now + 7200, 40.0
        payload = {"session_id": "bench", "rate_limits": {
            "five_hour": {"used_percentage": round(fu, 1), "resets_at": int(fr)},
            "seven_day": {"used_percentage": round(wu, 1), "resets_at": int(wr)}}}
    # The in-process renders (phases) read GLIDEPATH_HOME from this process too.
    os.environ["GLIDEPATH_HOME"] = home
    try:
        run_all(a, home, payload)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def run_all(a, home, payload):
    global STEP
    # Up to 4 series of recording renders: stay below 100%, where a reading stops changing.
    five = payload["rate_limits"]["five_hour"]
    five["used_percentage"] = min(five["used_percentage"], 40.0)
    STEP = min(0.1, 55.0 / (4 * max(a.runs, 1)))
    env = dict(os.environ, GLIDEPATH_HOME=home)
    flags = winshell.python_flags()  # as installed
    argv = [a.python] + flags + [os.path.join(ROOT, "bin", "glidepath"), "statusline"]

    def series(cmd, fresh):
        times, out = [], b""
        for i in range(a.runs):
            if fresh:
                payload["rate_limits"]["five_hour"]["used_percentage"] += STEP  # a fresh reading each run
            t = time.perf_counter()
            p = subprocess.run(cmd, input=json.dumps(payload).encode(), capture_output=True, env=env)
            times.append((time.perf_counter() - t) * 1000)
            out = p.stdout
            if p.returncode != 0:
                print("run %d of %s exited %d" % (i, cmd[-1], p.returncode))
        return times, out

    print("python: %s" % a.python)
    report("interpreter start", series([a.python] + flags + ["-c", "pass"], False)[0])
    imports = ("import sys; sys.path.insert(0, %r); import json, sqlite3; "
               "from glidepath import config, ingest, pace, profile, render, statusline, store, weekly" % ROOT)
    report("start and imports", series([a.python] + flags + ["-c", imports], False)[0])
    report("render, no write", series(argv + ["--no-record"], False)[0])
    times, out = series(argv, True)
    report("render, a fresh reading", times)
    print("runs: %d  p50 %.1f ms  p95 %.1f ms  max %.1f ms" % (len(times), pct(times, 0.5), pct(times, 0.95), max(times)))
    print("last line: %s" % out.decode("utf-8", "replace").strip())
    phases(payload, a.runs)
    # The slowest imports of one render (cumulative microseconds, -X importtime).
    p = subprocess.run([a.python] + flags + ["-X", "importtime"] + argv[len(flags) + 1:], input=json.dumps(payload).encode(),
                       capture_output=True, env=env)
    rows = []
    for line in p.stderr.decode("utf-8", "replace").splitlines():
        parts = line.split("|")
        if len(parts) == 3 and parts[1].strip().isdigit():
            rows.append((int(parts[1]), parts[2].strip()))
    print("slowest imports (cumulative):")
    for us, name in sorted(rows, reverse=True)[:12]:
        print("  %7.1f ms  %s" % (us / 1000.0, name))


if __name__ == "__main__":
    main()
