# Installing glidepath

[The README](../README.md) has the short version. This page has every step,
what glidepath changes on your machine, Windows in detail, upgrading and
uninstalling.

## Install (macOS and Linux)

```sh
git clone https://github.com/PershinIlya/glidepath.git ~/Projects/glidepath && cd ~/Projects/glidepath
python3 bin/glidepath install                          # dry run: shows the diff, changes nothing
python3 bin/glidepath install --apply --bin-dir ~/.local/bin
```

`install --apply`:

1. picks and verifies an absolute interpreter (`/usr/bin/python3` first, then
   Homebrew, then the one you ran it with; `--python /abs/path` to choose;
   Windows: see below) and pins it in the command, run as `python -I -S` so
   conda or PYTHONPATH cannot interfere;
2. backs up `~/.claude/settings.json` to
   `settings.json.glidepath-backup-<UTC time>` and sets only `statusLine`
   (`refreshInterval: 60`). If you already have a status line, glidepath
   **composes**: your command still runs, with the same input, and glidepath's
   segment is appended to its last line, or put on a line of its own when the
   joined line would not fit Claude Code's footer (`--replace` to replace it
   instead); a background job your command starts keeps running, and a command
   still running after `compose.timeout_ms` is stopped;
3. installs the Desktop mod from the local marketplace in this repo:
   `claude plugin marketplace add <repo>` then
   `claude plugin install glidepath@glidepath` (`--no-mod` to skip). If
   `claude` is not on PATH, it prints the two commands for you to run, and
   uninstall still undoes them;
4. writes `~/.glidepath/runtime.json` (the mod reads it: the interpreter,
   its flags and the entry point), a default `config.json`, and a `glidepath`
   launcher in `--bin-dir` (a later install repins it without `--bin-dir`,
   after `--python` or a moved clone too; a new `--bin-dir` moves it there);
5. builds the profile prior from `~/.claude/projects` transcripts
   (`--no-bootstrap` to skip).

The terminal status line applies at once. For the mod, restart Claude Code or
run `/reload-plugins` (in Desktop: start a new session). Then `glidepath doctor`.

## Install on Windows

You need Python 3.8+, and 3.12+ renders about 30 ms faster on Windows
(Performance): from python.org, `winget install Python.Python.3.12`, or the
Python install manager. Git for Windows is optional (`git clone` needs it,
or download the repo as a ZIP): Claude Code runs the status line in Git Bash
when it finds one, else in PowerShell, and install prints which. In
PowerShell, each command on its own line (Windows PowerShell 5.1 has no `&&`):

```powershell
git clone https://github.com/PershinIlya/glidepath.git $HOME\Projects\glidepath
cd $HOME\Projects\glidepath
py -3 bin\glidepath install                                    # dry run
py -3 bin\glidepath install --apply --bin-dir $HOME\.local\bin
```

From the clone, `py -3 glidepath ...` and `py -3 -m glidepath ...` work the same as
`py -3 bin\glidepath ...`; after install, the `glidepath` launcher works from anywhere.

In cmd.exe the same lines work with `%USERPROFILE%` for `$HOME` (and `cd /d`),
each such path in double quotes (`"%USERPROFILE%\.local\bin"`), since a profile
folder may hold a space, and without the comment. `~` in these docs is
`%USERPROFILE%` on Windows: settings in `%USERPROFILE%\.claude\settings.json`,
data in `%USERPROFILE%\.glidepath`. `install --apply` takes the steps above,
with these differences:

- **Python.** `python3` is often only the Microsoft Store stub ("Python was
  not found"), so start install with `py -3`. It pins, in this order, the
  first interpreter that passes: the registry (PEP 514, newest first),
  `py -0p`, the default install folders, the one you ran it with, then
  `python` and `python3` on PATH. Behind a Store alias or `py.exe` it pins the
  real `python.exe`, and for a venv's `python.exe` the base one it starts
  (glidepath runs `-I -S`, so a venv adds only a process); install's list says
  `via <path>` for these. A Store Python is used only when nothing else
  passes. `--python C:\path\to\python.exe` chooses.
- **The shell.** Claude Code looks for Git Bash in `CLAUDE_CODE_GIT_BASH_PATH`,
  `C:\Program Files\Git`, `C:\Program Files (x86)\Git` and beside the `git`
  on PATH; without one it uses PowerShell (pwsh 7, else Windows PowerShell
  5.1). install repeats that search ("Claude Code runs it with Git Bash
  here"), and your previous status line runs in the shell that runs glidepath's.
- **The command.** Forward slashes and `-X utf8` (a pipe gets the ANSI code
  page, and `-I` ignores `PYTHONUTF8`), in a form Git Bash and PowerShell read
  the same, so it keeps working when the shell changes:
  `C:/Users/ana/AppData/Local/Programs/Python/Python312/python.exe -I -S -X utf8 C:/Users/ana/Projects/glidepath/bin/glidepath statusline`.
  A path with a space is written with its folders' 8.3 short names
  (`C:/PROGRA~1/PYTHON~1/python.exe`); on a drive without them the command is
  quoted for the shell Claude Code uses now, and install warns that it runs
  only there. A previous status line that a shell would hand to Python
  changed (one with a `"`, which Windows PowerShell 5.1 alters, or one that
  starts with a POSIX path such as `/usr/bin/env`, which Git Bash rewrites) is
  stored as `--compose-b64 b<base64>` instead of `--compose '<command>'`.
  install says so, and `glidepath doctor` shows it decoded; to change it by hand,
  put `--compose '<command>'` in its place, then run `glidepath install --apply`,
  which encodes it again when a shell would change it (until then
  `glidepath doctor` warns).
- **Launchers.** `--bin-dir` writes `glidepath.cmd`, for PowerShell and cmd.exe,
  and an extensionless `glidepath`, for Git Bash and Claude Code's Bash tool
  (which runs in Git Bash). A later install repins each one you keep, and
  one you delete stays deleted until you pass `--bin-dir` again. The status
  line and the mod run `python.exe` itself, never a launcher. If `glidepath` is
  not found, add the folder to your user PATH, then open a new terminal and
  restart Claude Code:

  ```powershell
  $p = [Environment]::GetEnvironmentVariable('Path', 'User')
  [Environment]::SetEnvironmentVariable('Path', "$p;$HOME\.local\bin", 'User')
  ```

- **Quoting.** PowerShell reads a comma as a list: write
  `glidepath check --tail '5h=60m,week=2d'`.

After you install or remove Git for Windows, or set
`CLAUDE_CODE_GIT_BASH_PATH`, run `glidepath doctor`. If it warns about the
statusLine shell or your previous status line, run `glidepath install --apply`
again. A command that install said reads the same in Git Bash and PowerShell
needs nothing.

### Windows notes

- Only a command quoted for a single shell (above) depends on which shell
  Claude Code picks; `glidepath doctor` checks it.
- Claude Code started from a Git Bash terminal can pick another shell than
  when started from the Desktop app or PowerShell (Git Bash puts its own `git`
  first on PATH). install and doctor say so when run there; run
  `glidepath doctor` from PowerShell to check.
- Your previous status line runs in a nested Git Bash or PowerShell, which
  can take about 1 s to start cold, so `compose.timeout_ms` is 3000 on Windows.
  If your part of the line still drops out (a slow script), set it to 5000 in
  `config.json`.
- Claude Code inside WSL is a separate Linux install: follow the macOS and
  Linux steps there. The Desktop app draws no band in WSL sessions, where
  plugins are not available.
- `GLIDEPATH_HOME`: `~` is `%USERPROFILE%` (never `HOME`); `~\x`, `C:\x`, `C:/x`
  and Git Bash's `/c/x` work. `%USERPROFILE%` in settings.json `env` is not
  expanded (it names a folder called `%USERPROFILE%`). Any other path that
  starts with a single `/` (`/srv/glidepath`) is refused, since Git Bash would
  rewrite it to a folder under its own install (the message names the same
  path on your home's drive, where an older version kept it), and so is `C:x`
  (no slash after the colon, so relative to that drive's current folder).
  Keep it on a local drive: the band usually cannot read a `\\server\share`
  folder.
- `config.json` may be UTF-8 or UTF-16 (what Windows PowerShell 5.1's `>`
  writes); `glidepath config show` says when the file is not used.

## What glidepath changes on your machine

- `~/.claude/settings.json`: only `statusLine`, after a backup beside it
  (`settings.json.glidepath-backup-<UTC time>`, readable by you alone).
- Claude Code's plugins: the `glidepath` marketplace (this folder) and the
  `glidepath@glidepath` mod, through `claude plugin` commands.
- `~/.glidepath/` (or `GLIDEPATH_HOME`), readable by you alone: `tempo.db` (the
  history), `config.json`, `runtime.json` (the interpreter the mod runs),
  `install.json` (what install did, so uninstall can undo it), `errors.log`
  and `mod.log`.
- A `glidepath` launcher in `--bin-dir` (on Windows `glidepath.cmd` and an
  extensionless `glidepath` for Git Bash), when you pass it.
- With the menu bar: `glidepath.1m.sh` in SwiftBar's plugin folder.
- With `/usage` fetch on: Claude Code's trust for the `~/.glidepath` folder
  (you give it once by running `claude` there).

Nothing else: no shell profile, no hooks, no background service.

## Updating

```sh
cd ~/Projects/glidepath
git pull
python3 bin/glidepath install --apply      # Windows: py -3 bin\glidepath install --apply
```

Then restart Claude Code (or `/reload-plugins`) and, with the menu bar,
SwiftBar (`killall SwiftBar; open -a SwiftBar`).

### From pacer or ctempo

glidepath was called ctempo while it was private, and pacer for its first
public release. After `git pull` an old install keeps working: `bin/pacer`
and `bin/ctempo` still run glidepath, and glidepath reads `PACER_HOME` and
`CTEMPO_HOME`, and `~/.pacer` or `~/.claude-tempo` while `~/.glidepath` does
not exist. `glidepath install --apply` moves it: the data folder to
`~/.glidepath`, the `pacer@pacer` or `ctempo@claude-tempo` mod to
`glidepath@glidepath`, SwiftBar's `pacer.1m.sh` or `ctempo.1m.sh` to
`glidepath.1m.sh`, and the status line to `bin/glidepath`. Run it right after
the pull: the old mod may stop drawing the band until then. A `pacer` or
`ctempo` launcher you made with `--bin-dir` stays and keeps working; delete it
when you like.

If you cloned the repository as `pacer`, its folder name does not matter;
`git remote set-url origin https://github.com/PershinIlya/glidepath.git`
points it at the new address (the old one redirects too).

## When something is wrong

- `glidepath doctor` checks the interpreter, the database, the settings wiring,
  the mod, the shell on Windows and the `/usage` fetch, and says what to do
  for each problem it finds.
- **"waiting for usage data"**: Claude Code has not passed rate limits yet
  (send a message), or the account has none (an API key, not a Pro or Max
  plan).
- **No band in the Desktop app**: restart it or start a new session; check
  that `claude plugin list` shows `glidepath@glidepath`.
- **`/usage` prints a cost summary instead of limits**: Claude Code shows plan
  limits only in a folder it trusts; run `claude` once in `~/.glidepath` and
  choose "Yes, I trust this folder".
- Errors from the status line go to `~/.glidepath/errors.log`, the mod's to
  `~/.glidepath/mod.log`.

## Uninstall

```sh
glidepath uninstall            # dry run
glidepath uninstall --apply
```

It uninstalls the mod and the marketplace, puts your previous `statusLine` back
(or the command inside glidepath's `--compose` if you put or changed one there),
or removes glidepath's, removes the launcher (on Windows both) and `runtime.json`.
`--no-mod` keeps the mod, and `runtime.json` and the launchers with it, until
an uninstall without it. If `~/.glidepath/install.json` is gone (you deleted
the folder, or use another `GLIDEPATH_HOME`) while your settings still list the
mod, uninstall says so and removes it all the same. A launcher made before the
folder was deleted stays: remove it yourself. If you did not change
`settings.json` in between, the file comes back byte for byte (it is restored
from the backup). If `claude` is not on PATH, uninstall prints the two plugin
commands and exits 1; once your settings list neither the plugin nor its
marketplace (you ran them yourself), it completes. Your history stays in
`~/.glidepath/`; delete that folder yourself if you want it gone.
