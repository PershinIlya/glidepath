"""Paths and configuration.

Everything lives under GLIDEPATH_HOME (default ~/.glidepath; `~` is expanded
and a relative value is taken from the home directory). The config file is
optional: missing keys fall back to DEFAULTS, unknown keys are kept so a
newer config does not break an older glidepath.
"""

import copy
import json
import math
import os

# Windows rules apply (paths, shells, streams); tests patch it to run them anywhere.
IS_WINDOWS = os.name == "nt"



def compose_timeout_ms(windows=None):
    """Default compose.timeout_ms. On Windows the previous statusLine runs in a
    nested Git Bash or PowerShell, and a cold Windows PowerShell 5.1 start alone
    took about 1 s on CI runners (1.5 s once): 3 s there, 1.5 s elsewhere."""
    return 3000 if (IS_WINDOWS if windows is None else windows) else 1500


FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
WINDOWS = (FIVE_HOUR, SEVEN_DAY)
DURATION = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}
LABEL = {FIVE_HOUR: "5h", SEVEN_DAY: "wk"}

DEFAULTS = {
    # Level each window is paced against, in percent of the limit. 100 means
    # "use it all by the reset"; 90 keeps a 10 point reserve.
    "target_5h": 100.0,
    "target_week": 100.0,
    # 5-hour burn rate: least squares over the trailing window.
    "rate_window_min": 20,
    "rate_min_samples": 3,
    "rate_min_span_min": 5,
    # Multiplier thresholds for the verdict: >= speed_up means you can go
    # faster, < slow_down means you will overshoot the target.
    "verdict": {"speed_up": 1.2, "slow_down": 0.9},
    # A window is "in its tail" this close to its reset (used by colors and
    # by `glidepath check`).
    "tail": {"five_hour_min": 60, "seven_day_hours": 48},
    # Points of projected surplus that count as worth harvesting.
    "min_free": 10.0,
    "profile": {
        "half_life_days": 14.0,
        # Off-hours keep this fraction of the mean awake-hour weight, so the
        # expected pace never drops to zero (ccpool uses the same 0.15).
        "floor": 0.15,
        # Prior strength in observation-equivalents (see docs/design-decisions.md D-12);
        # a flat prior (no transcripts, no override) gets the weaker one.
        "prior_strength": 1.0,
        "prior_strength_uniform": 0.25,
        "bootstrap_days": 45,
        # Gaps between fresh readings longer than this carry little shape
        # information and are learned with low confidence.
        "max_gap_hours": 2.0,
        "active_hours_lookback": 24,
        # Manual override, like ccpool's work days and wake hours. Example:
        # {"work_days": [0,1,2,3,4], "wake_hours": [8, 23],
        #  "off_day_factor": 0.3, "mode": "prior"}  (mode "prior" or "replace")
        "override": None,
    },
    "reset": {
        # A drop counts as a reset only if this many consecutive fresh
        # readings are at least drop_points below the window's max (ccpace).
        "drop_points": 15.0,
        "drop_samples": 2,
    },
    "display": {
        # "bars": a small bar per window and words only when they matter;
        # "classic": the dense line of earlier versions.
        "style": "bars",
        "symbols": "unicode",  # or "ascii"
        "max_width": 80,
        "color": "auto",  # "auto" honors NO_COLOR, "never", "always"
    },
    # Composition with a pre-existing statusLine command (set by install).
    "compose": {
        "command": None,
        "timeout_ms": compose_timeout_ms(),
        "separator": "  ",
    },
    # Surfaces where the mod draws its band, e.g. ["desktop"]. null (the
    # default) picks: the Desktop app, and the terminal too when no status line
    # of ours is installed (band_surfaces).
    "mod": {"surfaces": None, "refresh_s": 60},
    # Every this many minutes the menu bar or a Desktop band asks Claude
    # Code's own /usage for the account's levels, so work in chats, cloud
    # sessions or on another computer shows too (0: never; `glidepath fetch`).
    "fetch_usage_min": 0,
    # The macOS menu bar (glidepath menubar).
    # notify: a macOS notification when a limit will run out or is reached,
    # or quota will go unused (once per window).
    # style: "bars" draws the bars as pictures, "text" as plain text.
    # charts: with "bars", a chart under each window's bar (what was used,
    # the forecast to the reset, forecasts made earlier).
    "menubar": {"notify": True, "style": "bars", "charts": True},
    "retention_days": 365,
    "write_min_interval_s": 60,
    "db_timeout_ms": 250,
}


def private_dir(d):
    """Make the folder `d` (and its parents) as glidepath's own: only the user
    reads a new one (its records hold earlier statusLine commands, session
    ids). An existing folder is left as it is."""
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)


def which(name, path=None):
    """shutil.which without the current folder: Windows searches it first
    (before PATH, whatever PATH says), so a cloned repo's py.bat or
    claude.cmd would run instead. Empty and "." PATH entries go too."""
    import shutil

    entries = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    try:
        here = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        here = None
    # Real paths: on macOS /var is a link to /private/var, and getcwd names the latter.
    keep = [e for e in entries if e.strip().strip('"') not in ("", ".")
            and os.path.normcase(os.path.realpath(e.strip().strip('"'))) != here]
    if IS_WINDOWS:
        os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # Python 3.12+ honors it
    found = shutil.which(name, path=os.pathsep.join(keep))
    if found and IS_WINDOWS and here:
        import ntpath

        if ntpath.normcase(ntpath.dirname(found)) == ntpath.normcase(os.getcwd()):
            return None
    return found


ENV, DIR = "GLIDEPATH_HOME", ".glidepath"
# The earlier names' variables and folders (pacer, then ctempo), newest first:
# read until `glidepath install --apply` moves them (legacy.py).
OLD_ENVS = ("PACER_HOME", "CTEMPO_HOME")
OLD_DIRS = (".pacer", ".claude-tempo")


def _default_home(join, user):
    """The new folder, or an old one while the new one does not exist."""
    new = join(user, DIR)
    if not os.path.exists(new):
        for name in OLD_DIRS:
            old = join(user, name)
            if os.path.isdir(old):
                return old
    return new


def home():
    value = next((v for v in (os.environ.get(k) for k in (ENV,) + OLD_ENVS) if v), None)
    if IS_WINDOWS:
        return windows_home(value)
    if not value:
        return _default_home(os.path.join, os.path.expanduser("~"))
    # Claude Code copies settings.json `env` values as written: expand `~`
    # here, and take a relative path from the home directory, never from the
    # session's cwd (each project would get a database of its own).
    return os.path.join(os.path.expanduser("~"), os.path.expanduser(value))


class HomeError(ValueError):
    """A GLIDEPATH_HOME glidepath refuses (Windows: see windows_home)."""


def windows_home(value):
    """home() by Windows rules (ntpath, so tests run on any OS). `~` is
    USERPROFILE (else HOMEDRIVE and HOMEPATH) as Python reads it, never HOME,
    which Git Bash sets and PowerShell does not. C:\\x, C:/x and
    \\\\server\\share\\x are absolute; Git Bash's /c/Users/x is C:/Users/x; a
    path rooted with a backslash and no drive (\\srv\\x) takes the home's
    drive; anything else is taken from the home directory. The result is
    normalized (backslashes).

    Any other path rooted with a slash and no drive (/srv/x) raises HomeError:
    Git Bash, which runs the statusLine, rewrites such a value to a folder
    under its own install before python.exe sees it, while the mod and
    PowerShell use it as given, so they would use different folders. The
    message names the same path on the home's drive, the folder older versions
    used. C:x (relative to that drive's current folder) raises HomeError too.
    The mod refuses both with the same messages (resolveHome)."""
    import ntpath

    user = ntpath.expanduser("~")
    if not value:
        return _default_home(ntpath.join, user)
    if value[:1] == "/" and value[1:2].isalpha() and value[1:2].isascii() and value[2:3] in ("", "/"):
        value = value[1].upper() + ":/" + value[3:]
    elif value[:1] == "/" and value[1:2] not in ("/", "\\"):
        raise HomeError("GLIDEPATH_HOME %s has no drive; Git Bash rewrites such a path to a folder under its own "
                        "install. Write it with a drive, e.g. %s%s, or as ~/..."
                        % (value, ntpath.splitdrive(user)[0] or "C:", value))
    if value[:1].isalpha() and value[:1].isascii() and value[1:2] == ":" and value[2:3] not in ("/", "\\"):
        raise HomeError("GLIDEPATH_HOME %s is relative to a drive's current folder; use an absolute path" % value)
    return ntpath.normpath(ntpath.join(user, ntpath.expanduser(value)))


def db_path():
    return os.path.join(home(), "tempo.db")


def config_path():
    return os.path.join(home(), "config.json")


def runtime_path():
    return os.path.join(home(), "runtime.json")


def stamp_path():
    """Touched when a reading changes what the band shows; the mod polls its
    time to redraw at once, not at its next refresh."""
    return os.path.join(home(), "changed")


def log_path():
    return os.path.join(home(), "errors.log")


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _compatible(default, value):
    """A user value replaces a default only if it has the same kind, so a
    typo in config.json falls back to the default instead of breaking."""
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return False


def _merge(base, over):
    for k, v in over.items():
        if k not in base:
            base[k] = v  # unknown keys are kept for newer versions
        elif isinstance(base[k], dict):
            if isinstance(v, dict):
                _merge(base[k], v)
        elif _compatible(base[k], v):
            base[k] = v
    return base


# Allowed ranges; a value outside is clamped (a typo must not break the line).
RANGES = {
    ("rate_window_min",): (2, 120),
    ("rate_min_samples",): (2, 100),
    ("rate_min_span_min",): (0.5, 60),
    ("verdict", "speed_up"): (1.0, 10.0),
    ("verdict", "slow_down"): (0.05, 1.0),
    ("tail", "five_hour_min"): (0, 300),
    ("tail", "seven_day_hours"): (0, 168),
    ("min_free",): (0, 100),
    ("profile", "half_life_days"): (0.5, 365),
    ("profile", "floor"): (0, 1),
    ("profile", "prior_strength"): (0, 100),
    ("profile", "prior_strength_uniform"): (0, 100),
    ("profile", "bootstrap_days"): (1, 365),
    ("profile", "max_gap_hours"): (0.1, 48),
    ("profile", "active_hours_lookback"): (1, 168),
    ("reset", "drop_points"): (1, 100),
    ("reset", "drop_samples"): (1, 10),
    ("display", "max_width"): (20, 400),
    ("compose", "timeout_ms"): (0, 10000),
    ("mod", "refresh_s"): (15, 3600),
    ("fetch_usage_min",): (0, 1440),
    ("retention_days",): (7, 3650),
    ("write_min_interval_s",): (0, 3600),
    ("db_timeout_ms",): (10, 5000),
}
CHOICES = {
    ("display", "style"): ("bars", "classic"),
    ("menubar", "style"): ("bars", "text"),
    ("display", "symbols"): ("unicode", "ascii"),
    ("display", "color"): ("auto", "never", "always"),
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _override(ov):
    """The profile override with each field checked on its own (a bad field
    falls back to its default, the rest is kept); None if not a dict."""
    if not isinstance(ov, dict):
        return None
    days = ov.get("work_days")
    days = sorted({int(d) for d in days if _num(d) and int(d) == d and 0 <= d <= 6}) if isinstance(days, list) else []
    wake = ov.get("wake_hours")
    if isinstance(wake, list) and len(wake) == 2 and all(_num(h) for h in wake):
        wake = [min(24, max(0, int(h))) for h in wake]
    else:
        wake = [8, 23]
    off = ov.get("off_day_factor")
    off = min(1.0, max(0.0, float(off))) if _num(off) else 0.5
    mode = str(ov.get("mode", "prior")).lower()
    return {"work_days": days or [0, 1, 2, 3, 4], "wake_hours": wake, "off_day_factor": off,
            "mode": mode if mode in ("prior", "replace") else "prior"}


def normalize(cfg):
    for keys, (lo, hi) in RANGES.items():
        d = cfg
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = min(hi, max(lo, d[keys[-1]]))
    for keys, allowed in CHOICES.items():
        d = cfg
        for k in keys[:-1]:
            d = d[k]
        if d[keys[-1]] not in allowed:
            d[keys[-1]] = DEFAULTS[keys[0]][keys[-1]]
    cmd = cfg["compose"].get("command")
    cfg["compose"]["command"] = cmd if isinstance(cmd, str) and cmd.strip() else None
    if not isinstance(cfg["compose"].get("separator"), str):
        cfg["compose"]["separator"] = "  "
    cfg["profile"]["override"] = _override(cfg["profile"].get("override"))
    given = cfg["mod"]["surfaces"]
    if isinstance(given, list):
        kept = [x for x in given if isinstance(x, str)]
        cfg["mod"]["surfaces"] = kept if (kept or not given) else None  # [] means: draw nowhere
    else:
        cfg["mod"]["surfaces"] = None
    return cfg


def band_surfaces(cfg):
    """Where the mod draws its band: mod.surfaces when set; else the Desktop
    app, and the terminal too when no `glidepath install` is here (no
    runtime.json), so no status line of ours shows there (the plugin alone)."""
    given = (cfg or {}).get("mod", {}).get("surfaces")
    if isinstance(given, list):
        return given
    return ["desktop"] if os.path.exists(runtime_path()) else ["desktop", "terminal"]


def _read(path=None):
    """The config file, parsed. Bytes, so json picks the encoding: UTF-8 with
    or without a BOM (Windows editors add one), or UTF-16/32, which Windows
    PowerShell 5.1's `>` and Out-File write."""
    with open(path or config_path(), "rb") as f:
        return json.loads(f.read())


def unreadable(path=None):
    """Why a config file that exists is not used (not JSON, or not an
    object), else None. A missing file is not a problem."""
    try:
        user = _read(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        return str(e) or type(e).__name__
    return None if isinstance(user, dict) else "not a JSON object"


def load(path=None):
    """Defaults merged with the user's config file, validated. Never raises."""
    try:
        cfg = copy.deepcopy(DEFAULTS)
        try:
            user = _read(path)
            if isinstance(user, dict):
                _merge(cfg, user)
                bar = user.get("menubar")
                old = bar.get("fetch_usage_min") if isinstance(bar, dict) else None
                if "fetch_usage_min" not in user and _compatible(0, old):  # where it was before
                    cfg["fetch_usage_min"] = old
        except (OSError, ValueError):
            pass
        return normalize(cfg)
    except Exception:  # a pathological file (say, nested 200k deep): defaults
        return copy.deepcopy(DEFAULTS)


def target(cfg, window):
    t = cfg["target_5h"] if window == FIVE_HOUR else cfg["target_week"]
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = 100.0
    return min(100.0, max(1.0, t))


def set_value(keys, value, path=None, drop=()):
    """Set one key in config.json, keeping everything else in the file but
    the keys in `drop` (tuples of keys, as `keys`)."""
    path = path or config_path()
    try:
        user = _read(path)
    except FileNotFoundError:
        user = {}
    if not isinstance(user, dict):
        raise ValueError("%s is not a JSON object" % path)
    d = user
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value
    for gone in drop:
        d = user
        for k in gone[:-1]:
            d = d.get(k) if isinstance(d, dict) else None
        if isinstance(d, dict):
            d.pop(gone[-1], None)
    private_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(user, f, indent=2)
        f.write("\n")
    return path


def write_defaults(path=None):
    """Write the default config if none exists. Returns the path."""
    path = path or config_path()
    if not os.path.exists(path):
        private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
            f.write("\n")
    return path
