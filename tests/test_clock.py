"""Local time goes through glidepath.clock, so the tests pin the zone on every
OS: time.tzset does not exist on Windows, and the process may run in any
zone. helpers.utc() pins UTC, helpers.zone() an IANA zone."""

import contextlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from glidepath import clock, profile, render, transcripts

from .helpers import MON, H, D, TZ_UTC, utc, zone


class Clock(unittest.TestCase):
    def setUp(self):
        utc()

    def test_the_suite_runs_in_utc_whatever_the_machines_zone(self):
        # MON is Monday 2026-10-12 00:00 UTC.
        self.assertEqual(render.hm(MON), "00:00")
        self.assertEqual(render.dhm(MON + 20 * H), "Mon 20:00")
        self.assertEqual(profile.bin_of(MON + 20 * H), 20)
        self.assertEqual(clock.strftime("%Y-%m-%d", MON), "2026-10-12")
        self.assertEqual(os.environ.get("TZ"), TZ_UTC)  # what child processes start in

    def test_a_zone_moves_the_reset_clock_the_bins_and_the_days(self):
        events = [(MON + 18 * H, 1.0), (MON + 19 * H, 1.0)]  # 23:30 and 00:30 in India
        self.assertEqual(transcripts.build_prior(events, MON - D, MON + 2 * D, 7)[1]["active_days"], 1)
        with zone("Asia/Kolkata"):  # UTC+05:30
            self.assertEqual(render.hm(MON), "05:30")
            self.assertEqual(render.dhm(MON + 20 * H), "Tue 01:30")
            self.assertEqual(profile.bin_of(MON + 20 * H), 24 + 1)
            self.assertEqual(clock.strftime("%Y-%m-%d", MON + 20 * H), "2026-10-13")
            self.assertEqual(transcripts.build_prior(events, MON - D, MON + 2 * D, 7)[1]["active_days"], 2)
        self.assertEqual(render.hm(MON), "00:00")  # and back in UTC after the block


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "bin", "glidepath")
JULY = 1784000000  # 2026-07-14 03:33 UTC: US daylight saving is on


class WindowsTz(unittest.TestCase):
    """Review 15: Windows' C runtime reads TZ only in its own form (3
    letters, then the hours: EST5EDT, UTC0). An IANA name inherited from a
    Cygwin login shell (TZ=Europe/Berlin) read as zone "Eur", offset 0, with
    US daylight saving: every local time glidepath showed was off. bin/glidepath
    drops such a TZ and has the C runtime read the zone again."""

    def drop(self, tz):
        env = {"PATH": "x"} if tz is None else {"PATH": "x", "TZ": tz}
        calls = []
        dropped = clock.windows_tz(env, lambda: calls.append("tzset"))
        return dropped, env, calls

    def test_a_tz_the_c_runtime_misreads_is_dropped_and_the_zone_read_again(self):
        for tz in ("Europe/Berlin", ":Europe/Berlin", "America/New_York", "CET", "UTC", "<+03>-3", "Asia/Kolkata"):
            dropped, env, calls = self.drop(tz)
            self.assertEqual((dropped, env, calls), (True, {"PATH": "x"}, ["tzset"]), tz)

    def test_the_c_runtime_form_and_no_tz_are_left_alone(self):
        for tz in (None, "", TZ_UTC, "EST5EDT", "GST-1GDT", "PST8PDT", "cet-1cest", "IST-5:30"):
            dropped, env, calls = self.drop(tz)
            self.assertEqual((dropped, env.get("TZ"), calls), (False, tz, []), tz)

    def test_a_tzset_that_fails_is_ignored(self):
        def broken():
            raise OSError("no ucrtbase here")

        env = {"TZ": "Europe/Berlin"}
        self.assertTrue(clock.windows_tz(env, broken))
        self.assertEqual(env, {})

    def test_the_launcher_calls_it_on_windows_only(self):
        for name, calls in (("nt", 1), ("posix", 0)):
            out = io.StringIO()
            with mock.patch.object(clock, "windows_tz") as fix, mock.patch.object(os, "name", name), \
                    mock.patch.object(sys, "argv", [ENTRY, "--version"]), \
                    mock.patch.object(sys, "path", list(sys.path)), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runpy.run_path(ENTRY, run_name="__main__")
            self.assertEqual(fix.call_count, calls, name)
            self.assertTrue(out.getvalue().startswith("glidepath "), out.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows' C runtime")
    def test_on_windows_an_iana_tz_reads_as_the_system_zone(self):
        home = tempfile.mkdtemp(prefix="glidepath-tz-")
        self.addCleanup(shutil.rmtree, home, True)
        base = dict(os.environ, GLIDEPATH_HOME=home, NO_COLOR="1")
        base.pop("TZ", None)  # the system's zone (UTC on CI)
        hour = ("import sys; sys.path.insert(0, %r); from glidepath.clock import windows_tz; windows_tz(); "
                "import time; print(time.localtime(%d).tm_hour)" % (ROOT, JULY))
        data = json.dumps({"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": JULY}}})
        seen = {}
        for tz in (None, "Europe/Berlin"):
            env = dict(base, **({"TZ": tz} if tz else {}))
            py = [sys.executable, "-I", "-S", "-X", "utf8"]
            h = subprocess.run(py + ["-c", hour], capture_output=True, env=env, timeout=60)
            line = subprocess.run(py + [ENTRY, "statusline", "--no-record", "--no-compose", "--now", str(JULY - 3600)],
                                  input=data.encode(), capture_output=True, env=env, timeout=60)
            self.assertEqual((h.returncode, line.returncode), (0, 0), (h.stderr, line.stderr))
            seen[tz] = (h.stdout.strip(), line.stdout.decode().strip())
        self.assertIn(" resets ", seen[None][1])
        self.assertEqual(seen["Europe/Berlin"], seen[None])


if __name__ == "__main__":
    unittest.main()
