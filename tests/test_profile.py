import copy
import os
import time
import unittest

from glidepath import clock, config, profile
from glidepath.config import SEVEN_DAY
from glidepath.store import FIRST

from .helpers import MON, H, D, TempHome, utc, zone


class Shape(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = copy.deepcopy(config.DEFAULTS)

    def test_empty_profile_is_uniform(self):
        w, awake, info = profile.Profile().weights(MON, self.cfg)
        self.assertAlmostEqual(sum(w), 1.0)
        self.assertTrue(all(abs(x - 1 / 168) < 1e-12 for x in w))
        self.assertEqual(info["source"], "uniform")
        self.assertEqual(sum(awake), 168)

    def test_ewma_half_life_by_hand(self):
        p = profile.Profile()
        hl = 14 * D
        p.observe(MON + 9 * H, 1.0, 1.0, hl)
        p.observe(MON + 14 * D + 9 * H, 0.0, 1.0, hl)  # same bin, 14 days later
        b = profile.bin_of(MON + 9 * H)
        # Weights 0.5 (two weeks old) and 1: mean = (0.5*1 + 1*0) / 1.5 = 1/3.
        self.assertAlmostEqual(p.S[b] / p.W[b], 1 / 3)
        m, n = p.observed(MON + 14 * D + 9 * H, hl)[b]
        self.assertAlmostEqual(n, 1.5)

    def test_shrinkage_by_hand(self):
        p = profile.Profile()
        hl = 14 * D
        b1, b2 = 10, 20
        p.S[b1], p.W[b1], p.T[b1] = 2.0, 1.0, MON
        p.S[b2], p.W[b2], p.T[b2] = 0.0, 1.0, MON
        r, M, src = p.rates(MON, self.cfg)  # uniform prior: k = 0.25
        # M = (2 + 0) / (1/168 + 1/168) = 168 points per week, so M p = 1.
        self.assertAlmostEqual(M, 168.0)
        self.assertAlmostEqual(r[b1], (2 + 0.25) / 1.25)  # (n m + k M p) / (n + k) = 1.8
        self.assertAlmostEqual(r[b2], (0 + 0.25) / 1.25)  # 0.2
        self.assertAlmostEqual(r[0], 1.0)  # unobserved: M p
        w, _, _ = p.weights(MON, self.cfg)
        self.assertAlmostEqual(w[b1], 1.8 / 168)
        # An informative prior (transcripts) uses k = 1: (2 + 1) / 2 = 1.5.
        p.prior = profile.uniform()
        r, M, src = p.rates(MON, self.cfg)
        self.assertEqual(src, "transcripts+observed")
        self.assertAlmostEqual(r[b1], 1.5)
        # A known weekly total replaces the ratio estimate: M p = 84/168.
        r, M, _ = p.rates(MON, self.cfg, weekly_total=84.0)
        self.assertAlmostEqual(r[b1], (2 + 0.5) / 2)
        self.assertAlmostEqual(r[0], 0.5)

    def test_floor_keeps_off_hours_alive(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["profile"]["override"] = {"work_days": [0, 1, 2, 3, 4], "wake_hours": [9, 17], "off_day_factor": 0.0, "mode": "replace"}
        w, awake, _ = profile.Profile().weights(MON, cfg)
        on = w[9]  # Mon 09:00
        off = w[3]  # Mon 03:00
        self.assertAlmostEqual(off / on, 0.15 * 40 / 56)
        self.assertEqual(sum(awake), 40)

    def test_walk_to_and_share(self):
        w = profile.uniform()
        self.assertAlmostEqual(profile.share_between(w, MON, MON + 84 * H), 0.5)
        self.assertAlmostEqual(profile.walk_to(w, MON, MON + 7 * D, 168.0, 10.0), MON + 10 * H)
        self.assertIsNone(profile.walk_to(w, MON, MON + 5 * H, 168.0, 10.0))


class DST(unittest.TestCase):
    def test_segments_across_fall_back(self):
        with zone("America/New_York"):
            # Sunday 2026-11-01: clocks go back at 02:00, the day has 25 hours.
            start = 1793505600  # 2026-11-01 00:00 EDT (04:00 UTC)
            segs = list(profile.segments(start, start + 25 * H))
            self.assertEqual(len(segs), 25)
            self.assertTrue(all(abs((e - s) - H) < 1e-6 for s, e, _, _ in segs))
            self.assertEqual(clock.localtime(start + 25 * H).tm_hour, 0)
            self.assertEqual(segs[1][3], segs[2][3])  # both 01:00 hours share Sunday 01:00

    def test_segments_across_spring_forward(self):
        with zone("America/New_York"):
            # Sunday 2027-03-14: clocks go forward at 02:00, the day has 23 hours.
            start = 1805000400  # 2027-03-14 00:00 EST (05:00 UTC)
            segs = list(profile.segments(start, start + 23 * H))
            self.assertEqual(len(segs), 23)
            self.assertTrue(all(abs((e - s) - H) < 1e-6 for s, e, _, _ in segs))
            self.assertEqual([b for _, _, _, b in segs[:3]], [6 * 24, 6 * 24 + 1, 6 * 24 + 3])  # no Sunday 02:00
            lt = clock.localtime(start + 23 * H)
            self.assertEqual((lt.tm_wday, lt.tm_hour), (0, 0))  # Monday 00:00


class HalfHourZone(unittest.TestCase):
    def test_local_hours_start_on_the_half_hour(self):
        with zone("Asia/Kolkata"):  # UTC+05:30, no DST
            # MON is Monday 05:30 in India: that local hour began at 05:00.
            self.assertEqual(profile.hour_start(MON), MON - 1800)
            segs = list(profile.segments(MON, MON + 2 * H))
            self.assertEqual(segs, [(MON, MON + 1800, MON - 1800, 5), (MON + 1800, MON + 5400, MON + 1800, 6),
                                    (MON + 5400, MON + 2 * H, MON + 5400, 7)])


class Learn(TempHome):
    def test_learns_work_hours_from_weekly_percent(self):
        # Three weeks: +1 point per hour Mon-Fri 9-17, nothing otherwise,
        # fresh readings every 15 minutes while it moves.
        used, t = 0.0, MON
        R = int(MON + 3 * D + 9 * H)  # weekly reset Thursday 09:00
        end = MON + 21 * D
        while t < end:
            if t >= R:
                R += int(7 * D)
                used = 0.0
            lt = time.gmtime(t)
            if lt.tm_wday < 5 and 9 <= lt.tm_hour < 17:
                used += 0.25
                self.feed(t + 15 * 60, week=(round(used, 2), R))
            t += 15 * 60
        prof = profile.load(self.st)
        n = profile.learn(self.st, prof, end, self.cfg, profile.uniform())
        self.assertGreater(n, 300)
        w, awake, info = prof.weights(end, self.cfg)
        work = w[2 * 24 + 11]  # Wed 11:00
        night = w[2 * 24 + 3]  # Wed 03:00
        weekend = w[5 * 24 + 11]  # Sat 11:00
        self.assertGreater(work / night, 5)
        self.assertGreater(work / weekend, 5)
        self.assertAlmostEqual(sum(w), 1.0)
        # Ratio estimate of the weekly total: 40 points a week.
        self.assertAlmostEqual(info["weekly_total"], 40.0, delta=8.0)
        # With closed windows the scale comes from their peaks instead.
        # Closed windows reset Thu 09:00 with peaks 24 (data began mid-window),
        # 40 and 40, weighted 0.5 ** (age / 14 days).
        typical = profile.typical_week(self.st, end, 14 * D)
        resets = [MON + 3 * D + 9 * H + k * 7 * D for k in range(3)]
        a = [0.5 ** ((end - r) / (14 * D)) for r in resets]
        self.assertAlmostEqual(typical, (a[0] * 24 + a[1] * 40 + a[2] * 40) / sum(a), places=6)
        # Learning again finds nothing new.
        self.assertEqual(profile.learn(self.st, prof, end, self.cfg, w), 0)


class LearnEdges(TempHome):
    def learned(self, now):
        prof = profile.load(self.st)
        profile.learn(self.st, prof, now, self.cfg, profile.uniform())
        return prof

    def test_moved_reset_is_a_continuation_not_new_spend(self):
        R = int(MON + 7 * D)
        t = MON + 2 * D + 9 * H  # Wednesday 09:00, 1 point an hour from 50%
        for k in range(13):
            self.feed(t + k * 900, week=(50 + k * 0.25, R))
        R2 = R + 1800  # resets_at moves 30 minutes later (more than the jitter)
        for k in range(13, 25):
            self.feed(t + k * 900, week=(50 + k * 0.25, R2))
        prof = self.learned(t + 8 * H)
        b = profile.bin_of(t + 3 * H)  # the hour of the move
        self.assertAlmostEqual(prof.S[b] / prof.W[b], 1.0, places=6)

    def test_learning_keeps_a_prior_committed_after_the_profile_was_read(self):
        from glidepath import statusline
        from glidepath.store import Store

        self.feed(MON + H, week=(5, int(MON + 7 * D)))
        stale = profile.load(self.st)  # what a render read before learning
        other = Store(os.path.join(self.dir, "tempo.db"))  # bootstrap, in another process
        fresh = profile.load(other)
        fresh.prior = profile.uniform()
        fresh.prior_meta = {"trust": 1.0, "messages": 3}
        other.begin()
        profile.save(other, fresh)
        other.commit()
        other.close()
        kept = statusline.maybe_learn(self.st, stale, MON + 3 * H, self.cfg)
        self.assertIsNotNone(kept.prior)
        self.assertIsNotNone(profile.load(self.st).prior)

    def test_early_reset_with_rounded_start_counts_from_zero(self):
        R = int(MON + 4 * D + 2 * H)  # resets Friday
        t = MON + 2 * D + 12 * H  # Wednesday 12:00, at 64% and climbing 1 point an hour
        for k in range(11):
            self.feed(t + k * 900, week=(64 + k * 0.25, R))
        # 14:37 an early reset; the new window's resets_at is on the hour: 14:00 next week.
        R2 = int(MON + 9 * D + 14 * H)
        for k in range(1, 17):
            self.feed(t + 2 * H + 37 * 60 + k * 900, week=(k * 0.25, R2))
        prof = self.learned(t + 8 * H)
        b = profile.bin_of(t + 4 * H)  # Wednesday 16:00, well inside the new window
        self.assertAlmostEqual(prof.S[b] / prof.W[b], 1.0, places=6)

    def test_light_users_early_reset_counts_from_zero_in_batch_and_incremental(self):
        # Review 4 (weekly-profile#4): the same early reset as above, but the
        # old window peaks at 12.5 (less than drop_points). The new window's
        # 0.25 is not "about the same level", so it counts from 0 when learned
        # in one batch (profile --rebuild) too, not as 0 until it passes 12.5.
        from glidepath import ingest
        from glidepath.store import Store

        R = int(MON + 4 * D + 2 * H)  # resets Friday
        t = MON + 2 * D + 12 * H  # Wednesday 12:00, at 10% and climbing 1 point an hour
        R2 = int(MON + 9 * D + 14 * H)  # early reset 14:37, resets_at on the hour
        rows = [(t + k * 900, 10 + k * 0.25, R) for k in range(11)]
        rows += [(t + 2 * H + 37 * 60 + k * 900, k * 0.25, R2) for k in range(1, 17)]
        for ts, used, r in rows:
            self.feed(ts, week=(used, r))
        batch = self.learned(t + 8 * H)
        for b in (profile.bin_of(t + 3 * H), profile.bin_of(t + 4 * H), profile.bin_of(t + 5 * H)):
            self.assertAlmostEqual(batch.S[b] / batch.W[b], 1.0, places=6)  # Wednesday 15:00, 16:00, 17:00
        st = Store(os.path.join(self.dir, "incremental.db"))
        inc = profile.Profile()
        for ts, used, r in rows:  # learn after every reading, as the statusline would
            got, rej, k = ingest.parse_payload({"rate_limits": {"seven_day": {"used_percentage": used, "resets_at": r}}}, ts)
            ingest.record(st, got, ts, "test", "s1", self.cfg, rej, k)
            profile.learn(st, inc, ts, self.cfg, profile.uniform())
        st.close()
        self.assertEqual(batch.processed_until, inc.processed_until)
        self.assertAlmostEqual(sum(batch.S), sum(inc.S), places=6)
        self.assertAlmostEqual(sum(batch.W), sum(inc.W), places=6)

    def test_the_old_windows_tail_before_a_reset_is_learned_idle(self):
        # Review 8 (long-horizon#1): the weekly window resets Monday 09:00 and
        # the user works Mon-Fri 9-18 at 1.25 points an hour (56.25 a week),
        # with no transcripts (uniform prior). From Friday 18:00 to the reset
        # no reading comes: that tail of the old window was never learned, so
        # the weekend kept the average rate for good. It counted 63 awake
        # hours more, the typical week read 90% and Wednesday projected 83%.
        from glidepath import weekly

        R0 = int(MON + 9 * H)
        now = MON + 44 * D + 12 * H  # Wednesday 12:00 of week 7
        self.known("s1")
        self.work(R0, now, R0 + int(7 * D), 1.25, learn=True)
        w, awake, info = profile.current_weights(self.st, profile.load(self.st), now, self.cfg)
        self.assertFalse(any(awake[5 * 24:]))  # Saturday and Sunday
        self.assertLessEqual(info["awake_hours"], 47)  # 45 true
        self.assertLess(sum(w[5 * 24:]), 0.12)  # at the floor: 0.099
        self.assertAlmostEqual(info["weekly_total"], 56.25, delta=3)
        # The floor's night hours expect spend that never comes, so the week's
        # own evidence runs a little high (D-37): 63, not 83.
        self.assertAlmostEqual(weekly.weekly(self.st, now, self.cfg)["projected"], 56.25, delta=7.5)

    def test_a_capped_windows_tail_is_not_learned_idle(self):
        # Review 8 (long-horizon#1), the other side: a heavy user (Mon-Fri
        # 9-17, 3.5 points an hour) caps every Tuesday 13:45 and is blocked
        # until the Thursday 09:00 reset. Blocked hours say nothing of the
        # habit: learned as idle by the rule above, they left the profile
        # without Wednesday, and Monday 11:00 at 63% read "on pace".
        from glidepath import weekly

        R0 = int(MON + 3 * D + 9 * H)
        now = R0 + 25 * D + 2 * H  # Monday 11:00, after three capped weeks
        self.known("s1")
        self.work(R0, now, R0 + int(7 * D), 3.5, hours=(9, 17), learn=True)
        w, awake, info = profile.current_weights(self.st, profile.load(self.st), now, self.cfg)
        self.assertTrue(all(awake[2 * 24 + 9:2 * 24 + 17]))  # Wednesday 9-17
        v = weekly.weekly(self.st, now, self.cfg)
        self.assertEqual(v["used"], 63)
        self.assertEqual(v["verdict"], "slow_down")  # it caps again on Tuesday
        self.assertIsNotNone(v["dry_ts"])

    def test_a_cap_first_read_by_a_new_session_is_a_cap(self):
        # Review 9 (delta-weekly#0): the same capper, but the last points go
        # on claude.ai: Claude Code reads 99, and the 100 first shows as a new
        # session's FIRST reading. Learning judged the cap by FRESH readings
        # only, so it learned the blocked hours as idle (the lookback counts
        # that cap, D-15), and Monday 11:00 at 63% read "on pace" again.
        from glidepath import weekly

        feed = self.feed

        def via_claude_ai(now, week):
            if week[0] < 100:
                return feed(now, week=week)
            feed(now, week=(99, week[1]))  # s1 repeats its 99
            return feed(now, week=week, sid="new-%d" % week[1])  # FIRST, then heartbeats

        self.feed = via_claude_ai
        R0 = int(MON + 3 * D + 9 * H)
        now = R0 + 25 * D + 2 * H  # Monday 11:00, after three capped weeks
        self.known("s1")
        self.work(R0, now, R0 + int(7 * D), 3.5, hours=(9, 17), learn=True)
        at_cap = self.st.db.execute("SELECT DISTINCT flag FROM samples WHERE used_pct >= 100").fetchall()
        self.assertEqual(at_cap, [(FIRST,)])  # never a FRESH 100
        w, awake, info = profile.current_weights(self.st, profile.load(self.st), now, self.cfg)
        self.assertTrue(all(awake[2 * 24 + 9:2 * 24 + 17]))  # Wednesday 9-17
        v = weekly.weekly(self.st, now, self.cfg)
        self.assertEqual(v["used"], 63)
        self.assertEqual(v["verdict"], "slow_down")
        self.assertIsNotNone(v["dry_ts"])

    def test_a_drop_reset_week_is_not_a_typical_week(self):
        # Review 9 (delta-weekly#2): Mon-Fri 9-18 at 2 points an hour, 90% a
        # week, and an early reset on Monday 12:00 at 42% that keeps the
        # Thursday 09:00 reset (D-10). The window it opened peaked at 48 in
        # its 2.9 days, and the typical week took that as a whole week: 77%.
        R0 = int(MON - 4 * D + 9 * H)  # Thursday 09:00
        drop = MON + 7 * D + 12 * H
        now = R0 + 21 * D + H  # Thursday 10:00, an hour into the fourth week
        self.known("s1")
        self.work(R0, drop, R0 + int(7 * D), 2.0)
        self.work(drop, R0 + 14 * D, R0 + int(14 * D), 2.0)  # from 0 again
        self.work(R0 + 14 * D, now, R0 + int(21 * D), 2.0)
        closed = [(i["close_reason"], i["max_used"]) for i in self.windows(SEVEN_DAY) if i["closed_at"] is not None]
        self.assertEqual(closed, [("expired", 90), ("drop", 42), ("expired", 48), ("expired", 90)])
        self.assertAlmostEqual(profile.typical_week(self.st, now, 14 * D), 90.0)

    def test_an_early_rollover_week_is_not_a_typical_week(self):
        # Review 10: a real early reset on Wednesday 10:00 that moved resets_at
        # a week on cut the window 23 h short, at 74%. Read in its last day, it
        # counted as a whole week and pulled the typical week below 90.
        R0 = int(MON - 4 * D + 9 * H)  # Thursday 09:00
        early = R0 + 13 * D + H  # Wednesday 10:00 of the second week
        now = R0 + 21 * D + H
        self.known("s1")
        self.work(R0, early, R0 + int(7 * D), 2.0)
        self.work(early, now, early + int(7 * D), 2.0)  # from 0 again, a new reset a week on
        closed = [(i["close_reason"], i["max_used"]) for i in self.windows(SEVEN_DAY) if i["closed_at"] is not None]
        self.assertEqual(closed, [("expired", 90), ("rollover", 74), ("expired", 89)])
        typical = profile.typical_week(self.st, now, 14 * D)  # the two whole weeks only
        self.assertGreaterEqual(typical, 89.0)
        self.assertLessEqual(typical, 90.0)

    def test_a_transcript_prior_fades_with_the_half_life(self):
        # Review 8 (long-horizon#2): the transcripts at install say Mon-Fri
        # 9-18 (trusted fully), then the user works Mon-Fri 14-23 for 8 weeks.
        # The idle mornings are only seen through overnight gaps, learned at
        # low confidence and spread by the prior's own shape, so the prior
        # kept Mon-Fri 9-13 at 20% of the week and 70 awake hours for good.
        from glidepath import transcripts

        T0 = int(MON + 3 * D + 10 * H)  # installed Thursday 10:00, the weekly reset
        day0 = MON - 42 * D  # a Monday more than 45 days before
        events = [(day0 + d * D + h * H + k * 600, 100.0) for d in range(45) if d % 7 < 5
                  for h in range(9, 18) for k in range(6)]
        prof = profile.load(self.st)
        prof.prior, prof.prior_meta = transcripts.build_prior(events, T0 - 45 * D, T0, 14)
        self.assertEqual(prof.prior_meta["trust"], 1.0)
        profile.save(self.st, prof)
        self.known("s1")
        now = T0 + 60 * D + 8 * H  # Monday 18:00, 8 weeks on
        self.work(T0, now, T0 + int(7 * D), 1.25, hours=(14, 23), learn=True)
        w, awake, info = profile.current_weights(self.st, profile.load(self.st), now, self.cfg)
        mornings = sum(w[d * 24 + h] for d in range(5) for h in range(9, 13))
        self.assertLess(mornings, 0.06)  # at the floor: 0.04
        self.assertLessEqual(info["awake_hours"], 47)  # 45 true
        self.assertEqual(info["source"], "transcripts+observed")

    def test_batch_and_incremental_learning_agree_with_parallel_dips(self):
        from glidepath import ingest
        from glidepath.store import Store

        R = int(MON + 7 * D)
        rows = []
        for k in range(32):  # a: +0.5 at :05 :20 :35 :50; b lags 0.5 at :58
            t = MON + 9 * H + 5 * 60 + k * 900
            rows.append((t, 10 + k * 0.5, "a"))
            if k % 4 == 3:
                rows.append((t + 8 * 60, 10 + k * 0.5 - 0.5, "b"))
        end = MON + 18 * H
        for t, used, sid in rows:
            self.feed(t, week=(used, R), sid=sid)
        batch = self.learned(end)
        st = Store(os.path.join(self.dir, "incremental.db"))
        inc = profile.Profile()
        for t, used, sid in rows:  # learn after every reading, as the statusline would
            r, rej, k = ingest.parse_payload({"rate_limits": {"seven_day": {"used_percentage": used, "resets_at": R}}}, t)
            ingest.record(st, r, t, "test", sid, self.cfg, rej, k)
            profile.learn(st, inc, t, self.cfg, profile.uniform())
        st.close()
        self.assertEqual(batch.processed_until, inc.processed_until)
        self.assertAlmostEqual(sum(batch.S), sum(inc.S), places=6)
        self.assertAlmostEqual(sum(batch.W), sum(inc.W), places=6)


if __name__ == "__main__":
    unittest.main()
