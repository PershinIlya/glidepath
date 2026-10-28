"""Smoke tests: every command runs on seeded data without a traceback."""

import argparse
import contextlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import glidepath
from glidepath import cli, commands, config, install, profile, weekly, winshell
from glidepath.config import SEVEN_DAY
from glidepath.store import Store

from .helpers import MON, H, D, TZ_UTC, TempHome, write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "glidepath")
M = 60.0


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="glidepath-cmd-")
        cls.env = dict(os.environ, GLIDEPATH_HOME=cls.home, TZ=TZ_UTC, CLAUDE_CONFIG_DIR=os.path.join(cls.home, "claude"))
        cls.env.pop("NO_COLOR", None)
        now = time.time()
        # Two closed 5h windows and one closed week, then the current ones.
        series = []
        for k in range(12):
            t = now - 8 * 86400 + k * 3600
            series.append((t, {"five_hour": {"used_percentage": 10 + k, "resets_at": int(now - 8 * 86400 + 5 * 3600)},
                               "seven_day": {"used_percentage": 50 + k, "resets_at": int(now - 7 * 86400 + 3600)}}))
        for k in range(25):
            t = now - 24 * 60 + k * 60
            series.append((t, {"five_hour": {"used_percentage": 20 + k * 0.5, "resets_at": int(now + 3 * 3600)},
                               "seven_day": {"used_percentage": 30 + k * 0.1, "resets_at": int(now + 6 * 86400)}}))
        for t, rl in series:
            subprocess.run([sys.executable, ENTRY, "statusline", "--now", str(t)], input=json.dumps(
                {"session_id": "s", "rate_limits": {
                    w: v for w, v in rl.items() if v["resets_at"] > t}}).encode(), env=cls.env, capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def run_cmd(self, *args, ok=(0,)):
        # UTF-8 through the pipe on every OS (Windows gave a pipe its ANSI code page).
        p = subprocess.run([sys.executable, ENTRY] + list(args), env=self.env, capture_output=True, encoding="utf-8",
                           timeout=60)
        self.assertNotIn("Traceback", p.stdout + p.stderr, p.stderr)
        self.assertIn(p.returncode, ok, p.stdout + p.stderr)
        return p.stdout

    def test_now(self):
        out = self.run_cmd("now")
        self.assertIn("5-hour window", out)
        self.assertIn("Weekly window", out)
        json.loads(self.run_cmd("now", "--json"))
        self.assertIn("5-hour window", self.run_cmd())  # bare glidepath = now

    def test_report(self):
        out = self.run_cmd("report", "--days", "30", "-v")
        self.assertIn("Weekly windows closed: 1", out)
        json.loads(self.run_cmd("report", "--json"))

    def test_profile(self):
        out = self.run_cmd("profile")
        self.assertIn("Hour-of-week profile", out)
        self.assertTrue(any(c in out for c in "·░▒▓█"), out)  # not in cp1252: raised on Windows
        self.assertIn("Rebuilt", self.run_cmd("profile", "--rebuild"))
        json.loads(self.run_cmd("profile", "--json"))

    def test_check(self):
        d = json.loads(self.run_cmd("check", "--tail", "5h=4h,week=7d", "--min-free", "5", ok=(0, 1)))
        self.assertIn("ok", d)
        d = json.loads(self.run_cmd("check", "--tail", "bogus", ok=(1,)))
        self.assertFalse(d["ok"])

    def test_doctor_and_config(self):
        settings = os.path.join(self.home, "settings.json")
        with open(settings, "w", encoding="utf-8", newline="") as f:
            json.dump({"statusLine": {"type": "command", "command": "x/bin/glidepath statusline", "refreshInterval": 60}}, f)
        out = self.run_cmd("doctor", "--settings", settings, ok=(0, 1))
        self.assertIn("statusLine runs glidepath", out)
        self.assertIn("database", out)
        json.loads(self.run_cmd("doctor", "--json", "--settings", settings, ok=(0, 1)))
        self.assertIn("target_5h", self.run_cmd("config"))
        self.assertTrue(self.run_cmd("config", "path").strip().endswith("config.json"))

    def test_bootstrap_without_transcripts(self):
        out = self.run_cmd("bootstrap", "--root", os.path.join(self.home, "none"), "--dry-run", ok=(1,))
        self.assertIn("No usable messages", out)


class ShiftedWindow(TempHome):
    """Review 4 (spec-docs#2): one reading with a bogus later resets_at opens a
    window that the real resets_at then closes as 'shift' (D-10). That window
    never ended, so report and profile must not count its unused points."""

    def setUp(self):
        super().setUp()
        now = time.time()
        r7 = int(now + 3 * D)
        self.feed(now - 1800, week=(40, r7))
        self.feed(now - 1500, week=(41, r7 + 7200))  # bogus later reset: rollover
        self.feed(now - 1200, week=(42, r7))  # the real reset again: anomaly
        self.feed(now - 900, week=(43, r7))  # sustained: shift, the real week reopens
        closed = [(i["close_reason"], i["unused_at_close"]) for i in self.windows(SEVEN_DAY) if i["closed_at"] is not None]
        self.assertEqual(closed, [("shift", 59.0)])

    def call(self, fn, **args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(fn(argparse.Namespace(**args), self.cfg), 0)
        return out.getvalue()

    def test_report_leaves_out_a_shift_closure(self):
        self.assertEqual(json.loads(self.call(commands.cmd_report, days=30, json=True, verbose=False))[SEVEN_DAY], [])
        out = self.call(commands.cmd_report, days=30, json=False, verbose=True)
        self.assertIn("Weekly windows closed: 0", out)
        self.assertIn("no weekly window has closed yet", out)

    def test_profile_leaves_out_a_shift_closure(self):
        out = self.call(commands.cmd_profile, rebuild=False, json=False)
        self.assertIn("needs at least one closed weekly window", out)
        self.assertNotIn("Points vs pace", out)


class MovedReset(TempHome):
    def test_report_counts_a_week_whose_reset_moved_once(self):
        # Review 8 (long-horizon#3): a week whose resets_at moved 1 h later on
        # its fourth day, at a rising level. The move closed the running week
        # as a rollover, so `report` listed two finished weeks and 95 unused
        # points where one week left 33.
        now = time.time()
        R = int(now - 2 * D)  # the reset after the move
        start = R - H - 7 * D
        t = start + 2 * H
        while t < R - 2 * H:
            self.feed(t, week=(round(60 * (t - start) / (7 * D), 1), R - int(H) if t < start + 4 * D else R))
            t += 2 * H
        self.feed(R - H, week=(67, R))
        self.feed(now - H, week=(5, R + int(7 * D)))  # the next week
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_report(argparse.Namespace(days=30, json=False, verbose=False), self.cfg), 0)
        self.assertIn("Weekly windows closed: 1\n", out.getvalue())
        self.assertIn("Totals: 33 weekly points unused over 1 weeks", out.getvalue())


class ProfileText(TempHome):
    def profile(self, now=None):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            if now is not None:
                stack.enter_context(mock.patch.object(commands.time, "time", return_value=now))
            stack.enter_context(contextlib.redirect_stdout(out))
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=False, json=False), self.cfg), 0)
        return out.getvalue()

    def ramp(self, t0, t1, start, reset, span):
        """On pace from `start`: floor(100 x the share of `span` passed), a
        reading every 30 minutes in [t0, t1)."""
        t = t0
        while t < t1:
            self.feed(t, week=(math.floor(100 * (t - start) / span), reset))
            t += 30 * M

    def test_points_vs_pace_paces_a_cut_week_from_its_own_start(self):
        # Review 9 (delta-ingest-texts#2, delta-weekly#2): an early reset on
        # Thursday 12:00, then on pace for the window it opened. The table
        # paced that window from resets_at - 7 d, kept the window it cut
        # short (its frozen level against a rising pace) and counted both as
        # weeks: "averaged over 3 past weeks", up to 18 points of "surplus"
        # in the last 3 days for a user on pace. Two kinds of early reset:
        # one that keeps the reset time (a drop, D-10), one that moves it later.
        cut = MON - 3.5 * D
        for moved in (False, True):
            self.use_home("moved" if moved else "drop")
            self.known("s1")
            self.ramp(MON - 14 * D + 10 * M, MON - 7 * D, MON - 14 * D, int(MON - 7 * D), 7 * D)  # on pace
            self.ramp(MON - 7 * D + 10 * M, cut, MON - 7 * D, int(MON), 7 * D)  # 49% at the cut
            if moved:  # a rollover to a full week from the cut
                R = int(cut + 7 * D)
                self.ramp(cut, R, cut, R, 7 * D)
            else:  # a drop: the same reset, paced over the 3.5 days left
                R = int(MON)
                self.ramp(cut, R, cut, R, R - cut)
            self.ramp(R + 10 * M, R + D, R, int(R + 7 * D), 7 * D)  # the next week closes it
            reasons = [i["close_reason"] for i in self.windows(SEVEN_DAY) if i["closed_at"] is not None]
            self.assertEqual(reasons, ["expired", "rollover" if moved else "drop", "expired"])
            out = self.profile(R + D)
            line = [x for x in out.splitlines() if x.startswith("Points vs pace")][0]
            head, cols = line.split(": ")
            self.assertEqual(head, "Points vs pace, averaged over 2 past weeks")
            values = dict(c.rsplit(" ", 1) for c in cols.split(", "))
            self.assertEqual(list(values), ["-6d", "-5d", "-4d", "-3d", "-2d", "-1d", "reset"])
            for x in values.values():
                self.assertLessEqual(abs(float(x)), 1, (moved, line))

    def test_the_prior_line_shows_its_age_and_weight_now(self):
        # Review 9 (delta-weekly#3): the transcript prior fades with the
        # half-life from its build (D-14), but `glidepath profile` printed the
        # trust from build time for good: "trusted 100%" 8 weeks on, when it
        # weighed 6% of that.
        prof = profile.load(self.st)
        prof.prior = profile.uniform()
        prof.prior_meta = {"built_at": MON, "days": 45.0, "messages": 1782, "active_hours": 296, "trust": 1.0}
        profile.save(self.st, prof)
        out = self.profile(MON + 28 * D)
        self.assertIn("Prior: transcripts, 45.0 days, 1782 messages, 296 active hours, "
                      "trusted 100% when built (full at 14 days)\n", out)
        self.assertIn("  built 28d 00h ago, weight now 25% (it halves every 14 days; "
                      "`glidepath bootstrap` builds a fresh one)\n", out)

    def test_an_override_leaves_the_prior_unused(self):
        # Review 10: under profile.override the transcript prior has no weight
        # (Profile.rates takes the override), but `glidepath profile` printed its
        # faded weight, 78% five days on.
        prof = profile.load(self.st)
        prof.prior = profile.uniform()
        prof.prior_meta = {"built_at": MON, "days": 45.0, "messages": 1782, "active_hours": 296, "trust": 1.0}
        profile.save(self.st, prof)
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 23], "off_day_factor": 0.5, "mode": "prior"}
        out = self.profile(MON + 5 * D)
        self.assertIn("  unused: profile.override replaces it (weight now 0%", out)
        self.assertNotIn("weight now 78%", out)

    def test_an_override_is_the_prior_when_there_is_no_transcript_prior(self):
        # Review 11: with profile.override set and no transcript prior,
        # `glidepath profile` still said "run `glidepath bootstrap`", whose prior the
        # override would leave unused.
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 23], "off_day_factor": 0.5, "mode": "prior"}
        out = self.profile(MON + 5 * D)
        self.assertIn("Prior: profile.override (work days Mon,Tue,Wed,Thu,Fri, wake 8-23, mode prior)\n", out)
        self.assertNotIn("run `glidepath bootstrap`", out)

    def test_doctor_takes_an_override_for_the_prior(self):
        # Review 11: doctor warned "no transcript prior, run `glidepath bootstrap`"
        # under profile.override, which leaves such a prior unused.
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 23], "off_day_factor": 0.5, "mode": "prior"}
        from glidepath import install

        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(install, "claude_bin", lambda: None):
            commands.cmd_doctor(argparse.Namespace(json=False, settings=None), self.cfg)
        self.assertIn("profile prior from profile.override", out.getvalue())
        self.assertNotIn("no transcript prior", out.getvalue())

    def test_mode_replace_leaves_learned_hours_unused(self):
        # Review 12: under profile.override mode replace the profile is the
        # override's shape alone, but `glidepath profile` and doctor said the
        # learned hours were "folded in".
        from glidepath import install

        prof = profile.load(self.st)
        prof.observe(MON + 10 * H, 1.0, 1.0, 14 * D)
        profile.save(self.st, prof)
        self.assertGreater(profile.load(self.st).learned_hours, 0)
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 23], "off_day_factor": 0.5, "mode": "replace"}
        out = self.profile(MON + 5 * D)
        self.assertIn("unused (profile.override mode replace ignores them)", out)
        self.assertNotIn("folded in", out)
        doc = io.StringIO()
        with contextlib.redirect_stdout(doc), mock.patch.object(install, "claude_bin", lambda: None):
            commands.cmd_doctor(argparse.Namespace(json=False, settings=None), self.cfg)
        self.assertIn("unused: profile.override mode replace ignores them", doc.getvalue())

    def test_reset_column_note_follows_the_weekly_target(self):
        # Review 8 (mod-and-texts#1): the points-vs-pace columns are the level
        # minus the on-pace level for target_week, so at the reset it is peak
        # minus target. At target 90 a week that peaked at 89 printed "reset
        # -1" as "what expired", while `report` said 11 points expired.
        now = time.time()
        R = int(now - 2 * D)
        for k in range(1, 84):
            self.feed(R - 7 * D + k * 2 * H, week=(round(89 * k / 83, 1), R))
        self.feed(now - H, week=(5, R + int(7 * D)))  # the next week closes it
        out = self.profile()
        self.assertIn("reset -11\n", out)
        self.assertIn("(negative = surplus building up; the reset column is what expired)", out)
        self.cfg["target_week"] = 90
        out = self.profile()
        self.assertIn("reset -1\n", out)
        self.assertIn("(negative = surplus building up; the reset column is the peak versus your 90% target; "
                      "`glidepath report` shows what expired)", out)

    def test_lightest_awake_blocks_are_awake(self):
        # Review 8 (mod-and-texts#2): a block counted as awake on its weight
        # alone. A flat profile listed "Mon 00-03, Mon 03-06, Mon 06-09" (a
        # tie), and a regular day listed blocks two-thirds asleep, light only
        # because of the hours asleep.
        self.assertNotIn("Lightest awake blocks", self.profile())
        self.cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [8, 22],
                                           "off_day_factor": 0.6, "mode": "replace"}
        out = self.profile()
        self.assertIn("Awake hours per week: 98 of 168", out)
        self.assertIn("Lightest awake blocks: Sat 09-12, Sat 12-15, Sat 15-18\n", out)


class NowText(TempHome):
    """Review 5: `glidepath now` gives the reasoning behind every number, so
    its reasons and its data age must be the ones that apply."""

    def now(self, now, as_json=False):
        out = io.StringIO()
        with mock.patch.object(commands.time, "time", return_value=now), contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_now(argparse.Namespace(json=as_json), self.cfg), 0)
        return out.getvalue()

    def test_no_5h_rate_names_its_cause(self):
        # Review 5 (A-pace-display#1, X-integration#1): since the fit starts at
        # the window's first real reading (D-11), a window opened hours ago
        # has no rate for 5 minutes after work starts. `now` said "the window
        # opened less than 5 min ago" next to "resets 15:00, in 48m".
        self.known("s1")
        cases = [  # (window start, readings after it, now after it, the reason)
            (MON + 10 * H, ((4 * H + 10 * M, 80), (4 * H + 11 * M, 82)), 4 * H + 12 * M,
             "less than 5 min since work started in this window, at 14:10"),
            (MON + D + 10 * H, ((50 * M, 3), (52 * M, 5)), 54 * M,
             "less than 5 min since work started in this window, at 10:50"),
            (MON + 2 * D + 10 * H, ((2 * M, 1),), 3 * M, "the window opened less than 5 min ago"),  # it did
        ]
        for start, readings, now, why in cases:
            for t, used in readings:
                self.feed(start + t, five=(used, int(start + 5 * H)))
            out = self.now(start + now)
            self.assertIn("burn rate      unknown (%s)" % why, out)

    def test_weekly_window_fallback_names_its_cause(self):
        # Review 5 (X-integration#1): first seen 3 days into the week at 30%,
        # the level holds for 2 h. The lookback was cut at the first timed
        # reading and has seen no step (D-15), but `now` said "recent span too
        # short" although 2 h is 1.19% of the week, above the 1% minimum.
        R = int(MON + 7 * D)
        T = MON + 3 * D  # Thursday 00:00
        self.known("s1")
        for k in range(25):
            self.feed(T + k * 300, week=(30, R))
        out = self.now(T + 2 * H)
        self.assertIn("this week so far: 30.0 pts, profile expected 44.0 (the level has not moved since the first "
                      "timed reading, Thu 00:00)", out)
        self.assertIn("pulled toward the target (no typical week yet), 1.00: this week counts 81%; uncertain by +/-22%", out)
        # The next week, a new session's first reading only (D-06): no timed reading yet.
        self.feed(T + 7 * D, week=(30, R + int(7 * D)), sid="s2")
        self.assertIn("(no timed reading yet)", self.now(T + 7 * D + 2 * H))

    def test_typical_and_assumed_fallbacks_name_their_cause(self):
        # Review 6 (E-display-commands-weekly#3): a cut lookback whose window is
        # still at 0 falls back to the typical week, then 1.0 (D-15), but `now`
        # said "your typical week (no recent data)" or "assumed (no data yet)"
        # above "Data: 25 samples, last reading 2h 00m ago". The week reset
        # Saturday 02:00 and the readings from Monday 09:00 are 0.
        R0 = int(MON - 2 * D + 2 * H)
        R1 = R0 + int(7 * D)
        still = "no evidence from this week yet (the level has not moved since the first timed reading, Mon 09:00)"
        cases = [  # (closed week at 70%, timed readings, the label)
            (True, True, still + "\n" + " " * 27 + "pulled toward your typical week, 0.70: this week counts 0%"),
            (False, True, still + "\n" + " " * 27 + "pulled toward the target (no typical week yet), 1.00: this week counts 0%"),
            (False, False, "no evidence from this week yet (no timed reading yet)"),  # a new session's first reading
        ]
        for i, (closed, timed, label) in enumerate(cases):
            self.use_home("zero-%d" % i)
            if timed:
                self.known("s1")
            if closed:
                self.feed(R0 - 12 * H, week=(69, R0))
                self.feed(R0 - 2 * H, week=(70, R0))
            for k in range(25):
                self.feed(MON + 9 * H + k * 300, week=(0, R1))
            out = self.now(MON + 11 * H)
            self.assertIn("intensity       %.2f     %s" % (0.7 if closed else 1.0, label), out)
            self.assertNotIn("no recent data", out)
            self.assertNotIn("no data yet", out)
        # A week that opened at 10:00, read at 1% and 2% since: no earlier week,
        # and an hour is under 2% of a week's expected spend.
        self.use_home("young")
        self.known("s1")
        R2 = int(MON + 10 * H + 7 * D)
        self.feed(MON + 10 * H + 10 * M, week=(1, R2))
        self.feed(MON + 10 * H + 40 * M, week=(2, R2))
        out = self.now(MON + 11 * H)
        self.assertIn("this week's last 1h 00m: spent 2.0 pts, profile expected 0.6", out)
        self.assertIn("this week counts 6%; uncertain by +/-49%", out)
        # 2% in its first hour: the low edge does not overshoot, so no "Ease off" (D-37).
        self.assertIn("Weekly window: on pace", out)

    def test_data_age_ignores_heartbeats(self):
        # Review 5 (X-integration#2): check measures data age from readings
        # that carry information (D-20), but `now` said "last 0m ago" from an
        # idle session's heartbeats while check called the data 3 hours old.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(11):  # work from 4 h to 3 h ago
            self.feed(now - 4 * H + i * 6 * M, five=(3 * i, r5), week=(20, r7))
        for i in range(179, -1, -1):  # then idle, re-rendering every minute
            self.feed(now - i * M, five=(30, r5), week=(20, r7))
        self.assertIn("last reading 3h 00m ago, last heartbeat 0m ago.", self.now(now))
        self.assertAlmostEqual(json.loads(self.now(now, as_json=True))["data_age_s"], 3 * H)


class ProfileScale(TempHome):
    def test_profile_uses_the_lines_weekly_total(self):
        # Review 5 (X-integration#3): `glidepath profile` (and --rebuild) built its
        # weights without the typical week (D-13). After a closed week that
        # peaked at 70%, learned only from its 10 busiest hours at 1 %/h, it
        # took the ratio estimate, 10 / (10/168) = 168, and printed "typical
        # week 168% of the limit" while the line used 70.
        now = time.time()
        R1 = int(now - 2 * D)
        self.known("s1")
        for k in range(41):  # 60 -> 70% over 10 h, ending 2 h before the reset
            self.feed(R1 - 12 * H + k * 900, week=(60 + 0.25 * k, R1))
        self.feed(now - H, week=(5, R1 + int(7 * D)), sid="s2")  # the new week, a first reading
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=True, json=False), self.cfg), 0)
        self.assertIn("typical week 70% of the limit", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_profile(argparse.Namespace(rebuild=False, json=True), self.cfg), 0)
        info = json.loads(out.getvalue())["info"]
        self.assertAlmostEqual(info["weekly_total"], 70.0)
        self.assertAlmostEqual(weekly.weekly(self.st, time.time(), self.cfg)["weekly_typical"], 70.0)


class RunAsFolder(TempHome):
    """`py glidepath ...` from the clone runs the package folder as a script
    (a user typed it on Windows): it works as bin/glidepath does, with no
    warning, and the folder's config.py and profile.py never shadow the
    standard library's."""

    def run_folder(self, folder, *args):
        env = dict(os.environ, GLIDEPATH_HOME=self.dir)
        return subprocess.run([sys.executable, "-W", "error", folder] + list(args), cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_relative_and_absolute(self):
        for folder in ("glidepath", os.path.join(ROOT, "glidepath")):
            p = self.run_folder(folder, "--version")
            self.assertEqual((p.returncode, p.stdout.strip(), p.stderr), (0, "glidepath " + glidepath.__version__, ""), folder)
            p = self.run_folder(folder, "config", "path")
            self.assertEqual((p.returncode, p.stdout.strip()), (0, config.config_path()), folder)

    def test_the_exit_code_passes_through(self):
        self.assertEqual(self.run_folder("glidepath", "no-such-command").returncode, 2)


class Utf8Output(unittest.TestCase):
    """Windows gives a pipe or a file its ANSI code page (cp1252), which has
    no ▲ → ░, and -I ignores PYTHONIOENCODING and PYTHONUTF8: `now`,
    `profile` and `doctor` raised UnicodeEncodeError there. The CLI switches
    such a stream to UTF-8; a console (UTF-8 already) and other systems keep
    theirs."""

    def stream(self, encoding):
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n")

    def test_windows_pipes_get_utf8(self):
        out, err = self.stream("cp1252"), self.stream("cp1252")
        with mock.patch.object(config, "IS_WINDOWS", True), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            cli.utf8_output()
            print("5h ×1.4▲ →88% ░▒▓█")
            print("wk ×0.8▼", file=sys.stderr)
        out.flush()
        err.flush()
        self.assertEqual(out.buffer.getvalue(), "5h ×1.4▲ →88% ░▒▓█\n".encode("utf-8"))
        self.assertEqual(err.buffer.getvalue(), "wk ×0.8▼\n".encode("utf-8"))

    def test_a_console_and_other_systems_keep_their_streams(self):
        console, pipe = self.stream("utf-8"), self.stream("cp1252")
        with mock.patch.object(config, "IS_WINDOWS", True), mock.patch.object(sys, "stdout", console):
            cli.utf8_output()
        self.assertEqual((console.encoding, console.errors), ("utf-8", "strict"))
        with mock.patch.object(config, "IS_WINDOWS", False), mock.patch.object(sys, "stdout", pipe):
            cli.utf8_output()
        self.assertEqual(pipe.encoding, "cp1252")


class DoctorCase(unittest.TestCase):
    PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-doctor-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"GLIDEPATH_HOME": self.dir, "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MSYSTEM", None)  # doctor in a Git Bash terminal says more (DoctorFromGitBash)
        self.settings = os.path.join(self.dir, "settings.json")

    def doctor(self, cmd, recorded=None, predicted=("powershell", PS51), windows=True, env=None):
        sl = {"type": "command", "command": cmd, "refreshInterval": 60}
        write_json(self.settings, dict({"statusLine": sl}, **({"env": env} if env else {})))
        if recorded:
            write_json(install.state_path(), {"version": 2, "records": {
                self.settings: {"installed": True, winshell.RECORD_KEY: recorded}}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(install, "claude_bin", lambda: None), \
                mock.patch.object(config, "IS_WINDOWS", windows), mock.patch.object(config, "home", return_value=self.dir), \
                mock.patch.object(winshell, "predict_shell", return_value=predicted) as predict:
            commands.cmd_doctor(argparse.Namespace(json=False, settings=self.settings), config.load(os.devnull))
        self.predict = predict
        return out.getvalue()


class DoctorRuntime(DoctorCase):
    def test_runtime_json_with_a_byte_order_mark_is_read(self):
        # A BOM (Windows PowerShell 5.1, older Notepad) made doctor report
        # "no runtime.json" for a file that was there.
        with open(os.path.join(self.dir, "runtime.json"), "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"python": sys.executable, "entry": ENTRY}).encode("utf-8"))
        out = self.doctor("printf x", windows=False)
        self.assertIn("pinned interpreter %s" % sys.executable, out)
        self.assertNotIn("no runtime.json", out)


class DoctorModLog(DoctorCase):
    def test_shows_the_end_of_the_mod_log(self):
        # The mod logs which surfaces a session draws on and whether one asked
        # for the band: on Desktop for Mac it ran and recorded, and drew nothing.
        lines = ["2026-10-17T07:%02d:00.000Z 8865d914 line %d" % (i, i) for i in range(9)]
        with open(os.path.join(self.dir, "mod.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        out = self.doctor("printf x", windows=False)
        self.assertIn("mod log, last 6 lines", out)
        self.assertIn("8865d914 line 8", out)
        self.assertNotIn("8865d914 line 2", out)


class DoctorShell(DoctorCase):
    """Windows: Claude Code runs the statusLine in Git Bash when it finds one,
    else in PowerShell, and that can change after install (Git for Windows
    installed or removed). doctor names the shell and warns when glidepath's
    command was written for the other one. Patched to run on any OS; macOS and
    Linux show no such line."""

    QUOTED = "'C:/Program Files/Python312/python.exe' -I -S -X utf8 C:/repo/bin/glidepath statusline"
    BARE = "C:/Python312/python.exe -I -S -X utf8 C:/repo/bin/glidepath statusline"

    def test_a_command_for_git_bash_when_powershell_would_run_it(self):
        out = self.doctor(self.QUOTED, recorded="bash")
        self.assertIn("WARN statusLine command is written for Git Bash, but Claude Code would run it in PowerShell"
                      "  (run `glidepath install --apply` to rewrite it (%s))" % self.PS51, out)
        out = self.doctor(self.QUOTED, recorded="bash", predicted=("bash", self.GIT_BASH))
        self.assertIn("ok   statusLine shell: Git Bash  (%s)" % self.GIT_BASH, out)
        self.assertNotIn("written for", out)

    def test_the_syntax_tells_when_the_record_does_not(self):
        out = self.doctor("& " + self.QUOTED, predicted=("bash", self.GIT_BASH))
        self.assertIn("WARN statusLine command is written for PowerShell, but Claude Code would run it in Git Bash", out)
        # Both shells read a quoted ' differently: only the record can tell.
        ps = self.BARE + " --compose 'it''s'"
        self.assertIn("ok   statusLine shell: Git Bash", self.doctor(ps, predicted=("bash", self.GIT_BASH)))
        out = self.doctor(ps, recorded="powershell", predicted=("bash", self.GIT_BASH))
        self.assertIn("WARN statusLine command is written for PowerShell", out)

    def test_bare_words_run_in_either_shell(self):
        out = self.doctor(self.BARE + " --compose 'printf x'", recorded="bash")
        self.assertIn("ok   statusLine shell: PowerShell  (%s; install assumed Git Bash, and the command reads "
                      "the same in both)" % self.PS51, out)
        self.assertNotIn("WARN statusLine command", out)

    def test_settings_env_and_no_shell(self):
        env = {"CLAUDE_CODE_GIT_BASH_PATH": r"D:\git\bin\bash.exe"}
        self.doctor("printf x", env=env)
        self.predict.assert_called_once_with(os.environ, env)
        out = self.doctor("printf x", predicted=("powershell", None))
        self.assertIn("WARN statusLine shell: no Git Bash and no PowerShell found", out)

    def test_not_on_macos_or_linux(self):
        out = self.doctor(self.QUOTED, recorded="bash", windows=False)
        self.assertNotIn("statusLine shell", out)
        self.assertNotIn("written for", out)
        self.predict.assert_not_called()


class DoctorFromGitBash(DoctorCase):
    """Review 15 (shells#0): in a Git Bash terminal, whose PATH puts Git's own git
    first, the shell doctor predicts can differ from the one Claude Code finds when
    started from the Desktop app or PowerShell: a note, never a call to reinstall."""

    QUOTED, BARE = DoctorShell.QUOTED, DoctorShell.BARE
    NOTE = ("; checked from Git Bash, so Claude Code started from the Desktop app or PowerShell may pick another shell: "
            "run `glidepath doctor` from PowerShell to check")

    def git_bash_doctor(self, *a, **kw):
        with mock.patch.dict(os.environ, {"MSYSTEM": "MINGW64"}):
            return self.doctor(*a, **kw)

    def test_a_note_instead_of_a_repin(self):
        out = self.git_bash_doctor(self.QUOTED, recorded="bash")
        self.assertIn("WARN statusLine command is written for Git Bash, but Claude Code would run it in PowerShell  (%s%s)"
                      % (self.PS51, self.NOTE), out)
        self.assertNotIn("to rewrite it", out)
        out = self.git_bash_doctor("& " + self.QUOTED, recorded="powershell")
        self.assertIn("ok   statusLine shell: PowerShell  (%s%s)" % (self.PS51, self.NOTE), out)
        # Bare words read the same in both shells: nothing to check.
        out = self.git_bash_doctor(self.BARE, recorded="bash")
        self.assertIn("ok   statusLine shell: PowerShell  (%s; install assumed Git Bash" % self.PS51, out)
        self.assertNotIn("checked from Git Bash", out)


class DoctorEncodedCompose(DoctorCase):
    """Review 15 (shells#1, shells#2): doctor shows a composed command stored encoded
    as the plain command, and asks for a reinstall when an older install left one
    as plain text that Windows PowerShell 5.1 or Git Bash would change."""

    BARE = DoctorShell.BARE
    REPIN = ("WARN your previous statusLine can reach glidepath changed in Windows PowerShell 5.1 or Git Bash  "
             "(run `glidepath install --apply` to store it encoded)")

    def test_shown_decoded(self):
        old = 'node "C:/Users/John Smith/sl.js"'
        out = self.doctor(self.BARE + " --compose-b64 " + winshell.encoded(old), recorded="bash")
        self.assertIn("ok   composing with your previous statusLine  (%s)" % old, out)
        self.assertNotIn("store it encoded", out)

    def test_an_older_plain_one_asks_for_a_reinstall(self):
        for old in ('node "C:/Users/John Smith/sl.js"', "/bin/bash ~/x.sh", "dir C:\\x y\\"):
            with self.subTest(old=old):
                self.assertIn(self.REPIN, self.doctor(self.BARE + " --compose '%s'" % old))
                self.assertNotIn("store it encoded", self.doctor(self.BARE + " --compose '%s'" % old, windows=False))
        for old in ("~/x.sh", "node C:/x.js"):
            self.assertNotIn("store it encoded", self.doctor(self.BARE + " --compose '%s'" % old))

    def test_the_flag_that_runs_decides(self):
        # Review 15 verify: doctor read the first --compose, while glidepath statusline, compose_of
        # and a repin take the last flag, so it missed an at-risk command that runs and warned
        # about one that does not.
        risky = 'node "C:/Users/John Smith/sl.js"'
        self.assertIn(self.REPIN, self.doctor(self.BARE + " --compose 'echo hi' --compose '%s'" % risky))
        self.assertNotIn("store it encoded",
                         self.doctor(self.BARE + " --compose '%s' --compose-b64 %s" % (risky, winshell.encoded("echo hi"))))
        self.assertIn(self.REPIN, self.doctor(self.BARE + " --compose-b64 %s --compose '%s'" % (winshell.encoded("echo hi"), risky)))


if __name__ == "__main__":
    unittest.main()
