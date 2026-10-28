"""The statusline as Claude Code runs it: a subprocess fed a payload on stdin."""

import io
import json
import ntpath
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from glidepath import config, render, statusline, winshell

from .helpers import MON, H, D, TZ_UTC, TempHome, payload, utc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "glidepath")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BAR = re.compile(r"\b(5h|Week|Wk) [━╍─┈#=.-]+ ")


def text(s):
    """The line without colors and bars: "5h ━━━╍╍╍── 42%" reads "5h 42%"."""
    return BAR.sub(r"\1 ", ANSI.sub("", s))
WINDOWS = os.name == "nt"
# The compose tests are written in POSIX shell. On Windows they run in Git
# Bash, as Claude Code runs a statusLine there; without it they cannot. Git
# for Windows in its default folder runs them whatever predict_shell says, so
# a prediction that misses it fails them instead of skipping them.
posix_shell = unittest.skipUnless(
    not WINDOWS or os.path.exists(r"C:\Program Files\Git\bin\bash.exe") or winshell.predict_shell()[0] == "bash",
    "needs Git Bash (the compose commands are POSIX shell)")
# A compose timeout well above a normal render (two process starts plus the
# 0.5 s quiet window) keeps "it ended well before the timeout" measurable on a
# loaded runner; Git Bash and process startup are slower on Windows. On POSIX
# the 3 s background jobs in these tests outlast the limit, so a render that
# waited for them fails it; the Windows limit is above 3 s, and there
# WindowsComposer's 4 s silent job is the one a reader waiting for EOF fails.
QUICK_TIMEOUT_MS, QUICK_LIMIT = (5000, 3.5) if WINDOWS else (3000, 2.5)


def sh_path(path):
    """A path as a word of a POSIX shell command: forward slashes (Git Bash
    drops backslashes), quoted."""
    return shlex.quote(path.replace("\\", "/"))


def run(payload, home, args=(), env=None, raw=None):
    e = dict(os.environ)
    e.update({"GLIDEPATH_HOME": home, "TZ": TZ_UTC, "CLAUDE_CONFIG_DIR": os.path.join(home, "claude")})
    e.pop("NO_COLOR", None)
    e.update(env or {})
    data = raw if raw is not None else json.dumps(payload).encode()
    t = time.time()
    p = subprocess.run([sys.executable, "-I", "-S", ENTRY, "statusline"] + list(args), input=data,
                       capture_output=True, env=e, timeout=30)
    return p, time.time() - t


class Statusline(unittest.TestCase):
    def setUp(self):
        utc()
        self.home = tempfile.mkdtemp(prefix="glidepath-sl-")
        self.now = time.time()
        self.p = {
            "session_id": "abc",
            "rate_limits": {
                "five_hour": {"used_percentage": 42, "resets_at": int(self.now + 2 * H)},
                "seven_day": {"used_percentage": 31.5, "resets_at": int(self.now + 3 * D)},
            },
        }

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_prints_one_line_and_records(self):
        p, _ = run(self.p, self.home)
        self.assertEqual(p.returncode, 0)
        out = p.stdout.decode()
        self.assertEqual(out.count("\n"), 1)
        plain = text(out.strip())
        self.assertTrue(plain.startswith("5h 42%"), plain)
        self.assertIn("Week 31%", plain)  # 31.5, rounded down as Claude Code shows it
        self.assertLessEqual(len(plain), 80)
        self.assertEqual(p.stderr, b"")
        self.assertTrue(os.path.exists(os.path.join(self.home, "tempo.db")))

    def test_no_color(self):
        p, _ = run(self.p, self.home, env={"NO_COLOR": "1"})
        self.assertNotIn(b"\x1b", p.stdout)

    def test_garbage_and_empty_input_fail_open(self):
        for raw in (b"", b"not json", b"[1,2]", b'{"rate_limits": {"five_hour": {"used_percentage": "x"}}}'):
            p, _ = run(None, self.home, raw=raw)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout.count(b"\n"), 1)
            self.assertNotIn(b"Traceback", p.stdout + p.stderr)

    def test_unwritable_home_degrades_to_bare_line(self):
        blocker = os.path.join(self.home, "file")
        open(blocker, "w").close()
        p, _ = run(self.p, os.path.join(blocker, "sub"))
        self.assertEqual(p.returncode, 0)
        plain = text(p.stdout.decode().strip())
        self.assertTrue(plain.startswith("5h 42% @"), plain)
        self.assertNotIn(b"Traceback", p.stdout + p.stderr)

    def test_bare_line_follows_the_display_settings(self):
        # Review 4: with a broken database the bare line joined with " · " and
        # was never fitted: "5h 42% @HH:MM · wk 32% @Ddd HH:MM" is 33 columns.
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"display": {"symbols": "ascii", "max_width": 24}}, f)
        with open(os.path.join(self.home, "tempo.db"), "wb") as f:
            f.write(b"not a database " * 100)
        for args in ((), ("--format", "json")):
            p, _ = run(self.p, self.home, args=args)
            out = p.stdout.decode().strip()
            line = json.loads(out)["line"] if args else text(out)
            self.assertTrue(line.startswith("5h 42% @"), line)
            self.assertEqual(line[13:], " | wk 31...")  # 21 columns kept, then "..."
            self.assertLessEqual(len(line), 24)

    def test_waiting_line_follows_the_display_settings(self):
        # Review 4: "glidepath: waiting for usage data" (33 columns) ignored max_width.
        no_limits = {"session_id": "abc"}
        for symbols, want in (("unicode", "glidepath: waiting for …"), ("ascii", "glidepath: waiting fo...")):
            with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
                json.dump({"display": {"symbols": symbols, "max_width": 24}}, f)
            p, _ = run(no_limits, self.home, env={"NO_COLOR": "1"})
            self.assertEqual(p.stdout.decode("utf-8"), want + "\n")

    def test_bad_config_types_fall_back_to_defaults(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"verdict": 5, "display": {"max_width": "wide", "symbols": "ascii"}, "reset": None,
                       "target_5h": "lots", "min_free": True, "future_key": 1}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["verdict"], config.DEFAULTS["verdict"])
        self.assertEqual(cfg["display"]["max_width"], 80)
        self.assertEqual(cfg["display"]["symbols"], "ascii")
        self.assertEqual(cfg["target_5h"], 100.0)
        self.assertEqual(cfg["min_free"], 10.0)
        self.assertEqual(cfg["future_key"], 1)
        p, _ = run(self.p, self.home)
        self.assertTrue(text(p.stdout.decode()).startswith("5h 42%"), p.stdout)

    def test_non_utf8_stdout_encoding_still_prints(self):
        e = dict(os.environ, GLIDEPATH_HOME=self.home, TZ=TZ_UTC, PYTHONIOENCODING="latin-1")
        e.pop("NO_COLOR", None)
        p = subprocess.run([sys.executable, "-S", ENTRY, "statusline"], input=json.dumps(self.p).encode(),
                           capture_output=True, env=e, timeout=30)
        out = p.stdout.decode("utf-8")
        self.assertIn("━", out)
        self.assertTrue(text(out).startswith("5h 42%"))

    def compose_timeout(self, ms):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"compose": {"timeout_ms": ms}}, f)

    @posix_shell
    def test_bad_columns_keeps_the_composed_line(self):
        for cols in ("abc", "80.0", "-5", ""):
            p, _ = run(self.p, self.home, args=["--compose", "printf theirs"], env={"COLUMNS": cols})
            self.assertTrue(text(p.stdout.decode()).startswith("theirs"), (cols, p.stdout))

    def test_invalid_config_values_are_dropped_or_clamped(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"profile": {"override": {"work_days": ["Mon", "Tue"], "wake_hours": [8, 23]}, "half_life_days": 0},
                       "compose": {"command": True, "timeout_ms": 1e9}, "display": {"max_width": 3, "symbols": "emoji"},
                       "target_5h": float("inf") if False else 100}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["profile"]["override"]["work_days"], [0, 1, 2, 3, 4])  # bad field: default
        self.assertEqual(cfg["profile"]["override"]["wake_hours"], [8, 23])
        self.assertEqual(cfg["profile"]["half_life_days"], 0.5)
        self.assertIsNone(cfg["compose"]["command"])
        self.assertEqual(cfg["compose"]["timeout_ms"], 10000)
        self.assertEqual(cfg["display"]["max_width"], 20)
        self.assertEqual(cfg["display"]["symbols"], "unicode")
        p, _ = run(self.p, self.home)
        line = text(p.stdout.decode()).strip()
        self.assertTrue(line.startswith("5h 42%"), line)  # full math, not the bare fallback
        self.assertLessEqual(len(line), 20)
        self.assertNotIn("error", line)

    def test_override_fields_are_checked_one_by_one_and_empty_surfaces_kept(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"profile": {"override": {"work_days": [0, 1, 2.0, 9, "x"], "wake_hours": [7.5, 23],
                                                "off_day_factor": 3, "mode": "REPLACE"}},
                       "mod": {"surfaces": []}}, f)
        cfg = config.load(os.path.join(self.home, "config.json"))
        self.assertEqual(cfg["profile"]["override"],
                         {"work_days": [0, 1, 2], "wake_hours": [7, 23], "off_day_factor": 1.0, "mode": "replace"})
        self.assertEqual(cfg["mod"]["surfaces"], [])
        p, _ = run(self.p, self.home, args=["--source", "mod", "--format", "json"])
        self.assertEqual(json.loads(p.stdout)["surfaces"], [])

    def test_config_with_a_byte_order_mark_is_read(self):
        # Windows PowerShell 5.1 (Set-Content -Encoding UTF8) and older
        # Notepad start a UTF-8 file with a BOM; json.load refused it and the
        # whole config fell back to the defaults.
        with open(os.path.join(self.home, "config.json"), "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"display": {"symbols": "ascii"}}).encode("utf-8"))
        self.assertEqual(config.load(os.path.join(self.home, "config.json"))["display"]["symbols"], "ascii")

    def test_pathological_config_falls_back_to_defaults(self):
        with open(os.path.join(self.home, "config.json"), "w", encoding="utf-8", newline="") as f:
            f.write("[" * 200000 + "]" * 200000)
        self.assertEqual(config.load(os.path.join(self.home, "config.json")), config.DEFAULTS)
        p, _ = run(self.p, self.home)
        self.assertTrue(text(p.stdout.decode()).startswith("5h 42%"))

    def test_error_json_still_tells_the_mod_where_to_draw(self):
        from contextlib import redirect_stdout
        from unittest import mock

        from glidepath import statusline

        class Out:
            def __init__(self):
                self.buffer = io.BytesIO()

        o = Out()
        with mock.patch.dict(os.environ, {"GLIDEPATH_HOME": self.home}), mock.patch.object(render, "plain", side_effect=RuntimeError("x")), \
                mock.patch.object(sys, "stdout", o), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(self.p).encode()))):
            statusline.main(["--format", "json"])
        d = json.loads(o.buffer.getvalue())
        self.assertFalse(d["ok"])
        self.assertEqual(d["surfaces"], ["desktop", "terminal"])  # no `glidepath install` here: the plugin alone
        self.assertEqual(d["refresh_s"], 60)

    def test_epoch_leak_payload_is_not_shown(self):
        self.p["rate_limits"]["seven_day"]["used_percentage"] = 1783720704
        p, _ = run(self.p, self.home)
        plain = text(p.stdout.decode())
        self.assertNotIn("1783720704", plain)
        self.assertIn("Week waiting", plain)

    def test_only_a_reading_that_changes_the_band_touches_the_stamp(self):
        # Review 18: a rejected window (no resets_at, one already passed, the
        # epoch leak) touched the stamp on every run, so 2 Desktop chats kept
        # waking each other every 5 s.
        stamp = os.path.join(self.home, "changed")
        run(self.p, self.home, args=["--source", "mod", "--format", "json"])
        with open(stamp, encoding="utf-8") as f:
            first = f.read()
        bad = json.loads(json.dumps(self.p))
        del bad["rate_limits"]["seven_day"]["resets_at"]
        for p_ in (bad, bad, self.p):  # the 5h reading repeats (a heartbeat), the week is rejected, then repeats
            run(p_, self.home, args=["--source", "mod", "--format", "json"])
            with open(stamp, encoding="utf-8") as f:
                self.assertEqual(f.read(), first)
        self.assertFalse(statusline.shown_changed("rejected:reset_missing"))
        self.assertFalse(statusline.shown_changed("anomaly"))
        self.assertTrue(statusline.shown_changed("reset_drop"))

    def test_json_format_for_the_mod(self):
        p, _ = run(self.p, self.home, args=["--source", "mod", "--format", "json"])
        d = json.loads(p.stdout)
        self.assertTrue(d["ok"])
        self.assertTrue(text(d["line"]).startswith("5h 42%"), d["line"])
        self.assertEqual("".join(s["text"] for s in d["segments"]), d["line"])
        self.assertIn(d["segments"][0]["tone"], tuple(render.ANSI))
        self.assertIn("desktop", d["surfaces"])
        h5, wk = d["windows"]
        self.assertEqual((h5["id"], h5["name"], h5["pct"]), ("h5", "5h", 42))
        self.assertEqual((wk["id"], wk["name"], wk["pct"]), ("wk", "Week", 31))
        self.assertTrue(h5["sentence"].startswith("5-hour: 42% used, resets "), h5["sentence"])
        self.assertAlmostEqual(d["updated_ts"], d["now"], delta=5)

    @posix_shell
    def test_compose_appends_to_existing_command(self):
        p, _ = run(self.p, self.home, args=["--compose", "printf 'theirs'"], env={"COLUMNS": "200"})
        plain = text(p.stdout.decode())
        self.assertTrue(plain.startswith("theirs  5h 42%"), plain)

    @posix_shell
    def test_compose_drops_carriage_returns(self):
        # A Windows command ends its lines with CRLF (print() in Python there):
        # the \r left before our segment sent the cursor back over the user's
        # text, since Claude Code trims only the ends of each line.
        p, _ = run(self.p, self.home, args=["--compose", "printf 'theirs\\r\\nmore\\r\\n'"], env={"COLUMNS": "200"})
        out = p.stdout.decode()
        self.assertNotIn("\r", out)
        self.assertTrue(text(out).startswith("theirs\nmore  5h 42%"), out)

    @posix_shell
    def test_compose_passes_stdin_through(self):
        cmd = "%s -c \"import sys, json; print(json.load(sys.stdin)['session_id'])\"" % sh_path(sys.executable)
        p, _ = run(self.p, self.home, args=["--compose", cmd], env={"COLUMNS": "200"})
        self.assertTrue(text(p.stdout.decode()).startswith("abc  5h"), p.stdout)

    @posix_shell
    def test_compose_wraps_when_too_wide(self):
        p, _ = run(self.p, self.home, args=["--compose", "printf '%080d' 0"], env={"COLUMNS": "100"})
        lines = text(p.stdout.decode()).rstrip("\n").split("\n")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("5h 42%"))

    @posix_shell
    def test_compose_failure_and_timeout_show_ours(self):
        self.compose_timeout(1500)  # the default is 3000 on Windows
        p, _ = run(self.p, self.home, args=["--compose", "exit 3"])
        self.assertTrue(text(p.stdout.decode()).startswith("5h 42%"))
        p, el = run(self.p, self.home, args=["--compose", "sleep 5; echo late"])
        self.assertTrue(text(p.stdout.decode()).startswith("5h 42%"))
        self.assertLess(el, 4.0)

    @posix_shell
    def test_compose_leaves_a_background_job_alone(self):
        # Review 7 (fresh-eyes#0): a background job that inherits stdout kept
        # the pipe open, so the render waited for EOF until the 1.5 s timeout,
        # dropped "hi" and SIGKILLed the job with the group. Like Claude Code,
        # the shell's exit plus a quiet window ends the read; only a shell
        # still running at the timeout is killed, with its children.
        left = os.path.join(self.home, "left")
        killed = os.path.join(self.home, "killed")
        t = time.time()
        self.compose_timeout(QUICK_TIMEOUT_MS)
        p, el = run(self.p, self.home, args=["--compose", "echo hi; (sleep 3; touch %s) &" % sh_path(left)],
                    env={"COLUMNS": "200"})
        self.assertTrue(text(p.stdout.decode()).startswith("hi  5h 42%"), p.stdout)
        self.assertLess(el, QUICK_LIMIT)
        self.compose_timeout(1500)
        t2 = time.time()
        p, _ = run(self.p, self.home, args=["--compose", "(sleep 3; touch %s) & sleep 5" % sh_path(killed)])
        self.assertTrue(text(p.stdout.decode()).startswith("5h 42%"), p.stdout)
        while not os.path.exists(left) and time.time() < t + 10:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(left))
        time.sleep(max(0.0, t2 + 4 - time.time()))
        self.assertFalse(os.path.exists(killed))

    @posix_shell
    def test_compose_reads_output_until_a_quiet_window(self):
        # Review 7 (fresh-eyes#0): after the shell exits, output its
        # background job writes soon after still counts; output that never
        # stops is read until the deadline, and what arrived is used.
        self.compose_timeout(QUICK_TIMEOUT_MS)
        p, el = run(self.p, self.home, args=["--compose", "echo a; (sleep 0.2; echo b) &"], env={"COLUMNS": "200"})
        self.assertTrue(text(p.stdout.decode()).startswith("a\nb  5h 42%"), p.stdout)
        self.assertLess(el, QUICK_LIMIT)
        self.compose_timeout(1500)
        chatty = "echo hi; (i=0; while [ $i -lt 40 ]; do echo x; sleep 0.1; i=$((i+1)); done) &"
        p, el = run(self.p, self.home, args=["--compose", chatty], env={"COLUMNS": "200"})
        lines = text(p.stdout.decode()).rstrip("\n").split("\n")
        self.assertEqual(lines[0], "hi")
        self.assertIn("5h 42%", lines[-1])
        self.assertGreater(len(lines), 3 if WINDOWS else 5)  # each sleep is a process: slower in Git Bash
        self.assertLess(el, 3.0)

    @posix_shell
    def test_no_recursion_when_composed_with_itself(self):
        cmd = "%s -I -S %s statusline --compose 'printf x'" % (sh_path(sys.executable), sh_path(ENTRY))
        p, el = run(self.p, self.home, args=["--compose", cmd], env={"COLUMNS": "300"})
        out = text(p.stdout.decode())
        self.assertEqual(out.count("5h 42%"), 2)  # inner glidepath does not compose again
        self.assertNotIn("x", out.split("5h")[0])


class StaleReadingAfterReset(unittest.TestCase):
    """An idle session's pre-reset reading must not win on display."""

    def test_shows_the_judged_level(self):
        from glidepath import ingest, statusline
        from glidepath.store import Store

        utc()
        home = tempfile.mkdtemp(prefix="glidepath-stale-")
        old = os.environ.get("GLIDEPATH_HOME")
        os.environ["GLIDEPATH_HOME"] = home
        try:
            cfg = config.load()
            st = Store()
            now = MON + 2 * H
            R = int(MON + 4 * H)
            for t, used, sid in ((now - 600, 95, "idle"), (now - 590, 95, "a"), (now - 300, 1, "a"), (now - 240, 2, "a")):
                r, rej, k = ingest.parse_payload({"rate_limits": {"five_hour": {"used_percentage": used, "resets_at": R}}}, t)
                ingest.record(st, r, t, "statusline", sid, cfg, rej, k)
            st.close()
            raw = json.dumps({"session_id": "idle", "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": R}}}).encode()
            opts = statusline.parse_args([])
            v5, vw, readings = statusline.compute(raw, cfg, opts, now)
            self.assertEqual(v5["used"], 2)
            self.assertNotEqual(v5["verdict"], "maxed")
        finally:
            if old is None:
                os.environ.pop("GLIDEPATH_HOME", None)
            else:
                os.environ["GLIDEPATH_HOME"] = old
            shutil.rmtree(home, ignore_errors=True)


class Render(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = config.load(os.devnull)

    def v5(self, **kw):
        v = dict(present=True, used=62.0, reset_ts=MON + 13 * H + 20 * 60, verdict="speed_up", multiplier=1.4,
                 projected=88.0, cap_ts=None, allowed=9.1, rate_source="lsq", severity="dim")
        v.update(kw)
        return v

    def vw(self, **kw):
        # Slow down without a dry time: a target_week below 100 (yellow).
        v = dict(present=True, used=48.0, reset_ts=MON + 3 * D + 9 * H, verdict="slow_down", multiplier=0.8,
                 projected=96.0, dry_ts=None, delta=3.2, severity="warn")
        v.update(kw)
        return v

    def line(self, v5, vw, cfg=None):
        return render.plain(render.segments(v5, vw, cfg or self.cfg))

    def test_reference_line(self):
        # Review 4: the README line, in a state the default targets can reach
        # (x0.8 at target 100 runs dry; x1.1 projects 48 + 52 / 1.1 = 95).
        vw = self.vw(verdict="on_pace", multiplier=1.1, projected=95.0, severity="dim")
        self.assertEqual(self.line(self.v5(), vw), "5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00")

    def test_dry_idle_and_fallbacks(self):
        s = self.line(self.v5(verdict="slow_down", multiplier=0.6, cap_ts=MON + 12 * H + 40 * 60),
                      self.vw(dry_ts=MON + 2 * D + 14 * H, delta=12.4))
        self.assertEqual(s, "5h 62% ×0.6▼ dry 12:40 @13:20 · wk 48% +12 ×0.8▼ dry~Wed 14:00 @Thu 09:00")
        self.assertTrue(self.line(self.v5(verdict="idle"), self.vw()).startswith("5h 62% idle ≤9%/h @13:20"))
        self.assertTrue(self.line(self.v5(rate_source="window_avg", verdict="on_pace", multiplier=1.0), self.vw()).startswith("5h 62% ~×1.0 →88%"))
        self.assertEqual(self.line({"present": False}, {"present": False}), "5h 0% · wk 0%")

    def test_unstored_reading_without_a_window(self):
        # Review 7 (fresh-eyes#2): with the write failed and no open window,
        # the payload's level and reset show, like the bare line, not "0%".
        v5 = {"present": False, "raw": (12.0, MON + 15 * H)}
        vw = {"present": False, "raw": (43.4, MON + 3 * D + 9 * H)}
        self.assertEqual(self.line(v5, {"present": False}), "5h 12% @15:00 · wk 0%")
        self.assertEqual(self.line({"present": False}, vw), "5h 0% · wk 43% @Thu 09:00")
        cfg = config.load(os.devnull)
        cfg["display"]["max_width"] = 20
        self.assertEqual(self.line(v5, vw, cfg), "5h 12% · wk 43%")

    def test_width_never_exceeds_limit(self):
        long5 = self.v5(verdict="slow_down", multiplier=0.06, cap_ts=MON + 12 * H, used=99.7)
        longw = self.vw(dry_ts=MON + 2 * D + 14 * H, delta=-88.0, multiplier=12.0)
        for width in (80, 60, 50, 40):
            cfg = config.load(os.devnull)
            cfg["display"]["max_width"] = width
            self.assertLessEqual(len(self.line(long5, longw, cfg)), width)

    def test_ascii_symbols(self):
        cfg = config.load(os.devnull)
        cfg["display"]["symbols"] = "ascii"
        s = self.line(self.v5(), self.vw())
        s2 = self.line(self.v5(), self.vw(), cfg)
        self.assertNotEqual(s, s2)
        self.assertTrue(all(ord(c) < 128 for c in s2), s2)

    def test_last_resort_cut_keeps_each_tone(self):
        # Review 4: when the required parts alone overflow, the cut made the
        # whole line one dim segment, so both red windows lost their color.
        # Required: 22 + 3 + 26 = 51 columns; 40 leaves 39 (unicode) or 37
        # (ascii) before the ellipsis.
        cfg = config.load(os.devnull)
        cfg["display"]["max_width"] = 40
        v5 = self.v5(verdict="slow_down", multiplier=0.6, cap_ts=MON + 12 * H + 40 * 60, severity="alert")
        vw = self.vw(dry_ts=MON + 2 * D + 14 * H, severity="alert")
        self.assertEqual(render.segments(v5, vw, cfg),
                         [("5h 62% ×0.6▼ dry 12:40", "alert"), (" · ", "dim"), ("wk 48% ×0.8▼ d…", "alert")])
        cfg["display"]["symbols"] = "ascii"
        self.assertEqual(render.segments(v5, vw, cfg),
                         [("5h 62% x0.6v dry 12:40", "alert"), (" | ", "dim"), ("wk 48% x0.8v...", "alert")])

    def test_ascii_cut_stays_ascii_and_within_width(self):
        # Review 4: the cut appended "…" in ascii mode too.
        long5 = self.v5(verdict="slow_down", multiplier=0.06, cap_ts=MON + 12 * H, used=99.7, severity="alert")
        longw = self.vw(dry_ts=MON + 2 * D + 14 * H, delta=-88.0, multiplier=12.0)
        for symbols in ("unicode", "ascii"):
            for width in range(20, 81):
                cfg = config.load(os.devnull)
                cfg["display"].update(symbols=symbols, max_width=width)
                segs = render.segments(long5, longw, cfg)
                line = render.plain(segs)
                self.assertLessEqual(len(line), width, (symbols, line))
                self.assertEqual(segs[0][1], "alert", (symbols, segs))
                if symbols == "ascii":
                    self.assertTrue(all(ord(c) < 128 for c in line), line)

    def test_bare_line_uses_the_symbols_and_fits(self):
        # Review 4: 13 + 3 + 17 = 33 columns; 32 keeps 29, then "...".
        from glidepath import statusline

        cfg = config.load(os.devnull)
        cfg["display"].update(symbols="ascii", max_width=32)
        readings = {config.FIVE_HOUR: (40, MON + 16 * H + 600), config.SEVEN_DAY: (30, MON + 5 * D + 14 * H + 600)}
        self.assertEqual(statusline.bare_line(readings, MON, cfg), "5h 40% @16:10 | wk 30% @Sat 1...")
        self.assertEqual(statusline.bare_line(readings, MON, self.cfg), "5h 40% @16:10 · wk 30% @Sat 14:10")

    def test_times_round_to_the_nearest_minute(self):
        # Review 8 (long-horizon#4): times were cut to the minute, so a reset
        # stored 2 s early showed a minute early.
        self.assertEqual(render.hm(MON + 14 * H - 2), "14:00")
        self.assertEqual(render.dhm(MON + 3 * D + 9 * H - 29), "Thu 09:00")
        self.assertEqual(render.hm(MON + 14 * H + 29), "14:00")
        self.assertEqual(render.hm(MON + 14 * H + 31), "14:01")

    def test_formatting_helpers(self):
        self.assertEqual(render.pct(99.6), "99%")
        self.assertEqual(render.pct(100.0), "100%")
        self.assertEqual(render.mult(12), "9+")
        self.assertEqual(render.rate(0.42), "0.4")

    def test_colors(self):
        segs = render.segments(self.v5(severity="alert"), self.vw(), self.cfg)
        out = render.to_ansi(segs, True)
        self.assertIn("\033[1;31m5h", out)
        self.assertIn("\033[33mwk", out)
        self.assertFalse(render.use_color(self.cfg, {"NO_COLOR": "1"}))
        self.assertTrue(render.use_color(self.cfg, {}))


class WriteFailedAtRollover(TempHome):
    """Review 7 (fresh-eyes#2): a write that fails (database locked, or not
    writable) at a rollover never opens the new window, so the view saw only
    the expired one and the line said "5h 0%" (or waited for usage data)
    while the payload carried the new window's level."""

    def locked_render(self, now, five=None, week=None):
        with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8", newline="") as f:
            json.dump({"db_timeout_ms": 10}, f)
        lock = sqlite3.connect(os.path.join(self.dir, "tempo.db"), isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            p, _ = run(payload(five, week), self.dir, args=["--now", str(now), "--no-compose"], env={"NO_COLOR": "1"})
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        return text(p.stdout.decode("utf-8").rstrip("\n"))

    def test_5h_rollover_shows_the_payload_level(self):
        R1, R7 = int(MON + 5 * H), int(MON + 7 * D)
        self.feed(MON + 4 * H, five=(80, R1), week=(42, R7))
        self.feed(MON + 4 * H + 600, five=(84, R1), week=(43, R7))
        line = self.locked_render(R1 + 120, five=(12, R1 + 5 * H), week=(43, R7))
        self.assertTrue(line.startswith("5h 12% "), line)

    def test_weekly_rollover_shows_the_payload_level(self):
        R5, R7 = int(MON + D + 2 * H), int(MON + D)
        self.feed(MON + 22 * H, five=(5, R5), week=(60, R7))
        line = self.locked_render(R7 + 120, five=(5, R5), week=(1, R7 + 7 * D))
        self.assertTrue(line.startswith("5h 5% "), line)
        self.assertTrue(line.endswith(" · Week 1%  resets Tue"), line)

    def test_first_render_does_not_wait_for_data_it_has(self):
        line = self.locked_render(MON, five=(42, MON + 2 * H), week=(31.5, MON + 3 * D))
        self.assertEqual(line, "5h 42%  resets 02:00 · Week 31%  resets Thu")


class WobbledReset(TempHome):
    def test_a_first_reading_seconds_early_shows_the_whole_minute(self):
        # Review 8 (long-horizon#4): a window keeps the resets_at of its first
        # reading (D-08). When that one wobbled 2 s early, the line showed
        # @04:59 and @Sun 23:59 for the whole window.
        from glidepath import commands

        r5, r7 = int(MON + 5 * H), int(MON + 7 * D)
        self.feed(MON + H, five=(10, r5 - 2), week=(20, r7 - 2))
        for k in range(1, 7):
            self.feed(MON + H + k * 600, five=(10 + k, r5), week=(20, r7))
        self.assertEqual([i["reset_ts"] for i in self.windows(config.FIVE_HOUR)], [r5 - 2])
        v5, vw, _ = commands.views(self.st, self.cfg, MON + 2 * H)
        line = render.plain(render.segments(v5, vw, self.cfg))
        self.assertIn(" @05:00 · wk ", line)
        self.assertTrue(line.endswith(" @Mon 00:00"), line)


class TildeHome(unittest.TestCase):
    """Review 4 (runtime-ui#0): Claude Code copies settings.json `env` values
    as written, so GLIDEPATH_HOME can arrive as "~/..." or as a relative path. It
    must resolve under the user's home, never under each session's cwd."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-tilde-")
        self.user = os.path.join(self.dir, "home")
        self.cwd = os.path.join(self.dir, "project")
        os.makedirs(self.user)
        os.makedirs(self.cwd)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_home_expands_tilde_and_takes_relative_paths_from_home(self):
        # USERPROFILE too: Python reads `~` from it on Windows, never HOME.
        elsewhere = os.path.join(self.dir, "elsewhere")
        # Windows refuses /srv/glidepath (tests/test_config.py, NoDriveHome); \srv\glidepath takes the home's drive.
        drive = ntpath.splitdrive(self.user)[0]
        rooted = ("\\srv\\glidepath", drive + "\\srv\\glidepath") if WINDOWS else ("/srv/glidepath", "/srv/glidepath")
        cases = (("~/.glidepath-moved", os.path.join(self.user, ".glidepath-moved")), ("~", self.user),
                 ("data/glidepath", os.path.join(self.user, "data", "glidepath")), (elsewhere, elsewhere),
                 rooted, ("", os.path.join(self.user, ".glidepath")))
        for value, want in cases:
            with mock.patch.dict(os.environ, {"HOME": self.user, "USERPROFILE": self.user, "GLIDEPATH_HOME": value}):
                self.assertEqual(config.home(), want, value)

    def test_windows_rules(self):
        # Windows paths whatever the OS: drive and UNC paths are absolute, a
        # path rooted with a backslash and no drive takes the home's drive
        # (with a slash it is refused: tests/test_config.py, NoDriveHome), Git
        # Bash's /c/Users/x is C:/Users/x, and `~` is USERPROFILE (HOME is ignored).
        user = "C:\\Users\\me"
        cases = ((None, user + "\\.glidepath"), ("", user + "\\.glidepath"), ("~", user),
                 ("~/tempo", user + "\\tempo"), ("~\\tempo\\", user + "\\tempo"), ("data/glidepath", user + "\\data\\glidepath"),
                 ("D:\\tempo", "D:\\tempo"), ("d:/tempo/", "d:\\tempo"),
                 ("\\srv\\glidepath", "C:\\srv\\glidepath"), ("/d/tempo", "D:\\tempo"), ("/c", "C:\\"), ("/e/", "E:\\"),
                 ("//server/share/tempo", "\\\\server\\share\\tempo"),
                 ("\\\\server\\share\\tempo", "\\\\server\\share\\tempo"), ("C:/Users/me/../tempo", "C:\\Users\\tempo"))
        with mock.patch.object(config, "IS_WINDOWS", True):
            for value, want in cases:
                env = {"USERPROFILE": user, "HOME": "/c/Users/other", "GLIDEPATH_HOME": value or ""}
                with mock.patch.dict(os.environ, env):
                    if value is None:
                        del os.environ["GLIDEPATH_HOME"]
                    self.assertEqual(config.home(), want, value)
            with mock.patch.dict(os.environ, {"HOMEDRIVE": "D:", "HOMEPATH": "\\Users\\me", "GLIDEPATH_HOME": ""}):
                os.environ.pop("USERPROFILE", None)
                self.assertEqual(config.home(), "D:\\Users\\me\\.glidepath")

    def test_statusline_with_tilde_home_writes_under_home_not_cwd(self):
        now = time.time()
        payload = {"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": int(now + 2 * H)}}}
        env = dict(os.environ, HOME=self.user, USERPROFILE=self.user, GLIDEPATH_HOME="~/.glidepath-moved", TZ=TZ_UTC,
                   CLAUDE_CONFIG_DIR=os.path.join(self.dir, "claude"))
        p = subprocess.run([sys.executable, "-I", "-S", ENTRY, "statusline"], input=json.dumps(payload).encode(),
                           capture_output=True, env=env, cwd=self.cwd, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.user, ".glidepath-moved", "tempo.db")))
        self.assertEqual(os.listdir(self.cwd), [])  # no directory literally named "~" in the project


class EncodedCompose(unittest.TestCase):
    """Review 15 (shells#1, shells#2): on Windows install writes a composed
    command that Windows PowerShell 5.1 or Git Bash would hand to Python changed
    as `--compose-b64 b<token>`. glidepath statusline decodes it on any OS; a
    malformed token composes nothing, and never raises."""

    def test_parse_args_decodes_it(self):
        from glidepath import statusline

        for cmd in ('powershell -NoProfile -File "C:/Users/John Smith/sl.ps1"', "/bin/bash ~/x.sh", "dir C:\\",
                    "echo \u25b2 J\u00fcrgen"):
            self.assertEqual(statusline.parse_args(["--compose-b64", winshell.encoded(cmd)])["compose_cmd"], cmd)
        for bad in (["--compose-b64"], ["--compose-b64", "b!!"], ["--compose-b64", "bZ"], ["--compose-b64", "b____"],
                    ["--compose-b64", "xYQ"], ["--compose-b64", ""]):
            self.assertIsNone(statusline.parse_args(bad)["compose_cmd"], bad)
        # The last one wins, as with 2 --compose (and as install.compose_of reads it).
        argv = ["--compose", "printf a", "--compose-b64", winshell.encoded("printf b")]
        self.assertEqual(statusline.parse_args(argv)["compose_cmd"], "printf b")

    @posix_shell
    def test_the_decoded_command_runs(self):
        home = tempfile.mkdtemp(prefix="glidepath-b64-")
        self.addCleanup(shutil.rmtree, home, True)
        now = time.time()
        p = payload((42, int(now + 2 * H)), (31.5, int(now + 3 * D)))
        out, _ = run(p, home, args=["--compose-b64", winshell.encoded("printf 'say \"hi\"'")], env={"COLUMNS": "200"})
        self.assertTrue(text(out.stdout.decode()).startswith('say "hi"  5h 42%'), out.stdout)
        out, _ = run(p, home, args=["--compose-b64", "b!!"], env={"COLUMNS": "200"})
        self.assertEqual(out.returncode, 0)
        self.assertTrue(text(out.stdout.decode()).startswith("5h 42%"), out.stdout)
        self.assertFalse(os.path.exists(os.path.join(home, "errors.log")))


class LogRotation(unittest.TestCase):
    """Review 15: on Windows the rename that rotates errors.log fails while
    the log is held open without delete sharing (CPython's open() and
    doctor's reader open files so), and the error being logged was dropped
    with it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-log-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"GLIDEPATH_HOME": self.dir})
        env.start()
        self.addCleanup(env.stop)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_rotation_that_fails_still_logs_the_error(self):
        from glidepath import statusline

        path = config.log_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("2026-10-14T08:00:00 record: old\n" * (statusline.LOG_MAX // 32 + 1))
        held = PermissionError(13, "The process cannot access the file because it is being used by another process")
        with mock.patch.object(os, "replace", side_effect=held) as replace:
            statusline.log_error(RuntimeError("database is locked"), "record")
        self.assertEqual(replace.call_count, 1)  # tried once: no retry on the hot path
        self.assertIn(" record: database is locked\n", self.read(path))
        self.assertFalse(os.path.exists(path + ".1"))
        statusline.log_error(RuntimeError("later"), "learn")  # released: the next error rotates
        self.assertIn(" record: database is locked\n", self.read(path + ".1"))
        self.assertTrue(self.read(path).endswith("RuntimeError: later\n") and " learn: later\n" in self.read(path))


class ComposeWidth(unittest.TestCase):
    """Review 9 (macos-readiness#0): Claude Code draws the status line in its
    prompt footer, 2 columns of padding on each side, and cuts it at the
    footer's width in display columns (an emoji takes 2). Our segment was
    joined to the user's line up to COLUMNS code points, so a joined line
    between COLUMNS - 4 and COLUMNS columns lost its weekly reset."""

    ours = "5h 41% ~×2.2▲ →68% @02:19 · wk 52% -18 ×2.2▲ →74% @Sat 02:19"

    def lines(self, theirs, columns=100):
        from glidepath import statusline

        return statusline.join_composed(theirs, self.ours, config.DEFAULTS, columns).split("\n")

    def test_joined_line_fits_the_footer(self):
        n = 96 - 2 - len(self.ours)  # exactly the footer's width at COLUMNS=100
        self.assertEqual(len(self.lines("x" * n)), 1)
        self.assertEqual(len(self.lines("x" * (n + 1))), 2)
        self.assertEqual(self.lines("\x1b[2m" + "x" * n + "\x1b[0m")[0][-len(self.ours):], self.ours)

    def test_wide_characters_count_twice(self):
        from glidepath import statusline

        theirs = "\U0001f4c1 tempo \U0001f33f main \U0001f916 Opus \U0001f4b0 $1.2 41%"
        self.assertEqual(statusline.visible_len(theirs), len(theirs) + 4)
        self.assertLessEqual(len(theirs) + 2 + len(self.ours), 96)  # fits in code points
        self.assertEqual(self.lines(theirs), [theirs, self.ours])
        # Combining marks and joiners take no column; VS16 makes a symbol an emoji.
        self.assertEqual(statusline.visible_len("cafe\u0301 \u2601\ufe0f a\u200db \u5f00"), 13)


@posix_shell
class WindowsComposer(unittest.TestCase):
    """The Composer's Windows branch: the old command runs in the shell Claude
    Code would pick (shell=True means cmd.exe there), its pipes are served by
    threads (select() takes sockets only), and one still running at the
    deadline is killed with its tree by taskkill. Off Windows the branch runs
    with bash standing in for Git Bash, and p.kill() for taskkill."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-wincomp-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patches = [mock.patch.object(config, "IS_WINDOWS", True),
                   mock.patch.object(winshell, "kill_tree", wraps=winshell.kill_tree),
                   mock.patch.object(winshell, "parent_shell", return_value=None)]  # the test runner's shell
        if not WINDOWS:
            patches.append(mock.patch.object(winshell, "predict_shell", return_value=("bash", "bash")))
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.kill = started[1]

    def compose(self, cmd, raw=b"", timeout=1.5):
        from glidepath import statusline

        t = time.time()
        c = statusline.Composer(cmd, raw, t + timeout)
        out = c.finish()
        return c, out, time.time() - t

    def test_runs_in_the_shell_with_stdin(self):
        c, out, _ = self.compose("cat", raw=b'{"session_id": "abc"}')
        self.assertEqual(out, '{"session_id": "abc"}')
        self.assertEqual(c.p.args[1:], ["-c", "cat"])  # bash -c, not shell=True
        _, out, _ = self.compose("wc -c", raw=b"x" * 1000000, timeout=10)  # more than a pipe holds
        self.assertEqual(out.strip(), "1000000")

    def test_crlf_lines_and_failures(self):
        self.assertEqual(self.compose("printf 'a\\r\\nb\\r\\n'")[1], "a\nb")
        self.assertIsNone(self.compose("exit 3")[1])
        self.kill.assert_not_called()

    def test_quiet_window_and_a_background_job(self):
        _, out, el = self.compose("echo a; (sleep 0.2; echo b) &", timeout=QUICK_TIMEOUT_MS / 1000.0)
        self.assertEqual(out, "a\nb")
        self.assertLess(el, QUICK_LIMIT)
        self.kill.assert_not_called()

    def test_still_running_at_the_deadline_is_killed(self):
        c, out, el = self.compose("sleep 5; echo late", timeout=0.5)
        self.assertIsNone(out)
        self.kill.assert_called_once_with(c.p.pid)
        self.assertIsNotNone(c.p.wait(5))
        self.assertLess(el, 3.0)

    def test_a_silent_background_job_is_not_waited_for(self):
        # Review 15 (regress-ci#16): the shell exited and its job holds stdout open past
        # QUICK_LIMIT on every OS, so a reader that waited for EOF fails here.
        _, out, el = self.compose("echo a; (sleep 4; echo late) &", timeout=QUICK_TIMEOUT_MS / 1000.0)
        self.assertEqual(out, "a")
        self.assertLess(el, QUICK_LIMIT)
        self.kill.assert_not_called()

    def test_runs_in_the_shell_that_started_glidepath(self):
        # Review 15 (shells#0): inside Git Bash, whose PATH puts its own git first, Claude
        # Code's search can end in PowerShell; the shell that ran glidepath is the one to use.
        bash = winshell.predict_shell()[1]  # Git Bash on Windows (never WSL's bash on PATH), else bash
        with mock.patch.object(winshell, "parent_shell", return_value=("bash", bash)), \
                mock.patch.object(winshell, "predict_shell", return_value=("powershell", None)):
            c, out, _ = self.compose("printf x")
        self.assertEqual(out, "x")
        self.assertEqual(c.p.args, [bash, "-c", "printf x"])

    def test_main_joins_a_crlf_line(self):
        from glidepath import statusline

        class Out:
            def __init__(self):
                self.buffer = io.BytesIO()

        o, now = Out(), time.time()
        p = {"session_id": "abc", "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": int(now + 2 * H)}}}
        with mock.patch.object(config, "home", return_value=self.dir), mock.patch.object(sys, "stdout", o), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(p).encode()))), \
                mock.patch.dict(os.environ, {"COLUMNS": "200", "NO_COLOR": "1"}):
            os.environ.pop("GLIDEPATH_COMPOSED", None)
            statusline.main(["--compose", "printf 'theirs\\r\\n'"])
        out = o.buffer.getvalue().decode("utf-8")
        self.assertTrue(text(out).startswith("theirs  5h 42%"), out)
        self.assertNotIn("\r", out)


@unittest.skipUnless(WINDOWS, "needs Windows PowerShell")
class PowerShellComposer(unittest.TestCase):
    """Without Git Bash, Claude Code runs the statusLine in PowerShell
    (-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command), and the
    Composer runs the old command there too. On a real Windows, with pwsh and
    with Windows PowerShell 5.1: output, the payload on stdin reaching a native
    program (as it must reach glidepath's own command), UTF-8 output relayed
    intact, and a command still running at the deadline killed."""

    def setUp(self):
        patches = [mock.patch.object(config, "IS_WINDOWS", True),
                   mock.patch.object(winshell, "kill_tree", wraps=winshell.kill_tree),
                   mock.patch.object(winshell, "parent_shell", return_value=None)]  # the test runner's shell
        self.kill = [p.start() for p in patches][1]
        for p in patches:
            self.addCleanup(p.stop)
        env = dict(os.environ)
        p51 = ntpath.join(winshell.getenv(env, "SYSTEMROOT") or "C:\\Windows", "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")
        self.shells = [p for p in (winshell.which("pwsh", env), p51) if p and os.path.isfile(p)]
        self.py = "& '%s'" % sys.executable.replace("'", "''")

    def compose(self, ps, cmd, raw=b"", timeout=15.0):
        from glidepath import statusline

        with mock.patch.object(winshell, "predict_shell", return_value=("powershell", ps)):
            c = statusline.Composer(cmd, raw, time.time() + timeout)
            return c, c.finish()

    def test_output_and_stdin(self):
        self.assertTrue(self.shells)
        for ps in self.shells:
            with self.subTest(ps=ps):
                self.assertEqual(self.compose(ps, "Write-Output theirs")[1], "theirs")
                cmd = self.py + " -c 'import sys; sys.stdout.write(sys.stdin.read())'"
                self.assertEqual(self.compose(ps, cmd, raw=b'{"session_id": "abc"}\n')[1], '{"session_id": "abc"}')

    def test_utf8_output_is_relayed(self):
        for ps in self.shells:
            with self.subTest(ps=ps):
                # No quotes inside: Windows PowerShell 5.1 passes a double quote on unescaped.
                cmd = self.py + " -c 'import sys; sys.stdout.buffer.write((chr(0x25b2) + chr(32) + chr(0x2192)).encode())'"
                self.assertEqual(self.compose(ps, cmd)[1], "\u25b2 \u2192")

    def test_still_running_at_the_deadline_is_killed(self):
        for ps in self.shells:
            with self.subTest(ps=ps):
                self.kill.reset_mock()
                c, out = self.compose(ps, "Start-Sleep 20", timeout=3.0)
                self.assertIsNone(out)
                self.kill.assert_called_once_with(c.p.pid)
                self.assertIsNotNone(c.p.wait(5))


if __name__ == "__main__":
    unittest.main()
