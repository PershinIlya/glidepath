# glidepath for Claude Code

**Know if your Claude usage will last until the reset.** glidepath is a small
local monitor for Claude's two usage limits, the rolling 5-hour window and
the weekly one. For each it answers three questions, right where you work:
how fast am I spending, how fast can I afford to, and where will I land at
the reset.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/band-demo-dark.gif">
    <img alt="glidepath's band above the prompt in the Claude Desktop Code tab: the pace changes from 2.1x faster to on pace to 1.9x slower, then Details opens with a chart and a few numbers per window" src="docs/images/band-demo-light.gif" width="760">
  </picture>
</p>

- **In Claude Code.** A band above the prompt in the Claude Desktop Code tab,
  or a line in the terminal's status line:
  `5h ━━━━╍╍╍╍ 55%  resets 16:30 · Week ━━━━╍╍╍╍ 48%  resets Fri`.
- **Its pace in plain words.** `1.4x faster` when you have room, `On pace`,
  `2x slower` when you will run out early, `Back at 16:30` when you are out.
- **Details.** A chart per window: what you used, the forecast to the reset
  with how sure it is, earlier forecasts to see how well it guesses, the
  week's nights, and when you would run out.
- **Anywhere on a Mac.** The same in the macOS menu bar, with a notification
  when a limit will run out.
- **Learns your week.** The weekly forecast follows your own hours, so a
  quiet night does not read as "plenty of room" and a busy morning does not
  read as "slow down".

It needs no account access: it reads the rate limits Claude Code already
hands to its status line, and keeps a small history on your machine. Python
3.8+ standard library only; macOS, Linux and Windows.

> glidepath is an independent project, not affiliated with or endorsed by
> Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Requirements

- **A Claude Pro or Max plan**, used through Claude Code. Claude Code passes
  the plan's rate limits to the status line and to mods; with an API key
  there are none to show.
- **A recent Claude Code**, in a terminal or the Desktop app.
- **Python 3.8 or newer** (3.9+ for the optional `/usage` fetch). macOS has
  one with the Command Line Tools (`xcode-select --install`); on Windows,
  install it from python.org or with `winget install Python.Python.3.12`.
- **Optional:** [SwiftBar](https://swiftbar.app) for the macOS menu bar.

## Install

**The band, in one line.** In Claude Code in a terminal:

```
/plugin install glidepath --marketplace PershinIlya/glidepath
```

Answer `y` to add the marketplace and pick the user scope. In the Claude
Desktop app, where `/plugin` does not run: click **+** next to the prompt box,
then **Plugins**, **Add marketplace** with `PershinIlya/glidepath`, then **Add
plugin**.

That is all for the band: it shows above the prompt in the Desktop Code tab
and in the terminal from your next message on, and on its first run learns a
first picture of your week from the timestamps of your past Claude Code
sessions, in the background. It runs the Python code that comes with it, with
the first Python 3.8+ it finds; when it finds none, the band says so.

**The status line and the menu bar.** For glidepath in the terminal's own
status line, and for the macOS menu bar, install it from a clone too. macOS
and Linux:

```sh
git clone https://github.com/PershinIlya/glidepath.git ~/Projects/glidepath
cd ~/Projects/glidepath
python3 bin/glidepath install                                  # a dry run: shows what it would change
python3 bin/glidepath install --apply --bin-dir ~/.local/bin
```

Windows, in PowerShell (one command a line):

```powershell
git clone https://github.com/PershinIlya/glidepath.git $HOME\Projects\glidepath
cd $HOME\Projects\glidepath
py -3 bin\glidepath install                                    # a dry run
py -3 bin\glidepath install --apply --bin-dir $HOME\.local\bin
```

`install --apply` backs up `~/.claude/settings.json`, sets its `statusLine`
(your existing status line keeps running, with glidepath's part added),
installs the band from the clone (so the line above is not needed then), and
pins the Python it found for both. The status line works at once; restart
Claude Code (or run `/reload-plugins`) for the band. Then run `glidepath
doctor` to check everything. [docs/install.md](docs/install.md) has every step
and what each one changes.

**Mac menu bar.** With SwiftBar installed and opened once:

```sh
glidepath menubar --install
```

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/menubar-dark.png">
    <img alt="glidepath's menu in the macOS menu bar: each window's bar, chart and numbers" src="docs/images/menubar-light.png" width="330">
  </picture>
</p>

**Usage from claude.ai, Cowork or another computer.** glidepath sees a new level
whenever Claude Code makes a request on this machine. To count everything
else too, let it ask Claude Code's own `/usage` every 10 minutes:
`glidepath fetch --every 10` (once, run `claude` in `~/.glidepath` and choose "Yes,
I trust this folder": Claude Code shows plan limits only in a trusted folder).

## Uninstall

The plugin alone: `/plugin uninstall glidepath`, then `/plugin marketplace
remove glidepath`. An install from a clone:

```sh
glidepath uninstall            # a dry run
glidepath uninstall --apply
```

It puts your previous status line back (byte for byte when you did not edit
the file since), removes the band and the menu bar plugin. Either way your
history stays in `~/.glidepath` (delete it if you want it gone).

## Privacy and safety

- **glidepath never reads your credentials** (no OAuth token, no Keychain, no
  `~/.claude/.credentials.json`) and makes no network requests of its own.
  It uses only the `rate_limits` Claude Code gives the status line and the
  mod, and, if you turn it on, what Claude Code's `/usage` prints: Claude
  Code makes that request with its own login.
- **No message content is kept.** From your session transcripts it reads
  only each assistant message's time and token counts, to learn your week's
  rhythm; the text is never stored, printed or logged (a test plants a
  secret and checks the database for it).
- **Everything stays in `~/.glidepath`:** a SQLite history (levels, reset times,
  session ids), the config, and logs, readable only by you.
- **Nothing is added to Claude's context**, and nothing changes in
  `settings.json` except the `statusLine`, with a backup first.

## How it works

The 5-hour pace is the slope of your recent usage. The weekly pace walks an
hour-of-week profile of when you usually work, scaled by how you spend this
week and pulled toward your typical week while the week is young. Both carry
an uncertainty that is wide far from the reset and narrows toward it, and a
warning shows only when it holds at the optimistic edge.
[docs/how-it-works.md](docs/how-it-works.md) has the formulas;
[docs/design-decisions.md](docs/design-decisions.md) has the reason for every
rule, often with the case that shaped it.

## How it compares

Many tools show Claude's limits; this is how glidepath differs from some of
the better-known ones, as their READMEs and source read in October 2026.
Corrections are welcome in an issue.

| | **glidepath** | [claude-pace] | [Claude-Usage-Tracker] | [ClaudeBar] | [Pacer] | [claumon] | [usage-quota] |
|---|---|---|---|---|---|---|---|
| Shows in | Status line, Claude Desktop band, macOS menu bar | Status line | macOS menu bar, status line | macOS menu bar | macOS menu bar, widgets | Web dashboard, VS Code | Claude Code band |
| OS | macOS, Linux, Windows | macOS, Linux | macOS | macOS | macOS | macOS, Linux, Windows | Where Claude Code runs |
| Uses your Claude login token | No | No | Yes | In API mode | Yes | Yes | No (Claude Code calls for it) |
| Network requests of its own | None | None | Yes | Yes | Yes | Yes | Usage endpoint, about every 2 min |
| Weekly forecast | Learned hour-of-week profile | None (pace vs. an even line) | Used ÷ time elapsed | Window average | Learned weekday × hour model | Constant rate, Bayesian | Linear, prior from past windows |
| Says how sure it is | Yes, narrowing toward the reset | No | No | No | Yes | Yes (80% interval) | No |
| Per-model weekly limits | No | No | Yes | Yes | Yes | Yes | No |
| Several accounts or providers | No | No | Yes (Codex too) | Yes (many providers) | Accounts | No | No |
| Install | Plugin (`git clone` for the status line) | Plugin, npx or curl | Homebrew, DMG | Homebrew, DMG | Homebrew, DMG | Homebrew, binary | Plugin |
| Runs on | Python 3.8+ standard library | bash, jq | Native app | Native app | Native app | Single binary | Claude Code |
| License | MIT | MIT | MIT | Apache-2.0 | MIT | MIT | MIT |

glidepath's niche is the forecast without your login: it reads only what
Claude Code hands its status line and mods, so it cannot see the per-model
weekly limits, other accounts or other providers. If you need those, the
menu bar apps above cover them. Claude Code's own `/usage` shows the levels
and reset times, without a forecast.

[claude-pace]: https://github.com/Astro-Han/claude-pace
[Claude-Usage-Tracker]: https://github.com/hamed-elfayome/Claude-Usage-Tracker
[ClaudeBar]: https://github.com/tddworks/ClaudeBar
[Pacer]: https://github.com/EricAndrechek/Pacer
[claumon]: https://github.com/fabioconcina/claumon
[usage-quota]: https://github.com/anantraghunath/claude-code-usage-quota-mod

## More

- [Reading glidepath](docs/guide.md): the band, Details, the charts, the menu
  bar, the terminal line.
- [Install in detail](docs/install.md): Windows, what changes on disk,
  upgrading, uninstalling.
- [Reference](docs/reference.md): commands, configuration, data, performance.
- **Something wrong?** `glidepath doctor` checks the interpreter, the database,
  the settings wiring and the mod, and says what to do. Errors go to
  `~/.glidepath/errors.log`.

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
Security issues: please report them privately ([SECURITY.md](SECURITY.md)).

## Credits

glidepath reimplements ideas from these projects (read, not run or copied):

- [SeanLF/ccpool](https://github.com/SeanLF/ccpool): least-squares burn on a
  running-max series, the `resets_at` jitter bucket, the epoch-leak guard,
  the off-hour pace floor, the dim-until-it-matters status line, the
  dry-run and backup install.
- [thevibeworks/claude-code-statusline](https://github.com/thevibeworks/claude-code-statusline):
  the decay-weighted hour-of-week profile, the hour-by-hour walk behind
  "dry Thu 09:00" and "N% will expire", and the 5-hour-to-weekly exchange
  rate.
- [thevibeworks/ccpace](https://github.com/thevibeworks/ccpace): the
  sustained-drop rule for resets.
- [fabioconcina/claumon](https://github.com/fabioconcina/claumon): a
  Bayesian forecast with an interval.
- [Astro-Han/claude-pace](https://github.com/Astro-Han/claude-pace): the
  network-free baseline.
- [NathanAB/statusline-anywhere](https://github.com/NathanAB/statusline-anywhere):
  how a mod runs a status-line command and draws it above the prompt.
- [anthropics/claude-code-playground](https://github.com/anthropics/claude-code-playground):
  the sample mods' conventions.

## License

[MIT](LICENSE) © 2026 Ilia Pershin
