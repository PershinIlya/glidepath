"""5-hour math against hand-computed values."""

import json
import os
import unittest

from glidepath import config, pace, render
from glidepath.config import FIVE_HOUR
from glidepath.store import Store

from .helpers import MON, H, TempHome

M = 60.0


class Slope(unittest.TestCase):
    def test_lsq_slope_by_hand(self):
        # (0 h, 0), (1 h, 10), (2 h, 20): 10 %/h exactly.
        self.assertAlmostEqual(pace.lsq_slope([(0, 0), (3600, 10), (7200, 20)]), 10.0)
        # x = 0, 1/12, 1/6 h; y = 40, 40, 50:
        # Sxy = (1/12)(10/3) + (1/12)(20/3) = 10/12, Sxx = 2/144, slope = 60 %/h.
        self.assertAlmostEqual(pace.lsq_slope([(0, 40), (300, 40), (600, 50)]), 60.0)
        self.assertIsNone(pace.lsq_slope([(5, 1)]))
        self.assertIsNone(pace.lsq_slope([(5, 1), (5, 2)]))

    def test_running_series_ignores_heartbeat_values(self):
        rows = [(1, 10, 0), (2, 5, 1), (3, 12, 0), (4, 3, 1)]
        self.assertEqual(pace.running_series(rows), [(1, 10), (2, 10), (3, 12), (4, 12)])


class FiveHour(TempHome):
    def setUp(self):
        super().setUp()
        self.t = MON + 10 * H
        self.R = int(self.t + 2 * H)

    def view(self):
        return pace.five_hour(self.st, self.t, self.cfg)

    def test_steady_burn(self):
        # 0.1 point per minute = 6 %/h for 20 minutes, ending at 50%.
        for i in range(21):
            self.feed(self.t - (20 - i) * M, five=(round(48 + 0.1 * i, 1), self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 6.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")
        self.assertAlmostEqual(v["allowed"], 25.0)  # (100 - 50) / 2 h
        self.assertAlmostEqual(v["multiplier"], 25.0 / 6.0)
        self.assertAlmostEqual(v["projected"], 62.0)  # 50 + 6 * 2
        self.assertIsNone(v["cap_ts"])
        self.assertEqual(v["verdict"], "speed_up")
        self.assertEqual(v["severity"], "dim")

    def test_burst_then_cap_time(self):
        self.known("s1", "s2")
        self.feed(self.t - 20 * M, five=(40, self.R))
        self.feed(self.t - 10 * M, five=(40, self.R), sid="s2")
        self.st.add_sample(self.t - 5 * M, FIVE_HOUR, 70, self.R, "test", "idle", 1, 1)  # a heartbeat: no level info
        self.feed(self.t, five=(50, self.R))
        v = self.view()
        # Level path: 40 at -20 min (s2's 40 is no new level), 50 now: 10 points
        # in 1/3 h = 30 %/h; three fresh readings in the window, so "lsq".
        self.assertAlmostEqual(v["rate"], 30.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")
        self.assertAlmostEqual(v["multiplier"], 25.0 / 30.0)  # allowed (100 - 50) / 2 h
        self.assertEqual(v["verdict"], "slow_down")
        self.assertAlmostEqual(v["projected_raw"], 110.0)
        self.assertEqual(v["projected"], 100.0)
        self.assertAlmostEqual(v["cap_ts"], self.t + 100 * M)  # (100 - 50) / 30 h
        # D-37: 25% slower (22.5 %/h) it lasts to the reset: may run out, yellow.
        self.assertFalse(v["sure_out"])
        self.assertEqual(v["severity"], "warn")
        # 90 at 60 %/h with 70 min left: even at 45 %/h it runs out 57 min early: red.
        for k, used in enumerate((60, 70, 80, 90)):
            self.feed(self.t + (20 + 10 * k) * M, five=(used, self.R))
        v = pace.five_hour(self.st, self.t + 50 * M, self.cfg)
        self.assertTrue(v["sure_out"])
        self.assertEqual(v["severity"], "alert")

    def test_heartbeats_never_make_a_burst(self):
        # An idle session re-renders its stale 52 after a sleep; the busy one read 60 and 61.
        self.feed(self.t - 60 * M, five=(60, self.R), sid="busy")
        self.feed(self.t - 59 * M, five=(52, self.R), sid="idle")  # its first, lower reading
        self.feed(self.t - 8 * M, five=(52, self.R), sid="idle")  # heartbeat after a 40-minute sleep
        self.feed(self.t - 7 * M, five=(61, self.R), sid="busy")
        for k in range(1, 8):
            self.feed(self.t - 7 * M + k * 60, five=(61, self.R), sid="busy")
        v = self.view()
        # Level path: 60 an hour ago, linear to 61 seven minutes ago, then flat:
        # a few %/h at most; never a burst from the idle session's stale 52.
        self.assertLess(v["rate"], 4.0)
        self.assertIsNone(v["cap_ts"])

    def test_open_session_heartbeats_do_not_squeeze_a_gap(self):
        self.feed(self.t - 2 * H, five=(30, self.R))
        for k in range(1, 120):  # the session stays open: a heartbeat a minute at 30
            self.feed(self.t - 2 * H + k * 60, five=(30, self.R))
        self.feed(self.t, five=(70, self.R), sid="s2")  # 40 points spent on claude.ai meanwhile
        v = self.view()
        self.assertAlmostEqual(v["rate"], 20.0, places=6)  # spread over the 2 h gap
        self.assertIsNone(v["cap_ts"] if v["projected_raw"] < 100 else None)

    def test_idle_is_zero_rate_and_shows_allowed(self):
        self.feed(self.t - 30 * M, five=(50, self.R))
        for i in range(1, 31, 2):
            self.feed(self.t - 30 * M + i * M, five=(50, self.R))  # heartbeats every 2 min
        v = self.view()
        self.assertEqual(v["rate"], 0.0)
        self.assertTrue(v["idle"])
        self.assertEqual(v["verdict"], "idle")
        self.assertIsNone(v["multiplier"])
        self.assertAlmostEqual(v["allowed"], 25.0)
        self.assertEqual(v["projected"], 50)

    def test_burst_fades_out_of_the_fit_window(self):
        self.feed(self.t - 40 * M, five=(30, self.R))
        self.feed(self.t - 38 * M, five=(45, self.R))  # burst 40 minutes ago
        for i in range(37, -1, -1):
            self.feed(self.t - i * M, five=(45, self.R))
        self.assertEqual(self.view()["rate"], 0.0)

    def test_one_reading_gives_the_window_average(self):
        r = int(self.t + 3 * H)  # window started 2 h ago, at 0
        self.feed(self.t, five=(30, r))
        v = self.view()
        self.assertEqual(v["rate_source"], "interpolated")
        self.assertAlmostEqual(v["rate"], 15.0)  # the path rises 30 points over 2 h
        self.assertAlmostEqual(v["allowed"], 70.0 / 3.0)
        self.assertAlmostEqual(v["projected"], 75.0)

    def test_unobserved_gap_is_averaged_not_squeezed_into_20_minutes(self):
        self.feed(self.t - 2 * H, five=(30, self.R))  # last read 2 h ago
        self.feed(self.t, five=(70, self.R))  # claude.ai use in between
        v = self.view()
        self.assertEqual(v["rate_source"], "interpolated")
        self.assertAlmostEqual(v["rate"], 20.0)  # 40 points over 2 h, not 120 %/h
        # 70 + 20 * 2 h = 110: the cap comes after 30 / 20 = 1.5 h, not in 15 minutes.
        self.assertAlmostEqual(v["projected_raw"], 110.0)
        self.assertAlmostEqual(v["cap_ts"], self.t + 1.5 * H)

    def test_idle_after_early_burst_is_idle_not_dry(self):
        r = int(self.t + 30 * M)  # window opened 4.5 h ago
        for i in range(0, 61, 5):  # 90 points in its first hour
            self.feed(r - 5 * H + i * M, five=(min(90, 1.5 * i), r))
        self.feed(self.t - 120, five=(90, r))  # first reading after 3.5 idle hours
        self.feed(self.t - 60, five=(90, r), sid="s2")
        self.feed(self.t, five=(90, r), sid="s3")  # readings minutes apart must not hide the gap
        v = self.view()
        self.assertEqual(v["rate"], 0.0)
        self.assertEqual(v["verdict"], "idle")
        self.assertEqual(v["severity"], "opportunity")  # 10 points left, 30 min to go

    def test_maxed(self):
        self.feed(self.t, five=(100, self.R))
        v = self.view()
        self.assertEqual(v["verdict"], "maxed")
        self.assertEqual(v["severity"], "alert")

    def test_tail_with_surplus_is_an_opportunity(self):
        r = int(self.t + 30 * M)
        for i in range(0, 21, 2):
            self.feed(self.t - (20 - i) * M, five=(40, r))
        v = self.view()
        self.assertTrue(v["in_tail"])
        self.assertEqual(v["unused_at_reset"], 60)
        self.assertEqual(v["severity"], "opportunity")

    def test_no_window(self):
        self.assertFalse(self.view()["present"])
        self.feed(self.t - 6 * H, five=(10, int(self.t - 2 * H)))
        self.assertFalse(self.view()["present"])

    def test_no_window_keeps_an_unstored_reading(self):
        # Review 7 (fresh-eyes#2): when the write failed at a rollover, only
        # the expired instance was there and the reading was dropped ("5h 0%"
        # while the payload said 12%). It is kept for display, with no math.
        self.assertEqual(pace.five_hour(self.st, self.t, self.cfg, (12.0, self.R))["raw"], (12.0, self.R))
        self.feed(self.t - 6 * H, five=(80, int(self.t - 2 * H)))
        v = pace.five_hour(self.st, self.t, self.cfg, (12.0, self.R))
        self.assertFalse(v["present"])
        self.assertEqual(v["raw"], (12.0, self.R))
        self.assertEqual(v["severity"], "dim")
        self.assertNotIn("raw", self.view())

    def test_steady_whole_percent_steps_keep_their_rate(self):
        # Review #0: 9 %/h in whole percents is one new value every 6 min 40 s.
        # 85% with 2 h left: allowed 7.5 %/h, so every render (one every 20 s)
        # must say slow down and show the cap, also between steps.
        self.known("s1")
        t0 = self.t - 60 * M
        for k in range(0, 181):
            ts = t0 + 20 * k
            self.feed(ts, five=(int(76 + 9 * (ts - t0) / H), self.R))
            if ts >= self.t - 20 * M:
                v = pace.five_hour(self.st, ts, self.cfg)
                self.assertEqual(v["verdict"], "slow_down", ts - self.t)
                self.assertIsNotNone(v["cap_ts"], ts - self.t)

    def test_slow_steady_work_averages_out(self):
        # 4 %/h in whole percents: a step every 15 min. Renders every 20 s over
        # 2 h average within 10% of 4 %/h.
        self.known("s1")
        r = self.R  # the window opened 3 h ago
        t0 = self.t - 2 * H
        rates = []
        for k in range(0, 361):
            ts = t0 + 20 * k
            self.feed(ts, five=(int(20 + 4 * (ts - t0) / H + 0.5), r))
            if k >= 60:
                rates.append(pace.five_hour(self.st, ts, self.cfg)["rate"])
        self.assertAlmostEqual(sum(rates) / len(rates), 4.0, delta=0.4)

    def test_a_late_start_is_not_diluted_by_the_ramp_from_0(self):
        # Review #1: the window opened at 10:00, work began at 10:50 at 30 %/h.
        self.known("s1")
        start = self.t - 56 * M  # now is 10:56
        r = int(start + 5 * H)
        for i, lvl in enumerate((1, 2, 3, 4)):
            self.feed(start + (50 + 2 * i) * M, five=(lvl, r))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 30.0, delta=1.0)
        self.assertEqual(v["rate_source"], "lsq")

    def test_a_late_start_from_a_new_session_waits_for_data(self):
        # Review 4: the same late start from a new session. Its 10:50 reading is
        # FIRST and lies under the ramp (1.92 at 10:50), so the first real point
        # is 10:52. At 10:56 only 4 minutes follow it: no rate yet, instead of a
        # ramp-diluted "lsq" speed up; at 10:58 the fit is 30 %/h.
        start = self.t - 56 * M  # now is 10:56
        r = int(start + 5 * H)
        for i, lvl in enumerate((1, 2, 3, 4)):
            self.feed(start + (50 + 2 * i) * M, five=(lvl, r))
        v = self.view()
        self.assertIsNone(v["rate"])
        self.assertEqual(v["verdict"], "unknown")
        self.t = start + 58 * M
        self.feed(self.t, five=(5, r))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 30.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")

    def renders_from_a_late_start(self, rate):
        """A new session works from 10:50 in a 10:00 window at `rate` %/h, in
        whole percents, with a render every 20 s for 40 minutes."""
        r = int(self.t + 5 * H)  # self.t is 10:00
        out = []
        for k in range(0, 40 * 60 + 1, 20):
            ts = self.t + 50 * M + k
            self.feed(ts, five=(int(1 + rate * k / H), r))
            out.append((ts, pace.five_hour(self.st, ts, self.cfg)))
        return out

    def test_steady_work_at_the_start_of_a_window_never_reads_idle(self):
        # Review 4: at 9 %/h the fit jumped to the first reading 5 minutes after
        # it, with no data after it yet, and read idle while work went on.
        idle = [ts - self.t for ts, v in self.renders_from_a_late_start(9) if v["verdict"] == "idle"]
        self.assertEqual(idle, [])

    def test_the_start_of_a_window_never_flips_from_speed_up_to_slow_down(self):
        # Review 4: at 30 %/h (allowed is about 24 %/h) renders said "x4.5 speed
        # up" without the ~ marker, then "x0.8 slow down, dry" with no new data.
        out = self.renders_from_a_late_start(30)
        lsq_up = [ts - self.t for ts, v in out if v["verdict"] == "speed_up" and v["rate_source"] == "lsq"]
        self.assertEqual(lsq_up, [])
        verdicts = [v["verdict"] for _, v in out]
        first_down = verdicts.index("slow_down")
        self.assertEqual(set(verdicts[first_down:]), {"slow_down"})
        self.assertAlmostEqual(out[-1][1]["rate"], 30.0, delta=1.0)

    def test_no_rate_yet_in_the_tail_is_never_green(self):
        # Review 5 (A-pace-display#0, X-integration#0, C-commands-config-mod#0):
        # a 10:00-15:00 window spent elsewhere until Claude Code reads it late.
        # From the second real reading there is no rate for 5 minutes (D-11),
        # and its leftover was 100 - used, as if nothing more were spent: the
        # tail turned green while the window burned toward dry.
        start = MON + 10 * H
        r = int(start + 5 * H)
        cases = [  # (first reading, level, step, seconds per step, known session)
            (start + 4 * H + 10 * M, 80, 2, 60, True),  # 120 %/h: dry at 14:20
            (start + 4 * H + 10 * M, 80, 2, 60, False),  # the same from a new session
            (start + 4 * H + 5 * M, 70, 1, 60, False),  # 60 %/h: dry at 14:35
            (start + 4 * H + 20 * M, 50, 1, 20, False),  # 180 %/h: dry at 14:36:40
        ]
        for i, (t0, first, step, every, known) in enumerate(cases):
            self.st.close()
            self.st = Store(os.path.join(self.dir, "tail-%d.db" % i))
            if known:
                self.known("s1")
            out = [pace.five_hour(self.st, ts, self.cfg) for ts in self.late_start(t0, first, step, every, r, renders=30)]
            unknown = [v for v in out if v["verdict"] == "unknown"]
            self.assertTrue(unknown, i)  # the rate-less state is reached in the tail
            for v in unknown:
                self.assertTrue(v["in_tail"])
                self.assertIsNone(v["unused_at_reset"], i)  # unknown, not 100 - used
                self.assertEqual(v["severity"], "dim", i)
            # Once the rate is known, the window runs dry: red.
            self.assertEqual(out[-1]["severity"], "alert", i)

    def test_no_rate_yet_in_the_tail_is_never_green_with_min_free_0(self):
        # Review 6 (E-display-commands-weekly#0, X-integration#0): with min_free
        # 0 in config.json (allowed: 0 to 100), the unknown leftover was read
        # as `None or 0`, and 0 >= 0 turned the late start green again.
        self.cfg["min_free"] = 0.0
        start = MON + 10 * H
        r = int(start + 5 * H)
        self.known("s1")
        out = [pace.five_hour(self.st, ts, self.cfg)
               for ts in self.late_start(start + 4 * H + 10 * M, 80, 2, 60, r, renders=30)]
        unknown = [v for v in out if v["verdict"] == "unknown"]
        self.assertEqual(len(unknown), 12)
        for v in unknown:
            self.assertTrue(v["in_tail"])
            self.assertEqual(v["severity"], "dim")
            segs = render.segments(v, {"present": False}, self.cfg)
            self.assertEqual(segs[0][1], "dim")
        self.assertEqual(out[-1]["severity"], "alert")
        base = dict(present=True, cap_ts=None, in_tail=True, verdict="unknown", unused_at_reset=None)
        self.assertEqual(pace.severity(base, self.cfg), "dim")
        self.assertEqual(pace.severity(dict(base, verdict="idle", unused_at_reset=5.0), self.cfg), "opportunity")
        # The config clamps min_free to 0..100 (config.RANGES); unclamped, the
        # unknown leftover still never passes.
        path = os.path.join(self.dir, "config.json")
        for given, kept in ((-5, 0.0), (1e9, 100.0)):
            with open(path, "w", encoding="utf-8", newline="") as f:
                json.dump({"min_free": given}, f)
            self.assertEqual(config.load(path)["min_free"], kept)
            self.assertEqual(pace.severity(base, dict(self.cfg, min_free=given)), "dim")

    def test_one_reading_fades_out_over_the_fit_window(self):
        # Review 4: a single reading gave the window average for 5 minutes, then
        # read idle at once. It now fades out over the 20-minute fit window.
        t0 = self.t
        r = int(t0 + 3 * H)  # window started 2 h ago, at 0
        self.feed(t0, five=(30, r))
        # +5 min: the fit spans -15..+5 min through 26.25 (on the ramp), 30, 30:
        # Sxy = 43.75, Sxx = 650/3 (minutes), slope 21/104 per minute = 315/26 %/h.
        self.t = t0 + 5 * M
        v = self.view()
        self.assertAlmostEqual(v["rate"], 315.0 / 26.0)
        self.assertEqual(v["rate_source"], "interpolated")
        self.assertEqual(v["verdict"], "speed_up")
        # +10 min: 27.5, 30, 30 at -10, 0, +10 min: 25 / 200 per minute = 7.5 %/h.
        self.t = t0 + 10 * M
        self.assertAlmostEqual(self.view()["rate"], 7.5)
        # +20 min: no spend is left in the fit window.
        self.t = t0 + 20 * M
        self.assertEqual(self.view()["verdict"], "idle")

    def test_a_first_reading_off_the_path_is_no_sample(self):
        # Review 4: s2's stale first 35 lies under the path; counting it made a
        # fit of two readings look well sampled (no ~ marker).
        self.known("s1")
        self.feed(self.t - 20 * M, five=(40, self.R))
        self.feed(self.t - 10 * M, five=(35, self.R), sid="s2")
        self.feed(self.t, five=(50, self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 30.0, places=6)  # 10 points in 20 minutes
        self.assertEqual(v["rate_source"], "interpolated")

    def test_a_first_reading_on_the_path_is_a_sample(self):
        # Review 4: s2's first 47 lifts the path (45 there), so it is a point of
        # it and counts: 40, 47, 50 at -20, -10, 0 min is a 30 %/h fit.
        self.known("s1")
        self.feed(self.t - 20 * M, five=(40, self.R))
        self.feed(self.t - 10 * M, five=(47, self.R), sid="s2")
        self.feed(self.t, five=(50, self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 30.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")

    def test_a_mild_overshoot_is_yellow_not_red(self):
        # Review 4: at target 100 every slow down also runs dry, so yellow never
        # showed. 27 %/h at 50% with 2 h left: allowed 25 %/h, multiplier 25/27
        # = 0.93 (on pace), 100% at +50/27 h, before the reset: yellow.
        self.known("s1")
        for i, lvl in enumerate((41, 45.5, 50)):
            self.feed(self.t - (20 - 10 * i) * M, five=(lvl, self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 27.0, places=6)
        self.assertEqual(v["rate_source"], "lsq")
        self.assertEqual(v["verdict"], "on_pace")
        self.assertAlmostEqual(v["cap_ts"], self.t + 50.0 / 27.0 * H)
        self.assertEqual(v["severity"], "warn")

    def test_severity_tiers(self):
        # Review 4: red is maxed, or a dry time with a slow down verdict; a dry
        # time with an on pace verdict, or slow down without a dry time (a
        # target below 100), is yellow.
        base = dict(present=True, cap_ts=None, in_tail=False, unused_at_reset=0)
        cases = [
            (dict(verdict="maxed"), "alert"),
            (dict(verdict="slow_down", cap_ts=self.t), "alert"),
            (dict(verdict="on_pace", cap_ts=self.t), "warn"),
            (dict(verdict="slow_down"), "warn"),
            (dict(verdict="on_pace"), "dim"),
            (dict(verdict="idle", in_tail=True, unused_at_reset=20), "opportunity"),
            # Review 5: no rate, no known leftover (D-11); at or above a target
            # below 100 is yellow whatever the verdict (B-weekly-profile#2).
            (dict(verdict="unknown", in_tail=True, unused_at_reset=None), "dim"),
            (dict(verdict="idle", in_tail=True, unused_at_reset=20, target=80, used=80), "warn"),
            (dict(verdict="unknown", target=80, used=85, unused_at_reset=None), "warn"),
            # Review 7 (checks-colors#1, integration#0): green counts only what
            # expires below the target. Idle at 75 of 80 has 25 points to
            # expire but 5 to spare (min_free 10): it was green; at 65, 15.
            (dict(verdict="idle", in_tail=True, unused_at_reset=25, target=80, used=75), "dim"),
            (dict(verdict="idle", in_tail=True, unused_at_reset=35, target=80, used=65), "opportunity"),
        ]
        for kw, tone in cases:
            self.assertEqual(pace.severity(dict(base, **kw), self.cfg), tone, kw)

    def test_short_rate_window_still_gives_a_rate(self):
        # Review #2: rate_window_min below rate_min_span_min used to give no rate.
        self.cfg["rate_window_min"] = 4
        self.known("s1")
        for i in range(11):
            self.feed(self.t - (10 - i) * M, five=(40 + i, self.R))
        v = self.view()
        self.assertAlmostEqual(v["rate"], 60.0, delta=1.0)

    def test_stale_first_reading_at_install_is_no_burst(self):
        # Review #3: at install every open session re-renders. An idle one shows
        # its 3-hour-old 30, a busy one 60 a minute later: 30 points did not go
        # in one minute.
        tt = self.t
        self.feed(tt - 2 * M, five=(30, self.R), sid="idle")
        self.feed(tt - 1 * M, five=(60, self.R), sid="busy")
        v = self.view()
        self.assertLess(v["rate"], 25.0)
        self.assertIsNone(v["cap_ts"])
        self.t = tt + 4 * M
        self.feed(self.t, five=(61, self.R), sid="busy")
        v = self.view()
        self.assertLess(v["rate"], 25.0)
        self.assertIsNone(v["cap_ts"])

    def test_target_below_100(self):
        self.cfg["target_5h"] = 80
        for i in range(21):
            self.feed(self.t - (20 - i) * M, five=(round(48 + 0.1 * i, 1), self.R))
        v = self.view()
        self.assertAlmostEqual(v["allowed"], 15.0)  # (80 - 50) / 2
        self.assertAlmostEqual(v["multiplier"], 2.5)

    def test_idle_above_a_target_below_100_is_yellow(self):
        # Review 5 (B-weekly-profile#2): with target_5h 80, an idle window at
        # 85% 50 minutes before its reset was green (15 points "about to
        # expire"), next to "idle ≤0.0%/h": it invited spending the reserve.
        self.cfg["target_5h"] = 80
        r = int(self.t + 50 * M)
        for i in range(0, 21, 2):
            self.feed(self.t - (20 - i) * M, five=(85, r))
        v = self.view()
        self.assertEqual(v["verdict"], "idle")
        self.assertTrue(v["in_tail"])
        self.assertAlmostEqual(v["unused_at_reset"], 15.0)
        self.assertEqual(v["allowed"], 0.0)
        self.assertEqual(v["severity"], "warn")

class Spare(unittest.TestCase):
    def test_spare_needs_some_points(self):
        # Review 6 follow-up: with min_free 0, a leftover of 0 is not spare,
        # and an unknown one never is.
        self.assertTrue(pace.spare(5, 0))
        self.assertTrue(pace.spare(10, 10))
        self.assertFalse(pace.spare(9.9, 10))
        self.assertFalse(pace.spare(0, 0))
        self.assertFalse(pace.spare(None, 0))
        cfg = {"min_free": 0}
        v = {"present": True, "verdict": "on_pace", "in_tail": True, "unused_at_reset": 0.0, "target": 100.0, "used": 99.0}
        self.assertEqual(pace.severity(v, cfg), "dim")
        self.assertEqual(pace.severity(dict(v, unused_at_reset=1.0), cfg), "opportunity")


if __name__ == "__main__":
    unittest.main()
