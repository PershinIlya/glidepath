"""`glidepath install` / `glidepath uninstall`.

Dry run by default; --apply writes. Edits exactly one key of settings.json
(statusLine), keeps every other key and hook, keeps the file's indentation
and trailing newline, backs it up with a timestamp before writing, and
records what it did in ~/.glidepath/install.json (one record per settings
file) so uninstall can put the original back. The record is saved before the
settings file is written. When the file has not changed since install apart
from what install and the plugin CLI did, uninstall restores it byte for
byte (line endings and mode included).

On Windows the statusLine runs in Git Bash or PowerShell (see winshell), so the
command uses forward slashes and -X utf8, in a form both shells read the same
when there is one, else in the syntax of the shell Claude Code will use (which
the record keeps); the launcher is a glidepath.cmd, with an sh `glidepath` beside it
for Git Bash.
"""

import calendar
import codecs
import difflib
import glob
import hashlib
import json
import ntpath
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from . import __version__, config, statusline, winshell

MARKETPLACE = "glidepath"
PLUGIN = "glidepath"
PLUGIN_ID = PLUGIN + "@" + MARKETPLACE
NAMES = ("glidepath", "pacer", "ctempo")  # this name and the earlier ones (legacy.py)
PLUGIN_KEYS = ("enabledPlugins", "extraKnownMarketplaces")
OWN_ENTRY = {"enabledPlugins": PLUGIN_ID, "extraKnownMarketplaces": MARKETPLACE}
LAUNCHER_MARK = "# glidepath launcher"
CMD_MARK = "rem glidepath launcher"  # the same, in the Windows glidepath.cmd
CLAUDE_TIMEOUT = 120
BOM = "\ufeff"


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def entry_path():
    return os.path.join(repo_root(), "bin", "glidepath")


def settings_path(explicit=None):
    """Absolute path (a relative one would mean another file from another directory)."""
    return os.path.abspath(os.path.expanduser(explicit or os.path.join(config.claude_dir(), "settings.json")))


def state_path():
    return os.path.join(config.home(), "install.json")


# -- JSON with the file's own formatting ------------------------------------------
class Fmt:
    def __init__(self, indent=2, newline=False, eol="\n", bom=False):
        self.indent = indent
        self.newline = newline
        self.eol = eol
        self.bom = bom


def detect_format(text):
    indent = 2
    m = re.search(r"\n([ \t]+)\S", text)
    if m:
        ws = m.group(1)
        indent = "\t" if ws.startswith("\t") else len(ws)
    eol = "\r\n" if "\r\n" in text else "\n"
    return Fmt(indent, text.endswith("\n"), eol, text.startswith(BOM))


def dumps(obj, fmt):
    # JSON strings cannot hold raw newlines, so only structural ones change.
    s = json.dumps(obj, indent=fmt.indent, ensure_ascii=False).replace("\n", fmt.eol)
    return (BOM if fmt.bom else "") + s + (fmt.eol if fmt.newline else "")


def parse_json(text):
    """A settings file's object from its text: a UTF-8 BOM (Windows editors write one)
    is skipped, and a blank file is {}, as Claude Code reads them."""
    body = text[1:] if text.startswith(BOM) else text
    return json.loads(body) if body.strip() else {}


def read_settings(path):
    """(object, raw text or None, Fmt). Raises ValueError on a file we must not touch.

    Read with newline="" so CRLF files keep their bytes. The raw text keeps a
    BOM, so a write keeps it too, and the hash is that of the file's bytes."""
    real = os.path.realpath(path)
    if os.path.islink(path) and not os.path.exists(real):
        raise ValueError("%s is a dangling symlink; refusing to touch it" % path)
    if not os.path.exists(real):
        return {}, None, Fmt(2, True)
    try:
        with open(real, encoding="utf-8", newline="") as f:
            raw = f.read()
    except UnicodeDecodeError:
        raise ValueError("%s is not UTF-8; refusing to touch it" % path)
    if not raw.lstrip(BOM).strip():
        return {}, raw, Fmt(2, True, bom=raw.startswith(BOM))
    try:
        obj = parse_json(raw)
    except ValueError as e:
        raise ValueError("%s is not valid JSON (%s); refusing to touch it" % (path, e))
    if not isinstance(obj, dict):
        raise ValueError("%s is not a JSON object; refusing to touch it" % path)
    return obj, raw, detect_format(raw)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _umask():
    m = os.umask(0)
    os.umask(m)
    return m


REPLACE_TRIES = 10


def _replace(src, dst):
    """os.replace. On Windows it fails while another process holds the target open
    without sharing delete (Claude Code reading settings.json, an antivirus scan):
    retried for a moment there. Windows also refuses a read-only target (attrib +R),
    which POSIX replaces like any other: the attribute is cleared for the move (and
    put back if it fails), and the new file has src's mode, which atomic_write made
    the target's, so it stays read-only."""
    tries = REPLACE_TRIES if config.IS_WINDOWS else 1
    mode = _writable(dst) if config.IS_WINDOWS else None
    try:
        for i in range(tries):
            try:
                return os.replace(src, dst)
            except PermissionError:
                if i == tries - 1:
                    raise
                time.sleep(0.05 * (i + 1))
    except BaseException:
        _chmod_back(dst, mode)
        raise


def _remove(path):
    """os.remove, which on Windows refuses a read-only file that POSIX removes like any other."""
    mode = _writable(path) if config.IS_WINDOWS else None
    try:
        os.remove(path)
    except BaseException:
        _chmod_back(path, mode)
        raise


def _writable(path):
    """Clear the read-only attribute of an existing file: its mode before, or None
    when it was writable already (or is not there)."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & stat.S_IWRITE:
            return None
        os.chmod(path, mode | stat.S_IWRITE)
        return mode
    except OSError:
        return None


def _chmod_back(path, mode):
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def atomic_write(path, data):
    """Write str or bytes via a private tmp file and rename; keeps the mode."""
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    mode = stat.S_IMODE(os.stat(real).st_mode) if os.path.exists(real) else (0o666 & ~_umask())
    # A new name opened exclusively (mkstemp: O_EXCL, 0600), never a link
    # someone left at a name they could guess.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real) or ".", prefix=os.path.basename(real) + ".glidepath.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        _replace(tmp, real)
    except BaseException:
        try:
            if config.IS_WINDOWS:  # the mode of a read-only target made it read-only, and Windows cannot delete that
                os.chmod(tmp, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_backup(path):
    """Copy the file's bytes to a new, never-overwritten timestamped backup. The
    time is UTC (a Z marks it): local time can go back (a move west, the fall-back hour)."""
    real = os.path.realpath(path)
    base = "%s.glidepath-backup-%s" % (real, time.strftime("%Y%m%d-%H%M%SZ", time.gmtime()))
    with open(real, "rb") as f:
        data = f.read()
    for i in range(1000):
        name = base if i == 0 else "%s-%d" % (base, i)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(real, name)
        return name
    raise OSError("could not create a unique backup next to %s" % real)


def sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def diff(old, new, path):
    return "".join(
        difflib.unified_diff(
            (old or "").splitlines(True), new.splitlines(True), fromfile=path + " (now)", tofile=path + " (after)"
        )
    )


# -- subprocesses --------------------------------------------------------------------
def _run(argv, timeout, env=None, cwd=None):
    """(exit code, stdout, stderr), decoded as UTF-8 (claude and the probe write UTF-8,
    whatever the locale); raises OSError or subprocess.TimeoutExpired. On Windows a
    timeout kills the whole tree: killing a .cmd shim or a launcher (py.exe, claude.cmd)
    leaves its child holding the pipes, and the read would wait for that child."""
    if not config.IS_WINDOWS:
        r = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", env=env, cwd=cwd,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                         env=env, cwd=cwd)
    try:
        so, se = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_tree(p)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # something outside the tree still holds a pipe: give up on its output
        raise e
    return p.returncode, so, se


def _kill_tree(p):
    """The process and its children (taskkill /T), then the process itself in any case."""
    winshell.kill_tree(p.pid)
    try:
        p.kill()
    except OSError:
        pass


# -- interpreter ---------------------------------------------------------------------
# One JSON line, ASCII whatever the code page: version, sqlite, and the interpreter's own path
# (on Windows a Store alias or a launcher can stand in front of the real python.exe).
PROBE = ("import sys, sqlite3, json; "
         "print(json.dumps(['%d.%d' % sys.version_info[:2], sqlite3.sqlite_version, sys.executable]))")
# On Windows the path is the base interpreter's: a venv's python.exe is a launcher that runs its
# base python.exe as a child. glidepath runs -I -S, so a venv adds nothing but that process on every
# render, and the process hides the shell that started glidepath from winshell.parent_shell.
WINDOWS_PROBE = PROBE.replace("sys.executable", "getattr(sys, '_base_executable', None) or sys.executable")


def _runnable(py):
    """An absolute path to something the OS can start. On Windows os.access(X_OK) is
    true for any file, and CreateProcess wants a .exe with a drive (or a UNC share)."""
    if not isinstance(py, str) or not py:
        return False
    if config.IS_WINDOWS:
        return bool(ntpath.splitdrive(py)[0]) and py.lower().endswith(".exe") and os.path.isfile(py)
    return os.path.isabs(py) and os.access(py, os.X_OK)


def _is_alias(path):
    """A Microsoft Store app execution alias, or the Store package behind one: the python.exe
    stub that opens the Store, or a Store Python, which writes under AppData to a private copy."""
    if "\\windowsapps\\" in path.replace("/", "\\").lower():
        return True
    try:  # the stat module defines the tag on Windows only
        return getattr(os.lstat(path), "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_APPEXECLINK", 0x8000001B)
    except OSError:
        return False


def probe(py):
    """(ok, version, sqlite, startup_ms, note)."""
    return probe_full(py)[:5]


def probe_full(py):
    """(ok, version, sqlite, startup_ms, note, executable): probe() plus the interpreter's
    own sys.executable. A candidate runs only with arguments: on Windows the python.exe
    stub opens the Store when run without any, and with them it fails."""
    if not _runnable(py):
        return False, None, None, None, ("not an absolute path to a .exe" if config.IS_WINDOWS
                                         else "not an executable absolute path"), None
    if sys.platform == "darwin" and py == "/usr/bin/python3":
        # Without the Command Line Tools this shim opens an install dialog.
        if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
            return False, None, None, None, "Command Line Tools not installed", None
    # The Python install manager's launchers install a runtime on first use unless told not to.
    env = dict(os.environ, PYTHON_MANAGER_AUTOMATIC_INSTALL="0") if config.IS_WINDOWS else None
    best = None
    out = ""
    for _ in range(3):
        t = time.perf_counter()
        try:
            code, so, se = _run([py, "-I", "-S", "-c", WINDOWS_PROBE if config.IS_WINDOWS else PROBE], timeout=15,
                                env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, None, None, None, str(e), None
        ms = (time.perf_counter() - t) * 1000
        if code != 0:
            return False, None, None, None, _probe_failure(se), None
        out = so.strip()
        best = ms if best is None else min(best, ms)
    try:
        ver, sq, exe = json.loads(out.splitlines()[-1])
        major, minor = (int(x) for x in ver.split("."))
    except (ValueError, TypeError, IndexError):
        return False, None, None, best, "unexpected probe output: %s" % out[-80:], None
    if (major, minor) < (3, 8):
        return False, ver, sq, best, "Python 3.8+ required", exe
    return True, ver, sq, best, "", exe


def _probe_failure(stderr):
    """Why a candidate failed the probe, in words. Python 2 (and 3 before 3.4)
    has no -I and answers with its usage text, whose last line was all we
    showed: Try `python -h' for more information."""
    text = stderr.strip()
    if re.search(r"Unknown option:? -I|unknown option -I|^usage: ", text, re.M | re.I):
        return "too old: Python 3.8+ required (this one has no -I flag, as Python 2 or before 3.4)"
    return (text.splitlines() or ["failed"])[-1]


def _pin(py, exe):
    """What to pin for a candidate that passed. On Windows, what the interpreter reports
    as itself when that is another real file (the candidate is a Store alias, py.exe, a
    venv's python.exe or another launcher in front of it), so no render pays for the extra hop."""
    if not config.IS_WINDOWS or not isinstance(exe, str) or not exe:
        return py
    same = ntpath.normcase(ntpath.normpath(exe)) == ntpath.normcase(ntpath.normpath(py))
    return py if same or _is_alias(exe) or not _runnable(exe) else exe


def candidates():
    seen, out = set(), []
    if config.IS_WINDOWS:
        for p in windows_candidates():
            key = ntpath.normcase(ntpath.normpath(p)) if p else None
            if p and key not in seen and os.path.exists(p):
                seen.add(key)
                out.append(p)
        return out
    for p in [
        "/usr/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        sys.executable,
        config.which("python3"),
    ]:
        if p and p not in seen and os.path.exists(p):
            seen.add(p)
            out.append(p)
    return out


def _version_key(name):
    """Sort key for a folder or registry tag such as Python312, "Python 3.12" or 3.13-64: newest first."""
    m = re.search(r"(\d)\.?(\d+)", name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def windows_candidates(env=None):
    """Interpreters to probe on Windows, in this order: the PEP 514 registry (PythonCore
    first, newest first), the py launcher's list, the default install folders, the one
    running install, then python and python3 on PATH (often only the Store stub, which
    fails the probe). The python.org installer leaves PATH alone by default, and the
    Desktop app does not see a shell's PATH, so PATH comes last."""
    env = os.environ if env is None else env
    out = _registry_pythons() + _py_launcher_pythons(env)
    local, pf, pf86 = (winshell.getenv(env, n) for n in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"))
    for base, pattern in ((local, ("Programs", "Python", "Python3*")), (pf, ("Python3*",)), (pf, ("Python 3*",)),
                          (pf86, ("Python3*",)), (pf86, ("Python 3*",)), (local, ("Python", "*"))):
        if base:  # the last one is the Python install manager's runtimes
            found = glob.glob(os.path.join(base, *pattern + ("python.exe",)))
            out += sorted(found, key=lambda p: _version_key(os.path.basename(os.path.dirname(p))), reverse=True)
    return out + [sys.executable, config.which("python"), config.which("python3")]


def _registry_pythons():
    """python.exe paths from the PEP 514 keys (HKCU, then HKLM in its 64-bit and 32-bit views)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive, view in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            root = winreg.OpenKey(hive, r"Software\Python", 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for company in sorted(_subkeys(winreg, root), key=lambda c: c != "PythonCore"):
                if company == "PyLauncher":
                    continue
                try:
                    ck = winreg.OpenKey(root, company)
                except OSError:
                    continue
                with ck:
                    for tag in sorted(_subkeys(winreg, ck), key=_version_key, reverse=True):
                        try:
                            ip = winreg.OpenKey(ck, tag + r"\InstallPath")
                        except OSError:
                            continue
                        with ip:
                            exe = _reg_value(winreg, ip, "ExecutablePath")
                            folder = _reg_value(winreg, ip, "")
                        if not exe and folder:  # PEP 514: python.exe in the (Default) folder
                            exe = os.path.join(folder, "python.exe")
                        if exe:
                            out.append(exe)
    return out


def _subkeys(winreg, key):
    names, i = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            return names
        i += 1


def _reg_value(winreg, key, name):
    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def _py_launcher_pythons(env):
    """The interpreters `py -0p` lists (the old py launcher and the install manager both keep that form)."""
    py = config.which("py")
    if not py:
        return []
    try:
        code, so, _ = _run([py, "-0p"], timeout=10, env=dict(env, PYTHON_MANAGER_AUTOMATIC_INSTALL="0"))
    except (OSError, subprocess.TimeoutExpired):
        return []
    if code != 0:
        return []
    found = (re.search(r"[A-Za-z]:[\\/].*\.exe", line, re.I) for line in so.splitlines())
    return [m.group(0).strip() for m in found if m]


def choose_python(explicit=None):
    """Pick the interpreter: explicit, else the first stable one that passes.

    Order prefers system and Homebrew paths over the one running install
    (often a conda env that may be activated, updated or removed). On Windows
    the pin is the real python.exe behind an alias or a launcher (a venv's
    included), and a Store Python is used only when nothing else passes.
    """
    report = []
    if explicit:
        ok, ver, sq, ms, note, exe = probe_full(explicit)
        pin = _pin(explicit, exe) if ok else explicit
        report.append((pin, ok, ver, sq, ms, _via(note, pin, explicit)))
        return (pin if ok else None), report
    chosen = fallback = None
    for p in candidates():
        ok, ver, sq, ms, note, exe = probe_full(p)
        pin = _pin(p, exe) if ok else p
        if ok and config.IS_WINDOWS and _is_alias(pin):
            note = note or "Microsoft Store alias: used only when nothing else passes"
            fallback = fallback or pin
        elif ok and chosen is None:
            chosen = pin
        report.append((pin, ok, ver, sq, ms, _via(note, pin, p)))
    return chosen or fallback, report


def _via(note, pin, candidate):
    return note if pin == candidate else ", ".join(x for x in (note, "via %s" % candidate) if x)


# -- the statusLine command --------------------------------------------------------
def is_ours(cmd):
    # pacer, ctempo: written under an earlier name (legacy.py), rewritten by install.
    return isinstance(cmd, str) and any(n in cmd for n in NAMES) and " statusline" in cmd


def _path_word(path):
    """A path as a word of the Windows statusLine command: forward slashes, and when
    that needs quotes, its folders' 8.3 short names if those need none (the command
    then reads the same in both shells; runtime.json keeps the long path)."""
    word = winshell.slashes(path)
    if not winshell.is_bare(word):
        short = winshell.short_path(path)
        if short and winshell.is_bare(winshell.slashes(short)):
            return winshell.slashes(short)
    return word


def our_command(python, entry, compose=None, shell=None):
    """The statusLine command. On Windows (see winshell): forward slashes (Git Bash
    eats backslashes) and -X utf8, in a form Git Bash and PowerShell read the same
    when there is one (the interpreter bare, its 8.3 short path if need be, the rest
    bare or in single quotes with no quote inside), since the shell Claude Code uses
    changes when Git for Windows comes or goes; otherwise in the syntax of `shell`
    (predicted when not given): single quotes for bash, `& ...` for PowerShell, whose
    `&` no bash reads, so the command says which shell it is for (winshell.written_for).
    A composed command that Windows PowerShell 5.1 or Git Bash would hand to Python
    changed goes base64-encoded after --compose-b64 (winshell.needs_encoding)."""
    if not config.IS_WINDOWS:
        parts = [shlex.quote(python), "-I", "-S", shlex.quote(entry), "statusline"]
        if compose:
            parts += ["--compose", shlex.quote(compose)]
        return " ".join(parts)
    argv = [_path_word(python)] + winshell.python_flags() + [_path_word(entry), "statusline"]
    if compose:
        argv += [winshell.COMPOSE_B64, winshell.encoded(compose)] if winshell.needs_encoding(compose) \
            else ["--compose", compose]
    bash = winshell.command_line(argv, "bash")
    if bash == winshell.command_line(argv, "powershell"):
        return bash
    if (shell or predicted_shell()) == "powershell":
        return "& " + " ".join(winshell.quote(w, "powershell") for w in argv)
    return bash


def split_command(cmd):
    """The argv of one of our statusLine commands, read with the rules of the shell it is
    written for: PowerShell's when only PowerShell reads it (on Windows, or after a `&`
    no sh reads), else POSIX sh's (Git Bash's), or None."""
    if not isinstance(cmd, str):
        return None
    if (config.IS_WINDOWS or cmd.lstrip()[:1] == "&") and winshell.written_for(cmd) == "powershell":
        return winshell.words(cmd, "powershell")
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def command_shell(cmd):
    """The one shell a statusLine command is written for on Windows ("bash" or
    "powershell"), as doctor reads it from the syntax: None when Git Bash and
    PowerShell read it the same (only such a command survives a change of shell
    without a repin), or when the syntax does not tell (the install record does)."""
    return winshell.written_for(cmd) if isinstance(cmd, str) else None


def compose_of(cmd):
    """The composed command embedded in one of our statusLine commands, as glidepath statusline
    reads it (statusline.parse_args, over the words after `statusline`): what follows the
    last --compose, or the last --compose-b64 decoded. A repin, doctor and uninstall then
    keep the command that runs, also after a hand edit that added a flag."""
    argv = split_command(cmd) or []
    words = argv[argv.index("statusline") + 1:] if "statusline" in argv else argv
    try:
        return statusline.parse_args(words)["compose_cmd"]
    except ValueError:  # a --now that is no number: glidepath statusline fails open, composing nothing
        return None


def settings_env(obj=None):
    """The `env` Claude Code lays over its environment for a settings file: the user file's,
    then that file's own (a project's wins), as far as glidepath can read them."""
    env = {}
    try:
        user = read_settings(settings_path(None))[0]
    except (OSError, ValueError):
        user = {}
    for o in (user, obj or {}):
        e = o.get("env") if isinstance(o, dict) else None
        if isinstance(e, dict):
            env.update((k, v) for k, v in e.items() if isinstance(k, str) and isinstance(v, str))
    return env


def home_problem(obj=None):
    """Windows: why glidepath refuses the GLIDEPATH_HOME of the settings env of `obj`,
    or of this process (config.windows_home), else None. Claude Code passes the
    settings env's value to the statusLine as written, and Git Bash rewrites a
    /x there before python.exe sees it: only here does the raw value show."""
    if not config.IS_WINDOWS:
        return None
    try:
        config.windows_home(settings_env(obj).get("GLIDEPATH_HOME"))
        config.home()
    except config.HomeError as e:
        return str(e)
    return None


def predicted_shell(obj=None):
    """The shell Claude Code will run the statusLine of settings `obj` with on Windows:
    "bash" or "powershell" (winshell.predict_shell, with that file's settings env)."""
    return winshell.predict_shell(os.environ, settings_env(obj))[0]


def plan_statusline(current, python, entry, replace=False, refresh=60, shell=None):
    """New statusLine value and a note, or (None, note) when nothing changes."""
    if isinstance(current, dict) and is_ours(current.get("command")):
        had = compose_of(current["command"])
        compose = None if replace else had
        want = dict(current)
        want["command"] = our_command(python, entry, compose, shell)
        want.setdefault("refreshInterval", refresh)
        if want == current:
            return None, "statusLine already runs glidepath"
        if replace and had:
            return want, "REPLACE: drop the composed statusLine command (kept in the backup and in install.json)"
        return want, "update glidepath's statusLine command (interpreter or path changed)"
    if isinstance(current, dict) and isinstance(current.get("command"), str) and current["command"].strip():
        if replace:
            return {"type": "command", "command": our_command(python, entry, None, shell), "refreshInterval": refresh}, (
                "REPLACE existing statusLine (kept in the backup and in install.json)"
            )
        want = dict(current)
        want["type"] = "command"
        want["command"] = our_command(python, entry, current["command"], shell)
        theirs = current.get("refreshInterval")
        want["refreshInterval"] = min(refresh, theirs) if isinstance(theirs, (int, float)) and theirs >= 1 else refresh
        return want, "compose: run your existing statusLine command and append glidepath's segment"
    return {"type": "command", "command": our_command(python, entry, None, shell), "refreshInterval": refresh}, \
        "add statusLine"


# -- plugin (mod) via the claude CLI -----------------------------------------------
def claude_bin():
    if config.IS_WINDOWS:
        # PATHEXT order in each PATH folder, as cmd.exe finds it: claude.exe (native, WinGet)
        # or claude.cmd (npm). npm's extensionless sh shim is nothing CreateProcess can start.
        found = config.which("claude")
        if found and os.path.splitext(found)[1].lower() in (".exe", ".cmd", ".bat"):
            return found
        return config.which("claude.exe") or config.which("claude.cmd")
    return config.which("claude")


# cmd.exe reads a .cmd's arguments again: these in a path would run as commands.
CMD_UNSAFE = set('&|<>^%!"')


def run_claude(args, env=None, cwd=None):
    exe = claude_bin()
    if not exe:
        return 127, "", "claude not found on PATH"
    if os.path.splitext(exe)[1].lower() in (".cmd", ".bat") and any(CMD_UNSAFE & set(a) for a in args):
        return 2, "", ("claude is %s, and cmd.exe would read one of & | < > ^ %% ! \" in %s as a command: "
                       "move the folder to a path without them, or install the native claude.exe"
                       % (os.path.basename(exe), " ".join(args)))
    try:
        return _run([exe] + args, timeout=CLAUDE_TIMEOUT, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        return 124, "", "claude %s timed out after %d s" % (" ".join(args[:3]), CLAUDE_TIMEOUT)
    except OSError as e:
        return 127, "", "could not run claude: %s" % e


def mod_commands(root):
    return [
        ["plugin", "marketplace", "add", root],
        ["plugin", "install", PLUGIN_ID, "--scope", "user"],
    ]


def _shown(argv):
    """A command line to print for the user to run: sh quoting, or on Windows double
    quotes, which cmd.exe and PowerShell both read."""
    return subprocess.list2cmdline(argv) if config.IS_WINDOWS else " ".join(shlex.quote(x) for x in argv)


def _run_mod_command(c, root, out):
    """Run one plugin command and print its outcome: (exit code, stdout, stderr). On Windows,
    a marketplace add that claude rejects is run again as `./` from the repo: the parser
    takes ./ on every OS, while whether it takes a drive path there is unconfirmed."""
    code, so, se = run_claude(c)
    if config.IS_WINDOWS and c[:3] == ["plugin", "marketplace", "add"] and code not in (0, 124, 127) \
            and "already" not in (so + se).lower():
        msg = (so.strip() or se.strip()).splitlines()
        out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
        out("  retrying from %s as: claude plugin marketplace add ./" % root)
        code, so, se = run_claude(c[:3] + ["./"], cwd=root)
    msg = (so.strip() or se.strip()).splitlines()
    out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
    return code, so, se


def unmod_commands():
    return [
        ["plugin", "uninstall", PLUGIN_ID, "--scope", "user"],
        ["plugin", "marketplace", "remove", MARKETPLACE],
    ]


def _user_keys(obj):
    """Which plugin keys the settings hold of their own: one that holds only
    glidepath's entry is the plugin CLI's (its commands were run by hand)."""
    return {k: k in obj and not (isinstance(obj[k], dict) and list(obj[k]) == [OWN_ENTRY[k]]) for k in PLUGIN_KEYS}


# -- launcher ------------------------------------------------------------------------
def launcher_name():
    return "glidepath.cmd" if config.IS_WINDOWS else "glidepath"


def launcher_text(python, entry):
    if config.IS_WINDOWS:
        # For running glidepath by hand only: the statusLine and the mod run python.exe itself
        # (Bun will not start a .cmd without a shell, and cmd.exe parses its arguments again).
        # cmd.exe reads a batch file one line at a time, opening it again by path and offset after
        # each command, and install and uninstall run from here rewrite or delete it. The failing
        # (goto) ends the batch on python's line, which still runs (%* expanded already), as in
        # npm's cmd-shim: nothing is read after python, and its exit code is the caller's.
        def q(p):
            return '"%s"' % p.replace("/", "\\").replace("%", "%%")

        return "@echo off\r\n%s\r\n(goto) 2>nul & %s %s %s %%*\r\n" % (
            CMD_MARK, q(python), " ".join(winshell.python_flags()), q(entry))
    return sh_launcher_text(python, entry)


def sh_launcher_text(python, entry):
    """The POSIX sh launcher: the launcher on macOS and Linux, and on Windows the `glidepath`
    beside glidepath.cmd that Git Bash runs (forward slashes, -X utf8)."""
    if config.IS_WINDOWS:
        python, entry = winshell.slashes(python), winshell.slashes(entry)
    return '#!/bin/sh\n%s\nexec %s %s %s "$@"\n' % (
        LAUNCHER_MARK, shlex.quote(python), " ".join(winshell.python_flags()), shlex.quote(entry))


def _sh_launcher_path(lp, old_sh, explicit):
    """Windows: the sh launcher to write, or None. Git Bash (and Claude Code's Bash tool, which
    runs in it) looks a command up by its very name, never with .cmd, so `glidepath` there needs a
    file of that name beside glidepath.cmd (lp), as npm's cmd-shim writes. As with glidepath.cmd, one
    the user deleted since an earlier install comes back only with --bin-dir: install.json keeps
    its record, so every repin leaves it out. One the user kept after deleting glidepath.cmd (lp is
    None), or after putting a file of their own in its place, is still glidepath's, so it follows
    the pin where it is: it never runs an old interpreter."""
    if not config.IS_WINDOWS or (not explicit and old_sh and not os.path.exists(old_sh)):
        return None
    return os.path.join(os.path.dirname(lp), "glidepath") if lp else old_sh


def _write_sh_launcher(sh, python, entry, state, out):
    """Write the sh launcher at sh, unless a file of the user's is there. Its record follows the
    file: it names the one just written, and goes for a file of the user's, which glidepath never
    writes over or removes. True when written."""
    if os.path.exists(sh) and not _is_launcher(sh):
        out("Skipped launcher: %s exists and is not ours" % sh)
        state.pop("sh_launcher", None)
        return False
    with open(sh, "wb") as f:  # LF and UTF-8, as Git Bash reads it
        f.write(sh_launcher_text(python, entry).encode("utf-8"))
    state["sh_launcher"] = sh
    out("Wrote launcher %s" % sh)
    return True


def _launcher_bytes(python, entry):
    """cmd.exe reads a batch file in the console code page (the OEM one, unless chcp changed
    it), so a path outside ASCII has to be written in it."""
    text = launcher_text(python, entry)
    if config.IS_WINDOWS:
        try:
            codecs.lookup("oem")
            return text.encode("oem")
        except (LookupError, UnicodeEncodeError):
            pass
    return text.encode("utf-8")


def _is_launcher(path):
    """The file is a launcher glidepath wrote (sh or cmd), not the user's."""
    try:
        text = read_text(path)
    except OSError:
        return False
    return LAUNCHER_MARK in text or CMD_MARK in text


def runtime_info(python, entry):
    """runtime.json, read by the mod: it runs python with args, then entry."""
    return {"python": python, "args": winshell.python_flags(), "entry": entry, "repo": repo_root(),
            "version": __version__, "written_at": time.time()}


# -- install records ---------------------------------------------------------------
STATE_VERSION = 2


def load_state():
    """{"version": 2, "records": {abs settings path: record}, ...globals}."""
    try:
        with open(state_path(), encoding="utf-8-sig") as f:  # a BOM (a Windows editor) is fine
            state = json.load(f)
    except (OSError, ValueError):
        state = None
    if not isinstance(state, dict):
        state = {}
    if "records" not in state:  # version 1: one record at the top level
        rec = {k: state[k] for k in ("settings_path", "had_file", "backup_path", "original_sha256", "had_statusLine",
                                     "original_statusLine", "had_keys", "installed", "written_statusLine") if k in state}
        # Version 1's uninstall restored the file and left the record behind: it is spent.
        path = _v1_key(rec) if rec.get("installed") is not False else None
        state = {k: v for k, v in state.items() if k not in rec}
        if path:
            rec["settings_path"] = path
        state["records"] = {path: rec} if path else {}
    state["version"] = STATE_VERSION
    return state


BACKUP_SUFFIX = re.compile(r"\.(?:glidepath|pacer|ctempo)-backup-\d{8}-\d{6}Z?(-\d+)?$")  # earlier names too


def _v1_key(rec):
    """Absolute path of a version-1 record. Version 1 stored --settings as
    typed (maybe relative); its backup path was built from the real path."""
    path = rec.get("settings_path")
    if isinstance(path, str) and os.path.isabs(path):
        return os.path.abspath(path)
    backup = rec.get("backup_path")
    if isinstance(backup, str) and BACKUP_SUFFIX.search(backup):
        return BACKUP_SUFFIX.sub("", backup)
    if isinstance(path, str) and path:
        return os.path.abspath(path)
    return None


def find_record(state, spath):
    """(key, record) for a settings file, matching symlink aliases too."""
    records = state.setdefault("records", {})
    if spath in records:
        return spath, records[spath]
    real = os.path.realpath(spath)
    for key, rec in records.items():
        if os.path.realpath(key) == real:
            return key, rec
    return spath, None


def save_state(state):
    """install.json, readable by the user alone: its records hold the
    statusLine commands found in settings.json, which may carry tokens."""
    config.private_dir(config.home())
    fd, tmp = tempfile.mkstemp(dir=config.home(), prefix="install.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _replace(tmp, state_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def installed_records(state):
    """Installed records, one per real file (aliases through symlinks count once)."""
    out, seen = {}, set()
    for p, r in state.get("records", {}).items():
        real = os.path.realpath(p)
        if r.get("installed") and real not in seen:
            seen.add(real)
            out[p] = r
    return out


def _backup_order(name):
    """Sort key of a backup name: a name ending in Z after every older one without it
    (an older version wrote those), then its time, then its sequence number ("-10" after "-9")."""
    m = re.search(r"-(\d{8})-(\d{6})(Z?)(?:-(\d+))?$", name)
    if not m:
        return (0, float("-inf"), 0)
    try:  # names ending in Z are UTC; older ones are local time, in a zone that may have changed since
        tt = time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        t = calendar.timegm(tt) if m.group(3) else time.mktime(tt)
    except (ValueError, OverflowError):
        t = float("-inf")
    return (1 if m.group(3) else 0, t, int(m.group(4) or 0))


def _original_from_backups(spath):
    """The statusLine in the newest backup that predates glidepath, if any. A command
    in glidepath's --compose in a newer backup (the user put it there, and --replace
    dropped it since) replaces its command, or is the statusLine when no backup
    predates glidepath."""
    real = os.path.realpath(spath)
    folder = os.path.dirname(real) or "."
    prefixes = tuple(os.path.basename(real) + "." + name + "-backup-" for name in NAMES)
    try:
        names = sorted((n for n in os.listdir(folder) if n.startswith(prefixes) and BACKUP_SUFFIX.search(n)),
                       key=_backup_order, reverse=True)
    except OSError:
        return None
    composed = None
    for n in names:
        try:
            obj = parse_json(read_text(os.path.join(folder, n)))  # a blank file is {}, as in read_settings
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        sl = obj.get("statusLine")
        if not (isinstance(sl, dict) and is_ours(sl.get("command"))):
            rec = _with_compose({"had_statusLine": "statusLine" in obj, "original_statusLine": sl}, composed)
            return rec["had_statusLine"], rec["original_statusLine"]
        if composed is None and compose_of(sl["command"]):
            composed = sl
    comp = compose_of(composed["command"]) if composed else None
    if comp and not is_ours(comp):
        return True, dict(composed, command=comp)
    return None


def _statusline_part(obj, spath):
    """What install should remember as the original statusLine."""
    current = obj.get("statusLine")
    if isinstance(current, dict) and is_ours(current.get("command")):
        # Already glidepath's (install record lost, or repinned): never record
        # glidepath itself as the original. Prefer a pre-glidepath backup, else
        # what --compose says.
        found = _original_from_backups(spath)
        if found is not None:
            return {"had_statusLine": found[0], "original_statusLine": found[1]}
        comp = compose_of(current["command"])
        if comp:
            return {"had_statusLine": True, "original_statusLine": dict(current, command=comp)}
        return {"had_statusLine": None, "original_statusLine": None}
    return {"had_statusLine": "statusLine" in obj, "original_statusLine": current}


def _with_compose(rec, cur):
    """The record with the command in glidepath's --compose as the original: the
    user may have changed it there, where settings.json shows what runs (D-17)."""
    comp = compose_of(cur["command"]) if isinstance(cur, dict) and isinstance(cur.get("command"), str) else None
    orig = rec.get("original_statusLine")
    if comp and not is_ours(comp) and rec.get("had_statusLine") is False:
        # No statusLine before glidepath, which then composes nothing: the user put it
        # there. Keep their keys, not the refreshInterval glidepath added.
        sl = {k: v for k, v in cur.items() if k != "refreshInterval"}
        return dict(rec, had_statusLine=True, original_statusLine=dict(sl, command=comp))
    if comp and not is_ours(comp) and rec.get("had_statusLine") is True and isinstance(orig, dict) \
            and not is_ours(orig.get("command")) and orig.get("command") != comp:
        return dict(rec, original_statusLine=dict(orig, command=comp))
    return rec


# -- install / uninstall -------------------------------------------------------------
def _shell_notes(cmd, shell, out):
    """Windows: what install tells about the statusLine command it leaves: a previous
    statusLine stored encoded, and a command that runs in one shell only."""
    if winshell.COMPOSE_B64 in (split_command(cmd) or []):
        why = "Git Bash would rewrite the POSIX path it starts with"
        if not winshell.passes_intact(compose_of(cmd) or ""):
            why = "Windows PowerShell 5.1 would pass its double quotes (or final backslash) to Python changed"
        out("  note: your previous statusLine is stored base64-encoded after %s, since %s; `glidepath doctor` shows it "
            "decoded" % (winshell.COMPOSE_B64, why))
    if winshell.reads_alike(cmd):
        return
    out("  warning: this command runs only in %s; after installing or removing Git for Windows, or setting "
        "CLAUDE_CODE_GIT_BASH_PATH, run `glidepath install --apply` again (`glidepath doctor` checks this)"
        % winshell.SHELLS[winshell.written_for(cmd) or shell])
    if winshell.getenv(os.environ, "MSYSTEM"):
        # Git Bash puts its own git first on PATH, which can change what Claude Code finds.
        out("  note: Claude Code started from this Git Bash terminal can pick another shell than when started from "
            "the Desktop app or PowerShell; run `glidepath doctor` from PowerShell to check")


def install(args, out=print):
    spath = settings_path(args.settings)
    try:
        obj, raw, fmt = read_settings(spath)
    except ValueError as e:
        out("error: %s" % e)
        return 1
    bad = home_problem(obj)
    if bad:  # the statusLine and the mod would use different folders, or none
        out("error: %s" % bad)
        return 1
    python, report = choose_python(args.python)
    out("Interpreter candidates (python -I -S startup, best of 3):")
    for p, ok, ver, sq, ms, note in report:
        out(
            "  %s %-40s %s%s%s"
            % (
                "*" if p == python else ("+" if ok else "-"),
                p,
                ("Python %s, sqlite %s" % (ver, sq)) if ver else "",
                (", %.0f ms" % ms) if ms else "",
                (" (" + note + ")") if note else "",
            )
        )
    if not python:
        out("error: no usable Python 3.8+ interpreter found; pass --python /abs/path")
        return 1
    entry = entry_path()
    current = obj.get("statusLine")
    # On Windows the command's syntax may depend on the shell Claude Code will run it with.
    shell, shell_path = winshell.predict_shell(os.environ, settings_env(obj)) if config.IS_WINDOWS else (None, None)
    want, note = plan_statusline(current, python, entry, replace=args.replace, shell=shell)
    new_obj = dict(obj)
    if want is not None:
        new_obj["statusLine"] = want
    new_raw = dumps(new_obj, fmt) if want is not None else raw
    out("")
    out("Settings: %s" % spath)
    out("  statusLine: %s" % note)
    if shell:
        cmd = (want if want is not None else current)["command"]
        out("  Claude Code runs it with %s here%s" % (
            winshell.SHELLS[shell] + ("" if shell == "bash" else " (no Git Bash found)"),
            "; the command reads the same in Git Bash and PowerShell" if winshell.reads_alike(cmd) else ""))
        if shell_path is None:
            out("  warning: no Git Bash and no PowerShell found, so Claude Code cannot run the statusLine here")
        _shell_notes(cmd, shell, out)
    if want is not None:
        d = diff(raw, new_raw, spath)
        out(d if d else "  (no textual change)")
    root = repo_root()
    use_mod = not args.no_mod
    if use_mod:
        out("Mod (Desktop Code tab band), via the local marketplace in %s:" % root)
        for c in mod_commands(root):
            out("  claude " + _shown(c))
        if not claude_bin():
            out("  (claude is not on PATH: run these yourself later)")
            if config.IS_WINDOWS:
                out("  (if claude rejects the folder: cd %s, then run claude plugin marketplace add ./)" % _shown([root]))
    bin_dir = os.path.abspath(os.path.expanduser(args.bin_dir)) if args.bin_dir else None
    # Without --bin-dir, each launcher an earlier install wrote follows the new pin,
    # and one the user deleted since stays deleted until a --bin-dir. On Windows that
    # holds for glidepath.cmd and the sh launcher for Git Bash beside it, each on its own.
    prev = load_state()
    old_lp, old_sh = prev.get("launcher"), prev.get("sh_launcher")
    lp = os.path.join(bin_dir if bin_dir else os.path.dirname(old_lp), launcher_name()) \
        if bin_dir or (old_lp and os.path.exists(old_lp)) else None
    sh = _sh_launcher_path(lp, old_sh, bool(bin_dir))
    if lp:
        out("Launcher: %s -> %s %s %s" % (lp, python, " ".join(winshell.python_flags()), entry))
    if sh:
        out("Launcher for Git Bash: %s" % sh)
    from . import legacy

    moving = legacy.pending_move()
    if moving:
        out("Data folder: %s -> %s (glidepath was called %s)" % (moving[1], moving[2], moving[0].name))
    out("Runtime info for the mod: %s" % (os.path.join(moving[2], "runtime.json") if moving else config.runtime_path()))
    if not args.no_bootstrap:
        out("Profile: bootstrap the prior from ~/.claude/projects transcripts (timestamps and token counts only)")
    if not args.apply:
        out("")
        out("DRY RUN: nothing was written. Re-run with --apply to install.")
        return 0

    # Apply. The record goes to disk before the settings file changes, so a
    # crash at any later point still leaves uninstall what it needs.
    legacy.move_home(out)  # an install from before the rename keeps its readings
    state = load_state()
    records = state.setdefault("records", {})
    key, rec = find_record(state, spath)
    if key != spath and rec is not None:  # the same file through another name: one record
        records[spath] = records.pop(key)
    cur_ours = isinstance(current, dict) and is_ours(current.get("command"))
    # On Windows, the shell the command was written for, so doctor can tell when that changes.
    shell_rec = {winshell.RECORD_KEY: shell} if shell else {}
    if want is not None:
        backup = make_backup(spath) if raw is not None else None
        user_file = os.path.realpath((state.get("mod") or {}).get("user_settings") or "") == os.path.realpath(spath)
        if (rec is None or not rec.get("installed")) and user_file and raw is not None:
            # The mod is in already, so the plugin CLI may have written here: the
            # file to come back to is this one, else (pristine) the one before the
            # mod, from this file's kept record or the snapshot taken then.
            base = rec if rec is not None else state["mod"]
            pristine = base.get("pristine") or {k: base.get(k) for k in ("had_file", "backup_path", "original_sha256")}
            rec = {"settings_path": spath, "had_file": True, "had_keys": dict(base.get("had_keys") or {}),
                   "backup_path": backup, "original_sha256": sha(raw), "pristine": pristine}
            rec.update(_statusline_part(obj, spath))
        elif rec is None or not rec.get("installed"):
            rec = {"settings_path": spath, "had_file": raw is not None, "had_keys": _user_keys(obj)}
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
        elif not cur_ours:
            # The statusLine changed since the last install (or the file was
            # deleted): that is now the original, and the backup just taken
            # is the file to come back to.
            rec.update(_statusline_part(obj, spath))
            rec.update(backup_path=backup, original_sha256=sha(raw) if raw is not None else None)
            rec["had_file"] = raw is not None
            if raw is None:
                rec["had_keys"] = {k: False for k in PLUGIN_KEYS}
        elif rec.get("had_statusLine") is None:  # no usable record: a backup, else --compose
            rec.update(_statusline_part(obj, spath))
        if cur_ours:
            rec = _with_compose(rec, current)  # a repin or --replace must not lose an edited --compose
        rec.update(installed=True, written_statusLine=want, **shell_rec)
        records[spath] = rec
        state.update(python=python, entry=entry)
        save_state(state)
        atomic_write(spath, new_raw)
        out("Wrote %s%s" % (spath, (" (backup: %s)" % backup) if backup else ""))
    elif rec is None and cur_ours:
        rec = {"settings_path": spath, "had_file": True, "had_keys": {}, "installed": True}
        rec.update(_statusline_part(obj, spath))
        rec.update(shell_rec)
        records[spath] = rec
    elif rec is not None and rec.get("installed"):
        rec.update(shell_rec)  # the command is already the one for this shell
    rc = 0
    try:
        state.update(python=python, entry=entry)
        config.private_dir(config.home())
        with open(config.runtime_path(), "w", encoding="utf-8") as f:
            json.dump(runtime_info(python, entry), f, indent=2)
        config.write_defaults()
        if lp:
            try:
                if bin_dir:
                    os.makedirs(bin_dir, exist_ok=True)
                if not os.path.exists(lp) or _is_launcher(lp):
                    with open(lp, "wb") as f:
                        f.write(_launcher_bytes(python, entry))
                    if not config.IS_WINDOWS:
                        os.chmod(lp, 0o755)
                    state["launcher"] = lp
                    out("Wrote launcher %s" % lp)
                    keep = [lp]
                    # sh is None on Windows when the user deleted glidepath's: its record stays for later repins.
                    if sh and _write_sh_launcher(sh, python, entry, state, out):
                        keep.append(sh)
                    # Only the newest launchers are glidepath's: those in an earlier --bin-dir go (a glidepath
                    # an older version wrote beside glidepath.cmd on Windows is the sh launcher now). The
                    # same file under another name (case on macOS, a bind mount) is not one realpath can tell.
                    for old in (old_lp, old_sh):
                        if old and os.path.exists(old) and not any(os.path.samefile(old, k) for k in keep) \
                                and _is_launcher(old):
                            os.remove(old)
                            out("Removed launcher %s" % old)
                else:
                    out("Skipped launcher: %s exists and is not ours" % lp)
                    if sh and os.path.exists(sh) and _is_launcher(sh):  # with --bin-dir naming its folder too
                        _write_sh_launcher(sh, python, entry, state, out)  # ours, beside the user's glidepath.cmd
            except OSError as e:
                out("Skipped launcher: %s" % e)
                rc = 1
        else:
            if old_lp:
                state.pop("launcher", None)  # the user deleted it
            if sh:  # Windows: the user deleted glidepath.cmd and kept the sh launcher, which follows the pin
                try:
                    _write_sh_launcher(sh, python, entry, state, out)
                except OSError as e:
                    out("Skipped launcher: %s" % e)
                    rc = 1
        if use_mod:
            m = state.get("mod") or {}
            # No snapshot yet (or an early mod entry without one), or a manual mod
            # (the plugin CLI never wrote for it): the user file may have changed since.
            if not m.get("user_settings") or (m.get("manual") and claude_bin() and not _cli_wrote(m)):
                # The plugin CLI edits the user settings file: remember
                # which plugin keys it had, so uninstall can tidy up there.
                # The mod is manual until glidepath sees the plugin CLI write there.
                us = settings_path(None)
                try:
                    uobj, uraw, _ = read_settings(us)
                    had_keys = _user_keys(uobj)
                except ValueError:  # not valid JSON: it exists, and its keys are unknown, so never removed
                    uobj, uraw, had_keys = {}, None, {k: True for k in PLUGIN_KEYS}
                # A key the user holds empty stays theirs once the printed commands fill it (_forget_keys).
                state["mod"] = {"user_settings": us, "had_file": uraw is not None or os.path.exists(os.path.realpath(us)),
                                "had_keys": had_keys, "manual": True,
                                "empty_keys": [k for k in PLUGIN_KEYS if uobj.get(k) == {}]}
                if uraw is not None and os.path.realpath(us) != os.path.realpath(spath):
                    try:
                        state["mod"].update(backup_path=make_backup(us), original_sha256=sha(uraw))
                    except OSError:
                        pass  # uninstall then tidies by keys instead of bytes
                _, urec = find_record(state, us)
                if urec is not None and not urec.get("installed"):
                    # Kept from before this snapshot, and the plugin CLI has not
                    # written there since: the snapshot is the newer baseline.
                    _drop_records(state, os.path.realpath(us))
            _forget_keys(state["mod"], bool(state["mod"].get("manual")))
            us = state["mod"]["user_settings"]
            before = _peek(us)
            if claude_bin():
                for old in legacy.old_mods_listed(us):
                    legacy.remove_old_mod(old, run_claude, out)
                for c in mod_commands(root):
                    code, so, se = _run_mod_command(c, root, out)
                    if code != 0 and "already" not in (so + se).lower():
                        rc = 1
            else:
                # The user may run the commands printed above: record the mod
                # anyway, so uninstall undoes them.
                out("  claude not on PATH: mod not installed")
            if _peek(us) != before or _mod_listed(us):
                state["mod"].pop("manual", None)  # the plugin CLI has written there
            state["mod"].update(marketplace=MARKETPLACE, plugin=PLUGIN_ID, root=root)
    finally:
        save_state(state)
    if not args.no_bootstrap:
        try:
            from . import transcripts
            from .store import Store

            st = Store()
            try:
                prior, meta, stats = transcripts.bootstrap(st, config.load())
            finally:
                st.close()
            if prior is not None:
                out("Profile prior from transcripts: %d messages over %s days, trusted %.0f%% (no content kept)." % (
                    stats["messages"], meta["days"], 100 * meta["trust"]))
            else:
                out("No transcripts in the last %s days: the profile starts uniform." % config.load()["profile"]["bootstrap_days"])
        except Exception as e:  # bootstrap is a bonus; never fail the install on it
            out("Bootstrap skipped: %s (run `glidepath bootstrap` later)" % e)
    legacy.move_menubar(out)  # SwiftBar's plugin from before the rename
    out("Done. Restart Claude Code or run /reload-plugins to load the mod; the statusLine applies at once.")
    return rc


def _done(code, so, se):
    """A plugin command succeeded, or there was nothing left to remove."""
    if code == 0:
        return True
    if code in (124, 127):  # claude timed out or could not run: nothing happened
        return False
    text = (so + se).lower()
    return "not found in installed plugins" in text or ("marketplace" in text and "not found" in text) \
        or "not installed" in text


def _peek(path):
    """The file's bytes, or None when there is no such file."""
    try:
        with open(os.path.realpath(path), "rb") as f:
            return f.read()
    except (OSError, TypeError):
        return None


def _mod_listed(us):
    """Whether the user settings file names the mod or its marketplace (True when it cannot tell)."""
    if not us:
        return True
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return True
    return _lists_mod(obj)


def _lists_mod(obj):
    ep, km = obj.get("enabledPlugins"), obj.get("extraKnownMarketplaces")
    return (isinstance(ep, dict) and PLUGIN_ID in ep) or (isinstance(km, dict) and MARKETPLACE in km)


def _listed_mod():
    """A mod record rebuilt from the user settings file when that lists the mod
    or its marketplace, else None (also when the file cannot be read)."""
    us = settings_path(None)
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return None
    if not _lists_mod(obj):
        return None
    # No snapshot to restore bytes from: tidy by keys, where one that holds only
    # glidepath's entry is the plugin CLI's.
    return {"user_settings": us, "had_file": True, "had_keys": _user_keys(obj)}


def _forget_keys(mod, out_of_sight=False):
    """A plugin key the user file no longer holds at all (the user deleted or
    blanked the file since) is not the user's: tidy it away once empty. A key
    holding only glidepath's entry counts as gone only when the plugin CLI wrote
    it out of glidepath's sight (a manual mod whose commands the user ran) and the
    key was not empty at the snapshot: otherwise that is just the user's own
    empty key with the mod in, also after the user added and removed other
    plugins since."""
    try:
        obj = read_settings(mod["user_settings"])[0]
    except (KeyError, TypeError, OSError, ValueError):
        return
    now = {k: k in obj for k in PLUGIN_KEYS}
    if out_of_sight:
        own = _user_keys(obj)
        now = {k: own[k] or (now[k] and k in (mod.get("empty_keys") or ())) for k in PLUGIN_KEYS}
    if mod.get("had_keys"):
        mod["had_keys"] = {k: bool(had) and now.get(k, False) for k, had in mod["had_keys"].items()}


def _cli_wrote(mod):
    """Whether the plugin CLI has written to the user settings file: it lists the
    mod, or has a plugin key it did not have before (True when it cannot tell)."""
    us = mod.get("user_settings")
    if _mod_listed(us):
        return True
    try:
        obj, _, _ = read_settings(us)
    except (OSError, ValueError):
        return True
    return any(k in obj and not had for k, had in (mod.get("had_keys") or {}).items())


def _tidy_user_settings(state, mod, skip_real, touched=True):
    """Undo what the plugin CLI left in the user settings file: back to its
    backup's bytes when nothing else changed there, else drop empty keys.
    When the plugin CLI never wrote there, only the file's kept record goes."""
    us = mod.get("user_settings") or settings_path(None)  # version 1 named none: the default one
    if os.path.realpath(us) == skip_real:
        return None
    key, rec = find_record(state, us)
    if rec is not None and rec.get("installed"):
        return None  # glidepath's statusLine is still there: its own uninstall restores the file
    if not touched or not os.path.exists(os.path.realpath(us)):
        _drop_records(state, os.path.realpath(us))  # the mod goes, and so does the file's kept record
        return None
    # Its own record (a statusLine uninstalled earlier, kept for this), else
    # the snapshot taken before the plugin CLI first wrote to it.
    base = dict(rec) if rec is not None else {
        "settings_path": us, "had_file": mod.get("had_file"),
        "backup_path": mod.get("backup_path"), "original_sha256": mod.get("original_sha256")}
    hk = dict(base.get("had_keys") or {})
    for k, had in (mod.get("had_keys") or {}).items():
        hk[k] = hk.get(k, had) and had
    base["had_keys"] = hk
    try:
        obj, raw, fmt = read_settings(us)
    except ValueError as e:
        _drop_records(state, os.path.realpath(us))
        return "Left %s alone: %s" % (us, e)
    _, new_raw = restore(obj, raw, fmt, base)
    _drop_records(state, os.path.realpath(us))
    if new_raw is None:
        _remove(os.path.realpath(us))
        return "Removed %s (it did not exist before the mod was installed)" % us
    if new_raw == raw or new_raw == (raw or "").encode("utf-8"):
        return None
    atomic_write(us, new_raw)
    return "Tidied %s" % us


def _drop_records(state, real):
    """Remove every record of a file, whatever name it was recorded under."""
    records = state.setdefault("records", {})
    for p in [p for p in records if os.path.realpath(p) == real]:
        records.pop(p, None)


def uninstall(args, out=print):
    config.home()  # a GLIDEPATH_HOME glidepath refuses (config.HomeError) stops it before anything changes
    state = load_state()
    records = state.setdefault("records", {})
    if args.settings:
        spath = settings_path(args.settings)
    else:
        live = installed_records(state)
        if len(live) > 1:
            out("error: glidepath is installed in several settings files; pass --settings one of:")
            for p in sorted(live):
                out("  " + p)
            return 1
        spath = next(iter(live)) if live else settings_path(None)
    key, rec = find_record(state, spath)
    rec = dict(rec or {})
    rec.setdefault("settings_path", spath)
    real = os.path.realpath(spath)
    mod = state.get("mod") or {}
    rebuilt = None if mod else _listed_mod()
    if rebuilt:
        # install.json was lost (or this is another GLIDEPATH_HOME), but the user file
        # still lists the mod: uninstall removes it all the same (--no-mod keeps it,
        # and runtime.json with it).
        mod = state["mod"] = rebuilt
    us = mod.get("user_settings")
    out_of_sight = bool(mod.get("manual"))
    if mod.get("manual") and _mod_listed(us):
        mod.pop("manual")  # the user ran the printed commands: the plugin CLI has written there
    _forget_keys(mod, out_of_sight)
    if mod.get("had_keys") and os.path.realpath(us or "") == real:
        # This is the file the plugin CLI edits: its record of plugin keys counts.
        hk = dict(rec.get("had_keys") or {})
        for k, had in mod["had_keys"].items():
            hk[k] = hk.get(k, had) and had
        rec["had_keys"] = hk
        if "had_file" not in rec:  # no record of its own: the snapshot taken before the plugin CLI wrote here
            rec.update({k: mod[k] for k in ("had_file", "backup_path", "original_sha256") if k in mod})
    if not os.path.exists(real):
        out("Settings: %s does not exist; nothing to restore there." % spath)
        obj, raw, fmt = None, None, None
    else:
        try:
            obj, raw, fmt = read_settings(spath)
        except ValueError as e:
            out("error: %s" % e)
            return 1
        out("Settings: %s" % spath)
    others = [p for p in installed_records(state) if os.path.realpath(p) != real]
    cmds = unmod_commands() if (mod and not args.no_mod and not others) else []
    if rebuilt and cmds:
        out("  (%s lists the glidepath mod, but %s has no record of it: uninstall removes it too)" % (us, state_path()))
    for c in cmds:
        out("  claude " + " ".join(c))
    # With claude not on PATH and no trace of the mod in the user file (the
    # commands never ran or failed, or the user undid them by hand), whatever
    # the record says, there is nothing to undo.
    never_ran = bool(cmds) and not claude_bin() and not _mod_listed(us)
    # The plugin CLI never wrote to the user file (install left the commands to
    # the user, who never ran them, or they timed out), so it is not ours to
    # rewrite, unless the commands below write there.
    untouched = bool(cmds and mod.get("manual")) and not _cli_wrote(mod)
    if never_ran:
        out("  (claude is not on PATH and %s lists no glidepath mod: nothing to undo)" % us)
    cur = obj.get("statusLine") if obj else None
    ours = isinstance(cur, dict) and is_ours(cur.get("command"))
    if obj is not None and not ours:
        out("  statusLine is not glidepath's; it is left alone")
    if not args.apply:
        if obj is not None and ours:
            new_obj, new_raw = restore(obj, raw, fmt, rec)
            d = diff(raw, new_raw if isinstance(new_raw, str) else (new_raw or b"").decode("utf-8"), spath) \
                if new_raw is not None else "  (file would be removed: it did not exist before install)\n"
            out(d if d else "  (no change)")
        if cmds:
            out("  (the plugin commands also remove enabledPlugins / extraKnownMarketplaces entries)")
        out("DRY RUN: nothing was written. Re-run with --apply to uninstall.")
        return 0
    mod_ok = True
    before = _peek(us)
    for c in [] if never_ran else cmds:
        code, so, se = run_claude(c)
        msg = (so.strip() or se.strip()).splitlines()
        out("  claude %s -> exit %d %s" % (" ".join(c[:3]), code, msg[-1] if msg else ""))
        mod_ok = mod_ok and _done(code, so, se)
    untouched = untouched and _peek(us) == before
    if os.path.exists(real):
        obj, raw, fmt = read_settings(spath)  # the plugin CLI may have rewritten (or created) it
        cur = obj.get("statusLine")
        ours = isinstance(cur, dict) and is_ours(cur.get("command"))
        mine = not (untouched and os.path.realpath(us) == real)  # not the user file the plugin CLI never wrote to
        if ours or (cmds and mod_ok and mine):
            new_obj, new_raw = restore(obj, raw, fmt, rec if (mod_ok or not cmds) else dict(rec, had_keys={}))
            if new_raw is None:
                _remove(real)
                out("Removed %s (it did not exist before install)" % spath)
            elif new_raw != raw and new_raw != (raw or "").encode("utf-8"):
                atomic_write(spath, new_raw)
                out("Restored %s" % spath)
    if cmds and mod_ok:
        msg = _tidy_user_settings(state, mod, real, touched=not untouched)
        if msg:
            out(msg)
    _drop_records(state, real)
    # A mod record without user_settings (version 1) had its commands edit the default user file.
    if mod and not (cmds and mod_ok) and os.path.realpath(mod.get("user_settings") or settings_path(None)) == real:
        # The plugin keys in this file outlive this run (the mod stays for
        # another settings file, or its commands failed): keep the record so
        # whoever removes the mod can bring the file back byte for byte.
        records[spath] = dict(rec, installed=False)
    rc = 0
    if not installed_records(state):
        if cmds and not mod_ok:
            out("warning: the plugin commands failed, so the glidepath mod may still be installed. "
                "Run `glidepath uninstall --apply` again with claude on PATH, or the commands above yourself.")
            rc = 1  # keep state["mod"] and runtime.json so a rerun retries
        else:
            if cmds and mod_ok:
                state.pop("mod", None)
            if state.get("mod"):  # --no-mod: the mod stays, and it reads runtime.json
                out("Kept the mod (--no-mod) and %s, which it reads; `glidepath uninstall --apply` removes both."
                    % config.runtime_path())
            else:
                for key in ("launcher", "sh_launcher"):
                    lp = state.pop(key, None)
                    if lp and os.path.exists(lp) and _is_launcher(lp):
                        os.remove(lp)
                        out("Removed launcher %s" % lp)
                from . import menubar

                mp = state.pop("menubar", None)
                if menubar.remove(mp):
                    out("Removed the menu bar plugin %s" % mp)
                if os.path.exists(config.runtime_path()):
                    os.remove(config.runtime_path())
    save_state(state)
    if rc == 0:
        out("Uninstalled. Your data stays in %s (delete it yourself if you want)." % config.home())
    else:
        out("Partly uninstalled.")
    return rc


def restore(obj, raw, fmt, rec):
    """Settings with glidepath's statusLine undone: (obj, text or bytes), or (obj, None) to delete the file."""
    new = dict(obj)
    cur = obj.get("statusLine")
    if isinstance(cur, dict) and is_ours(cur.get("command")):
        orig = rec.get("original_statusLine")
        orig_ours = isinstance(orig, dict) and is_ours(orig.get("command"))
        usable = rec.get("had_statusLine") is False or (rec.get("had_statusLine") is True and not orig_ours)
        found = None if usable or not rec.get("settings_path") else _original_from_backups(rec["settings_path"])
        if found is not None:
            # No usable record: rebuild it as install would (a pre-glidepath backup), so
            # a command the user put in --compose wins over the backup here too.
            rec = dict(rec, had_statusLine=found[0], original_statusLine=found[1])
        rec = _with_compose(rec, cur)
        orig = rec.get("original_statusLine")
        orig_ours = isinstance(orig, dict) and is_ours(orig.get("command"))
        if rec.get("had_statusLine") is True and not orig_ours:
            new["statusLine"] = orig
        elif rec.get("had_statusLine") is False:
            new.pop("statusLine", None)
        else:  # no usable record and no backup from before glidepath: what the command says
            comp = compose_of(cur["command"])
            if comp:
                new["statusLine"] = dict(cur, command=comp)
            else:
                new.pop("statusLine", None)
    for k, had in (rec.get("had_keys") or {}).items():
        if not had and k in new and new[k] in ({}, []):
            new.pop(k)
    pristine = rec.get("pristine") or {}
    if rec.get("had_file") is False and not new:
        return new, None
    # Byte-exact when nothing else changed: hand back the backup's bytes (the
    # file before this install, else the one before the mod).
    for snap in (rec, pristine):
        backup = snap.get("backup_path")
        if not (backup and os.path.exists(backup)):
            continue
        try:
            with open(backup, "rb") as f:
                orig_bytes = f.read()
            orig_obj = parse_json(orig_bytes.decode("utf-8"))  # a blank file is {}, as in read_settings
            if orig_obj == new and hashlib.sha256(orig_bytes).hexdigest() == snap.get("original_sha256"):
                return new, orig_bytes
        except (OSError, ValueError):
            pass
    if pristine.get("had_file") is False and not new:  # no file before the mod, nor one to come back to
        return new, None
    if new == obj and raw is not None:
        return new, raw
    return new, dumps(new, fmt)
