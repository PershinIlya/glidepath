"""Installs made under the earlier names, pacer and ctempo (glidepath/legacy.py)."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from glidepath import config, install, legacy, menubar

from .helpers import TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACER, CTEMPO = legacy.OLD


class Legacy(TempHome):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.dir, "u")
        os.makedirs(self.home)
        self.env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        self.env.start()
        for k in (config.ENV,) + config.OLD_ENVS:
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_an_old_folder_is_used_until_it_moves(self):
        new = os.path.join(self.home, ".glidepath")
        for old in (PACER, CTEMPO):
            with self.subTest(old.name):
                folder = os.path.join(self.home, old.dir)
                self.assertEqual(config.home(), new)  # nothing yet: the new name
                os.makedirs(folder)
                self.assertEqual(config.home(), folder)
                with mock.patch.dict(os.environ, {old.env: "elsewhere"}):
                    self.assertEqual(config.home(), os.path.join(self.home, "elsewhere"))
                    self.assertFalse(legacy.move_home(lambda *_: None))  # a folder named by the user stays
                self.assertEqual(legacy.pending_move(), (old, folder, new))  # what install's plan shows
                lines = []
                self.assertTrue(legacy.move_home(lines.append))
                self.assertTrue(os.path.isdir(new) and not os.path.exists(folder))
                self.assertEqual(config.home(), new)
                self.assertIn("glidepath was called %s" % old.name, lines[0])
                self.assertFalse(legacy.move_home(lines.append))  # once
                self.assertIsNone(legacy.pending_move())
                os.rmdir(new)

    def test_the_newer_old_folder_wins(self):
        for old in (PACER, CTEMPO):
            os.makedirs(os.path.join(self.home, old.dir))
        self.assertEqual(config.home(), os.path.join(self.home, ".pacer"))
        self.assertTrue(legacy.move_home(lambda *_: None))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude-tempo")))  # left as it is
        self.assertEqual(config.home(), os.path.join(self.home, ".glidepath"))

    def test_the_newest_variable_comes_first(self):
        a, b, c = (os.path.join(self.home, n) for n in "abc")
        with mock.patch.dict(os.environ, {"GLIDEPATH_HOME": a, "PACER_HOME": b, "CTEMPO_HOME": c}):
            self.assertEqual(os.path.normcase(config.home()), os.path.normcase(a))
        with mock.patch.dict(os.environ, {"PACER_HOME": b, "CTEMPO_HOME": c}):
            self.assertEqual(os.path.normcase(config.home()), os.path.normcase(b))

    def test_old_commands_and_backups_are_ours(self):
        for name in ("pacer", "ctempo"):
            self.assertTrue(install.is_ours("/usr/bin/python3 -I -S /repo/bin/%s statusline --source cli" % name))
            self.assertTrue(install.BACKUP_SUFFIX.search("settings.json.%s-backup-20261020-101010Z" % name))
        self.assertTrue(install.BACKUP_SUFFIX.search("settings.json.glidepath-backup-20261020-101010Z-2"))

    def test_an_old_mod_goes_when_it_is_there(self):
        for old in (PACER, CTEMPO):
            calls = []
            self.assertTrue(legacy.remove_old_mod(old, lambda a: calls.append(a) or (0, "", ""), lambda *_: None))
            self.assertEqual(calls, [["plugin", "uninstall", old.plugin_id, "--scope", "user"],
                                     ["plugin", "marketplace", "remove", old.marketplace]])
            calls.clear()
            self.assertFalse(legacy.remove_old_mod(old, lambda a: calls.append(a) or (1, "", "not found"),
                                                   lambda *_: None))
            self.assertEqual(len(calls), 1)  # not installed: the marketplace is left alone

    def test_old_mods_are_looked_for_in_the_user_settings(self):
        us = os.path.join(self.dir, "settings.json")
        with open(us, "w") as f:
            f.write('{"enabledPlugins": {"other@x": true}}')
        self.assertEqual(legacy.old_mods_listed(us), ())
        with open(us, "w") as f:
            f.write('{"enabledPlugins": {"ctempo@claude-tempo": true}}')
        self.assertEqual(legacy.old_mods_listed(us), (CTEMPO,))
        with open(us, "w") as f:
            f.write('{"enabledPlugins": {"pacer@pacer": true}, "extraKnownMarketplaces": {"claude-tempo": {}}}')
        self.assertEqual(legacy.old_mods_listed(us), (PACER, CTEMPO))
        self.assertEqual(legacy.old_mods_listed(os.path.join(self.dir, "none.json")), ())

    def test_swiftbars_old_plugin_is_replaced(self):
        for old in (PACER, CTEMPO):
            with self.subTest(old.name):
                folder = os.path.join(self.dir, "swiftbar-" + old.name)
                os.makedirs(folder)
                path = os.path.join(folder, old.menubar)
                with open(path, "w") as f:
                    f.write("#!/bin/sh\n%s (written by `%s menubar --install`)\n" % (old.mark, old.name))
                install.save_state({"menubar": path})
                with mock.patch("sys.stdout"):
                    self.assertTrue(legacy.move_menubar(lambda *_: None))
                self.assertFalse(os.path.exists(path))
                self.assertTrue(os.path.exists(os.path.join(folder, menubar.PLUGIN)))
                self.assertEqual(install.load_state()["menubar"], os.path.join(folder, menubar.PLUGIN))

    def test_a_plugin_that_is_not_ours_stays(self):
        folder = os.path.join(self.dir, "swiftbar")
        os.makedirs(folder)
        path = os.path.join(folder, "pacer.1m.sh")  # another tool's, by the same name
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho hi\n")
        install.save_state({"menubar": path})
        self.assertFalse(legacy.move_menubar(lambda *_: None))
        self.assertTrue(os.path.exists(path))

    def test_old_launchers_still_run_glidepath(self):
        for name in ("pacer", "ctempo"):
            p = subprocess.run([sys.executable, "-I", "-S", os.path.join(ROOT, "bin", name), "--version"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.stdout.strip().split()[0], "glidepath", p.stderr)


if __name__ == "__main__":
    unittest.main()
