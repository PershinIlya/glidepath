# Contributing to glidepath

Thanks for taking a look. glidepath is small and opinionated; issues, ideas and
pull requests are all welcome.

## Before you start

- **Bugs:** open an issue with what you saw and what you expected, your OS,
  `claude --version` and `glidepath doctor`'s output. Doctor prints your
  `statusLine` command: remove anything secret from it first.
- **Bigger changes:** open an issue first, so we can agree on the shape
  before you spend the time.
- **Security issues:** never in a public issue; see [SECURITY.md](SECURITY.md).

## How the code is laid out

- `glidepath/`: the CLI, Python 3.8+ standard library only (no third-party
  packages, ever: the status line runs as `python -I -S`). The math is in
  `pace.py` (5 hours), `weekly.py` and `profile.py` (the week); what the
  band, the line and the menu bar say is in `band.py` and `render.py`.
- The repository is also the Claude Code plugin: `.claude-plugin/` (the
  manifest and the marketplace), `hooks/register.tsx` (the mod, TypeScript),
  `types/` (its state). The mod only runs the CLI and draws what it prints;
  all the words and numbers come from the CLI. [docs/mod.md](docs/mod.md)
  has how it finds Python and the CLI.
- `tests/`: unittest, hermetic, and the mod's `register.test.ts` (every test gets its own temporary
  `GLIDEPATH_HOME` and `CLAUDE_CONFIG_DIR`; nothing touches your real `~/.claude`).
- [docs/design-decisions.md](docs/design-decisions.md): why each rule is the
  way it is. A change to a rule gets its entry there (a new `D-` number, or
  an addition to the one it changes), and code comments point to it.

## Conventions

- Plain, short English in user-facing text, numerals for numbers.
- Every bug fix comes with a test that fails without the fix.
- The version lives in `glidepath/__init__.py` and `.claude-plugin/plugin.json`:
  bump both together, and add a line to [CHANGELOG.md](CHANGELOG.md).

## Tests

```sh
python3 -m unittest discover -s tests -t .     # 562 tests, Python 3.8 to 3.13
claude plugin validate .
claude plugin test .                           # 79 tests
python3 scripts/ci_integration.py              # end to end, throwaway folders
```

(On Windows, `py -3` for `python3`, and in cmd.exe drop the `#` comments.)

The suite feeds synthetic time series (steady burn, bursts, idle, a reset
mid-series, two parallel sessions, missing windows, bogus `resets_at`, the
epoch leak, a week with nights and weekends, DST) and checks the math against
values computed by hand in the test comments. Install and uninstall run on
throwaway settings files, including the byte-identical round trip (CRLF, mode,
several settings files, a crash mid-install, a lost install record). A
model-based fuzzer runs 400 seeded random sequences of installs and uninstalls
(some with `--no-mod`) on two settings files, with claude working, missing or
hanging, hand edits (the command inside glidepath's `--compose` included), the
plugin commands run by hand and the data folder deleted in between. It checks
after every step that `runtime.json` exists while a mod record does, and at
the end that nothing of glidepath is left, each file holds the user's JSON, and a
file the user never edited comes back byte for byte.

Windows rules run on every OS with the one platform flag (`config.IS_WINDOWS`)
patched; the 12 tests that need Windows itself (cmd.exe, Git Bash, PowerShell,
taskkill, 8.3 names, the read-only attribute, the C runtime's `TZ`, long paths)
skip elsewhere. CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu,
macOS and Windows, the mod's tests on all 3, and `scripts/ci_integration.py` on
Windows and macOS: install into a throwaway settings.json, a payload piped into
the command it wrote through each shell Claude Code may use (Git Bash, pwsh and
powershell.exe; `/bin/sh` on macOS), composed commands in plain and encoded
form, and uninstall back to the same bytes; a read-only settings.json; and on
Windows the launchers from cmd.exe, PowerShell and Git Bash, a drive-less
`GLIDEPATH_HOME`, and a venv's `python.exe` pinned as its base interpreter.
`.gitattributes` keeps `bin/glidepath`, `*.py` and `*.sh` LF on a Windows
checkout, where a CR breaks the `#!` line in Git Bash.

## Pull requests

Keep them focused, run the tests above, and describe what changes for a
user. CI runs everything on Ubuntu, macOS and Windows.
