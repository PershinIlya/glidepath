"""The menu bar indicator (glidepath/menubar.py)."""

import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

from glidepath import band, config, install, menubar

from .helpers import MON, H, D, TempHome
from .test_band import NOW, STATES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Lines(TempHome):
    def out(self, state, updated=NOW - 60, cfg=None):
        return menubar.lines(band.windows(*STATES[state], NOW), updated, NOW, cfg or self.cfg)

    def test_a_calm_title_is_two_bars_and_the_numbers(self):
        out = self.out("on pace")
        self.assertRegex(out[0], r"^55% · 48% \| image=[A-Za-z0-9+/=]+,[A-Za-z0-9+/=]+ width=24 height=18$")
        self.assertEqual(out[1], "---")
        # Each window: a line across the menu (its bar, then its name and
        # percent in Menlo, one length for both), then its Details rows.
        self.assertRegex(out[2], r"^5h 55% \| image=\S+ width=199 height=14 font=Menlo size=12 trim=false "
                                 r"tooltip=\"5-hour: 55% used")
        # With color= on a menu line, SwiftBar drew its text and dropped its image.
        self.assertFalse([line for line in out[1:] if "image=" in line and "color=" in line])
        self.assertRegex(out[8], r"^Wk 48% \| image=\S+ width=199 ")
        menlo = " | font=Menlo size=11 trim=false color="
        self.assertEqual(out[3], "Used      55%" + menlo + menubar.DIM)
        self.assertEqual(out[4], "Pace      On pace" + menlo + menubar.DIM)
        # A row's note follows its value after a dot.
        self.assertEqual(out[5], "At reset  about 95% · 5% unused" + menlo + menubar.DIM)
        self.assertIn("Resets    16:30" + menlo + menubar.DIM, out)
        self.assertIn("Rate      17%/h, 18%/h fits" + menlo + menubar.DIM, out)
        self.assertIn("Resets    Fri 04:00" + menlo + menubar.DIM, out)
        self.assertIn("Updated 1 min ago | size=11 color=%s" % menubar.DIM, out)
        self.assertEqual(out[-1], "Refresh | refresh=true")

    def test_a_window_that_needs_you_colors_the_title(self):
        title = self.out("runs out")[0]
        self.assertTrue(title.startswith("70% 2x slower · 48% | image="), title)
        self.assertTrue(title.endswith(" color=" + menubar.TONE["error"]), title)
        menu = self.out("runs out")
        self.assertIn("Pace      2x slower | font=Menlo size=11 trim=false color=" + menubar.TONE["error"], menu)
        self.assertIn("Runs out  15:40, 50 min early · resets 16:30 | font=Menlo size=11 trim=false color="
                      + menubar.TONE["error"], menu)
        self.assertIn("Pace      ~2x faster | font=Menlo size=11 trim=false color=" + menubar.TONE["success"],
                      self.out("early"))
        title = self.out("unused")[0]  # green ("faster"): the color says it, the words are in the menu
        self.assertTrue(title.startswith("12% · 40% | image="), title)
        self.assertTrue(title.endswith(" color=" + menubar.TONE["success"]), title)
        self.assertTrue(self.out("maxed")[0].startswith("100% Back 16:30 · 52% | image="))
        self.assertIn("Limit     reached | font=Menlo size=11 trim=false color=" + menubar.TONE["error"],
                      self.out("maxed"))

    def test_old_readings_dim_the_title_and_say_where_they_come_from(self):
        out = self.out("on pace", updated=NOW - 2 * H)
        self.assertTrue(out[0].endswith(" color=" + menubar.DIM), out[0])
        # Review: an old "faster" is no all-clear: grey, not green; what needs you stays red.
        self.assertTrue(self.out("unused", updated=NOW - 2 * H)[0].endswith(" color=" + menubar.DIM))
        self.assertTrue(self.out("runs out", updated=NOW - 2 * H)[0].endswith(" color=" + menubar.TONE["error"]))
        self.assertIn("Updated 2 h ago | size=11 color=%s" % menubar.DIM, out)
        self.assertTrue(any(line.startswith("Readings come from Claude Code sessions") for line in out))

    def test_waiting(self):
        out = menubar.lines(band.windows({}, {}, NOW), None, NOW, self.cfg)
        self.assertTrue(out[0].startswith(" | image=") and out[0].endswith(" color=" + menubar.DIM), out[0])
        self.assertTrue(out[2].startswith("Waiting for the first reading"))

    def test_the_text_style_is_kept(self):
        cfg = dict(self.cfg, menubar=dict(self.cfg["menubar"], style="text"))
        out = self.out("runs out", cfg=cfg)
        self.assertEqual(out[0], "5h 70% 2x slower · Wk 48% | color=" + menubar.TONE["error"])
        # Review: the text style's title is green with room to spare, as the bars style's.
        self.assertEqual(self.out("unused", cfg=cfg)[0], "5h 12% · Wk 40% | color=" + menubar.TONE["success"])
        self.assertEqual(out[2], "5h ━━━━━━╍╍ 70% 2x slower  resets 16:30 | font=Menlo size=12 "
                                 "trim=false")

    def test_no_pipe_reaches_swiftbar_from_the_words(self):
        # SwiftBar reads " | " as the start of a line's parameters.
        for state in STATES:
            for line in self.out(state):
                self.assertLessEqual(line.count(" | "), 1, line)


class BarWidth(TempHome):
    def test_the_windows_lines_are_as_wide_as_the_widest_line(self):
        self.assertEqual(menubar.bar_width(["Used  5% | font=Menlo size=11"]), menubar.BAR_MIN)
        row = "Runs out        Sun 22:45, 4 days 5 h early | font=Menlo size=11"  # 43 Menlo characters
        self.assertEqual(menubar.bar_width([row]), 289)
        self.assertEqual(menubar.bar_width(["x" * 200 + " | size=11"]), menubar.BAR_MAX)
        out = menubar.lines(band.windows(*STATES["week dry"], NOW), NOW - 60, NOW, self.cfg)
        widths = {line.split(" width=")[1].split()[0] for line in out if " | image=" in line and "font=Menlo" in line}
        # "Runs out  Wed 11:00, 1 day 17 h early · resets Fri 04:00": 56 Menlo
        # characters, 376 points, less 6 Menlo characters and the gap.
        self.assertEqual(widths, {"325"})
        out = menubar.lines(band.windows(*STATES["maxed"], NOW), NOW - 60, NOW, self.cfg)
        lines = [line for line in out if " | image=" in line and "font=Menlo" in line]
        # A longer label, a shorter bar: both lines end at the same point.
        self.assertEqual([(len(line.split(" | ")[0]), line.split(" width=")[1].split()[0]) for line in lines],
                         [(7, "192"), (6, "199")])


class Show(TempHome):
    def test_reads_the_store_and_records_nothing(self):
        r5, r7 = int(MON + 4 * H), int(MON + 5 * D)
        self.known("s1")
        for k in range(6):
            self.feed(MON + H + 300 * k, five=(10 + k, r5), week=(30, r7))
        count = self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.time", return_value=MON + H + 1600):
            self.assertEqual(menubar.show(self.cfg), 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("15% · 30% | image="), lines[0])
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], count)

    def test_no_database_yet_is_waiting(self):
        self.st.close()
        os.remove(os.path.join(self.dir, "tempo.db"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(menubar.show(self.cfg), 0)
        self.assertTrue(out.getvalue().startswith(" | image="))


class Plugin(TempHome):
    def test_install_runs_and_uninstall_removes_it(self):
        folder = os.path.join(self.dir, "plugins")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menubar.install(folder), 0)
        path = os.path.join(folder, menubar.PLUGIN)
        self.assertTrue(os.access(path, os.X_OK) or os.name == "nt")
        self.assertEqual(install.load_state()["menubar"], path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        # SwiftBar's own submenu takes a column on the right of every line; its "Updated" repeats ours.
        self.assertIn("# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>", text)
        self.assertIn("# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>", text)
        if os.name != "nt":  # SwiftBar is macOS; the script is sh
            env = {k: v for k, v in os.environ.items() if k != "GLIDEPATH_HOME"}
            p = subprocess.run(["sh", path], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(" | image=", p.stdout.splitlines()[0])
        with contextlib.redirect_stdout(io.StringIO()):
            menubar.uninstall()
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("menubar", install.load_state())

    def test_never_overwrites_a_plugin_that_is_not_ours(self):
        folder = os.path.join(self.dir, "plugins")
        os.makedirs(folder)
        path = os.path.join(folder, menubar.PLUGIN)
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menubar.install(folder), 1)
            menubar.uninstall(folder)
        with open(path) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho mine\n")

    def test_without_a_folder_it_says_how_to_get_one(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(menubar, "plugin_dir", return_value=None):
            self.assertEqual(menubar.install(None), 1)
        self.assertIn("brew install --cask swiftbar", out.getvalue())

    def test_the_cli_command(self):
        p = subprocess.run([sys.executable, "-I", "-S", os.path.join(ROOT, "bin", "glidepath"), "menubar"],
                           capture_output=True, text=True, env=dict(os.environ, GLIDEPATH_HOME=self.dir), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("\n---\n", p.stdout)


class Notify(TempHome):
    def test_what_is_worth_a_notification(self):
        def titles(state):
            return [t for _, t, _ in menubar.events(band.windows(*STATES[state], NOW))]

        self.assertEqual(titles("runs out"), ["5-hour limit runs out at 15:40"])
        self.assertEqual(titles("maxed"), ["5-hour limit reached, back at 16:30"])
        self.assertEqual(titles("unused"), ["Weekly: 40% will go unused"])
        self.assertEqual(titles("week dry"), ["Weekly limit runs out Wed 11:00"])
        for calm in ("on pace", "early", "idle", "half"):
            self.assertEqual(titles(calm), [], calm)

    def test_once_per_window_and_never_from_old_readings(self):
        ws = band.windows(*STATES["runs out"], NOW)
        sent = []
        send = lambda title, body: sent.append((title, body))  # noqa: E731
        self.assertEqual(menubar.notify(self.st, ws, NOW - 3 * H, NOW, send), [])  # 3 h old: nothing
        menubar.notify(self.st, ws, NOW - 60, NOW, send)
        menubar.notify(self.st, ws, NOW, NOW + 60, send)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].startswith("5-hour: 70% used"), sent[0])

    def test_a_lone_waiting_window_reads_waiting(self):
        out = menubar.lines(band.windows(*STATES["half"], NOW), NOW - 60, NOW, self.cfg)
        self.assertTrue(any(line.startswith("Wk wait | image=") for line in out), out)


class Pictures(TempHome):
    def test_bars_are_png_at_twice_the_point_size(self):
        import base64
        import struct
        import zlib

        from glidepath import menuart

        for state in STATES:
            for w in band.windows(*STATES[state], NOW):
                data = base64.b64decode(menuart.bar(w, 120, 14, dark=False))
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", data[16:24]), (240, 28))
                idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
                self.assertEqual(len(zlib.decompress(idat)), 28 * (1 + 240 * 4))

    def test_the_bar_leaves_room_before_its_label(self):
        from glidepath import menuart
        from glidepath.png import Canvas

        w = band.windows(*STATES["unused"], NOW)[0]  # green dots run to the bar's end
        drawn = {}
        real = Canvas.png
        with mock.patch.object(Canvas, "png", lambda c, dpi=144: drawn.setdefault("c", c) and real(c, dpi)):
            menuart.bar(w, 200, 14, False, menubar.BAR_PAD)
        c = drawn["c"]
        clear = [c.px[y * c.w + x][3] for y in range(c.h) for x in range(c.w - 2 * menubar.BAR_PAD, c.w)]
        self.assertEqual(max(clear), 0.0)  # the last 10 points are empty

    def test_each_kind_draws_its_own_colors(self):
        from glidepath import menuart
        from glidepath.png import Canvas

        def colors(state, i):
            w = band.windows(*STATES[state], NOW)[i]
            c = Canvas(240, 28)
            menuart._draw(c, w, 0.5, 4.5, 118.5, 5, False)
            return {tuple(int(v) for v in p[:3]) for p in c.px if p[3] > 0.95}

        self.assertIn((217, 119, 87), colors("on pace", 0))  # orange: used
        self.assertIn((208, 86, 76), colors("maxed", 0))  # red: reached
        self.assertIn((91, 156, 100), colors("unused", 1))  # green dots
