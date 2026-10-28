"""winshell: the shell Claude Code runs the statusLine in on Windows, and
commands that read right there. Windows path rules on any OS, so this runs
everywhere; the tests marked "needs Windows" need a real one."""

import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from glidepath import config, winshell

GIT_DEFAULT = r"C:\Program Files\Git\bin\bash.exe"
PS51 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def fs(*paths):
    """An exists/is_file stand-in over these paths, case-insensitive like NTFS."""
    have = {p.lower() for p in paths}
    return lambda p: p.lower() in have


class PredictShell(unittest.TestCase):
    """The search order of Claude Code 2.1.300: CLAUDE_CODE_GIT_BASH_PATH, the
    two Program Files installs, git on PATH, then PowerShell (pwsh first)."""

    def predict(self, env, *files, settings_env=None):
        f = fs(*files)
        return winshell.predict_shell(env, settings_env, f, f)

    def test_git_bash_path_wins_when_it_names_bash_or_sh(self):
        for name in ("bash.exe", "sh.exe", "bash", "SH"):
            given = "D:\\tools\\git\\usr\\bin\\" + name
            self.assertEqual(self.predict({"CLAUDE_CODE_GIT_BASH_PATH": given}, given, GIT_DEFAULT), ("bash", given))
        # Not a bash or sh, or missing: the search goes on.
        for given in (r"D:\tools\zsh.exe", r"D:\missing\bash.exe"):
            self.assertEqual(self.predict({"CLAUDE_CODE_GIT_BASH_PATH": given}, r"D:\tools\zsh.exe", GIT_DEFAULT),
                             ("bash", GIT_DEFAULT))

    def test_settings_env_counts_over_the_process_env(self):
        given = r"D:\git\bin\bash.exe"
        env = {"claude_code_git_bash_path": r"D:\old\bash.exe"}  # names ignore case on Windows
        got = self.predict(env, given, r"D:\old\bash.exe", settings_env={"CLAUDE_CODE_GIT_BASH_PATH": given, "N": 1})
        self.assertEqual(got, ("bash", given))

    def test_program_files_then_x86_then_git_on_path(self):
        x86 = r"C:\Program Files (x86)\Git\bin\bash.exe"
        self.assertEqual(self.predict({}, GIT_DEFAULT, x86), ("bash", GIT_DEFAULT))
        self.assertEqual(self.predict({}, x86), ("bash", x86))
        env = {"Path": 'C:\\Windows\\System32;;"D:\\Apps\\Git\\cmd"', "PATHEXT": ".COM;.EXE"}
        self.assertEqual(self.predict(env, r"D:\Apps\Git\cmd\git.exe", r"D:\Apps\Git\bin\bash.exe"),
                         ("bash", r"D:\Apps\Git\bin\bash.exe"))
        # git without a bash two folders up: PowerShell.
        self.assertEqual(self.predict(env, r"D:\Apps\Git\cmd\git.exe", PS51), ("powershell", PS51))

    def test_git_in_the_current_directory_is_skipped(self):
        here = os.getcwd()
        env = {"PATH": here + ";D:\\Git\\cmd"}
        self.assertEqual(winshell.which("git", env, fs(ntpath.join(here, "git.EXE"))), None)
        self.assertEqual(winshell.which("git", env, fs(ntpath.join(here, "git.EXE"), r"D:\Git\cmd\git.exe")),
                         r"D:\Git\cmd\git.EXE")
        self.assertEqual(winshell.which("git", {"PATH": "D:\\a;D:\\b", "PATHEXT": ".CMD;.EXE"},
                                        fs(r"D:\a\git.exe", r"D:\b\git.cmd")), r"D:\a\git.EXE")

    def test_powershell_order(self):
        env = {"PATH": r"C:\bin;C:\Windows\System32\WindowsPowerShell\v1.0", "ProgramFiles": r"C:\Program Files",
               "LOCALAPPDATA": r"C:\Users\me\AppData\Local", "USERPROFILE": r"C:\Users\me", "SystemRoot": r"C:\Windows"}
        on_path = r"C:\bin\pwsh.exe"
        pf = r"C:\Program Files\PowerShell\7\pwsh.exe"
        apps = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe"
        dotnet = r"C:\Users\me\.dotnet\tools\pwsh.exe"
        self.assertEqual(self.predict(env, on_path, pf, PS51), ("powershell", r"C:\bin\pwsh.EXE"))  # PATHEXT's case
        self.assertEqual(self.predict(env, pf, apps, PS51), ("powershell", pf))
        self.assertEqual(self.predict(env, apps, dotnet, PS51), ("powershell", apps))
        self.assertEqual(self.predict(env, dotnet, PS51), ("powershell", dotnet))
        self.assertEqual(self.predict(env, PS51), ("powershell", PS51[:-3] + "EXE"))  # found on PATH
        self.assertEqual(self.predict({"SystemRoot": r"C:\Windows"}, PS51), ("powershell", PS51))
        self.assertEqual(self.predict({}), ("powershell", None))


class ShellArgv(unittest.TestCase):
    """The Composer runs the old command as Claude Code would: never cmd.exe."""

    def argv(self, cmd, env, kind, path):
        with mock.patch.object(winshell, "predict_shell", return_value=(kind, path)):
            return winshell.shell_argv(cmd, env)

    def test_bash_puts_its_folder_first_on_path(self):
        argv, env = self.argv("printf x", {"Path": r"C:\Windows", "A": "1"}, "bash", GIT_DEFAULT)
        self.assertEqual(argv, [GIT_DEFAULT, "-c", "printf x"])
        self.assertEqual(env, {"Path": r"C:\Program Files\Git\bin;C:\Windows", "A": "1"})
        argv, env = self.argv("x", {}, "bash", "bash")  # a relative path is left off PATH, as Claude Code does
        self.assertEqual((argv, env), (["bash", "-c", "x"], {}))

    def test_bash_runs_a_script_first_word_with_bash(self):
        for cmd in ("~/sl.sh --x", "'C:/my dir/sl.sh'", "C:/my\\ dir/sl.sh a"):
            self.assertEqual(self.argv(cmd, {}, "bash", GIT_DEFAULT)[0][2], "bash " + cmd)
        self.assertEqual(self.argv("sl.sh.exe", {}, "bash", GIT_DEFAULT)[0][2], "sl.sh.exe")

    def test_powershell_flags_and_placeholders(self):
        argv, _ = self.argv("node ${CLAUDE_PROJECT_DIR}/x.js ${env:CLAUDE_PLUGIN_ROOT}", {}, "powershell", PS51)
        self.assertEqual(argv, [PS51, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                "node ${env:CLAUDE_PROJECT_DIR}/x.js ${env:CLAUDE_PLUGIN_ROOT}"])
        argv, _ = self.argv("x", {"CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY": "1"}, "powershell", None)
        self.assertEqual(argv, ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "x"])

    def test_the_shell_given_wins_over_the_prediction(self):
        # Review 15 (shells#0): the Composer passes the shell that started glidepath.
        mine = r"D:\Git\bin\bash.exe"
        with mock.patch.object(winshell, "predict_shell", side_effect=AssertionError("not asked")):
            argv, env = winshell.shell_argv("printf x", {"PATH": r"C:\Windows"}, ("bash", mine))
            self.assertEqual((argv, env), ([mine, "-c", "printf x"], {"PATH": r"D:\Git\bin;C:\Windows"}))
            argv, _ = winshell.shell_argv("x", {}, ("powershell", PS51))
            self.assertEqual(argv[0], PS51)
        self.assertEqual(self.argv("x", {}, "powershell", PS51)[0][0], PS51)  # None: the prediction

    def test_first_word_reads_quotes_and_escapes_as_claude_code(self):
        self.assertEqual(winshell.first_word('  "C:/my dir/x.sh" a'), "C:/my dir/x.sh")
        self.assertEqual(winshell.first_word("a\\ b.sh c"), "a b.sh")
        self.assertEqual(winshell.first_word("'open"), "open")


class Quoting(unittest.TestCase):
    PY = "C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe"
    ENTRY = "C:/Users/me/Claude-Code-Mods/bin/glidepath"
    TRICKY = ["", " ", "a b", "it's", "\u2019x\u2018", 'say "hi"', "$HOME `x` ${y} %PATH% !", "a\\b\\", "tab\there",
              "semi;colon&amp|pipe", "~/x", "@a,b", "C:/Program Files/x.exe", "J\u00fcrgen", "en\u2013dash", "''"]

    def test_bare_paths_read_the_same_in_both_shells(self):
        argv = [self.PY, "-I", "-S", "-X", "utf8", self.ENTRY, "statusline"]
        cmd = winshell.command_line(argv, "bash")
        self.assertEqual(cmd, " ".join(argv))
        self.assertEqual(winshell.command_line(argv, "powershell"), cmd)
        self.assertTrue(winshell.reads_alike(cmd))
        self.assertIsNone(winshell.written_for(cmd))
        # Non-ASCII letters need no quotes either.
        self.assertTrue(winshell.reads_alike(winshell.command_line(["C:/Users/J\u00fcrgen/py.exe", "-I"], "bash")))

    def test_a_quoted_first_word_needs_the_call_operator_in_powershell(self):
        argv = ["C:/Program Files/Python312/python.exe", "-I", "-S", "-X", "utf8", self.ENTRY, "statusline"]
        b, p = winshell.command_line(argv, "bash"), winshell.command_line(argv, "powershell")
        self.assertEqual(b, "'C:/Program Files/Python312/python.exe' -I -S -X utf8 %s statusline" % self.ENTRY)
        self.assertEqual(p, "& " + b)
        self.assertEqual((winshell.written_for(b), winshell.written_for(p)), ("bash", "powershell"))
        self.assertIsNone(winshell.words(b, "powershell"))  # a string, not a command, in PowerShell
        self.assertIsNone(winshell.words(p, "bash"))

    def test_compose_argument_reads_alike_without_a_single_quote(self):
        argv = [self.PY, self.ENTRY, "statusline", "--compose", 'powershell -NoProfile -File "C:/x y/sl.ps1"']
        cmd = winshell.command_line(argv, "bash")
        self.assertEqual(cmd, winshell.command_line(argv, "powershell"))
        self.assertTrue(winshell.reads_alike(cmd))
        argv[-1] = "echo it's"
        b, p = winshell.command_line(argv, "bash"), winshell.command_line(argv, "powershell")
        self.assertTrue(b.endswith("""'echo it'"'"'s'"""), b)
        self.assertTrue(p.endswith("'echo it''s'"), p)
        self.assertEqual(winshell.written_for(b), "bash")
        self.assertIsNone(winshell.written_for(p))  # bash reads other words there: only the record can tell

    def test_every_word_comes_back(self):
        for shell in ("bash", "powershell"):
            for w in self.TRICKY:
                for argv in ([self.PY, w], [w, "-I"], [self.PY, w, w + w]):
                    cmd = winshell.command_line(argv, shell)
                    self.assertEqual(winshell.words(cmd, shell), argv, (shell, cmd))
        self.assertEqual(winshell.quote("\u2019x", "powershell"), "'\u2019\u2019x'")
        self.assertEqual(winshell.command_line([".\\x", "a"], "powershell"), "& '.\\x' a")
        self.assertEqual(winshell.command_line(["-x", "a"], "powershell"), "& -x a")

    def test_bare_words(self):
        for w in ("C:/x/y.exe", "-I", "utf8", "--compose", "a+b=c:d_e.f", "\u00fc\u4e2d"):
            self.assertTrue(winshell.is_bare(w), w)
        for w in ("", "a b", "C:\\x", "~", "$x", "a,b", "@a", "%x%", "\u2013", "\u2019", "\u00a0", "a;b", "{x}"):
            self.assertFalse(winshell.is_bare(w), w)
        self.assertEqual(winshell.slashes(r"C:\Users\me\x"), "C:/Users/me/x")

    def test_a_tilde_inside_an_8dot3_name_is_bare(self):
        # Review 15 (journey#22): C:/PROGRA~1/... and JOHNSM~1 need no quotes, as neither shell
        # expands a ~ after a letter or digit; one that starts a word or follows a symbol does.
        for w in ("C:/PROGRA~1/PYTHON~1/python.exe", "JOHNSM~1", "a~", "9~1~2"):
            self.assertTrue(winshell.is_bare(w), w)
        for w in ("~", "~/x", "C:/x/~1", "x=~", "a:~", "-~", ".~", "_~", "\u00fc~"):
            self.assertFalse(winshell.is_bare(w), w)
        argv = ["C:/PROGRA~1/PYTHON~1/python.exe", "-I", "C:/Users/JOHNSM~1/repo/bin/glidepath", "statusline"]
        cmd = winshell.command_line(argv, "bash")
        self.assertEqual(cmd, " ".join(argv))
        self.assertEqual(winshell.command_line(argv, "powershell"), cmd)
        self.assertEqual(winshell.words(cmd, "bash"), argv)
        self.assertEqual(winshell.words(cmd, "powershell"), argv)
        self.assertTrue(winshell.reads_alike(cmd))
        self.assertIsNone(winshell.written_for(cmd))
        # A ~ the bash reading meets after a quote, or at a word's start, is outside the simple form.
        for cmd in ("x 'a'~1", "x ~1", "x a/~"):
            self.assertIsNone(winshell.words(cmd, "bash"), cmd)
        self.assertEqual(winshell.quote("~/x", "bash"), "'~/x'")

    def test_what_powershell_51_passes_intact(self):
        self.assertTrue(winshell.passes_intact("printf 'a b' C:\\x"))
        self.assertTrue(winshell.passes_intact("C:\\x\\"))  # no whitespace: not wrapped in quotes
        self.assertFalse(winshell.passes_intact('echo "hi"'))
        self.assertFalse(winshell.passes_intact("dir C:\\"))

    def test_outside_the_simple_form(self):
        for cmd in ("echo $(date)", "a | b", "'open", "x 'a'b", "x \"$y\"", "&x"):
            self.assertIsNone(winshell.words(cmd, "powershell"), cmd)
        for cmd in ("echo $(date)", "'open", "& x", "x \"a$b\""):
            self.assertIsNone(winshell.words(cmd, "bash"), cmd)
        self.assertFalse(winshell.reads_alike(""))

    def test_python_flags(self):
        self.assertEqual(winshell.python_flags(False), ["-I", "-S"])
        self.assertEqual(winshell.python_flags(True), ["-I", "-S", "-X", "utf8"])
        with mock.patch.object(config, "IS_WINDOWS", True):
            self.assertEqual(winshell.python_flags(), ["-I", "-S", "-X", "utf8"])


class ComposeEncoding(unittest.TestCase):
    """Review 15 (shells#1, shells#2): a composed command Windows PowerShell 5.1
    (a double quote, or whitespace and a final backslash) or Git Bash's MSYS2
    path conversion (a POSIX path in its first word) would hand to python.exe
    changed goes as one encoded word every shell passes on as is."""

    def test_what_msys2_would_convert(self):
        for arg in ("/bin/bash ~/.claude/statusline.sh", "/usr/bin/env bash x", "/c/x/node.exe a", "FOO=/x bash ~/s.sh",
                    "PATH=/usr/bin:$PATH x", '"/bin/bash" x', "$(/bin/x)", "ab:/c x", "C:/x:/y", "C:/x;D:/y z", "/",
                    "//server/share/x"):
            self.assertTrue(winshell.msys_converts(arg), arg)
        for arg in ("~/.claude/statusline.sh", "~/x=/y", "node C:/x.js", "echo /usr/x", " /bin/bash x", "echo hi",
                    "C:/nodejs/node.exe C:/x.js", "C:\\x\\node.exe a", "bin/sl.sh --x=/y", "./sl.sh", "../x/y.sh",
                    "$HOME/.claude/sl.sh", "bash", "", "\u00fcber/x.sh"):
            self.assertFalse(winshell.msys_converts(arg), arg)

    def test_what_needs_encoding(self):
        for arg in ('powershell -NoProfile -File "C:/Users/John Smith/sl.ps1"', "dir C:\\", "/bin/bash ~/x.sh",
                    "FOO=/x bash ~/s.sh", 'echo "J\u00fcrgen"'):
            self.assertTrue(winshell.needs_encoding(arg), arg)
        for arg in ("node C:/x.js", "~/.claude/statusline.sh", "echo hi", "echo it's", "C:\\x\\", "echo J\u00fcrgen \u2192"):
            self.assertFalse(winshell.needs_encoding(arg), arg)

    def test_encoded_round_trip(self):
        for cmd in ('powershell -NoProfile -File "C:/Users/John Smith/sl.ps1"', "/bin/bash ~/x.sh", "dir C:\\",
                    "echo \u25b2 \u2192 J\u00fcrgen \U0001f916", "a", "ab", "abc", "x" * 1000):
            word = winshell.encoded(cmd)
            self.assertRegex(word, r"\Ab[A-Za-z0-9_-]*\Z")
            self.assertTrue(winshell.is_bare(word))
            self.assertTrue(winshell.passes_intact(word))
            self.assertFalse(winshell.msys_converts(word))
            self.assertEqual(winshell.decoded(word), cmd)

    def test_a_malformed_word_decodes_to_nothing(self):
        for word in (None, "", "b", "x" + winshell.encoded("echo hi")[1:], "bZ", "b!!!!", "bYQ==", "b" + "_" * 4,
                     winshell.encoded("echo hi") + " ", 42):
            self.assertIsNone(winshell.decoded(word), word)


class ShortPath(unittest.TestCase):
    """Review 15 (journey#22): a path that needs quotes may have an 8.3 short
    form that needs none, so the command reads the same in both shells."""

    def test_folders_go_short_and_the_file_name_stays(self):
        seen = []

        def short(p):
            seen.append(p)
            return r"C:\PROGRA~1\PYTHON~1"

        with mock.patch.object(config, "IS_WINDOWS", True):
            self.assertEqual(winshell.short_path("C:/Program Files/Python312/python.exe", short),
                             r"C:\PROGRA~1\PYTHON~1\python.exe")
            self.assertEqual(seen, [r"C:\Program Files\Python312"])
            self.assertIsNone(winshell.short_path(r"C:\x y\a.exe", lambda p: None))
            self.assertIsNone(winshell.short_path(r"C:\x y\a.exe", mock.Mock(side_effect=OSError)))
            if os.name != "nt":
                self.assertIsNone(winshell.short_path(r"C:\x y\a.exe"))  # no kernel32 here: None, never an error
        with mock.patch.object(config, "IS_WINDOWS", False):
            self.assertIsNone(winshell.short_path(r"C:\x y\a.exe", short))

    @unittest.skipUnless(os.name == "nt", "needs Windows")
    def test_a_real_folder(self):
        # The folder of this interpreter exists; its short form names the same file.
        got = winshell.short_path(sys.executable)
        self.assertIsNotNone(got)
        self.assertTrue(os.path.samefile(got, sys.executable), got)
        self.assertEqual(ntpath.basename(got), ntpath.basename(sys.executable))


class ParentShell(unittest.TestCase):
    """Review 15 (shells#0): the Composer runs the old command in the shell that
    started glidepath, where Claude Code ran the statusLine. Inside Git Bash the
    PATH puts Git's own git first, so repeating Claude Code's search there can
    end in PowerShell."""

    def parent(self, image, *files):
        with mock.patch.object(config, "IS_WINDOWS", True):
            return winshell.parent_shell(lambda pid: image, fs(*files))

    def test_bash_sh_and_powershell(self):
        usr = r"D:\Git\usr\bin\bash.exe"
        self.assertEqual(self.parent(usr, r"D:\Git\bin\bash.exe"), ("bash", r"D:\Git\bin\bash.exe"))
        self.assertEqual(self.parent(usr), ("bash", usr))  # MSYS2 has no bin\bash.exe: as is
        self.assertEqual(self.parent(r"D:\Git\USR\BIN\sh.exe", r"D:\Git\bin\sh.exe"), ("bash", r"D:\Git\bin\sh.exe"))
        self.assertEqual(self.parent(GIT_DEFAULT, GIT_DEFAULT), ("bash", GIT_DEFAULT))
        self.assertEqual(self.parent(r"C:\msys64\usr\bin\BASH.EXE"), ("bash", r"C:\msys64\usr\bin\BASH.EXE"))
        pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"
        self.assertEqual(self.parent(pwsh), ("powershell", pwsh))
        self.assertEqual(self.parent(PS51), ("powershell", PS51))

    def test_anything_else_is_none(self):
        for image in (r"C:\Windows\System32\cmd.exe", r"C:\Python312\python.exe", r"C:\x\claude.exe", r"C:\x\zsh.exe",
                      "", None):
            self.assertIsNone(self.parent(image), image)
        with mock.patch.object(config, "IS_WINDOWS", True):
            self.assertIsNone(winshell.parent_shell(mock.Mock(side_effect=OSError("access denied"))))
            if os.name != "nt":
                self.assertIsNone(winshell.parent_shell())  # no kernel32 here: None, never an error
        reader = mock.Mock(return_value=GIT_DEFAULT)
        with mock.patch.object(config, "IS_WINDOWS", False):
            self.assertIsNone(winshell.parent_shell(reader))
        reader.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "needs Windows")
    def test_a_child_of_each_shell_sees_it(self):
        tmp = tempfile.mkdtemp(prefix="glidepath-parent-")
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, "child.py")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(script, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.path.insert(0, %r)\nfrom glidepath import winshell\n"
                    "print('%%s|%%s' %% (winshell.parent_shell() or (None, None)))\n" % root)
        argv = [winshell.slashes(sys.executable), winshell.slashes(script)]
        env = dict(os.environ)
        bash = r"C:\Program Files\Git\bin\bash.exe"
        bash = bash if os.path.exists(bash) else winshell.git_bash(env)
        runs = [("bash", bash, [bash, "-c", winshell.command_line(argv, "bash")])] if bash else []
        ps51 = ntpath.join(winshell.getenv(env, "SYSTEMROOT") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe")
        for ps in (winshell.which("pwsh", env), ps51):
            if ps and os.path.isfile(ps):
                runs.append(("powershell", ps, [ps, "-NoProfile", "-NonInteractive", "-Command",
                                                winshell.command_line(argv, "powershell")]))
        self.assertTrue(runs)
        for kind, shell, cmd in runs:
            with self.subTest(shell=shell):
                p = subprocess.run(cmd, capture_output=True, timeout=120)
                got = p.stdout.decode("utf-8", "replace").strip()
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(got.split("|")[0], kind, got)
                path = got.split("|")[-1]
                if kind == "bash":  # its bin\bash.exe runs usr\bin\bash.exe: mapped back to the one started
                    self.assertEqual(ntpath.normcase(path), ntpath.normcase(shell), got)
                else:  # a pwsh on PATH may be an alias of the real one
                    self.assertEqual(ntpath.basename(path).lower(), ntpath.basename(shell).lower(), got)


class RealGitBash(unittest.TestCase):
    """Review 15 (regress-ci#18): a check that does not lean on the code under
    test, so a prediction that misses Git Bash fails here instead of skipping
    the compose tests that need it."""

    @unittest.skipUnless(os.name == "nt", "needs Windows")
    def test_finds_the_installed_git_bash(self):
        if not os.path.exists(r"C:\Program Files\Git\bin\bash.exe"):
            self.skipTest("no Git for Windows")
        self.assertEqual(winshell.predict_shell()[0], "bash")


class KillTree(unittest.TestCase):
    def test_no_taskkill_is_a_false(self):
        self.assertFalse(winshell.kill_tree(2 ** 22, {"SYSTEMROOT": os.path.join(os.sep, "nowhere")}))

    @unittest.skipUnless(os.name == "nt", "needs Windows")
    def test_kills_a_process_and_its_children(self):
        code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"
        p = subprocess.Popen([sys.executable, "-c", code])
        time.sleep(1.0)
        self.assertTrue(winshell.kill_tree(p.pid))
        self.assertIsNotNone(p.wait(10))


if __name__ == "__main__":
    unittest.main()
