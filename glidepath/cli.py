"""Argument parsing and dispatch. `statusline` is dispatched before this
module loads (see bin/glidepath) so the hot path skips argparse."""

import argparse
import sys

from . import __version__, config


def build_parser():
    p = argparse.ArgumentParser(prog="glidepath", description="Claude usage pace monitor (5-hour and weekly windows).")
    p.add_argument("--version", action="version", version="glidepath " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("statusline", help="read the statusLine payload on stdin, record it, print the line")
    s.add_argument("rest", nargs=argparse.REMAINDER)

    s = sub.add_parser("now", help="multi-line readout with the reasoning")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("report", help="waste ledger: unused %% at each closed window")
    s.add_argument("--days", type=int, default=28)
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="list every 5-hour window")

    s = sub.add_parser("profile", help="hour-of-week heatmap and where surplus sits")
    s.add_argument("--rebuild", action="store_true", help="re-learn from all stored samples")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("check", help="exit 0 when there is spare quota to harvest now (JSON on stdout)")
    s.add_argument("--min-free", type=float, default=None, help="points of projected surplus required (default from config)")
    s.add_argument("--tail", default=None, help="how close to the reset counts as the tail, e.g. 5h=60m,week=2d")
    s.add_argument("--max-age", default="30m", help="newest fresh reading must be at most this old; heartbeats do not count (default 30m)")

    s = sub.add_parser("bootstrap", help="build the profile prior from ~/.claude/projects transcripts")
    s.add_argument("--days", type=float, default=None)
    s.add_argument("--root", default=None, help="transcripts folder (default ~/.claude/projects)")
    s.add_argument("--dry-run", action="store_true")

    for name, helptext in (("install", "wire the statusLine and the mod (dry run unless --apply)"),
                           ("uninstall", "undo install (dry run unless --apply)")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--apply", action="store_true")
        s.add_argument("--settings", default=None, help="settings.json to edit (default ~/.claude/settings.json)")
        s.add_argument("--no-mod", action="store_true", help="skip the Desktop mod")
        if name == "install":
            s.add_argument("--python", default=None, help="absolute interpreter path to pin")
            s.add_argument("--replace", action="store_true", help="replace an existing statusLine instead of composing")
            s.add_argument("--bin-dir", default=None, help="also write a `glidepath` launcher here, e.g. ~/.local/bin")
            s.add_argument("--no-bootstrap", action="store_true", help="skip building the profile prior from transcripts")

    s = sub.add_parser("doctor", help="check interpreter, database, settings, mod, sample age")
    s.add_argument("--settings", default=None)
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("menubar", help="the band in the macOS menu bar, through SwiftBar (prints its plugin output)")
    s.add_argument("--install", nargs="?", const="", default=None, metavar="FOLDER",
                   help="write the SwiftBar plugin (default: the plugin folder SwiftBar uses)")
    s.add_argument("--uninstall", nargs="?", const="", default=None, metavar="FOLDER", help="remove it")
    s.add_argument("--fetch", type=float, default=None, metavar="MIN", help="the same as `glidepath fetch --every MIN`")

    s = sub.add_parser("fetch", help="ask Claude Code's /usage for the account's levels and record them")
    s.add_argument("--every", type=float, default=None, metavar="MIN",
                   help="from now on, every MIN minutes from the menu bar or a Desktop band (0: never, the default)")

    s = sub.add_parser("config", help="show the effective config")
    s.add_argument("action", nargs="?", default="show", choices=["show", "init", "path"])
    return p


def utf8_output():
    """Windows: a pipe or a file gets the ANSI code page, which has no ▲ → ░
    (`now`, `profile` and `doctor` raised UnicodeEncodeError), and -I ignores
    PYTHONIOENCODING and PYTHONUTF8: such a stream is switched to UTF-8. The
    console is UTF-8 already (PEP 528); macOS and Linux are left alone."""
    if not config.IS_WINDOWS:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    try:  # colors on the Windows 10+ console: turn on its VT processing (best effort)
        import ctypes

        k = ctypes.windll.kernel32
        k.GetStdHandle.restype = ctypes.c_void_p  # a HANDLE, pointer-sized
        h, mode = ctypes.c_void_p(k.GetStdHandle(-11)), ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def main(argv):
    utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:
        args = parser.parse_args(["now"])
    if args.cmd == "statusline":
        from .statusline import main as sl

        return sl(args.rest)
    cfg = config.load()
    if args.cmd in ("install", "uninstall"):
        from . import install

        try:
            return install.install(args) if args.cmd == "install" else install.uninstall(args)
        except config.HomeError as e:
            return refused(e)
    if args.cmd == "fetch":
        from . import usagecmd

        try:
            return usagecmd.main(args)
        except config.HomeError as e:
            return refused(e)
    if args.cmd == "menubar":
        from . import menubar

        try:
            return menubar.main(args, cfg)
        except config.HomeError as e:
            return refused(e)
    from . import commands

    fn = {
        "now": commands.cmd_now,
        "report": commands.cmd_report,
        "profile": commands.cmd_profile,
        "check": commands.cmd_check,
        "bootstrap": commands.cmd_bootstrap,
        "doctor": commands.cmd_doctor,
        "config": commands.cmd_config,
    }[args.cmd]
    try:
        return fn(args, cfg)
    except config.HomeError as e:
        return refused(e)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


def refused(e):
    """A GLIDEPATH_HOME glidepath refuses (config.HomeError): the reason, no traceback."""
    print("glidepath: %s" % e, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
