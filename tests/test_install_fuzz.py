"""Model-based fuzzing of install / uninstall on throwaway settings files.

Random sequences (fixed seeds, so every run is the same) of installs into the
user file or a project file, uninstalls (some with --no-mod), claude coming
and going, the user running the plugin commands glidepath printed by hand,
the data folder wiped (install.json and runtime.json lost), and the user
editing either file between steps, the command inside glidepath's --compose
included. A model tracks what each file holds for the user: its first content
with the user's edits applied. After every step, while a mod record remains,
runtime.json exists (the mod reads it). Once claude is back and everything is
uninstalled, glidepath must have left no trace: no records, no mod, no
runtime.json, each file's JSON is the user's (with the user's own statusLine
and no glidepath plugin keys), and a file the user never edited is byte for byte
what it was, or still absent. Where only install.json knew and the folder was
wiped, or the plugin CLI run by hand rewrote the user file out of glidepath's
sight, a check is narrowed for exactly that case (see World.run and World.step).

A failure prints the seed and the steps. To replay one seed with each step's
output: GLIDEPATH_FUZZ_SEED=<seed> python3 -m unittest tests.test_install_fuzz
"""

import io
import json
import os
import random
import re
import shlex
import shutil
import sys
import tempfile
import traceback
import unittest
from contextlib import redirect_stdout
from unittest import mock

from glidepath import config, install, winshell

from . import test_install as ti

SEQUENCES = 400

# The user's own statusLine, in hand-formatted text.
WITH_STATUSLINE = ('{\n  "model": "opus",\n  "statusLine": {"type": "command", "command": "~/bin/line.sh", "padding": 1},\n'
                   '    "env": {"FOO": "bär"}\n}\n')
INITIAL = {
    "absent": None,
    "blank": "",
    "irregular": ti.IRREGULAR,
    "crlf": ti.IRREGULAR.replace("\n", "\r\n"),
    "statusline": WITH_STATUSLINE,
    "statusline-crlf": WITH_STATUSLINE.replace("\n", "\r\n"),
    "plugins": ti.WITH_PLUGINS,
}
CLAUDE = ("ok", "missing", "hang")
# What the user puts inside glidepath's --compose (the last one is their own statusLine's).
COMPOSE = ("~/bin/other.sh", "echo 'hi there'", "~/bin/line.sh")


def hang(cmd, env=None, cwd=None):
    return 124, "", "claude %s timed out after 120 s" % " ".join(cmd[:3])


def by_hand(user, cmds):
    """The user runs plugin commands; like the real CLI, one with nothing to do writes nothing."""
    cli = ti.plugin_cli(user)
    for c in cmds:
        key = "extraKnownMarketplaces" if c[1] == "marketplace" else "enabledPlugins"
        held = load(install.read_text(user)).get(key) if os.path.exists(user) else None
        listed = isinstance(held, dict) and install.OWN_ENTRY[key] in held
        if listed == (c[1] == "uninstall" or c[2] == "remove"):
            cli(c)


def lists_mod(text):
    obj = load(text)
    return any(isinstance(obj.get(k), dict) and install.OWN_ENTRY[k] in obj[k] for k in install.PLUGIN_KEYS)


def fast_python(explicit=None):
    """The interpreter probe is not under test here, and it is slow."""
    return sys.executable, [(sys.executable, True, "3", "3", 1.0, "")]


def quoted(cmd, argv):
    """argv written back as the user would edit it, in the syntax cmd is written in: sh
    quoting, or on Windows PowerShell's (`& '...'`) when glidepath wrote that."""
    if install.command_shell(cmd) == "powershell":
        return "& " + " ".join(a if a.startswith("-") or a in ("utf8", "statusline") else winshell.quote(a, "powershell")
                               for a in argv)
    return " ".join(shlex.quote(a) for a in argv)


def load(text):
    """A settings file as the user sees it: absent and blank are {}."""
    return json.loads(text) if text and text.strip() else {}


def generate(seed):
    """(initial file kinds, claude at the start, steps) for one seed."""
    r = random.Random(seed)
    initial = {"U": r.choice(sorted(INITIAL)), "P": r.choice(sorted(INITIAL))}
    claude = r.choice(("ok", "ok", "missing", "hang"))
    steps = []
    for i in range(r.randint(4, 12)):
        x = r.random()
        f = r.choice("UP")
        if x < 0.32:
            steps.append("i" + f + ("-nomod" if r.random() < 0.3 else "") + ("-replace" if r.random() < 0.2 else ""))
        elif x < 0.55:
            steps.append("u" + r.choice("UUPP*") + ("-nomod" if r.random() < 0.3 else ""))
        elif x < 0.67:
            steps.append("claude=" + r.choice(CLAUDE))
        elif x < 0.74:
            steps.append(r.choice(("mod-by-hand", "unmod-by-hand")))
        elif x < 0.78:
            steps.append("wipe")
        else:
            what = r.choice(("key", "key", "reformat", "delete", "blank", "compose", "compose"))
            if what == "key":
                what = "k%d=%d" % (r.randint(0, 2), i)
            elif what == "compose":
                what = "compose=%d" % r.randrange(len(COMPOSE))
            steps.append("edit %s %s" % (f, what))
    return initial, claude, steps


class World:
    """One sequence: its temp files, the claude in use, and the model."""

    def __init__(self, root, initial, claude):
        self.paths = {"U": os.path.join(root, "claude", "settings.json"),
                      "P": os.path.join(root, "proj", ".claude", "settings.json")}
        # Files whose bytes are not promised (their JSON still is): the user edited them, the
        # data folder was wiped while glidepath or the plugin CLI had changed them (their
        # baselines went with install.json), or the plugin CLI run by hand rewrote the user
        # file where glidepath could not tell it from a user's reformat (see step and run).
        self.initial, self.user, self.nobytes = {}, {}, set()
        for f, kind in initial.items():
            os.makedirs(os.path.dirname(self.paths[f]))
            self.initial[f] = INITIAL[kind]
            if INITIAL[kind] is not None:
                self.write(f, INITIAL[kind])
            self.user[f] = load(INITIAL[kind])  # what the file holds for the user
        self.claude = claude
        self.trace, self.mid = [], []
        # The data folder was wiped since the last install (wiped), or ever (lost).
        self.wiped = self.lost = False
        # Narrowed, where only install.json knew and it was wiped (see step): files whose
        # statusLine is not checked, the backups each file had when the user deleted it, and
        # the user file's plugin keys that may be left empty.
        self.blind, self.deleted, self.empty_keys = set(), {}, set()
        self.hand_text = None  # the user file as the reverse plugin commands run by hand left it
        self.unseen = False  # the user file's plugin keys went (deleted or blanked), and glidepath has not looked since

    def text(self, f):
        return install.read_text(self.paths[f]) if os.path.exists(self.paths[f]) else None

    def write(self, f, text):
        with open(self.paths[f], "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def run(self, name, fn, target, **kw):
        exe, cli = {"ok": ("/bin/claude", ti.plugin_cli(self.paths["U"])), "missing": (None, ti.claude_missing),
                    "hang": ("/bin/claude", hang)}[self.claude]
        if fn is install.install and self.hand_text is not None and self.text("U") == self.hand_text:
            # The plugin CLI, run by hand, rewrote the user file out of glidepath's sight, so
            # this install may take it as is for the file's baseline (D-18: the file as it
            # is then, its own backup), the plugin CLI's formatting included.
            self.nobytes.add("U")
        buf = io.StringIO()
        with mock.patch.object(install, "claude_bin", lambda: exe), mock.patch.object(install, "run_claude", cli), \
                redirect_stdout(buf):
            rc = fn(ti.args(target, apply=True, **kw))
        self.trace.append((name, rc, buf.getvalue()))
        if (fn is install.uninstall or not kw.get("no_mod")) and "error:" not in buf.getvalue():
            self.unseen = False  # it looked at the user file's plugin keys
        for f in "UP" if self.lost else ():
            # After a wipe, with no backup from before glidepath left, nothing tells whether the
            # refreshInterval beside a command restored from --compose was glidepath's: it stays.
            sl, want = load(self.text(f)).get("statusLine"), self.user[f].get("statusLine")
            if isinstance(want, dict) and "refreshInterval" not in want and sl == dict(want, refreshInterval=60) \
                    and not any(self.before_glidepath(b) for b in self.backups(f)):
                self.user[f]["statusLine"] = sl
        return rc

    def ours(self, f):
        sl = load(self.text(f)).get("statusLine")
        return isinstance(sl, dict) and install.is_ours(sl.get("command"))

    def backups(self, f):
        folder, name = os.path.split(os.path.realpath(self.paths[f]))
        return {os.path.join(folder, n) for n in os.listdir(folder) if n.startswith(name + ".glidepath-backup-")}

    def before_glidepath(self, backup):
        sl = load(install.read_text(backup)).get("statusLine")
        return not (isinstance(sl, dict) and install.is_ours(sl.get("command")))

    def loose_empty_keys(self):
        """The user file's empty plugin keys that the user does not have may stay or go."""
        obj = load(self.text("U"))
        self.empty_keys.update(k for k in install.PLUGIN_KEYS if obj.get(k) == {} and k not in self.user["U"])

    def step(self, s):
        if s.startswith("claude="):
            self.claude = s.split("=")[1]
        elif s.startswith("edit "):
            _, f, what = s.split()
            self.edit(f, what)
        elif s.endswith("-by-hand"):
            # The commands glidepath printed, at install or at an uninstall that failed. Only
            # while it has a mod record: run while it has none (after a wipe), the reverse
            # commands leave the plugin CLI's empty keys behind, and nothing tells glidepath.
            if "mod" in install.load_state():
                before = self.text("U")
                by_hand(self.paths["U"], install.mod_commands(install.repo_root()) if s == "mod-by-hand"
                        else install.unmod_commands())
                if s == "mod-by-hand" and self.unseen and self.text("U") != before \
                        and not install.load_state()["mod"].get("manual"):
                    # The plugin CLI wrote glidepath's entries into keys the user removed (deleted or
                    # blanked the file) before glidepath looked, and the mod is not manual, so glidepath
                    # takes the CLI's writes for its own. It next finds keys that hold only its
                    # entries, as the user's own keys do after the user removed their other plugins
                    # with the plugin CLI (D-18, review 8): it cannot tell the two apart and keeps
                    # them as the user's, so they may stay empty.
                    self.empty_keys.update(k for k in install.PLUGIN_KEYS if k not in self.user["U"])
                if s == "unmod-by-hand" and self.text("U") != before:
                    self.hand_text = self.text("U")
                    if install.load_state()["mod"].get("manual"):
                        # glidepath never saw the plugin CLI write there (the mod listed only between
                        # the two by-hand runs), so it never rewrites the file (D-18): the CLI's
                        # formatting stays, as a reformat by the user would.
                        self.nobytes.add("U")
                    if self.unseen:
                        # The plugin CLI wrote the keys the user removed and emptied them again
                        # before glidepath looked (D-18: when glidepath next looks): an empty one is
                        # the user's for all it can tell, as at a wipe below.
                        self.loose_empty_keys()
        elif s == "wipe":  # the user deletes the data folder: install.json and runtime.json go
            for f in "UP":
                if self.text(f) != self.initial[f]:
                    self.nobytes.add(f)  # its baselines went with install.json
                if f in self.deleted and self.ours(f) and \
                        not any(self.before_glidepath(b) for b in self.backups(f) - self.deleted[f]):
                    # The user deleted it after its newest backup from before glidepath, and install
                    # created it again (no file, no backup): only install.json knew, so uninstall
                    # takes the statusLine from that older backup.
                    self.blind.add(f)
            # A plugin key the reverse commands run by hand left empty: for all glidepath can tell
            # now, it is the user's, until the plugin CLI writes glidepath's entry there again and
            # it is not (D-18), so it may stay empty or go.
            self.loose_empty_keys()
            shutil.rmtree(config.home(), ignore_errors=True)
            self.wiped = self.lost = True
        else:
            fn = install.install if s[0] == "i" else install.uninstall
            self.run(s, fn, self.paths.get(s[1]), no_mod="-nomod" in s, replace="-replace" in s)
        # runtime.json goes with a wiped data folder, and only an install writes it: a mod
        # record that a failed uninstall rebuilt from the user file (kept for the retry)
        # has none until the next install.
        if os.path.exists(config.runtime_path()):
            self.wiped = False
        elif "mod" in install.load_state() and not self.wiped:
            self.mid.append("after %s: a mod record, but no runtime.json" % s)

    def edit(self, f, what):
        """The user edits the file by hand; the model follows."""
        text = self.text(f)
        if what.startswith("compose="):  # the command inside glidepath's --compose: now the user's statusLine
            if not self.ours(f):
                return
            obj, x = load(text), COMPOSE[int(what.split("=")[1])]
            cmd = obj["statusLine"]["command"]
            argv = install.split_command(cmd)
            if winshell.COMPOSE_B64 in argv[:-1]:  # stored encoded (Windows): the user writes it out plainly
                i = argv.index(winshell.COMPOSE_B64)
                del argv[i:i + 2]
            if "--compose" in argv:
                argv[argv.index("--compose") + 1] = x
            else:
                argv += ["--compose", x]
            obj["statusLine"] = dict(obj["statusLine"], command=quoted(cmd, argv))
            self.write(f, install.dumps(obj, install.detect_format(text)))
            prev = self.user[f].get("statusLine")
            self.user[f]["statusLine"] = dict(prev if isinstance(prev, dict) else {"type": "command"}, command=x)
        elif what == "delete":
            if text is None:
                return
            os.remove(self.paths[f])
            self.user[f] = {}
            self.deleted[f] = self.backups(f)
            self.blind.discard(f)
            self.unseen = self.unseen or f == "U"
        elif what == "blank":
            self.write(f, "")
            self.user[f] = {}
            self.blind.discard(f)
            self.unseen = self.unseen or f == "U"
        elif what == "reformat":  # same JSON
            if not (text and text.strip()):
                return
            self.write(f, json.dumps(json.loads(text), indent=4, ensure_ascii=False) + "\n")
        else:  # add or change a key glidepath has nothing to do with
            key, value = what.split("=")
            obj = load(text)
            eol = "\r\n" if text and "\r\n" in text else "\n"
            if key in obj:
                new = re.sub(r'("%s":\s*)-?\d+' % key, r"\g<1>" + value, text, count=1)
            elif obj:
                new = text.replace("{", "{" + eol + '  "%s": %s,' % (key, value), 1)
            else:
                new = "{" + eol + '  "%s": %s' % (key, value) + eol + "}" + eol
            self.write(f, new)
            self.user[f][key] = int(value)
        self.nobytes.add(f)

    def finish(self):
        """Claude back, then uninstall whatever is installed until nothing is: after a
        wipe, also each file still running glidepath, and the mod the user file lists."""
        self.claude = "ok"
        for _ in range(4):
            state = install.load_state()
            live = sorted(install.installed_records(state))
            known = {os.path.realpath(p) for p in live}
            stray = [self.paths[f] for f in "UP" if self.ours(f) and os.path.realpath(self.paths[f]) not in known]
            if not live and not stray and "mod" not in state and not lists_mod(self.text("U")):
                return
            for target in live + stray or [None]:
                self.run("cleanup u%s" % ("*" if target is None else "UP"[target == self.paths["P"]]),
                         install.uninstall, target, no_mod=False)

    def problems(self):
        out = list(self.mid)
        state = install.load_state()
        if state.get("records"):
            out.append("records left: %s" % state["records"])
        if "mod" in state:
            out.append("mod left: %s" % state["mod"])
        if os.path.exists(config.runtime_path()):
            out.append("runtime.json left")
        for f in "UP":
            text = self.text(f)
            try:
                got = load(text)
            except ValueError:
                got = "invalid JSON"
            want = self.user[f]
            if f == "U" and isinstance(got, dict):
                got = {k: v for k, v in got.items() if not (k in self.empty_keys and v == {} and k not in want)}
            if f in self.blind and isinstance(got, dict):  # its statusLine only needs to be the user's, not glidepath's
                sl = got.get("statusLine")
                if isinstance(sl, dict) and install.is_ours(sl.get("command")):
                    out.append("%s still runs glidepath: %r" % (f, sl))
                got, want = ({k: v for k, v in o.items() if k != "statusLine"} for o in (got, want))
            if got != want:
                out.append("%s holds %r, the user's is %r" % (f, got, want))
            if f not in self.nobytes and text != self.initial[f]:
                out.append("%s should be byte for byte %r, but is %r" % (f, self.initial[f], text))
        return out


def run_sequence(seed):
    """(initial, claude, steps, problems, trace) for one seed, in its own temp dirs."""
    initial, claude, steps = generate(seed)
    root = tempfile.mkdtemp(prefix="glidepath-fuzz-")
    env = {"GLIDEPATH_HOME": os.path.join(root, "home"), "CLAUDE_CONFIG_DIR": os.path.join(root, "claude"),
           "HOME": os.path.join(root, "userhome"), "USERPROFILE": os.path.join(root, "userhome")}
    problems, world = [], None
    try:
        with mock.patch.dict(os.environ, env), mock.patch.object(install, "choose_python", fast_python):
            world = World(root, initial, claude)
            try:
                for s in steps:
                    world.step(s)
                world.finish()
                problems += ["%s exited %r" % (name, rc) for name, rc, _ in world.trace if rc not in (0, 1)]
                problems += world.problems()
            except Exception:
                problems.append("raised: " + traceback.format_exc().strip().splitlines()[-1])
                problems.append(traceback.format_exc())
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return initial, claude, steps, problems, world.trace if world else []


def describe(seed, initial, claude, steps, problems, trace=()):
    return "seed %d: U %s, P %s, claude %s\n  steps: %s\n  exits: %s\n  %s" % (
        seed, initial["U"], initial["P"], claude, ", ".join(steps),
        ", ".join("%s %d" % (name, rc) for name, rc, _ in trace), "\n  ".join(problems))


class Fuzz(unittest.TestCase):
    def test_random_sequences_leave_no_trace(self):
        replay = os.environ.get("GLIDEPATH_FUZZ_SEED")
        seeds = [int(replay)] if replay else range(SEQUENCES)
        failures = []
        for seed in seeds:
            initial, claude, steps, problems, trace = run_sequence(seed)
            if replay:
                for name, rc, out in trace:
                    print("== %s -> %d\n%s" % (name, rc, out))
            if problems:
                failures.append(describe(seed, initial, claude, steps, problems, trace))
        self.assertFalse(failures, "%d of %d sequences failed (replay one with GLIDEPATH_FUZZ_SEED=<seed>):\n%s" % (
            len(failures), len(seeds), "\n".join(failures[:10])))


if __name__ == "__main__":
    unittest.main()
