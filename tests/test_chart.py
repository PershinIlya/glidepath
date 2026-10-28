"""Charts for Details and the menu bar (glidepath/chart.py, menuart.chart, png lines)."""

import argparse
import base64
import calendar
import contextlib
import io
import json
import re
import struct
import time
import unittest
import zlib
from unittest import mock

from glidepath import band, chart, clock, commands, config, menuart, menubar, profile, statusline
from glidepath.png import Canvas

from .helpers import MON, H, D, TempHome, payload, zone
from .test_band import NOW, STATES, v5 as a_v5

R5 = int(MON + 16 * H + 30 * 60)  # Monday 16:30 UTC: the 5 hours from 11:30
RW = int(MON + 4 * D + 4 * H)  # Friday 04:00 UTC


def pixels(b64):
    """(width, height, [alpha per pixel]) of a PNG from Canvas.png."""
    data = base64.b64decode(b64)
    w, h = struct.unpack(">II", data[16:24])
    i = data.index(b"IDAT")
    n = struct.unpack(">I", data[i - 4:i])[0]
    raw = zlib.decompress(data[i + 4:i + 4 + n])
    stride = 1 + 4 * w
    return w, h, [raw[y * stride + 1 + 4 * x + 3] for y in range(h) for x in range(w)]


class Forecast(unittest.TestCase):
    def test_the_5_hours_go_on_at_their_rate_flat_at_100(self):
        self.assertEqual(chart._line(40.0, 10.0, 0, 2 * H), [[0, 40.0], [2 * H, 60.0]])
        # 40 + 30%/h reaches 100 in 2 hours, then stays there.
        self.assertEqual(chart._line(40.0, 30.0, 0, 3 * H), [[0, 40.0], [2 * H, 100.0], [3 * H, 100.0]])
        self.assertEqual(chart._line(100.0, 30.0, 0, H), [[0, 100.0], [H, 100.0]])
        self.assertEqual(chart._line(40.0, None, 0, H), [])
        self.assertEqual(chart._line(40.0, 10.0, H, H), [])

    def test_the_week_walks_the_profile(self):
        # Work only on Monday 9 to 17 (UTC): the forecast is flat outside it.
        w = [0.0] * profile.BINS
        for h in range(9, 17):
            w[h] = 1.0 / 8
        out = chart._walk(w, 20.0, 40.0, MON + 6 * H, MON + 20 * H)
        at = dict((t, u) for t, u in out)
        self.assertAlmostEqual(at[MON + 9 * H], 20.0)
        self.assertAlmostEqual(at[MON + 13 * H], 40.0)  # half of 40 points over half the hours
        self.assertAlmostEqual(at[MON + 17 * H], 60.0)
        self.assertAlmostEqual(out[-1][1], 60.0)
        self.assertEqual(out[-1][0], MON + 20 * H)
        # Capped at 100, then flat to the end.
        out = chart._walk(w, 90.0, 40.0, MON, MON + D)
        self.assertEqual(out[-1], [MON + D, 100.0])
        self.assertTrue(all(u <= 100.0 for _, u in out))
        self.assertEqual(chart._walk(w, 20.0, None, MON, MON + D), [])

    def test_thinning_keeps_the_ends_and_the_order(self):
        pts = [[MON + 60 * i, float(i)] for i in range(600)]
        out = chart._thin(pts, MON, MON + 600 * 60, 50)
        self.assertLessEqual(len(out), 52)
        self.assertEqual(out[0], pts[0])
        self.assertEqual(out[-1], pts[-1])
        self.assertEqual(out, sorted(out))
        self.assertEqual(chart._thin(pts[:10], MON, MON + 600, 50), pts[:10])


class Ticks(unittest.TestCase):
    def test_hours_for_the_5_hours(self):
        grid, labels = chart._ticks(config.FIVE_HOUR, R5 - 5 * H, R5)
        self.assertEqual(grid, [int(MON + h * H) for h in (12, 13, 14, 15, 16)])
        self.assertEqual([t for _, t in labels], ["12", "13", "14", "15", "16"])
        # A window opened on the hour labels its ends too.
        grid, labels = chart._ticks(config.FIVE_HOUR, MON + 12 * H, MON + 17 * H)
        self.assertEqual(grid, [int(MON + h * H) for h in (13, 14, 15, 16)])
        self.assertEqual([t for _, t in labels], ["12", "13", "14", "15", "16", "17"])

    def test_midnights_and_day_names_for_the_week(self):
        grid, labels = chart._ticks(config.SEVEN_DAY, RW - 7 * D, RW)
        self.assertEqual(grid, [int(MON + d * D) for d in range(-2, 4 + 1)])
        # Fri 04:00 to Sat 00:00 is named, after the whole days; Fri 00:00 to
        # 04:00 is too short for a name.
        self.assertEqual([t for _, t in labels], ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"])
        self.assertEqual(labels[2][0], int(MON + 12 * H))
        self.assertEqual(labels[-1][0], int(RW - 7 * D + 10 * H))


class Nights(unittest.TestCase):
    def test_the_hours_outside_the_waking_hours(self):
        t0, t1 = RW - 7 * 86400, RW  # Fri 04:00 to Fri 04:00 (UTC)
        n = chart._nights(t0, t1, (8, 23))
        self.assertEqual(n[0], [t0, int(t0 + 4 * H)])  # to 08:00
        self.assertEqual(n[1], [int(t0 + 19 * H), int(t0 + 28 * H)])  # 23:00 to 08:00
        self.assertEqual(n[-1], [int(t1 - 5 * H), t1])  # 23:00 to the reset
        self.assertEqual(len(n), 8)
        # Waking hours across midnight: awake 20:00 to 04:00.
        self.assertEqual(chart._nights(MON, MON + D, (20, 4)), [[int(MON + 4 * H), int(MON + 20 * H)]])

    def test_with_a_dst_change(self):
        with zone("America/New_York"):
            reset = utc_ts(2026, 3, 10, 16)
            for a, b in chart._nights(reset - 7 * D, reset, (8, 23)):
                self.assertIn(clock.localtime(a).tm_hour, (23, 12))
                self.assertIn(clock.localtime(b).tm_hour, (8, 12))


class Snapshots(TempHome):
    def v(self, **kw):
        v = dict(window=config.FIVE_HOUR, present=True, window_id=7, used=30.0, rate=12.0, verdict="on_pace")
        v.update(kw)
        return v

    def log(self, window=config.FIVE_HOUR):
        return (self.st.meta_get("forecasts:" + window) or {}).get("log", [])

    def test_one_every_15_minutes_for_the_5_hours(self):
        self.assertTrue(chart.snapshot(self.st, self.v(projected_raw=55.0), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(used=31.0), NOW + 14 * 60))
        self.assertTrue(chart.snapshot(self.st, self.v(used=33.0), NOW + 15 * 60))
        # [time, level, slope, where it lands uncapped] (the level when it is not known).
        self.assertEqual(self.log(), [[int(NOW), 30.0, 12.0, 55.0], [int(NOW + 15 * 60), 33.0, 12.0, 33.0]])

    def test_a_new_window_starts_over_and_the_last_ones_are_kept(self):
        for k in range(chart.KEEP + 5):
            chart.snapshot(self.st, self.v(), NOW + k * 900)
        self.assertEqual(len(self.log()), chart.KEEP)
        self.assertEqual(self.log()[-1][0], int(NOW + (chart.KEEP + 4) * 900))
        chart.snapshot(self.st, self.v(window_id=8), NOW + 100 * 900)
        self.assertEqual(len(self.log()), 1)

    def test_nothing_to_keep_without_a_forecast(self):
        self.assertFalse(chart.snapshot(self.st, self.v(verdict="maxed"), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(verdict="unknown"), NOW))
        self.assertFalse(chart.snapshot(self.st, self.v(rate=None), NOW))
        self.assertFalse(chart.snapshot(self.st, {"window": config.FIVE_HOUR, "present": False}, NOW))
        # The week keeps its percent per share, one every 2 hours.
        wk = self.v(window=config.SEVEN_DAY, rate=None, _path_k=55.0)
        self.assertTrue(chart.snapshot(self.st, wk, NOW))
        self.assertFalse(chart.snapshot(self.st, wk, NOW + 2 * H - 60))
        self.assertTrue(chart.snapshot(self.st, wk, NOW + 2 * H))
        self.assertEqual(self.log(config.SEVEN_DAY)[0], [int(NOW), 30.0, 55.0, 30.0])


class Build(TempHome):
    def work(self, until, every=600, step=1.0, start=11 * H + 40 * 60):
        """5-hour readings from 11:40 rising `step` a reading, the week fixed."""
        self.known("s1")
        t, u = MON + start, 0.0
        while t <= until:
            u += step
            self.feed(t, five=(int(u), R5), week=(30, RW))
            t += every
        return int(u)

    def views(self, now):
        v5, vw, _ = commands.views(self.st, self.cfg, now)
        return v5, vw

    def test_used_and_the_forecast_to_the_reset(self):
        now = MON + 14 * H + 40 * 60
        self.work(now)
        v5, vw = self.views(now)
        c = chart.build(self.st, v5, now)
        self.assertEqual((c["t0"], c["t1"], c["now"]), (R5 - 5 * 3600, R5, int(now)))
        self.assertEqual(c["actual"][0], [int(MON + 11 * H + 30 * 60), 0.0])  # the window's start at 0
        self.assertEqual(c["actual"][-1], [int(now), v5["used"]])
        self.assertEqual(c["actual"], sorted(c["actual"]))
        self.assertEqual(c["forecast"][0], [int(now), v5["used"]])
        self.assertEqual(c["forecast"][-1][0], R5)
        self.assertAlmostEqual(c["forecast"][-1][1], v5["projected"], places=1)
        lo, hi = c["fan"]  # a fitted rate: 25% either way (pace.SPREAD)
        self.assertAlmostEqual(lo[-1][1], round(v5["used"] + 0.75 * v5["rate"] * (R5 - now) / H, 2), places=2)
        self.assertAlmostEqual(hi[-1][1], round(min(100, v5["used"] + 1.25 * v5["rate"] * (R5 - now) / H), 2), places=2)
        self.assertEqual(c["ghosts"], [])  # no snapshot yet
        self.assertEqual([t for _, t in c["labels"]], ["12", "13", "14", "15", "16"])
        w = chart.build(self.st, vw, now)
        self.assertEqual(w["t1"], RW)
        self.assertAlmostEqual(w["forecast"][-1][1], vw["projected"], places=0)
        json.dumps(c)  # plain data for the band's JSON

    def test_an_early_estimate_has_a_fan(self):
        now = MON + 11 * H + 52 * 60
        self.work(now, step=2.0)  # 2 readings: fewer than rate_min_samples
        v5, _ = self.views(now)
        self.assertEqual(v5["rate_source"], "interpolated")
        c = chart.build(self.st, v5, now)
        lo, hi = c["fan"]
        # The lines at the rate 40% lower and higher, each capped at 100.
        line = lambda k: [[int(t), round(u, 2)] for t, u in chart._line(v5["used"], v5["rate"] * k, now, R5)]  # noqa: E731
        self.assertEqual((lo, hi), (line(0.6), line(1.4)))
        self.assertLess(lo[-1][1], c["forecast"][-1][1])
        self.assertGreater(hi[-1][1], c["forecast"][-1][1])

    def test_a_forecast_that_runs_out_early_stays_inside_its_fan(self):
        now = MON + 11 * H + 52 * 60
        self.work(now, step=25.0)  # 50% in 2 readings: every line reaches 100 before 16:30
        v5, _ = self.views(now)
        self.assertEqual(v5["rate_source"], "interpolated")
        c = chart.build(self.st, v5, now)
        lo, hi = c["fan"]
        self.assertEqual(len(c["forecast"]), 3)  # up to 100, then flat
        for path in (lo, hi, c["forecast"]):
            for t, _ in path:
                u = chart._at(c["forecast"], t)
                self.assertLessEqual(chart._at(lo, t) - 0.01, u, t)
                self.assertLessEqual(u, chart._at(hi, t) + 0.01, t)
        self.assertLess(hi[1][0], c["forecast"][1][0])  # the faster line runs out first

    def test_a_capped_estimate_fans_out_over_when_it_runs_out(self):
        # 50% used at 50%/h, 4 hours left: every line reaches 100, at different times.
        five = {"window": config.FIVE_HOUR, "spread": 0.4}
        lo, hi = chart._fan(five, None, 50.0, 50.0, 0, 4 * H)
        self.assertEqual(lo, [[0, 50.0], [int(5 * H / 3), 100.0], [int(4 * H), 100.0]])
        self.assertEqual(hi, [[0, 50.0], [int(5 * H / 7), 100.0], [int(4 * H), 100.0]])
        self.assertIsNone(chart._fan(five, None, 64.0, 0.0, 0, H))  # idle
        self.assertIsNone(chart._fan(five, None, 99.5, 50.0, 0, H))  # nothing left to spread

    def snaps(self, window, log, wid):
        self.st.meta_set("forecasts:" + window, {"wid": wid, "log": log})

    def test_an_hour_old_forecast_is_a_ghost(self):
        now = MON + 14 * H + 40 * 60
        self.work(now)
        v5, _ = self.views(now)
        wid = v5["window_id"]
        # 1 h 40 min ago is too far from an hour; so is 20 min ago.
        far = [int(MON + 13 * H), 9.0, 6.0, 30.0]
        near = [int(now - 20 * 60), 50.0, 15.0, 80.0]
        self.snaps(config.FIVE_HOUR, [far, near], wid)
        self.assertEqual(chart.build(self.st, v5, now)["ghosts"], [])
        hour = [int(MON + 13 * H + 45 * 60), 20.0, 12.0, 53.0]
        self.snaps(config.FIVE_HOUR, [far, hour, near], wid)
        g = chart.build(self.st, v5, now)["ghosts"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0], [[hour[0], 20.0], [R5, 20.0 + 12.0 * (R5 - hour[0]) / H]])
        # Another window's snapshots are not this one's.
        self.snaps(config.FIVE_HOUR, [hour], wid + 1)
        self.assertEqual(chart.build(self.st, v5, now)["ghosts"], [])

    def test_a_week_snapshot_from_the_first_version_still_draws(self):
        # [time, level, slope], before snapshots kept where they land: walked with that slope.
        now = MON + 14 * H
        self.known("s1")
        for k in range(1, 30):
            self.feed(MON - 2 * D + k * 2 * H, five=None, week=(k, RW))
        _, vw = self.views(now)
        old, new = [int(now - D), 10.0, 40.0], [int(now - 2 * D), 3.0, 40.0, 60.0]
        self.snaps(config.SEVEN_DAY, [new, old], vw["window_id"])
        c = chart.build(self.st, vw, now)
        walk = [[int(t), round(u, 2)] for t, u in chart._thin(chart._walk(vw["_weights"], 10.0, 40.0, old[0], RW),
                                                                old[0], RW, 60)]
        self.assertEqual(c["ghosts"][0], walk)
        self.assertAlmostEqual(c["ghosts"][1][-1][1], 60.0, places=1)

    def test_the_week_has_ghosts_from_a_day_and_2_days_ago_that_land_where_they_said(self):
        now = MON + 14 * H
        self.known("s1")
        for k in range(1, 30):
            self.feed(MON - 2 * D + k * 2 * H, five=None, week=(k, RW))
        _, vw = self.views(now)
        wid = vw["window_id"]
        day, two = [int(now - D - H), 10.0, 40.0, 70.0], [int(now - 2 * D + 2 * H), 3.0, 40.0, 130.0]
        self.snaps(config.SEVEN_DAY, [two, day], wid)
        g = chart.build(self.st, vw, now)["ghosts"]
        self.assertEqual([x[0] for x in g], [[day[0], 10.0], [two[0], 3.0]])
        # Walked along today's profile, each ends where its forecast said (capped at 100).
        self.assertAlmostEqual(g[0][-1][1], 70.0, places=1)
        self.assertEqual(g[1][-1], [RW, 100.0])
        self.assertEqual(g[0][-1][0], RW)

    def test_a_run_out_before_the_reset_is_marked(self):
        # 50% at 30 %/h from 14:40 with 1 h 50 left: out at 16:20 (the reset 16:30
        # is 10 min on: within the margin, no mark); 80% at 60 %/h: out at 15:00.
        now = MON + 14 * H + 40 * 60
        self.work(now, step=5.0, every=600, start=14 * H)
        v5, _ = self.views(now)
        c = chart.build(self.st, v5, now)
        if v5.get("cap_ts") and R5 - v5["cap_ts"] > 600 and not v5.get("shaky"):
            self.assertEqual(c["out"], int(v5["cap_ts"]))
            from glidepath import band
            self.assertEqual(c["out_label"], band.at(v5["cap_ts"], now))
        else:
            self.assertIsNone(c["out"])
        # Built by hand: a firm run-out is marked, a shaky one or one at the reset is not.
        v = dict(v5, cap_ts=now + 1800, shaky=False)
        self.assertEqual(chart.build(self.st, v, now)["out"], int(now + 1800))
        self.assertIsNone(chart.build(self.st, dict(v, shaky=True), now)["out"])
        self.assertIsNone(chart.build(self.st, dict(v, cap_ts=R5 - 300), now)["out"])
        self.assertIsNone(chart.build(self.st, dict(v, cap_ts=None), now)["out"])

    def test_a_maxed_week_flattens_the_5_hours(self):
        now = MON + 14 * H + 40 * 60
        self.work(now)
        v5, vw = self.views(now)
        vw = dict(vw, verdict="maxed")
        chart.attach(self.st, v5, vw, now)
        f = v5["chart"]["forecast"]
        self.assertEqual(f, [[int(now), round(v5["used"], 2)], [R5, round(v5["used"], 2)]])
        self.assertIsNone(v5["chart"]["fan"])
        self.assertIsNone(v5["chart"]["out"])  # nothing runs out: it cannot move

    def test_a_broken_chart_is_none_not_the_band(self):
        v5 = {"window": config.FIVE_HOUR, "present": True}  # no reset_ts
        vw = {"window": config.SEVEN_DAY, "present": False}
        chart.attach(self.st, v5, vw, NOW)
        self.assertIsNone(v5["chart"])
        self.assertIsNone(vw["chart"])

    def test_the_status_line_keeps_snapshots_and_the_band_has_charts(self):
        self.known("s1")
        opts = statusline.parse_args(["--source", "mod", "--format", "json"])
        t = MON + 11 * H + 40 * 60
        for k in range(12):
            raw = json.dumps(payload((2 * k + 1, R5), (30, RW))).encode()
            v5, vw, _ = statusline.compute(raw, self.cfg, opts, t + k * 600)
        log = self.st.meta_get("forecasts:" + config.FIVE_HOUR)["log"]
        self.assertGreaterEqual(len(log), 2)
        self.assertTrue(all(b[0] - a[0] >= 900 for a, b in zip(log, log[1:])))
        ws = band.windows(v5, vw, t + 11 * 600)
        self.assertTrue(all(isinstance(w["chart"], dict) and w["chart"]["actual"] for w in ws), ws)
        # The terminal line draws no chart.
        opts = statusline.parse_args(["--source", "test"])
        v5, _, _ = statusline.compute(json.dumps(payload((25, R5), (30, RW))).encode(), self.cfg, opts, t + 12 * 600)
        self.assertNotIn("chart", v5)

    def test_now_json_has_no_private_keys(self):
        self.known("s1")
        self.feed(MON + 12 * H, five=(10, R5), week=(30, RW))
        _, vw, _ = commands.views(self.st, self.cfg, MON + 13 * H)
        self.assertIn("_weights", vw)  # the chart's
        out = io.StringIO()
        with mock.patch.object(time, "time", return_value=MON + 13 * H), contextlib.redirect_stdout(out):
            self.assertEqual(commands.cmd_now(argparse.Namespace(json=True), self.cfg), 0)
        view = json.loads(out.getvalue())
        self.assertTrue(view["seven_day"]["present"])
        for w in ("five_hour", "seven_day"):
            self.assertFalse([k for k in view[w] if k.startswith("_")], w)


def utc_ts(*when):
    return calendar.timegm(when + (0,) * (6 - len(when)))


class DaylightSaving(unittest.TestCase):
    """A week's days in zones whose clocks change: each day starts at its own
    midnight (01:00 where the change skips midnight), whatever its length."""

    def week(self, name, reset, late=None):
        """The week to `reset` in zone `name`: 7 day starts on 7 dates, each at
        00:00, or at 01:00 on the day of the month `late`."""
        with zone(name):
            grid, labels = chart._ticks(config.SEVEN_DAY, reset - 7 * D, reset)
            starts = [clock.localtime(t) for t in grid]
        self.assertEqual(len(grid), 7, [time.strftime("%a %d %H:%M", lt) for lt in starts])
        self.assertEqual(len({(lt.tm_mon, lt.tm_mday) for lt in starts}), 7)
        for lt in starts:
            self.assertEqual((lt.tm_hour, lt.tm_min), (1 if lt.tm_mday == late else 0, 0), time.strftime("%c", lt))
        return [t for _, t in labels]

    def test_new_york_spring_and_fall(self):
        # Tue 12:00 resets around the change on Sun Mar 8 and Sun Nov 1, 2026.
        names = self.week("America/New_York", utc_ts(2026, 3, 10, 16))
        self.assertEqual(names, ["Wed", "Thu", "Fri", "Sat", "Sun", "Mon"])
        self.week("America/New_York", utc_ts(2026, 11, 3, 17))

    def test_berlin_spring_and_fall(self):
        self.week("Europe/Berlin", utc_ts(2026, 3, 31, 10))
        self.week("Europe/Berlin", utc_ts(2026, 10, 27, 11))

    def test_santiago_skips_midnight(self):
        # Sun Sep 6, 2026 starts at 01:00: the clocks go from 00:00 to 01:00.
        self.week("America/Santiago", utc_ts(2026, 9, 8, 15), late=6)


class Png(unittest.TestCase):
    def test_a_line_is_drawn_where_it_goes(self):
        c = Canvas(40, 10)
        c.polyline([(5, 5), (35, 5)], 2.0, (0, 0, 0, 1.0))
        a = [p[3] for p in c.px]
        self.assertEqual(a[5 * 40 + 20], 1.0)
        self.assertEqual(a[4 * 40 + 20], 1.0)
        self.assertEqual(a[1 * 40 + 20], 0.0)
        self.assertEqual(a[5 * 40 + 39], 0.0)

    def test_a_dashed_line_has_gaps_and_joints_are_not_darker(self):
        c = Canvas(40, 10)
        c.polyline([(0, 5), (40, 5)], 2.0, (0, 0, 0, 1.0), dash=(4, 4))
        row = [c.px[5 * 40 + x][3] for x in range(40)]
        self.assertEqual(row[1], 1.0)
        self.assertEqual(row[6], 0.0)
        c = Canvas(40, 10)
        c.polyline([(0, 5), (20, 5), (40, 5)], 2.0, (0, 0, 0, 0.5))
        self.assertAlmostEqual(c.px[5 * 40 + 20][3], 0.5)

    def test_fill_between_two_lines(self):
        c = Canvas(10, 10)
        c.fill_between(0, 10, lambda x: 2.5, lambda x: 8.0, (0, 0, 0, 1.0))
        col = [c.px[y * 10 + 4][3] for y in range(10)]
        self.assertEqual(col, [0.0, 0.0, 0.5, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0])

    def test_a_ring(self):
        c = Canvas(20, 20)
        c.ring(10, 10, 6, 2.0, (0, 0, 0, 1.0))
        self.assertEqual(c.px[10 * 20 + 10][3], 0.0)  # the middle is open
        self.assertGreater(c.px[10 * 20 + 16][3], 0.9)


def a_chart(window=config.FIVE_HOUR):
    if window == config.FIVE_HOUR:
        t0, t1 = R5 - 5 * 3600, R5
    else:
        t0, t1 = RW - 7 * 86400, RW
    now = t0 + (t1 - t0) * 0.6
    grid, labels = chart._ticks(window, t0, t1)
    return {"t0": t0, "t1": t1, "now": int(now), "actual": [[t0, 0.0], [int(now) - 600, 50.0], [int(now), 55.0]],
            "forecast": [[int(now), 55.0], [t1, 90.0]], "fan": [[[int(now), 55.0], [t1, 80.0]], [[int(now), 55.0], [t1, 100.0]]],
            "ghosts": [[[t0 + 3600, 20.0], [t1, 70.0]]],
            "grid": grid, "labels": labels}


class MenuChart(TempHome):
    def test_a_chart_picture_of_the_size_asked(self):
        for dark in (False, True):
            w, h, alpha = pixels(menuart.chart(a_chart(), 300, 40, dark, "error"))
            self.assertEqual((w, h), (600, 80))
            self.assertTrue(any(alpha))

    def test_labels_sit_under_their_grid_lines(self):
        c = a_chart()
        text = menuart.labels(c, 300, 6.02)
        self.assertLessEqual(len(text) * 6.02, 300)
        for t, label in c["labels"]:
            col = text.index(label)
            middle = (col + len(label) / 2.0) * 6.02
            self.assertLess(abs(middle - menuart._x(c, t, 300)), 6.02, (label, text))
        week = menuart.labels(a_chart(config.SEVEN_DAY), 300, 6.02)
        self.assertEqual(week.split(), ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])

    def test_every_whole_day_is_named_in_the_narrowest_menu(self):
        # A Friday 17:00 reset: the 7 hours before the first midnight go unnamed,
        # so Saturday keeps its name at the menu's narrowest (BAR_MIN).
        t1 = int(MON + 4 * D + 17 * H)
        grid, labels = chart._ticks(config.SEVEN_DAY, t1 - 7 * 86400, t1)
        c = {"t0": t1 - 7 * 86400, "t1": t1, "grid": grid, "labels": labels}
        self.assertEqual(menuart.labels(c, menubar.BAR_MIN, menubar.LABEL_PT).split(),
                         ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"])

    def test_a_named_edge_day_gives_way_to_a_whole_day(self):
        # A Friday 06:00 reset names the 18 hours of the first Friday; in a
        # narrow line it has no room beside Saturday, which keeps its name.
        t1 = int(MON + 4 * D + 6 * H)
        grid, labels = chart._ticks(config.SEVEN_DAY, t1 - 7 * 86400, t1)
        # Whole days first, then the edge day.
        self.assertEqual([t for _, t in labels], ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"])
        self.assertEqual(labels[-1][0], t1 - 7 * 86400 + 9 * 3600)
        c = {"t0": t1 - 7 * 86400, "t1": t1, "grid": grid, "labels": labels}
        self.assertEqual(menuart.labels(c, 160, menubar.LABEL_PT).split()[0], "Sat")
        self.assertEqual(menuart.labels(c, 300, menubar.LABEL_PT).split(),
                         ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])

    def test_labels_that_do_not_fit_are_left_out(self):
        # 13 a minute after 12 has no room; 16 at the right edge ends there.
        c = dict(a_chart(), labels=[[R5 - 3 * 3600, "12"], [R5 - 3 * 3600 + 60, "13"], [R5, "16"]])
        text = menuart.labels(c, 100, 6.02)
        self.assertEqual(text.split(), ["12", "16"])
        self.assertEqual(len(text), int(100 // 6.02))

    def menu(self, cfg=None):
        v5, vw = STATES["on pace"]
        ws = band.windows(dict(v5, chart=a_chart()), dict(vw, chart=a_chart(config.SEVEN_DAY)), NOW)
        return menubar.lines(ws, NOW - 60, NOW, cfg or self.cfg)

    def test_the_menu_has_a_chart_under_each_bar(self):
        out = self.menu()
        bars = [i for i, line in enumerate(out) if line.startswith(("5h ", "Wk "))]
        self.assertEqual(len(bars), 2)
        for i in bars:
            width = int(out[i].split(" width=")[1].split()[0]) + 0
            self.assertRegex(out[i + 1], r'^ \| image=\S+,\S+ width=\d+ height=%d tooltip="What was used' % menubar.CHART_H)
            self.assertNotIn("color=", out[i + 1])  # SwiftBar drops a picture with a color
            self.assertTrue(out[i + 2].endswith(" | font=Menlo size=10 trim=false color=" + menubar.DIM), out[i + 2])
            self.assertGreater(int(out[i + 1].split(" width=")[1].split()[0]), width)  # under the bar and its label
        self.assertEqual(out[bars[1] + 2].split(" | ")[0].split(), ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])

    def test_charts_can_be_turned_off(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["menubar"]["charts"] = False
        self.assertFalse([line for line in self.menu(cfg) if line.startswith(" | image=")])
        v5, vw = STATES["on pace"]
        out = menubar.lines(band.windows(v5, vw, NOW), NOW - 60, NOW, self.cfg)  # no chart in the views
        self.assertFalse([line for line in out if line.startswith(" | image=")])

    def test_the_forecast_takes_the_paces_color(self):
        cases = [("error", STATES["runs out"][0]),
                 ("warning", a_v5(used=60, verdict="slow_down", multiplier=0.8, projected=96, severity="warn")),
                 ("inactive", STATES["on pace"][0])]
        for tone, v in cases:
            ws = band.windows(dict(v, chart=a_chart()), STATES["on pace"][1], NOW)
            self.assertEqual(menubar.tone(ws[0]), tone)
            out = menubar.lines(ws, NOW - 60, NOW, self.cfg)
            line = out[[i for i, x in enumerate(out) if x.startswith("5h ")][0] + 1]
            width = int(re.search(r" width=(\d+)", line).group(1))
            pics = re.search(r"image=(\S+),(\S+) ", line).groups()
            self.assertEqual(pics, (menuart.chart(a_chart(), width, menubar.CHART_H, False, tone),
                                    menuart.chart(a_chart(), width, menubar.CHART_H, True, tone)), tone)
        self.assertNotEqual(menuart.chart(a_chart(), 300, 40, False, "error"),
                            menuart.chart(a_chart(), 300, 40, False, "warning"))

    def test_swiftbar_reads_the_whole_tooltips(self):
        out = self.menu()
        tips = [swiftbar_params(line.split(" | ", 1)[1]).get("tooltip") for line in out if "tooltip=" in line]
        self.assertEqual(len(tips), 4)  # 2 bars, 2 charts
        self.assertTrue(tips[0].startswith("5-hour: 55% used, resets 16:30."), tips[0])
        self.assertEqual(tips[1], "What was used (the line), the forecast to the reset (dashed) and the "
                                  "forecasts made an hour ago (grey)")
        self.assertEqual(menubar.quoted('it\'s "x" a\\b | c'), '"it\u2019s \u201dx\u201d a/b / c"')

    def test_no_charts_are_built_for_a_menu_that_draws_none(self):
        self.known("s1")
        now = time.time()
        self.feed(now - 600, five=(10, int(now + 3 * H)), week=(30, int(now + 3 * D)))
        for style, charts, built in (("bars", True, True), ("bars", False, False), ("text", True, False)):
            cfg = json.loads(json.dumps(self.cfg))
            cfg["menubar"].update(style=style, charts=charts, notify=False)
            with mock.patch.object(chart, "attach") as attach, contextlib.redirect_stdout(io.StringIO()):
                menubar.show(cfg)
            self.assertEqual(attach.called, built, (style, charts))


def swiftbar_params(text):
    """SwiftBar's parameter parser (MenuLineParameters.getParams, v2.0.1):
    a quoted value runs to its closing quote, an unquoted one to a space."""
    params, i, n = {}, 0, len(text)
    while i < n:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        k = i
        while i < n and text[i] != "=":
            i += 1
        if i >= n:
            break
        key = text[k:i].strip().lower()
        i += 1
        value = ""
        if i < n and text[i] in "\"'":
            q, i = text[i], i + 1
            while i < n and text[i] != q:
                value += text[i]
                i += 1
            i += 1
        else:
            while i < n and text[i] not in " \t":
                value += text[i]
                i += 1
        params[key] = value
    return params


if __name__ == "__main__":
    unittest.main()
