# Changelog

## 0.2.0 (2026-10)

pacer is now **glidepath**. Four other tools in the same niche were called
pacer or nearly, two of them with a `pacer` command.

- `git pull`, then `glidepath install --apply` (Windows: `py -3 bin\glidepath
  install --apply`) moves an install made as pacer or ctempo: the data
  folder to `~/.glidepath`, the mod to `glidepath@glidepath`, SwiftBar's
  plugin to `glidepath.1m.sh`, the status line to `bin/glidepath`. Until
  then `bin/pacer`, `PACER_HOME` and `~/.pacer` keep working.
- The repository moves to https://github.com/PershinIlya/glidepath (the old
  addresses redirect).
- The band installs in one line, `/plugin install glidepath --marketplace
  PershinIlya/glidepath`, with no clone: the repository is the plugin, and
  the band runs the Python code that comes with it, builds the weekly
  profile in the background on its first runs, and draws in the terminal too
  when no status line of ours is installed. The status line and the menu bar
  still come from `glidepath install`.
- The README compares glidepath with similar tools.

## 0.1.0 (2026-10)

First public release, as pacer.

- The 5-hour and weekly pace in the Claude Code status line, a band above
  the prompt in the Claude Desktop Code tab, and the macOS menu bar
  (SwiftBar), on macOS, Linux and Windows.
- Details: a chart per window (what was used, the forecast with its
  uncertainty, earlier forecasts, the week's nights, when it runs out) and a
  few numbers.
- An hour-of-week profile learned from your sessions, a weekly forecast
  that follows it, and forecasts that warn only when the warning holds.
- Optional `/usage` fetch, so usage from claude.ai, Cowork or another
  computer counts too; macOS notifications.
- `pacer install` / `uninstall` with a dry run, backups and a byte-for-byte
  restore; `pacer doctor`.

Before this release pacer was called ctempo. `pacer install --apply` moves
an install made under that name (its data folder, mod and menu bar plugin).
