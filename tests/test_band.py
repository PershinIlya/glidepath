"""The band: bars, words and the terminal line (glidepath/band.py)."""

import json
import os
import unittest

from glidepath import band, config, render, statusline

from .helpers import MON, H, D, TempHome, payload, utc

NOW = MON + 14 * H + 40 * 60  # Monday 14:40 UTC
CELLS = set("━╍─┈")


def v5(**kw):
    v = dict(window=config.FIVE_HOUR, present=True, used=55.3, reset_ts=int(MON + 16 * H + 30 * 60),
             verdict="on_pace", multiplier=1.05, projected=95.0, cap_ts=None, allowed=18.0, rate=17.0,
             rate_source="lsq", severity="dim", target=100.0, unused_at_reset=5.0, known_ts=NOW - 60,
             last_seen=NOW - 60)
    v.update(kw)
    return v


def vw(**kw):
    v = dict(window=config.SEVEN_DAY, present=True, used=48.0, reset_ts=int(MON + 4 * D + 4 * H),
             verdict="on_pace", multiplier=1.05, projected=96.0, dry_ts=None, delta=1.0, severity="dim",
             target=100.0, expire=4.0, intensity_source="recent", last_seen=NOW - 300)
    v.update(kw)
    return v


STATES = {
    "on pace": (v5(), vw()),
    "early": (v5(used=8, rate_source="interpolated", verdict="speed_up", multiplier=1.8, projected=58),
              vw(used=2, verdict="speed_up", multiplier=2.7, projected=39)),
    "runs out": (v5(used=70, verdict="slow_down", multiplier=0.5, cap_ts=MON + 15 * H + 40 * 60, projected=100,
                    severity="alert"), vw()),
    "maxed": (v5(used=100, verdict="maxed", severity="alert", projected=100), vw(used=52)),
    "idle": (v5(used=64, verdict="idle", rate=0.0, projected=64), vw(used=35)),
    "unused": (v5(used=12, verdict="speed_up", multiplier=1.7, projected=40),
               vw(used=40, projected=60, severity="opportunity", verdict="speed_up", multiplier=2.5, expire=40.0)),
    "week dry": (v5(), vw(used=80, verdict="slow_down", multiplier=0.4, dry_ts=MON + 2 * D + 11 * H,
                          projected=100, severity="alert")),
    "half": (v5(), {"present": False}),
}


def words(p):
    return None if p is None else p["pre"] + p["num"] + p["post"]


class Words(unittest.TestCase):
    def setUp(self):
        utc()

    def test_each_state_reads_as_designed(self):
        # The same kind of words for both windows: the pace against the reset.
        want = {
            "on pace": (("run", "On pace"), ("run", "On pace")),
            "early": (("rough", "~2x faster"), ("run", "2.7x faster")),
            "runs out": (("dry", "2x slower"), ("run", "On pace")),
            "maxed": (("maxed", "Back at 16:30"), ("run", "On pace")),
            "idle": (("idle", "Idle"), ("run", "On pace")),
            "unused": (("run", "1.7x faster"), ("expire", "2.5x faster")),
            "week dry": (("run", "On pace"), ("dry", "2.5x slower")),
            "half": (("run", "On pace"), ("wait", None)),
        }
        for name, (a, b) in STATES.items():
            got = tuple((w["kind"], words(w["phrase"])) for w in band.windows(a, b, NOW))
            self.assertEqual(got, want[name], name)

    def test_tones_and_attention(self):
        h5, wk = band.windows(*STATES["unused"], NOW)
        self.assertEqual([(w["phrase"]["tone"], w["phrase"]["attention"], w["phrase"]["calm"]) for w in (h5, wk)],
                         [("success", False, False)] * 2)  # green, kept where room is short
        h5, wk = band.windows(*STATES["runs out"], NOW)
        self.assertEqual((h5["phrase"]["tone"], h5["phrase"]["attention"], h5["phrase"]["num"]), ("error", True, "2x"))
        self.assertEqual((wk["phrase"]["tone"], wk["phrase"]["calm"]), ("inactive", True))  # "On pace" goes first
        # A milder overshoot or an early estimate: yellow; close to 1: "Ease off".
        soft = band.phrase(v5(verdict="on_pace", cap_ts=MON + 16 * H, severity="warn"), NOW)
        self.assertEqual((words(soft), soft["tone"], soft["attention"]), ("Ease off", "warning", True))
        p = band.phrase(v5(verdict="slow_down", severity="warn", multiplier=0.7), NOW)
        self.assertEqual((words(p), p["tone"]), ("1.4x slower", "warning"))  # a mild overshoot
        p = band.phrase(v5(verdict="slow_down", severity="alert", multiplier=0.7), NOW)
        self.assertEqual((words(p), p["tone"]), ("1.4x slower", "warning"))  # above the target, no run-out
        # Review: "Pause", the worst, was yellow beside a red "2x slower".
        p = band.phrase(v5(verdict="slow_down", severity="alert", multiplier=0.05, cap_ts=MON + 15 * H), NOW)
        self.assertEqual((words(p), p["tone"]), ("Pause", "error"))
        # Review: a run-out within the margin of the reset (plain bar, no Runs out row) is not red.
        v = v5(verdict="slow_down", severity="alert", multiplier=0.85, cap_ts=int(MON + 16 * H + 25 * 60))
        self.assertEqual((band.bar_kind(v), band.phrase(v, NOW)["tone"]), ("run", "warning"))
        self.assertEqual(words(band.phrase(v5(verdict="slow_down", multiplier=None), NOW)), "Ease off")
        self.assertIsNone(band.phrase(v5(verdict="unknown", multiplier=None, projected=None), NOW))

    def test_multipliers(self):
        cases = [(1.3, "lsq", "1.3x faster"), (2.0, "lsq", "2x faster"), (12, "lsq", "Plenty of room"),
                 (1.2, "interpolated", "A bit faster"), (1.3, "interpolated", "~1.5x faster"),
                 (2.3, "interpolated", "~2.5x faster")]
        for m, source, want in cases:
            v = v5(verdict="speed_up", multiplier=m, rate_source=source)
            p = band.phrase(v, NOW)
            self.assertEqual((words(p), p["narrow_pre"] + p["num"] + p["narrow"]), (want, want), (m, source))
            self.assertIn(want.lstrip("~").split(" faster")[0].lower(), band.sentence(v, NOW).lower())

    def test_slow_down_multipliers(self):
        # How much slower: 1 / the multiplier; an early estimate in halves.
        cases = [(0.5, "lsq", "2x slower", "Go 2x slower"), (0.75, "lsq", "1.3x slower", "Go 1.3x slower"),
                 (0.05, "lsq", "Pause", "Pause"), (0.6, "interpolated", "~1.5x slower", "Go ~1.5x slower"),
                 (0.8, "interpolated", "Ease off", "Ease off")]
        for m, source, want, said in cases:
            v = v5(verdict="slow_down", multiplier=m, rate_source=source, projected=110)
            self.assertEqual(words(band.phrase(v, NOW)), want, (m, source))
            self.assertTrue(band.sentence(v, NOW).endswith(said + "."), band.sentence(v, NOW))

    def test_details_rows(self):
        def rows(w):
            return [(r["label"], r["value"], r["tone"]) for r in w["details"]]

        h5, wk = band.windows(*STATES["runs out"], NOW)
        # Running out before the reset: that time, the reset its note.
        self.assertEqual(rows(h5), [("Used", "70%", "text"), ("Pace", "2x slower", "error"),
                                    ("At reset", "about 100%", "text"), ("Runs out", "15:40, 50 min early", "error"),
                                    ("Rate", "17%/h, 18%/h fits", "text")])
        self.assertEqual(h5["details"][3]["note"], "resets 16:30")
        _, wk = band.windows(*STATES["unused"], NOW)
        # What goes unused is the note of where it lands.
        at = [r for r in wk["details"] if r["label"] == "At reset"][0]
        self.assertEqual((at["value"], at["note"], at["note_tone"]), ("about 60%", "40% unused", "success"))
        self.assertEqual(rows(wk)[-1], ("Resets", "Fri 04:00", "text"))  # no rate for the week
        self.assertNotIn("note", wk["details"][-1])  # no time it would run out: no pace in the fixture
        h5, _ = band.windows(*STATES["maxed"], NOW)
        self.assertEqual(rows(h5), [("Used", "100%", "text"), ("Limit", "reached", "error"), ("Resets", "16:30", "text")])
        h5, _ = band.windows(*STATES["idle"], NOW)
        self.assertEqual(rows(h5)[-1], ("Rate", "idle, 18%/h fits", "text"))
        # Review: a window without the math still fills its column in Details.
        self.assertEqual(rows(band.windows({"present": False}, {"present": False}, NOW)[0]),
                         [("Used", "waiting for a reading", "inactive")])
        h5, _ = band.windows({"present": False, "raw": (12.0, MON + 17 * H)}, {"present": False}, NOW)
        self.assertEqual(rows(h5), [("Used", "12%", "text"), ("Pace", "no pace yet", "inactive"),
                                    ("Resets", "17:00", "text")])
        # Review: an ended 5 hours does not open "with your next message" while the week is at its limit.
        h5, _ = band.windows({"present": False, "ended_ts": MON + 3 * H}, vw(used=100, verdict="maxed"), NOW)
        self.assertEqual((h5["phrase"], rows(h5)[1:]), (None, [("Pace", "blocked by the week", "error"),
                                                               ("Opens", "after Fri 04:00", "text")]))

    def test_week_back_and_resets_name_the_day(self):
        v = vw(used=100, verdict="maxed", severity="alert")
        self.assertEqual(words(band.phrase(v, NOW)), "Back Fri 04:00")
        self.assertEqual(band.sentence(v, NOW), "Week: limit reached. Back Fri 04:00.")
        self.assertEqual(band.windows(v5(), vw(), NOW)[1]["reset"], "Fri")
        self.assertEqual(band.windows(v5(), vw(reset_ts=int(NOW + 5 * H)), NOW)[1]["reset"], "19:40")

    def test_sentences(self):
        h5, wk = band.windows(*STATES["runs out"], NOW)
        self.assertEqual(h5["sentence"], "5-hour: 70% used, resets 16:30. Runs out at 15:40, 50 min before the reset. "
                                         "Go 2x slower.")
        self.assertEqual(wk["sentence"], "Week: 48% used, resets Fri 04:00. On pace, about 96% by then.")
        _, wk = band.windows(*STATES["unused"], NOW)
        self.assertEqual(wk["sentence"], "Week: 40% used, resets Fri 04:00. Can go 2.5x faster, about 60% by then: "
                                         "40% will go unused.")
        h5, _ = band.windows(*STATES["idle"], NOW)
        self.assertEqual(h5["sentence"], "5-hour: 64% used, resets 16:30. Idle: up to 18% an hour is safe.")
        h5, wk = band.windows({"present": False}, {"present": False}, NOW)
        self.assertEqual((h5["sentence"], wk["pct"]), ("5-hour: waiting for the first reading.", None))
        h5, _ = band.windows({"present": False, "raw": (12.0, MON + 17 * H)}, {"present": False}, NOW)
        self.assertEqual((h5["pct"], h5["kind"], h5["sentence"]), (12, "run", "5-hour: 12% used, resets 17:00."))
        _, wk = band.windows(*STATES["week dry"], NOW)
        self.assertEqual(wk["sentence"], "Week: 80% used, resets Fri 04:00. Runs out Wed 11:00, 1 day 17 h before "
                                         "the reset. Go 2.5x slower.")

    def test_over_a_target_below_100_is_a_warning_even_when_idle(self):
        # Review 18: the classic line was yellow at or above a target below
        # 100 whatever the verdict; the band said a calm grey "Idle".
        for verdict in ("idle", "unknown"):
            v = v5(verdict=verdict, used=85, target=80.0, severity="warn", rate=0.0, projected=85, multiplier=None)
            p = band.phrase(v, NOW)
            self.assertEqual((words(p), p["tone"], p["attention"]), ("Over your 80% target", "warning", True), verdict)
            self.assertIn("Over your 80% target.", band.sentence(v, NOW))

    def test_nothing_green_for_less_than_a_point(self):
        # Review 18: with min_free 0 a leftover of 0.6 said "0% will go unused".
        v = vw(used=99.4, projected=99.4, expire=0.6, severity="opportunity", verdict="on_pace")
        self.assertEqual(band.bar_kind(v), "run")
        self.assertEqual(words(band.phrase(v, NOW)), "On pace")
        self.assertNotIn("unused", band.sentence(v, NOW))

    def test_a_maxed_week_blocks_the_5_hours(self):
        # Review 18: the week at its limit, and the 5 hours still said "Can go 2.2x faster".
        h5, wk = band.windows(v5(verdict="speed_up", multiplier=2.2, used=30, projected=70, known_ts=NOW - 100,
                                 rate=40),
                              vw(used=100, verdict="maxed", severity="alert", projected=100), NOW)
        self.assertEqual((h5["kind"], h5["projected"], h5["phrase"], h5["pct"]), ("idle", None, None, 30))
        self.assertEqual(h5["sentence"], "5-hour: 30% used, resets 16:30. Blocked by the weekly limit until Fri 04:00.")
        self.assertEqual(words(wk["phrase"]), "Back Fri 04:00")

    def test_a_run_out_close_to_the_reset_is_on_pace(self):
        # The person's first day: week 17%, the window average stretched over
        # the week ran out 50 min before Friday's reset, and the band said "Out".
        reset = int(MON + 4 * D + 4 * H)
        v = vw(used=17, verdict="on_pace", multiplier=0.98, projected=100, dry_ts=reset - 50 * 60,
               severity="warn", intensity_source="typical", E=0.83)
        self.assertEqual(words(band.phrase(v, NOW)), "On pace")
        self.assertEqual(band.bar_kind(v), "rough")
        self.assertIn("On pace", band.sentence(v, NOW))
        h5 = v5(used=80, verdict="on_pace", cap_ts=int(MON + 16 * H + 25 * 60), severity="warn")  # 5 min early
        self.assertEqual(words(band.phrase(h5, NOW)), "On pace")
        h5 = v5(used=80, verdict="on_pace", cap_ts=int(MON + 16 * H), severity="warn")  # 30 min early
        self.assertEqual(words(band.phrase(h5, NOW)), "Ease off")
        self.assertIn("May run out at 16:00", band.sentence(h5, NOW))

    def test_early_in_the_week_the_window_average_is_rough(self):
        # D-37: while the typical week weighs more than this week's spend.
        early = vw(used=17, verdict="slow_down", multiplier=0.7, projected=100, dry_ts=MON + 2 * D,
                   severity="alert", intensity_source="typical", E=0.85)
        p = band.phrase(early, NOW)
        self.assertEqual((words(p), p["tone"]), ("~1.5x slower", "warning"))
        later = dict(early, intensity_source="recent", E=0.5)
        self.assertEqual(band.phrase(later, NOW)["tone"], "error")

    def test_a_milder_run_out_is_soft(self):
        h5, _ = band.windows(v5(verdict="on_pace", cap_ts=MON + 16 * H, severity="warn"), vw(), NOW)
        self.assertEqual((h5["kind"], h5["soft"], words(h5["phrase"])), ("dry", True, "Ease off"))
        h5, _ = band.windows(*STATES["runs out"], NOW)
        self.assertEqual((h5["kind"], h5["soft"]), ("dry", False))

    def test_percents_round_down_as_claude_code_shows_them(self):
        self.assertEqual([render.pct(x) for x in (36.7, 36.0, 99.99, 100.0, 0.4)], ["36%", "36%", "99%", "100%", "0%"])
        self.assertEqual(band.windows(v5(used=36.96, known_ts=None), vw(used=31.5), NOW)[0]["pct"], 36)
        self.assertEqual(band.windows(v5(), vw(used=31.5), NOW)[1]["pct"], 31)


class Level(unittest.TestCase):
    """The percent is the last reading's, never lifted by an estimate of the
    work since: a band ahead of Claude Code's own number reads as wrong."""

    def setUp(self):
        utc()

    def test_the_last_reading_while_work_goes_on(self):
        h5, _ = band.windows(v5(used=36.6, rate=60, known_ts=NOW - 120), vw(), NOW)
        self.assertEqual((h5["pct"], h5["used"]), (36, 36.6))
        self.assertNotIn("approx", h5)
        self.assertTrue(h5["sentence"].startswith("5-hour: 36% used, resets 16:30."), h5["sentence"])

    def test_an_ended_window_reads_not_started(self):
        # The morning after: the last 5 hours ended at night, the next one
        # opens with the next message; the band said "waiting" until then.
        h5, wk = band.windows({"present": False, "ended_ts": MON + 3 * H}, vw(), NOW)
        self.assertEqual((h5["pct"], h5["kind"], words(h5["phrase"]), h5["phrase"]["attention"], h5["reset"]),
                         (0, "idle", "Not started", False, None))
        self.assertEqual(h5["sentence"], "5-hour: the last window ended at 03:00; a new one starts with your next "
                                         "message.")
        h5, _ = band.windows({"present": False, "ended_ts": MON - 2 * D}, vw(), NOW)
        self.assertTrue(h5["sentence"].startswith("5-hour: the last window ended Sat 00:00;"), h5["sentence"])
        self.assertEqual(render.plain(band.segments([h5, wk], config.load(os.devnull))),
                         "5h ──────── 0% Not started · Week ━━━━╍╍╍╍ 48% On pace  resets Fri")


class TerminalLine(unittest.TestCase):
    def setUp(self):
        utc()
        self.cfg = config.load(os.devnull)

    def line(self, state, width=None, cfg=None):
        return render.plain(band.segments(band.windows(*STATES[state], NOW), cfg or self.cfg, width))

    def test_reference_lines(self):
        self.assertEqual(self.line("on pace"), "5h ━━━━╍╍╍╍ 55% On pace  resets 16:30 · Week ━━━━╍╍╍╍ 48% On pace  "
                                               "resets Fri")
        self.assertEqual(self.line("on pace", 60), "5h ━━━━╍╍╍╍ 55% On pace · Week ━━━━╍╍╍╍ 48% On pace")
        self.assertEqual(self.line("runs out"), "5h ━━━━━━╍╍ 70% 2x slower  resets 16:30 · Week ━━━━╍╍╍╍ 48% On pace  "
                                                "resets Fri")
        self.assertEqual(self.line("early", 60), "5h ━╍╍╍╍─── 8% ~2x faster · Week ━╍╍───── 2% 2.7x faster")
        # Short of room the green words go, the second window's first; what needs you stays.
        self.assertEqual(self.line("unused", 50), "5h ━╍─── 12% 1.7x faster · Wk ━━╍┈┈ 40%")
        self.assertEqual(self.line("maxed", 40), "5h ━━━━━ 100% Back 16:30 · Wk ━━━╍╍ 52%")
        self.assertEqual(self.line("runs out", 40), "5h ━━━━╍ 70% 2x slower · Week ━━╍╍╍ 48%")
        self.assertEqual(self.line("week dry", 40), "5h ━━━╍╍ 55% · Wk ━━━━╍ 80% 2.5x slower")
        self.assertEqual(self.line("half"), "5h ━━━━╍╍╍╍ 55% On pace  resets 16:30 · Week waiting")

    def test_tones(self):
        segs = band.segments(band.windows(*STATES["runs out"], NOW), self.cfg)
        tones = dict((t, tone) for t, tone in segs)
        self.assertEqual(tones[" 2x slower"], "error")
        self.assertEqual(tones["48% On pace  resets Fri"], "inactive")  # dims beside one that needs you
        self.assertEqual(tones["70%"], "text")
        self.assertIn("\033[1;31m 2x slower", render.to_ansi(segs, True))
        tones = dict((t, tone) for t, tone in band.segments(band.windows(*STATES["unused"], NOW), self.cfg))
        self.assertEqual(tones[" 1.7x faster"], "success")

    def test_every_width_fits_and_keeps_what_needs_you(self):
        for symbols in ("unicode", "ascii"):
            cfg = config.load(os.devnull)
            cfg["display"]["symbols"] = symbols
            for state in STATES:
                for width in range(20, 141):
                    line = self.line(state, width, cfg)
                    self.assertLessEqual(len(line), width, (symbols, state, width, line))
                    if symbols == "ascii":
                        self.assertTrue(all(ord(c) < 128 for c in line), line)
                    if width >= 40 and state in ("runs out", "maxed", "week dry"):
                        h5, wk = band.windows(*STATES[state], NOW)
                        p = (h5 if h5["phrase"]["attention"] else wk)["phrase"]
                        self.assertTrue(p["pre"] + p["num"] in line or p["narrow_pre"] + p["num"] in line,
                                        (state, width, line))

    def test_classic_style_is_kept(self):
        self.cfg["display"]["style"] = "classic"
        line = render.plain(band.line(*STATES["on pace"], self.cfg, NOW))
        self.assertTrue(line.startswith("5h 55% ×1.1 →95% @16:30 · wk 48% +1"), line)
        self.cfg["display"]["style"] = "fancy"
        self.assertEqual(config.normalize(self.cfg)["display"]["style"], "bars")


class Stamp(TempHome):
    """A reading that changes what is shown touches GLIDEPATH_HOME/changed, so
    a band in another chat redraws within seconds; a heartbeat does not."""

    def render(self, now, five, week=(30, int(MON + 5 * D)), sid="s1"):
        raw = json.dumps(payload(five, week, sid)).encode()
        opts = statusline.parse_args(["--source", "mod", "--format", "json"])
        return statusline.compute(raw, self.cfg, opts, now)

    def stamp(self):
        try:
            with open(config.stamp_path(), encoding="utf-8") as f:
                return float(f.read())
        except OSError:
            return None

    def test_fresh_readings_touch_it_and_heartbeats_do_not(self):
        r5 = int(MON + 4 * H)
        self.assertIsNone(self.stamp())
        self.render(MON + H, (10, r5))
        self.assertEqual(self.stamp(), MON + H)
        self.render(MON + H + 20, (10, r5))  # the same reading again: a heartbeat
        self.assertEqual(self.stamp(), MON + H)
        self.render(MON + H + 40, (11, r5))
        self.assertEqual(self.stamp(), MON + H + 40)

    def test_the_view_knows_when_the_level_was_last_read(self):
        r5 = int(MON + 4 * H)
        self.known("s1", "s2")
        for k in range(8):
            self.render(MON + H + 60 * k, (10 + k, r5))
        self.render(MON + H + 485, (17, r5), sid="s2")  # another chat reads the same level later
        self.render(MON + H + 495, (17, r5))  # a heartbeat: not a new reading
        v5_, vw_, _ = self.render(MON + H + 500, (17, r5))
        self.assertEqual(v5_["known_ts"], MON + H + 485)
        self.assertEqual(band.updated(v5_, vw_), MON + H + 485)


if __name__ == "__main__":
    unittest.main()


class EndedWindow(TempHome):
    def test_the_store_says_when_the_last_window_ended(self):
        from glidepath import pace

        r5 = int(MON + 4 * H)
        self.known("s1")
        self.feed(MON + H, five=(30, r5), week=(20, int(MON + 5 * D)))
        self.assertNotIn("ended_ts", pace.five_hour(self.st, MON + 2 * H, self.cfg))
        v = pace.five_hour(self.st, MON + 6 * H, self.cfg)
        self.assertEqual((v["present"], v["ended_ts"]), (False, r5))
        self.assertEqual(band.window(v, MON + 6 * H)["phrase"]["pre"], "Not started")


class RunsOutLater(unittest.TestCase):
    def setUp(self):
        utc()

    def rows(self, v):
        return {r["label"]: r for r in band.details(v, NOW)}

    def test_a_window_that_lasts_says_when_it_would_run_out(self):
        # The reset, and when it would reach 100% at this pace as its note.
        r = self.rows(v5(empty_ts=NOW + 2 * H + 38 * 60))["Resets"]  # 17:18
        self.assertEqual((r["value"], r["note"], r["note_tone"]), ("16:30", "100% 17:18", "inactive"))
        r = self.rows(vw(empty_ts=MON + 5 * D + 6 * H))["Resets"]
        self.assertEqual((r["value"], r["note"]), ("Fri 04:00", "100% Sat 06:00"))
        self.assertNotIn("note", self.rows(vw(empty_ts=NOW + 9 * D))["Resets"])  # over a week away
        # A shaky run-out (D-37), before the reset: maybe.
        self.assertEqual(self.rows(vw(empty_ts=MON + 3 * D, shaky=True))["Resets"]["note"], "maybe 100% Thu 00:00")
        # Idle or without a pace: no time.
        self.assertNotIn("note", self.rows(v5(verdict="idle", empty_ts=NOW + 9 * H))["Resets"])
        self.assertNotIn("note", self.rows(v5(verdict="unknown", empty_ts=NOW + 9 * H))["Resets"])



class RunsOutLaterViews(TempHome):
    def test_the_views_have_it(self):
        from glidepath import commands

        self.known("s1")
        r5 = int(MON + 16 * H + 30 * 60)
        for k, u in enumerate((10, 14, 18, 22)):
            self.feed(MON + 12 * H + k * 600, five=(u, r5), week=(30 + k, int(MON + 4 * D)))
        now = MON + 12 * H + 1800
        a, b, _ = commands.views(self.st, self.cfg, now)
        self.assertAlmostEqual(a["empty_ts"], now + (100 - a["used"]) / a["rate"] * 3600, delta=1)
        self.assertGreater(b["empty_ts"], now)
