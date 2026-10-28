# Reference

Commands, configuration, what glidepath keeps, and how fast it is. Tests are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Commands

| Command | What it does |
|---|---|
| `glidepath statusline` | reads the status-line JSON on stdin, records a sample, prints the line (`--format json` for the mod) |
| `glidepath now` | the line plus the reasoning: rates, allowed rates, on-pace level, intensity, projections, exchange rate, and the age of the last reading (heartbeats shown apart) |
| `glidepath report [--days N]` | waste ledger: % unused at each closed weekly and 5-hour window, totals, in 5h-window equivalents |
| `glidepath profile [--rebuild]` | hour-of-week heatmap, the prior's age and weight now, lightest awake blocks, points vs pace by day before reset |
| `glidepath check [--min-free X] [--tail 5h=60m,week=2d] [--max-age 30m]` | for a future scavenger: exit 0 and JSON when there is quota to harvest now, else exit 1; data older than `--max-age` is a no-go, and heartbeats do not count as data; so is a 5h window whose burn rate is not known yet (its leftover is unknown); spare quota is measured against each window's target (`spare` in the JSON), so a window at or above a target below 100, or one that should slow down, is never harvestable; `--min-free` is clamped to 0 to 100 like `min_free`, and a leftover counts only when it is at least that and more than 0 |
| `glidepath bootstrap [--days N] [--dry-run]` | rebuild the profile prior from transcripts |
| `glidepath install` / `uninstall` | see above; dry run unless `--apply` |
| `glidepath doctor` | interpreter, database, settings wiring, mod, last samples, last line and the raw rate limits it came from, rate-limit kinds seen; on Windows, the shell Claude Code runs the status line in and whether the command suits it |
| `glidepath fetch [--every MIN]` | ask Claude Code's `/usage` for the account's levels now, or from now on every MIN minutes (0: never) |
| `glidepath menubar [--install|--uninstall]` | the band in the macOS menu bar (SwiftBar) |
| `glidepath config [show|init|path]` | effective configuration |

`glidepath` with no command is `glidepath now`.

## Configuration

`~/.glidepath/config.json` (written with defaults by the first install, and
kept as it is after that: a key you leave out takes the default of the version
you run, one in the file keeps its value, so delete a key to get a newer
default):

| Key | Default | Meaning |
|---|---|---|
| `target_5h`, `target_week` | 100 | level to pace against; 90 keeps a 10-point reserve, which green and `check` never count as spare |
| `rate_window_min` | 20 | 5h least-squares window, minutes |
| `verdict.speed_up`, `verdict.slow_down` | 1.2, 0.9 | multiplier thresholds |
| `tail.five_hour_min`, `tail.seven_day_hours` | 60, 48 | "last stretch" of each window |
| `min_free` | 10 | points about to expire below the target that count as worth using (green, `check`); 0 to 100, and 0 still needs more than 0 points |
| `profile.half_life_days` | 14 | how fast the profile forgets |
| `profile.floor` | 0.15 | off-hour weight relative to a typical awake hour |
| `profile.override` | null | `{"work_days": [0,1,2,3,4], "wake_hours": [8, 23], "off_day_factor": 0.3, "mode": "prior"}`; days are 0 = Monday; mode `replace` ignores learning |
| `reset.drop_points`, `reset.drop_samples` | 15, 2 | a sustained drop that counts as a reset |
| `display.style` | `bars` | the terminal line: `bars`, or `classic` for the dense line of earlier versions |
| `display.symbols` | `unicode` | `ascii` for `# = .` bars (and `x ^ v ->` in the classic line) |
| `display.max_width` | 80 | the line drops optional parts to fit, then is cut with `…` (`...` in ascii mode); each part keeps its color |
| `mod.surfaces` | `["desktop"]` | where the mod draws its band (add `terminal` if you do not use the status line) |
| `mod.refresh_s` | 60 | how often the mod refreshes the band when no reading moved (15 to 3600) |
| `compose.timeout_ms` | 1500 (3000 on Windows) | how long your previous status line may take; once it exits, glidepath reads its output for up to 500 ms more, and only a command still running at the timeout is stopped; on Windows, 5000 for a slow PowerShell or Git Bash script |
| `fetch_usage_min` | 0 | every this many minutes the menu bar or a Desktop band runs `claude -p "/usage"` for the account's levels; 0 never (`glidepath fetch --every N`) |
| `menubar.notify`, `menubar.style`, `menubar.charts` | true, `bars`, true | macOS notifications; `text` for the plain text menu; a chart under each bar |
| `retention_days` | 365 | history kept |

`GLIDEPATH_HOME` moves everything (default `~/.glidepath`). The Desktop app
does not inherit your shell's environment, so if you move it, set it in
`~/.claude/settings.json` under `env` too. Claude Code passes that value as
written, so glidepath expands a leading `~` itself and takes a relative path from
your home directory, never from the session's folder. The mod does not expand
`~otheruser/...`: use an absolute path for that. On Windows the home directory
is `%USERPROFILE%`, and some forms are refused: see Windows notes. Values of
the wrong type or out of range fall back to the default or are clamped; a
`config.json` that is not valid JSON is ignored as a whole (`glidepath config
show` says so).

## Data and privacy

- `~/.glidepath/tempo.db` (SQLite, WAL): `samples` (time, window, used %,
  resets_at, source, session id), `windows` (one row per window: start, reset,
  peak, % unused at close), the profile, counters. Kept 365 days.
- The status-line payload carries no message content; only the rate limits and
  the session id are stored.
- `bootstrap` reads transcript lines to get each assistant message's timestamp,
  id and token counts. Message content is parsed with the line and discarded; it
  is never stored, printed or logged (a test checks the database for a planted
  secret).
- No hooks inject anything into Claude's context.
- Errors from the status line go to `~/.glidepath/errors.log` (64 KB, one
  rotation), never to the line.

## Performance

`python3 scripts/bench_statusline.py` seeds 45 days of history (38,000 rows)
and times 50 renders the way Claude Code runs them (fresh process, payload on
stdin, a new sample written each time). In the build container: p50 35 ms,
p95 47 ms on Python 3.13; p50 34 ms, p95 44 ms on Python 3.9. CI runs it on
GitHub's runners with Python 3.12: macOS p50 24 ms, p95 27 ms; Windows p50 51
ms, p95 55 ms (with Python 3.8 to 3.11 on Windows, about 30 ms more: D-05).
The budget is 100 ms. It also prints where the time goes: the interpreter's
start, the imports, and each phase of a render.

## Layout

```
bin/glidepath              launcher (python -I -S safe); bin/pacer and
                           bin/ctempo run it for installs made under the
                           earlier names
glidepath/                 the core: store, ingest, pace, weekly, profile, band
                           (what the band and the line say), chart (the
                           charts' data), menubar, menuart and png (the
                           menu bar's pictures), render, statusline, commands,
                           install, legacy (the move from pacer and ctempo),
                           usagecmd (/usage), transcripts, clock (local time),
                           winshell (Windows shells and commands)
mods/glidepath/            the Desktop mod (TypeScript, thin)
.claude-plugin/            local marketplace: glidepath
tests/                     unittest suite
scripts/bench_statusline.py
scripts/ci_integration.py  end to end through each shell (CI on Windows, macOS)
.github/                   CI on 3 OSes, issue and pull request templates
docs/                      the guide, install, how it works, reference,
                           design decisions, images
```
