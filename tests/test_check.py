"""`glidepath check`: the scavenger's go / no-go."""

import argparse
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from glidepath import commands, weekly
from glidepath.config import FIVE_HOUR, SEVEN_DAY
from glidepath.store import HEARTBEAT, Store

from .helpers import MON, H, D, TempHome

M = 60.0


class Check(TempHome):
    def result(self, now, min_free=10, tail="5h=60m,week=2d", max_age="30m"):
        return commands.check_result(self.st, self.cfg, now, min_free, commands.parse_tail(tail, self.cfg),
                                     commands.parse_duration(max_age))

    def test_parse(self):
        t = commands.parse_tail("5h=45m,week=1.5d", self.cfg)
        self.assertEqual(t[FIVE_HOUR], 45 * 60)
        self.assertEqual(t[SEVEN_DAY], 1.5 * D)
        self.assertEqual(commands.parse_duration("90s"), 90)
        with self.assertRaises(ValueError):
            commands.parse_tail("month=2d", self.cfg)

    def test_5h_tail_with_surplus_is_go(self):
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(0, 21, 2):  # idle at 30% for 20 minutes
            self.feed(now - (20 - i) * M, five=(30, r5), week=(20, r7))
        res = self.result(now)
        self.assertTrue(res["ok"], res["reasons"])
        self.assertTrue(res["five_hour"]["harvestable"])
        self.assertAlmostEqual(res["five_hour"]["projected_unused"], 70)
        self.assertFalse(res["seven_day"]["harvestable"])

    def test_not_in_tail_is_no_go(self):
        now = MON + 3 * D + 10 * H
        self.feed(now, five=(30, int(now + 3 * H)), week=(20, int(MON + 7 * D)))
        res = self.result(now)
        self.assertFalse(res["ok"])
        self.assertIn("no window is in its tail", res["reasons"][0])

    def test_stale_data_is_no_go(self):
        now = MON + 3 * D + 10 * H
        self.feed(now - 2 * H, five=(30, int(now + 40 * M)), week=(20, int(MON + 7 * D)))
        res = self.result(now)
        self.assertFalse(res["ok"])
        self.assertTrue(res["stale"])

    def test_weekly_tail_go_and_tight_week_blocks_5h(self):
        r7 = int(MON + 7 * D)
        now = r7 - D  # one day before the weekly reset
        self.known("s1")  # a timed 40, not a first reading: a FIRST never anchors the weekly lookback
        for i in range(25, -1, -1):
            self.feed(now - i * H, week=(40, r7))  # idle week at 40%
        self.feed(now, five=(5, int(now + 4 * H)), week=(40, r7))
        res = self.result(now)
        self.assertTrue(res["ok"])
        self.assertTrue(res["seven_day"]["harvestable"])
        # Idle for a day, but the target still weighs (D-37): some spend is expected.
        i = weekly.PRIOR_SHARE / (1 / 7 + weekly.PRIOR_SHARE)
        self.assertAlmostEqual(res["seven_day"]["projected_unused"], 60 - i * 100 / 7)

    def test_weekly_harvest_waits_while_5h_runs_dry(self):
        r7 = int(MON + 7 * D)
        now = r7 - D
        for i in range(25, -1, -1):
            self.feed(now - i * H, week=(40, r7))
        r5 = int(now + 3 * H)
        for i in range(0, 21):  # burning 60 %/h in the 5h window
            self.feed(now - (20 - i) * M, five=(30 + i, r5), week=(40, r7))
        res = self.result(now)
        self.assertFalse(res["seven_day"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("no room" in r for r in res["reasons"]))

    def test_5h_leftover_blocked_when_week_runs_dry(self):
        r7 = int(MON + 7 * D)
        now = MON + 4 * D
        for i in range(30, -1, -1):  # weekly at 1 point per hour: runs dry
            self.feed(now - i * H, week=(90 - i, r7))
        r5 = int(now + 30 * M)
        for i in range(0, 21, 2):
            self.feed(now - (20 - i) * M + 1, five=(20, r5), week=(90, r7))
        res = self.result(now)
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("tight" in r for r in res["reasons"]))

    def test_heartbeats_do_not_make_old_data_fresh(self):
        # Review 4 (runtime-ui#2): an open idle session re-renders its last
        # reading every 60 s. Those heartbeats say nothing about the level, so
        # a level last read 3 hours ago is 3 hours old, not 1 minute.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for i in range(11):  # work from 4 h to 3 h ago: 5h 0 -> 30%
            self.feed(now - 4 * H + i * 6 * M, five=(3 * i, r5), week=(20, r7))
        for i in range(179, -1, -1):  # then idle, re-rendering every minute
            self.feed(now - i * M, five=(30, r5), week=(20, r7))
        beats = self.st.db.execute("SELECT COUNT(*) FROM samples WHERE flag=?", (HEARTBEAT,)).fetchone()[0]
        self.assertGreaterEqual(beats, 360)  # 180 per window
        res = self.result(now)
        self.assertTrue(res["stale"])
        self.assertFalse(res["ok"])
        self.assertAlmostEqual(res["data_age_s"], 3 * H)
        self.assertIn("no fresh reading in the last 30m", res["reasons"])

    def test_no_5h_rate_yet_is_no_go(self):
        # Review 5 (A-pace-display#0, X-integration#0, C-commands-config-mod#0):
        # a 10:00-15:00 window spent elsewhere until Claude Code reads 80% at
        # 14:10, then 2 points a minute (dry at 14:20). From the second real
        # reading, 14:11, to 14:15 there is no rate (D-11). Its leftover was
        # 100 - used, as if nothing more were spent: check said go for the 5h
        # window, and for a week in its tail.
        T = MON + 3 * D
        r5 = int(T + 15 * H)
        r7 = int(T + D + 14 * H)  # the week ends tomorrow 14:00: in its tail
        t0 = T + 14 * H + 10 * M
        self.known("s1")
        for i in range(25, 0, -1):
            self.feed(t0 - i * H, week=(40, r7))  # an idle week at 40%: 60 points to expire
        unknown = 0
        for ts in self.late_start(t0, 80, 2, 60, r5, week=(40, r7), renders=20):
            res = self.result(ts)
            f, k = res["five_hour"], res["seven_day"]
            self.assertFalse(res["ok"], (ts - t0, res["reasons"]))
            if f["verdict"] == "unknown":
                unknown += 1
                self.assertTrue(f["in_tail"])
                self.assertIsNone(f["projected_unused"])
                self.assertFalse(f["harvestable"])
                self.assertIsNone(res["weekly_cost_of_5h_leftover"])
                self.assertTrue(k["in_tail"])
                self.assertGreaterEqual(k["projected_unused"], 10)
                self.assertFalse(k["harvestable"])  # the 5h room test fails too
                self.assertEqual(res["reasons"], ["5h burn rate not known yet"])
        self.assertEqual(unknown, 12)  # 14:11:00 to 14:14:40, one render every 20 s

    def test_no_5h_rate_yet_is_no_go_whatever_min_free(self):
        # Review 6 (E-display-commands-weekly#0, X-integration#0): the same late
        # start with min_free 0. The unknown leftover (None) was compared as
        # `None or 0`, and 0 >= 0: the 5h window counted as harvestable, and
        # its reason formatted None as a number (TypeError, no JSON). Below 0
        # (--min-free was not clamped) the same; far above 100 nothing is
        # harvestable either.
        T = MON + 3 * D
        r5 = int(T + 15 * H)
        r7 = int(T + D + 14 * H)
        t0 = T + 14 * H + 10 * M
        self.known("s1")
        for i in range(25, 0, -1):
            self.feed(t0 - i * H, week=(40, r7))
        unknown = 0
        for ts in self.late_start(t0, 80, 2, 60, r5, week=(40, r7), renders=20):
            for min_free in (0, 0.0, -5, 1e9):
                res = self.result(ts, min_free=min_free)
                f, k = res["five_hour"], res["seven_day"]
                if f["verdict"] == "unknown":
                    unknown += 1
                    self.assertFalse(res["ok"], (min_free, ts - t0, res["reasons"]))
                    self.assertIsNone(f["projected_unused"])
                    self.assertFalse(f["harvestable"], min_free)
                    self.assertFalse(k["harvestable"], min_free)
                    self.assertEqual(res["reasons"], ["5h burn rate not known yet"], min_free)
        self.assertEqual(unknown, 12 * 4)

    def test_weekly_harvest_waits_for_the_5h_rate_with_min_free_0(self):
        # Review 6 (X-integration#0): the week ends tomorrow (in its tail) with
        # 60 points to expire, and a new 5h window opens. For its first 5
        # minutes there is no rate (D-11), so the weekly harvest's 5h room test
        # must fail (D-20); with min_free 0 it passed on `None or 0`.
        T = MON + 3 * D + 23 * H
        r7 = int(T + D)
        r5 = int(T + 5 * H)
        self.known("s1")
        for i in range(25, 0, -1):
            self.feed(T - i * H, week=(40, r7))  # an idle week at 40%
        seen = set()
        for j in range(1, 25):  # 2% from 30 s in, re-rendered every 20 s
            ts = T + 10 + 20 * j
            self.feed(ts, five=(2, r5), week=(40, r7))
            res = self.result(ts, min_free=0)
            f, k = res["five_hour"], res["seven_day"]
            seen.add(f["verdict"])
            self.assertTrue(k["in_tail"])
            if f["verdict"] == "unknown":
                self.assertFalse(k["harvestable"], ts - T)
                self.assertFalse(res["ok"], ts - T)
                self.assertEqual(res["reasons"], ["5h burn rate not known yet"])
            else:  # the rate is known: the 5h window has room, so go
                self.assertTrue(k["harvestable"], (ts - T, res["reasons"]))
                self.assertTrue(res["ok"], ts - T)
        self.assertIn("unknown", seen)
        self.assertGreater(len(seen), 1)  # and the rate comes in later

    def test_check_command_prints_json_with_min_free_0(self):
        # Review 6 (E-display-commands-weekly#0): `glidepath check --min-free 0`
        # in the late start raised a TypeError and printed nothing, while D-20
        # promises JSON on stdout either way. --min-free is now clamped to the
        # config's range (0 to 100), and nan or inf is an error.
        T = MON + 3 * D
        r5 = int(T + 15 * H)
        t0 = T + 14 * H + 10 * M
        self.known("s1")
        ts = list(self.late_start(t0, 80, 2, 60, r5, renders=4))[-1]  # 14:11, 82%: no rate yet

        def check(min_free):
            out = io.StringIO()
            args = argparse.Namespace(tail=None, max_age="30m", min_free=min_free)
            with mock.patch.object(commands.time, "time", return_value=ts), contextlib.redirect_stdout(out):
                code = commands.cmd_check(args, self.cfg)
            return code, json.loads(out.getvalue())

        for given, used in ((0.0, 0.0), (-5.0, 0.0), (250.0, 100.0), (None, 10.0)):
            code, res = check(given)
            self.assertEqual(code, 1, given)
            self.assertFalse(res["ok"])
            self.assertEqual(res["five_hour"]["verdict"], "unknown")
            self.assertEqual(repr(res["params"]["min_free"]), repr(used), given)
            self.assertIn("5h burn rate not known yet", res["reasons"])
        for bad in (float("nan"), float("inf")):
            code, res = check(bad)
            self.assertEqual(code, 1)
            self.assertFalse(res["ok"])
            self.assertIn("--min-free", res["error"])

    def test_tight_week_reason_prices_nothing_while_the_5h_rate_is_unknown(self):
        # Review 6 (E-display-commands-weekly#2): with no 5h rate the leftover
        # is unknown and the weekly cost null (D-20), but the tight week reason
        # still said "priced at 50 weekly points per 5h window" and the JSON
        # said exchange_rate_assumed. The week runs dry on its own here (1
        # point an hour at 90% on Friday), and the exchange rate is not known.
        r7 = int(MON + 7 * D)
        now = MON + 4 * D
        self.known("s1")
        for i in range(30, -1, -1):
            self.feed(now - i * H, week=(90 - i, r7))
        t0 = now + 10 * M
        r5 = int(t0 + 50 * M)
        states = set()
        for ts in self.late_start(t0, 80, 1, 60, r5, week=(90, r7), renders=24):
            res = self.result(ts)
            self.assertIsNone(res["exchange_rate"])
            self.assertIsNotNone(res["seven_day"]["dry_at"])
            self.assertFalse(res["ok"])
            tight = [r for r in res["reasons"] if "already tight" in r]
            self.assertEqual(len(tight), 1, res["reasons"])  # true either way: the week runs dry
            if res["five_hour"]["verdict"] == "unknown":
                states.add("unknown")
                self.assertIsNone(res["weekly_cost_of_5h_leftover"])
                self.assertFalse(res["exchange_rate_assumed"])  # nothing was priced
                self.assertNotIn("priced at", tight[0])
                self.assertIn("5h burn rate not known yet", res["reasons"])
            elif res["five_hour"]["spare"] > 0:
                states.add("priced")
                self.assertGreater(res["weekly_cost_of_5h_leftover"], 0)
                self.assertTrue(res["exchange_rate_assumed"])
                self.assertIn("priced at 50 weekly points per 5h window", tight[0])
            else:
                # Review 8 (delta-core#2): once it runs dry before the reset
                # nothing is left to price, but a leftover of 0 was "priced at
                # 50" and the JSON said exchange_rate_assumed.
                states.add("nothing left")
                self.assertEqual(res["weekly_cost_of_5h_leftover"], 0)
                self.assertFalse(res["exchange_rate_assumed"])
                self.assertEqual(tight[0], "the weekly budget is already tight")
        self.assertEqual(states, {"unknown", "priced", "nothing left"})

    def test_a_5h_leftover_of_0_is_not_priced(self):
        # Review 8 (delta-core#2): target_5h 70, idle at 75 in the tail (spare
        # 0) while the week runs dry on its own. Nothing was priced, yet the
        # JSON said exchange_rate_assumed and the reason "priced at 50".
        self.cfg["target_5h"] = 70
        r7 = int(MON + 7 * D)
        now = MON + 4 * D
        self.known("s1")
        for i in range(30, -1, -1):
            self.feed(now - i * H, week=(90 - i, r7))
        r5 = int(now + 40 * M)
        for i in range(0, 21, 2):
            self.feed(now - (20 - i) * M + 1, five=(75, r5), week=(90, r7))
        res = self.result(now)
        self.assertIsNone(res["exchange_rate"])
        self.assertEqual(res["five_hour"]["spare"], 0)
        self.assertEqual(res["weekly_cost_of_5h_leftover"], 0)
        self.assertFalse(res["exchange_rate_assumed"])
        self.assertIn("the weekly budget is already tight", res["reasons"])
        self.assertFalse([r for r in res["reasons"] if "priced at" in r or "leftover" in r], res["reasons"])

    def test_no_5h_rate_yet_is_no_go_from_a_new_session(self):
        # Review 5 (C-commands-config-mod#0, X-integration#0): the same from a
        # new session, 40 min before the reset at 50% and 1 point every 20 s
        # (180 %/h, dry 23 min before the reset); and 5 min before the hour
        # at 70% and 1 point a minute.
        T = MON + 3 * D
        r5 = int(T + 15 * H)
        for i, (t0, first, every) in enumerate(((T + 14 * H + 20 * M, 50, 20), (T + 14 * H + 5 * M, 70, 60))):
            self.st.close()
            self.st = Store(os.path.join(self.dir, "new-%d.db" % i))
            unknown = 0
            for ts in self.late_start(t0, first, 1, every, r5, renders=20):
                res = self.result(ts)
                if res["five_hour"]["verdict"] == "unknown":
                    unknown += 1
                    self.assertFalse(res["ok"], (i, ts - t0, res["reasons"]))
                    self.assertIsNone(res["five_hour"]["projected_unused"])
                    self.assertIn("5h burn rate not known yet", res["reasons"])
            self.assertGreater(unknown, 0, i)

    def test_unknown_exchange_rate_prices_5h_leftover_at_its_maximum(self):
        # Review 4 (runtime-ui#3): until the exchange rate is published (the
        # 5h side must rise 50 points), a 5h leftover was priced at 0 weekly
        # points. Here the week ends near 99%, and spending the 65 points left
        # in the 5h window costs about 5 weekly points at the rate seen so far.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 45 * M)
        r7 = int(now + 4 * H)
        t0 = now - 3.5 * H
        for k in range(26):  # 5h 10 -> 35%, week 95 -> 97%, then idle for 1 h
            self.feed(t0 + k * 6 * M, five=(10 + k, r5), week=(95 + 2 * k // 25, r7))
        res = self.result(now, max_age="2h")
        self.assertIsNone(res["exchange_rate"])
        self.assertTrue(res["exchange_rate_assumed"])
        self.assertAlmostEqual(res["five_hour"]["projected_unused"], 65)
        self.assertIsNone(res["seven_day"]["dry_at"])
        self.assertLess(res["seven_day"]["projected"], 100)
        self.assertAlmostEqual(res["weekly_cost_of_5h_leftover"], 32.5)  # 65 points at 50 per full 5h window
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("tight" in r for r in res["reasons"]), res["reasons"])

    def test_known_exchange_rate_is_not_flagged_as_assumed(self):
        # Review 4 (runtime-ui#3): once published (50 paired 5h points, 3
        # weekly), the observed rate prices the leftover: 3 per 50 is 6 per window.
        now = MON + 3 * D + 10 * H
        r5 = int(now + 45 * M)
        r7 = int(MON + 7 * D)
        t0 = now - 4 * H
        for k in range(51):  # 5h 0 -> 50%, week 20 -> 23%, then idle for 1 h
            self.feed(t0 + k * 3.6 * M, five=(k, r5), week=(20 + 3 * k // 50, r7))
        res = self.result(now, max_age="2h")
        self.assertAlmostEqual(res["exchange_rate"], 6.0)
        self.assertFalse(res["exchange_rate_assumed"])
        self.assertAlmostEqual(res["weekly_cost_of_5h_leftover"], 3.0)  # 50 points left at 6 per window
        self.assertTrue(res["five_hour"]["harvestable"])

    def test_nothing_left_is_not_spare_with_min_free_0(self):
        # Review 6 follow-up: at min_free 0, "at least min_free" passed a maxed
        # 5h window (0 points free), so the weekly harvest said go into it.
        now = MON + 6 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(now + D)  # the week is in its tail with 60 points to expire
        self.known("s1")
        for i in range(0, 21, 2):  # maxed for 20 minutes
            self.feed(now - (20 - i) * M, five=(100, r5), week=(40, r7))
        res = self.result(now, min_free=0)
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["seven_day"]["harvestable"])
        self.assertFalse(res["ok"])

    def fresh_store(self, name):
        self.st.close()
        self.st = Store(os.path.join(self.dir, name))

    def test_spare_5h_quota_is_measured_against_its_target(self):
        # Review 7 (checks-colors#1, integration#0): with target_5h 90 ("90
        # keeps a 10-point reserve"), check measured the 5h leftover against
        # 100. Idle at 90 (or 92, min_free 5) in the tail, the line was yellow
        # and check said go on ~10% (~8%); idle at 84 the line was green and
        # check harvested ~16%, ending at 100. Spare is now max(0, 90 -
        # projected), on the line and in check alike.
        self.cfg["target_5h"] = 90
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        cases = ((92, 5, "warn", 0, False), (90, 10, "warn", 0, False), (84, 10, "dim", 6, False),
                 (60, 10, "opportunity", 30, True))
        for used, min_free, tone, spare, go in cases:
            with self.subTest(used=used, min_free=min_free):
                self.fresh_store("t5-%d.db" % used)
                self.cfg["min_free"] = min_free
                for i in range(0, 21, 2):  # idle for 20 minutes
                    self.feed(now - (20 - i) * M, five=(used, r5), week=(20, r7))
                res = self.result(now, min_free=min_free)
                f = res["five_hour"]
                self.assertEqual(f["harvestable"], go, res["reasons"])
                self.assertEqual(res["ok"], go)
                self.assertEqual(commands.views(self.st, self.cfg, now)[0]["severity"], tone)
                self.assertEqual(f["target"], 90)
                self.assertAlmostEqual(f["projected_unused"], 100 - used)  # still out of 100
                self.assertAlmostEqual(f["spare"], spare)
                if used >= 90:
                    self.assertIn("5h window is at or above its 90% target", res["reasons"])
                if go:
                    self.assertIn("5h window ends in 40m with ~30% projected unused below its 90% target", res["reasons"])
                    self.assertAlmostEqual(res["weekly_cost_of_5h_leftover"], 15)  # 30 points at 50 per window

    def test_a_5h_window_that_should_slow_down_is_not_harvested(self):
        # Review 7 (checks-colors#1): target_5h 80, 75% at 15 %/h with 40
        # minutes left: projected 85, slow down (yellow ▼), yet check said go
        # on its ~15% projected unused.
        self.cfg["target_5h"] = 80
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        self.known("s1")
        for i in range(21):
            self.feed(now - (20 - i) * M, five=(70 + 0.25 * i, r5), week=(20, int(MON + 7 * D)))
        res = self.result(now)
        f = res["five_hour"]
        self.assertEqual(f["verdict"], "slow_down")
        self.assertAlmostEqual(f["projected"], 85, delta=0.5)
        self.assertFalse(f["harvestable"])
        self.assertFalse(res["ok"])
        self.assertEqual(commands.views(self.st, self.cfg, now)[0]["severity"], "warn")
        self.assertEqual(f["spare"], 0)
        self.assertIn("5h window is spending faster than its 80% target allows (slow down)", res["reasons"])

    def test_a_fractional_target_is_printed_as_set(self):
        # Review 8 (delta-core#3): targets were printed with %.0f, so a target
        # of 99.5 read "at or above its 100% target", and a go reason said
        # "below its 100% target", a suffix only a target below 100 gets.
        self.cfg["target_5h"] = 99.5
        self.cfg["target_week"] = 90.4
        now = MON + 3 * D + 10 * H
        r5 = int(now + 40 * M)
        r7 = int(MON + 7 * D)
        for used, reason in ((30, "5h window ends in 40m with ~70% projected unused below its 99.5% target"),
                             (99.6, "5h window is at or above its 99.5% target")):
            with self.subTest(used=used):
                self.fresh_store("frac-%s.db" % used)
                for i in range(0, 21, 2):  # idle for 20 minutes
                    self.feed(now - (20 - i) * M, five=(used, r5), week=(20, r7))
                self.assertIn(reason, self.result(now)["reasons"])
        v5, vw, _ = commands.views(self.st, self.cfg, now)
        out = io.StringIO()
        with mock.patch.object(commands.time, "time", return_value=now), contextlib.redirect_stdout(out):
            commands._print_5h(v5, self.cfg)
            commands._print_week(vw, None, now)
        self.assertIn("(target 99.5%)", out.getvalue())
        self.assertIn("= (99.5 - 99.6) /", out.getvalue())
        self.assertIn("(target 90.4%)", out.getvalue())

    def week_in_its_tail(self, levels, five=None):
        """A week that reads `levels` (oldest first) hour by hour up to now,
        one day before its reset; `five` adds a 5h reading now."""
        r7 = int(MON + 7 * D)
        now = r7 - D
        self.known("s1")
        for i, lvl in enumerate(levels):
            self.feed(now - (len(levels) - 1 - i) * H, week=(lvl, r7))
        if five is not None:
            self.feed(now, five=(five[0], int(now + five[1])), week=(levels[-1], r7))
        return now

    def test_spare_weekly_quota_is_measured_against_its_target(self):
        # Review 7 (checks-colors#1, integration#0): target_week 90. Idle at
        # 92 (yellow) with min_free 5, check said go on ~8% to expire; idle at
        # 85, it refused a 5h leftover as "already tight" and said go on the
        # week's ~15% in the same JSON, which takes the week to 100.
        self.cfg["target_week"] = 90
        # Idle for a day, the target still weighs (D-37): 5.3 points expected.
        spend = weekly.PRIOR_SHARE / (1 / 7 + weekly.PRIOR_SHARE) * 90 / 7
        cases = ((92, 5, "warn", 0, False), (85, 10, "dim", 0, False), (75, 5, "opportunity", 15 - spend, True),
                 (70, 10, "opportunity", 20 - spend, True))
        for used, min_free, tone, spare, go in cases:
            with self.subTest(used=used, min_free=min_free):
                self.fresh_store("tw-%d-%d.db" % (used, min_free))
                self.cfg["min_free"] = min_free
                now = self.week_in_its_tail([used] * 26, five=(5, 4 * H))
                res = self.result(now, min_free=min_free)
                k = res["seven_day"]
                self.assertEqual(k["harvestable"], go, res["reasons"])
                self.assertEqual(res["ok"], go)
                self.assertEqual(commands.views(self.st, self.cfg, now)[1]["severity"], tone)
                self.assertAlmostEqual(k["projected_unused"], 100 - used - spend)
                self.assertAlmostEqual(k["spare"], spare)
                if used >= 90:
                    self.assertIn("weekly window is at or above its 90% target", res["reasons"])
                if go:
                    self.assertIn("weekly window ends in 1d 00h with ~%.0f%% projected to expire below its 90%% target"
                                  % spare, res["reasons"])
        # The week at 85 with the 5h window in its tail: no go on either.
        self.fresh_store("tw-tail.db")
        self.cfg["min_free"] = 10
        now = self.week_in_its_tail([85] * 26, five=(30, 40 * M))
        res = self.result(now)
        self.assertFalse(res["five_hour"]["harvestable"])
        self.assertFalse(res["seven_day"]["harvestable"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("already tight" in r for r in res["reasons"]), res["reasons"])

    def test_a_week_that_should_slow_down_is_not_harvested(self):
        # Review 7 (integration#0): target_week 85, half a point an hour, 75%
        # one day before the reset: projected 87, slow down (yellow ▼), yet
        # check said go on the week's ~13% to expire.
        self.cfg["target_week"] = 85
        now = self.week_in_its_tail([75 - 0.5 * h for h in range(30, -1, -1)], five=(5, 4 * H))
        res = self.result(now)
        k = res["seven_day"]
        self.assertEqual(k["verdict"], "slow_down")
        self.assertAlmostEqual(k["projected"], 87, delta=0.5)
        self.assertFalse(k["harvestable"])
        self.assertFalse(res["ok"])
        self.assertEqual(commands.views(self.st, self.cfg, now)[1]["severity"], "warn")
        self.assertEqual(k["spare"], 0)
        self.assertIn("weekly window is spending faster than its 85% target allows (slow down)", res["reasons"])

    def test_bad_durations_are_errors(self):
        # Review 6 follow-up: --max-age nan made every age "fresh".
        for bad in ("nan", "inf", "-5m", "nanm"):
            with self.assertRaises(ValueError, msg=bad):
                commands.parse_duration(bad)


if __name__ == "__main__":
    unittest.main()
