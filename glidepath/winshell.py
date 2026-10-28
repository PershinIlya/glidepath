"""How Claude Code runs a statusLine command on Windows, and how to write one.

On Windows Claude Code runs the statusLine through Git Bash when it finds one
(`bash.exe -c "<command>"`), else through PowerShell (pwsh first, then
Windows PowerShell 5.1) as `-NoProfile -NonInteractive -ExecutionPolicy
Bypass -Command "<command>"`. The statusLine setting has no `shell` or `args`
field, so glidepath predicts the shell the way Claude Code finds it, writes a
command that reads right there (D-17), and runs a composed command the same
way. A command made of bare words reads the same in both shells, so it keeps
working when the shell Claude Code picks changes.

Every function takes the environment as a mapping and uses Windows path rules
(ntpath) on any OS, so the Windows branches are tested anywhere;
config.IS_WINDOWS says when they apply.
"""

import ntpath
import os
import re
import subprocess

from . import config

SHELLS = {"bash": "Git Bash", "powershell": "PowerShell"}
# Key of a settings record in install.json: the shell the command was written for.
RECORD_KEY = "shell"
GIT_BASH = (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe")
# ${VAR} placeholders Claude Code rewrites to ${env:VAR} for PowerShell.
PS_PLACEHOLDERS = ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")
# PowerShell reads each of these as a single quote, inside a quoted word too.
PS_QUOTES = "'\u2018\u2019\u201a\u201b"
ASCII_ALNUM = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
# Characters that mean nothing to bash or PowerShell inside a word.
BARE_ASCII = ASCII_ALNUM | frozenset("_./:+=-")
# glidepath statusline's flag for a composed command stored encoded (see encoded).
COMPOSE_B64 = "--compose-b64"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def python_flags(windows=None):
    """Interpreter flags for every Python command glidepath writes (statusLine,
    runtime.json, launcher). -I ignores PYTHONUTF8 and PYTHONIOENCODING, and on
    Windows a pipe gets the ANSI code page, so Windows adds -X utf8."""
    windows = config.IS_WINDOWS if windows is None else windows
    return ["-I", "-S", "-X", "utf8"] if windows else ["-I", "-S"]


# -- which shell ------------------------------------------------------------------
def getenv(env, name):
    """env[name], ignoring case as Windows does (a plain dict keeps the case it was given)."""
    if name in env:
        return env[name]
    up = name.upper()
    for k, v in env.items():
        if k.upper() == up:
            return v
    return None


def merged(env, settings_env=None):
    """The environment Claude Code runs with: its own, settings.json `env` over it."""
    out = dict(env)
    if isinstance(settings_env, dict):
        for k, v in settings_env.items():
            if isinstance(k, str) and isinstance(v, str):
                for old in [x for x in out if x.upper() == k.upper()]:
                    del out[old]
                out[k] = v
    return out


def which(name, env, is_file=os.path.isfile):
    """`name` with a PATHEXT extension in env's PATH, in order, as where.exe
    finds it. A match in the current directory is skipped, as Claude Code
    skips it. None if there is none."""
    exts = [e for e in (getenv(env, "PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    try:
        here = ntpath.normcase(ntpath.normpath(os.getcwd()))
    except OSError:
        here = None
    for d in (getenv(env, "PATH") or "").split(";"):
        d = d.strip().strip('"')
        if not d or ntpath.normcase(ntpath.normpath(d)) == here:
            continue
        for ext in exts:
            p = ntpath.join(d, name + ext)
            if is_file(p):
                return p
    return None


def git_bash(env, exists=os.path.exists, is_file=os.path.isfile):
    """Claude Code's Git Bash, or None: CLAUDE_CODE_GIT_BASH_PATH when it names
    an existing bash or sh, the two Program Files installs, then the git on
    PATH and ..\\..\\bin\\bash.exe from it."""
    given = getenv(env, "CLAUDE_CODE_GIT_BASH_PATH")
    if given and ntpath.basename(given).lower() in ("bash.exe", "sh.exe", "bash", "sh") and exists(given):
        return given
    for p in GIT_BASH:
        if exists(p):
            return p
    git = which("git", env, is_file)
    if git:
        p = ntpath.normpath(ntpath.join(git, "..", "..", "bin", "bash.exe"))
        if exists(p):
            return p
    return None


def powershell(env, is_file=os.path.isfile):
    """The PowerShell Claude Code falls back to, or None: pwsh on PATH, pwsh in
    its usual installs, then Windows PowerShell 5.1."""
    found = which("pwsh", env, is_file)
    if found:
        return found
    for var, parts in (("ProgramFiles", ("PowerShell", "7")), ("LOCALAPPDATA", ("Microsoft", "WindowsApps")),
                       ("USERPROFILE", (".dotnet", "tools"))):
        root = getenv(env, var)
        if root and is_file(ntpath.join(root, *parts, "pwsh.exe")):
            return ntpath.join(root, *parts, "pwsh.exe")
    found = which("powershell", env, is_file)
    if found:
        return found
    p = ntpath.join(getenv(env, "SYSTEMROOT") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if is_file(p) else None


def predict_shell(env=None, settings_env=None, exists=os.path.exists, is_file=os.path.isfile):
    """("bash", path) when Claude Code would run the statusLine in Git Bash,
    else ("powershell", path), path None when no PowerShell is found either.
    env defaults to os.environ; settings_env is settings.json `env`, which
    Claude Code applies over its own environment."""
    env = merged(os.environ if env is None else env, settings_env)
    bash = git_bash(env, exists, is_file)
    if bash:
        return "bash", bash
    return "powershell", powershell(env, is_file)


def first_word(cmd):
    """The first word as Claude Code reads it for its `.sh` rule: quotes
    dropped, a backslash takes the next character as is."""
    s, out, i = cmd.strip(), [], 0
    while i < len(s):
        c = s[i]
        if c in "\"'":
            j = s.find(c, i + 1)
            if j < 0:
                out.append(s[i + 1:])
                break
            out.append(s[i + 1:j])
            i = j + 1
        elif c == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
        elif c.isspace():
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def process_image(pid):
    """The full path of process `pid`'s executable (Windows), or None."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # its own handle: windll's is shared
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else None
    finally:
        k32.CloseHandle(h)


def parent_shell(image=None, exists=os.path.exists):
    """The shell that started this process: ("bash", path) for a bash.exe or
    sh.exe, ("powershell", path) for pwsh.exe or powershell.exe, else None
    (off Windows, on any error, or another parent such as a
    CLAUDE_CODE_SHELL_PREFIX wrapper). Claude Code ran the statusLine there,
    so the Composer runs the old command there too: inside Git Bash, whose
    PATH puts its own git first, Claude Code's search (predict_shell) can end
    elsewhere. Git Bash's bin\\bash.exe runs usr\\bin\\bash.exe, which is the
    parent seen here; the bin one, which Claude Code runs, is returned when it
    exists. `image` reads a pid's executable path (tests pass their own)."""
    if not config.IS_WINDOWS:
        return None
    try:
        path = (image or process_image)(os.getppid())
        if not path:
            return None
        name = ntpath.basename(path).lower()
        if name in ("pwsh.exe", "powershell.exe"):
            return "powershell", path
        if name not in ("bash.exe", "sh.exe"):
            return None
        folder = ntpath.dirname(path)
        if ntpath.basename(folder).lower() == "bin" and ntpath.basename(ntpath.dirname(folder)).lower() == "usr":
            top = ntpath.join(ntpath.dirname(ntpath.dirname(folder)), "bin", ntpath.basename(path))
            if exists(top):
                return "bash", top
        return "bash", path
    except Exception:
        return None


def shell_argv(cmd, env, shell=None):
    """(argv, env) that run `cmd` the way Claude Code runs a statusLine
    command on Windows: `bash -c` with bash's folder first on PATH (and `bash `
    before a first word ending in .sh), or PowerShell with Claude Code's flags
    and its ${env:...} placeholders. Never cmd.exe. `shell` is the (kind,
    path) to run it with (parent_shell's answer), else predict_shell's."""
    kind, path = shell or predict_shell(env)
    env = dict(env)
    if kind == "bash":
        if path[:1] in ("/", "\\") or path[1:3] in (":/", ":\\"):  # path.win32.isAbsolute, as Claude Code checks
            key = next((k for k in env if k.upper() == "PATH"), "PATH")
            env[key] = ntpath.dirname(path) + (";" + env[key] if env.get(key) else "")
        return [path, "-c", "bash " + cmd if first_word(cmd).endswith(".sh") else cmd], env
    flags = ["-NoProfile", "-NonInteractive"]
    if not getenv(env, "CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY"):
        flags += ["-ExecutionPolicy", "Bypass"]
    for name in PS_PLACEHOLDERS:
        cmd = cmd.replace("${%s}" % name, "${env:%s}" % name)
    return [path or "powershell.exe"] + flags + ["-Command", cmd], env


def kill_tree(pid, env=None):
    """End a process and every process it started (`taskkill /T /F`): Git
    Bash's bash.exe runs the real bash as its child. True if taskkill did."""
    root = getenv(os.environ if env is None else env, "SYSTEMROOT") or r"C:\Windows"
    try:
        r = subprocess.run([ntpath.join(root, "System32", "taskkill.exe"), "/T", "/F", "/PID", str(pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# -- writing a command ------------------------------------------------------------
def slashes(path):
    """Forward slashes: Git Bash drops unquoted backslashes, Windows reads both."""
    return path.replace("\\", "/")


def short_name(path):
    """GetShortPathNameW: the 8.3 form of an existing path, or None."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetShortPathNameW.restype = wintypes.DWORD
    k32.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    n = k32.GetShortPathNameW(path, None, 0)
    if not n:
        return None
    buf = ctypes.create_unicode_buffer(n)
    got = k32.GetShortPathNameW(path, buf, n)
    return buf.value if 0 < got < n else None


def short_path(path, short=None):
    """The path with its folders in their 8.3 short form (C:\\PROGRA~1\\
    PYTHON~1\\python.exe) and its file name as is, so a command keeps naming
    glidepath; or None: off Windows, on any error, or for a folder that does not
    exist. A volume that keeps no short names gives the long folders back.
    `short` maps an existing path to its 8.3 form (tests pass their own)."""
    if not config.IS_WINDOWS:
        return None
    try:
        path = ntpath.normpath(path)
        folder = (short or short_name)(ntpath.dirname(path))
    except Exception:
        return None
    return ntpath.join(folder, ntpath.basename(path)) if folder else None


def _bare_at(s, i):
    c = s[i]
    if c == "~":  # not first, after a letter or digit (PROGRA~1): neither shell expands it there
        return i > 0 and s[i - 1] in ASCII_ALNUM
    return c in BARE_ASCII or (ord(c) > 127 and c.isalnum())


def is_bare(word):
    """True when the word means the same unquoted in bash and in PowerShell:
    ASCII letters, digits and _./:+=-, non-ASCII letters and digits (so
    C:/Users/Jürgen/... needs no quotes), and a ~ after an ASCII letter or
    digit (an 8.3 short name, C:/PROGRA~1/...); never empty."""
    return word != "" and all(_bare_at(word, i) for i in range(len(word)))


def quote(word, shell):
    """The word as `shell` must read it: as is when bare, else single quotes
    (bash closes and reopens them around a ', PowerShell doubles it)."""
    if is_bare(word):
        return word
    if shell == "powershell":
        return "'" + "".join(c * 2 if c in PS_QUOTES else c for c in word) + "'"
    return "'" + word.replace("'", "'\"'\"'") + "'"


def command_line(argv, shell):
    """The command string that runs argv in `shell` ("bash" or "powershell"),
    each word quoted only when it needs it. Bare words come out the same for
    both shells, and so do quoted words without a single quote, except a
    quoted first word: PowerShell runs it only after `& `, which bash rejects."""
    out = [quote(w, shell) for w in argv]
    if shell == "powershell" and argv and (out[0] != argv[0] or argv[0][:1] in ".-"):
        return "& " + " ".join(out)
    return " ".join(out)


def passes_intact(word):
    """True when every PowerShell passes the word to a native program as is.
    Windows PowerShell 5.1 wraps a word with whitespace in double quotes but
    escapes nothing, so a double quote inside, or a backslash ending such a
    word, reaches the program changed (pwsh 7.3+ escapes both)."""
    return '"' not in word and not (re.search(r"\s", word) and word.endswith("\\"))


def msys_converts(arg):
    """True when Git Bash may rewrite `arg` on its way to a native program
    such as python.exe. Its MSYS2 runtime looks only at the text before the
    first whitespace, and only when a / or \\ comes before that whitespace; it
    leaves an argument starting with ~ alone. It rewrites a POSIX path there
    (/bin/bash becomes C:/Program Files/Git/usr/bin/bash), also after a
    leading quote or $(, and what follows a = or a colon (FOO=/x, ab:/c). A
    drive path (C:/x), a relative one (bin/x, ./x) and $HOME/x pass as is."""
    m = re.match(r"[^\s/\\]*[/\\]\S*", arg)
    if not m or arg[:1] == "~":
        return False
    word = m.group(0)
    # MSYS2 skips what cannot start a path: a quote, $, (, ...
    word = word[next((i for i, c in enumerate(word) if c in ASCII_ALNUM or ord(c) > 127 or c in "/\\:.-"),
                     len(word)):]
    if word[:1] == "/":
        return True
    if re.match(r"[A-Za-z]:[/\\]", word):
        return ":" in word[2:] or ";" in word
    return "=" in word or ":" in word


def needs_encoding(arg):
    """True when a shell Claude Code may run the statusLine in can hand `arg`
    to python.exe changed: Windows PowerShell 5.1 (passes_intact) or Git Bash
    (msys_converts). Such a composed command is written encoded."""
    return not passes_intact(arg) or msys_converts(arg)


def encoded(cmd):
    """cmd as one word every shell passes on unchanged: b and its UTF-8 in
    URL-safe base64 without padding, so only letters, digits, _ and - (the b
    keeps it from reading as an option or a number)."""
    import base64

    return "b" + base64.urlsafe_b64encode(cmd.encode("utf-8")).decode("ascii").rstrip("=")


def decoded(word):
    """The command in a word `encoded` wrote, or None when it is not one."""
    import base64

    if not isinstance(word, str) or not re.match(r"b[A-Za-z0-9_-]*\Z", word):
        return None
    try:
        return base64.urlsafe_b64decode(word[1:] + "=" * (-len(word[1:]) % 4)).decode("utf-8") or None
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
        return None


def words(cmd, shell):
    """The words `shell` reads in a command made of bare words and quoted
    ones (what command_line writes), or None when the command is not of that
    form, or would not run as a command there: `&` cannot start a bash
    command, and PowerShell needs `& ` before a quoted first word."""
    s = cmd.strip()
    if shell == "powershell":
        call = s[:1] == "&" and s[1:2].isspace()
        return _ps_words(s[1:] if call else s, call)
    return None if s[:1] == "&" else _bash_words(s)


def _bash_words(s):
    out, cur, i, n = [], None, 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            if cur is not None:
                out.append(cur)
                cur = None
            i += 1
            continue
        cur = cur or ""
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                return None
            cur += s[i + 1:j]
            i = j + 1
        elif c == '"':  # only a plain double-quoted run, as the '"'"' in quote()
            j = s.find('"', i + 1)
            if j < 0 or any(x in s[i + 1:j] for x in "$`\\!"):
                return None
            cur += s[i + 1:j]
            i = j + 1
        elif _bare_at(s, i):
            cur += c
            i += 1
        else:
            return None
    if cur is not None:
        out.append(cur)
    return out or None


def _ps_words(s, call):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c in PS_QUOTES:
            word, j = [], i + 1
            while True:
                if j >= n:
                    return None
                if s[j] in PS_QUOTES:
                    if j + 1 < n and s[j + 1] in PS_QUOTES:
                        word.append(s[j + 1])
                        j += 2
                        continue
                    break
                word.append(s[j])
                j += 1
            if not out and not call:
                return None  # a quoted first word is a string, not a command
            out.append("".join(word))
            i = j + 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] not in PS_QUOTES:
                j += 1
            word = s[i:j]
            if not is_bare(word) or (not out and not call and word[:1] in ".-"):
                return None
            out.append(word)
            i = j
        if i < n and not s[i].isspace():
            return None  # text glued to a quoted word: outside the simple form
    return out or None


def reads_alike(cmd):
    """True when bash and PowerShell read the same words in the command, so
    it runs whichever shell Claude Code picks."""
    b = words(cmd, "bash")
    return b is not None and b == words(cmd, "powershell")


def written_for(cmd):
    """The one shell the command runs in ("bash" or "powershell"), or None:
    it reads the same in both, or is not of the simple form, or reads
    differently in each (a quoted ' in a word)."""
    b, p = words(cmd, "bash"), words(cmd, "powershell")
    if b is not None and p is None:
        return "bash"
    if p is not None and b is None:
        return "powershell"
    return None
