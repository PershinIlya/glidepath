"""Findings of the audit before publishing: each one's regression test."""

import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

from glidepath import band, chart, commands, config, install, menubar, render, usagecmd

from .helpers import MON, H, D, TempHome
from .test_band import NOW, v5, vw


class Which(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX file modes")
    def test_the_current_folder_is_never_searched(self):
        # Windows searches the current folder before PATH: a cloned repo's
        # py.bat or claude.cmd would run. Empty and "." PATH entries go too.
        d = tempfile.mkdtemp(prefix="glidepath-which-")  # on macOS under /var, a link to /private/var
        old = os.getcwd()
        try:
            exe = os.path.join(d, "glidepath-probe")
            with open(exe, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            os.chdir(d)
            for path in (".", "", d, os.pathsep.join(["", "/nonexistent"])):
                self.assertIsNone(config.which("glidepath-probe", path=path), path)
            os.chdir(old)
            self.assertEqual(config.which("glidepath-probe", path=d), exe)
        finally:
            os.chdir(old)

    def test_cmd_never_gets_a_path_it_would_run(self):
        # cmd.exe reads a .cmd's arguments again: "R&D" in a folder ran "D".
        with mock.patch.object(install, "claude_bin", lambda: r"C:\npm\claude.cmd"), \
                mock.patch.object(install, "_run") as run:
            code, _, err = install.run_claude(["plugin", "marketplace", "add", r"C:\Users\x\R&D\repo"])
        self.assertEqual(code, 2)
        self.assertIn("move the folder", err)
        run.assert_not_called()
        with mock.patch.object(install, "claude_bin", lambda: r"C:\x\claude.exe"), \
                mock.patch.object(install, "_run", return_value=(0, "", "")) as run:
            self.assertEqual(install.run_claude(["plugin", "marketplace", "add", r"C:\R&D"])[0], 0)


class UsageParse(unittest.TestCase):
    def test_a_long_odd_line_is_skipped_at_once(self):
        # The pattern backtracked for seconds on long runs of spaces.
        line = "Current session: 1% used x resets " + " " * 2000 + "(a" + " " * 2000 + "z"
        t = time.monotonic()
        self.assertEqual(usagecmd.parse(line, MON), {})
        self.assertLess(time.monotonic() - t, 1.0)
        m = usagecmd.LINE.match("Current session: 26% used · resets Oct 19, 2:59pm (Europe/Berlin)")
        self.assertEqual(m.groups(), ("session", "26", "Oct 19, 2:59pm", "Europe/Berlin"))


class SwiftBarText(unittest.TestCase):
    def test_data_never_starts_parameters_or_lines(self):
        note = "/usage: could not read: Error: x | bash=/usr/bin/open param1=-a\nsecond | line"
        line = [x for x in menubar.lines(band.windows(v5(), vw(), NOW), NOW - 60, NOW, config.load(), note=note)
                if x.startswith("/usage")][0]
        self.assertEqual(line.count(" | "), 1, line)
        self.assertNotIn("\n", line)
        self.assertTrue(line.endswith("| size=11 color=" + menubar.TONE["warning"]))


@unittest.skipIf(sys.platform == "win32", "POSIX file modes")
class Files(TempHome):
    def test_settings_are_written_through_a_new_temp_file(self):
        d = tempfile.mkdtemp(prefix="glidepath-aw-")
        target = os.path.join(d, "settings.json")
        victim = os.path.join(d, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        # A link at the name the old code used.
        os.symlink(victim, "%s.glidepath.%d.tmp" % (target, os.getpid()))
        install.atomic_write(target, "{}\n")
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")
        with open(target) as f:
            self.assertEqual(f.read(), "{}\n")

    def test_the_data_folder_and_install_record_are_private(self):
        home = os.path.join(self.dir, "fresh", "home")
        os.environ["GLIDEPATH_HOME"] = home
        install.save_state({"records": {"x": {"original_statusLine": {"command": "TOKEN=s mybar"}}}})
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(install.state_path()).st_mode), 0o600)


class Forecasts(TempHome):
    def test_day_one_of_a_workweek_is_not_a_red_run_out(self):
        # A new user, Mon-Fri 9-18 at 2 points an hour (the week ends at 90%).
        # The prior was the profile's scale, the busy hours stretched over the
        # week (3.48): a red "Runs out Wed" and a notification on day one.
        R = int(MON + 7 * D)
        self.known("s1")
        now = MON + 18 * H + 15 * 60
        self.work(MON, now, R, 2.0, hours=(9, 18), learn=True)
        _, w, _ = commands.views(self.st, self.cfg, now)
        self.assertEqual(w["prior_source"], "assumed")
        self.assertAlmostEqual(w["intensity_prior"], 1.0)
        self.assertNotEqual(w["severity"], "alert")

    def test_an_idle_week_says_no_run_out(self):
        # 88% by Wednesday, then nothing for 40 hours: "Idle" with a red mark
        # and a yellow "Runs out" from the prior alone.
        R = int(MON + 7 * D)
        self.known("s1")
        for h in range(0, 61, 2):
            self.feed(MON + h * H, week=(int(88 * h / 60), R))
        now = MON + 100 * H
        self.feed(now, week=(88, R))
        _, w, _ = commands.views(self.st, self.cfg, now)
        self.assertEqual(w["verdict"], "idle")
        rows = {r["label"] for r in band.details(w, now)}
        self.assertNotIn("Runs out", rows)
        self.assertIsNone(chart.build(self.st, w, now)["out"])

    def test_a_time_past_the_reset_names_its_day(self):
        # "Resets 15:00 · 100% 02:40" read as tonight; a week on, "Fri" read as tomorrow.
        h5 = band.details(v5(empty_ts=NOW + 12 * H), NOW)  # tomorrow 02:40
        self.assertEqual([r.get("note") for r in h5 if r["label"] == "Resets"], ["100% Tue 02:40"])
        wk = band.details(vw(empty_ts=NOW + 6 * D + 16 * H), NOW)
        self.assertEqual([r.get("note") for r in wk if r["label"] == "Resets"], ["100% next Mon 06:40"])

    def test_both_limits_reached_the_5_hours_wait_for_the_week(self):
        h5, wk = band.windows(v5(used=100, verdict="maxed", severity="alert", projected=100),
                              vw(used=100, verdict="maxed", severity="alert", projected=100), NOW)
        self.assertEqual(h5["phrase"]["pre"] + h5["phrase"]["num"], "Back Fri 04:00")
        self.assertIn("until Fri 04:00", h5["sentence"])
        self.assertEqual([e[1] for e in menubar.events([h5, wk])][0], "5-hour limit reached, back Fri 04:00")

    def test_the_classic_line_shows_no_dry_for_an_early_guess(self):
        v = vw(used=4, verdict="on_pace", multiplier=0.7, dry_ts=MON + 5 * D, shaky=True, delta=3.0)
        text = " ".join(t for t, _ in render.weekly_parts(v, render.SYMBOLS["unicode"]))
        self.assertNotIn("dry", text)
        h = v5(cap_ts=NOW + 3600, shaky=True)
        self.assertNotIn("dry", " ".join(t for t, _ in render.five_hour_parts(h, render.SYMBOLS["unicode"])))


if __name__ == "__main__":
    unittest.main()
