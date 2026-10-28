"""Readings from Claude Code's own `/usage` command.

The status line and the mod see the rate limits only when a Claude Code
session on this machine talks to the API. `claude -p "/usage"` asks for the
account's current levels whenever it runs, so work in chats, Cowork, cloud
sessions or on another computer shows too. glidepath never sees a credential:
Claude Code makes the request. Off by default (fetch_usage_min 0); the menu
bar runs it at most every that many minutes, and so does a Desktop band, in
the background (`spawn`). It prints

  Current session: 11% used · resets Oct 17 at 6:20pm (Asia/Dubai)
  Current week (all models): 20% used · resets Oct 23 at 6am (Asia/Dubai)

which becomes a statusLine-shaped payload from session "usage-cmd".
"""

import datetime
import os
import re
import subprocess
import time

from . import config

SESSION_ID = "usage-cmd"
SOURCE = "usage"
TIMEOUT_S = 45
LINE = re.compile(r"^\s*Current (session|week \(all models\)):\s*([0-9]+(?:\.[0-9]+)?)% used"
                  r"(?:\s+\S+\s+resets\s+(\S[^()]*?)\s*\(([^()]+)\))?\s*$")
LONGEST = 300  # longer lines are not /usage's: never fed to the pattern
# What /usage prints instead of the plan's limits (fetch).
COST = re.compile(r"^\s*Total cost:\s*\$", re.M)
# "Oct 17 at 6:20pm" (2.1.302), "Oct 19, 2:59pm" (2.1.303), or the time alone.
WHEN = re.compile(r"^(?:([A-Z][a-z]{2})\s+(\d{1,2})(?:,|\s+at)\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)$", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WINDOW = {"session": config.FIVE_HOUR, "week (all models)": config.SEVEN_DAY}


def reset_epoch(text, tz_name, now):
    """Epoch seconds of "Oct 17 at 6:20pm" (or "6:20pm") in the IANA zone,
    the next such time after now (within 8 days); None if it does not read."""
    m = WHEN.match(text.strip())
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:  # Python 3.8 has no zoneinfo; a wrong zone would move the window by hours
        return None
    mon, day, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    minute = int(minute or 0)
    here = datetime.datetime.fromtimestamp(now, tz)
    for add in range(0, 9):
        d = here.date() + datetime.timedelta(days=add)
        if mon and (d.month != MONTHS.index(mon.lower()) + 1 or d.day != int(day)):
            continue
        at = datetime.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).timestamp()
        if at > now - 60:
            return at
    return None


def parse(text, now):
    """{window: (used, resets_at)} from `/usage` output."""
    out = {}
    for line in text.splitlines():
        m = LINE.match(line) if len(line) <= LONGEST else None
        if not m or not m.group(3):
            continue
        reset = reset_epoch(m.group(3), m.group(4), now)
        if reset is not None:
            out[WINDOW[m.group(1)]] = (float(m.group(2)), int(round(reset)))
    return out


# What marks a process as running inside a Claude Code session: a nested
# `claude` refuses to start, or takes it for its own.
SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "GLIDEPATH_HOME")


def claude_bin():
    """claude on PATH, or where its installers put it (SwiftBar runs plugins with a bare PATH)."""
    extra = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    if os.environ.get("APPDATA"):  # npm's global folder on Windows
        extra.append(os.path.join(os.environ["APPDATA"], "npm"))
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra)
    return config.which("claude", path=path)


def fetch(now=None, run=None):
    """Run `/usage` and record what it says. Returns (ok, message)."""
    now = time.time() if now is None else now
    exe = claude_bin()
    if not exe and run is None:
        return False, "claude not found"
    try:
        if run is None:
            env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
            # Claude Code writes UTF-8 to a pipe; Windows' default (cp1252) read its "·" as "Â·".
            p = subprocess.run([exe, "-p", "/usage"], capture_output=True, encoding="utf-8", errors="replace",
                               timeout=TIMEOUT_S, cwd=config.home(), env=env)
            text, rc = p.stdout, p.returncode
        else:
            text, rc = run()
    except (OSError, subprocess.SubprocessError) as e:
        return False, "claude -p /usage failed: %s" % e
    readings = parse(text, now)
    if rc == 0 and not readings and COST.search(text or ""):
        # /usage showed the session's spend. Seen on a Windows machine,
        # signed in with Max: claude -p asks for the plan's limits only in a folder
        # it trusts, and this runs in GLIDEPATH_HOME. Or an API-key login.
        return False, ("claude -p /usage shows spend, not plan limits: Claude Code shows them only in a folder "
                       "it trusts. Run claude once in %s and choose \"Yes, I trust this folder\" (or sign in "
                       "with your Claude subscription: /login)" % config.home())
    lines = [line.strip() for line in (text or "").splitlines() if line.strip().startswith("Current ")]
    if rc == 0 and not readings and lines:
        return False, "could not read the reset time in /usage: %s" % lines[0][:120]  # a new format
    if rc != 0 or not readings:
        last = (text or "").strip().splitlines()[-1:]  # Claude Code's own error, if any
        return False, "could not read the usage from claude -p /usage" + (": " + last[0][:120] if last else "")
    payload_rl = {w: {"used_percentage": u, "resets_at": r} for w, (u, r) in readings.items()}
    record(payload_rl, now)
    return True, ", ".join("%s %g%%" % (config.LABEL[w], u) for w, (u, _) in sorted(readings.items()))


def record(rate_limits, now):
    from . import ingest
    from .statusline import shown_changed, touch_stamp
    from .store import Store

    cfg = config.load()
    readings, rejects, kinds = ingest.parse_payload({"rate_limits": rate_limits}, now)
    st = Store(timeout_ms=2000)
    try:
        actions = ingest.record(st, readings, now, SOURCE, SESSION_ID, cfg, rejects, kinds)
        st.meta_set("usage_cmd_ok_at", now)
    finally:
        st.close()
    if any(shown_changed(a) for a in actions.values()):
        touch_stamp(now)


def every(cfg):
    return cfg.get("fetch_usage_min") or 0


def set_every(minutes):
    """Turn the fetch on (every so many minutes) or off (0). Returns the config path."""
    return config.set_value(("fetch_usage_min",), minutes, drop=[("menubar", "fetch_usage_min")])


def say_every(minutes, path):
    print("Every %g min the menu bar and the Desktop band ask Claude Code's /usage for your levels (%s)."
          % (minutes, path) if minutes > 0 else "glidepath no longer runs /usage (%s)." % path)


def main(args):
    """`glidepath fetch [--every MIN]`."""
    if args.every is not None:
        say_every(args.every, set_every(args.every))
        return 0
    ok, msg = run_now()
    print(("Recorded " if ok else "glidepath fetch: ") + msg)
    return 0 if ok else 1


def run_now(now=None):
    """`glidepath fetch`: one fetch, its outcome kept for doctor. Returns (ok, message)."""
    from .store import Store

    now = time.time() if now is None else now
    ok, msg = fetch(now)
    try:
        st = Store(timeout_ms=2000)
        try:
            st.meta_set("usage_cmd_note", None if ok else msg)
        finally:
            st.close()
    except Exception:
        pass
    return ok, msg


def spawn(st, now, cfg, runtime=None, popen=None):
    """A Desktop band's run: start `glidepath fetch` in the background when one is
    due, so the band never waits for it; its readings wake the bands when
    they land (the stamp). Returns whether it started one."""
    if not due(st, now, every(cfg)):
        return False
    if runtime is None:
        from .menubar import runtime as installed

        runtime = installed()
    python, args, entry = runtime
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
              cwd=config.home(), env=dict(os.environ, GLIDEPATH_HOME=config.home()))
    if config.IS_WINDOWS:
        # No console window; out of the band's process group, and of its job when allowed.
        flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        tries = [dict(kw, creationflags=flags | 0x01000000), dict(kw, creationflags=flags)]  # | BREAKAWAY_FROM_JOB
    else:
        tries = [dict(kw, start_new_session=True)]
    popen = popen or subprocess.Popen
    for opts in tries:
        try:
            popen([python] + list(args) + [entry, "fetch"], **opts)
            return True
        except OSError:
            continue
    return False


def due(st, now, every_min):
    """Whether a fetch is due, and marks the attempt so overlapping runs do not pile up."""
    if not every_min or every_min <= 0:
        return False
    last = st.meta_get("usage_cmd_tried_at", 0) or 0
    if now - last < every_min * 60:
        return False
    st.meta_set("usage_cmd_tried_at", now)
    return True
