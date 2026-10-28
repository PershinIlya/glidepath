"""`glidepath statusline`: read the payload, record a sample, print the line.

Hot path. Fail open: any error prints a minimal line and exits 0, the
traceback goes to ~/.glidepath/errors.log (capped), never to stdout.
"""

import json
import os
import re
import sys
import time

from . import config

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")
LOG_MAX = 64 * 1024
FOOTER_PAD = 4  # Claude Code's prompt footer pads the status line by 2 columns a side


def log_error(exc, where="statusline"):
    try:
        import traceback

        from . import clock

        path = config.log_path()
        config.private_dir(os.path.dirname(path))
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            try:
                os.replace(path, path + ".1")
            except OSError:  # Windows: the log is held open without delete sharing; a later error rotates it
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (clock.strftime("%Y-%m-%dT%H:%M:%S", time.time()), where, exc))
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:]))
    except Exception:
        pass


def parse_args(argv):
    opts = {"source": "statusline", "format": "ansi", "record": True, "compose": True, "now": None, "compose_cmd": None}
    it = iter(argv)
    for a in it:
        if a == "--source":
            opts["source"] = next(it, "statusline")
        elif a == "--format":
            opts["format"] = next(it, "ansi")
        elif a == "--no-record":
            opts["record"] = False
        elif a == "--no-compose":
            opts["compose"] = False
        elif a == "--compose":
            opts["compose_cmd"] = next(it, None)
        elif a == "--compose-b64":  # what install writes on Windows for a command a shell would change
            from .winshell import decoded

            opts["compose_cmd"] = decoded(next(it, None))
        elif a == "--now":
            opts["now"] = float(next(it, "0"))
    return opts


def compute(raw, cfg, opts, now, line_of=None):
    """Record the payload and compute both views. Returns (v5, vw, readings).

    `line_of(v5, vw)` renders the plain line so it can be kept for `doctor`
    while the database is open."""
    import sqlite3

    from . import ingest, pace, profile, weekly
    from .store import Store

    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        payload = {}
    readings, rejects, kinds = ingest.parse_payload(payload, now)
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    sid = sid if isinstance(sid, str) and sid else None
    st = Store(timeout_ms=cfg.get("db_timeout_ms", 250))
    try:
        recorded = False
        if opts["record"] and (readings or rejects or kinds):
            try:
                actions = ingest.record(st, readings, now, opts["source"], sid, cfg, rejects, kinds)
                recorded = True
                if any(shown_changed(a) for a in actions.values()):
                    touch_stamp(now)
            except sqlite3.OperationalError as e:  # locked: skip this write, still render
                log_error(e, "record")
        # Once stored, the database has the judged level; the raw reading may
        # be an idle session's stale value. Only an unstored reading is used.
        shown = {} if recorded else readings
        prof = profile.load(st)
        if opts["record"]:
            prof = maybe_learn(st, prof, now, cfg)
        v5 = pace.five_hour(st, now, cfg, shown.get(config.FIVE_HOUR))
        vw = weekly.weekly(st, now, cfg, shown.get(config.SEVEN_DAY), prof)
        from . import chart

        if opts["record"]:
            try:  # a forecast now and then, for the charts' earlier forecasts
                for v in (v5, vw):
                    chart.snapshot(st, v, now)
            except sqlite3.OperationalError as e:
                log_error(e, "snapshot")
        if opts.get("format") == "json":  # the band draws the charts
            chart.attach(st, v5, vw, now, cfg)
        if opts["record"] and line_of is not None:
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            note_render(st, now, opts["source"], line_of(v5, vw), rl)
        if opts["record"] and opts["source"] == "mod" and cfg.get("fetch_usage_min"):
            try:  # a band asks /usage in the background now and then (usagecmd)
                from . import usagecmd

                usagecmd.spawn(st, now, cfg)
            except Exception as e:
                log_error(e, "fetch")
        if opts["record"] and opts["source"] == "mod":
            try:  # a band with no profile prior builds one, once (transcripts)
                from . import transcripts

                transcripts.spawn_bootstrap(st, now)
            except Exception as e:
                log_error(e, "bootstrap")
    finally:
        st.close()
    return v5, vw, readings


def shown_changed(action):
    """Whether what record() did with a reading changes what a band shows: a
    new level, a new window, a reset. Heartbeats, duplicates, rejects and a
    pending anomaly do not, and touching the stamp for them would keep
    bands in other chats waking each other every few seconds."""
    return action in ("fresh", "new_window", "rollover", "rise") or action.startswith("reset_")


def touch_stamp(now):
    try:
        with open(config.stamp_path(), "w", encoding="utf-8") as f:
            f.write("%.3f\n" % now)
    except OSError:
        pass


def note_render(st, now, source, line, rate_limits):
    """Keep the last line and the raw rate_limits per source (numbers and
    window names only) for `glidepath doctor`; at most once a minute unless the
    line changed."""
    import sqlite3

    key = "last_render:" + source
    prev = st.meta_get(key) or {}
    if prev.get("line") == line and now - (prev.get("ts") or 0) < 60:
        return
    keep = None
    if isinstance(rate_limits, dict):
        keep = {
            str(k)[:32]: {f: v for f, v in w.items() if isinstance(v, (int, float, str)) and len(str(v)) < 40}
            for k, w in list(rate_limits.items())[:8]
            if isinstance(w, dict)
        }
    try:
        st.meta_set(key, {"ts": now, "line": line, "rate_limits": keep})
    except sqlite3.OperationalError:
        pass


def maybe_learn(st, prof, now, cfg):
    """Fold newly completed hours into the profile, at most every 10 minutes.

    The profile is re-read inside the write transaction, so a bootstrap or a
    rebuild committed meanwhile is not overwritten. Returns the profile."""
    import sqlite3

    from . import profile

    checked = st.meta_get("profile_checked_at", 0) or 0
    if now - checked < 600:
        return prof
    try:
        st.begin()
        prof = profile.load(st)
        w, _, _ = profile.current_weights(st, prof, now, cfg)
        profile.learn(st, prof, now, cfg, w)
        profile.save(st, prof)
        st.meta_set("profile_checked_at", now)
        st.commit()
    except sqlite3.OperationalError as e:
        st.rollback()
        log_error(e, "learn")
    return prof


def bare_line(readings, now, cfg=None):
    """Fallback without the database: levels and reset times only."""
    from .render import dhm, fit, hm, pct, plain, symbols

    cfg = cfg or config.DEFAULTS
    parts = []
    if config.FIVE_HOUR in readings:
        u, r = readings[config.FIVE_HOUR]
        parts.append("5h %s @%s" % (pct(u), hm(r)))
    if config.SEVEN_DAY in readings:
        u, r = readings[config.SEVEN_DAY]
        parts.append("wk %s @%s" % (pct(u), dhm(r)))
    return plain(fit([(symbols(cfg)["sep"].join(parts) or "glidepath: no data", "dim")], cfg))


class Composer:
    """Runs the user's previous statusLine command in parallel with ours.

    A thread feeds its stdin and reads its stdout so our own work overlaps
    with theirs. As in Claude Code, the output is complete once the command
    has exited and stdout is closed or quiet for QUIET_S: a background job it
    started may keep stdout open, and is left running. The command gets its
    own process group, so one still running at the deadline is killed with
    its children.

    On Windows it runs in the shell that started glidepath, where Claude Code ran
    the statusLine, else in the one Claude Code would pick (Git Bash, else
    PowerShell; never cmd.exe, which shell=True means there), the pipes are
    served by threads (select() takes sockets only), and a command still
    running at the deadline is killed with its tree by taskkill.
    """

    QUIET_S = 0.5  # Claude Code waits 500 ms for the rest of a hook's output

    def __init__(self, cmd, raw, deadline):
        import subprocess
        import threading

        env = dict(os.environ)
        env["GLIDEPATH_COMPOSED"] = "1"
        self.result = None
        self.deadline = deadline
        self.windows = config.IS_WINDOWS
        if self.windows:
            from . import winshell

            argv, env = winshell.shell_argv(cmd, env, winshell.parent_shell())
            # No console window of its own; a group of its own, as on POSIX.
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env, creationflags=flags)
            run = self._run_threads
        else:
            self.p = subprocess.Popen(
                cmd,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            run = self._run
        self.t = threading.Thread(target=run, args=(raw or b"",), daemon=True)
        self.t.start()

    def _run_threads(self, raw):
        # Windows: blocking reads and writes in threads of their own, ended by
        # the same rule as _run (exited, then EOF or QUIET_S without output).
        import threading

        p, out, eof, cond = self.p, [], [], threading.Condition()

        def feed():
            for step in (lambda: p.stdin.write(raw), p.stdin.close):
                try:
                    step()
                except (OSError, ValueError):  # it exited or closed stdin unread
                    pass

        def drain():
            fd = p.stdout.fileno()
            while True:
                try:
                    data = os.read(fd, 32768)
                except OSError:
                    data = b""
                with cond:
                    (out if data else eof).append(data)
                    cond.notify()
                if not data:
                    p.stdout.close()
                    return

        try:
            for target in (feed, drain):
                threading.Thread(target=target, daemon=True).start()
            quiet = None
            with cond:
                seen = 0
                while not eof:
                    now = time.time()
                    if quiet is None and p.poll() is not None:
                        quiet = now + self.QUIET_S
                    elif quiet is not None and len(out) != seen:
                        quiet = now + self.QUIET_S
                    seen = len(out)
                    end = self.deadline if quiet is None else min(quiet, self.deadline)
                    if now >= end:
                        break
                    cond.wait(min(0.05, end - now))
                data = b"".join(out)
            # Exited: its output. Still running at the deadline: TimeoutExpired, result stays None.
            self.result = (p.wait(max(0.0, self.deadline - time.time())), data)
        except Exception:
            pass

    def _run(self, raw):
        # Not communicate(): it waits for EOF, which a background job holding
        # stdout delays past the deadline, and on 3.8 a retry after a timeout
        # stops feeding stdin. Writes go in PIPE_BUF chunks, which never block.
        import select
        import selectors

        p, out, sent, quiet = self.p, [], 0, None
        try:
            with getattr(selectors, "PollSelector", selectors.SelectSelector)() as sel:
                sel.register(p.stdout, selectors.EVENT_READ)
                if raw:
                    sel.register(p.stdin, selectors.EVENT_WRITE)
                else:
                    p.stdin.close()
                while sel.get_map():
                    now = time.time()
                    if quiet is None and p.poll() is not None:
                        quiet = now + self.QUIET_S
                    end = self.deadline if quiet is None else min(quiet, self.deadline)
                    if now >= end:
                        break
                    for key, _ in sel.select(min(0.05, end - now)):
                        if key.fileobj is p.stdout:
                            data = os.read(key.fd, 32768)
                            if not data:
                                sel.unregister(p.stdout)
                            out.append(data)
                            if quiet is not None:
                                quiet = time.time() + self.QUIET_S
                        else:
                            try:
                                sent += os.write(key.fd, raw[sent:sent + getattr(select, "PIPE_BUF", 512)])
                            except OSError:  # it exited or closed stdin unread
                                sent = len(raw)
                            if sent >= len(raw):
                                sel.unregister(p.stdin)
                                p.stdin.close()
            # Exited: its output. Still running at the deadline: TimeoutExpired, result stays None.
            self.result = (p.wait(max(0.0, self.deadline - time.time())), b"".join(out))
        except Exception:
            pass

    def finish(self):
        self.t.join(max(0.0, self.deadline - time.time()) + 0.1)
        if self.result is None:
            # Kill only a command still running (or a stuck reader): a background
            # job left by one that exited is not ours to stop.
            if self.t.is_alive() or self.p.poll() is None:
                self._kill()
            return None
        rc, out = self.result
        if rc != 0:
            return None
        # A Windows command ends its lines with CRLF; a \r left before our
        # segment would send the cursor back over the user's text.
        return "\n".join(line.rstrip("\r") for line in out.decode("utf-8", "replace").split("\n")).rstrip("\n")

    def _kill(self):
        if self.windows:
            from . import winshell

            if winshell.kill_tree(self.p.pid):
                return
        else:
            import signal

            try:
                os.killpg(self.p.pid, signal.SIGKILL)
                return
            except (OSError, AttributeError):
                pass
        try:
            self.p.kill()
        except OSError:
            pass


def visible_len(s):
    """Display columns, as Claude Code measures them when it cuts the line:
    wide characters (CJK, most emoji) take 2, combining marks and joiners 0,
    and VS16 turns a narrow symbol into a 2-column emoji."""
    import unicodedata

    n = prev = 0
    for ch in ANSI_RE.sub("", s):
        if ch == "\ufe0f":
            n += 1 if prev == 1 else 0
            prev = 2
        elif unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        else:
            prev = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            n += prev
    return n


def join_composed(theirs, ours, cfg, columns):
    if not theirs:
        return ours
    lines = theirs.split("\n")
    sep = cfg["compose"].get("separator", "  ")
    # Claude Code cuts the line at its footer's width, not at COLUMNS.
    if visible_len(lines[-1]) + visible_len(sep) + visible_len(ours) <= columns - FOOTER_PAD:
        lines[-1] = lines[-1] + sep + ours
    else:
        lines.append(ours)
    return "\n".join(lines)


def main(argv):
    t0 = time.time()
    opts = {"format": "ansi"}
    color = False
    out = ""
    composer = None
    cfg = None
    try:
        opts = parse_args(argv)
        raw = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
        cfg = config.load()
        from . import render

        color = render.use_color(cfg) and opts["format"] == "ansi"
        cmd = opts["compose_cmd"] or cfg["compose"].get("command")
        if (
            cmd
            and opts["compose"]
            and opts["source"] == "statusline"
            and opts["format"] == "ansi"
            and not os.environ.get("GLIDEPATH_COMPOSED")
        ):
            try:
                composer = Composer(cmd, raw, t0 + float(cfg["compose"].get("timeout_ms", 1500)) / 1000.0)
            except Exception as e:
                log_error(e, "compose")
        now = opts["now"] or time.time()
        readings = {}
        wins = updated = None
        try:
            from . import band

            v5, vw, readings = compute(raw, cfg, opts, now, lambda a, b: render.plain(band.line(a, b, cfg, now)))
            segs = band.line(v5, vw, cfg, now)
            if opts["format"] == "json":
                wins = band.windows(v5, vw, now)
                updated = band.updated(v5, vw)
        except Exception as e:  # database trouble: degrade to the bare line
            log_error(e)
            if not readings:
                from . import ingest

                try:
                    readings = ingest.parse_payload(json.loads(raw or b"{}"), now)[0]
                except Exception:
                    readings = {}
            segs = [(bare_line(readings, now, cfg), "dim")]
            v5 = vw = {}
        if opts["format"] == "json":
            out = json.dumps(
                {
                    "ok": True,
                    "line": render.plain(segs),
                    "segments": [{"text": t, "tone": tone} for t, tone in segs],
                    "surfaces": config.band_surfaces(cfg),
                    "refresh_s": cfg["mod"].get("refresh_s", 60),
                    "five_hour": _brief(v5),
                    "seven_day": _brief(vw),
                    # The band: one entry per window (band.window), and when a reading last changed.
                    "windows": wins,
                    "updated_ts": updated,
                    "now": now,
                },
                separators=(",", ":"),
            )
        else:
            out = render.to_ansi(segs, color)
    except Exception as e:
        log_error(e)
        if opts.get("format") == "json":
            mod = (cfg or config.DEFAULTS)["mod"]
            out = json.dumps(
                {"ok": False, "line": "glidepath: error", "segments": [{"text": "glidepath: error", "tone": "dim"}],
                 "surfaces": config.band_surfaces(cfg or config.DEFAULTS), "refresh_s": mod.get("refresh_s", 60)},
                separators=(",", ":"),
            )
        else:
            out = "glidepath: error"
    if composer is not None:
        # Outside the main try: whatever failed above, the user's own line still shows.
        try:
            theirs = composer.finish()
            out = join_composed(theirs, out, cfg, columns_env())
        except Exception as e:
            log_error(e, "compose")
    write_out(out)
    return 0


def columns_env():
    try:
        n = int(float((os.environ.get("COLUMNS") or "").strip() or 0))
    except (ValueError, OverflowError):
        n = 0
    return n if n > 0 else 200


def write_out(out):
    """UTF-8 bytes whatever the locale: Claude Code reads stdout as UTF-8."""
    try:
        data = (out + "\n").encode("utf-8", "replace")
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except Exception as e:
        log_error(e, "write")


def _brief(v):
    keys = ("present", "used", "reset_ts", "verdict", "severity", "multiplier", "projected", "delta")
    return {k: v.get(k) for k in keys if k in v} if v else {}
