#!/usr/bin/env python3
"""glidepath end to end, the way Claude Code runs it, in throwaway folders.

    python scripts/ci_integration.py [--no-marketplace] [--keep]

CI runs it on Windows and macOS (.github/workflows/tests.yml); it runs on
Linux too. GLIDEPATH_HOME, CLAUDE_CONFIG_DIR, HOME and USERPROFILE point into
a temporary folder for every step: the real ones are never read or written.

1. Report: the shell Claude Code would run the statusLine with here
   (winshell.predict_shell(), which matters on Windows, and must say Git
   Bash when Git for Windows is in its default folder) and the installer's
   interpreter report (install.choose_python()).
2. For each starting settings.json (no statusLine; CRLF line endings; a
   statusLine of the user's to compose with, in plain form and, where a
   shell would change it on the way to Python, encoded; glidepath and its
   interpreter in folders with a space, where Windows may use their 8.3
   short paths; the interpreter is a copy of this Python installation on
   Windows, a venv elsewhere): `python bin/glidepath install --apply
   --settings <tmp>/settings.json --no-bootstrap --no-mod`, then the
   statusLine command is read back and a statusLine payload with rate
   limits is piped into it through each shell Claude Code may use on this
   OS, with COLUMNS set (Windows: Git Bash's `bash.exe -c`, and pwsh and
   powershell.exe with -NoProfile -NonInteractive -ExecutionPolicy Bypass
   -Command; elsewhere `/bin/sh -c`). Each must exit 0 and print the glidepath
   line with its UTF-8 intact. A command that reads the same in every shell
   must work in every one; one written for a single shell (a quoted
   interpreter on Windows), in that shell, which must be the one Claude
   Code would use. Then `glidepath uninstall --apply`, after which
   settings.json must be byte for byte what it was.
3. Which forms of this repo's path `claude plugin marketplace add` accepts
   (absolute; with forward slashes on Windows; ./ with the repo as the
   working directory), each in its own temporary config, and whether
   `claude plugin install` works from the first accepted one. Reported
   only: this part never fails the run.
4. Windows: the launchers `install --bin-dir` writes, with that folder on
   PATH: glidepath.cmd from `cmd /d /c`, pwsh and powershell.exe, and the sh
   glidepath beside it from Git Bash (`bash -lc`) run --version, check (exit
   1), an install that repins to this interpreter by a path of another
   length and an uninstall, which rewrite and delete the launcher while it
   runs. Each must exit with glidepath's code, and cmd.exe must not say "The
   batch file cannot be found." or "is not recognized".
5. A read-only settings.json (attrib +R on Windows): install and uninstall
   go through, and it comes back byte for byte, still read-only.
6. Windows: a GLIDEPATH_HOME rooted with a slash and no drive (/srv/glidepath-ci)
   in the settings env. What Git Bash hands python.exe for it is reported
   only; `install --apply` must refuse it up front ("has no drive", exit 1),
   with settings.json byte for byte what it was and no glidepath home made.
7. Windows: `install --python` with a venv's python.exe pins the venv's
   base interpreter, and its report names the venv.

Exit 1 when a check in 1, 2, 4, 5, 6 or 7 fails.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)

from glidepath import winshell  # noqa: E402

WINDOWS = os.name == "nt"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
PS_FLAGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OURS = re.compile(r"\b5h [━╍─┈]+ 42%.* Week [━╍─┈]+ 17%")  # the bars line, its bars in UTF-8
COLUMNS = "120"  # Claude Code sets COLUMNS from its terminal
SEP = "\u00b7"  # the line's separator: not ASCII, so it shows whether UTF-8 came through intact
FAILURES = []
ROWS = []  # (scenario, shell, result) for the GitHub step summary

REPORT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from glidepath import config, install, winshell
print("winshell.predict_shell(): %r" % (winshell.predict_shell(),))
if not config.IS_WINDOWS:
    print("  (Windows only: here Claude Code runs the statusLine with /bin/sh -c)")
elif hasattr(install, "windows_candidates"):
    print("install.windows_candidates(): %r" % (install.windows_candidates(),))
chosen, report = install.choose_python()
print("install.choose_python():")
for row in report:
    path, ok, ver, sq, ms, note = row[:6]
    print("  %s %s: %s%s%s" % ("*" if path == chosen else ("+" if ok else "-"), path,
                               ("Python %s, sqlite %s" % (ver, sq)) if ver else "failed",
                               (", %.0f ms" % ms) if ms else "", (" (%s)" % note) if note else ""))
print("  chosen: %s" % chosen)
"""


def fail(msg):
    FAILURES.append(msg)
    print("FAIL: " + msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::error::" + msg.replace("\n", " "))


def show(text, prefix="    | "):
    for line in text.rstrip().splitlines():
        print(prefix + line)


def sandbox(tmp):
    """The environment of a user whose every folder is under tmp."""
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env.update(GLIDEPATH_HOME=os.path.join(tmp, "glidepath"), CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"),
               HOME=home, USERPROFILE=home, COLUMNS=COLUMNS,
               DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


def hidden():
    """Claude Code spawns the statusLine with windowsHide: no console window."""
    if not WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def family(shell):
    return "powershell" if shell in ("pwsh", "powershell") else shell


def shells(env):
    """(name, argv before the command, env) of each shell Claude Code may run the statusLine
    with here that exists (report() fails the run for a missing one)."""
    if not WINDOWS:
        return [("sh", ["/bin/sh", "-c"], env)]
    found = []
    if os.path.exists(GIT_BASH):
        e = dict(env)  # Claude Code puts Git Bash's folder first on PATH
        e["PATH"] = os.path.dirname(GIT_BASH) + os.pathsep + e.get("PATH", "")
        found.append(("bash", [GIT_BASH, "-c"], e))
    for name in ("pwsh", "powershell"):  # Claude Code prefers pwsh
        exe = shutil.which(name, path=env.get("PATH"))
        if exe:
            found.append((name, [exe] + PS_FLAGS, env))
    return found


def shell_versions(env):
    found = shells(env)
    for name, argv, e in found:
        if name == "sh":
            print("  sh: /bin/sh -> %s" % os.path.realpath("/bin/sh"))
            continue
        probe = "echo $BASH_VERSION" if name == "bash" else "$PSVersionTable.PSVersion.ToString()"
        try:
            p = subprocess.run(argv + [probe], capture_output=True, env=e, timeout=60)
            print("  %s: %s (%s)" % (name, p.stdout.decode("utf-8", "replace").strip(), argv[0]))
        except (OSError, subprocess.SubprocessError) as ex:
            print("  %s: %r (%s)" % (name, ex, argv[0]))
    want = ["bash", "pwsh", "powershell"] if WINDOWS else ["sh"]
    for name in want:
        if name not in [f[0] for f in found]:
            fail("%s not found (%s)" % (name, GIT_BASH if name == "bash" else "PATH"))


def glidepath(entry, env, args):
    p = subprocess.run([sys.executable, entry] + args, env=env, capture_output=True, timeout=600)
    print("  $ python %s %s -> exit %d" % (entry, " ".join(args), p.returncode))
    show((p.stdout + p.stderr).decode("utf-8", "replace"))
    return p.returncode


def render(argv, cmd, env):
    """The command run as Claude Code runs it: the payload and a newline on stdin as UTF-8,
    stdout read as UTF-8, trimmed, split into lines, each trimmed, blank ones dropped."""
    now = time.time()
    data = json.dumps({
        "session_id": "ci-integration",
        "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
        "workspace": {"current_dir": ROOT, "project_dir": ROOT},
        "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": int(now + 2 * 3600)},
            "seven_day": {"used_percentage": 17, "resets_at": int(now + 3 * 86400)},
        },
    }) + "\n"
    t = time.perf_counter()
    try:
        p = subprocess.run(argv + [cmd], input=data.encode("utf-8"), capture_output=True, env=env, timeout=120,
                           **hidden())
    except subprocess.TimeoutExpired as e:  # reported as a failed render, and the run goes on
        p = subprocess.CompletedProcess(e.cmd, -9, e.stdout or b"", (e.stderr or b"") + b"\n(timed out after 120 s)")
    secs = time.perf_counter() - t
    try:
        text, utf8 = p.stdout.decode("utf-8"), True
    except UnicodeDecodeError:
        text, utf8 = p.stdout.decode("utf-8", "replace"), False
    lines = [ANSI.sub("", s).strip() for s in text.strip().split("\n")]
    return p, [s for s in lines if s], utf8, secs


def problems_of(p, lines, utf8, extra):
    ours = next((s for s in lines if OURS.search(s)), None)
    out = []
    if p.returncode != 0:
        out.append("exit %d" % p.returncode)
    if ours is None:
        out.append("no glidepath line (5h [bar] 42% ... Week [bar] 17%)")
    elif not utf8 or SEP not in ours:
        out.append("UTF-8 garbled on the way (no %r in the line)" % SEP)
    if extra and not any(extra in s for s in lines):
        out.append("the composed command's %r is missing" % extra)
    return out


def try_shells(name, cmd, env, target=None, extra=None):
    """Run cmd through every shell; target None: it must work in all of them."""
    for sh, argv, e in shells(env):
        p, lines, utf8, secs = render(argv, cmd, e)
        print("  %-10s %4.1f s, exit %d: %s" % (sh, secs, p.returncode, " / ".join(lines) or "(no output)"))
        problems = problems_of(p, lines, utf8, extra)
        if not problems:
            ROWS.append((name, sh, "ok, %.1f s" % secs))
            continue
        if target and family(sh) != target:
            print("    (not required: the command is written for %s)" % target)
            ROWS.append((name, sh, "not required (%s)" % "; ".join(problems)))
            continue
        ROWS.append((name, sh, "FAILED: %s" % "; ".join(problems)))
        fail("%s, through %s: %s" % (name, sh, "; ".join(problems)))
        show(p.stderr.decode("utf-8", "replace"))


def recorded_shell(env, path):
    """The shell install.json records for this settings file (Windows), or None."""
    try:
        with open(os.path.join(env["GLIDEPATH_HOME"], "install.json"), encoding="utf-8-sig") as f:
            records = json.load(f).get("records") or {}
    except (OSError, ValueError, AttributeError):
        return None
    want = os.path.normcase(os.path.realpath(path))
    recs = [r for k, r in records.items() if os.path.normcase(os.path.realpath(k)) == want] or list(records.values())
    return recs[0].get(winshell.RECORD_KEY) if len(recs) == 1 and isinstance(recs[0], dict) else None


def target_shell(name, cmd, env, obj, path):
    """None when the command must run in every shell here, else the one it is written for."""
    if not WINDOWS:
        return None
    target = winshell.written_for(cmd)
    settings_env = obj.get("env") if isinstance(obj, dict) else None
    predicted = winshell.predict_shell(env, settings_env)[0]
    print("  written for: %s; Claude Code here would use %s; install.json records %s"
          % (target or "every shell", predicted, recorded_shell(env, path)))
    if target and target != predicted:
        fail("%s: the command is written for %s, but Claude Code here runs %s" % (name, target, predicted))
    return target


def scenario(name, tmp, before, entry, python=None, extra=None):
    """Install into a settings file holding `before`, render through the shells,
    uninstall, and compare the file with what it was."""
    print("\n== %s" % name)
    env = sandbox(tmp)
    path = os.path.join(tmp, "settings.json")
    with open(path, "wb") as f:
        f.write(before)
    args = ["install", "--apply", "--settings", path, "--no-bootstrap", "--no-mod"]
    if glidepath(entry, env, args + (["--python", python] if python else [])) != 0:
        return fail("%s: install failed" % name)
    try:
        with open(path, encoding="utf-8-sig") as f:
            obj = json.load(f)
        cmd = obj["statusLine"]["command"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return fail("%s: no statusLine command after install (%r)" % (name, e))
    print("  statusLine command: %s" % cmd)
    try_shells(name, cmd, env, target_shell(name, cmd, env, obj, path), extra)
    log = os.path.join(env["GLIDEPATH_HOME"], "errors.log")
    if os.path.exists(log):
        fail("%s: glidepath logged errors" % name)
        with open(log, encoding="utf-8", errors="replace") as f:
            show(f.read())
    if glidepath(entry, env, ["uninstall", "--apply", "--settings", path, "--no-mod"]) != 0:
        return fail("%s: uninstall failed" % name)
    with open(path, "rb") as f:
        after = f.read()
    if after != before:
        fail("%s: settings.json differs after uninstall:\n  before %r\n  after  %r" % (name, before, after))
    else:
        print("  settings.json is byte for byte what it was")
    return env


def base_python():
    """This interpreter's base python.exe: itself, unless it runs in a venv."""
    return getattr(sys, "_base_executable", None) or sys.executable


# Folders of a Python installation that glidepath never loads, left out of base_copy.
NOT_COPIED = {"doc", "include", "libs", "tcl", "tools", "scripts"}


def base_copy(dest):
    """Windows: this Python installation (sys.base_prefix) copied to dest, and its python.exe
    there, else None after a failure. A python.org installation runs from any folder: python.exe
    finds the standard library by Lib\\os.py beside it. A venv will not do here, since install
    pins a venv's base interpreter on Windows. Left out: what glidepath never loads (site-packages,
    the docs, headers and import libraries, Tcl/Tk, Tools, Scripts, the test suites, __pycache__)."""
    top, lib = (os.path.normcase(p) for p in (sys.base_prefix, os.path.join(sys.base_prefix, "Lib")))

    def skip(folder, names):
        here = os.path.normcase(folder)
        return [n for n in names if n.lower() in ("test", "tests", "idle_test", "__pycache__")
                or (here == top and n.lower() in NOT_COPIED) or (here == lib and n.lower() == "site-packages")]

    python = os.path.join(dest, "python.exe")
    try:
        t = time.perf_counter()
        shutil.copytree(sys.base_prefix, dest, ignore=skip)
        p = subprocess.run([python, "-I", "-S", "-c", "import sqlite3, sys; print(sys.prefix)"], capture_output=True,
                           timeout=120)
        out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
        secs = time.perf_counter() - t
        print("  (%s copied to %s in %.1f s; its sys.prefix: %s)" % (sys.base_prefix, dest, secs, out))
        if p.returncode == 0:
            return python
        fail("the copy of this Python installation in %s does not run: %s" % (dest, out))
    except (OSError, shutil.Error, subprocess.SubprocessError) as e:
        fail("could not copy this Python installation to %s: %r" % (dest, e))
    return None


def spaced_copy(base):
    """glidepath copied into base (a folder with a space), and an interpreter there (None if none):
    on Windows a copy of this Python installation (base_copy), elsewhere a venv, which macOS and
    Linux pin as given."""
    for part in ("bin", "glidepath"):
        shutil.copytree(os.path.join(ROOT, part), os.path.join(base, "repo", part),
                        ignore=shutil.ignore_patterns("__pycache__"))
    if WINDOWS:
        return os.path.join(base, "repo", "bin", "glidepath"), base_copy(os.path.join(base, "python"))
    venv = os.path.join(base, "venv")
    p = subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], capture_output=True, timeout=300)
    python = os.path.join(venv, "Scripts", "python.exe") if WINDOWS else os.path.join(venv, "bin", "python")
    if p.returncode != 0 or not os.path.exists(python):
        print("  (no venv, so the interpreter's path has no space: %s)" % p.stderr.decode("utf-8", "replace").strip())
        python = None
    return os.path.join(base, "repo", "bin", "glidepath"), python


def own_forms(env, python, entry):
    """Windows: glidepath's command in each shell's own syntax (what install writes when a
    path must be quoted) runs in that shell, whichever one Claude Code would pick."""
    argv = [python.replace("\\", "/")] + winshell.python_flags(True) + [entry.replace("\\", "/"), "statusline"]
    for sh, sargv, e in shells(env):
        name = "the %s form" % family(sh)
        cmd = winshell.command_line(argv, family(sh))
        print("\n== %s, through %s: %s" % (name, sh, cmd))
        p, lines, utf8, secs = render(sargv, cmd, e)
        print("  %-10s %4.1f s, exit %d: %s" % (sh, secs, p.returncode, " / ".join(lines) or "(no output)"))
        problems = problems_of(p, lines, utf8, None)
        ROWS.append((name, sh, "FAILED: %s" % "; ".join(problems) if problems else "ok, %.1f s" % secs))
        if problems:
            fail("%s, through %s: %s" % (name, sh, "; ".join(problems)))
            show(p.stderr.decode("utf-8", "replace"))


def git_bash_predicted(tmp):
    """Windows: Git for Windows in its default folder means Claude Code runs the statusLine
    in Git Bash, so winshell.predict_shell() must say bash. A fact the code under test does
    not decide, which target_shell's comparisons then lean on."""
    if not WINDOWS or not os.path.exists(GIT_BASH):
        return
    got = winshell.predict_shell(sandbox(tmp))
    print("\n== Git Bash is at %s; winshell.predict_shell() says %r" % (GIT_BASH, got))
    if got[0] != "bash":
        fail("Git Bash is at %s but winshell.predict_shell() says %r" % (GIT_BASH, got))


# (the user's statusLine, written encoded on Windows, shells to run it through, a check of its
# line, what that check means): see composed_forms.
COMPOSED = (
    ('echo "mine two"', True, ("bash", "pwsh", "powershell", "sh"), lambda s: s.startswith("mine two"),
     "a double quote reaches Python intact (Windows PowerShell 5.1 splits it in plain form)"),
    ("/usr/bin/printf mine", True, ("bash", "sh"), lambda s: s.startswith("mine"),
     "Git Bash's MSYS2 conversion of a leading POSIX path is avoided"),
    ("echo /usr/x", False, ("bash", "sh"), lambda s: s == "/usr/x" or s.startswith("/usr/x "),
     "MSYS2 leaves an argument alone when whitespace comes before its first slash; if this fails, the plain-form "
     "criterion (winshell.msys_converts) is wrong"),
    ("~/mine.sh", False, ("bash", "sh"), lambda s: s.startswith("mine"),
     "MSYS2 leaves an argument starting with ~ alone, and the composed .sh runs"),
)


def composed_forms(tmp, entry):
    """A previous statusLine that a shell would hand to Python changed on Windows goes
    base64-encoded after --compose-b64, any other as the readable --compose <command>
    (never encoded on macOS or Linux). Each: install, check the form, render through the
    shells given (the composed command's line must be there, and glidepath's), uninstall,
    and compare settings.json byte for byte."""
    for i, (theirs, encoded, names, ok, meaning) in enumerate(COMPOSED):
        name = "composed %s" % theirs
        print("\n== %s" % name)
        root = os.path.join(tmp, str(i))
        env = sandbox(root)
        with open(os.path.join(env["HOME"], "mine.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/bin/sh\necho mine\n")
        os.chmod(os.path.join(env["HOME"], "mine.sh"), 0o755)
        os.makedirs(env["GLIDEPATH_HOME"], exist_ok=True)
        with open(os.path.join(env["GLIDEPATH_HOME"], "config.json"), "w", encoding="utf-8") as f:
            json.dump({"compose": {"timeout_ms": 5000}}, f)  # what is checked here is the text, not the timing
        path = os.path.join(root, "settings.json")
        before = (json.dumps({"statusLine": {"type": "command", "command": theirs}}, indent=2) + "\n").encode("utf-8")
        with open(path, "wb") as f:
            f.write(before)
        if glidepath(entry, env, ["install", "--apply", "--settings", path, "--no-bootstrap", "--no-mod"]) != 0:
            fail("%s: install failed" % name)
            continue
        with open(path, encoding="utf-8-sig") as f:
            cmd = json.load(f)["statusLine"]["command"]
        b64 = winshell.COMPOSE_B64 in cmd
        print("  statusLine command: %s\n  install wrote it %s" % (
            cmd, "encoded after %s" % winshell.COMPOSE_B64 if b64 else "as plain text after --compose"))
        want = encoded and WINDOWS
        ROWS.append((name, "form", "encoded" if b64 else "plain"))
        if b64 != want:
            fail("%s: install wrote it %s, expected %s" % (name, "encoded" if b64 else "plain",
                                                          "encoded" if want else "plain"))
        for sh, argv, e in [s for s in shells(env) if s[0] in names]:
            p, lines, utf8, secs = render(argv, cmd, e)
            print("  %-10s %4.1f s, exit %d: %s" % (sh, secs, p.returncode, " / ".join(lines) or "(no output)"))
            problems = problems_of(p, lines, utf8, None)
            if not any(ok(s) for s in lines):
                problems.append("no line from the composed command (%s)" % meaning)
            ROWS.append((name, sh, "FAILED: %s" % "; ".join(problems) if problems else "ok, %.1f s" % secs))
            if problems:
                fail("%s, through %s: %s" % (name, sh, "; ".join(problems)))
                show(p.stderr.decode("utf-8", "replace"))
        log = os.path.join(env["GLIDEPATH_HOME"], "errors.log")
        if os.path.exists(log):
            fail("%s: glidepath logged errors" % name)
            with open(log, encoding="utf-8", errors="replace") as f:
                show(f.read())
        if glidepath(entry, env, ["uninstall", "--apply", "--settings", path, "--no-mod"]) != 0:
            fail("%s: uninstall failed" % name)
            continue
        with open(path, "rb") as f:
            after = f.read()
        if after != before:
            fail("%s: settings.json differs after uninstall:\n  before %r\n  after  %r" % (name, before, after))
        else:
            print("  settings.json is byte for byte what it was")


def short_paths(python, entry):
    """Windows, the paths with a space: whether the statusLine command names the interpreter
    and glidepath by their 8.3 short paths (then it reads the same in every shell, and the
    scenario above required it to work in all of them). Reported only."""
    if not WINDOWS or not python:
        return
    from glidepath import install

    print("\n== 8.3 short paths for the paths with a space")
    for what, path in (("interpreter", python), ("glidepath", entry)):
        word = install._path_word(path)
        used = word != winshell.slashes(path)
        print("  %s: %s -> %s (%s)" % (what, path, word, "8.3 short path" if used else "no bare short path"))
        ROWS.append(("8.3 short path", what, "used" if used else "not used"))


def other_python():
    """This interpreter by a path of another length, with dotted folders, which install pins as
    given (it is the same file), so a repin through a launcher rewrites it with a line of another
    length. Not a venv's python.exe: install pins its base interpreter on Windows, this one."""
    folder, name = os.path.split(base_python())
    return os.path.join(folder, *["."] * 8 + [name])


def launchers(tmp):
    """Windows: glidepath.cmd from cmd.exe, pwsh and powershell.exe, and the sh glidepath beside it
    from Git Bash, with the --bin-dir folder on PATH: --version, check (its exit 1 must reach
    the caller), an install that repins to an interpreter whose path has another length (it
    rewrites the launchers while one runs) and an uninstall (it deletes them while one runs).
    Each must exit with glidepath's own code, and cmd.exe must not read the batch file again after
    python returned ("The batch file cannot be found.", or a piece of the new text run as a
    command: "is not recognized")."""
    if not WINDOWS:
        return
    print("\n== The launchers: glidepath.cmd from cmd.exe and PowerShell, glidepath from Git Bash")
    env = sandbox(tmp)
    bindir = os.path.join(tmp, "bin")
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    settings = os.path.join(tmp, "settings.json")
    before = b'{\n  "theme": "dark"\n}\n'
    entry = os.path.join(ROOT, "bin", "glidepath")
    other = other_python()
    print("  interpreters: %s (%d characters), then %s (%d)" % (sys.executable, len(sys.executable), other, len(other)))
    cmd_exe = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
    callers = [("cmd", "glidepath.cmd", lambda words: '"%s" /d /c %s' % (cmd_exe, subprocess.list2cmdline(words)))]
    for ps in ("pwsh", "powershell"):
        exe = shutil.which(ps, path=env["PATH"])
        if exe:  # shell_versions() fails the run for a missing one
            callers.append((ps, "glidepath.cmd", lambda words, exe=exe: [exe] + PS_FLAGS + [
                "& %s; exit $LASTEXITCODE" % " ".join(winshell.quote(w, "powershell") for w in words)]))
    if os.path.exists(GIT_BASH):
        callers.append(("Git Bash", "glidepath", lambda words: [GIT_BASH, "-lc", " ".join(
            winshell.quote(winshell.slashes(w), "bash") for w in words)]))
    inst = ["install", "--apply", "--no-mod", "--no-bootstrap", "--settings", settings]
    for caller, name, line in callers:
        print("\n  -- %s, running %s" % (caller, name))
        with open(settings, "wb") as f:
            f.write(before)
        if glidepath(entry, env, inst + ["--bin-dir", bindir]) != 0:
            fail("launchers, %s: install --bin-dir failed" % caller)
            continue
        steps = [([name, "--version"], 0, "glidepath "), ([name, "check"], 1, None),
                 ([name] + inst + ["--python", other], 0, "Wrote launcher"),
                 ([name, "uninstall", "--apply", "--no-mod", "--settings", settings], 0, "Uninstalled")]
        if name == "glidepath.cmd":  # by its bare name too: PATHEXT finds glidepath.cmd, never the sh glidepath
            steps.insert(1, (["glidepath", "--version"], 0, "glidepath "))
        problems = []
        for words, want, text in steps:
            try:
                p = subprocess.run(line(words), env=env, capture_output=True, timeout=120)
                code, out = p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError) as e:
                code, out = None, repr(e)
            print("  $ %s -> exit %s" % (" ".join(words[:2]), code))
            show(out)
            said = [s for s in ("batch file cannot be found", "is not recognized", "command not found")
                    if s in out.lower()]
            if code != want or (text and text not in out) or said:
                problems.append("%s: exit %s (want %d)%s%s" % (" ".join(words[:2]), code, want,
                                                              "" if not text or text in out else ", no %r" % text,
                                                              "".join(", says %r" % s for s in said)))
            if words[1] == "install":  # repinned while one of them ran
                for path, want_text in ((os.path.join(bindir, "glidepath.cmd"), other),
                                        (os.path.join(bindir, "glidepath"), winshell.slashes(other))):
                    try:
                        with open(path, encoding="latin-1") as f:
                            if want_text not in f.read():
                                problems.append("%s does not run %s after the repin" % (path, other))
                    except OSError as e:
                        problems.append("%s after the repin: %s" % (path, e))
        left = [n for n in ("glidepath.cmd", "glidepath") if os.path.exists(os.path.join(bindir, n))]
        if left:
            problems.append("uninstall left %s" % ", ".join(left))
        with open(settings, "rb") as f:
            if f.read() != before:
                problems.append("settings.json differs after uninstall")
        ROWS.append(("launchers", "%s, %s" % (caller, name), "FAILED: %s" % "; ".join(problems) if problems else "ok"))
        for msg in problems:
            fail("launchers, %s: %s" % (caller, msg))


def read_only(tmp):
    """A read-only settings.json (attrib +R on Windows, mode 0444 elsewhere): install and
    uninstall go through as on POSIX, the file comes back byte for byte and is still
    read-only. One install created and the user made read-only since: uninstall removes it."""
    name = "a read-only settings.json"
    print("\n== %s" % name)
    env = sandbox(tmp)
    entry = os.path.join(ROOT, "bin", "glidepath")
    path, new = os.path.join(tmp, "settings.json"), os.path.join(tmp, "new", "settings.json")
    before = b'{\r\n  "theme": "dark"\r\n}\r\n'
    with open(path, "wb") as f:
        f.write(before)

    def protect(p):
        if WINDOWS:
            r = subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "attrib.exe"),
                                "+R", p], capture_output=True, timeout=60)
            if r.returncode != 0:
                problems.append("attrib +R failed: %s" % r.stdout.decode("utf-8", "replace").strip())
        else:
            os.chmod(p, 0o444)

    def read_only_now(p, when):
        if os.stat(p).st_mode & stat.S_IWRITE:
            problems.append("settings.json is not read-only %s" % when)

    problems = []
    flags = ["--no-bootstrap", "--no-mod"]
    try:
        protect(path)
        if glidepath(entry, env, ["install", "--apply", "--settings", path] + flags) != 0:
            problems.append("install failed")
        else:
            with open(path, encoding="utf-8") as f:
                if "glidepath" not in (json.load(f).get("statusLine") or {}).get("command", ""):
                    problems.append("no glidepath statusLine after install")
            read_only_now(path, "after install")
            if glidepath(entry, env, ["uninstall", "--apply", "--settings", path, "--no-mod"]) != 0:
                problems.append("uninstall failed")
            with open(path, "rb") as f:
                if f.read() != before:
                    problems.append("it differs after uninstall")
            read_only_now(path, "after uninstall")
        if glidepath(entry, env, ["install", "--apply", "--settings", new] + flags) != 0:
            problems.append("install into a new settings.json failed")
        else:
            protect(new)
            if glidepath(entry, env, ["uninstall", "--apply", "--settings", new, "--no-mod"]) != 0:
                problems.append("uninstall of a new settings.json made read-only failed")
            elif os.path.exists(new):
                problems.append("uninstall left the settings.json it created")
    finally:
        for folder, _, names in os.walk(tmp):  # so the temporary folder can go (Windows deletes no read-only file)
            for n in names:
                try:
                    os.chmod(os.path.join(folder, n), stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
    if not problems:
        print("  settings.json is byte for byte what it was, and still read-only")
    ROWS.append((name, "install, uninstall", "FAILED: %s" % "; ".join(problems) if problems else "ok"))
    for msg in problems:
        fail("%s: %s" % (name, msg))


def drive_less_home(tmp, entry):
    """Windows: what Git Bash hands python.exe for a GLIDEPATH_HOME rooted with a slash and no
    drive (reported only), and that install refuses such a value in the settings env, which
    Claude Code passes to the statusLine as written."""
    print("\n== A GLIDEPATH_HOME with no drive: /srv/glidepath-ci")
    if not WINDOWS:
        print("  (Windows only: elsewhere it is an absolute path)")
        return
    env = sandbox(tmp)
    probe = "'%s' -I -S -c 'import os; print(os.environ[\"GLIDEPATH_HOME\"])'" % sys.executable.replace("\\", "/")
    for sh, argv, e in shells(env):
        if sh == "bash":
            p = subprocess.run(argv + [probe], env=dict(e, GLIDEPATH_HOME="/srv/glidepath-ci"), capture_output=True,
                               timeout=60, **hidden())
            seen = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
            print("  Git Bash hands python.exe GLIDEPATH_HOME=%s" % seen)
            ROWS.append(("GLIDEPATH_HOME /srv/glidepath-ci", sh, "python.exe sees %s" % seen))
    path = os.path.join(tmp, "settings.json")
    before = b'{\n  "env": {\n    "GLIDEPATH_HOME": "/srv/glidepath-ci"\n  }\n}\n'
    with open(path, "wb") as f:
        f.write(before)
    args = ["install", "--apply", "--settings", path, "--no-bootstrap", "--no-mod"]
    p = subprocess.run([sys.executable, entry] + args, env=env, capture_output=True, timeout=600)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    print("  $ python %s %s (its env: GLIDEPATH_HOME=/srv/glidepath-ci) -> exit %d" % (entry, " ".join(args), p.returncode))
    show(out)
    with open(path, "rb") as f:
        after = f.read()
    ok = p.returncode == 1 and "has no drive" in out and after == before and not os.path.exists(env["GLIDEPATH_HOME"])
    ROWS.append(("GLIDEPATH_HOME /srv/glidepath-ci", "install --apply", "refused" if ok else "FAILED"))
    if not ok:
        fail("install --apply with GLIDEPATH_HOME=/srv/glidepath-ci in the settings env: not refused up front")


def venv_pin(tmp):
    """Windows: `install --python` with a venv's python.exe pins the venv's base interpreter. That
    python.exe is a launcher that starts the base one as a child process, which then sees the
    launcher as its parent instead of the shell (winshell.parent_shell), on every render."""
    print("\n== A venv's python.exe: install pins its base interpreter")
    if not WINDOWS:
        print("  (Windows only: macOS and Linux pin a venv's interpreter as given)")
        return
    venv = os.path.join(tmp, "venv")
    p = subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], capture_output=True, timeout=300)
    python = os.path.join(venv, "Scripts", "python.exe")
    if p.returncode != 0 or not os.path.exists(python):
        print("  (no venv, not checked: %s)" % p.stderr.decode("utf-8", "replace").strip())
        ROWS.append(("venv pin", "install", "not checked (no venv)"))
        return
    from glidepath import install

    pin, report = install.choose_python(python)
    note = report[0][5] if report else ""
    print("  install.choose_python(%s) -> %s (%s); base interpreter: %s" % (python, pin, note, base_python()))
    try:
        ok = bool(pin) and os.path.samefile(pin, base_python()) and note == "via %s" % python
    except OSError:
        ok = False
    ROWS.append(("venv pin", "install", "base interpreter" if ok else "FAILED: %s" % pin))
    if not ok:
        fail("install --python %s pins %s, not the base interpreter %s with a note naming the venv (%r)"
             % (python, pin, base_python(), note))


def report(tmp):
    print("\n== The statusLine shell and the installer's interpreter report")
    env = sandbox(tmp)
    shell_versions(env)
    p = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", REPORT, ROOT], env=env, capture_output=True,
                       timeout=300)
    show((p.stdout + p.stderr).decode("utf-8", "replace"), "  ")
    if p.returncode != 0:
        fail("the interpreter report failed (exit %d)" % p.returncode)


def marketplace(tmp):
    print("\n== claude plugin marketplace add (reported only, never fails the run)")
    claude = shutil.which("claude")
    if not claude:
        print("  claude is not on PATH (npm install -g @anthropic-ai/claude-code): not tried")
        return

    def run(args, env, cwd):
        try:
            p = subprocess.run([claude] + args, env=env, cwd=cwd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            print("  $ claude %s (in %s) -> %r" % (" ".join(args), cwd, e))
            return None, ""
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        print("  $ claude %s (in %s) -> exit %d" % (" ".join(args), cwd, p.returncode))
        show(out)
        return p.returncode, out

    forms = [("absolute path", ROOT, None)]
    if WINDOWS:
        forms.append(("absolute path with forward slashes", ROOT.replace("\\", "/"), None))
    forms.append(("./ with the repo as the working directory", "./", ROOT))
    accepted = []
    for i, (label, source, cwd) in enumerate(forms):
        print("\n  -- %s: %s" % (label, source))
        env = sandbox(os.path.join(tmp, str(i)))
        cwd = cwd or env["HOME"]
        if i == 0:
            run(["--version"], env, cwd)
        code, _ = run(["plugin", "marketplace", "add", source], env, cwd)
        _, listed = run(["plugin", "marketplace", "list", "--json"], env, cwd)
        ok = code == 0 and "glidepath" in listed
        ROWS.append(("marketplace add", label, "accepted" if ok else "rejected"))
        if ok:
            accepted.append((label, env, cwd))
    names = [a[0] for a in accepted]
    print("\n  marketplace add accepts: %s" % (", ".join(names) or "none of the forms"))
    print("  marketplace add rejects: %s" % (", ".join(f[0] for f in forms if f[0] not in names) or "none of the forms"))
    if os.environ.get("GITHUB_ACTIONS"):
        print("::notice title=claude plugin marketplace add::accepts: %s" % (", ".join(names) or "none of the forms"))
    if accepted:
        label, env, cwd = accepted[0]
        print("\n  -- plugin install, after the marketplace was added by %s" % label)
        code, _ = run(["plugin", "install", "glidepath@glidepath", "--scope", "user"], env, cwd)
        _, listed = run(["plugin", "list", "--json"], env, cwd)
        ROWS.append(("plugin install", "glidepath@glidepath",
                     "installed" if code == 0 and "glidepath@glidepath" in listed else "failed"))


def summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not ROWS:
        return
    lines = ["### glidepath integration on %s, Python %s" % (sys.platform, sys.version.split()[0]), "",
             "| step | shell or form | result |", "| --- | --- | --- |"]
    lines += ["| %s | %s | %s |" % tuple(c.replace("|", "/") for c in row) for row in ROWS]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def main(argv):
    for stream in (sys.stdout, sys.stderr):  # a pipe in CI: the ANSI code page on Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-marketplace", action="store_true", help="skip the claude plugin marketplace forms")
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder")
    args = ap.parse_args(argv)
    print("%s, Python %s (%s), repo %s" % (sys.platform, sys.version.split()[0], sys.executable, ROOT))
    tmp = tempfile.mkdtemp(prefix="glidepath-ci-")
    try:
        report(os.path.join(tmp, "report"))
        git_bash_predicted(os.path.join(tmp, "predicted"))
        entry = os.path.join(ROOT, "bin", "glidepath")
        scenario("no statusLine before", os.path.join(tmp, "add"), b'{\n  "theme": "dark"\n}\n', entry)
        scenario("CRLF line endings", os.path.join(tmp, "crlf"), b'{\r\n  "theme": "dark"\r\n}\r\n', entry)
        theirs = b'{\n  "statusLine": {\n    "type": "command",\n    "command": "echo mine"\n  }\n}\n'
        scenario("composed with the user's statusLine", os.path.join(tmp, "compose"), theirs, entry, extra="mine")
        composed_forms(os.path.join(tmp, "forms"), entry)
        spaces = os.path.join(tmp, "spaces", "with space")  # settings.json and GLIDEPATH_HOME too
        entry, python = spaced_copy(spaces)
        env = scenario("paths with a space", spaces, b"{}\n", entry, python=python)
        short_paths(python, entry)
        if WINDOWS and python and env:
            own_forms(env, python, entry)
        launchers(os.path.join(tmp, "launchers"))
        read_only(os.path.join(tmp, "read-only"))
        drive_less_home(os.path.join(tmp, "nodrive"), os.path.join(ROOT, "bin", "glidepath"))
        venv_pin(os.path.join(tmp, "venv-pin"))
        if not args.no_marketplace:
            marketplace(os.path.join(tmp, "marketplace"))
    finally:
        if args.keep:
            print("\nkept %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    summary()
    print("\n%s" % ("%d failure(s):\n  %s" % (len(FAILURES), "\n  ".join(FAILURES)) if FAILURES else "all good"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
