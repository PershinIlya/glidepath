"""Readings from Claude Code's /usage (glidepath/usagecmd.py)."""

import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from glidepath import config, menubar, usagecmd

from .helpers import TempHome, _zone_info

# Claude Code 2.1.302's output (Asia/Dubai, UTC+4, no DST).
OUTPUT = """You are currently using your subscription to power your Claude Code usage

Current session: 11% used · resets Oct 17 at 6:20pm (Asia/Dubai)
Current week (all models): 20% used · resets Oct 23 at 6am (Asia/Dubai)
Current week (Fable): 0% used · resets Oct 23 at 6am (Asia/Dubai)

What's contributing to your limits usage?
Last 24h · 2 requests · 2 sessions
"""
NOW = 1792233000.0  # 2026-10-17 13:10 in Dubai (09:10 UTC)
R5 = 1792246800  # 2026-10-17 18:20 Dubai
R7 = 1792720800  # 2026-10-23 06:00 Dubai


def needs_zones(test):
    return unittest.skipIf(_zone_info("Asia/Dubai") is None, "no zoneinfo or zone database")(test)


class Parse(unittest.TestCase):
    @needs_zones
    def test_the_real_output(self):
        self.assertEqual(usagecmd.parse(OUTPUT, NOW),
                         {config.FIVE_HOUR: (11.0, R5), config.SEVEN_DAY: (20.0, R7)})

    @needs_zones
    def test_the_newer_output(self):
        # The person's Windows machine (Claude Code 2.1.303): a comma, not "at".
        text = OUTPUT.replace("Oct 17 at 6:20pm", "Oct 17, 6:20pm").replace("Oct 23 at 6am", "Oct 23, 6am")
        self.assertEqual(usagecmd.parse(text, NOW), {config.FIVE_HOUR: (11.0, R5), config.SEVEN_DAY: (20.0, R7)})

    @needs_zones
    def test_a_separator_read_in_the_wrong_encoding(self):
        # The person's Windows machine before the fetch read UTF-8: "·" came as "Â·".
        self.assertEqual(usagecmd.parse(OUTPUT.replace("·", "\u00c2\u00b7"), NOW),
                         {config.FIVE_HOUR: (11.0, R5), config.SEVEN_DAY: (20.0, R7)})

    @needs_zones
    def test_times_without_a_date_and_unknown_lines(self):
        self.assertEqual(usagecmd.reset_epoch("6:20pm", "Asia/Dubai", NOW), R5)
        self.assertEqual(usagecmd.reset_epoch("6am", "Asia/Dubai", NOW),
                         usagecmd.reset_epoch("Oct 18 at 6am", "Asia/Dubai", NOW))  # the next 6am
        self.assertIsNone(usagecmd.reset_epoch("tomorrow", "Asia/Dubai", NOW))
        self.assertIsNone(usagecmd.reset_epoch("6am", "Not/AZone", NOW))
        self.assertEqual(usagecmd.parse("Current session: 11% used\nsomething else\n", NOW), {})


class Fetch(TempHome):
    def run_fetch(self, text, rc=0, now=NOW):
        return usagecmd.fetch(now, run=lambda: (text, rc))

    @needs_zones
    def test_records_both_windows_from_its_own_session(self):
        ok, msg = self.run_fetch(OUTPUT)
        self.assertTrue(ok, msg)
        rows = self.st.db.execute("SELECT window, used_pct, resets_at, source, session_id FROM samples ORDER BY window")
        self.assertEqual(rows.fetchall(), [(config.FIVE_HOUR, 11.0, R5, "usage", "usage-cmd"),
                                           (config.SEVEN_DAY, 20.0, R7, "usage", "usage-cmd")])
        self.assertEqual(self.st.meta_get("usage_cmd_ok_at"), NOW)
        self.assertTrue(os.path.exists(config.stamp_path()))  # a new window wakes the bands

    def test_a_format_it_cannot_read_says_which_line(self):
        ok, msg = self.run_fetch("Current session: 18% used · resets in a while (Europe/Berlin)\n")
        self.assertEqual((ok, msg), (False, "could not read the reset time in /usage: Current session: 18% used · "
                                            "resets in a while (Europe/Berlin)"))

    def test_a_failure_records_nothing(self):
        self.assertEqual(self.run_fetch("Error: not logged in", rc=1)[0], False)
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)

    def test_an_api_key_login_says_so(self):
        # The person's Windows claude (2.1.301): /usage printed the session's spend.
        spend = ("Total cost:            $0.0000\nTotal duration (API):  0s\nTotal duration (wall): 1s\n"
                 "Total code changes:    0 lines added, 0 lines removed\n"
                 "Usage:                 0 input, 0 output, 0 cache read, 0 cache write\n")
        ok, msg = self.run_fetch(spend)
        self.assertFalse(ok)
        self.assertIn("shows spend, not plan limits", msg)
        self.assertIn("trust", msg)
        self.assertIn(config.home(), msg)
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)

    def test_due_runs_at_most_every_so_many_minutes(self):
        self.assertFalse(usagecmd.due(self.st, NOW, 0))
        self.assertTrue(usagecmd.due(self.st, NOW, 10))
        self.assertFalse(usagecmd.due(self.st, NOW + 300, 10))
        self.assertTrue(usagecmd.due(self.st, NOW + 601, 10))


class MenuBar(TempHome):
    @needs_zones
    def test_the_menu_bar_fetches_when_asked_and_says_so(self):
        usagecmd.set_every(10)
        cfg = config.load()
        out, real = io.StringIO(), usagecmd.fetch
        with contextlib.redirect_stdout(out), mock.patch("time.time", return_value=NOW + 60), \
                mock.patch.object(usagecmd, "fetch", side_effect=lambda now: real(now, run=lambda: (OUTPUT, 0))):
            menubar.show(cfg)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("11% · 20% | image="), out.getvalue())
        self.assertIn("Updated just now, checked just now | size=11 color=%s" % menubar.DIM, lines)

    def test_off_by_default(self):
        self.assertEqual(config.load()["fetch_usage_min"], 0)
        with mock.patch.object(usagecmd, "fetch") as fetch, contextlib.redirect_stdout(io.StringIO()):
            menubar.show(config.load())
        fetch.assert_not_called()

    def test_set_every_keeps_the_rest_of_the_file(self):
        path = config.config_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"target_5h": 90, "menubar": {"other": 1, "fetch_usage_min": 10}}, f)
        self.assertEqual(config.load(path)["fetch_usage_min"], 10)  # where it was before: still read
        usagecmd.set_every(5)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"target_5h": 90, "menubar": {"other": 1}, "fetch_usage_min": 5})
        usagecmd.set_every(0)
        self.assertEqual(config.load(path)["fetch_usage_min"], 0)


class Background(TempHome):
    """A Desktop band (statusline --source mod) starts `glidepath fetch` in the
    background when one is due, and never waits for it."""

    def test_spawn_when_due_only(self):
        started = []
        popen = lambda cmd, **kw: started.append((cmd, kw))  # noqa: E731
        cfg = dict(self.cfg, fetch_usage_min=10)
        rt = ("/py", ["-I", "-S"], "/repo/bin/glidepath")
        self.assertTrue(usagecmd.spawn(self.st, NOW, cfg, rt, popen))
        self.assertFalse(usagecmd.spawn(self.st, NOW + 300, cfg, rt, popen))  # 5 min later: not yet
        self.assertFalse(usagecmd.spawn(self.st, NOW + 900, dict(cfg, fetch_usage_min=0), rt, popen))
        self.assertEqual(len(started), 1)
        cmd, kw = started[0]
        self.assertEqual(cmd, ["/py", "-I", "-S", "/repo/bin/glidepath", "fetch"])
        self.assertIs(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["env"]["GLIDEPATH_HOME"], config.home())

    def test_the_mod_run_spawns_and_the_terminal_does_not(self):
        from glidepath import statusline

        cfg = dict(self.cfg, fetch_usage_min=10)
        self.st.close()
        with mock.patch.object(usagecmd, "spawn", return_value=True) as spawn:
            for source in ("statusline", "mod"):
                opts = dict(statusline.parse_args([]), source=source)
                statusline.compute("{}", cfg, opts, NOW, lambda a, b: "")
        self.assertEqual(spawn.call_count, 1)  # the mod's run only
        statusline.compute("{}", self.cfg, dict(statusline.parse_args([]), source="mod"), NOW, lambda a, b: "")
        self.assertEqual(spawn.call_count, 1)  # off: not even asked

    def test_a_nested_claude_does_not_see_the_session(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw["env"])
            seen["encoding"] = kw.get("encoding")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        env = dict(os.environ, CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="claude-desktop", CLAUDE_CONFIG_DIR="/x")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(usagecmd, "claude_bin", return_value="claude"), \
                mock.patch("subprocess.run", side_effect=fake_run):
            ok, msg = usagecmd.fetch(NOW)
        self.assertFalse(ok)
        self.assertNotIn("CLAUDECODE", seen)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", seen)
        self.assertEqual(seen.get("CLAUDE_CONFIG_DIR"), "/x")
        self.assertEqual(seen["encoding"], "utf-8")  # whatever the system's code page
