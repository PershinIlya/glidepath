"""`glidepath menubar`: the band in the macOS menu bar, through SwiftBar.

SwiftBar (a free menu bar app) runs a plugin script every minute and shows
what it prints: the first line in the menu bar, the lines after `---` in the
menu that opens on a click. The script `glidepath menubar --install` writes
runs `glidepath menubar`, which reads the store and prints

  [two small bars] 22% · 18%             (5 hours on top; green with room to spare,
  ---                                       red or yellow words when a window needs you)
  [bar ━━━━━━━━━━━━━━━━━━]  5h 22%        (each window's bar as a picture, as the
  [chart ____....----~~~~~~]                Desktop band draws it, and its name, a
   12      13      14      15                line across the menu dividing the 2
  Pace            1.4x faster               sections; its chart, as in Details, and
  ...                                       its hours or days; then its Details rows)
  ...
  Updated 2 min ago

It records nothing: the numbers are the ones Claude Code sessions on this Mac
(the status line, the Desktop mod) last stored, and "Updated" says how old.
"""

import math
import os
import shlex
import subprocess
import sys
import time

from . import band, config, render

PLUGIN = "glidepath.1m.sh"  # SwiftBar reads the refresh interval from the name
MARK = "# glidepath menu bar plugin"
STALE_S = 30 * 60
TONE = {  # SwiftBar takes "light,dark"
    "error": "#c2453b,#ec6a5e",
    "warning": "#a86b12,#e0b252",
    "success": "#3f7d47,#7cbf83",
}
DIM = "#8a867c,#a3a19a"
RANK = ("success", "warning", "error")


def lines(ws, updated, now, cfg, checked=None, note=None):
    """SwiftBar's output for the windows: the title, `---`, the menu.
    `checked`: when /usage last answered (usagecmd); `note`: why it did not.
    menubar.style "bars" (the default) draws the bars as pictures; "text"
    is the plain text of earlier versions."""
    if cfg.get("menubar", {}).get("style", "bars") == "text":
        return text_lines(ws, updated, now, cfg, checked, note)
    from . import menuart

    waiting = all(w["pct"] is None for w in ws)
    newest = max([t for t in (updated, checked) if t is not None] or [0])
    stale = not newest or now - newest > STALE_S
    parts = []
    for w in ws:
        if w["pct"] is None:
            parts.append("-")
            continue
        part = "%d%%" % w["pct"]
        p = w.get("phrase")
        if p and p["attention"]:  # the words that need you; green ("1.4x faster") says it by color
            part += " " + p["narrow_pre"] + p["num"] + p["narrow"]
        parts.append(part)
    color = title_color(ws, stale or waiting)
    pic = "image=%s,%s width=24 height=18" % (menuart.icon(ws, False), menuart.icon(ws, True))
    title = ("" if waiting else " · ".join(parts)) + " | " + pic + (" color=" + color if color else "")
    foot = []
    times = []
    if updated is not None:
        times.append(age(now - updated))
    if checked is not None:
        times.append("checked " + age(now - checked)[8:])
    if times:
        foot.append("%s | size=11 color=%s" % (", ".join(times), DIM))
    if note:
        foot.append("%s | size=11 color=%s" % (clean(note), TONE["warning"]))
    if stale and not waiting and checked is None:
        foot.append("Readings come from Claude Code sessions on this Mac | size=11 color=%s" % DIM)
    out = [title, "---"]
    if waiting:
        out.append("Waiting for the first reading: start a Claude Code session | size=12")
    sections = [(w, detail_lines(w)) for w in ([] if waiting else ws)]
    charts = cfg.get("menubar", {}).get("charts", True)
    # Each window's line, its bar then its short name and percent, spans the
    # menu, a divider between the 2 sections: the bar takes what the widest
    # line leaves after its label (Menlo, so its width is known and both
    # lines end at the same point).
    width = bar_width([line for _, rows in sections for line in rows] + foot)
    for w, rows in sections:
        bar = max(BAR_LEAST, width - int(math.ceil(len(head(w)) * HEAD_PT + IMAGE_GAP_PT)))
        img = "image=%s,%s width=%d height=14" % (menuart.bar(w, bar, 14, False, BAR_PAD),
                                                 menuart.bar(w, bar, 14, True, BAR_PAD), bar)
        # No color= on this line: with it, SwiftBar drew the text and dropped
        # the image (seen on a Mac); the menu's own grey is kept.
        out.append("%s | %s font=Menlo size=12 trim=false tooltip=%s" % (head(w), img, quoted(w["sentence"])))
        if charts and w.get("chart"):
            out.extend(chart_lines(w, width))
        out.extend(rows)
    out.append("---")
    out.extend(foot)
    out.append("Refresh | refresh=true")
    return out


# Points per character: Menlo 11 (0.602 em, with a little to spare), the
# system font at 11 (wider than its average) and Menlo 12 (the windows'
# lines); the space between an image and its text in a menu.
MENLO_PT, TEXT_PT, HEAD_PT, IMAGE_GAP_PT = 6.7, 6.2, 7.23, 7  # the gap as measured on a Mac
BAR_MIN, BAR_MAX, BAR_LEAST = 250, 380, 120
BAR_PAD = 2  # clear points at the bar's right end: with the image's gap, 9 before the label
CHART_H = 40  # the chart's height in points; its labels in Menlo 10 (0.602 em a character)
LABEL_SIZE, LABEL_PT = 10, 6.02


def head(w):
    """A window's short name and percent, as the title has them: "5h 83%",
    "Wk 46%"."""
    return ("5h" if w["id"] == "h5" else "Wk") + " " + ("wait" if w["pct"] is None else "%d%%" % w["pct"])


def bar_width(lines):
    """The width in points the windows' lines span, from the menu's widest
    other line: Menlo rows and small text. Too wide only widens the menu to
    these lines; too narrow would leave the rows longer, so it errs wide."""
    widest = BAR_MIN
    for line in lines:
        text, _, params = line.partition(" | ")
        widest = max(widest, len(text) * (MENLO_PT if "font=Menlo" in params else TEXT_PT))
    return int(min(BAR_MAX, math.ceil(widest)))


def clean(text):
    """Text from data (a /usage error, an exception) as one SwiftBar line: no
    " | " to start parameters (a bash= one runs on a click), no line breaks
    to start lines."""
    return " ".join(str(text).replace("|", "/").split())


def quoted(text):
    """A parameter value with spaces, as SwiftBar reads it: in double quotes
    (unquoted, it ends at the first space). Its versions end a quoted value
    at either quote and keep a backslash, so none is left inside."""
    text = clean(text).replace("\\", "/").replace('"', "\u201d").replace("'", "\u2019")
    return '"%s"' % text


def tone(w):
    """The pace's tone, as the card says it: the band's words, else the Pace row."""
    if w.get("phrase"):
        return w["phrase"]["tone"]
    return next((r["tone"] for r in w.get("details") or [] if r["label"] == "Pace"), None)


def chart_lines(w, width):
    """A window's chart, as wide as its bar's line (no color=, which drops a
    picture), and its hours or days under it, each under its grid line."""
    from . import menuart

    c = w["chart"]
    ago = "an hour ago" if w["id"] == "h5" else "a day and 2 days ago"
    tip = "What was used (the line), the forecast to the reset (dashed) and the forecasts made %s (grey)" % ago
    out = [" | image=%s,%s width=%d height=%d tooltip=%s" % (
        menuart.chart(c, width, CHART_H, False, tone(w)), menuart.chart(c, width, CHART_H, True, tone(w)),
        width, CHART_H, quoted(tip))]
    text = menuart.labels(c, width, LABEL_PT)
    if text.strip():
        out.append("%s | font=Menlo size=%d trim=false color=%s" % (text, LABEL_SIZE, DIM))
    return out


LABEL_W = len("At reset") + 2


def detail_lines(w):
    """A window's rows under its bar, the rows of the Desktop band's Details
    (band.details): label and value in columns (Menlo), each row in its
    tone (a row's note after a dot, in its color: one color a line); plain
    numbers dim, so the pace and what needs you stand out."""
    rows = w.get("details") or []
    if not rows:
        return ["%s | size=11 color=%s" % (clean(w["sentence"]), DIM)]
    width = max([LABEL_W] + [len(r["label"]) + 2 for r in rows])  # one column for both windows
    return ["%s%s | font=Menlo size=11 trim=false color=%s" % (
        r["label"].ljust(width), clean(r["value"] + (" · " + r["note"] if r.get("note") else "")),
        TONE.get(r["tone"], DIM)) for r in rows]


def title_color(ws, dim):
    """The title's color: red or yellow when a window needs you; grey when
    the readings are old (an old "faster" is no all-clear); green when a
    window has room to spare; else the menu bar's own."""
    tones = [w["phrase"]["tone"] for w in ws if w.get("pct") is not None and w.get("phrase")
             and (w["phrase"]["attention"] or w["phrase"]["tone"] == "success")]
    worst = max(tones, key=RANK.index) if tones else None
    if worst in ("warning", "error"):
        return TONE[worst]
    if dim:
        return DIM
    return TONE[worst] if worst else None


def text_lines(ws, updated, now, cfg, checked=None, note=None):
    """The plain text menu (menubar.style "text")."""
    title_parts = []
    for w in ws:
        short = "5h" if w["id"] == "h5" else "Wk"
        if w["pct"] is None:
            title_parts.append(short + " -")
            continue
        part = "%s %d%%" % (short, w["pct"])
        p = w.get("phrase")
        if p and p["attention"]:
            part += " " + p["narrow_pre"] + p["num"] + p["narrow"]
        title_parts.append(part)
    waiting = all(w["pct"] is None for w in ws)
    newest = max([t for t in (updated, checked) if t is not None] or [0])
    stale = not newest or now - newest > STALE_S
    if waiting:
        title = "glidepath | color=%s" % DIM
    else:
        title = " · ".join(title_parts)
        color = title_color(ws, stale)
        if color:
            title += " | color=%s" % color
    out = [title, "---"]
    if waiting:
        out.append("Waiting for the first reading: start a Claude Code session | size=12")
    else:
        for w in ws:
            text = render.plain(band.segments([w], cfg, 60)) if w["pct"] is not None else w["name"] + " waiting"
            out.append("%s | font=Menlo size=12 trim=false" % text)
            out.append("%s | size=11 color=%s" % (w["sentence"], DIM))
    out.append("---")
    if updated is not None:
        out.append("%s | size=11 color=%s" % (age(now - updated), DIM))
    if checked is not None:
        out.append("Checked with /usage %s | size=11 color=%s" % (age(now - checked)[8:], DIM))
    if note:
        out.append("%s | size=11 color=%s" % (clean(note), TONE["warning"]))
    if stale and not waiting and checked is None:
        out.append("Readings come from Claude Code sessions on this Mac | size=11 color=%s" % DIM)
    out.append("Refresh | refresh=true")
    return out


def age(s):
    m = int(max(0, s) // 60)
    if m < 1:
        return "Updated just now"
    if m < 60:
        return "Updated %d min ago" % m
    h = int(round(m / 60.0))
    return "Updated %d h ago" % h if h < 48 else "Updated %d days ago" % int(round(h / 24.0))


def show(cfg):
    from .commands import _open, views

    from . import usagecmd

    now = time.time()
    note = None
    try:
        every = usagecmd.every(cfg)
        st = _open(create=bool(every))
        if st is not None and usagecmd.due(st, now, every):
            st.close()
            ok, msg = usagecmd.run_now(now)
            note = None if ok else "/usage: " + msg
            st = _open()
        checked = None
        if st is None:
            ws, updated = band.windows({}, {}, now), None
        else:
            try:
                v5, vw, _ = views(st, cfg, now)
                bar = cfg.get("menubar", {})
                if bar.get("style", "bars") == "bars" and bar.get("charts", True):
                    from . import chart

                    chart.attach(st, v5, vw, now, cfg)
                checked = st.meta_get("usage_cmd_ok_at") if every else None
                ws, updated = band.windows(v5, vw, now), band.updated(v5, vw)
                if cfg.get("menubar", {}).get("notify", True):
                    notify(st, ws, max([t for t in (updated, checked) if t] or [0]), now)
            finally:
                st.close()
        out = lines(ws, updated, now, cfg, checked, note)
    except Exception as e:  # the menu bar shows why instead of nothing
        out = ["glidepath ! | color=%s" % TONE["warning"], "---", "glidepath: %s | size=12" % clean(e)[:200],
               "Refresh | refresh=true"]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


# -- notifications ----------------------------------------------------------------------

def events(ws):
    """[(key, title, body)]: what is worth a notification, once per window
    and kind: a limit that runs out before its reset (not the milder "may"),
    one that is reached, and quota that will go unused."""
    out = []
    when = lambda t: ("at " + t) if " " not in t else t  # noqa: E731  ("at 16:30", "Fri 04:00")
    for w in ws:
        if w.get("pct") is None or w.get("reset_ts") is None:
            continue
        name = "5-hour" if w["id"] == "h5" else "Weekly"
        if w["kind"] == "maxed":
            title = "%s limit reached, back %s" % (name, when((w.get("phrase") or {}).get("num") or w["reset"]))
        elif w["kind"] == "dry" and not w.get("soft") and w.get("runs_out"):
            title = "%s limit runs out %s" % (name, when(w["runs_out"]))
        elif w["kind"] == "expire" and w.get("unused"):
            title = "%s: %d%% will go unused" % (name, w["unused"])
        else:
            continue
        out.append(("notified:%s:%s:%s" % (w["id"], w["reset_ts"], w["kind"]), title, w["sentence"]))
    return out


def notify(st, ws, newest, now, send=None):
    """Post each new event as a macOS notification, once. Old readings post
    nothing: a stale alarm is worse than none."""
    if not newest or now - newest > STALE_S:
        return []
    sent = []
    for key, title, body in events(ws):
        if st.meta_get(key):
            continue
        try:
            st.meta_set(key, now)
        except Exception:
            continue
        (send or post)(title, body)
        sent.append(title)
    return sent


def post(title, body):
    if sys.platform != "darwin":
        return
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title \"glidepath\" "
              "subtitle (item 1 of argv)", "-e", "end run"]
    try:
        subprocess.Popen(["osascript"] + script + [title, body], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


# -- the SwiftBar plugin ---------------------------------------------------------------

def plugin_dir():
    """SwiftBar's plugin folder, as it saved it on first launch, or None."""
    try:
        p = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    path = p.stdout.strip()
    return os.path.expanduser(path) if p.returncode == 0 and path else None


def script(python, args, entry, home):
    q = shlex.quote
    return "\n".join([
        "#!/bin/sh",
        MARK + " (written by `glidepath menubar --install`; `glidepath menubar --uninstall` removes it)",
        "# <swiftbar.title>glidepath</swiftbar.title>",
        "# <swiftbar.desc>Claude usage pace: the 5-hour and weekly windows</swiftbar.desc>",
        "# <swiftbar.hideAbout>true</swiftbar.hideAbout>",
        "# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>",
        "# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>",
        # The menu says when it was updated; SwiftBar's own submenu takes a column on
        # the right of every line. Option-click shows both.
        "# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>",
        "# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>",
        "export GLIDEPATH_HOME=%s" % q(home),
        "exec %s menubar" % " ".join(q(a) for a in [python] + list(args) + [entry]),
    ]) + "\n"


def runtime():
    """(python, args, entry): the install's pinned interpreter, else this one."""
    import json

    try:
        with open(config.runtime_path(), encoding="utf-8-sig") as f:
            rt = json.load(f)
        if isinstance(rt.get("python"), str) and isinstance(rt.get("entry"), str):
            args = rt.get("args") if isinstance(rt.get("args"), list) else ["-I", "-S"]
            return rt["python"], args, rt["entry"]
    except (OSError, ValueError):
        pass
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "glidepath")
    return sys.executable, ["-I", "-S"], entry


def ours(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return MARK in f.read(4096)
    except OSError:
        return False


def install(folder):
    folder = folder or plugin_dir()
    if not folder:
        print("SwiftBar's plugin folder is not set. Install SwiftBar (brew install --cask swiftbar, or "
              "https://swiftbar.app), open it once and choose a plugin folder, then run this again; "
              "or name the folder: glidepath menubar --install ~/SwiftBarPlugins")
        return 1
    folder = os.path.abspath(os.path.expanduser(folder))
    path = os.path.join(folder, PLUGIN)
    if os.path.exists(path) and not ours(path):
        print("%s exists and is not glidepath's; not touched" % path)
        return 1
    python, args, entry = runtime()
    os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script(python, args, entry, config.home()))
    os.chmod(path, 0o755)
    from . import install as inst

    state = inst.load_state()
    state["menubar"] = path
    inst.save_state(state)
    print("Wrote %s: SwiftBar shows it within a minute. SwiftBar reads a plugin's settings (which of its own "
          "items to hide) only when it starts: after an update, restart it once: killall SwiftBar; open -a SwiftBar"
          % path)
    return 0


def uninstall(folder=None):
    from . import install as inst

    state = inst.load_state()
    path = os.path.join(os.path.abspath(os.path.expanduser(folder)), PLUGIN) if folder else state.get("menubar")
    removed = remove(path)
    state.pop("menubar", None)
    inst.save_state(state)
    print("Removed %s" % path if removed else "No glidepath menu bar plugin to remove")
    return 0


def remove(path):
    if path and os.path.exists(path) and ours(path):
        os.remove(path)
        return True
    return False


def main(args, cfg):
    if args.fetch is not None:
        from . import usagecmd

        usagecmd.say_every(args.fetch, usagecmd.set_every(args.fetch))
        if args.install is None and args.uninstall is None:
            return 0
    if args.install is not None:
        return install(args.install or None)
    if args.uninstall is not None:
        return uninstall(args.uninstall or None)
    return show(cfg)
