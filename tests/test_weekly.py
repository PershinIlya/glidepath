"""Weekly math against hand-computed values."""

import math
import os
import unittest

from glidepath import profile, render, weekly
from glidepath.config import FIVE_HOUR, SEVEN_DAY
from glidepath.store import Store

from .helpers import MON, H, D, TempHome

K = weekly.PRIOR_SHARE


def post(actual, share, prior=1.0, target=100.0):
    """D-37: this week's evidence pulled toward the prior intensity."""
    return (actual + K * target * prior) / (target * (share + K))


WORKWEEK = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [9, 17], "off_day_factor": 0.0, "mode": "replace"}


class Weekly(TempHome):
    def view(self, now):
        return weekly.weekly(self.st, now, self.cfg)

    def test_uniform_midweek_linear_history(self):
        # Window Mon 00:00 -> next Mon 00:00, now Thu 12:00 (84 h in, E = 0.5).
        R = int(MON + 7 * D)
        now = MON + 84 * H
        for h in range(0, 85):
            self.feed(MON + h * H, week=(40.0 * h / 84.0, R))
        v = self.view(now)
        self.assertAlmostEqual(v["E"], 0.5, places=9)
        self.assertAlmostEqual(v["on_pace"], 50.0, places=6)
        self.assertAlmostEqual(v["delta"], -10.0, places=6)
        # Last 24 h: spent 40*24/84 = 11.43, profile expected 100*24/168 = 14.29,
        # pulled toward the target (no typical week yet): 0.88.
        i = post(40 * 24 / 84, 24 / 168)
        self.assertEqual(v["intensity_source"], "recent")
        self.assertAlmostEqual(v["intensity"], i, places=6)
        self.assertAlmostEqual(v["intensity_weight"], (1 / 7) / (1 / 7 + K), places=6)
        self.assertAlmostEqual(v["projected"], 40 + i * 50, places=6)
        self.assertAlmostEqual(v["multiplier"], 60 / (i * 50), places=6)
        self.assertAlmostEqual(v["expire"], 60 - i * 50, places=6)
        self.assertEqual(v["verdict"], "speed_up")
        self.assertIsNone(v["dry_ts"])

    def test_profile_with_nights_and_weekends(self):
        self.cfg["profile"]["override"] = dict(WORKWEEK)
        R = int(MON + 7 * D)
        now = MON + 2 * D + 13 * H  # Wednesday 13:00
        self.feed(now, week=(30, R))
        v = self.view(now)
        # 40 awake bins at 1.0; typical = mean of the top 56 bins = 40/56;
        # floor = 0.15 * 40/56 = 6/56 for the other 128 bins.
        f = 0.15 * 40 / 56
        total = 40 + 128 * f
        # Ahead: Wed 13-17 (4) + Thu (8) + Fri (8) awake hours, and 107 - 20 = 87 off hours.
        E = (20 + 87 * f) / total
        self.assertAlmostEqual(v["E"], E, places=9)
        self.assertAlmostEqual(v["on_pace"], 100 * (1 - E), places=6)
        self.assertEqual(v["awake_hours"], 40)

    def test_active_lookback_skips_nights_and_weekend(self):
        w, awake, _ = profile.Profile().weights(MON, {"profile": dict(self.cfg["profile"], override=WORKWEEK)})
        now = MON + 2 * D + 13 * H  # Wed 13:00
        # 24 awake hours back: Wed 9-13 (4), Tue (8), Mon (8), then last Fri 13-17 (4).
        t0 = profile.active_lookback(awake, now, 24, 7 * D)
        self.assertEqual(t0, MON - 3 * D + 13 * H)

    def test_dry_walk_uniform(self):
        now = MON + 4 * D
        R = int(now + 3 * D)
        for h in range(30, -1, -1):  # 1 point per hour, 90% now
            self.feed(now - h * H, week=(90 - h, R))
        v = self.view(now)
        # 24 points in 24 h vs 100*24/168 expected (1.68), pulled toward 1: 1.4,
        # 0.83 %/h.
        self.assertAlmostEqual(v["intensity"], 1.4, places=6)
        per_h = 1.4 * 100 / 168
        self.assertAlmostEqual(v["dry_ts"], now + 10 / per_h * H, places=3)
        self.assertAlmostEqual(v["projected_raw"], 90 + 72 * per_h, places=6)
        # Even 32% slower (the spread) it runs out long before the reset: red.
        self.assertAlmostEqual(v["spread"], 0.5 * math.sqrt(K / (1 / 7 + K)), places=6)
        self.assertTrue(v["sure_out"])
        self.assertEqual(v["severity"], "alert")
        self.assertAlmostEqual(v["multiplier"], 10 / (72 * per_h), places=6)

    def test_a_mild_overshoot_is_yellow_not_red(self):
        # Review 4: at target 100 every slow down also runs dry, so yellow never
        # showed. Intensity 1.4 (0.83 %/h) at 42% with 72 h left: 60 points to
        # come, multiplier 58/60 = 0.97 (on pace), dry 2.4 h early: yellow.
        now = MON + 4 * D
        R = int(now + 3 * D)
        for h in range(30, -1, -1):
            self.feed(now - h * H, week=(42 - h, R))
        v = self.view(now)
        self.assertAlmostEqual(v["intensity"], 1.4, places=6)
        self.assertAlmostEqual(v["multiplier"], 58 / 60, places=6)
        self.assertEqual(v["verdict"], "on_pace")
        self.assertAlmostEqual(v["dry_ts"], now + 58 / (1.4 * 100 / 168) * H, places=3)
        self.assertEqual(v["severity"], "warn")

    def test_a_run_out_that_the_low_edge_escapes_is_yellow(self):
        # D-37: slow down and dry, but at the low edge of the estimate it lasts
        # to the reset: "may run out", yellow, no notification.
        base = dict(dry_ts=MON, in_tail=False, expire=0, verdict="slow_down")
        self.assertEqual(weekly._severity(dict(base, sure_out=False), self.cfg), "warn")
        self.assertEqual(weekly._severity(dict(base, sure_out=True), self.cfg), "alert")

    def test_severity_tiers(self):
        # Review 4: the same tiers as the 5h window, with dry_ts.
        base = dict(dry_ts=None, in_tail=False, expire=0)
        cases = [
            (dict(verdict="maxed"), "alert"),
            (dict(verdict="slow_down", dry_ts=MON), "alert"),
            (dict(verdict="on_pace", dry_ts=MON), "warn"),
            (dict(verdict="slow_down"), "warn"),
            (dict(verdict="on_pace"), "dim"),
            (dict(verdict="speed_up", in_tail=True, expire=20), "opportunity"),
            # Review 5 (B-weekly-profile#2): at or above a target below 100 is
            # yellow whatever the verdict, idle and unknown included.
            (dict(verdict="idle", in_tail=True, expire=15, target=80, used=85), "warn"),
            (dict(verdict="unknown", target=90, used=90), "warn"),
            (dict(verdict="speed_up", in_tail=True, expire=30, target=80, used=50), "opportunity"),
            # Review 7 (checks-colors#1, integration#0): green counts only what
            # expires below the target: 25 points expire, 5 of them below 80.
            (dict(verdict="speed_up", in_tail=True, expire=25, target=80, used=50), "dim"),
        ]
        for kw, tone in cases:
            self.assertEqual(weekly._severity(dict(base, **kw), self.cfg), tone, kw)

    def test_the_last_days_of_the_previous_week_do_not_count(self):
        # The person's Windows band: the week reset at 03:00 after a last day
        # that spent what would have gone unused (40 points in 24 h); 5 hours
        # into the new week at 2%, "Ease off, runs out Thu". The lookback now
        # stops at the reset: this week's 2% over 5 h, pulled toward the
        # target, is on pace.
        now = MON + 3 * D
        r_prev = int(now - 5 * H)
        r_cur = int(r_prev + 7 * D)
        self.known("s1")
        for h in range(30, 5, -1):
            self.feed(now - h * H, week=(60 + 40 * (30 - h) / 25, r_prev))
        self.feed(now - 3 * H, week=(1, r_cur))
        self.feed(now, week=(2, r_cur))
        v = self.view(now)
        self.assertEqual(v["lookback_start"], r_prev)
        self.assertAlmostEqual(v["lookback_actual"], 2.0)
        # 5 hours weigh less than the typical week (the last one, at 100%).
        self.assertEqual(v["intensity_source"], "typical")
        self.assertAlmostEqual(v["intensity"], post(2, 5 / 168, v["intensity_prior"]), places=6)
        self.assertEqual(v["verdict"], "on_pace")
        self.assertIsNone(v["dry_ts"])

    def test_an_early_run_out_its_low_edge_escapes_is_no_warning(self):
        # D-37: 2% in the week's first hour, no typical week. At 1.13 the walk
        # runs out on Sunday, but the estimate is early (the target weighs
        # 94%) and 49% slower it lands under 100: on pace, no color, no
        # "Runs out"; Details notes when, as a maybe.
        from glidepath import band

        R = int(MON + 10 * H + 7 * D)
        self.known("s1")
        self.feed(MON + 10 * H + 10 * 60, week=(1, R))
        self.feed(MON + 10 * H + 40 * 60, week=(2, R))
        now = MON + 11 * H
        v = self.view(now)
        self.assertEqual(v["intensity_source"], "assumed")
        self.assertIsNotNone(v["dry_ts"])
        self.assertFalse(v["sure_out"])
        self.assertTrue(v["shaky"])
        self.assertEqual(v["verdict"], "on_pace")
        self.assertEqual(v["severity"], "dim")
        w = band.window(v, now)
        self.assertFalse(w["phrase"]["attention"])
        rows = {r["label"]: r for r in w["details"]}
        self.assertNotIn("Runs out", rows)
        self.assertTrue(rows["Resets"]["note"].startswith("maybe 100% "), rows["Resets"])
        self.assertEqual(rows["Resets"]["note_tone"], "inactive")

    def test_a_drop_reset_is_paced_from_the_drop(self):
        # Review 8 (mod-and-texts#0): the weekly limit is reset early (42% to
        # 0, resets_at unchanged, D-10) on Monday 12:00, and from then on the
        # user spends exactly what lands on 100% at the Thursday 09:00 reset.
        # On-pace and the 'window' fallback still measured from resets_at - 7 d:
        # "x9+ ->2%" at first, then 38 points "behind" for days.
        R0 = int(MON - 4 * D + 9 * H)  # Thursday 09:00, two weeks before
        R = R0 + int(14 * D)
        self.known("s1")
        self.work(R0, MON + 7 * D + 12 * H, R0 + int(7 * D), 2.0)  # 90% a week, Mon-Fri 9-18
        v = self.view(MON + 7 * D + 12 * H)
        self.assertEqual((v["used"], v["start_ts"]), (42, R - 7 * D))
        drop = MON + 7 * D + 12 * H
        rate = 100 / ((R - drop) / H)  # around the clock: on pace from the drop
        for k in range(1, 98):  # 0 at 12:15, then 1 at 12:45 confirms the reset
            self.feed(drop + k * 900, week=(math.floor(rate * k / 4), R))
            if k == 3:
                v = self.view(drop + H)
                self.assertEqual(v["start_ts"], drop + 900)  # the new window opened at 12:15
                # 45 minutes say little: the typical week (90%) leads, not
                # 1 point over 4 days.
                self.assertEqual(v["intensity_source"], "typical")
                self.assertAlmostEqual(v["intensity_prior"], 0.9, places=6)
                self.assertLess(abs(v["intensity"] - 0.9), 0.15)
        now = drop + 24 * H + 900  # Tuesday 12:15, a day into the new window
        v = self.view(now)
        span = (R - drop - 900) / H
        self.assertAlmostEqual(v["on_pace"], 100 * 24 / span, places=6)
        self.assertLess(abs(v["delta"]), 1.0)
        # A day of spending at 2.45 times a week's pace, pulled toward the
        # typical week (0.9): 1.81, so the verdict leans to "faster" until
        # the new pace has more weight; never "slower".
        self.assertAlmostEqual(v["intensity"], post(35, 24 / 168, 0.9), places=6)
        self.assertNotEqual(v["verdict"], "slow_down")

    def test_bogus_shifted_window_is_not_taken_as_the_previous_week(self):
        R1 = int(MON + 7 * D)
        for h in range(0, 167, 2):  # week 1 at 0.5 %/h, one bogus later reset in the middle
            self.feed(MON + h * H, week=(0.5 * h, R1), sid="a")
            if h == 100:
                self.feed(MON + h * H + 60, week=(50, R1 + 1200), sid="b")
        R2 = R1 + int(7 * D)
        now = R1 + 6 * H
        for h in range(0, 7):
            self.feed(R1 + h * H, week=(0.5 * h, R2), sid="a")
        v = self.view(now)
        # The lookback stops at this week's start: 6 h at 0.5 = 3 points.
        self.assertEqual(v["lookback_start"], R1)
        self.assertAlmostEqual(v["lookback_actual"], 3.0, delta=0.51)
        self.assertGreater(v["intensity"], 0.7)

    def test_parallel_sessions_running_max(self):
        R = int(MON + 7 * D)
        now = MON + 3 * D
        self.known("busy")  # its 38 at the lookback start is timed, not a first reading
        for i in range(24, -1, -1):
            self.feed(now - i * H, week=(50 - i * 0.5, R), sid="busy")
            self.feed(now - i * H + 30, week=(20, R), sid="idle")
        v = self.view(now)
        self.assertEqual(v["used"], 50)
        self.assertAlmostEqual(v["lookback_actual"], 12.0)
        self.assertEqual(len(self.windows(SEVEN_DAY)), 1)

    def test_lookback_reaching_window_start_uses_zero_there(self):
        R = int(MON + 7 * D)
        now = MON + D
        self.feed(now, week=(10, R))  # first sample, but the window began 24 h ago at 0%
        v = self.view(now)
        self.assertEqual(v["intensity_source"], "recent")
        self.assertAlmostEqual(v["intensity"], post(10, 24 / 168), places=6)

    def test_window_fallback_when_lookback_start_unknown(self):
        R = int(MON + 7 * D)
        now = MON + 3 * D
        self.feed(now, week=(10, R))  # first sample ever, 3 days into the window
        v = self.view(now)
        # The window started at 0: 10 points over its 3 days are the evidence.
        self.assertEqual(v["intensity_source"], "window")
        self.assertAlmostEqual(v["intensity"], post(10, 3 / 7), places=6)

    def test_first_readings_never_anchor_the_lookback(self):
        # Review 4 (weekly-profile#0): at install every open session re-renders
        # its last reading as FIRST. An idle session's old 45 must not become
        # the level at the lookback start, whichever session renders first.
        R = int(MON + 3 * D + 2 * H)  # resets Thursday 02:00
        T = MON + D + 10 * H  # installed Tuesday 10:00
        levels = {"idle": 45, "busy": 51}
        for order in (("idle", "busy"), ("busy", "idle")):
            self.st.close()
            self.st = Store(os.path.join(self.dir, "%s-first.db" % order[0]))
            self.feed(T, week=(levels[order[0]], R), sid=order[0])
            self.feed(T + 0.5, week=(levels[order[1]], R), sid=order[1])
            self.feed(T + 2 * H, week=(52, R), sid="busy")  # the first FRESH reading
            v = self.view(T + 2 * H + 60)
            # The count starts at the first FRESH reading: nothing spent since
            # (no step yet: no news), so the whole window speaks.
            self.assertEqual(v["lookback_start"], T + 2 * H)
            self.assertEqual(v["intensity_source"], "window")
            self.assertIsNone(v["dry_ts"])
            self.feed(T + 4 * H, week=(53, R), sid="busy")
            v = self.view(T + 4 * H)
            # 53 - 52 in 2 h vs 100*2/168 expected (old code: 53 - 45 in 4 h, 3.36, dry).
            self.assertAlmostEqual(v["lookback_actual"], 1.0)
            self.assertAlmostEqual(v["intensity"], post(1, 2 / 168), places=6)
            self.assertIsNone(v["dry_ts"])

    def test_spend_in_a_long_gap_is_spread_not_charged_to_the_lookback(self):
        # Review 4 (weekly-profile#1): no readings from Friday 16:00 (40%) to
        # Monday 08:00 (60%), the 20 points spent on claude.ai meanwhile.
        R = int(MON + 3 * D + 9 * H)  # resets Thursday 09:00
        self.known("s1")
        for h in range(8, -1, -1):
            self.feed(MON - 2 * D - 8 * H - h * H, week=(40 - h, R))  # Friday 08:00 .. 16:00
        now = MON + 8 * H
        self.feed(now, week=(60, R))
        v = self.view(now)
        # Uniform profile: the lookback starts Sunday 08:00, 40 h into the 64 h
        # gap, so 40/64 of the rise came before it: 40 + 20 * 40/64 = 52.5.
        self.assertAlmostEqual(v["lookback_actual"], 60 - 52.5, places=6)
        self.assertAlmostEqual(v["intensity"], post(7.5, 24 / 168), places=6)  # 0.72, not 1.4
        self.assertIsNone(v["dry_ts"])
        self.assertEqual(v["verdict"], "speed_up")

    def test_cut_lookback_without_a_step_is_not_idle(self):
        # Review 4 (weekly-profile#2): first seen 3 days into the window, the
        # level repeats for 2 h (whole percents, no step yet): no news, not idle.
        R = int(MON + 7 * D)
        T = MON + 3 * D
        self.known("s1")
        for k in range(25):
            self.feed(T + k * 300, week=(30, R))
        v = self.view(T + 2 * H)
        self.assertEqual(v["intensity_source"], "window")
        self.assertAlmostEqual(v["intensity"], post(30, 74 / 168), places=6)
        self.assertNotEqual(v["verdict"], "idle")
        # The first step brings the lookback back: 1 point in 3 h, which says
        # less than the prior.
        self.feed(T + 3 * H, week=(31, R))
        v = self.view(T + 3 * H)
        self.assertEqual(v["intensity_source"], "assumed")
        self.assertAlmostEqual(v["intensity"], post(1, 3 / 168), places=6)

    def test_cut_lookback_with_the_window_still_at_0_falls_back_to_the_typical_week(self):
        # Review 5 (B-weekly-profile#1): the week reset Saturday 02:00, nothing
        # was used over the weekend, and the first reading of the new window,
        # Monday 09:00, is 0. The cut lookback's zero is no news (D-15), and
        # neither is the whole window's: it gave intensity 0, "×? →0%".
        R0 = int(MON - 2 * D + 2 * H)  # Saturday 02:00
        R1 = R0 + int(7 * D)
        now = MON + 11 * H  # Monday 11:00, two hours of heartbeats at 0
        # 57 of the week's 168 hours are behind (uniform profile): E = 111/168.
        cases = [(True, "typical", 0.7), (False, "assumed", 1.0)]  # with or without a closed week at 70%
        for i, (closed, source, intensity) in enumerate(cases):
            self.st.close()
            self.st = Store(os.path.join(self.dir, "zero-%d.db" % i))
            self.known("s1")
            if closed:
                self.feed(R0 - 12 * H, week=(69, R0))
                self.feed(R0 - 2 * H, week=(70, R0))  # observed in its last day
            for k in range(25):
                self.feed(MON + 9 * H + k * 300, week=(0, R1))
            v = self.view(now)
            self.assertTrue(v["lookback_start"] > now - 24 * H)  # cut at Monday 09:00
            self.assertEqual(v["lookback_actual"], 0.0)
            self.assertEqual(v["intensity_source"], source)
            self.assertAlmostEqual(v["intensity"], intensity, places=9)
            self.assertAlmostEqual(v["projected"], intensity * 100 * 111 / 168, places=6)  # 46.25 / 66.07
            self.assertAlmostEqual(v["multiplier"], 168 / (intensity * 111), places=6)  # 2.16 / 1.51
            self.assertEqual(v["verdict"], "speed_up")
            text = " ".join(t for t, _ in render.weekly_parts(v, render.SYMBOLS["unicode"]))
            self.assertNotIn("×?", text)
            self.assertNotIn("→0%", text)

    def test_idle_above_a_target_below_100_is_yellow(self):
        # Review 5 (B-weekly-profile#2): with target_week 80, a week at 85%
        # with 40 h to its reset and nothing spent in the last 24 awake hours
        # was green (15 points "about to expire"); a point more spent made it
        # yellow. At or above a target below 100 is yellow, idle included.
        self.cfg["target_week"] = 80
        now = MON + 4 * D + 8 * H  # Friday 08:00
        R = int(now + 40 * H)
        self.known("s1")
        for h in range(40, 29, -1):  # 75 -> 85% until 30 h ago
            self.feed(now - h * H, week=(115 - h, R))
        for h in range(29, -1, -1):  # then idle
            self.feed(now - h * H, week=(85, R))
        v = self.view(now)
        self.assertEqual(v["intensity_source"], "recent")
        self.assertEqual(v["verdict"], "idle")
        self.assertTrue(v["in_tail"])
        # Idle lately, but the prior still expects some spend before the reset.
        i = post(0, 24 / 168, target=80)
        self.assertAlmostEqual(v["expire"], 15.0 - i * 80 * 40 / 168, places=6)
        self.assertEqual(v["severity"], "warn")

    def test_multiplier_is_zero_past_a_target_below_100(self):
        # Review 4 (weekly-profile#3): target_week 90 and 93 used gave ×-0.0▼.
        self.cfg["target_week"] = 90
        now = MON + 4 * D
        R = int(now + 3 * D)
        self.known("s1")
        for h in range(30, -1, -1):  # 1 point per hour, 93% now
            self.feed(now - h * H, week=(93 - h, R))
        v = self.view(now)
        # 24 points in 24 h vs 90*24/168 expected, pulled toward 1.
        i = post(24, 24 / 168, target=90)
        self.assertAlmostEqual(v["intensity"], i, places=6)
        self.assertAlmostEqual(v["projected_raw"], 93 + i * 90 * 72 / 168, places=6)
        self.assertEqual(v["multiplier"], 0.0)  # max(0, 90 - 93) / remaining
        self.assertEqual(v["verdict"], "slow_down")
        text = " ".join(t for t, _ in render.weekly_parts(v, render.SYMBOLS["unicode"]))
        self.assertIn("×0.0▼", text)
        self.assertNotIn("×-", text)

    def test_missing_weekly_window(self):
        self.feed(MON + H, five=(10, int(MON + 3 * H)))
        self.assertFalse(self.view(MON + H)["present"])

    def test_missing_weekly_window_keeps_an_unstored_reading(self):
        # Review 7 (fresh-eyes#2): when the write failed at a rollover, only
        # the expired instance was there and the reading was dropped ("wk 0%"
        # while the payload said 1%). It is kept for display, with no math.
        R7 = int(MON + 7 * D)
        self.feed(MON - 6 * D, week=(90, int(MON)))
        v = weekly.weekly(self.st, MON + 120, self.cfg, (1.0, R7))
        self.assertFalse(v["present"])
        self.assertEqual(v["raw"], (1.0, R7))
        self.assertEqual(v["severity"], "dim")
        self.assertNotIn("raw", self.view(MON + 120))

    def test_exchange_rate(self):
        R5 = int(MON + 5 * H)
        R7 = int(MON + 7 * D)
        for i in range(61):
            self.feed(MON + i * 240, five=(i * 1.0, R5), week=(i * 0.1, R7))
        ppw, rf, rs = weekly.exchange_rate(self.st, MON + D)
        self.assertAlmostEqual(rf, 60)
        self.assertAlmostEqual(rs, 6)
        self.assertAlmostEqual(ppw, 10.0)  # a full 5h window = 10% of the week


if __name__ == "__main__":
    unittest.main()
