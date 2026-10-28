# Reading glidepath

What the band, its Details, the charts, the macOS menu bar and the terminal
line say, and how to keep them fresh. [How it works](how-it-works.md) has the
math behind each number.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/band-dark.png">
    <img alt="glidepath's band above the prompt in the Claude Desktop Code tab, Details open: a chart and a few numbers per window" src="images/band-light.png" width="760">
  </picture>
</p>

## The band

One row per window: its name, a small bar, the percent, and its pace.

- **The bar.** Solid orange is what you have used. After a thin gap, a pale
  segment runs to where you will land at the reset; it fades out while the
  estimate is still rough (the first minutes of work, or an early week). A
  red segment into a short red wall means you will run out before the reset.
  In a window's last stretch (its last hour, or the last 2 days of the week),
  green dots fill quota that will go unused, when that is at least 10 points.
  A solid red bar means the limit is reached, a grey fill that nothing is
  moving.
- **The percent.** The last reading, rounded down, as Claude Code's own
  usage bar shows it. When one window needs you, the other one's percent
  turns grey. After a 5-hour window ends, the next one starts only with your
  next message: until then the band reads `5h 0% Not started`.
- **The pace.** Both windows say the same kind of thing: how fast you can
  go and still land on your target at the reset. `1.4x faster` (green: room
  to spare; `~2x` while the estimate is rough, `Plenty of room` from 10x),
  `On pace` (grey), `2x slower` (red; yellow while the estimate is rough or
  the overshoot mild; `Pause` from 10x, `Ease off` when it is close to 1),
  `Back at 16:30` when the limit is reached, `Over your 80% target` (with a
  target below 100), `Idle`. While the week is at its limit, the 5-hour bar
  turns grey: nothing more can be spent there either.
- **Resets** follow when the band is wide. In a narrower window they move to
  Details; with the smallest bars only one window that does not need you
  keeps its words.
- **Details** (the button, or `d` when the band has the focus) opens a card
  per window, side by side (one under the other in a narrow window): its
  name, its percent large and its pace in color on top, its chart, then
  a few rows, the label on the left and the value on the right, a second
  fact after a dot in grey: At reset (where it lands at this pace · what
  will go unused, green when it is worth using), Resets (when · when it
  would reach 100% at this pace, "maybe" while the estimate is early) or,
  when it runs out first, Runs out (when, and how early, in color · when
  it resets), and for the 5 hours Rate (what you spend an hour and what
  fits). How old the reading is shows once it is 5 minutes old. The menu
  bar's menu shows the same rows.
- **The chart** in each card, under its head, shows the window from its
  start to its reset (the week's nights, outside the waking hours of
  `profile.override`, else 8 to 23, as a faint tint and a thin strip under
  the axis): what was used (the line over a pale area, a dot per
  reading while there are few), now (a hairline), the forecast to the reset
  (dashed, in the pace's color when it needs you: red when it runs out or
  the week blocks it, yellow when it may run out or passes your target,
  inside a pale fan: how sure it is, wide early in the window and narrow
  near its reset; when it runs out before the reset, that moment is marked
  in the same color: a dot on the limit, a dashed line down, its time
  beside it, and the rest of the window, blocked, faintly tinted),
  and the forecasts made earlier in the window in grey, an hour ago for the
  5 hours, a day and 2 days ago for the week, so you can see by eye how
  well the forecast holds. The week's forecast follows your usual hours
  (slower at night; a straight line only while the profile is uniform:
  no transcripts and nothing learned yet). The grid marks hours (5 hours) or the days (the week)
  ([D-36](design-decisions.md)).

The terminal status line says the same in text: `━` used, `╍` forecast, `┈`
unused (`#`, `=`, `.` with `display.symbols: "ascii"`), in color unless
`NO_COLOR` is set. When it does not fit `display.max_width`, the resets go
first, then the words get shorter, then the grey words go, then the bars
shrink.

A reading is only as new as the last answer Claude got from the API in some
chat on this machine, so the band can trail Claude Code's own usage bar by a
point for a moment. The Desktop band checks every 5 seconds for a new reading,
from its own chat or another one, and redraws at once ([D-31](design-decisions.md)). Work elsewhere
(claude.ai chats, Cowork, cloud sessions, another computer) reaches it only
through `/usage`, below.

### Usage from everywhere

`glidepath fetch --every 10` lets glidepath ask Claude Code itself: at most every
10 minutes the macOS menu bar, or a Desktop band on any system, runs
`claude -p "/usage"` in the background (about 6 s) and records the account's
levels it prints, as readings from a session of its own. glidepath never sees a
credential: Claude Code makes the request with its own login. `glidepath fetch`
runs it once now; `glidepath fetch --every 0` turns it off (the default);
`glidepath doctor` says when it last answered, or why not. It needs Python 3.9
or later (time zones) and the `claude` command (on Windows in
`%USERPROFILE%\.local\bin` or npm's folder). Claude Code answers `/usage`
with the plan's limits only in a folder it trusts, and glidepath asks it in its
own folder (`~/.glidepath`): once, run `claude` there, choose "Yes, I trust
this folder", and exit (`doctor` says so when this is missing).

## The menu bar (macOS)

The band shows only in Claude Code (the Desktop Code tab, the terminal). To
see the pace anywhere, in a regular chat or another app, glidepath can put it in
the macOS menu bar through [SwiftBar](https://swiftbar.app), a free menu bar
app:

```sh
brew install --cask swiftbar      # or download it from swiftbar.app
open -a SwiftBar                  # once: it asks for a plugin folder
glidepath menubar --install       # writes glidepath.1m.sh into that folder
```

The menu bar then shows two small bars, the 5 hours on top and the week below,
with `22% · 18%` beside them: green when there is room to spare, red or yellow
with the words when a window needs you (`70% 2x slower · 48%`). A click shows
2 sections, one per window: a line across the whole menu with its bar, as
the Desktop band draws it, and its short name and percent, `5h 83%` (a
divider between the sections), its chart, as in the Desktop band's Details,
with its hours or days under it, then the rows of Details (Used, Pace, At
reset, Resets or Runs out, Rate) in columns; then how old the
readings are. `"menubar": {"charts": false}` leaves the charts out. The full sentence is the bar's tooltip. SwiftBar's own items
(its submenu, which takes a column on the right of every line, and its
"Updated") are hidden: Option-click the menu bar item to show them. SwiftBar
reads that when it starts, so after an update that changes the plugin, run
`glidepath menubar --install` and restart SwiftBar once
(`killall SwiftBar; open -a SwiftBar`). `"menubar": {"style": "text"}` brings back plain text. It refreshes every minute and
records nothing itself: the readings come from Claude Code sessions on this
Mac (the status line and the Desktop mod), so while none runs the numbers
grow old, the title turns grey after 30 minutes and the menu says so.
`glidepath menubar --uninstall` (or `glidepath uninstall --apply`) removes it.

To keep the numbers fresh while you work in chats, Cowork or cloud sessions,
turn on [Usage from everywhere](#usage-from-everywhere) (`glidepath fetch --every
10`; `glidepath menubar --fetch 10` does the same). The menu then also says when
`/usage` last answered.

The menu bar also posts a macOS notification, once per window, when a limit
will run out before its reset, when it is reached, or when quota will go
unused at the end of a window ("5-hour limit runs out at 15:40"), and only
from readings under 30 minutes old. `"menubar": {"notify": false}` in
`config.json` turns them off.

## The classic line

`display.style: "classic"` brings back the denser line of earlier versions:

```
5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00
```

| Part | Meaning |
|---|---|
| `5h 62%` | 5-hour window: 62% used |
| `×1.4▲` | multiplier: you can go 1.4 times faster and still land on the target at the reset. `▲` speed up (≥ 1.2), no glyph on pace, `▼` slow down (< 0.9) |
| `~×1.4` | same, but the rate leans on interpolation (fewer than 3 fresh readings in the last 20 minutes) |
| `→88%` | projected level at the reset at the current burn rate |
| `dry 12:40` | instead of the projection: you hit 100% at 12:40, before the reset |
| `idle ≤9%/h` | nothing spent in the last 20 minutes; you can afford up to 9 points an hour |
| `≤24%/h` | no rate yet (a window's first minutes, or the first 5 minutes after work starts); you can afford up to 24 points an hour; the burn is unknown, so this part is never green and `check` says no go |
| `@13:20` | reset time |
| `wk 48%` | weekly window: 48% used |
| `+3` | points versus your on-pace level (plus: ahead of your usual rhythm, minus: surplus) |
| `×1.1` | weekly multiplier (same reading as above; no glyph: on pace) |
| `→95%` | projected weekly level at the reset; 100 minus this will expire |
| `dry~Wed 14:00` | the week runs out on Wednesday around 14:00 |
| `@Thu 09:00` | weekly reset |
| `5h 0%` / `wk 0%` | no open window (the next message starts one) |
| `5h 12% @15:00` (level and reset only) | the database could not be written when a new window opened (locked, read-only or full); this is the payload's level, without pace math, until a write succeeds |

Colors: dim when fine; **yellow** when a window runs dry before its reset but
the multiplier is still on pace (0.9 to 1, no glyph), or when you are at or
above a target set below 100 (idle included); **red** when a window runs dry
before its reset and the verdict is slow down (`▼`), or it is maxed; **green**
when a window is in its last stretch (1 hour for 5h, 2 days for the week) with
at least 10 points about to expire below its target (the reserve that a target
below 100 keeps never counts; never while the 5h burn rate is not known yet).
`NO_COLOR` turns colors off; the ▲/▼ glyphs keep the verdict.

`glidepath now` prints the line with the reasoning behind every number.
