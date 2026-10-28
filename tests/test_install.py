"""install / uninstall on throwaway settings files."""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from glidepath import config, install, winshell

SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(git status)"], "deny": []},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]},
    "env": {"FOO": "bär"},
}


# Hand-formatted: no serializer writes it back like this, so only a restore
# from the backup's bytes gives it back.
IRREGULAR = '{\n  "model": "opus",\n  "env": {"FOO": "b\u00e4r"},\n    "permissions": {"allow": ["Bash(git status)"], "deny": []}\n}\n\n'
# A user file that lists another plugin: both plugin keys exist before glidepath.
WITH_PLUGINS = ('{\n  "model": "opus",\n  "enabledPlugins": {"other@mkt": true},\n'
                '  "extraKnownMarketplaces": {"mkt": {"source": {"source": "github", "repo": "a/b"}}}\n}\n')


def put(path, text):
    """Write a fixture byte for byte: UTF-8, and no newline translation (Windows would write CRLF)."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def plugin_cli(user):
    """A fake `claude plugin` that edits the user settings file the way the real one does."""
    def run(cmd, env=None, cwd=None):
        text = install.read_text(user) if os.path.exists(user) else ""
        obj = install.parse_json(text)  # a blank file is {}
        if cmd[:2] == ["plugin", "install"]:
            obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
        elif cmd[:3] == ["plugin", "marketplace", "add"]:
            obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
        elif cmd[:2] == ["plugin", "uninstall"]:
            obj.get("enabledPlugins", {}).pop(install.PLUGIN_ID, None)
        elif cmd[:3] == ["plugin", "marketplace", "remove"]:
            obj.get("extraKnownMarketplaces", {}).pop(install.MARKETPLACE, None)
        put(user, json.dumps(obj, indent=2))
        return 0, "ok", ""
    return run


def claude_missing(cmd, env=None, cwd=None):
    return 127, "", "claude not found on PATH"


def claude_hangs(cmd, env=None, cwd=None):
    return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])


def nothing_installed(cmd, env=None, cwd=None):
    """The plugin CLI with nothing to remove: it says so and writes nothing."""
    if cmd[:2] == ["plugin", "uninstall"]:
        return 1, "", 'Plugin "%s" not found in installed plugins' % install.PLUGIN_ID
    return 1, "", "Marketplace '%s' not found" % install.MARKETPLACE


def args(settings, **kw):
    a = dict(apply=False, settings=settings, no_mod=True, python=sys.executable, replace=False, bin_dir=None,
             no_bootstrap=True)
    a.update(kw)
    return argparse.Namespace(**a)


@contextlib.contextmanager
def time_zone(tz):
    """Local time in another time zone (a POSIX TZ value): the wall clock moves, time does not."""
    try:
        with mock.patch.dict(os.environ, {"TZ": tz}):
            time.tzset()
            yield
    finally:
        time.tzset()


class Sandbox(unittest.TestCase):
    """Throwaway settings files, data folder and home; no tests of its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-inst-")
        self.home = os.path.join(self.dir, "home")
        # Hermetic: no code path (old or new) can reach the real ~/.claude (Windows reads USERPROFILE).
        self.env = mock.patch.dict(os.environ, {"GLIDEPATH_HOME": self.home, "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "claude"),
                                                "HOME": os.path.join(self.dir, "userhome"),
                                                "USERPROFILE": os.path.join(self.dir, "userhome")})
        self.env.start()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, obj, indent=2, newline=False):
        text = json.dumps(obj, indent=indent, ensure_ascii=False) + ("\n" if newline else "")
        put(self.path, text)
        return text

    def read(self):
        return install.read_text(self.path)

    def run_cmd(self, fn, a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fn(a)
        return rc, buf.getvalue()

    def _reset(self):
        """A clean slate between subtests: no install.json, no backups."""
        shutil.rmtree(self.home, ignore_errors=True)
        for n in os.listdir(self.dir):
            if ".glidepath-backup-" in n:
                os.remove(os.path.join(self.dir, n))


class Install(Sandbox):
    def test_dry_run_writes_nothing(self):
        before = self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path))
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn('+  "statusLine"', out)
        self.assertEqual(self.read(), before)
        self.assertFalse(os.path.exists(self.home))

    def test_apply_then_uninstall_is_byte_identical(self):
        for indent, newline in ((2, False), (2, True), (4, True), ("\t", False)):
            with self.subTest(indent=indent, newline=newline):
                before = self.write(SETTINGS, indent, newline)
                rc, out = self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual({k: v for k, v in after.items() if k != "statusLine"}, SETTINGS)
                self.assertTrue(install.is_ours(after["statusLine"]["command"]))
                self.assertEqual(after["statusLine"]["refreshInterval"], 60)
                backups = [f for f in os.listdir(self.dir) if ".glidepath-backup-" in f]
                self.assertTrue(backups)
                self.assertEqual(install.read_text(os.path.join(self.dir, backups[-1])), before)
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.read(), before)
                for b in backups:
                    os.remove(os.path.join(self.dir, b))
                shutil.rmtree(self.home)

    def test_compose_with_existing_statusline_and_restore(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/my line.sh", "padding": 1, "refreshInterval": 5})
        before = self.write(s, newline=True)
        self.run_cmd(install.install, args(self.path, apply=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertEqual(install.compose_of(sl["command"]), "~/.claude/my line.sh")
        self.assertEqual(sl["padding"], 1)
        self.assertEqual(sl["refreshInterval"], 5)  # theirs was faster: keep it
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_replace_flag(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "theirs"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True, replace=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertIsNone(install.compose_of(sl["command"]))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_idempotent(self):
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        once = self.read()
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertIn("already runs glidepath", out)
        self.assertEqual(self.read(), once)
        backups = [f for f in os.listdir(self.dir) if ".glidepath-backup-" in f]
        self.assertEqual(len(backups), 1)

    def test_other_changes_after_install_survive_uninstall(self):
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        obj = json.loads(self.read())
        obj["theme"] = "dark"
        put(self.path, json.dumps(obj, indent=2))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        after = json.loads(self.read())
        self.assertEqual(after, dict(SETTINGS, theme="dark"))

    def test_missing_file_is_created_and_removed(self):
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertIn("statusLine", json.loads(self.read()))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(self.path))

    def test_refuses_invalid_json(self):
        put(self.path, "{ nope")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(rc, 1)
        self.assertIn("refusing", out)
        self.assertEqual(self.read(), "{ nope")

    def test_symlinked_settings_keep_the_link(self):
        real = os.path.join(self.dir, "dotfiles-settings.json")
        put(real, json.dumps(SETTINGS, indent=2))
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertTrue(os.path.islink(self.path))
        self.assertIn("statusLine", json.loads(install.read_text(real)))

    def test_plugin_cli_keys_are_cleaned_up(self):
        """Simulate `claude plugin` adding enabledPlugins / extraKnownMarketplaces
        on install and leaving them empty on uninstall."""
        before = self.write(SETTINGS)
        calls = []

        def fake(cmd, env=None, cwd=None):
            calls.append(cmd)
            obj = json.loads(self.read())
            if cmd[:2] == ["plugin", "install"]:
                obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
            elif cmd[:3] == ["plugin", "marketplace", "add"]:
                obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {"source": "directory", "path": cmd[3]}}
            elif cmd[:2] == ["plugin", "uninstall"]:
                obj["enabledPlugins"].pop(install.PLUGIN_ID)
            elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                obj["extraKnownMarketplaces"].pop(install.MARKETPLACE)
            put(self.path, json.dumps(obj, indent=2))
            return 0, "ok", ""

        with mock.patch.object(install, "run_claude", fake), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))
            self.run_cmd(install.uninstall, args(self.path, apply=True, no_mod=False))
        self.assertEqual(self.read(), before)
        self.assertEqual([c[:3] for c in calls], [["plugin", "marketplace", "add"], ["plugin", "install", install.PLUGIN_ID],
                                                    ["plugin", "uninstall", install.PLUGIN_ID], ["plugin", "marketplace", "remove"]])

    def test_launcher(self):
        self.write(SETTINGS)
        bindir = os.path.join(self.dir, "bin")
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=bindir))
        lp = os.path.join(bindir, install.launcher_name())
        self.assertTrue(os.access(lp, os.X_OK))
        self.assertEqual(install.read_text(lp), install.launcher_text(sys.executable, install.entry_path()))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(lp))

    def test_launchers_of_this_system(self):
        # Review 15 (W-4): on Windows glidepath.cmd and, for Git Bash, an sh glidepath; elsewhere glidepath alone.
        self.write(SETTINGS)
        bindir = os.path.join(self.dir, "bin")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=bindir))
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(bindir)), ["glidepath", "glidepath.cmd"] if config.IS_WINDOWS else ["glidepath"])
        self.assertEqual("sh_launcher" in install.load_state(), config.IS_WINDOWS)
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(bindir), [])

    def alt_python(self):
        """Another absolute path to the same interpreter: forces a repin. A symlink would not
        do on Windows, where python.exe finds its DLLs and library beside itself."""
        return os.path.join(os.path.dirname(sys.executable), ".", os.path.basename(sys.executable))

    # -- review findings ---------------------------------------------------------------
    def test_reinstall_after_user_changed_statusline_restores_the_new_one(self):
        before = self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        obj = json.loads(self.read())
        obj["statusLine"] = {"type": "command", "command": "~/.claude/statusline-command.sh"}
        changed = json.dumps(obj, indent=2)
        put(self.path, changed)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(install.compose_of(json.loads(self.read())["statusLine"]["command"]), "~/.claude/statusline-command.sh")
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), changed)
        self.assertNotEqual(self.read(), before)

    def test_lost_install_record_never_restores_glidepath_itself(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/theirs.sh"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True))
        shutil.rmtree(self.home)  # install.json gone
        self.run_cmd(install.install, args(self.path, apply=True, python=self.alt_python()))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        sl = json.loads(self.read())["statusLine"]
        self.assertFalse(install.is_ours(sl["command"]))
        self.assertEqual(sl["command"], "~/bin/theirs.sh")
        self.assertEqual(self.read(), before)

    def test_records_are_per_settings_file(self):
        a = self.path
        b = os.path.join(self.dir, "project-settings.json")
        sa = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/my-line.sh", "padding": 0})
        before_a = self.write(sa)
        put(b, json.dumps({"model": "sonnet"}, indent=2))
        self.run_cmd(install.install, args(a, apply=True))
        self.run_cmd(install.install, args(b, apply=True))
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 1)
        self.assertIn("several settings files", out)
        self.run_cmd(install.uninstall, args(b, apply=True))
        self.assertEqual(json.loads(install.read_text(b)), {"model": "sonnet"})
        self.run_cmd(install.uninstall, args(a, apply=True))
        self.assertEqual(self.read(), before_a)

    def test_crash_after_write_leaves_a_usable_record(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/theirs.sh", "padding": 2})
        before = self.write(s)

        def boom(cmd, env=None, cwd=None):
            raise RuntimeError("claude died")

        with mock.patch.object(install, "run_claude", boom), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            with self.assertRaises(RuntimeError):
                self.run_cmd(install.install, args(self.path, apply=True, replace=True, no_mod=False))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_run_claude_reports_instead_of_raising(self):
        with mock.patch.object(install, "claude_bin", lambda: "/nonexistent/claude"):
            code, so, se = install.run_claude(["plugin", "list"])
        self.assertEqual(code, 127)

    def test_two_installs_in_one_second_keep_the_pristine_backup(self):
        before = self.write({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}, indent=None)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.install, args(self.path, apply=True, python=self.alt_python()))
        backups = sorted(f for f in os.listdir(self.dir) if ".glidepath-backup-" in f)
        self.assertEqual(len(backups), 2)
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_relative_settings_path_and_missing_file(self):
        self.write(SETTINGS)
        cwd = os.getcwd()
        try:
            os.chdir(self.dir)
            self.run_cmd(install.install, args("settings.json", apply=True))
            other = os.path.join(self.dir, "other")
            os.makedirs(other)
            os.chdir(other)
            self.run_cmd(install.uninstall, args(None, apply=True))
            self.assertFalse(os.path.exists(os.path.join(other, "settings.json")))
            self.assertEqual(json.loads(self.read()), SETTINGS)
            rc, out = self.run_cmd(install.uninstall, args(os.path.join(other, "nope.json"), apply=True))
            self.assertFalse(os.path.exists(os.path.join(other, "nope.json")))
        finally:
            os.chdir(cwd)

    def test_replace_after_compose(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "sleep 5; echo slow"})
        before = self.write(s)
        self.run_cmd(install.install, args(self.path, apply=True))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
        self.assertIn("REPLACE", out)
        self.assertIsNone(install.compose_of(json.loads(self.read())["statusLine"]["command"]))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(self.read(), before)

    def test_crlf_and_mode_survive(self):
        raw = b'{\r\n  "model": "opus"\r\n}\r\n'
        with open(self.path, "wb") as f:
            f.write(raw)
        os.chmod(self.path, 0o600)
        self.run_cmd(install.install, args(self.path, apply=True))
        with open(self.path, "rb") as f:
            mid = f.read()
        self.assertNotIn(b"\n", mid.replace(b"\r\n", b""))  # every newline still CRLF
        if os.name == "posix":  # Windows keeps only a read-only bit
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    # -- second review ---------------------------------------------------------------
    def test_v1_record_with_relative_path_is_recovered(self):
        s = dict(SETTINGS, statusLine={"type": "command", "command": "echo mine"})
        before = self.write(s)
        backup = os.path.realpath(self.path) + ".glidepath-backup-20261014-090000"
        put(backup, before)
        obj = dict(SETTINGS, statusLine={"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
                                         "refreshInterval": 60})
        put(self.path, json.dumps(obj, indent=2))
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what version 1 wrote for `--settings settings.json`
            json.dump({"installed": True, "settings_path": "settings.json", "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(before), "had_statusLine": True,
                       "original_statusLine": s["statusLine"], "had_keys": {k: False for k in install.PLUGIN_KEYS}}, f)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)

    def test_failed_plugin_uninstall_keeps_the_mod_for_a_retry(self):
        before = self.write(SETTINGS)
        self.env2 = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})
        self.env2.start()
        try:
            def ok(cmd, env=None, cwd=None):
                obj = json.loads(self.read())
                if cmd[:2] == ["plugin", "install"]:
                    obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
                elif cmd[:3] == ["plugin", "marketplace", "add"]:
                    obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
                elif cmd[:2] == ["plugin", "uninstall"]:
                    obj["enabledPlugins"].pop(install.PLUGIN_ID, None)
                elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                    obj["extraKnownMarketplaces"].pop(install.MARKETPLACE, None)
                put(self.path, json.dumps(obj, indent=2))
                return 0, "ok", ""

            with mock.patch.object(install, "run_claude", ok), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
                self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            with mock.patch.object(install, "run_claude", lambda c, env=None, cwd=None: (127, "", "claude not found on PATH")):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 1)
            self.assertIn("may still be installed", out)
            self.assertTrue(install.load_state().get("mod"))
            self.assertTrue(os.path.exists(os.path.join(self.home, "runtime.json")))
            with mock.patch.object(install, "run_claude", ok):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.read(), before)
        finally:
            self.env2.stop()

    def test_plugin_keys_are_tidied_in_the_user_file_when_it_went_first(self):
        user = self.path  # CLAUDE_CONFIG_DIR/settings.json: the file the plugin CLI edits
        proj = os.path.join(self.dir, "proj-settings.json")
        before_user = self.write(SETTINGS)
        put(proj, json.dumps({"model": "sonnet"}, indent=2))

        def fake(cmd, env=None, cwd=None):
            obj = json.loads(install.read_text(user))
            if cmd[:2] == ["plugin", "install"]:
                obj.setdefault("enabledPlugins", {})[install.PLUGIN_ID] = True
            elif cmd[:3] == ["plugin", "marketplace", "add"]:
                obj.setdefault("extraKnownMarketplaces", {})[install.MARKETPLACE] = {"source": {}}
            elif cmd[:2] == ["plugin", "uninstall"]:
                obj["enabledPlugins"].pop(install.PLUGIN_ID, None)
            elif cmd[:3] == ["plugin", "marketplace", "remove"]:
                obj["extraKnownMarketplaces"].pop(install.MARKETPLACE, None)
            put(user, json.dumps(obj, indent=2))
            return 0, "ok", ""

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}), \
                mock.patch.object(install, "run_claude", fake), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            self.run_cmd(install.install, args(user, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(user, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(json.loads(install.read_text(user)), SETTINGS)
        self.assertEqual(install.read_text(user), before_user)
        self.assertEqual(json.loads(install.read_text(proj)), {"model": "sonnet"})

    def _user_file(self, text):
        if text is None:  # no user file
            if os.path.exists(self.path):
                os.remove(self.path)
        else:
            put(self.path, text)
        return mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})  # self.path is the user file

    def test_retry_after_failed_plugin_uninstall_is_byte_identical(self):
        # Review #4: the partial run must not drop the record the retry needs.
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"):
            with mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            with mock.patch.object(install, "run_claude", lambda c, env=None, cwd=None: (127, "", "claude not found")):
                rc, _ = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
            self.assertEqual(rc, 1)
            with mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.load_state().get("records"), {})

    def test_user_file_uninstalled_first_comes_back_byte_identical(self):
        # Review #5: the user file goes first, the mod stays for a project file.
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.uninstall, args(self.path, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))  # the mod is still in use
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        self.assertEqual(install.load_state().get("records"), {})

    def test_user_file_touched_only_by_the_plugin_cli_comes_back_byte_identical(self):
        # Review #5: no statusLine there, so no record; the baseline is the
        # snapshot taken before the plugin CLI first wrote to it.
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.assertIn("enabledPlugins", json.loads(self.read()))
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), IRREGULAR)

    def test_two_records_of_one_file_both_go(self):
        # Review #6: a version-1 record and a later one under another name.
        real = os.path.join(self.dir, "dot-settings.json")
        put(real, IRREGULAR)
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(real, apply=True))
        st = install.load_state()
        (k, rec), = st["records"].items()
        st["records"][self.path] = dict(rec, settings_path=self.path)
        install.save_state(st)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.load_state().get("records"), {})
        self.assertEqual(install.read_text(real), IRREGULAR)

    def test_symlink_alias_is_one_record(self):
        real = os.path.join(self.dir, "dot-settings.json")
        put(real, json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": "echo X"}), indent=4))
        before = install.read_text(real)
        os.symlink(real, self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.install, args(real, apply=True))
        self.assertEqual(len(install.installed_records(install.load_state())), 1)
        rc, out = self.run_cmd(install.uninstall, args(None, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(real), before)

    def test_reinstall_after_file_deleted_leaves_no_file(self):
        self.write({"model": "opus"})
        self.run_cmd(install.install, args(self.path, apply=True))
        os.remove(self.path)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertFalse(os.path.exists(self.path))

    # -- fourth review ---------------------------------------------------------------
    def _proj(self):
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        return proj

    def _user_text(self):
        return install.read_text(self.path) if os.path.exists(self.path) else None

    def _steps(self, proj, *steps):
        """Run i(nstall) / u(ninstall) with the mod on the U(ser) file, the P(roject)
        file or * (no --settings); "-nomod" adds --no-mod, "-fail" makes claude exit 127,
        "-hang" makes it time out (124), "-none" makes it find nothing to remove, "-nobin"
        takes claude off PATH. "mod-by-hand" / "unmod-by-hand": the user runs the plugin
        commands (no exit code is recorded for those)."""
        rcs = []
        for s in steps:
            if s.endswith("-by-hand"):
                cli = plugin_cli(self.path)
                for c in install.mod_commands(install.repo_root()) if s == "mod-by-hand" else install.unmod_commands():
                    cli(c)
                continue
            fn = install.install if s[0] == "i" else install.uninstall
            target = {"U": self.path, "P": proj, "*": None}[s[1]]
            cli = {"-fail": claude_missing, "-nobin": claude_missing, "-hang": claude_hangs,
                   "-none": nothing_installed}.get(s[2:]) or plugin_cli(self.path)
            exe = None if s.endswith("-nobin") else "/bin/claude"
            with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli):
                rc, out = self.run_cmd(fn, args(target, apply=True, no_mod=s.endswith("-nomod")))
            rcs.append(rc)
        return rcs

    def test_user_file_without_a_record_of_its_own_comes_back_from_the_mod_snapshot(self):
        # Review 4 (install#0): a retry without --settings after a failed project
        # uninstall, a full uninstall after `--no-mod`, and a stray uninstall of the
        # user file all reach the user file with no record of its own. Its baseline
        # is the snapshot taken before the plugin CLI first wrote there.
        proj = self._proj()
        for steps in (("iP", "uP-fail", "u*"), ("iP", "uP-nomod", "u*"), ("iP", "uU", "u*")):
            for before in (IRREGULAR, None):
                with self.subTest(steps=steps, before=before):
                    self._reset()
                    with self._user_file(before):
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs[-1], 0)
                    self.assertEqual(self._user_text(), before)  # None: the file the plugin CLI created is gone
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    self.assertEqual(install.load_state().get("records"), {})

    def test_installing_into_the_user_file_after_the_mod_keeps_the_pristine_baseline(self):
        # Review 4 (install#1): the user file already holds the plugin CLI's keys, so
        # its baseline is its kept record, else the mod's snapshot, not the file as it is.
        proj = self._proj()
        for steps in (("iP", "iU", "uU", "uP"), ("iP", "iU", "uP", "uU"),
                      ("iU", "uU-nomod", "iU", "uU"), ("iU", "uU-fail", "iU", "uU")):
            for before in (IRREGULAR, None):
                with self.subTest(steps=steps, before=before):
                    self._reset()
                    with self._user_file(before):
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs[-1], 0)
                    self.assertEqual(self._user_text(), before)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    self.assertEqual(install.load_state().get("records"), {})

    def test_command_edited_inside_compose_is_the_one_uninstall_restores(self):
        # Review 4 (install#2): settings.json shows what runs (D-17), so a command the
        # user put in --compose is the original now, also after a reinstall or --replace.
        s = dict(SETTINGS, statusLine={"type": "command", "command": "~/.claude/statusline-old.sh", "padding": 0})
        newer = "npx -y ccstatusline@latest"
        for then in (None, {}, {"replace": True}):
            with self.subTest(then=then):
                self._reset()
                self.write(s)
                self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                put(self.path, json.dumps(obj, indent=2, ensure_ascii=False))
                if then is not None:
                    rc, out = self.run_cmd(install.install, args(self.path, apply=True, **then))
                    self.assertIn("REPLACE" if then else "already runs glidepath", out)
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine"), {"type": "command", "command": newer, "padding": 0})
                self.assertEqual(after, SETTINGS)

    def test_plugin_commands_run_by_hand_are_undone_by_uninstall(self):
        # Review 4 (install#3): claude was not on PATH at install, so the user ran the
        # plugin commands. Uninstall undoes them, and keeps the mod record and
        # runtime.json (exit 1) while claude is still missing.
        cli = plugin_cli(self.path)
        calls = []

        def recording(cmd, env=None, cwd=None):
            calls.append(cmd)
            return cli(cmd)

        runtime = os.path.join(self.home, "runtime.json")
        with self._user_file(IRREGULAR):
            with mock.patch.object(install, "claude_bin", lambda: None), mock.patch.object(install, "run_claude", claude_missing):
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
                self.assertIn("run these yourself", out)
                for c in install.mod_commands(install.repo_root()):
                    cli(c)  # the user, by hand
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 1, out)
                self.assertIn("may still be installed", out)
                self.assertTrue(os.path.exists(runtime))
            with mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), mock.patch.object(install, "run_claude", recording):
                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, install.unmod_commands())
        self.assertEqual(self.read(), IRREGULAR)
        self.assertFalse(os.path.exists(runtime))
        self.assertNotIn("mod", install.load_state())

    def test_plugin_commands_never_run_by_hand_do_not_block_uninstall(self):
        # Review 4 (install#3): claude still missing and no trace of the mod in the
        # user file: the printed commands never ran, so there is nothing to undo.
        proj = self._proj()
        for target in (self.path, proj):
            with self.subTest(target=target):
                self._reset()
                with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: None), \
                        mock.patch.object(install, "run_claude", claude_missing):
                    self.run_cmd(install.install, args(target, no_mod=False))
                    self.assertFalse(os.path.exists(self.home))  # the dry run writes nothing
                    self.run_cmd(install.install, args(target, apply=True, no_mod=False))
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 0, out)
                self.assertIn("Uninstalled.", out)
                self.assertEqual(self.read(), IRREGULAR)
                self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                state = install.load_state()
                self.assertEqual(state.get("records"), {})
                self.assertNotIn("mod", state)
                self.assertFalse(os.path.exists(os.path.join(self.home, "runtime.json")))

    def test_blank_settings_file_comes_back_byte_identical(self):
        # Review 4 (install#4): install reads a blank file as {}; restore must read
        # its backup the same way, or the file comes back as "{}".
        for text in ("", "\n", "  \n", "\r\n"):
            for mod in (False, True):
                with self.subTest(text=text, mod=mod):
                    self._reset()
                    with self._user_file(text):
                        rcs = self._steps(None, "iU" + ("" if mod else "-nomod"), "u*" + ("" if mod else "-nomod"))
                    self.assertEqual(rcs, [0, 0])
                    with open(self.path, "rb") as f:
                        self.assertEqual(f.read(), text.encode("utf-8"))

    def test_version_1_uninstalled_state_still_gets_a_user_file_snapshot(self):
        # Review 4 (install#5): version 1's uninstall left installed false and a "mod"
        # entry with no user-file snapshot. The spent record must not be the user
        # file's baseline, and the entry must not stop install from taking the snapshot.
        proj = self._proj()
        v1_text = '{"model": "opus"}\n'  # the user file when version 1 installed; edited since
        backup = os.path.realpath(self.path) + ".glidepath-backup-20261001-090000"
        put(backup, v1_text)
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what c016f98's uninstall left
            json.dump({"installed": False, "settings_path": self.path, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(v1_text), "had_statusLine": False, "original_statusLine": None,
                       "had_keys": {k: False for k in install.PLUGIN_KEYS}, "python": sys.executable,
                       "entry": install.entry_path(), "written_statusLine": None,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)
        with self._user_file(IRREGULAR):
            rcs = self._steps(proj, "iP", "uP")
        self.assertEqual(rcs, [0, 0])
        self.assertEqual(self.read(), IRREGULAR)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    # -- fifth review ----------------------------------------------------------------
    def test_command_put_in_compose_with_no_statusline_before_is_restored(self):
        # Review 5 (D-install#0): with no statusLine (or no file) before glidepath, a command
        # the user put in --compose is the original too, also after a repin or --replace
        # (which must record it first). The refreshInterval 60 glidepath added goes with it.
        newer = "npx -y ccstatusline@latest"
        for before in (SETTINGS, None):
            for then in (None, {}, {"replace": True}):
                with self.subTest(before=before is not None, then=then):
                    self._reset()
                    if before is None:
                        if os.path.exists(self.path):
                            os.remove(self.path)
                    else:
                        self.write(before)
                    self.run_cmd(install.install, args(self.path, apply=True))
                    obj = json.loads(self.read())
                    obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                    obj["statusLine"]["padding"] = 0
                    put(self.path, json.dumps(obj, indent=2, ensure_ascii=False))
                    if then is not None:
                        rc, out = self.run_cmd(install.install, args(self.path, apply=True, **then))
                        self.assertIn("REPLACE" if then else "already runs glidepath", out)
                    if then:  # "kept in the backup and in install.json"
                        rec = install.load_state()["records"][self.path]
                        self.assertEqual(rec["original_statusLine"], {"type": "command", "command": newer, "padding": 0})
                    rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                    self.assertEqual(rc, 0, out)
                    after = json.loads(self.read())
                    self.assertEqual(after.pop("statusLine"), {"type": "command", "command": newer, "padding": 0})
                    self.assertEqual(after, before or {})

    def test_uninstall_completes_with_claude_missing_once_the_user_file_lists_no_mod(self):
        # Review 5 (D-install#1): claude is missing at uninstall. Once the user file lists
        # neither the plugin nor its marketplace (the user ran the reverse commands by hand
        # as the warning says, or the install commands timed out), there is nothing to
        # undo whatever the mod record says, and uninstall completes byte for byte. The
        # warning never claims the mod is installed when nothing shows it is.
        proj = self._proj()
        runtime = os.path.join(self.home, "runtime.json")
        cli = plugin_cli(self.path)

        def timed_out(cmd, env=None, cwd=None):
            return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])

        for target in (self.path, proj):
            for how in ("claude", "by hand", "timed out"):
                with self.subTest(target=os.path.basename(target), install=how):
                    self._reset()
                    with self._user_file(IRREGULAR):
                        with mock.patch.object(install, "claude_bin", lambda: None if how == "by hand" else "/bin/claude"), \
                                mock.patch.object(install, "run_claude", timed_out if how == "timed out" else cli):
                            self.run_cmd(install.install, args(target, apply=True, no_mod=False))
                            if how == "by hand":
                                for c in install.mod_commands(install.repo_root()):
                                    cli(c)  # the user, by hand
                            if how == "timed out":  # claude still there, still hanging: retry later
                                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                                self.assertEqual(rc, 1, out)
                                self.assertNotIn("mod is still installed", out)
                                self.assertIn("may still be installed", out)
                        with mock.patch.object(install, "claude_bin", lambda: None), \
                                mock.patch.object(install, "run_claude", claude_missing):
                            if how != "timed out":
                                rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                                self.assertEqual(rc, 1, out)  # the user file still lists the mod
                                self.assertIn("may still be installed", out)
                                for c in install.unmod_commands():
                                    cli(c)  # the user, by hand
                            rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertIn("Uninstalled.", out)
                    self.assertEqual(self.read(), IRREGULAR)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)
                    self.assertFalse(os.path.exists(runtime))

    def test_user_file_edited_before_installing_into_it_comes_back_as_edited(self):
        # Review 5 (D-install#2): the mod went in first (for the project file, or for the
        # user file with --no-mod uninstalled since), then the user edited the user file,
        # then glidepath was installed into it. The file right before that install is its
        # baseline (the one before the mod only a fallback), so it comes back byte for byte.
        proj = self._proj()
        for first, last in ((("iP",), ("iU", "uU")), (("iP",), ("iU-nomod", "uU-nomod")),
                            (("iU", "uU-nomod"), ("iU", "uU-nomod")),
                            (("iP-nobin",), ("iU-nobin", "uU-nobin", "uP-nobin"))):
            with self.subTest(steps=first + ("edit",) + last):
                self._reset()
                with self._user_file(IRREGULAR):
                    self._steps(proj, *first)
                    edited = self._user_text().replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)
                    put(self.path, edited)
                    rcs = self._steps(proj, *last)
                self.assertEqual(rcs, [0] * len(last))
                self.assertEqual(self._user_text(), edited)

    def test_uninstall_leaves_a_user_file_the_mod_never_touched_alone(self):
        # Review 5 (D-install#3): claude was not on PATH at install and the printed commands
        # never ran, so the plugin CLI never wrote to the user file. Uninstall must not
        # rewrite it (here reformatted, or created since), with claude still missing or
        # back on PATH and reporting nothing to remove.
        proj = self._proj()

        def not_installed(cmd, env=None, cwd=None):  # the plugin CLI with nothing to remove
            if cmd[:2] == ["plugin", "uninstall"]:
                return 1, "", 'Plugin "%s" not found in installed plugins' % install.PLUGIN_ID
            return 1, "", "Marketplace '%s' not found" % install.MARKETPLACE

        reformatted = json.dumps(json.loads(IRREGULAR), indent=4, ensure_ascii=False)  # same JSON
        for before, now in ((IRREGULAR, reformatted), (None, "{}")):
            for exe, cli in ((None, claude_missing), ("/bin/claude", not_installed)):
                with self.subTest(before=before is not None, claude=exe):
                    self._reset()
                    with self._user_file(before):
                        with mock.patch.object(install, "claude_bin", lambda: None), \
                                mock.patch.object(install, "run_claude", claude_missing):
                            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
                        put(self.path, now)
                        with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli):
                            rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(self._user_text(), now)
                    self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def test_plugin_commands_first_run_at_a_later_install_take_a_fresh_snapshot(self):
        # Review 5 (D-install#3): a manual install whose commands never ran, a hand edit of
        # the user file, then an install with claude on PATH that runs them for the first
        # time. The user file comes back as it was right before they ran.
        proj = self._proj()
        with self._user_file(IRREGULAR):
            self._steps(proj, "iP-nobin")
            edited = IRREGULAR.replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)
            put(self.path, edited)
            rcs = self._steps(proj, "iP", "uP")
        self.assertEqual(rcs, [0, 0])
        self.assertEqual(self.read(), edited)
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_blank_backup_means_no_statusline_when_the_record_is_lost(self):
        # Review 5 (D-install#4): with install.json gone, the newest backup is the blank file
        # from before the last install, so there was no statusLine then. An older backup's
        # statusLine must not come back, at uninstall or through a reinstall's record.
        for reinstall in (False, True):
            with self.subTest(reinstall=reinstall):
                self._reset()
                self.write(dict(SETTINGS, statusLine={"type": "command", "command": "~/old-line.sh"}))
                self.run_cmd(install.install, args(self.path, apply=True))
                self.run_cmd(install.uninstall, args(self.path, apply=True))
                put(self.path, "")
                self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(len([n for n in os.listdir(self.dir) if ".glidepath-backup-" in n]), 2)
                shutil.rmtree(self.home)  # install.json gone
                if reinstall:
                    self.run_cmd(install.install, args(self.path, apply=True))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(json.loads(self.read()), {})

    # -- sixth review ----------------------------------------------------------------
    def _assert_clean(self, proj):
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_user_file_the_plugin_cli_rewrote_comes_back_although_its_plugin_keys_existed(self):
        # Review 6 (F-install#0): the user file lists another plugin, so both plugin keys were
        # there before glidepath. Once glidepath has seen the mod listed there (a failed uninstall
        # after the user ran the printed commands, or a repin without claude after the plugin
        # CLI installed it), the plugin CLI has written there, whatever comes next: after the
        # user removes the plugin by hand, uninstall still gives the file back byte for byte.
        proj = self._proj()
        for steps in (("iP-nobin", "mod-by-hand", "uP-nobin", "unmod-by-hand", "uP-nobin"),
                      ("iP-nobin", "mod-by-hand", "uP-nobin", "unmod-by-hand", "uP"),
                      ("iP", "iP-nobin", "unmod-by-hand", "uP-nobin"),
                      ("iP", "iP-nobin", "unmod-by-hand", "uP"),
                      ("iP", "iP-nobin", "unmod-by-hand", "iP", "uP")):
            with self.subTest(steps=steps):
                self._reset()
                with self._user_file(WITH_PLUGINS):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(self._user_text(), WITH_PLUGINS)
                self._assert_clean(proj)

    def test_user_file_the_plugin_cli_never_wrote_to_is_never_rewritten(self):
        # Review 6 (F-install#1): the plugin CLI never wrote to the user file (claude was off
        # PATH, or the install commands timed out), and the user reformatted it since (or
        # created it). No uninstall rewrites it: not one that reaches it as its own target
        # (after a --no-mod uninstall, or a rerun after claude hung), not one that finds
        # claude back with nothing to remove, not one after install commands that timed out.
        proj = self._proj()
        reformatted = json.dumps(json.loads(IRREGULAR), indent=4, ensure_ascii=False)  # same JSON
        for before, now, first, last in ((IRREGULAR, reformatted, ("iP-nobin", "uP-nomod"), ("u*-nobin",)),
                                         (IRREGULAR, reformatted, ("iP-nobin", "uP-nomod"), ("u*-none",)),
                                         (IRREGULAR, reformatted, ("iP-nobin",), ("uP-hang", "u*-nobin")),
                                         (IRREGULAR, reformatted, ("iP-nobin",), ("uP-hang", "u*-none")),
                                         (IRREGULAR, reformatted, ("iP-hang",), ("uP-nobin",)),
                                         (IRREGULAR, reformatted, ("iP-hang",), ("uP-none",)),
                                         (None, "{}", ("iP-nobin", "uP-nomod"), ("u*-nobin",))):
            with self.subTest(before=before is not None, steps=first + ("edit",) + last):
                self._reset()
                with self._user_file(before):
                    self._steps(proj, *first)
                    put(self.path, now)
                    rcs = self._steps(proj, *last)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(self._user_text(), now)
                self._assert_clean(proj)

    def test_full_uninstall_without_a_user_file_leaves_no_record_of_it(self):
        # Review 6 (F-install#2): no user file, glidepath installed into it and into a project
        # file, then everything uninstalled. The user file's kept record must go with the mod,
        # or a later install cycle takes it as that file's baseline from before the mod and
        # loses the byte-for-byte restore. Install also drops such a record left by older code.
        proj = self._proj()
        for first in (("iP-nobin", "iU-nobin", "uU-nobin", "uP-nobin"), ("iP-hang", "uU-nobin", "uP-nobin"), None):
            with self.subTest(first=first):
                self._reset()
                with self._user_file(None):
                    if first is None:  # what the code before this fix left behind
                        install.save_state({"records": {self.path: {
                            "settings_path": self.path, "had_file": False, "installed": False,
                            "had_keys": {k: False for k in install.PLUGIN_KEYS}}}})
                    else:
                        rcs = self._steps(proj, *first)
                        self.assertEqual(rcs[-1], 0)
                        self._assert_clean(proj)
                    put(self.path, IRREGULAR)
                    rcs = self._steps(proj, "iP", "iU", "uU", "uP")
                self.assertEqual(rcs, [0, 0, 0, 0])
                self.assertEqual(self._user_text(), IRREGULAR)
                self._assert_clean(proj)

    def test_fresh_user_file_snapshot_replaces_its_kept_record(self):
        # Review 6 (F-install#3): glidepath went into the user file and out again while a manual
        # mod stayed for the project file, so the user file has a kept record. The user then
        # edited (or created) it, and an install with claude on PATH runs the plugin commands
        # for the first time. The snapshot taken then is the newer baseline from before the
        # plugin CLI, so the file comes back as it was right before them.
        proj = self._proj()
        edits = (("key", IRREGULAR, lambda t: t.replace("{\n", '{\n  "sandbox": {"enabled": true},\n', 1)),
                 ("reformat", IRREGULAR, lambda t: json.dumps(json.loads(t), indent=4, ensure_ascii=False)),
                 ("create {}", None, lambda t: "{}\n"),
                 ("create", None, lambda t: IRREGULAR))
        for name, before, edit in edits:
            for last in ("uP", "u*"):
                with self.subTest(edit=name, last=last):
                    self._reset()
                    with self._user_file(before):
                        self._steps(proj, "iU-nobin", "iP-nobin", "uU-nobin")
                        edited = edit(self._user_text())
                        put(self.path, edited)
                        rcs = self._steps(proj, "iP", last)
                    self.assertEqual(rcs, [0, 0])
                    self.assertEqual(self._user_text(), edited)
                    self._assert_clean(proj)

    def test_command_put_in_compose_survives_replace_after_the_record_is_lost(self):
        # Review 6 (F-install#4): install.json is gone, and the user put or changed a command in
        # glidepath's --compose. `install --replace` drops it from the statusLine, so the record it
        # rebuilds must hold it ("kept in the backup and in install.json"), whether the newest
        # backup from before glidepath shows no statusLine, another one, or there is no backup left.
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, backups in ((SETTINGS, True), (dict(SETTINGS, statusLine=theirs), True), (SETTINGS, False)):
            with self.subTest(statusLine=start.get("statusLine"), backups=backups):
                self._reset()
                self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                shutil.rmtree(self.home)  # install.json gone
                if not backups:  # and the backups: a repin rebuilds a record that knows nothing
                    for n in os.listdir(self.dir):
                        if ".glidepath-backup-" in n:
                            os.remove(os.path.join(self.dir, n))
                    self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                put(self.path, json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
                self.assertIn("REPLACE", out)
                self.assertIsNone(install.compose_of(json.loads(self.read())["statusLine"]["command"]))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual((after.pop("statusLine", None) or {}).get("command"), newer)
                self.assertEqual(after, SETTINGS)

    def test_user_file_created_after_the_mod_is_restored_not_deleted(self):
        # Review 6 (F-install#5): there was no user file when the mod went in; the user then
        # created one ('{}', or blank), and glidepath went into it and out again. The file right
        # before that install is its first baseline, so it comes back; the snapshot from before
        # the mod (no file) is only a fallback and must not get it deleted.
        proj = self._proj()
        for created in ("{}\n", "{}", ""):
            for steps in (("iU-nobin", "uU-nobin"), ("iU-nobin", "uU-nobin", "uP-nobin"),
                          ("iU", "uU", "uP"), ("iU", "uP", "uU")):
                with self.subTest(created=created, steps=steps):
                    self._reset()
                    with self._user_file(None):
                        self._steps(proj, "iP-nobin")
                        put(self.path, created)
                        rcs = self._steps(proj, *steps)
                    self.assertEqual(rcs, [0] * len(steps))
                    self.assertEqual(self._user_text(), created)

    def test_user_file_the_plugin_cli_writes_during_uninstall_comes_back(self):
        # Install fuzzer: the plugin CLI never wrote for a manual mod, but the reverse commands
        # at uninstall rewrite the user file (or create it) although there is nothing to
        # remove. Then it is the plugin CLI's doing, so the file comes back as it was (or goes).
        proj = self._proj()
        for before, steps in ((IRREGULAR, ("iP-nobin", "uP")), (IRREGULAR.replace("\n", "\r\n"), ("iP-nobin", "uP")),
                              (None, ("iP-nobin", "uP")), (None, ("iU-hang", "uU-hang", "u*"))):
            with self.subTest(before=before, steps=steps):
                self._reset()
                with self._user_file(before):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                if before is None:
                    self.assertFalse(os.path.exists(self.path))
                else:
                    with open(self.path, "rb") as f:
                        self.assertEqual(f.read(), before.encode("utf-8"))
                self._assert_clean(proj)

    def test_plugin_keys_gone_from_the_user_file_are_not_left_behind_empty(self):
        # Install fuzzer: the user file listed another plugin, so both plugin keys were there at
        # the snapshot. The user then blanked or deleted the file, and the plugin CLI wrote its
        # keys again (a later install, or the printed commands run by hand): they are not the
        # user's any more, so uninstall must not leave them behind as empty objects.
        proj = self._proj()
        for how in ("blank", "delete"):
            for steps in (("iP", "edit", "iP", "uP"), ("iU", "edit", "iU", "uU"),
                          ("iP-nobin", "edit", "mod-by-hand", "uP"),
                          ("iP-nobin", "edit", "mod-by-hand", "iP-nobin", "unmod-by-hand", "uP")):
                with self.subTest(how=how, steps=steps):
                    self._reset()
                    with self._user_file(WITH_PLUGINS):
                        i = steps.index("edit")
                        self._steps(proj, *steps[:i])
                        if how == "blank":
                            put(self.path, "")
                        else:
                            os.remove(self.path)
                        rcs = self._steps(proj, *steps[i + 1:])
                    self.assertEqual(rcs[-1], 0)
                    text = self._user_text()
                    self.assertEqual(json.loads(text) if text and text.strip() else {}, {})
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def test_plugin_keys_that_hold_only_glidepath_are_not_the_users(self):
        # Install fuzzer: the user ran the plugin commands by hand before glidepath's snapshot of
        # the user file, so its plugin keys hold nothing but glidepath's entries. They are the
        # plugin CLI's, so uninstall removes them once empty.
        proj = self._proj()
        for steps in (("mod-by-hand", "iP", "uP"), ("mod-by-hand", "iU", "uU"), ("mod-by-hand", "iP-nobin", "uP")):
            with self.subTest(steps=steps):
                self._reset()
                with self._user_file(IRREGULAR):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(json.loads(self._user_text()), json.loads(IRREGULAR))
                self._assert_clean(proj)

    def test_probe_and_command_quoting(self):
        ok, ver, sq, ms, note = install.probe(sys.executable)
        self.assertTrue(ok, note)
        self.assertFalse(install.probe("python3")[0])  # not absolute
        cmd = install.our_command("/opt/my py/python3", "/x/bin/glidepath", "echo 'a b'")
        self.assertEqual(install.compose_of(cmd), "echo 'a b'")
        self.assertTrue(install.is_ours(cmd))

    def test_tidy_leaves_an_invalid_user_file_alone(self):
        # Review 6 follow-up: a user settings file that is not valid JSON made
        # the tidy raise out of uninstall.
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        quiet = lambda cmd, env=None, cwd=None: (0, "ok", "")  # the plugin CLI, writing nothing
        with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            put(self.path, '{"model": "opus",')  # the user broke it by hand
            with mock.patch.object(install, "run_claude", quiet):
                rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertIn("Left", out)
        self.assertEqual(self.read(), '{"model": "opus",')

    def test_snapshot_of_an_invalid_user_file(self):
        # Review 6 follow-up: an invalid user file is not "absent": it exists,
        # and its plugin keys are unknown, so none is ever removed.
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        quiet = lambda cmd, env=None, cwd=None: (0, "ok", "")
        with self._user_file('{"oops"'), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", quiet):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            mod = install.load_state()["mod"]
            self.assertTrue(mod["had_file"])
            self.assertEqual(mod["had_keys"], {k: True for k in install.PLUGIN_KEYS})
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), '{"oops"')

    def test_backups_sort_by_time_then_sequence(self):
        # Review 6 follow-up: names sorted as strings put "-9" after "-10".
        self.write(SETTINGS)
        for suffix, cmd in (("", "zero"), ("-9", "nine"), ("-10", "ten")):
            put(self.path + ".glidepath-backup-20261014-090000" + suffix, json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": cmd})))
        put(self.path + ".glidepath-backup-20261013-235959-99", json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": "yesterday"})))
        had, sl = install._original_from_backups(self.path)
        self.assertTrue(had)
        self.assertEqual(sl["command"], "ten")

    # -- seventh review --------------------------------------------------------------
    def test_uninstall_no_mod_keeps_what_the_kept_mod_needs(self):
        # Review 7 (install-fuzz#0): `uninstall --no-mod` keeps the mod, which reads
        # runtime.json, so runtime.json and the launcher stay with it; the full uninstall
        # that removes the mod later removes them too.
        proj = self._proj()
        runtime = os.path.join(self.home, "runtime.json")
        lp = os.path.join(self.dir, "bin", install.launcher_name())
        for target in (self.path, proj):
            with self.subTest(target=os.path.basename(target)):
                self._reset()
                with self._user_file(IRREGULAR), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                        mock.patch.object(install, "run_claude", plugin_cli(self.path)):
                    self.run_cmd(install.install, args(target, apply=True, no_mod=False, bin_dir=os.path.dirname(lp)))
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=True))
                    self.assertEqual(rc, 0, out)
                    self.assertIn(install.PLUGIN_ID, json.loads(self.read())["enabledPlugins"])  # the mod stays
                    self.assertTrue(install.load_state().get("mod"))
                    self.assertTrue(os.path.exists(runtime), out)
                    self.assertTrue(os.path.exists(lp), out)
                    rc, out = self.run_cmd(install.uninstall, args(None, apply=True, no_mod=False))
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.read(), IRREGULAR)
                self.assertFalse(os.path.exists(runtime))
                self.assertFalse(os.path.exists(lp))
                self._assert_clean(proj)

    def test_uninstall_removes_the_mod_after_install_json_is_lost(self):
        # Review 7 (install-fuzz#1): the data folder was wiped (or the shell has another
        # GLIDEPATH_HOME), so install.json has no mod record, but the user file still lists
        # the mod. Uninstall rebuilds the record from the user file, says so, and runs the
        # reverse commands, also in the dry run; with claude missing it exits 1 to retry.
        proj = self._proj()
        cli = plugin_cli(self.path)
        for target in (self.path, proj):
            for first in ((), ("u*-nobin",)):
                with self.subTest(target=os.path.basename(target), first=first):
                    self._reset()
                    calls = []

                    def recording(cmd, env=None, cwd=None):
                        calls.append(cmd)
                        return cli(cmd)

                    with self._user_file(IRREGULAR):
                        self._steps(proj, "iU" if target == self.path else "iP")
                        shutil.rmtree(self.home)  # install.json gone
                        if first:
                            rcs = self._steps(proj, *first)
                            self.assertEqual(rcs, [1])
                            self.assertTrue(install.load_state().get("mod"))  # kept for the rerun
                        with mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                                mock.patch.object(install, "run_claude", recording):
                            rc, out = self.run_cmd(install.uninstall, args(target, no_mod=False))  # dry run
                            self.assertEqual(rc, 0, out)
                            self.assertIn("claude plugin uninstall", out)
                            if not first:
                                self.assertIn("no record of it", out)
                            self.assertEqual(calls, [])
                            rc, out = self.run_cmd(install.uninstall, args(target, apply=True, no_mod=False))
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(calls, install.unmod_commands())
                    # Without install.json no snapshot is left to restore bytes from: the JSON comes back.
                    self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
                    self.assertEqual(json.loads(install.read_text(proj)), {"model": "sonnet"})
                    state = install.load_state()
                    self.assertEqual(state.get("records"), {})
                    self.assertNotIn("mod", state)

    def _v1_install(self, text):
        """What version 1 (c016f98) left after `install --apply` into the user file with
        claude on PATH: its backup, the file with the plugin CLI's keys, and its record."""
        backup = os.path.realpath(self.path) + ".glidepath-backup-20261001-090000"
        put(backup, text)
        sl = {"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
              "refreshInterval": 60}
        obj = dict(json.loads(text), statusLine=sl, enabledPlugins={install.PLUGIN_ID: True},
                   extraKnownMarketplaces={install.MARKETPLACE: {"source": {}}})
        put(self.path, json.dumps(obj, indent=2))
        os.makedirs(self.home, exist_ok=True)
        put(os.path.join(self.home, "runtime.json"), "{}")
        with open(install.state_path(), "w") as f:
            json.dump({"installed": True, "settings_path": self.path, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha(text), "had_statusLine": False, "original_statusLine": None,
                       "had_keys": {k: False for k in install.PLUGIN_KEYS}, "python": sys.executable,
                       "entry": install.entry_path(), "written_statusLine": sl,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)

    def test_version_1_mod_record_keeps_the_user_file_baseline_for_a_rerun(self):
        # Review 7 (install-fuzz#2): version 1's mod record names no user file; its plugin
        # commands always edited the default one. An uninstall that leaves the mod (claude
        # missing or hanging, or --no-mod) must keep that file's record, so the run that
        # removes the mod brings the file back byte for byte, without empty plugin keys.
        for first in ("u*-nobin", "u*-hang", "u*-nomod"):
            with self.subTest(first=first):
                self._reset()
                with self._user_file(IRREGULAR):
                    self._v1_install(IRREGULAR)
                    rcs = self._steps(None, first, "u*")
                self.assertEqual(rcs, [0 if first.endswith("nomod") else 1, 0])
                self.assertEqual(self.read(), IRREGULAR)
                state = install.load_state()
                self.assertEqual(state.get("records"), {})
                self.assertNotIn("mod", state)

    def test_version_1_mod_record_takes_the_user_files_kept_record_with_it(self):
        # Review 7 (install-fuzz#2, found again by the stress fuzzer on the fix): version 1
        # installed into a project file, its plugin commands wrote the default user file, and
        # glidepath went into that file with --no-mod and out again while the mod stayed. The
        # user file's record is kept for the mod; the uninstall that removes the mod must
        # tidy that file and drop the record, though the version-1 mod names no user file.
        proj = self._proj()
        sl = {"type": "command", "command": install.our_command(sys.executable, install.entry_path()),
              "refreshInterval": 60}
        put(proj, json.dumps({"model": "sonnet", "statusLine": sl}, indent=2))
        backup = os.path.realpath(proj) + ".glidepath-backup-20261001-090000"
        put(backup, '{"model": "sonnet"}')
        os.makedirs(self.home)
        with open(install.state_path(), "w") as f:  # what version 1 wrote for `install --apply --settings proj`
            json.dump({"installed": True, "settings_path": proj, "had_file": True, "backup_path": backup,
                       "original_sha256": install.sha('{"model": "sonnet"}'), "had_statusLine": False,
                       "original_statusLine": None, "had_keys": {k: False for k in install.PLUGIN_KEYS},
                       "python": sys.executable, "entry": install.entry_path(), "written_statusLine": sl,
                       "mod": {"marketplace": install.MARKETPLACE, "plugin": install.PLUGIN_ID, "root": install.repo_root()}}, f)
        with self._user_file(IRREGULAR):
            for c in install.mod_commands(install.repo_root()):
                plugin_cli(self.path)(c)  # version 1's plugin commands
            rcs = self._steps(proj, "iU-nomod", "uU", "uP")
        self.assertEqual(rcs, [0, 0, 0])
        self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
        self.assertEqual(install.read_text(proj), '{"model": "sonnet"}')
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_command_put_in_compose_wins_over_a_backup_when_uninstall_finds_no_record(self):
        # Review 7 (install-fuzz#3): install.json is gone and the user changed the command in
        # glidepath's --compose. Uninstall restores that command, as a repin would have recorded
        # it, not the older one in the backup from before glidepath (and, when there was no
        # statusLine before glidepath, without the refreshInterval glidepath added).
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, want in ((dict(SETTINGS, statusLine=theirs), dict(theirs, command=newer)),
                            (SETTINGS, {"type": "command", "command": newer, "padding": 0})):
            with self.subTest(statusLine=start.get("statusLine")):
                self._reset()
                self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                shutil.rmtree(self.home)  # install.json gone
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                put(self.path, json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine", None), want)
                self.assertEqual(after, SETTINGS)

    def test_command_put_in_compose_survives_replace_and_then_a_lost_record(self):
        # Review 7 (install fuzzer): the user changed the command in glidepath's --compose, then
        # `install --replace` dropped it from the file (kept in the backup it took and in
        # install.json), then install.json was lost. That backup's --compose wins over the
        # older backup from before glidepath, or is used when there is none (then with the
        # statusLine's other keys as glidepath ran it: nothing tells which were glidepath's).
        newer = "npx -y ccstatusline@latest"
        theirs = {"type": "command", "command": "~/old-line.sh"}
        for start, want in ((dict(SETTINGS, statusLine=theirs), dict(theirs, command=newer)),
                            (SETTINGS, {"type": "command", "command": newer, "padding": 0}),
                            (None, {"type": "command", "command": newer, "padding": 0, "refreshInterval": 60})):
            with self.subTest(start=start and start.get("statusLine", "none")):
                self._reset()
                if start is None:
                    if os.path.exists(self.path):
                        os.remove(self.path)
                else:
                    self.write(start)
                self.run_cmd(install.install, args(self.path, apply=True))
                obj = json.loads(self.read())
                obj["statusLine"]["command"] = install.our_command(sys.executable, install.entry_path(), newer)
                obj["statusLine"]["padding"] = 0
                put(self.path, json.dumps(obj, indent=2, ensure_ascii=False))
                rc, out = self.run_cmd(install.install, args(self.path, apply=True, replace=True))
                self.assertIn("REPLACE", out)
                shutil.rmtree(self.home)  # install.json gone
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                after = json.loads(self.read())
                self.assertEqual(after.pop("statusLine", None), want)
                self.assertEqual(after, start and SETTINGS or {})

    def test_uninstall_no_mod_keeps_runtime_json_for_a_mod_only_the_user_file_lists(self):
        # Review 7 (install fuzzer): install.json was lost while the mod was installed, an
        # `install --no-mod` wrote runtime.json again, and `uninstall --no-mod` deleted it as if
        # no mod were left, so a later uninstall that failed kept a mod record without it. The
        # user file lists the mod: it is kept, and runtime.json with it, until a full uninstall.
        runtime = os.path.join(self.home, "runtime.json")
        with self._user_file(IRREGULAR):
            self._steps(None, "iU")
            shutil.rmtree(self.home)  # install.json gone
            rcs = self._steps(None, "iU-nomod", "u*-nomod")
            self.assertEqual(rcs, [0, 0])
            self.assertTrue(install.load_state().get("mod"))
            self.assertTrue(os.path.exists(runtime))
            rcs = self._steps(None, "u*-nobin")
            self.assertEqual(rcs, [1])
            self.assertTrue(os.path.exists(runtime))
            rcs = self._steps(None, "u*")
        self.assertEqual(rcs, [0])
        self.assertEqual(json.loads(self.read()), json.loads(IRREGULAR))
        self.assertFalse(os.path.exists(runtime))
        state = install.load_state()
        self.assertEqual(state.get("records"), {})
        self.assertNotIn("mod", state)

    def test_the_users_own_empty_plugin_key_survives(self):
        # Review 7 follow-up: the plugin CLI leaves "enabledPlugins": {} after
        # its last plugin goes, so many user files hold it. Once the mod was in
        # it, uninstall took the key for the plugin CLI's and removed it.
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, '{"model": "sonnet"}')
        before = '{\n  "model": "opus",\n  "enabledPlugins": {}\n}\n'
        with self._user_file(before), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", plugin_cli(self.path)):
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))
            self.run_cmd(install.install, args(proj, apply=True, no_mod=False))  # a repin looks again
            rc, out = self.run_cmd(install.uninstall, args(proj, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)

    # -- eighth review ---------------------------------------------------------------
    def test_launcher_follows_a_repin_and_only_the_newest_is_glidepaths(self):
        # Review 8 (delta-install#0): a repin without --bin-dir left the launcher running the
        # old interpreter, and a launcher in an earlier --bin-dir outlived the full uninstall.
        before = self.write(SETTINGS)
        first, second = os.path.join(self.dir, "bin"), os.path.join(self.dir, "bin2")
        lp1, lp2 = os.path.join(first, install.launcher_name()), os.path.join(second, install.launcher_name())
        alt = self.alt_python()
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=first))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(lp1), install.launcher_text(alt, install.entry_path()))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=second))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp1), out)
        self.assertEqual(install.read_text(lp2), install.launcher_text(sys.executable, install.entry_path()))
        os.remove(lp2)  # the user deleted it: only --bin-dir brings it back
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp2), out)
        self.assertNotIn("launcher", install.load_state())
        # A file the user put in a launcher's place is theirs: never rewritten or removed.
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=first))
        put(lp1, "#!/bin/sh\necho mine\n")
        self.run_cmd(install.install, args(self.path, apply=True, python=alt))
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=second))
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(lp1), "#!/bin/sh\necho mine\n")
        self.assertFalse(os.path.exists(lp2), out)
        self.assertEqual(self.read(), before)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_backups_keep_their_order_when_the_clock_moves_west(self):
        # Review 8 (delta-install#1): backups were named in local time, so after a move west
        # (or the fall-back hour) a newer backup sorted before an older one, and an uninstall
        # without install.json restored a statusLine the user had removed.
        with time_zone("EST5"):  # New York
            self.write(dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/one.sh"}))
            self.run_cmd(install.install, args(self.path, apply=True))
            self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.write(SETTINGS)  # the user removed their statusLine
        with time_zone("PST8"):  # Los Angeles, the same day
            self.run_cmd(install.install, args(self.path, apply=True))
            shutil.rmtree(self.home)  # install.json goes with the data folder
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.read()), SETTINGS)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_backups_named_in_utc_sort_among_older_local_time_names(self):
        # Review 8 (delta-install#1): new backups are named in UTC (a Z marks them), older ones
        # in local time. Review 9 (delta-install#1): a Z name comes from a newer version than
        # any local-time name, so it sorts after them all, also where local time reads later.
        with time_zone("JST-9"):  # Tokyo, 9 hours ahead of UTC
            for legacy, want in (("20261014-120000", "utc"), ("20261014-150000", "utc")):
                with self.subTest(legacy=legacy):
                    self._reset()
                    for name, cmd in ((legacy, "legacy"), ("20261014-050000Z", "utc")):
                        put(self.path + ".glidepath-backup-" + name, json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": cmd})))
                    self.assertEqual(install._original_from_backups(self.path)[1]["command"], want)

    def test_the_users_own_empty_plugin_key_survives_other_plugins_coming_and_going(self):
        # Review 8 (delta-install#2): glidepath saw the user's key with another plugin in it, and the
        # user then removed that plugin with the plugin CLI. Holding only glidepath's entry at the
        # next look, the key counted as changed out of glidepath's sight, so uninstall removed it.
        def other(add):
            """The user installs or removes another plugin with the plugin CLI."""
            obj = json.loads(self.read())
            if add:
                obj["enabledPlugins"]["other@mkt2"] = True
            else:
                obj["enabledPlugins"] = {k: v for k, v in obj["enabledPlugins"].items() if not k.startswith("other@")}
            put(self.path, json.dumps(obj, indent=2))

        empty = '{\n  "model": "opus",\n  "enabledPlugins": {}\n}\n'
        listed = '{\n  "model": "opus",\n  "enabledPlugins": {"other@mkt": true}\n}\n'
        for before, steps in ((empty, ("iU", "iP", "add", "uP", "remove", "uU")), (listed, ("iU", "remove", "uU")),
                              (listed, ("iP", "remove", "uP")), (empty, ("iP", "add", "iP", "remove", "uP"))):
            with self.subTest(steps=steps):
                self._reset()
                proj = self._proj()
                with self._user_file(before):
                    for s in steps:
                        if s in ("add", "remove"):
                            other(s == "add")
                        else:
                            self.assertEqual(self._steps(proj, s), [0])
                self.assertEqual(json.loads(self.read()), {"model": "opus", "enabledPlugins": {}})
                if before == empty:
                    self.assertEqual(self.read(), before)
                self._assert_clean(proj)

    # -- ninth review ----------------------------------------------------------------
    def test_the_users_own_empty_plugin_key_survives_a_manual_mod(self):
        # Review 9 (delta-install#0): with claude off PATH at install, the user runs the printed
        # plugin commands. Their own empty key then holds only glidepath's entry, which counted as
        # the plugin CLI's for a manual mod, so uninstall removed it and rewrote the file.
        empty = '{\n  "model": "opus",\n  "enabledPlugins": {}\n}\n'
        for steps in (("iU-nobin", "mod-by-hand", "uU"), ("iP-nobin", "mod-by-hand", "uP"),
                      ("iU-nobin", "mod-by-hand", "uU-nobin", "unmod-by-hand", "uU-nobin"),
                      ("iP-nobin", "mod-by-hand", "uP-nobin", "unmod-by-hand", "uP-nobin")):
            with self.subTest(steps=steps):
                self._reset()
                proj = self._proj()
                with self._user_file(empty):
                    rcs = self._steps(proj, *steps)
                self.assertEqual(rcs[-1], 0)
                self.assertEqual(self.read(), empty)
                self._assert_clean(proj)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_backups_named_in_utc_sort_after_local_time_names_from_an_older_version(self):
        # Review 9 (delta-install#1): a name without Z was read in the zone in effect when read, so
        # after a move west a backup an older version had named in local time sorted after a newer
        # Z backup, and an uninstall without install.json restored a statusLine the user removed.
        def legacy_backup(path):
            """make_backup as versions before review 8 named it: local time, no Z."""
            name = "%s.glidepath-backup-%s" % (os.path.realpath(path), time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(path, name)
            return name

        with time_zone("EST5"), mock.patch.object(install, "make_backup", legacy_backup):  # New York, the older version
            self.write(dict(SETTINGS, statusLine={"type": "command", "command": "~/bin/one.sh"}))
            self.run_cmd(install.install, args(self.path, apply=True))
            self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.write(SETTINGS)  # the user removed their statusLine
        with time_zone("PST8"):  # Los Angeles, the same day, the newer version
            self.run_cmd(install.install, args(self.path, apply=True))
            shutil.rmtree(self.home)  # install.json goes with the data folder
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.read()), SETTINGS)

    def test_launcher_moved_to_its_own_directory_under_another_name_stays(self):
        # Review 9 (delta-install#2): realpath neither folds case (macOS disks ignore it) nor sees
        # through bind mounts, so a --bin-dir naming the earlier one another way counted as another
        # directory: install wrote the launcher there, then removed it as the earlier one.
        before = self.write(SETTINGS)
        first, alias = os.path.join(self.dir, "bin"), os.path.join(self.dir, "Bin")
        os.makedirs(first)
        realpath = os.path.realpath

        def case_blind(path, *a, **kw):
            """realpath as on a case-insensitive disk: Bin stays Bin."""
            return os.path.abspath(path) if os.path.abspath(path).startswith(alias + os.sep) else realpath(path, *a, **kw)

        if os.path.exists(alias):  # a case-insensitive disk (macOS, Windows): Bin is the real alias already
            disk = contextlib.nullcontext()
        else:
            os.symlink(first, alias)  # one directory, two names
            disk = mock.patch.object(os.path, "realpath", case_blind)
        self.run_cmd(install.install, args(self.path, apply=True, bin_dir=alias))
        with disk:
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=first))
        self.assertEqual(rc, 0, out)
        lp = os.path.join(first, install.launcher_name())
        self.assertTrue(os.path.exists(lp), out)
        self.assertEqual(install.read_text(lp), install.launcher_text(sys.executable, install.entry_path()))
        self.assertEqual(install.load_state()["launcher"], lp)
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp), out)
        self.assertEqual(self.read(), before)


# -- Windows ------------------------------------------------------------------------
# The Windows branches run here with config.IS_WINDOWS patched, on paths that only have
# to look like Windows ones; the windows-latest CI job runs the suite for real.
PY = r"C:\Users\me\AppData\Local\Programs\Python\Python312\python.exe"
PY_SPACE = r"C:\Program Files\Python312\python.exe"
ENTRY = r"C:\Users\me\Claude-Code-Mods\bin\glidepath"
ENTRY_SPACE = r"C:\Users\me\My Repos\Claude-Code-Mods\bin\glidepath"
FWD = "C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe -I -S -X utf8 C:/Users/me/Claude-Code-Mods/bin/glidepath statusline"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


@contextlib.contextmanager
def windows(on=True):
    """config.IS_WINDOWS, the one platform flag, set (or cleared). Off Windows the data folder
    stays the sandbox's GLIDEPATH_HOME: Windows rules would read a POSIX temp path as one
    without a drive (and a test without a sandbox fails here rather than reach ~). No 8.3
    short paths: the fake paths may exist on a real Windows (a test gives its own)."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(config, "IS_WINDOWS", on))
        stack.enter_context(mock.patch.object(winshell, "short_path", return_value=None))
        if on and os.name != "nt":
            stack.enter_context(mock.patch.object(config, "home", lambda: os.environ["GLIDEPATH_HOME"]))
        yield


class FakeWinreg:
    """The part of winreg that PEP 514 discovery uses, over a dict tree: a dict is a key, a str a value."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 1, 2, 4

    class Key:
        def __init__(self, node):
            self.node = node

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self, hives):
        self.hives = hives  # {(hive, view): tree}

    def OpenKey(self, key, sub, reserved=0, access=1):
        node = self.hives.get((key, access & 6)) if isinstance(key, str) else key.node
        for part in sub.split("\\"):
            if not isinstance(node, dict) or not isinstance(node.get(part), dict):
                raise FileNotFoundError(sub)
            node = node[part]
        return self.Key(node)

    def EnumKey(self, key, i):
        subs = [k for k, v in key.node.items() if isinstance(v, dict)]
        if i >= len(subs):
            raise OSError("no more keys")
        return subs[i]

    def QueryValueEx(self, key, name):
        if not isinstance(key.node.get(name), str):
            raise FileNotFoundError(name)
        return key.node[name], 1


class WindowsCommand(unittest.TestCase):
    def test_posix_command_and_flags_are_unchanged(self):
        with windows(False):
            self.assertEqual(install.our_command("/opt/my py/python3", "/x/bin/glidepath", "echo 'a b'"),
                             "'/opt/my py/python3' -I -S /x/bin/glidepath statusline --compose 'echo '\"'\"'a b'\"'\"''")
            self.assertEqual(winshell.python_flags(), ["-I", "-S"])
            self.assertEqual(install.launcher_name(), "glidepath")
            self.assertEqual(install.runtime_info("/usr/bin/python3", "/x/bin/glidepath")["args"], ["-I", "-S"])

    def test_forms_for_git_bash_and_powershell(self):
        # Unquoted forward-slash paths read the same in both shells; so do plain single quotes
        # after a bare interpreter. A quoted interpreter, or a quote inside an argument, takes
        # the predicted shell's syntax: single quotes for bash, `& ...` for PowerShell (always
        # with the `&`, which bash rejects, so the command itself says which shell it is for).
        cases = (
            (PY, ENTRY, None, FWD, FWD, None),
            (PY, ENTRY, "~/.claude/line.sh", FWD + " --compose '~/.claude/line.sh'", FWD + " --compose '~/.claude/line.sh'",
             None),
            (PY, ENTRY_SPACE, None,
             "C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe -I -S -X utf8 "
             "'C:/Users/me/My Repos/Claude-Code-Mods/bin/glidepath' statusline", None, None),
            (PY, ENTRY, "echo 'hi'", FWD + " --compose 'echo '\"'\"'hi'\"'\"''", "& " + FWD + " --compose 'echo ''hi'''",
             "per shell"),
            (PY, ENTRY, "echo \u2018hi\u2019", FWD + " --compose 'echo \u2018hi\u2019'",
             "& " + FWD + " --compose 'echo \u2018\u2018hi\u2019\u2019'", "per shell"),
            (PY_SPACE, ENTRY, None, "'C:/Program Files/Python312/python.exe' -I -S -X utf8 C:/Users/me/Claude-Code-Mods/bin/glidepath "
             "statusline", "& 'C:/Program Files/Python312/python.exe' -I -S -X utf8 C:/Users/me/Claude-Code-Mods/bin/glidepath "
             "statusline", "per shell"),
            (PY_SPACE, ENTRY_SPACE, "echo \u2019x\u2019 \"q\" a,b", None, None, "per shell"),
        )
        with windows():
            self.assertEqual(winshell.python_flags(), ["-I", "-S", "-X", "utf8"])
            for py, entry, compose, bash, ps, specific in cases:
                for shell, want in (("bash", bash), ("powershell", ps if specific else bash)):
                    with self.subTest(py=py, entry=entry, compose=compose, shell=shell):
                        cmd = install.our_command(py, entry, compose, shell)
                        if want is not None:
                            self.assertEqual(cmd, want)
                        self.assertTrue(install.is_ours(cmd))
                        self.assertEqual(install.compose_of(cmd), compose)
                        argv = install.split_command(cmd)
                        self.assertEqual(argv[:5], [py.replace("\\", "/"), "-I", "-S", "-X", "utf8"])
                        self.assertEqual(argv[5], entry.replace("\\", "/"))
                        # doctor reads the same from it: the shell it is for, or none when it reads alike
                        self.assertEqual(install.command_shell(cmd), shell if specific else None)
                        self.assertEqual(winshell.written_for(cmd), shell if specific else None)
                        self.assertEqual(winshell.reads_alike(cmd), not specific)
                        self.assertEqual(winshell.words(cmd, shell), argv)

    def test_quote_for_each_shell(self):
        self.assertEqual(winshell.quote("it's", "bash"), "'it'\"'\"'s'")
        self.assertEqual(winshell.quote("it's", "powershell"), "'it''s'")
        self.assertEqual(winshell.quote("\u2018x\u2019", "powershell"), "'\u2018\u2018x\u2019\u2019'")
        self.assertEqual(install.split_command("& " + winshell.quote("it's \u2018x\u2019", "powershell")), ["it's \u2018x\u2019"])

    def test_powershell_reading_rejects_what_it_would_not_read_as_written(self):
        for cmd in ("a 'b'c", "a \"b\"", "a b,c", "a @b", "a $b", "'unterminated"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(winshell.words("& " + cmd, "powershell"))
        self.assertEqual(install.split_command("& a 'b c' '' 'it''s'"), ["a", "b c", "", "it's"])

    def test_a_shell_change_rewrites_only_a_command_written_for_one_shell(self):
        with windows():
            for py, specific in ((PY, False), (PY_SPACE, True)):
                current = {"type": "command", "command": install.our_command(py, ENTRY, None, "bash"), "refreshInterval": 60}
                with self.subTest(py=py):
                    want, note = install.plan_statusline(current, py, ENTRY, shell="powershell")
                    if specific:
                        self.assertEqual(want["command"], install.our_command(py, ENTRY, None, "powershell"))
                        self.assertTrue(want["command"].startswith("& "))
                    else:
                        self.assertIsNone(want, note)

    def test_predicted_shell_follows_claude_codes_search(self):
        # What install records and doctor compares: the shell winshell predicts (the core's tests
        # cover the PowerShell side), from the process env with settings.json `env` over it.
        mine = r"D:\Git\bin\bash.exe"
        portable_git, portable_bash = r"D:\PortableGit\cmd\git.exe", r"D:\PortableGit\bin\bash.exe"
        cases = (  # (process env, settings env, existing files, shell, its path)
            ({}, None, (), "powershell", None),
            ({}, None, (GIT_BASH,), "bash", GIT_BASH),
            ({"CLAUDE_CODE_GIT_BASH_PATH": mine}, None, (mine,), "bash", mine),
            ({"CLAUDE_CODE_GIT_BASH_PATH": r"D:\tools\zsh.exe"}, None, (r"D:\tools\zsh.exe",), "powershell", None),
            ({"CLAUDE_CODE_GIT_BASH_PATH": mine}, None, (GIT_BASH,), "bash", GIT_BASH),  # missing: auto-detection
            ({}, {"CLAUDE_CODE_GIT_BASH_PATH": mine}, (mine,), "bash", mine),
            # settings.json `env` is laid over the process environment, so its value is the one Claude Code checks
            ({"CLAUDE_CODE_GIT_BASH_PATH": mine}, {"CLAUDE_CODE_GIT_BASH_PATH": r"E:\nope\bash.exe"}, (mine,), "powershell",
             None),
            ({"PATH": r"D:\PortableGit\cmd"}, None, (portable_git, portable_bash), "bash", portable_bash),
            ({"PATH": r"D:\PortableGit\cmd"}, None, (portable_git,), "powershell", None),
            ({"PATH": r"D:\PortableGit\cmd", "SYSTEMROOT": r"C:\Windows"}, None, (portable_git, PS51), "powershell", PS51),
        )
        for env, settings, present, shell, path in cases:
            with self.subTest(env=env, settings=settings, present=present):
                lower = {p.lower() for p in present}
                fake = lambda p: p.lower() in lower  # noqa: E731
                self.assertEqual(winshell.predict_shell(env, settings, exists=fake, is_file=fake), (shell, path))

    def test_shown_commands_use_double_quotes_on_windows(self):
        with windows():
            self.assertEqual(install._shown(["plugin", "marketplace", "add", r"C:\My Repo"]),
                             'plugin marketplace add "C:\\My Repo"')
        with windows(False):
            self.assertEqual(install._shown(["plugin", "marketplace", "add", "/my repo"]), "plugin marketplace add '/my repo'")

    def test_claude_bin_on_windows_takes_a_program_cmd_can_start(self):
        for found, want in (({"claude": r"C:\Users\me\.local\bin\claude.exe"}, r"C:\Users\me\.local\bin\claude.exe"),
                            ({"claude": r"C:\npm\claude", "claude.cmd": r"C:\npm\claude.cmd"}, r"C:\npm\claude.cmd"),
                            ({"claude.exe": r"C:\x\claude.exe", "claude.cmd": r"C:\npm\claude.cmd"}, r"C:\x\claude.exe"),
                            ({}, None)):
            with self.subTest(found=found), windows(), mock.patch("shutil.which", lambda n, *a, **kw: found.get(n)):
                self.assertEqual(install.claude_bin(), want)

    def test_launcher_cmd_text(self):
        with windows():
            text = install.launcher_text(r"C:\100%\python.exe", "C:/repo/bin/glidepath")
        self.assertEqual(text, '@echo off\r\nrem glidepath launcher\r\n(goto) 2>nul & "C:\\100%%\\python.exe" -I -S -X utf8 '
                               '"C:\\repo\\bin\\glidepath" %*\r\n')

    def test_the_cmd_launcher_ends_the_batch_on_the_python_line(self):
        # Review 15 (W-6): cmd.exe opened glidepath.cmd again after python returned, to read
        # `exit /b`, so an uninstall run through it ended with "The batch file cannot be found."
        # and a repin to a path of another length ran the middle of the new text as a command.
        # The python call is the last line, behind the (goto) that ends the batch first.
        for py, entry in ((PY, ENTRY), (PY_SPACE, ENTRY_SPACE), (r"C:\100%\python.exe", "C:/repo/bin/glidepath")):
            with self.subTest(py=py):
                with windows():
                    lines = install.launcher_text(py, entry).split("\r\n")
                self.assertEqual(lines[:2], ["@echo off", install.CMD_MARK])
                self.assertEqual(lines[3:], [""])  # nothing after the python line
                self.assertTrue(lines[2].startswith("(goto) 2>nul & "), lines[2])
                self.assertTrue(lines[2].endswith(" %*"), lines[2])
                self.assertNotIn("\n", "".join(lines))  # CRLF only
        # A launcher an older version wrote is still glidepath's, so a repin rewrites it.
        with tempfile.TemporaryDirectory() as d:
            for text in ('@echo off\r\nrem glidepath launcher\r\n"C:\\py\\python.exe" -I -S -X utf8 "C:\\repo\\bin\\glidepath" %*'
                         '\r\nexit /b %ERRORLEVEL%\r\n', install.launcher_text(PY, ENTRY), "@echo mine\r\n"):
                put(os.path.join(d, "glidepath.cmd"), text)
                self.assertEqual(install._is_launcher(os.path.join(d, "glidepath.cmd")), text != "@echo mine\r\n")

    def test_sh_launcher_text_for_git_bash(self):
        # Review 15 (W-4): Git Bash never looks for glidepath.cmd, so a `glidepath` sh launcher sits
        # beside it: forward slashes, sh quoting, the Windows flags, LF only.
        with windows():
            self.assertEqual(install.sh_launcher_text(PY, ENTRY),
                             '#!/bin/sh\n# glidepath launcher\nexec C:/Users/me/AppData/Local/Programs/Python/Python312/'
                             'python.exe -I -S -X utf8 C:/Users/me/Claude-Code-Mods/bin/glidepath "$@"\n')
            self.assertEqual(install.sh_launcher_text(PY_SPACE, r"C:\Users\Jürgen's\bin\glidepath"),
                             "#!/bin/sh\n# glidepath launcher\nexec 'C:/Program Files/Python312/python.exe' -I -S -X utf8 "
                             "'C:/Users/J\u00fcrgen'\"'\"'s/bin/glidepath' \"$@\"\n")
        with windows(False):  # the macOS and Linux launcher, as before
            self.assertEqual(install.launcher_text("/opt/my py/python3", "/x/bin/glidepath"),
                             "#!/bin/sh\n# glidepath launcher\nexec '/opt/my py/python3' -I -S /x/bin/glidepath \"$@\"\n")

    @unittest.skipUnless(os.name == "posix", "runs the text in /bin/sh")
    def test_sh_launcher_for_git_bash_runs_in_sh(self):
        # The Windows form, with paths that exist here: sh reads the quoting and passes the
        # arguments and the exit code through.
        with tempfile.TemporaryDirectory() as d:
            entry = os.path.join(d, "it's", "entry")
            os.makedirs(os.path.dirname(entry))
            put(entry, "import sys; print(sys.flags.isolated, sys.flags.no_site, sys.flags.utf8_mode, sys.argv[1:]);"
                       " sys.exit(3)\n")
            with windows():
                text = install.sh_launcher_text(sys.executable, entry)
            lp = os.path.join(d, "glidepath")
            with open(lp, "wb") as f:
                f.write(text.encode("utf-8"))
            p = subprocess.run(["/bin/sh", lp, "a b", "--x"], capture_output=True, encoding="utf-8")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (3, "1 1 1 ['a b', '--x']\n", ""))


class WindowsProbe(unittest.TestCase):
    def fake_run(self, reports, calls):
        """_run for the probe: candidate -> (exit code, sys.executable it reports)."""
        def run(argv, timeout, env=None, cwd=None):
            calls.append((argv, env))
            code, exe = reports[argv[0]]
            return code, json.dumps(["3.12", "3.45.1", exe]) + "\n" if code == 0 else "", "" if code == 0 else "stub"
        return run

    def test_the_probe_reports_the_interpreter_itself(self):
        ok, ver, sq, ms, note, exe = install.probe_full(sys.executable)
        self.assertTrue(ok, note)
        # On Windows the base interpreter: a venv's python.exe only starts it.
        base = getattr(sys, "_base_executable", None) or sys.executable
        self.assertTrue(os.path.samefile(exe, base if config.IS_WINDOWS else sys.executable))
        self.assertEqual(install.probe(sys.executable)[:3], (ok, ver, sq))  # doctor's 5 values
        with mock.patch.object(install, "_run", lambda *a, **kw: (0, "Python 3.12\n", "")):
            self.assertIn("unexpected probe output", install.probe(sys.executable)[4])

    def test_windows_pins_the_real_exe_behind_an_alias_or_a_launcher(self):
        real = PY
        alias = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.12.exe"
        store = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_x\python.exe"
        dotted = r"C:\Users\me\AppData\Local\Programs\Python\Python312\.\python.exe"
        reports = {alias: (0, real), r"C:\Windows\py.exe": (0, real), dotted: (0, real), store: (0, store),
                   real: (0, real), r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe": (9009, None)}
        calls = []
        with windows(), mock.patch.object(install, "_run", self.fake_run(reports, calls)), \
                mock.patch.object(install, "_runnable", lambda p: p in reports):
            for cand, pin in ((alias, real), (r"C:\Windows\py.exe", real), (dotted, dotted), (store, store)):
                with self.subTest(candidate=cand):
                    py, report = install.choose_python(cand)
                    self.assertEqual(py, pin)
                    self.assertEqual(report[0][0], pin)
                    self.assertEqual("via %s" % cand in report[0][5], pin != cand)
            # Never without arguments, and the install manager may not install a runtime on the way.
            self.assertTrue(all(argv[1:4] == ["-I", "-S", "-c"] for argv, env in calls))
            self.assertTrue(all(env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] == "0" for argv, env in calls))
            stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe"
            self.assertEqual(install.choose_python(stub)[0], None)
            # A Store Python only when nothing else passes.
            with mock.patch.object(install, "candidates", lambda: [stub, store, alias]):
                py, report = install.choose_python()
            self.assertEqual(py, real)
            self.assertIn("used only when nothing else passes", report[1][5])
            with mock.patch.object(install, "candidates", lambda: [stub, store]):
                self.assertEqual(install.choose_python()[0], store)
        calls.clear()
        with windows(False), mock.patch.object(install, "_run", self.fake_run({"/opt/py/bin/python3": (0, "/opt/py/bin/python3")}, calls)), \
                mock.patch.object(install, "_runnable", lambda p: True):
            install.probe_full("/opt/py/bin/python3")
        self.assertIsNone(calls[0][1])  # the environment as it is, off Windows

    def test_windows_pins_a_venvs_base_interpreter(self):
        # Review 15 verify: on Windows a venv's python.exe is a launcher that runs its base python.exe
        # as a child, so the process running glidepath had python.exe as its parent, not the shell, and the
        # Composer fell back to predicting the shell (winshell.parent_shell). glidepath runs -I -S: the venv
        # adds nothing, so its base interpreter is pinned, and the report says which venv it came from.
        venv = r"C:\Users\me\proj\.venv\Scripts\python.exe"

        def run(argv, timeout, env=None, cwd=None):
            """The probe, run here as a venv's interpreter: sys.executable is the venv's, and on
            Windows (any Python 3.8+) sys._base_executable is the python.exe it started."""
            buf = io.StringIO()
            with mock.patch.object(sys, "executable", argv[0]), \
                    mock.patch.object(sys, "_base_executable", PY if argv[0] == venv else argv[0], create=True), \
                    redirect_stdout(buf):
                exec(argv[-1], {})
            return 0, buf.getvalue(), ""

        with mock.patch.object(install, "_run", run), \
                mock.patch.object(install, "_runnable", lambda p: p in (venv, PY)):
            with windows():
                self.assertEqual(install.probe_full(venv)[5], PY)
                py, report = install.choose_python(venv)
                self.assertEqual((py, report[0][0], report[0][5]), (PY, PY, "via %s" % venv))
                with mock.patch.object(install, "candidates", lambda: [venv, PY]):
                    py, report = install.choose_python()
                self.assertEqual([(r[0], r[5]) for r in report], [(PY, "via %s" % venv), (PY, "")])
                self.assertEqual(py, PY)
                self.assertEqual(install.probe_full(PY)[5], PY)  # no venv: the interpreter itself
            with windows(False):  # macOS and Linux: as before, the venv's own path
                self.assertEqual(install.probe_full(venv)[5], venv)
                self.assertEqual(install.choose_python(venv)[0], venv)

    def test_windows_wants_a_drive_and_a_exe(self):
        with windows(), mock.patch("os.path.isfile", lambda p: True):
            self.assertTrue(install._runnable(PY))
            self.assertTrue(install._runnable(r"\\server\share\python.exe"))
            self.assertFalse(install._runnable(r"\Python312\python.exe"))  # Git Bash reads / as its own root
            self.assertFalse(install._runnable(r"C:\Python312\python"))
            self.assertFalse(install._runnable("python.exe"))
            self.assertEqual(install.probe(r"C:\x\python")[4], "not an absolute path to a .exe")

    def test_windows_candidates_in_order(self):
        tmp = tempfile.mkdtemp(prefix="glidepath-win-")
        self.addCleanup(shutil.rmtree, tmp, True)
        local, pf = os.path.join(tmp, "Local"), os.path.join(tmp, "Program Files")
        made = []
        for parts in (("Programs", "Python", "Python311"), ("Programs", "Python", "Python312"), ("Python", "pythoncore-3.14-64")):
            made.append(os.path.join(local, *parts, "python.exe"))
        made.append(os.path.join(pf, "Python 3.10", "python.exe"))
        for p in made:
            os.makedirs(os.path.dirname(p))
            put(p, "")
        core = {"3.12": {"InstallPath": {"": r"C:\Py312", "ExecutablePath": r"C:\Py312\python.exe"}},
                "3.13": {"InstallPath": {"": r"C:\Py313"}}, "3.9": {"Help": {}}}
        reg = FakeWinreg({("HKCU", 0): {"Software": {"Python": {
                              "ContinuumAnalytics": {"Anaconda3-64": {"InstallPath": {"ExecutablePath": r"C:\conda\python.exe"}}},
                              "PyLauncher": {"x": {"InstallPath": {"": r"C:\nope"}}}, "PythonCore": core}}},
                          ("HKLM", 2): {"Software": {"Python": {"PythonCore": {"3.11": {"InstallPath": {"": r"C:\Py311"}}}}}}})
        stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python.exe"
        with mock.patch.dict(sys.modules, {"winreg": reg}), \
                mock.patch("shutil.which", lambda n, *a, **kw: {"python": stub}.get(n)):
            got = install.windows_candidates({"LOCALAPPDATA": local, "PROGRAMFILES": pf})
        self.assertEqual(got, [os.path.join(r"C:\Py313", "python.exe"), r"C:\Py312\python.exe", r"C:\conda\python.exe",
                               os.path.join(r"C:\Py311", "python.exe"), made[1], made[0], made[3], made[2],
                               sys.executable, stub, None])

    def test_py_launcher_list(self):
        listing = (" -V:3.13 *        C:\\Users\\me\\AppData\\Local\\Programs\\Python\\Python313\\python.exe\n"
                   " -V:3.12          C:\\Program Files\\Python312\\python.exe\n"
                   " -V:ContinuumAnalytics/Anaconda3-64 C:/conda/python.exe\n")
        seen = []

        def run(argv, timeout, env=None, cwd=None):
            seen.append((argv, env))
            return 0, listing, ""

        with mock.patch("shutil.which", lambda n, *a, **kw: r"C:\Windows\py.exe" if n == "py" else None), \
                mock.patch.object(install, "_run", run):
            got = install._py_launcher_pythons({"PATH": "x"})
        self.assertEqual(got, [r"C:\Users\me\AppData\Local\Programs\Python\Python313\python.exe",
                               r"C:\Program Files\Python312\python.exe", "C:/conda/python.exe"])
        self.assertEqual(seen[0][0], [r"C:\Windows\py.exe", "-0p"])
        self.assertEqual(seen[0][1]["PYTHON_MANAGER_AUTOMATIC_INSTALL"], "0")


class WindowsInstall(Sandbox):
    """install / uninstall through the Windows branches."""

    def setUp(self):
        super().setUp()
        os.environ.pop("MSYSTEM", None)  # doctor in a Git Bash terminal says more; the Sandbox puts it back
        self.shell, self.shell_path = "bash", GIT_BASH
        win = windows()
        win.__enter__()
        self.addCleanup(win.__exit__, None, None, None)
        for p in (mock.patch.object(install, "entry_path", lambda: ENTRY),
                  mock.patch.object(install, "choose_python", self.fake_choose),
                  mock.patch.object(winshell, "predict_shell", self.fake_predict)):
            p.start()
            self.addCleanup(p.stop)

    def fake_predict(self, env=None, settings_env=None, *a, **kw):
        return self.shell, self.shell_path

    @staticmethod
    def fake_choose(explicit=None):
        py = explicit if explicit and explicit != sys.executable else PY
        return py, [(py, True, "3.12", "3.45.1", 20.0, "")]

    def test_portable_command_shell_record_runtime_and_cmd_launcher(self):
        before = self.write(SETTINGS)
        bindir = os.path.join(self.dir, "bin")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=bindir))
        self.assertEqual(rc, 0, out)
        self.assertIn("Claude Code runs it with Git Bash here; the command reads the same in Git Bash and PowerShell", out)
        self.assertEqual(json.loads(self.read())["statusLine"]["command"], FWD)
        self.assertEqual(install.load_state()["records"][self.path][winshell.RECORD_KEY], "bash")
        with open(install.config.runtime_path(), encoding="utf-8") as f:
            rt = json.load(f)
        self.assertEqual((rt["python"], rt["args"], rt["entry"]), (PY, ["-I", "-S", "-X", "utf8"], ENTRY))
        lp, sh = os.path.join(bindir, "glidepath.cmd"), os.path.join(bindir, "glidepath")
        with open(lp, "rb") as f:
            self.assertEqual(f.read(), install.launcher_text(PY, ENTRY).encode("utf-8"))
        with open(sh, "rb") as f:  # for Git Bash, which never looks for glidepath.cmd
            self.assertEqual(f.read(), install.sh_launcher_text(PY, ENTRY).encode("utf-8"))
        self.assertEqual((install.load_state()["launcher"], install.load_state()["sh_launcher"]), (lp, sh))
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(lp), out)
        self.assertFalse(os.path.exists(sh), out)
        self.assertEqual(self.read(), before)

    def test_a_shell_change_repins_a_command_written_for_one_shell(self):
        before = self.write(dict(SETTINGS, statusLine={"type": "command", "command": "echo 'mine'"}))
        self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
        cmd = json.loads(self.read())["statusLine"]["command"]
        self.assertEqual(install.command_shell(cmd), "bash")
        self.shell, self.shell_path = "powershell", PS51  # Git for Windows was uninstalled since
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
        self.assertIn("PowerShell (no Git Bash found)", out)
        cmd = json.loads(self.read())["statusLine"]["command"]
        self.assertTrue(cmd.startswith("& 'C:/Program Files/Python312/python.exe' -I -S -X utf8 "), cmd)
        self.assertEqual(install.compose_of(cmd), "echo 'mine'")
        self.assertEqual(install.load_state()["records"][self.path][winshell.RECORD_KEY], "powershell")
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)

    def test_a_shell_change_leaves_a_portable_command_and_updates_the_record(self):
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        once = self.read()
        self.shell, self.shell_path = "powershell", PS51
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertIn("already runs glidepath", out)
        self.assertEqual(self.read(), once)
        self.assertEqual(install.load_state()["records"][self.path][winshell.RECORD_KEY], "powershell")

    def doctor(self):
        from glidepath import commands

        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(install, "claude_bin", lambda: None):
            commands.cmd_doctor(argparse.Namespace(json=False, settings=self.path), config.load(os.devnull))
        return buf.getvalue()

    def test_doctor_reads_the_record_and_the_shell_names_install_writes(self):
        # A command for one shell (a quoted interpreter): doctor warns once Claude Code would use the other.
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
        self.assertIn("ok   statusLine shell: Git Bash  (%s)" % GIT_BASH, self.doctor())
        self.shell, self.shell_path = "powershell", PS51
        self.assertIn("WARN statusLine command is written for Git Bash, but Claude Code would run it in PowerShell",
                      self.doctor())
        self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))  # the repin doctor asks for
        out = self.doctor()
        self.assertIn("ok   statusLine shell: PowerShell  (%s)" % PS51, out)
        self.assertNotIn("written for", out)
        # A portable command: doctor names the shell install assumed, and never warns.
        self._reset()
        self.shell, self.shell_path = "bash", GIT_BASH
        self.write(SETTINGS)
        self.run_cmd(install.install, args(self.path, apply=True))
        self.shell, self.shell_path = "powershell", PS51
        self.assertIn("ok   statusLine shell: PowerShell  (%s; install assumed Git Bash, and the command reads the same "
                      "in both)" % PS51, self.doctor())

    def test_no_shell_at_all_is_said(self):
        self.shell, self.shell_path = "powershell", None
        self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path))
        self.assertIn("warning: no Git Bash and no PowerShell found", out)

    def test_settings_env_names_the_git_bash(self):
        # CLAUDE_CODE_GIT_BASH_PATH counts from the user file's env and the target file's own (which wins).
        seen = []
        proj = os.path.join(self.dir, "proj-settings.json")
        put(proj, json.dumps({"env": {"CLAUDE_CODE_GIT_BASH_PATH": r"E:\proj\bash.exe"}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}), \
                mock.patch.object(winshell, "predict_shell",
                                  lambda env=None, settings_env=None: seen.append(settings_env) or ("bash", GIT_BASH)):
            self.write({"env": {"CLAUDE_CODE_GIT_BASH_PATH": r"D:\user\bash.exe", "OTHER": "1"}})
            self.run_cmd(install.install, args(self.path))
            self.run_cmd(install.install, args(proj))
            self.path = proj
            self.doctor()  # doctor predicts with the same env as install
        self.assertEqual(seen[0]["CLAUDE_CODE_GIT_BASH_PATH"], r"D:\user\bash.exe")
        self.assertEqual(seen[1]["CLAUDE_CODE_GIT_BASH_PATH"], r"E:\proj\bash.exe")
        self.assertEqual(seen[1]["OTHER"], "1")
        self.assertEqual(seen[2], seen[1])

    def test_a_repin_moves_an_older_sh_launcher_to_glidepath_cmd(self):
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        old = os.path.join(bindir, "glidepath")
        put(old, "#!/bin/sh\n# glidepath launcher\nexec python3 x \"$@\"\n")  # what an older version wrote there
        os.makedirs(self.home)
        install.save_state({"launcher": old, "records": {}})
        self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        # The old glidepath is the sh launcher for Git Bash now (rewritten with LF), not a stale launcher.
        with open(old, "rb") as f:
            self.assertEqual(f.read(), install.sh_launcher_text(PY, ENTRY).encode("utf-8"))
        self.assertTrue(os.path.exists(os.path.join(bindir, "glidepath.cmd")), out)
        self.assertEqual(install.load_state()["launcher"], os.path.join(bindir, "glidepath.cmd"))
        self.assertEqual(install.load_state()["sh_launcher"], old)
        # A file of the user's in its place is theirs.
        put(os.path.join(bindir, "glidepath.cmd"), "@echo mine\r\n")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=bindir))
        self.assertIn("exists and is not ours", out)
        self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(install.read_text(os.path.join(bindir, "glidepath.cmd")), "@echo mine\r\n")
        self.assertFalse(os.path.exists(old))

    def test_the_sh_launcher_follows_glidepath_cmd(self):
        # Review 15 (W-4): `glidepath` was "command not found" in Git Bash and in Claude Code's Bash
        # tool. The sh launcher beside glidepath.cmd is repinned, moved and removed with it, and a
        # file of the user's named glidepath is never written over or removed.
        before = self.write(SETTINGS)
        a, b, c = (os.path.join(self.dir, n) for n in ("a", "b", "c"))
        other = r"C:\Python313\python.exe"

        def data(path):
            with open(path, "rb") as f:
                return f.read()

        def check(folder, python, names=("glidepath", "glidepath.cmd")):
            self.assertEqual(sorted(os.listdir(folder)), list(names))
            self.assertEqual(data(os.path.join(folder, "glidepath.cmd")), install.launcher_text(python, ENTRY).encode("utf-8"))
            if "glidepath" in names:
                self.assertEqual(data(os.path.join(folder, "glidepath")), install.sh_launcher_text(python, ENTRY).encode("utf-8"))

        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=a))
        self.assertEqual(rc, 0, out)
        self.assertIn("Launcher for Git Bash: %s" % os.path.join(a, "glidepath"), out)
        check(a, PY)
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=other))  # a repin: both follow
        self.assertEqual(rc, 0, out)
        check(a, other)
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=b))  # both move
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(a), [])
        check(b, PY)
        os.remove(os.path.join(b, "glidepath"))  # the user deleted it: only --bin-dir brings it back
        for python in (other, PY):  # Review 15 verify: the 2nd repin wrote it back
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=python))
            self.assertEqual(rc, 0, out)
            check(b, python, ("glidepath.cmd",))
            self.assertEqual(install.load_state()["sh_launcher"], os.path.join(b, "glidepath"))  # remembered
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=b))
        self.assertEqual(rc, 0, out)
        check(b, PY)
        os.makedirs(c)
        put(os.path.join(c, "glidepath"), "#!/bin/sh\necho mine\n")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=c))
        self.assertEqual(rc, 0, out)
        self.assertIn("Skipped launcher: %s exists and is not ours" % os.path.join(c, "glidepath"), out)
        self.assertEqual(os.listdir(b), [])
        self.assertNotIn("sh_launcher", install.load_state())
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(c), ["glidepath"])
        self.assertEqual(install.read_text(os.path.join(c, "glidepath")), "#!/bin/sh\necho mine\n")
        self.assertEqual(self.read(), before)

    def test_an_sh_launcher_kept_without_glidepath_cmd_follows_the_pin(self):
        # Review 15 verify: with glidepath.cmd deleted and the sh launcher kept, a repin (often because
        # the old Python was uninstalled) left Git Bash's `glidepath` running the old interpreter.
        before = self.write(SETTINGS)
        a, b = (os.path.join(self.dir, n) for n in ("a", "b"))
        sh_a, sh_b = os.path.join(a, "glidepath"), os.path.join(b, "glidepath")
        other = r"C:\Python313\python.exe"
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=a))
        self.assertEqual(rc, 0, out)
        os.remove(os.path.join(a, "glidepath.cmd"))  # the user deleted it and kept the sh launcher
        for python in (other, PY, other):
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=python))
            self.assertEqual(rc, 0, out)
            self.assertIn("Wrote launcher %s" % sh_a, out)
            self.assertEqual(os.listdir(a), ["glidepath"])  # glidepath.cmd stays deleted
            self.assertEqual(install.read_text(sh_a), install.sh_launcher_text(python, ENTRY))
            self.assertNotIn("launcher", install.load_state())
            self.assertEqual(install.load_state()["sh_launcher"], sh_a)
        rc, out = self.run_cmd(install.install, args(self.path, python=PY))
        self.assertIn("Launcher for Git Bash: %s" % sh_a, out)  # the dry run says so
        self.assertEqual(install.read_text(sh_a), install.sh_launcher_text(other, ENTRY))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=b))  # both, and it moves
        self.assertEqual(rc, 0, out)
        self.assertEqual((os.listdir(a), sorted(os.listdir(b))), ([], ["glidepath", "glidepath.cmd"]))
        self.assertEqual(install.load_state()["sh_launcher"], sh_b)
        # The same with a glidepath.cmd of the user's in place of glidepath's, which stays theirs.
        put(os.path.join(b, "glidepath.cmd"), "@echo mine\r\n")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=other))
        self.assertIn("Skipped launcher: %s exists and is not ours" % os.path.join(b, "glidepath.cmd"), out)
        self.assertEqual(install.read_text(sh_b), install.sh_launcher_text(other, ENTRY))
        self.assertEqual(install.read_text(os.path.join(b, "glidepath.cmd")), "@echo mine\r\n")
        # Also when --bin-dir names that folder again (Review 15 verify: it was left on the old pin).
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY, bin_dir=b))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(sh_b), install.sh_launcher_text(PY, ENTRY))
        self.assertEqual(install.read_text(os.path.join(b, "glidepath.cmd")), "@echo mine\r\n")
        # A file of the user's in place of the sh launcher is theirs: never written over, nor remembered.
        os.remove(os.path.join(b, "glidepath.cmd"))
        put(sh_b, "#!/bin/sh\necho mine\n")
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=other))
        self.assertEqual(rc, 0, out)
        self.assertIn("Skipped launcher: %s exists and is not ours" % sh_b, out)
        self.assertNotIn("sh_launcher", install.load_state())
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(b), ["glidepath"])
        self.assertEqual(install.read_text(sh_b), "#!/bin/sh\necho mine\n")
        self.assertEqual(self.read(), before)

    def test_a_deleted_sh_launcher_comes_back_with_a_bin_dir_only(self):
        # Review 15 verify: the repin after the user deleted it dropped its record, so the next
        # one wrote it back. It stays deleted however many repins follow, until a --bin-dir.
        self.write(SETTINGS)
        a, b = (os.path.join(self.dir, n) for n in ("a", "b"))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=a))
        self.assertEqual(rc, 0, out)
        os.remove(os.path.join(a, "glidepath"))
        for python in (r"C:\Python313\python.exe", PY, r"C:\Python313\python.exe"):
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=python))
            self.assertEqual(rc, 0, out)
            self.assertNotIn("Launcher for Git Bash", out)
            self.assertEqual(os.listdir(a), ["glidepath.cmd"])
            self.assertEqual(install.read_text(os.path.join(a, "glidepath.cmd")), install.launcher_text(python, ENTRY))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, bin_dir=b))  # a move brings it back
        self.assertEqual(rc, 0, out)
        self.assertEqual((os.listdir(a), sorted(os.listdir(b))), ([], ["glidepath", "glidepath.cmd"]))
        self.assertEqual(install.load_state()["sh_launcher"], os.path.join(b, "glidepath"))
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY))  # and it follows again
        self.assertEqual(install.read_text(os.path.join(b, "glidepath")), install.sh_launcher_text(PY, ENTRY))
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(os.listdir(b), [])

    def test_marketplace_add_falls_back_to_dot_slash(self):
        # Whether claude on Windows takes a drive path for a local marketplace is unconfirmed:
        # when it rejects one, the documented ./ form runs from the repo instead.
        root = install.repo_root()
        cli, calls = plugin_cli(self.path), []

        def picky(cmd, env=None, cwd=None):
            calls.append((cmd, cwd))
            if cmd[:3] == ["plugin", "marketplace", "add"] and cmd[3] != "./":
                return 1, "", "Invalid marketplace source format. Try: owner/repo, https://..., or ./path"
            return cli(cmd)

        before = self.write(SETTINGS)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir}), \
                mock.patch.object(install, "claude_bin", lambda: r"C:\x\claude.exe"), \
                mock.patch.object(install, "run_claude", picky):
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, no_mod=False))
            self.assertEqual(rc, 0, out)
            self.assertIn("retrying from", out)
            self.assertEqual(calls, [(["plugin", "marketplace", "add", root], None), (["plugin", "marketplace", "add", "./"], root),
                                     (["plugin", "install", install.PLUGIN_ID, "--scope", "user"], None)])
            self.assertIn(install.PLUGIN_ID, json.loads(self.read())["enabledPlugins"])
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True, no_mod=False))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)
        with windows(False), mock.patch.object(install, "claude_bin", lambda: "/bin/claude"), \
                mock.patch.object(install, "run_claude", picky):
            self.assertEqual(install._run_mod_command(install.mod_commands(root)[0], root, lambda s: None)[0], 1)

    def test_manual_commands_say_how_to_add_the_folder_if_claude_rejects_it(self):
        self.write(SETTINGS)
        with mock.patch.object(install, "claude_bin", lambda: None):
            rc, out = self.run_cmd(install.install, args(self.path, no_mod=False))
        self.assertIn("run claude plugin marketplace add ./", out)


@unittest.skipUnless(os.name == "nt", "runs glidepath.cmd in cmd.exe")
class CmdLauncherRuns(unittest.TestCase):
    """Review 15 (W-6): glidepath.cmd in a real cmd.exe while the program it runs rewrites or deletes
    it, as an install or uninstall run through it does. cmd.exe used to read the batch file again
    after python returned: "The batch file cannot be found.", or the middle of the new text run as
    a command ("is not recognized"), and the exit code was not python's."""

    STUB = ("import os, sys\n"
            "lp = os.environ['GLIDEPATH_TEST_LAUNCHER']\n"
            "if sys.argv[1:] == ['delete']:\n"
            "    os.remove(lp)\n"
            "elif sys.argv[1:] == ['rewrite']:\n"
            "    with open(os.environ['GLIDEPATH_TEST_NEXT'], 'rb') as f, open(lp, 'wb') as g:\n"
            "        g.write(f.read())\n"
            "sys.exit(3)\n")

    def test_the_exit_code_is_pythons_and_cmd_reads_nothing_after_it(self):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        others = (None, r"C:\a\much\longer\folder\of\another\interpreter\python.exe", r"C:\py\python.exe")
        for action, other in (("run", None), ("delete", None), ("rewrite", others[1]), ("rewrite", others[2])):
            with self.subTest(action=action, other=other), tempfile.TemporaryDirectory() as d:
                stub, lp, nxt = (os.path.join(d, n) for n in ("stub.py", "glidepath.cmd", "next.cmd"))
                put(stub, self.STUB)
                with open(lp, "wb") as f:
                    f.write(install._launcher_bytes(sys.executable, stub))
                with open(nxt, "wb") as f:
                    f.write(install._launcher_bytes(other or sys.executable, stub))
                env = dict(os.environ, GLIDEPATH_TEST_LAUNCHER=lp, GLIDEPATH_TEST_NEXT=nxt)
                p = subprocess.run([comspec, "/d", "/c", "glidepath.cmd", action], cwd=d, env=env, capture_output=True,
                                   timeout=60)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (3, b"", b""))
                self.assertEqual(os.path.exists(lp), action != "delete")


class ReadOnlySettings(Sandbox):
    """Review 15 (W-10): a read-only settings.json (attrib +R). POSIX replaces and removes it like
    any other file, and the new one keeps the mode; Windows refuses both (MoveFileEx, DeleteFile),
    so install and uninstall raised after 2 s of retries."""

    def tearDown(self):
        for folder, _, names in os.walk(self.dir):  # Windows cannot delete a read-only file
            for n in names:
                try:
                    os.chmod(os.path.join(folder, n), stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
        super().tearDown()

    def mode(self, path=None):
        return stat.S_IMODE(os.stat(path or self.path).st_mode)

    @contextlib.contextmanager
    def windows_rules(self):
        """Windows, on any OS: os.replace and os.remove refuse a read-only target (WinError 5),
        and the retries do not wait."""
        real_replace, real_remove = os.replace, os.remove

        def refuse(path):
            if os.path.exists(path) and not os.stat(path).st_mode & stat.S_IWRITE:
                raise PermissionError(13, "Access is denied", path)

        def replace(src, dst):
            refuse(dst)
            return real_replace(src, dst)

        def remove(path):
            refuse(path)
            return real_remove(path)

        with windows(), mock.patch("os.replace", replace), mock.patch("os.remove", remove), \
                mock.patch.object(install.time, "sleep", lambda s: None), \
                mock.patch.object(install, "choose_python", WindowsInstall.fake_choose), \
                mock.patch.object(install, "entry_path", lambda: ENTRY), \
                mock.patch.object(winshell, "predict_shell", lambda *a, **kw: ("bash", GIT_BASH)):
            yield

    def install_and_uninstall(self, rules=contextlib.nullcontext):
        text = IRREGULAR.replace("\n", "\r\n")
        put(self.path, text)
        os.chmod(self.path, 0o444)
        with rules():
            rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertTrue(install.is_ours(json.loads(self.read())["statusLine"]["command"]))
        self.assertEqual(self.mode(), 0o444)
        with rules():
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), text.encode("utf-8"))
        self.assertEqual(self.mode(), 0o444)
        # A file install created, made read-only since: uninstall removes it, as on POSIX.
        new = os.path.join(self.dir, "new", "settings.json")
        with rules():
            rc, out = self.run_cmd(install.install, args(new, apply=True))
        self.assertEqual(rc, 0, out)
        os.chmod(new, 0o444)
        with rules():
            rc, out = self.run_cmd(install.uninstall, args(new, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(new), out)

    def test_install_and_uninstall_on_windows_rules(self):
        self.install_and_uninstall(self.windows_rules)

    @unittest.skipUnless(os.name == "nt", "the read-only attribute as Windows enforces it")
    def test_install_and_uninstall_on_windows(self):
        self.install_and_uninstall()

    def test_the_attribute_comes_back_when_the_move_fails(self):
        put(self.path, IRREGULAR)
        for win in (True,) if os.name == "nt" else (True, False):  # off its rules, Windows keeps the read-only temp file
            with self.subTest(windows=win):
                os.chmod(self.path, 0o444)
                seen = []

                def held(src, dst):
                    seen.append(bool(os.stat(dst).st_mode & stat.S_IWRITE))
                    raise PermissionError(13, "in use by another process")

                with windows(win), mock.patch("os.replace", held), \
                        mock.patch.object(install.time, "sleep", lambda s: None):
                    with self.assertRaises(PermissionError):
                        install.atomic_write(self.path, "{}\n")
                # Writable for the move on Windows only, and read-only again after it failed.
                self.assertEqual(seen, [win] * (install.REPLACE_TRIES if win else 1))
                self.assertEqual(self.mode(), 0o444)
                self.assertEqual(self.read(), IRREGULAR)
                self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])


class WindowsFiles(Sandbox):
    """BOMs, held files and the subprocess helper, on any OS."""

    def test_a_bom_is_kept_and_the_file_comes_back_byte_for_byte(self):
        for text in (install.BOM + IRREGULAR, install.BOM, install.BOM + '{"statusLine": {"type": "command", "command": "x"}}'):
            with self.subTest(text=text):
                self._reset()
                put(self.path, text)
                rc, out = self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                with open(self.path, "rb") as f:
                    mid = f.read()
                self.assertTrue(mid.startswith(b"\xef\xbb\xbf"), mid)
                self.assertTrue(install.is_ours(install.parse_json(mid.decode("utf-8"))["statusLine"]["command"]))
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                with open(self.path, "rb") as f:
                    self.assertEqual(f.read(), text.encode("utf-8"))

    def test_a_bom_backup_is_read_when_the_record_is_lost(self):
        text = install.BOM + json.dumps(dict(SETTINGS, statusLine={"type": "command", "command": "~/mine.sh"}), indent=2,
                                        ensure_ascii=False)
        put(self.path, text)
        self.run_cmd(install.install, args(self.path, apply=True, replace=True))
        shutil.rmtree(self.home)  # install.json gone: only the backup knows
        rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(install.read_text(self.path), text)

    def test_install_json_with_a_bom(self):
        os.makedirs(self.home)
        with open(install.state_path(), "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"version": 2, "records": {}, "launcher": "x"}).encode())
        self.assertEqual(install.load_state()["launcher"], "x")

    def test_a_held_file_is_retried_on_windows_only(self):
        self.write(SETTINGS)
        real_replace = os.replace
        for win, fails, ok in ((True, 2, True), (True, install.REPLACE_TRIES, False), (False, 1, False)):
            with self.subTest(windows=win, fails=fails):
                tries = []

                def held(src, dst):
                    tries.append(dst)
                    if len(tries) <= fails:
                        raise PermissionError(13, "in use by another process")
                    return real_replace(src, dst)

                with windows(win), mock.patch("os.replace", held), \
                        mock.patch.object(install.time, "sleep", lambda s: None):
                    if ok:
                        install.atomic_write(self.path, "{}\n")
                    else:
                        with self.assertRaises(PermissionError):
                            install.atomic_write(self.path, "{}\n")
                self.assertEqual(len(tries), fails + 1 if ok else fails)
                self.assertEqual(self.read() == "{}\n", ok)
                self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])  # no temp file left
                self.write(SETTINGS)

    def test_run_decodes_utf8_and_stops_on_timeout(self):
        say = "import sys; sys.stdout.buffer.write(b'\\xe2\\x8f\\xba ok \\xff'); sys.stderr.buffer.write(b'\\xc3\\xa4')"
        for win in (False, True):
            with self.subTest(windows=win), windows(win), \
                    mock.patch.object(install, "claude_bin", lambda: sys.executable):
                self.assertEqual(install.run_claude(["-c", say]), (0, "\u23fa ok \ufffd", "\u00e4"))
                with mock.patch.object(install, "CLAUDE_TIMEOUT", 0.5):
                    t = time.monotonic()
                    code, so, se = install.run_claude(["-c", "import time; time.sleep(30)"])
                self.assertEqual(code, 124, se)
                self.assertIn("timed out after", se)
                self.assertLess(time.monotonic() - t, 4)  # killed, not waited for (the read gives up after 5 s)


SHORT_PATH = winshell.short_path  # the real one, for tests that feed it their own 8.3 names


class WindowsEncodedCompose(unittest.TestCase):
    """Review 15 (shells#1, shells#2, installer#3, journey#20, runtime#7): on Windows a
    composed command that Windows PowerShell 5.1 (a double quote, or whitespace and a final
    backslash) or Git Bash's MSYS2 path conversion (a POSIX path in its first word) would
    hand to python.exe changed goes as `--compose-b64 b<token>`; any other keeps the readable
    `--compose <command>`. Every reader of the command sees it plain."""

    CASES = (  # (the previous statusLine, written encoded)
        ('powershell -NoProfile -File "C:/Users/John Smith/sl.ps1"', True),
        ("/bin/bash ~/.claude/statusline.sh", True),
        ("FOO=/x bash ~/s.sh", True),
        ("node C:/x.js", False),
        ("~/.claude/statusline.sh", False),
        ("echo hi", False),
        ("echo J\u00fcrgen \u2192", False),
        ('printf "\u25b2 J\u00fcrgen"', True),
        ("dir C:\\Users\\me\\", True),
    )

    def test_each_shell_hands_python_the_command_as_written(self):
        from glidepath import statusline

        with windows():
            for compose, enc in self.CASES:
                for shell in ("bash", "powershell"):
                    with self.subTest(compose=compose, shell=shell):
                        cmd = install.our_command(PY, ENTRY, compose, shell)
                        tail = [winshell.COMPOSE_B64, winshell.encoded(compose)] if enc else ["--compose", compose]
                        self.assertEqual(install.split_command(cmd)[7:], tail)
                        self.assertTrue(winshell.reads_alike(cmd), cmd)
                        self.assertTrue(install.is_ours(cmd))
                        self.assertEqual(install.compose_of(cmd), compose)
                        for sh in ("bash", "powershell"):
                            words = winshell.words(cmd, sh)
                            # No word that Windows PowerShell 5.1 or Git Bash would hand to python.exe changed.
                            self.assertEqual([w for w in words if winshell.needs_encoding(w)], [], sh)
                            self.assertEqual(statusline.parse_args(words[7:])["compose_cmd"], compose)

    def test_macos_and_linux_never_encode(self):
        with windows(False):
            for compose, _ in self.CASES:
                cmd = install.our_command("/usr/bin/python3", "/x/bin/glidepath", compose)
                self.assertNotIn(winshell.COMPOSE_B64, cmd)
                self.assertEqual(install.split_command(cmd)[4:], ["statusline", "--compose", compose])
                self.assertEqual(install.compose_of(cmd), compose)
            # One written on Windows and synced here reads the same.
            cmd = "/usr/bin/python3 -I -S /x/bin/glidepath statusline --compose-b64 " + winshell.encoded('echo "x"')
            self.assertEqual(install.compose_of(cmd), 'echo "x"')

    def test_a_malformed_token_is_no_composed_command(self):
        with windows():
            for tail in (" --compose-b64 b!!", " --compose-b64", " --compose-b64 bZ", " --compose-b64 xYQ"):
                self.assertIsNone(install.compose_of(FWD + tail), tail)

    def test_a_repin_writes_an_older_plain_one_encoded_and_keeps_it(self):
        old = "/bin/bash ~/.claude/statusline.sh"
        with windows():
            want, _ = install.plan_statusline({"type": "command", "command": old}, PY, ENTRY, shell="bash")
            self.assertEqual(want["command"], FWD + " --compose-b64 " + winshell.encoded(old))
            self.assertEqual(install.plan_statusline(want, PY, ENTRY, shell="powershell"),
                             (None, "statusLine already runs glidepath"))
            plain = dict(want, command=FWD + " --compose '%s'" % old)  # what an older install wrote
            self.assertEqual(install.plan_statusline(plain, PY, ENTRY, shell="bash")[0], want)
            dropped, note = install.plan_statusline(want, PY, ENTRY, replace=True, shell="bash")
            self.assertEqual(dropped["command"], FWD)
            self.assertIn("REPLACE", note)

    def test_with_both_flags_every_reader_takes_the_last_one(self):
        # Review 15 verify: glidepath statusline runs the last --compose or --compose-b64, while install
        # read the first, so after a hand edit that added one, doctor showed another command than the
        # one running, and a repin wrote the old one back in its place.
        from glidepath import statusline

        def running(cmd):
            words = install.split_command(cmd)
            return statusline.parse_args(words[words.index("statusline") + 1:])["compose_cmd"]

        old, mine = "/usr/bin/env bash ~/.claude/sl.sh", "node ~/mine.js"
        with windows():
            first = install.our_command(PY, ENTRY, old, "bash")
            self.assertIn(winshell.COMPOSE_B64, first)
            for cmd, want in ((first + " --compose '%s'" % mine, mine),  # the user's, added after
                              (FWD + " --compose '%s' " % mine + first.split(" statusline ")[1], old),
                              (first + " --compose", None),  # a last flag with nothing after it composes nothing
                              (first + " --now x", None)):  # nor does a statusline that fails to read its flags
                with self.subTest(cmd=cmd):
                    try:
                        runs = running(cmd)
                    except ValueError:
                        runs = None  # glidepath statusline prints its bare line: no composed command runs
                    self.assertEqual((install.compose_of(cmd), runs), (want, want))
                    repin, _ = install.plan_statusline({"type": "command", "command": cmd}, PY_SPACE, ENTRY,
                                                       shell="bash")
                    self.assertEqual((install.compose_of(repin["command"]), running(repin["command"])), (want, want))
        with windows(False):
            posix = install.our_command("/usr/bin/python3", "/x/bin/glidepath", old) + " --compose '%s'" % mine
            self.assertEqual((install.compose_of(posix), running(posix)), (mine, mine))
            repin, _ = install.plan_statusline({"type": "command", "command": posix}, "/opt/py/bin/python3",
                                               "/x/bin/glidepath")
            self.assertEqual(install.split_command(repin["command"])[4:], ["statusline", "--compose", mine])

    def test_8dot3_short_paths_when_they_need_no_quotes(self):
        # Review 15 (journey#22): the folders' short names make a spaced path bare, so the
        # command reads the same in both shells; one that still needs quotes is not used.
        shorts = {r"C:\Program Files\Python312": r"C:\PROGRA~1\PYTHON~1",
                  r"C:\Users\me\My Repos\Claude-Code-Mods\bin": r"C:\Users\me\MYREPO~1\CLAUDE~1\bin"}
        with windows():
            with mock.patch.object(winshell, "short_path", lambda p: SHORT_PATH(p, shorts.get)):
                cmd = install.our_command(PY_SPACE, ENTRY_SPACE, "echo hi", "powershell")
                self.assertEqual(cmd, "C:/PROGRA~1/PYTHON~1/python.exe -I -S -X utf8 C:/Users/me/MYREPO~1/CLAUDE~1/bin/glidepath "
                                      "statusline --compose 'echo hi'")
                self.assertTrue(winshell.reads_alike(cmd))
                self.assertTrue(install.is_ours(cmd))
                self.assertEqual(install.our_command(PY, ENTRY, None, "bash"), FWD)  # a bare path stays as is
            for short in (lambda d: d, lambda d: r"C:\A&B~1", lambda d: None):  # short names off, not bare, none
                with mock.patch.object(winshell, "short_path", lambda p: SHORT_PATH(p, short)):
                    self.assertEqual(install.our_command(PY_SPACE, ENTRY, None, "bash"),
                                     "'C:/Program Files/Python312/python.exe' -I -S -X utf8 "
                                     "C:/Users/me/Claude-Code-Mods/bin/glidepath statusline")


class WindowsShellNotes(Sandbox):
    """Review 15: what install says on Windows about the command it writes, and
    uninstall after a composed command stored encoded."""

    def setUp(self):
        super().setUp()
        os.environ.pop("MSYSTEM", None)  # the Sandbox's patched environment puts it back
        self.shell, self.shell_path = "bash", GIT_BASH
        win = windows()
        win.__enter__()
        self.addCleanup(win.__exit__, None, None, None)
        for p in (mock.patch.object(install, "entry_path", lambda: ENTRY),
                  mock.patch.object(install, "choose_python", WindowsInstall.fake_choose),
                  mock.patch.object(winshell, "predict_shell", lambda *a, **kw: (self.shell, self.shell_path))):
            p.start()
            self.addCleanup(p.stop)

    def test_a_composed_command_stored_encoded_is_said_and_comes_back(self):
        for old, why in (('node "C:/Users/John Smith/sl.js"',
                          "Windows PowerShell 5.1 would pass its double quotes (or final backslash) to Python changed"),
                         ("/usr/bin/env bash ~/.claude/statusline.sh", "Git Bash would rewrite the POSIX path it starts with")):
            with self.subTest(old=old):
                self._reset()
                before = self.write(dict(SETTINGS, statusLine={"type": "command", "command": old}))
                rc, out = self.run_cmd(install.install, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(json.loads(self.read())["statusLine"]["command"],
                                 FWD + " --compose-b64 " + winshell.encoded(old))
                self.assertIn("note: your previous statusLine is stored base64-encoded after --compose-b64, since %s; "
                              "`glidepath doctor` shows it decoded" % why, out)
                self.assertIn("the command reads the same in Git Bash and PowerShell", out)
                self.assertNotIn("warning", out)
                rc, out = self.run_cmd(install.install, args(self.path, apply=True))
                self.assertIn("already runs glidepath", out)
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.read(), before)
                # With install.json and the backups lost, the command inside it is still the original.
                self.write(dict(SETTINGS, statusLine={"type": "command", "command": old}))
                self.run_cmd(install.install, args(self.path, apply=True))
                self._reset()
                rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
                self.assertEqual(rc, 0, out)
                self.assertEqual(json.loads(self.read())["statusLine"]["command"], old)

    def test_a_command_for_one_shell_says_when_to_reinstall(self):
        # Review 15 (journey#22): installing or removing Git for Windows later changes the shell.
        self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
        self.assertIn("warning: this command runs only in Git Bash; after installing or removing Git for Windows, or "
                      "setting CLAUDE_CODE_GIT_BASH_PATH, run `glidepath install --apply` again (`glidepath doctor` checks this)",
                      out)
        self.assertNotIn("Git Bash terminal", out)
        self.shell, self.shell_path = "powershell", PS51
        rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
        self.assertIn("warning: this command runs only in PowerShell;", out)
        # Review 15 (shells#0): in a Git Bash terminal, Git's own git comes first on PATH.
        os.environ["MSYSTEM"] = "MINGW64"
        rc, out = self.run_cmd(install.install, args(self.path, python=PY_SPACE))
        self.assertIn("note: Claude Code started from this Git Bash terminal can pick another shell than when started "
                      "from the Desktop app or PowerShell; run `glidepath doctor` from PowerShell to check", out)
        # A command that reads the same in both shells needs neither.
        self._reset()
        self.write(SETTINGS)
        rc, out = self.run_cmd(install.install, args(self.path, apply=True))
        self.assertNotIn("runs only in", out)
        self.assertNotIn("Git Bash terminal", out)

    def test_short_paths_go_in_the_command_and_long_ones_in_runtime_json(self):
        shorts = {r"C:\Program Files\Python312": r"C:\PROGRA~1\PYTHON~1",
                  r"C:\Users\me\My Repos\Claude-Code-Mods\bin": r"C:\Users\me\MYREPO~1\CLAUDE~1\bin"}
        before = self.write(SETTINGS)
        with mock.patch.object(winshell, "short_path", lambda p: SHORT_PATH(p, shorts.get)), \
                mock.patch.object(install, "entry_path", lambda: ENTRY_SPACE):
            rc, out = self.run_cmd(install.install, args(self.path, apply=True, python=PY_SPACE))
            self.assertEqual(rc, 0, out)
            self.assertEqual(json.loads(self.read())["statusLine"]["command"],
                             "C:/PROGRA~1/PYTHON~1/python.exe -I -S -X utf8 C:/Users/me/MYREPO~1/CLAUDE~1/bin/glidepath "
                             "statusline")
            self.assertIn("the command reads the same in Git Bash and PowerShell", out)
            self.assertNotIn("warning", out)
            with open(config.runtime_path(), encoding="utf-8") as f:
                rt = json.load(f)
            self.assertEqual((rt["python"], rt["entry"]), (PY_SPACE, ENTRY_SPACE))  # the mod runs argv, no shell
            rc, out = self.run_cmd(install.uninstall, args(self.path, apply=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(), before)


class WindowsReadme(unittest.TestCase):
    """Review 15 (journey#21, regress-ci#19, journey#24, journey#25): the docs had a Mac
    install only, whose commands fail in Windows PowerShell 5.1, cmd.exe and Git Bash. The
    Windows section (docs/install.md) must say what the code does there."""

    def section(self):
        with open(os.path.join(install.repo_root(), "docs", "install.md"), encoding="utf-8") as f:
            text = f.read()
        start = text.find("\n## Install on Windows\n")
        self.assertGreater(start, 0, "docs/install.md has no Windows install section")
        return text[start:text.find("\n## ", start + 1)]

    def test_the_example_command_is_the_one_install_writes(self):
        sec = self.section()
        example = re.search(r"`(C:/\S+/python\.exe -I -S -X utf8 C:/\S+/bin/glidepath statusline)`", sec).group(1)
        argv = example.split()
        short = re.search(r"`(C:/PROGRA~1/\S+/python\.exe)`", sec).group(1)
        shorts = {r"C:\Program Files\Python312": short.rsplit("/", 1)[0].replace("/", "\\")}
        with windows():
            for shell in ("bash", "powershell"):
                self.assertEqual(install.our_command(argv[0].replace("/", "\\"), argv[5].replace("/", "\\"), None, shell),
                                 example)
            self.assertTrue(winshell.reads_alike(example))
            with mock.patch.object(winshell, "short_path", lambda p: SHORT_PATH(p, shorts.get)):
                self.assertEqual(install.our_command(PY_SPACE, ENTRY, None, "bash").split()[0], short)

    def test_what_goes_encoded_and_the_home_forms_it_names(self):
        sec = self.section()
        for word in ("--compose-b64", "/usr/bin/env", "`~\\x`", "`C:\\x`", "`C:/x`", "`/c/x`", "`/srv/glidepath`"):
            self.assertIn(word, sec)
        self.assertTrue(winshell.needs_encoding('echo "mine"'))  # Windows PowerShell 5.1
        self.assertTrue(winshell.needs_encoding("/usr/bin/env bash ~/.claude/statusline.sh"))  # Git Bash
        self.assertFalse(winshell.needs_encoding("~/.claude/statusline.sh"))
        user = r"C:\Users\ana"
        with mock.patch.dict(os.environ, {"USERPROFILE": user}):
            for value, want in (("~\\x", user + "\\x"), ("C:\\x", "C:\\x"), ("C:/x", "C:\\x"), ("/c/x", "C:\\x"),
                                ("%USERPROFILE%\\x", user + "\\%USERPROFILE%\\x")):
                self.assertEqual(config.windows_home(value), want, value)
            with self.assertRaises(config.HomeError):
                config.windows_home("/srv/glidepath")
            self.assertIn("`C:x`", sec)  # refused by the CLI too since review 15 verify
            with self.assertRaises(config.HomeError):
                config.windows_home("C:x")

    def test_its_advice_holds_in_cmd_exe_and_after_a_hand_edit(self):
        # Review 15 verify (docs#2 to docs#4): a composed command put back by hand in plain form
        # stays at risk until an install encodes it again; Windows PowerShell 5.1 has no `&&`, and
        # cmd.exe passes `# ...` on as arguments; an unquoted %USERPROFILE% splits at a space.
        sec = " ".join(self.section().split())
        self.assertIn("put `--compose '<command>'` in its place, then run `glidepath install --apply`, which encodes "
                      "it again when a shell would change it (until then `glidepath doctor` warns)", sec)
        hand = FWD + " --compose '%s'" % 'node "C:/Users/John Smith/sl.js"'
        with windows():
            want, _ = install.plan_statusline({"type": "command", "command": hand}, PY, ENTRY, shell="bash")
        self.assertEqual(want["command"], FWD + " --compose-b64 " + winshell.encoded('node "C:/Users/John Smith/sl.js"'))
        self.assertIn('(`"%USERPROFILE%\\.local\\bin"`), since a profile folder may hold a space', sec)
        with open(os.path.join(install.repo_root(), "CONTRIBUTING.md"), encoding="utf-8") as f:
            block, note = re.search(r"\n## Tests\n\n```sh\n(.*?)```\n\n(.*?)\n\n", f.read(), re.S).groups()
        self.assertNotIn("&&", block)
        self.assertIn("in cmd.exe drop the `#` comments", note)

    def test_its_commands_run_in_windows_powershell(self):
        # Windows PowerShell 5.1 has no &&, and reads an unquoted comma as a list.
        sec = self.section()
        blocks = re.findall(r"```powershell\n(.*?)```", sec, re.S)
        self.assertGreaterEqual(len(blocks), 2)
        for block in blocks:
            self.assertNotIn("&&", block)
            self.assertNotIn("python3", block)
        self.assertIn("--tail '5h=60m,week=2d'", sec)


if __name__ == "__main__":
    unittest.main()


class ProbeFailure(unittest.TestCase):
    def test_python_2_says_it_is_too_old(self):
        # Review 18: the installer showed Python 2.7's last usage line,
        # "Try `python -h' for more information.", as the reason.
        se = "Unknown option: -I\nusage: /usr/bin/python [option] ... [-c cmd | -m mod | file | -] [arg] ...\n" \
             "Try `python -h' for more information.\n"
        self.assertIn("Python 3.8+ required", install._probe_failure(se))
        self.assertEqual(install._probe_failure("boom\nModuleNotFoundError: sqlite3\n"), "ModuleNotFoundError: sqlite3")
