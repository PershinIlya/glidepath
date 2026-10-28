# Design decisions

Every rule in glidepath that is not obvious, with the reason and, often, the case
that shaped it. Numbered so code comments can point here (`D-37`). glidepath was
called ctempo while it was private and pacer for its first public release;
some entries still say so.

## Environment and packaging

**D-01. Portable by construction.** glidepath was built and tested mostly
without the machines it runs on. The code has no OS-specific paths outside
`config.IS_WINDOWS` branches: it uses `time.localtime`, POSIX `sh` for
composition (Git Bash or PowerShell on Windows, D-26), and probes
`/usr/bin/python3`, `/opt/homebrew/bin/python3` and `/usr/local/bin/python3`
(macOS locations) first. Windows branches run on Linux with the one platform
flag patched, and on real Windows in CI (the suite, the mod's tests, and
`scripts/ci_integration.py` through Git Bash, pwsh, powershell.exe and
cmd.exe); D-25 to D-29 hold its choices.

**D-02. Python 3.8+, standard library only, package plus launcher.** The core
is `glidepath/` (importable) and `bin/glidepath` (a launcher that puts the repo on
`sys.path`, so it runs under `python -I -S`). macOS Command Line Tools ship
Python 3.9, so nothing newer than 3.8 syntax is used; the suite runs on 3.8,
3.9, 3.11, 3.12 and 3.13. The statusline path does not import argparse.

**D-03. Interpreter pinning.** `glidepath install` probes candidates with
`python -I -S -c "import sys, sqlite3, json"` (three runs, best time), in this
order: `/usr/bin/python3` (skipped on macOS when the Command Line Tools are
missing, because the shim would pop an install dialog), Homebrew, the
interpreter running install, `which python3`. The first that passes wins;
`--python` overrides. System and Homebrew paths come first because a conda env
can be deactivated, updated or deleted, and the Desktop app does not inherit
the shell's PATH. The chosen absolute path goes into the statusLine command
and into `~/.glidepath/runtime.json` (read by the mod). `-I` ignores
PYTHON* variables and the user site; `-S` skips `site` (faster, and stdlib
needs nothing from it). On Windows the candidates, the probe and the pin
differ (D-27), and every command adds `-X utf8` (D-25).

## Data

**D-04. Sources.** Primary: the statusLine stdin payload, written by
`glidepath statusline` (no network, no token). Desktop: the mod reads
`$.session.usage().rateLimits` (and the same list from `session.measure`),
converts it to the statusLine shape (ISO `resetsAt` to epoch seconds) and
calls the same CLI. One code path, two collectors.

glidepath never reads credentials. It does not call Anthropic's usage
endpoints itself: the only levels it sees are the ones Claude Code hands the
status line or the mod, and, when you turn it on, what Claude Code's own
`/usage` prints (D-32), a request Claude Code makes with its own login.

**D-05. Store.** SQLite at `~/.glidepath/tempo.db` (override with
`GLIDEPATH_HOME`; a leading `~` is expanded and a relative path is taken from the
home directory, because Claude Code passes settings.json `env` values as
written and the statusLine runs in each session's folder; on Windows by the
rules of D-28), WAL,
`synchronous=NORMAL`, busy timeout 250 ms, schema version in `PRAGMA
user_version`. The first open of a new database waits up to the busy timeout
for another opener's switch to WAL. SQLite answers a concurrent switch with
"locked" at once, without the timeout, so when every open session re-rendered
at install, some fell back to the bare line and lost their reading (found in
review). On Windows with Python 3.12+ the last connection closes without a
checkpoint (`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`): folding the WAL into the
database and deleting it took 18 ms of a 30 ms render on the CI runner (p95
32 of 44), and a render that records a reading went from p50 83 ms, p95 101
ms to p50 51 ms, p95 55 ms there. The WAL stays between renders; a close that
finds it 1000 pages or larger empties it (`wal_checkpoint(TRUNCATE)`, about
once in 165 renders, never waiting for another process's read): a process that
opens the database alone rebuilds the WAL index as if nothing had been copied,
so without that every commit past 1000 pages copied the whole WAL again (found
in review). Tables: `samples(ts, window, used_pct, resets_at, source, session_id,
flag, window_id)` (the brief's columns plus `flag` and `window_id`), `windows`
(one row per window instance: start, reset, first/last seen, last and max
used, closed_at, unused_at_close, close_reason), `readers` (each session's
last reading, for freshness), `kinds` (every rate-limit kind seen, with source
and count), `meta` (profile, counters, rejections). One `BEGIN IMMEDIATE`
transaction per ingest. Any `sqlite3.OperationalError` (locked) skips the
write and still renders; when the skipped write would have opened a new window
(no open instance whose reset is still ahead), that window shows the payload's
level and reset only, with no pace math, as the bare line does; a broken
database falls back to a line built from the payload alone.

**D-06. Freshness.** The statusLine payload carries the rate limits from the
session's *last API response*. An idle session re-renders every 60 s with an
old value; with parallel sessions, that stale value interleaves with a busy
session's fresh ones (ccpool saw 76% of rows sharing timestamps). A reading is
FRESH when it differs from what the same session reported before; otherwise it
is a HEARTBEAT: it proves time passed, never moves the series, never raises a
window's max, never rolls a window over, does not update `last_seen`, and
cannot trigger a reset. A session id can be shared by 2 processes
(`claude -c`, or `--resume` without `--fork-session`, in a second terminal
while the first is open). The reader row then holds whichever wrote last, so
the idle process's re-render of its last API response differs from it and
looked fresh. A single process only moves forward: within a window its level
only rises, a moved reset keeps the level, and it returns to a `resets_at` it
left only to undo its own bogus later one (D-10). So a changed reading that
goes back to one the session already gave is the idle process's replay, and it
is a heartbeat: its reading before last (each session keeps it in a second
reader row), or a reading it gave a window that is no longer open (superseded
by a moved or an early reset). At the open window's reset only the first test
applies, and only when the session's last reading had that reset too (A, B, A
within the window): a render there whose session read another reset in between
is the busy process's, and it breaks the stale one's pending run (D-10). A
return to a later reset at the very level the session left stays fresh (a
reset moved earlier and back, read by a maxed session), and a return to an
earlier reset from a session that last read a later one is left to D-10. A
replay of a reading the session gave a window that is no longer open also
withdraws that reading's earlier render from a pending reset, so a later real
reset cannot sweep it in. At the open window's reset it does not: there A, B,
A is symmetric (the busy process's re-render of its own new reading matches it
too), and withdrawing there deleted the busy process's real pending drop, so a
real early reset was never taken; the statusline and the Desktop mod of one
session, which share its id, did the same when their writes crossed (found in
review). Instead the reading that completes a drop or a rise run must have
moved forward from its own session's last reading, as a single process does.
Idle twins faked drop resets below another session's max, reopened windows
that a moved or an early reset had superseded, and rolled a window whose reset
moved earlier back to the old reset on every render pair (found in review).
Limits: the reading before last does not catch 3 or more processes cycling on
1 session id; a reading that another session's identical row deduplicated
(D-07) leaves no sample to match; and a real reset whose `resets_at` lands on
that of a window glidepath closed, read at levels the session already gave that
window, waits for a level it did not give. Three more cases are known and left
as they are, since 2 processes on 1 session id are rare and glidepath cannot tell
them apart without a process identity (Claude Code runs each statusLine render
through a new `sh` in a new process group, so neither the parent nor the group
says which process it is): an idle process's stale reading can complete a real
drop run and set the new window's level until the real level passes it; with a
light week near 0, idle processes of 2 sessions can reopen a week that was
reset early; and a process's replay of a later reset it left can roll a window
whose reset moved earlier back (found in review). Opening one conversation in
a second terminal with `--fork-session` avoids all of them. A session's very
first reading is stored as FIRST: when the statusLine is installed, every open
session re-renders at once with whatever it last saw, so that reading is a
real level but may be hours old. It counts for the window's max and for the
weekly peak, cannot be an anomaly (two idle sessions would otherwise fake a
drop reset), is not used for profile learning (except that one at 100 marks
its window as capped, D-13), enters the 5h level path only as a lower bound
(D-11), and never fixes the start of the weekly lookback (D-15). These rules
were found in review. The line shows the level as judged and stored; the raw
reading is used only when the write failed (an idle session's stale pre-reset
value once showed "maxed"). Analysis uses the running max of fresh readings
per window instance (usage is monotone within a window).

**D-07. Write throttle and dedupe.** A fresh reading identical to the last
stored row of that window within 60 s is not stored again (two sessions
reporting the same value). A heartbeat is stored only if the window's last row
is at least 60 s old, across all sessions. The per-session reader row is
written only on change or once a day.

**D-08. Window instances.** `resets_at` within 300 s of the open instance is
the same window: the epoch wobbles by seconds between renders (ccpool,
claumon). A later `resets_at` rolls over (the old instance closes with
`unused_at_close = 100 - max_used`). When the new window began before the old
one's last fresh reading, at about the same level (less than
min(`drop_points`, max(1, half the old max)) below it), the reset moved: that
window never ended, and it closes as `shift` (D-10), so `report`, the
profile's points-vs-pace table, the typical week and the weekly lookback leave
it out. The 2 conditions are the continuation test learning uses (D-13). A
weekly reset that moved 1 h later mid-week listed the running week as
finished, with 55 phantom unused points (found in review). A new window that
began after the last reading is a real (early) reset, whatever its level.
Judged on the level alone, a light week that peaked at 10%, was reset early on
Wednesday and was first read again on Thursday at 6% (spent on claude.ai
first) closed as `shift`, and `report` left out a week that had ended (found
in review). Instances whose reset time passed close as `expired` at their
reset time; a reading whose `resets_at` is not in the future is rejected (no
grace, so the two rules agree). Window start is `resets_at - 5 h` or `- 7 d`.

**D-09. Guards.** `used_percentage` must be a finite number in [0, 105]:
Claude Code bug #52326 sometimes puts the `resets_at` epoch there (ccpool,
Claude-Code-Usage-Monitor); values in (100, 105] clamp to 100. `resets_at`
may be seconds, milliseconds, a numeric string or ISO 8601; it must be after
2017, not already passed (5 s grace), and no further ahead than the window
length plus 15 min (5h) or 1 h (7d). Each rejection is counted in `meta`
(`glidepath doctor` shows them). Booleans are not numbers.

**D-10. Reset detection.** Besides a later `resets_at`: a fresh reading at
least 15 points below the window's max (15 included) is an ANOMALY; two
consecutive fresh anomalies (default `reset.drop_samples = 2`) are a reset,
the new instance starts at the first one, and the weekly math paces it from
there (D-15). The instance the drop closed (reason `drop`) never reached its
reset, so the weekly lookback and the profile's points-vs-pace table leave it
out. The typical week also leaves out the instance the drop opened, which
holds only part of a week (D-13). This is ccpace's rule; ccpace observed a
real 100 to 0 reset with an unchanged `resets_at`. Every low in the run must
itself be 15 points down, which closes ccpace's over-count case (50 then 30,
48). The anomaly that completes the run must also agree with its own session's
previous reading (a low one, for a drop): an idle twin's re-render goes
straight from the busy twin's reading to the anomaly, so it joins a run but
cannot complete it. Sessions that each had an idle twin with an old, lower
reading made a drop reset when the statusLine was installed, and the idle
twins of 2 sessions made one whenever their busy twins read something in
between (found in review). A drop that several sessions read once each
therefore waits for a second low reading from one of them. The window a drop
opens is exposed to the old level too: an idle twin's pre-reset reading is
fresh whenever its busy twin read twice since its last render, and it raised a
new window at 2% back to 85%, after which the real readings faked another drop
every minute (found in review). A fresh reading 15 points or more above the
max of a window a drop opened is an anomaly too (\"rise\"), taken by the same
rule, so a real jump (claude.ai use while Claude Code was idle) shows 1
reading later, and one that a maxed or idle session then only repeats waits
for another reading. A twin's pre-reset reading less than 15 points above the
new window's max still raises it; only that margin guards it, as on the undo
below. A fresh reading with an *earlier* `resets_at` is an anomaly too
("shift"): if it is sustained, the open instance came from a bogus later
reset, and the real window, closed by it, is reopened instead of being split
in two. A reset moved earlier at about the same level (D-08's test, less than
`drop_points` above the max, and not below the session's own last reading of
the open window), to a `resets_at` that no window instance ever had, is taken
on 1 reading, as a move later is. A maxed session whose 5-hour reset moved 1
hour earlier repeated its reading only as heartbeats, so the line kept the old
reset 1 hour past the real one, and a bogus later reset that was the first
reading of a new window was never undone (found in review). A stale reading (a
lagging session's or an idle twin's) carries a reset some instance had, so it
still needs `drop_samples`. One fresh reading undoes it, because one reading
did the damage. A maxed or idle session repeats its real reading only as
heartbeats, so the second fresh one never came: the line kept the bogus reset
time past the real reset, and `report` counted the window twice (found in
review). The undo by one reading takes 4 conditions. First, the bogus
instance's rollover closed the window that this reading's `resets_at` belongs
to, either as `shift` (D-08: the reset moved and the level held) or as
`rollover` with the bogus reading at or above that window's max (a maxed
session reads no fresh value after its 100, so its bogus reset can begin after
its last reading; matching `shift` only left it stuck again, found in review).
The undo follows the session's own chain of bogus rollovers back to the window
this reading belongs to, and every bogus instance in between closes as
`shift`: a maxed session that read two bogus later resets in a row kept a
bogus reset time, and `report` counted 3 windows (found in review). Each link
of that chain must pass the first condition, not only the last: a rollover
below its window's max is a real early reset, and the chain stops there
(walking across one let an idle twin reopen a week that had been reset early,
found in review). Second, the reading comes from the session whose reading
opened the bogus instance. Third, it is not below that opening reading, nor
below the session's own last reading of the moved reset (a session's real
reading never is; once the busy twin read a higher level at the moved reset,
an idle twin's stale reading equal to the opening one still undid the move,
found in review). Fourth, it sits less than `drop_points` above the bogus
instance's max. Two processes can share a session id (`claude -c` or
`--resume` without `--fork-session`), and the idle one keeps re-rendering the
old window. When the undo also matched a window closed as `rollover` (the
level dropped, or the new window began after the last reading, which is what a
real early reset looks like), only the `drop_points` margin guarded it. That
stale reading then flipped the line and `check` back to the dead window on
every render pair: from the first render after a weekly window at 10% was
reset early, and once a new 5-hour window climbed within 15 points of the old
90%. After a move to a later reset at 42%, a twin's stale 41% did the same
(found in review, twice). A twin whose stale reading equals the session's
reading of the moved reset still undoes it on each render pair until the level
moves on, and a twin's reading at the old later reset that equals the
session's reading of a reset moved earlier rolls it back the same way; the
readings cannot tell it from the session's own real reading. The undo still
moves the real window's pending readings from other sessions (up to
`drop_samples - 1`) into it. One was left behind in the superseded instance,
and the reopened window showed a level too low until the next fresh reading
(found in review). From any other session the undo still takes `drop_samples`,
since that session may lag a real early reset, and so does a window closed as
`rollover` above the bogus reading (a real early reset shows a lower level). A
session whose last reading, or its reading before last, had a later reset
completes such a run only with what a reset moved earlier looks like (about
the same level, not below its own last reading of the open window) when its
reading may be a replay: one it already gave (D-06), or its first change,
whose reading before last is unknown. The idle twins of 2 sessions, each at
the old reset while its busy twin read the reset moved later, made the run
that reopened the old window, and at install 2 sessions' idle twins put the
line on a week that had ended before glidepath saw it (found in review). The
superseded instance stays closed with reason `shift`. It never ended, so
`report`, the profile's points-vs-pace table, the typical week and the weekly
lookback all leave it out.

## 5-hour math

**D-11. Burn rate.** A heartbeat repeats a session's last API response and
says nothing about the account's level (claude.ai keeps spending while a
Claude Code session idles), so the rate uses fresh readings only. They form a
*level path*: a point where each new level (running max) was first read (a
repeat of the same whole percent says nothing about when the next one comes),
through 0 at the window start (every window opens empty), linear between
readings (spend in an unobserved gap is spread over the gap instead of being
squeezed into the moment we looked). After the last step between two readings
the path keeps that step's slope for one reading quantum (the smallest step
seen, 0.1 to 1 point: steady work shows no new value between whole-percent
steps, and a flat tail used to read it as slowing down), then goes flat (no
news, no spend: a finished burst fades out and idle reads exactly 0). A
session's FIRST reading (D-06) only says the level was at least that by then,
so it becomes a point only where it lifts the path: a 3-hour-old 30%
re-rendered at install next to a fresh 60% is no 30-point burst. The rate is
the least-squares slope of that path over the trailing 20 minutes (brief),
sampled at its edges and at every point inside, but never reaching back past
the window's first real point once a second real point follows it (the
0-at-start ramp diluted a late start); until 5 minutes follow that first point
there is no rate, and the line shows only the allowed rate. The burn is then
unknown, not zero, and so is the leftover: the window is never green (D-16)
and `check` says no go (D-20); `glidepath now` names which wait it is (the window
opened less than 5 minutes ago, or work started in it less than 5 minutes ago,
at HH:MM). An unknown burn priced as zero turned a late start in the tail
green, with `check` saying go, while it ran dry (found in review). A single
real point keeps reaching back: the window average, which fades out over the
fit window. (Jumping to the first point on elapsed time alone turned a single
reading into idle after 5 minutes, and at a window's start flipped speed up to
dry with no new data.) With at least 3 readings spanning 5 minutes (or the
whole fit window, if that is shorter; a FIRST reading counts only where it is
a point of the path) the fit is plain least squares; otherwise it leans on
interpolation and the line marks it `~` (a single reading in a window gives
the window average). Slopes below 0.05 %/h count as idle. ccpool fits only raw
samples and ends at the last one, so an old burst keeps a positive slope. This
model came out of three review passes: anchoring on the last stored value let
an idle session's stale heartbeat fake a 36 %/h burst, an open session's
heartbeats squeezed two hours of claude.ai use into minutes, and the flat tail
made steady 9 %/h work flicker between "slow down" and "speed up".

**D-12. Allowed rate, multiplier, projection.** As in the brief: allowed =
max(0, target - used) / hours to reset; multiplier = allowed / current;
projection = used + rate x remaining, capped at 100, with the cap time when it
would hit 100 before the reset. Verdict: multiplier >= 1.2 is "speed up", <
0.9 is "slow down", else "on pace" (configurable). When idle the line shows
the allowed rate ("idle <=9%/h").

## Weekly math

**D-13. Profile shape.** 168 bins in local time (Monday 00:00 = bin 0); time is
cut at local hour boundaries, so DST days with 23 or 25 hours are handled.
`w(h)` combines:

- prior `p(h)`: from transcripts (D-14), from the manual override
  (`work_days`, `wake_hours`, `off_day_factor`, mode `prior` or `replace`,
  like ccpool's work days and wake hours), else uniform;
- observations: weekly-percent spend per completed local hour. Spend between
  consecutive fresh readings is spread evenly in time when the gap is at most
  2 h (well localized, confidence 1), by the current profile when longer (only
  the total is known, confidence 0.25). An hour is final once a fresh reading
  after it exists. At a window change, the new window continues the old one (a
  moved or bogus reset, the real window reopened) only when it started before
  the previous reading and its level is closer to the old level than to 0:
  less than min(`drop_points`, max(1, half the old level)) below it. Otherwise
  it counts from 0. With the 15-point drop alone, `profile --rebuild` learned
  a light user's early reset as 0 %/h (found in review). At a real reset, the
  old window's tail from its last fresh reading to the new window's start,
  when longer than `max_gap_hours`, is learned as no spend at confidence 0.25
  (no reading, no news, no spend, D-11). Unlearned, those hours kept `M p(h)`
  for good. With a Monday 09:00 reset and a uniform prior, a weekday user's
  weekend stayed awake: 107 awake hours instead of 45, and a typical week of
  90% for a true 56% (found in review). A window that reached 100% is the
  exception. The account was blocked in its tail, which says nothing about the
  habit, and learning it as idle made a user who caps every Tuesday read "on
  pace" on Monday. Reaching 100% counts by any FRESH or FIRST reading, as in
  the lookback (D-15). When the last points go on claude.ai, a new session's
  first reading can be the only one at 100, and learning, which looked at
  FRESH readings only, learned that blocked tail as idle (found in review).
  Per bin, a decay-weighted mean with a 14-day half-life (continuous-time
  EWMA: weight `0.5 ** (age / 14 d)`).
- shrinkage: `r(h) = (n m + k M p) / (n + k)`, `n` the decayed confidence
  weight, `m` the observed mean, `M` the weekly total. `k = 1` for an
  informative prior (transcripts, override), `0.25` for the uniform one so a
  few observations can outweigh "I know nothing". `M` is the decay-weighted
  peak of recent closed weekly windows that were observed in their last day
  (by the last fresh reading, not by heartbeats; an unobserved tail
  understates the peak, claumon's weak spot) and lasted a whole week (the
  window a drop reset opens holds only part of one: with one among 3 weeks at
  90%, the typical week read 77%, found in review; nor does one that an early
  reset cut short more than 1 hour before its reset: read in its last day, a
  window closed 23 h early at 74% pulled the typical week to 86%, found in
  review); without those, the unweighted ratio `sum(m) / sum(p)` over observed
  bins. Unweighted because busy hours are observed with more confidence (short
  gaps), which inflated the weighted ratio from 40 to 106 in a test. The line,
  `glidepath profile`, `profile --rebuild` and learning all build the weights
  this way (`profile.current_weights`); `profile` used the ratio and printed a
  typical week of 168% (found in review). `glidepath profile` names the three
  lightest 3-hour blocks whose hours are all awake by that rule, and none on a
  flat profile. It listed "Mon 00-03, Mon 03-06, Mon 06-09" on a flat profile,
  and wake-up or bedtime blocks that were two-thirds asleep (found in review).
  Its points-vs-pace table is the level minus the on-pace level for
  `target_week`, each window paced from its own start as on the line (D-15),
  so the reset column is what expired (100 - peak) only at target 100. It
  leaves out a window that an early reset cut short (a drop, D-10, or a
  rollover more than 1 hour before its reset). The window that replaced it
  holds the rest of that week, so each week counts once. The table paced a
  drop's window from `resets_at - 7 d`, set the closed window's frozen level
  against a rising pace, and counted the drop week twice: up to 18 points of
  "surplus" for a user on pace (found in review). Below 100 the note under the
  table says it is the peak versus the target and points to `report` (found in
  review).
- floor: every bin is raised to `0.15 x typical`, typical = mean of the
  busiest third of the bins (ccpool's 0.15). An hour is "awake" when its rate
  is at least 0.25 x typical (thevibeworks' rest-hour threshold).

Why not thevibeworks' factorized weekday x hour-of-day model: it has more data
per parameter, but cannot represent "Friday afternoons are light" and needs 14
days before it speaks. The transcript prior removes the cold start, and the
shrinkage handles the noise of 168 bins.

Learning runs inside the statusline at most every 10 minutes (incremental, a
few ms); `glidepath profile --rebuild` relearns from all samples.

**D-14. Transcript prior.** Assistant messages under `~/.claude/projects/**`
(subagents included) from the last 45 days (`profile.bootstrap_days`); files
not modified in that period are skipped unopened. Per message only the
timestamp, message id and token counts are kept; content is parsed (the line
is one JSON object) and dropped. A message streamed over several lines shares
one id; the largest usage wins. Weight =
`input + 1.25 cache_creation + 0.1 cache_read + 5 output` (API price ratios
relative to fresh input); only the distribution over the week matters. Each
bin's mean is over *every* hour of the period, zeros included (thevibeworks
averages per active day, which runs high), then lightly smoothed along the
hour axis. A short history is blended toward uniform:
`prior = mix x shape + (1 - mix) x uniform`, with
`mix = min(1, covered_days / 14, active_days / 4)`; `covered_days` starts
where 5% of the cost is behind, so one stray old message does not buy trust
(found in review). The prior strength `k` moves from 0.25 to 1 with `mix`.
Found on live data: one hour of transcripts otherwise made a three-hour spike.
It misses claude.ai and Desktop chat; the percent observations correct that
over time. For that, the prior fades toward uniform with the profile half-life
from its `built_at`, in shape (`a x prior + (1 - a) x uniform`,
`a = 0.5 ** (age / 14 d)`) and in strength (trust times `a`). Hours that went
idle since then are only seen through overnight gaps, learned at low
confidence and spread by the prior's own shape. So a prior of constant
strength kept Mon-Fri 9-13 at 20% of the week 8 weeks after a move to 14-23
(found in review). `glidepath profile` prints the prior's age and its weight now
(trust times `a`) beside the trust it was built with. It printed the
build-time trust for good: "trusted 100%" 8 weeks on (found in review). Under
`profile.override` the transcript prior is unused, and `glidepath profile` says
so; it printed the prior's faded weight there too (found in review). With an
override and no transcript prior, `glidepath profile` names the override as the
prior and `doctor` no longer warns "no transcript prior, run
`glidepath bootstrap`" (found in review). Under mode `replace` the learned hours
are unused as well, and `glidepath profile` and `doctor` say so instead of
"folded in" (found in review). `glidepath bootstrap` builds a fresh one.
`install --apply` runs the bootstrap too (`--no-bootstrap` skips it).

**D-15. Weekly numbers.** As in the brief, with these details: `E` is
normalized by the week's total share (DST weeks have 167 or 169 local hours).
After a drop reset (D-10) the instance starts at the drop. The elapsed share,
the on-pace level (`target x (1 - E / span)`, `span` the instance's own share
of the week) and the whole-window fallback count from there, while `E`,
intensity and the projection keep the full week's normalization. Measured from
`resets_at - 7 d`, a user exactly on pace for the new window read 38 points
behind for days. In its first hour, the whole-window fallback divided the new
level by the old week's elapsed share: `×9+▲ →2%` (found in review). Normal
instances are unchanged. Intensity uses the last 24 *awake* hours, walking
back through nights and weekends (since D-37 never past the window's start,
and pulled toward the typical week; the rest of this paragraph on the
previous window describes the rule before it), and crosses into the previous weekly window
when needed (spend = previous window's peak minus its value at the lookback
start, plus the current window from 0). The previous window is the one whose
reset is nearest the current window's start, never one that did not end
(`shift`) or that a drop reset replaced (`drop`). Both drop windows end at the
same reset, give or take the seconds `resets_at` wobbles, so the nearer one
was picked by chance and could be the frozen one, which lost the spend since
the drop (found in review). The value at the lookback start is the running max
of readings up to it. A FIRST reading (D-06) never fixes that start: with no
FRESH reading at or before it, the count starts at the first FRESH one (a
stale level re-rendered at install showed a red "dry" for a day, depending on
which session rendered first). When the start falls in a gap between readings
longer than `max_gap_hours`, the gap's rise is spread over it by profile
share, as in learning (D-13), and only the part after the start counts (a
weekend of claude.ai use was charged to the last 24 hours). When the lookback
reaches into a previous window that reached 100%, the hours from its cap to
the new window's start are left out of the expected share, since nothing could
be spent there. The cap is that window's first FRESH or FIRST reading at 100,
an upper bound. Two hours after a Thursday reset, a user blocked since Tuesday
read "on pace" and capped again (found in review). The rest of the previous
window after its last reading still counts as no spend: a claude.ai-only last
day and an idle one leave the same readings, so spreading that tail by the
profile would trade one error for the other. Fallbacks when the lookback holds
less than 1% of a week's expected spend, or when it was cut short at the first
reading and has not seen a whole-percent step yet (that zero is no news, not
idle): the whole window so far (unless it is still at 0 after a cut lookback,
which is no news either; it showed `×? →0%`), then the typical week
`M / target`, then 1.0, each labeled in `glidepath now` with its reason (recent
span too short; the level has not moved since the first timed reading; no
timed reading yet); the typical week and 1.0 also say why the whole window
could not stand in (it is still at 0%, or it is too new: under 2% of a week's
expected spend has passed since it opened). They said "no recent data" and "no
data yet" next to hours of readings. "Dry" is the hour-by-hour walk at that
intensity (thevibeworks' `_profile_walk` shape). Verdict thresholds are the
same as for 5h; "idle" when the last 24 awake hours spent nothing. The
multiplier is max(0, target - used) / remaining spend, so it is 0 past a
target below 100, as for 5h.

**D-23. Exchange rate.** Weekly points per full 5-hour window, from paired
readings (thevibeworks' `pct_per_window`: published once the 5h side rose 50
points and the weekly side 3, clamped to [1, 50]). `now` and `report` use it
to say how many 5-hour windows the expiring weekly surplus is worth; `check`
uses it to price a 5h leftover in weekly points, and uses the clamp maximum
(50) while it is unknown.

## Display

**D-16. The classic line.** The terminal's line until the band (D-30), kept
as `display.style: "classic"`:
`5h 62% ×1.4▲ →88% @13:20 · wk 48% +3 ×1.1 →95% @Thu 09:00`.
`×` is the multiplier with a verdict glyph (▲ speed up, ▼ slow down, none on
pace), so the verdict survives `NO_COLOR`. `+3` is points versus the on-pace
level (plus = spending faster than your profile). `dry 12:40` / `dry~Wed
14:00` replaces the projection when the limit would be hit before the reset.
`~` marks the 5h window-average fallback. Tones: dim when fine; yellow for a
mild overshoot (it runs dry before the reset while the verdict is still on
pace), for slow down without a dry time, or at or above a target set below 100
whatever the verdict (an idle window above its target was green, found in
review); bold red when it runs dry with a slow down verdict, or is maxed; and
green (an addition to the brief) when a window is in its tail with at least
`min_free` points about to expire below its target (max(0, target -
projected): a target below 100 keeps a reserve, and green below the target
invited spending it, found in review), never while the 5h burn rate is unknown
(D-11), whatever `min_free` (0 included), and never for 0 points. Times
(resets, dry times) are rounded to the nearest minute: a window keeps the
`resets_at` of its first reading (D-08), and when that one wobbled 2 s early
the line showed `@13:59` and `@Thu 08:59` for the whole window (found in
review); a wobble of 30 s or more still shows a minute early. At target 100
every slow down runs dry, so yellow was unreachable at the defaults until red
was made to need both (found in review). `NO_COLOR` and `TERM=dumb` disable
color; `display.symbols: "ascii"` swaps the glyphs. Output is written as UTF-8
bytes whatever the locale. Config values of the wrong type or out of range
fall back to the default or are clamped at load (`config.RANGES`), so a typo
never breaks the line. When the line would exceed `display.max_width` (80),
optional parts are dropped in order: 5h reset time, weekly delta, weekly reset
time, then projections. If it still does not fit, it is cut at the width with
`…` (`...` in ascii mode) and each part keeps its tone; the bare line and the
waiting message use the configured separator and are fitted the same way. The
statusline never prints a traceback: errors go to `~/.glidepath/errors.log`
(64 KB, one rotation) and the line degrades to "glidepath: error" or a bare line
from the payload.

**D-17. Composing with an existing statusLine.** `install` keeps the old
command inside ours (`--compose '<old command>'`, shell-quoted), so
`settings.json` shows exactly what runs. At render time the old command starts
first, in its own process group, with the same stdin, and runs in parallel
with glidepath's own work; its output comes first and our segment is appended to
its last line (two spaces) or put on a new line when the joined line would not
fit where Claude Code draws it. Its prompt footer pads the line by 2 columns a
side, so the room is `COLUMNS - 4`, counted in display columns: CJK characters
and most emoji take 2, combining marks and joiners take 0, and VS16 makes a
narrow symbol 2. Measured as `COLUMNS` code points, a joined line in the last
4 columns, or one with emoji in it, lost its weekly reset to Claude Code's `…`
(found in review). A nonzero `statusLine.padding` takes 2 more columns per
unit; glidepath does not read it. Its output is complete once it has exited and
its stdout has closed or stayed quiet for 500 ms (Claude Code waits the same
for a hook's remaining output); a background job it started may keep stdout
open, and is left running. Timeout 1.5 s, 3 s on Windows (`compose.timeout_ms`,
counted from glidepath's start): a command still running then is killed with its
children and only our segment shows; one that already exited keeps the output
that arrived. `GLIDEPATH_COMPOSED=1` stops recursion. `--replace` replaces instead.
`refreshInterval` becomes 60, or stays lower if the old one was lower.

On Windows a command that a shell would hand to Python changed is stored
encoded instead (D-26). The old command runs in the shell Claude Code ran
glidepath in (D-26), never through cmd.exe, which `shell=True` means there; it
gets no console window, and a process group of its own as on POSIX. Threads
serve its pipes (`select()` takes only sockets there) with the same
exit-plus-quiet rule, and a command still running at the deadline is killed
with its tree by `taskkill /T /F` (Git Bash's `bash.exe` runs the real bash as
its child). On every system a `\r` at the end of each of its lines is dropped:
a Windows command ends its lines with CRLF, and the `\r` left before our
segment sent the cursor back over the user's text.

**D-19. The mod.** Thin: it builds the statusLine-shaped payload from
`rateLimits`, runs `python -I -S bin/glidepath statusline --source mod --format
json` through `$.process.run` (the flags are `runtime.json`'s `args`: `-I -S -X
utf8` on Windows, and `-I -S` when a file written before `args` has none) and
draws the tone-tagged segments it gets back (no ANSI parsing). It calls the CLI
after each `session.measure` whose `rateLimits` changed, when its 5 s poll sees
a new reading (D-31) and every 60 s (time-dependent numbers), never on the
start path, and coalesces calls that
arrive while one runs (statusline-anywhere drops them). It draws on the
surfaces in `mod.surfaces` (default `["desktop"]`; VS Code and mobile draw no
band per the docs) and passes the band on in the terminal, where the statusLine
already shows the line; it keeps what other mods draw below its own row. Every
`kind` it sees is logged by the CLI in the `kinds` table. It resolves
`GLIDEPATH_HOME` like the CLI (empty counts as unset, a leading `~` is the home
directory, a relative path is taken from it; `~user/...` is an error that asks
for an absolute path), passes the home it resolved to the CLI, shows the CLI's
stderr when it fails, and re-arms its timer to `mod.refresh_s` (clamped to
15..3600 s). The home directory is `HOME`, and on Windows (`OS=Windows_NT`,
since no `$` API names the OS) `USERPROFILE`, else `HOMEDRIVE` plus `HOMEPATH`,
never `HOME`, as in Python 3.8+; the Windows forms are in D-28. A leading byte
order mark in `runtime.json` is ignored. Desktop does not inherit the shell
environment: set `GLIDEPATH_HOME` in `settings.json` `env` if you move the home
(Claude Code passes the value as written, without expanding `~`). On Windows a
missing interpreter does not fail to start: the spawn falls back to cmd.exe,
whose message ("...operable program or batch file.", exit 9009) named neither
Python nor the fix. So after a failed run the mod checks that the interpreter
exists, says "Python not found at <path>: run `glidepath install --apply`", and
reads `runtime.json` again on its next refresh; for exit 9009 it shows
cmd.exe's whole message. That fallback is inferred from the engine's spawn
code, not seen on Windows (the mod tests stub `$.process.run`). The band draws
in the Desktop app's local sessions only: plugins are not available in WSL
sessions.

**D-30. The band.** The first band drew the line's segments as text, and the
person using it found the symbols (`×0.9▼`, `→88%`, `@Thu`) unreadable. 3 design
rounds, judged by 2 reviewers each, ended at "Quiet Bar Line": one 18 px row,
English words only, at most one bold number per window. Each window is a dim
name, a small SVG bar, the percent rounded down (D-31) and words only when they
matter. The bar is solid orange (Claude's own accent) for what is used, then
after a hairline gap a pale forecast to where the window lands at its reset;
the forecast fades while the estimate is rough (fewer than 3 fresh readings
for the 5 hours, a typical or assumed week); it turns red into a short wall
when the window runs out before the reset; green dots fill quota that will go
unused (the opportunity tone of D-16); the whole bar is red when maxed and
grey when idle. A calm window says nothing ("on pace" is the bar), or a dim
"Can go 2.7x faster" (halves of an early estimate, "about 2x"; "a bit" below
1.5; "Plenty of room" from 10). A window that needs you says one thing in
color: "Runs out 15:40" (red; "May run out" in yellow for the milder
overshoot, whose red forecast and wall fade), "Ease off", "40% will go
unused" (green, only from a whole point: with `min_free` 0 a leftover that
rounds to 0 said "0% will go unused", found in review), "Back at 16:30",
"Over your 80% target" (at or above a target below 100 while idle or without
a rate, yellow as in D-16; the band said a calm "Idle", found in review); the
other window's percent then dims. While the week is maxed, the 5-hour bar is
grey with no forecast and no words (it said "Can go 2.2x faster", found in
review). Widths are thresholds on the band's
`bodyColumns`, checked in Chromium from 40 to 140 columns: halves with resets
and calm words from 104, bars 64 px from 72 and 44 px below, no Details button
and "Wk" below 52, and below 44 a calm window beside one that needs you keeps
only its name and percent. With the small bars the words that need you take
short forms ("Out 15:40", "Back Fri 04:00"); below 104 cells, when 2 windows
need you or one phrase is longer than 14 characters (12 below 56 cells), the
Details button leaves its room to them, and below 56 cells 2 such phrases drop
the bars (in Chromium at 55 columns both phrases were cut mid-word, found in
review). The mod's own tree, laid out in Chromium for 17 states at 10 widths
from 40 to 115 cells, light and dark, cuts no word and stays one 18 px row.
Resets and calm words that do not fit go to Details (hotkey d, one label in
both states so the row never shifts), one sentence per window plus how old
the reading is. The design put them in the bar's tooltip too, which needs an
interactive Svg, a sandboxed frame; Chromium paints such a frame an opaque
white where the embedding page's color scheme and the frame's differ (a dark
app on a light system), found in review. So the bars are images, which are
always transparent. Text uses theme keys only; each SVG carries its own small
style with a dark-scheme block, and its base colors read on both
backgrounds, since an image follows the system's scheme, not the app's. The words and sentences come from the CLI (`glidepath/band.py`),
which also draws the terminal line with text bars (`━` used, `╍` forecast,
`┈` unused; `#`, `=`, `.` in ascii mode), dropping resets, then calm words,
then bar width, then "Week" for "Wk" to fit `display.max_width`.

**D-30a. Run-outs at the reset.** On a user's first day the band read
"Week 17% Out Fri 04:09": the window average, one working day stretched over
the week, ran out 50 minutes before Friday's reset. The band now calls a
run-out within 10 minutes (5 hours) or 6 hours (week) of the reset on pace,
and a week paced from the window average while more than 75% of its expected
spend is still ahead is a rough estimate ("May run out", in yellow, its
forecast fading). The verdicts, `check` and the classic line are unchanged.

**D-31. Freshness.** The band lagged Claude Code's own usage bar by a point
(36 against 37) because a reading is only as new as the last API response of
some local chat, and other chats' readings reached a band only at its next
60 s refresh. 3 cheap changes, none of them reading a token: percents are
rounded down, as Claude Code's `/usage` shows them (rounding to nearest showed
the same or 1 more on the same data, which hid real lag and invented false
gaps); the mod polls every 5 s and runs Python only when its session's
`rateLimits` changed (any amount; `session.measure` waits for a whole point) or
`GLIDEPATH_HOME/changed` did (the CLI touches it when a reading changes what is
shown: a new level, window or reset, never a heartbeat or a rejected window,
which kept 2 chats waking each other every 5 s, found in review; another
chat's reading arrives within seconds, and a run ignores its own touch by the
time written in it); a failing run is retried by the 60 s refresh, not the
poll; and the 5-hour percent was nowcast: the last
level plus the fitted rate times the time since that level was read, capped at
1 point, shown as "≈37%". A simulation of 2 chats on one store put glidepath
below the native bar 5% of the time in steady work and above it at most 1%.
On a user's Windows machine it read 15% against the account's 14% when
work had paused, which read as wrong, so it is gone (D-33). Exact agreement
needs the usage endpoint, which is left out (D-04).

**D-32. Where the band can show.** A mod draws only where the app attaches a
surface to the session. Tested on a user's Mac (desktop app 2.42304.0,
Claude Code 2.1.302): a local session in the Code tab draws (`desktop`), and so
does the terminal; a cloud session viewed in the same app loads the mod, and
the mod reads the rate limits, but `$.session.surfaces()` is empty and
`$.ui.open` answers "no-surface", so nothing it draws (band, status, toast,
pane) is shown; regular chats run no Claude Code at all. The mod now logs, to
`GLIDEPATH_HOME/mod.log`, the surfaces each session draws on and whether one asked
for the band, and `glidepath doctor` prints the end of it. For everything outside
Claude Code, `glidepath menubar` prints the band as a SwiftBar plugin for the macOS
menu bar: read only, from the store, refreshed every minute; its readings are
as old as the last Claude Code session's on that Mac, which the title (grey
after 30 minutes) and the menu say. With `menubar.fetch_usage_min` above 0
(now `fetch_usage_min`, D-33; `glidepath menubar --fetch 10`, off by default) the menu bar runs
`claude -p "/usage"`, Claude Code's own usage command, at most that often and
records the levels it prints (whole percents, resets read in the IANA zone it
names, so Python 3.9 or later) as readings of a session "usage-cmd": account
levels whatever ran them, with no credential in glidepath's hands (D-04 still
holds: Claude Code makes the request). On a user's Mac it took about 6 s.
That it spends no quota is inferred (a local command), and is to be watched.
The menu bar also posts a macOS notification (osascript) for the events
worth acting on, once per window instance and kind: runs out (red, not the
milder "may"), reached, will go unused; never from readings over 30 minutes
old. Push to a phone (ntfy, as ccpace does) is left out: the numbers would pass
through a third-party service.

**D-33. Multipliers back, and readings from everywhere.** A user's
feedback after a day on Windows: the band had become only percents, and the
multipliers ("how much faster I can go, how much slower I must go") were the
most useful part. Now the grey "Can go 2.7x faster" shows at every width but
the narrowest, short ("2.7x faster", "~2x faster") below the halves, and for
one window only with the small bars; the slow side has a number too: "Go 1.4x
slower" for a pace above a target (1 / the multiplier, halves for an early
estimate, under 1.1x or 1.5x it stays "Ease off", from 10x "Pause until the
reset") and a run-out says it after the time: "Runs out 15:40, go 2x slower"
(from 80 cells when it is the only window that needs you; the reset then goes
to Details in the halves, which start at 112 cells now). Words sit a little
further from the percent, and in the menu bar 2 em spaces separate the words
from the reset. Laid out in Chromium for 19 states at 19 widths from 40 to
140 cells, light and dark: no word cut. Accuracy: the nowcast is gone (D-31),
so a percent is always a reading. The remaining gaps are readings that have
not arrived: work in claude.ai chats, Cowork, cloud sessions or on another
computer reaches a machine only through `/usage`. So the fetch of D-32 is no
longer the menu bar's alone: `fetch_usage_min` (top level now; the old
`menubar.fetch_usage_min` is still read) also lets a Desktop band, on any
system, start `glidepath fetch` in the background when one is due (never the
terminal status line, which must stay fast), detached and without a console
window on Windows, with the session's markers (`CLAUDECODE`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`) removed so the nested
`claude` neither refuses nor joins the session; one attempt per interval
across all bands and the menu bar (the store's `usage_cmd_tried_at`), and
`doctor` shows when it last answered or why not. On a user's Windows
machine, signed in with Max, `claude -p "/usage"` printed only the session's
spend until the folder it ran in was trusted (in a trusted one it printed the
limits): the fetch runs in `GLIDEPATH_HOME`, which is trusted once by hand, and
its message says so. Claude Code 2.1.303 also writes the reset as "Oct 19,
2:59pm" where 2.1.302 wrote "Oct 19 at 4:59pm"; both are read, and a line
that does not read is named. And after a 5-hour window
ends, the band read "waiting" until the first message of the day; it now
reads "5h 0% Not started", since the next window opens only with that
message.

**D-34. One pace for both windows; Details as a table.** On Windows the
band read "5h 28% 59% will go unused · Week 31% 1.4x faster": 2 kinds of
words for 2 windows, and "59%" beside "28%" read as what is left now. The
person prefers the multiplier, in color. Now each window says its pace, the
same kind for both: "1.4x faster" (green), "On pace" (grey), "2x slower"
(red; yellow while rough or mild), "Back at 16:30" when reached. Quota that
will go unused and the run-out time moved to Details, which was 2 sentences
run together and, on narrow bands, gone (a long phrase took the button's
room). Details is now a table, a column per window: Used, Pace, At reset,
Will go unused, Runs out, Resets, Rate, from the CLI (`band.details`), and
the menu bar's menu shows the same rows under each bar (Menlo columns), so
the Mac and Windows read alike (each window's line, its bar then its name
and percent, spans the menu, a divider between the 2 sections: as wide as
the widest line, from 6.7 points a Menlo character, at least 250, at most
380, the bar taking what the label in Menlo 12, one length for both, leaves;
the plugin hides SwiftBar's own submenu, whose arrow kept a column of about
40 points empty at the right of every line, and its "Updated", which
repeats ours; Option-click shows them; SwiftBar reads those settings only
when it starts, so the first try showed no change until a restart; the
label is short, as the title has it, "5h 83%" and "Wk 46%" (in Menlo, so
each line's bar is as long as its label leaves and both end alike), and the
bar keeps 2 points clear before it, 9 with the image's own gap (7, measured
on a user's Mac); the label keeps the menu's own grey: with a color= on
that line SwiftBar drew the text and dropped the bar; macOS leaves about 10 points after the text of every line, so
the line ends that much short of the separators); the button is always there, "⋯" below 56
cells or when 2 windows need you with the small bars (gone only at 40 cells
with 2 such windows), below 52 cells only the words that need you show, and the table puts one
window under the other below 72 cells (a run-out row, "Sun 22:45, 4 days
5 h early", needs 28 cells). The terminal
line fits by dropping the resets, then grey words, then green ones (the
second window's first); the words that need you stay. Notifications read
the run-out time and the unused quota from fields of their own. A review
(4 reviewers, each finding verified by another) confirmed 13: "Pause", the
worst overshoot, was yellow beside a red "2x slower", and a run-out within
the margin of the reset was red with a plain bar and no Runs out row (red
now means a firm run-out before the reset, yellow everything else that
needs you); a raw or waiting window left its Details column empty and an
older CLI an empty table (rows for them, the sentence for the older CLI);
rows came in the order one window happened to list them (a fixed order
now), with no gutter between the columns; the menu bar title turned green
on hours-old readings (grey now) and the text style lost its green; an ended
5 hours said it opens with the next message while the week blocked it; and
stale words in the docs.

**D-35. Details as cards.** A user found Details' table hard to read
on Windows: rows ran together and it was not clear which label a number
belonged to. 3 designs, drawn from the mod's own elements (Box borders and
padding, Text sizes) in Chromium: a card per window, plain sentences with
bold numbers, and the table banded. The cards read best (the banded table
overlapped its heads at 56 cells): each window a rounded card, its name,
its percent in the heading size and its pace in color on top, then a row
per fact with the label on the left and the value on the right, so each
number stays with its row; side by side from 72 cells, one under the other
below, no word cut from 44 to 115 cells. Used and Pace are the card's head,
not rows. "Next window" became "Opens", short enough for a narrow card.

**D-36. Charts.** A user asked for a chart beside the bars: the history
of the window, the forecast to its reset, and a way to see by eye how good
the model is. From 3 mockups they chose a chart in each Details card and one
under each bar in the menu bar's menu (chart.py builds it once for both):
what was used (the level path of D-11, fresh readings only, linear between
them, from the window's start at 0; not the path's own guess past the last
step), the forecast from now to the reset (the 5 hours a line at the fitted
rate, the week a walk along the hour-of-week profile at its intensity, so
it is slower at night, down to the profile's floor, and a straight line
while the profile is still uniform; both end where `At reset` says, capped
at 100), a fan while the 5-hour rate is still an early estimate (the lines
at that rate 40% lower and 40% higher, each capped at 100, so the forecast
always runs inside it, also where it runs out early), and earlier forecasts
as grey ghosts: an hour ago for the 5 hours, a day and 2 days ago for the
week, each the snapshot closest to that age within a third of it. The
ghosts come from snapshots the status line keeps when it records (`meta`
"forecasts:<window>": the time, the level, the slope and where it lands
uncapped, one per 15 minutes or 2 hours, the last 60, started over with each
window instance), so no forecast's points are stored. The 5 hours' ghost is
its line again; the week's is walked along today's profile (learned as you
go, it moves a little each hour) with its slope set so it lands where that
forecast said, so its end is what was forecast and only its shape is
today's. The forecast takes the pace's color when it needs you (red when it
runs out or a maxed week blocks the 5 hours, yellow when it may run out or
passes a target below 100). A maxed week flattens the 5-hour forecast:
nothing more can be spent until it resets. The grid is local hours (the 5
hours) or the start of each local day (the week), found an hour at a time
so a 23 or 25 hour day and a DST change that skips midnight (the day then
starts at 01:00) need no arithmetic of their own (the first version
computed midnight as 24 hours on and looped for good in the week of a
spring change); a day's name sits at its middle, a day at the week's edge
shorter than 16 hours goes unnamed, and the CLI lists the whole days' names
before the edge days': each name is drawn only where it touches none drawn
before it, so an edge day gives way to a whole day, and every whole day
keeps its name in the menu and in cards from 160 px (a narrower card
leaves out every other one). US peak hours are not drawn: since May 6, 2026 Anthropic no longer
spends Claude Code's 5-hour limits faster in them (they did from March 26,
weekdays 5 to 11 am PT), and for chats it is not said; a band for a rule
that may not apply would mislead. On the Desktop the chart is an SVG image
with no size, so the card's slot sets it; its marks are mid-tones, as the
bars' (D-30), since the image follows the system's scheme and not the
app's. SwiftBar shows pictures, not drawings, and glidepath has no fonts: the
menu's chart is a PNG (png.Canvas: anti-aliased lines, dashes, rings,
fills) as wide as the bar's line, its labels a line of Menlo 10 under it,
each centered under its hour or the middle of its day, kept inside at the
edges; a label that would touch one drawn before it is left out, not moved,
on the Desktop too. The chart's line carries no `color=` (D-34). SwiftBar reads an
unquoted parameter only to the first space, so tooltips are quoted, with no
quote or backslash inside (one SwiftBar version ends a quoted value at
either quote). `"menubar": {"charts": false}` (or the text style) leaves the
charts out of the menu and skips building them. A chart that fails to build
is none; the band stays. A user then asked for day and night on the
week's chart, since the work happens by day: the hours outside the waking
hours (`profile.override`'s, else 8 to 23, the override's own default) are
pale bands behind the lines, found an hour at a time like the days; and for
when the quota would run out at this pace even when the window lasts:
"Runs out" now always shows while there is a pace, in color with how long
early when it comes before the reset (as before), in grey with "past the
reset" when it does not (the 5 hours at the fitted rate, the week walked
along the profile up to 4 weeks on; "over a week away" beyond 7 days).

**D-37. Forecasts with their uncertainty.** Five hours after a weekly reset,
at 2%, a user's band said "Ease off" and "Runs out about Thu 13:30". The
lookback (D-15) reached back past the reset into the old week's last day,
when the quota that would go unused got spent (glidepath itself suggests it),
and that burst set the new week's pace. They asked for a forecast whose
spread is wide far from the reset and narrows toward it. Now:

- The week's lookback stops at the window's start: what was spent before a
  reset never counts for the week after it. The previous-window walk, its
  capped-hours rule and its pick among drop windows are gone with it.
- Intensity = (spent in the lookback + K × target × prior) / (target ×
  (the lookback's profile share + K)), K = 0.1 of a week: the evidence
  pulled toward the prior: what closed weeks reached (the typical week), or
  the profile's scale once most of the week's 168 hours were observed (the
  scale before that stretches the busy hours over the whole week: on day one
  it read 3.5 times the target), else 1 (the target). It is the mean of a gamma prior on the
  spend rate updated by what this week spent: right after a reset the prior
  leads, and its pull fades as the week brings evidence (to 30 to 40% with a
  full 24 awake hours of it). A cut lookback with no step yet (D-15) uses
  the whole week so far, which started at 0, or nothing while it is still at
  0. "Idle" is a lookback of at least 1% of a week's share that spent
  nothing; it still expects some spend from the prior.
- Spread: the intensity is taken as ±50% × √(K / (K + share)), from 50%
  with no evidence to 27 to 32% with a full lookback; the 5-hour rate as
  ±25%, or ±40% while fewer than `rate_min_samples` readings carry the fit
  (the old fan). Since the spend to come is the rate times what is left of
  the window, the spread of the landing level grows with the time to the
  reset and narrows near it. The chart's fan is that band for both windows.
- "Runs out" is red (alert, a notification) only when it runs out at the
  low edge too (more than 10 minutes or 6 hours early); otherwise yellow,
  "may". An early estimate (the 5 hours on fewer readings than a fit needs,
  the week while the prior weighs more than the evidence) warns only of what
  holds at its low edge: a run-out it escapes there is `shaky`, with no
  "Runs out" and no color (Details notes "maybe 100% …" on the Resets row),
  and so is a run-out of an idle week, which only the prior predicts, and "slow down" becomes "on pace" unless the multiplier at
  the low edge is under the threshold too. 2% in the first hour of a week,
  with nothing known, now reads "On pace" where it read "Ease off".

The 5-hour rate itself is unchanged (the least-squares slope of the last 20
minutes, D-11): its window is short and its reset close, so the spread does
the work. The weights of D-13 still expect a little spend at night (the
floor), so this week's own evidence runs a few percent high for a user who
never works at night.

**D-38. Compact Details.** Details grew past the band's room on both
computers, so it scrolled for a row or two. In the same room: facts that
belong together share a row, the second a quieter note after a dot (a
`note` and `note_tone` on a row; the menu bar appends it in the row's
color): At reset · what goes unused; Resets · when it would reach 100% at
this pace; or, when it runs out first, Runs out · when it resets. A
5-hour card went from 5 rows to 3, a week's from 4 to 2. The chart is 64
px high, not 76. "Updated" shows only once the reading is 5 minutes old,
when it says something. The week's nights, a 9% tint that was all but
invisible on a Windows screen, are now a 6% tint and a thin rounded strip
under the axis at 45%, which reads on any screen.

**D-39. The run-out mark.** On a flat chart a forecast that reaches 100
before the reset just ran along the top, and when it got there was hard to
see. When the band says it runs out (band._out: not within the margin of
the reset, not an early estimate's guess, D-37), the chart carries `out`
and `out_label` (the time as Details says it) and marks it in the pace's
color: a dot on the limit, a dashed line down, the time beside it (to the
right, or the left near the end), and the rest of the window, when nothing
more can be spent, tinted at 9%. The menu bar's picture has the same mark
without the time, which is the "Runs out" row just under it.

**D-24. No hooks into Claude's context.** v1 adds no settings hooks and no
`prompt.*` hooks: the line is for a user, not the model.

## Install

**D-18. install / uninstall.** Dry run by default: prints the interpreter
probe table, a unified diff of `settings.json`, the plugin commands and the
files it would write. `--apply` copies `settings.json` to
`settings.json.glidepath-backup-YYYYmmdd-HHMMSSZ` (UTC), then writes atomically
(tmp + rename, symlinked settings keep the link). Only `statusLine` changes;
the file's indentation, line endings (CRLF too), trailing newline and mode are
kept. What was there before is recorded in `~/.glidepath/install.json`, one
record per absolute settings path, saved *before* the settings file is written
(a crash later still leaves uninstall what it needs). A statusLine changed
since the last install becomes the new original, and so does a command the
user put in glidepath's `--compose` argument, also when there was no statusLine
before glidepath (then without the `refreshInterval` glidepath added; uninstall
restores it, and a repin or `--replace` records it first, also into a record
rebuilt after install.json was lost; without a record it wins over a backup,
at a repin and at uninstall alike, and the `--compose` of a newer backup that
holds glidepath's command, as the one `--replace` takes, wins over an older
backup from before glidepath, or stands in for one, then with the statusLine's
other keys as glidepath ran it); glidepath's own command is never recorded or
restored as the original (a pre-glidepath backup, where a blank one means there
was no statusLine, or `--compose` is used instead). Backups are created
exclusively and never overwritten. `uninstall` asks which file when several
are installed and never creates a missing file. Records are matched by real
path, so a symlinked settings file is one record; a version-1 record with a
relative path is recovered from its backup's name, and one that version 1
already uninstalled is dropped. If the plugin commands fail during uninstall
(claude missing or hanging), the mod record and `runtime.json` are kept and
uninstall exits 1 so a rerun retries (the warning says the mod may still be
installed). `uninstall --no-mod` keeps them too, and the launcher (the kept
mod reads `runtime.json`); they go with the mod. When install.json has no mod
record (the data folder was deleted, or another `GLIDEPATH_HOME`) but the user
settings file lists glidepath's plugin or its marketplace, uninstall rebuilds the
record from that file and says so, then removes the mod as usual (`--no-mod`
keeps it); with no snapshot left, that file is tidied by keys (a key that
holds only glidepath's entry is not the user's). A version-1 mod record names no
user file: its commands edited the default one, so that file's record is kept
through a run that leaves the mod, and goes with the mod. When claude is not
on PATH at install, install prints the plugin commands for the user to run and
still records the mod (as manual, with the user-file snapshot), so uninstall
runs the reverse commands. A mod stays manual until glidepath sees the plugin CLI
write to the user file: the mod listed there at any install or uninstall, or
the file changed by a plugin command glidepath ran (commands that time out leave
it manual). A later install that finds claude on PATH while a manual mod's
user file shows no trace of it takes the user-file snapshot again before
running them, and that snapshot replaces a record kept for the user file.
Whatever the mod record, if claude is missing at uninstall and the user
settings file lists neither the plugin nor its marketplace (the commands never
ran or failed, or the user undid them by hand), there is nothing to undo and
uninstall completes. A user file the plugin CLI never wrote to (a manual mod)
is never rewritten, also when an uninstall reaches it as its own target,
unless the reverse commands write there at uninstall (then it comes back from
its snapshot, or goes if it did not exist). A user file that is not valid JSON
is left alone (it is not "absent" for the snapshot either: its plugin keys
count as the user's). The plugin CLI edits the user settings file, so that
file's record outlives its own uninstall while the mod stays (installed
elsewhere, or its commands failed), and whoever removes the mod brings the
file back from its backup; a user file that only the plugin CLI touched gets a
backup of its own before the first plugin command (also when an older mod
record has none), and that snapshot is the file's baseline whenever it has no
record of its own, as when an uninstall reaches it without one. A later
install into it keeps two baselines: the file as it is then (its own backup),
and, as a fallback, the file before the mod (its kept record's baseline, else
that snapshot). Restore tries them in that order, and deletes the file because
there was none before the mod only when neither backup matches. Every record
of a file, under any name, goes when it is uninstalled, and the user file's
kept record goes with the mod (also when that file is gone). Backups are
ordered by their time and sequence number, never as strings: a name ending in
Z is UTC and sorts after every older one without it, which an older version
named in local time; local time, read in the zone in effect at the time,
orders only those older names among themselves (local time goes back after a
move west or at the fall-back hour, and a newer backup then sorted first,
found in review twice: the second time, an older version's local-time backup
sorted after a newer Z one). `uninstall` restores the original `statusLine`
(or removes it), removes `enabledPlugins` / `extraKnownMarketplaces` if they
were not the user's (absent before, or holding only glidepath's own entry because
the commands were run by hand; a key that glidepath next finds gone, as after the
user deleted or blanked the file, stops counting as theirs, and so does one
holding only glidepath's entry for a manual mod whose commands the user ran,
unless the key was empty at the mod's snapshot. A user's own empty key with
the mod in keeps counting, also after the user added and removed other
plugins, and also when the user ran the printed commands for a manual mod: the
plugin CLI leaves `{}` after removing its last plugin, so many files hold one,
and uninstall used to remove it, found in review 3 times. When the user
deletes or blanks the user file and then runs the printed commands by hand
(after glidepath ran them, or for a manual mod whose snapshot held the key
empty), glidepath cannot tell those keys from the user's own, and they may stay
empty) and the plugin CLI left them empty, and when the result equals the
backup (a blank backup counts as `{}`, as on install) it writes the backup's
bytes, so an untouched file comes back byte for byte (the test suite checks
four formatting styles and blank files). The mod is installed with
`claude plugin marketplace add <repo>` and
`claude plugin install glidepath@glidepath` (user scope); `--no-mod` skips it.
`--bin-dir ~/.local/bin` adds a `glidepath` launcher with the pinned interpreter.
A later install rewrites it with the new pin without `--bin-dir` (a `--python`
repin, a moved clone), unless the user deleted it. A launcher written in
another `--bin-dir` replaces it: the earlier one is removed while it is still
glidepath's, so uninstall removes the only one there is (found in review). A
`--bin-dir` that names the same directory another way (another case on a
case-insensitive macOS disk, a bind mount) holds the same file, which stays:
install used to remove the launcher it had just written (found in review). On
Windows the launcher is `glidepath.cmd`, with an sh `glidepath` beside it for Git
Bash, and settings writes take care of held and read-only files (D-27).

**D-40. The name: glidepath.** The first public release was called pacer.
A look at the tools around it found four others with that name or nearly
(two of them with a `pacer` command, one already in the list we meant to
join), in a niche of a few thousand repositories. A glide path is the
line an aircraft follows to touch down where it means to; the forecast is
that line for a limit, drawn to the reset. Earlier names keep working
until `install --apply` moves them (legacy.OLD, newest first): the data
folder, the variable that names it, the mod and its marketplace,
SwiftBar's plugin, the statusLine command and its backups, and a launcher
in bin/ for each.

## Windows

**D-25. Windows: which shell, and one command for both.** Claude Code runs the
statusLine through Git Bash (`bash.exe -c`) when it finds one:
`CLAUDE_CODE_GIT_BASH_PATH` (from its environment or settings.json `env`, when
it names an existing bash or sh), `C:\Program Files\Git\bin\bash.exe`, the
`(x86)` one, then the `git` on PATH (never one in the current folder) and
`..\..\bin\bash.exe` from it. Otherwise it runs it through PowerShell (pwsh on
PATH or in its usual installs, then Windows PowerShell 5.1) with `-NoProfile
-NonInteractive -ExecutionPolicy Bypass -Command`. The statusLine setting has
no `shell` or `args` field, and the shell changes when Git for Windows comes or
goes. So `glidepath/winshell.py` repeats that search (`predict_shell`,
settings.json `env` over the process environment), and install writes, when it
can, a command both shells read alike: bare words (ASCII letters, digits and
`_./:+=-`, non-ASCII letters and digits, a `~` after an ASCII letter or digit),
which neither shell changes, and words in single quotes with no quote inside,
which both keep literal, except as the first word. Paths use forward slashes:
Git Bash drops an unquoted backslash, and Windows reads both. An interpreter or
glidepath path that would need quotes (a space) is written with its folders' 8.3
short names when those need none (`C:/PROGRA~1/PYTHON~1/python.exe`; the file
name is kept, so the command still names glidepath), while `runtime.json`, the
launchers and install's report keep the long paths. When there is no such form
(a volume without short names, or a short form that still needs quotes), the
command is written for the predicted shell: single quotes for bash, or `&
'...'` with a quote doubled for PowerShell, where a quoted first word is a
string, not a command. No bash reads that `&`, so the command says which shell
it is for (`winshell.written_for`), and the install record keeps the shell too.
install warns that such a command runs in that shell only, and `doctor` names
the shell Claude Code would pick now and warns when the command was written for
the other one; a command both shells read alike never warns. A Git Bash
terminal puts its own `git` first on PATH, so the prediction there can differ
from what Claude Code finds when started from the Desktop app or PowerShell:
install and doctor run there say so, and point to `glidepath doctor` from
PowerShell rather than to a reinstall. Every Python command glidepath writes (the
statusLine, `runtime.json`'s `args`, the launchers) adds `-X utf8` on Windows:
a pipe gets the ANSI code page there, and `-I` ignores `PYTHONUTF8` and
`PYTHONIOENCODING`.

**D-26. Windows: the composed command.** Windows PowerShell 5.1 and Git Bash
can both hand the old command to python.exe changed. Windows PowerShell 5.1
wraps an argument that holds whitespace in double quotes and escapes nothing,
so a `"` inside, or a backslash ending such an argument, arrives altered (pwsh
7.3+ escapes both). Git Bash's MSYS2 runtime converts what looks like a POSIX
path in the text before the first whitespace, when a `/` or `\` comes before it
and the text does not start with `~`: a first word that starts with `/` (also
after a leading quote or `$(`), or holds a `=` or a colon other than a drive's
(`FOO=/x`, `ab:/c`). `/bin/bash ~/.claude/statusline.sh` reached glidepath as
`C:/Program Files/Git/usr/bin/bash ...` and broke at the space. Such a command
(`winshell.needs_encoding`) is written as `--compose-b64 b<token>`, the
URL-safe base64 of its UTF-8 without padding: only letters, digits, `_` and
`-`, which every shell passes on as is, and the `b` keeps it from reading as an
option or a number. Any other keeps `--compose '<command>'`, and macOS and
Linux never encode. This departs from D-17's "settings.json shows exactly what
runs" for those commands only: a readable form that the shell corrupts drops
the user's line without a word, while the encoded one runs the same in Git
Bash, pwsh and powershell.exe. To keep it in sight, install says when it
encoded one and why, `doctor` shows it decoded, and a repin, `--replace`,
uninstall's restore and the backup scan read it decoded (a repin of the same
command changes nothing). A malformed token means no composed command (the
config's `compose.command` applies if set), never an error, and with both flags
the last one wins, in install as in `glidepath statusline`: `compose_of` reads the
words after `statusline` with the statusline's own parser, so `doctor`, a repin
and uninstall keep the command that runs. Install read the first one, so after
a hand edit that added a flag, a repin wrote the old command back in place of
the running one (found in review). A user may put `--compose '<command>'` back
by hand; the next install encodes it again if need be. An older install's plain
form that is at risk gets a `doctor` warning until `glidepath install --apply`
rewrites it.

The Composer (D-17) runs the old command in the shell that started glidepath, read
from the parent process's image through kernel32 (`winshell.parent_shell`),
since Claude Code ran the statusLine there. Repeating Claude Code's search went
wrong inside Git Bash, whose PATH puts its own `git` first: with Git installed
outside Program Files, a bash statusLine ran in PowerShell. Git Bash's
`bin\bash.exe` runs `usr\bin\bash.exe`, the parent glidepath sees, so that one
maps back to `bin\bash.exe` when it exists. Any other parent (a
`CLAUDE_CODE_SHELL_PREFIX` wrapper, or a venv's `python.exe` launcher, which
install no longer pins: D-27) falls back to `predict_shell`. As Claude Code
runs a statusLine, bash gets its own folder first on PATH and `bash ` before a
first word ending in `.sh`, and PowerShell gets Claude Code's flags (no
`-ExecutionPolicy Bypass` under
`CLAUDE_CODE_POWERSHELL_RESPECT_EXECUTION_POLICY`) and its
`${CLAUDE_PROJECT_DIR}` style placeholders as `${env:...}`. A nested PowerShell
took about 0.3 s warm and close to 1 s cold on CI runners, and a composed
`echo mine` in Windows PowerShell 5.1 once missed the 1.5 s timeout there, so
`compose.timeout_ms` defaults to 3000 on Windows (`config.compose_timeout_ms`);
the README suggests 5000 for a slow script.

**D-27. Windows: install.** Interpreters: `--python`, else the PEP 514 registry
(HKCU, then HKLM in its 64-bit and 32-bit views; PythonCore first, newest
first; `ExecutablePath`, else `python.exe` in the key's folder), `py -0p`, the
python.org and install manager folders, the interpreter running install, then
`python` and `python3` on PATH. PATH comes last because the python.org
installer leaves it alone by default and the Desktop app does not see a shell's
PATH. `os.access(X_OK)` is true for any file on Windows, so a candidate must be
a `.exe` with a drive or a UNC share, and it runs only with arguments (`-I -S
-c`, 15 s timeout, `PYTHON_MANAGER_AUTOMATIC_INSTALL=0` so the install manager
installs nothing on the way): the Store's `python.exe` stub opens the Store
when run bare, and fails with arguments. The probe reports `sys.executable`,
and behind a Store alias, `py.exe` or another launcher that real `python.exe`
is pinned, so no render pays for the extra process. On Windows it reports the
base interpreter (`sys._base_executable`), so a venv's `python.exe`, a launcher
that starts its base `python.exe` as a child process, is pinned as that base:
glidepath runs `-I -S`, so the venv adds nothing but the process, and that process
was the parent `winshell.parent_shell` saw instead of the shell (found in
review). install's list says `via <path>` for each such candidate. macOS and
Linux pin a venv's interpreter as given. A Store Python, which writes under
AppData to a private copy, is used only when nothing else passes.

Launchers: `--bin-dir` writes `glidepath.cmd`, which cmd.exe and PowerShell find
through PATHEXT, and an extensionless sh `glidepath` (LF, UTF-8, forward slashes,
`exec` of the pinned interpreter), as npm's cmd-shim does: Git Bash, where
Claude Code's Bash tool runs, looks a command up by its very name, never with
`.cmd`, so `glidepath` was "command not found" there. install.json records it as
`sh_launcher`. It follows `glidepath.cmd` (D-18): it is written beside it, moved
and removed with it, and a file of that name that is not glidepath's is never
written over or removed, nor recorded. Each launcher follows the pin on its
own: one the user deleted stays deleted until a `--bin-dir`, however many
repins come first (install.json keeps the sh launcher's record for that; the
2nd repin used to write it back), and one the user kept after deleting
`glidepath.cmd` (or putting a file of their own there) is still glidepath's, so a
repin rewrites it where it is rather than leave Git Bash's `glidepath` running an
interpreter that may be gone (both found in review). The `glidepath` an older
version wrote beside `glidepath.cmd` is rewritten as the sh launcher. `glidepath.cmd`
is CRLF, in the console code page, with `%` doubled; its last line is `(goto)
2>nul & "<python>" -I -S -X utf8 "<entry>" %*`. cmd.exe reads a batch file a
line at a time and opens it again after each command, so an install or
uninstall run through it, which rewrites or deletes the file, ended with "The
batch file cannot be found." while an `exit /b` line followed python, and a
repin to a path of another length ran the middle of the new text. The `(goto)`
ends the batch before python runs (`%*` is expanded already), so cmd.exe reads
nothing after it and python's exit code is the caller's. The one install that
replaces an older launcher, run through that launcher, can still show the
message once. Neither launcher runs the statusLine or the mod: Bun will not
start a `.cmd` without a shell, and cmd.exe parses its arguments again.

`claude` is `claude.exe` (native, WinGet) or `claude.cmd` (npm), never npm's
extensionless sh shim. Its output and the probe's are read as UTF-8 whatever
the locale, and a timeout kills the whole tree (`taskkill /T /F`), since
killing `claude.cmd` alone leaves node holding the pipes. Whether `claude
plugin marketplace add` takes a drive path on Windows is unconfirmed, so when
it is rejected, install runs it again as `./` from the repo, a form the parser
takes on every OS.

Settings writes: `os.replace` is retried for about 2 s while another process
holds the target (Claude Code reading settings.json, an antivirus scan).
Windows refuses to replace or delete a read-only file (`attrib +R`), which
POSIX replaces like any other, and install and uninstall failed with a
traceback there. They now clear the attribute for the move or the removal and
put it back if that fails; the new file takes the old one's mode, so it stays
read-only, and a backup copies the mode, as on every OS. A UTF-8 BOM in
settings.json (Windows PowerShell 5.1 and old Notepad write one) is kept, and
install.json, the backups and `runtime.json` are read with one.

**D-28. Windows: `GLIDEPATH_HOME`.** The CLI (`config.windows_home`) and the mod
(`resolveHome`) read it alike. `~` is USERPROFILE (else HOMEDRIVE and
HOMEPATH), as Python reads it, never HOME, which Git Bash sets and PowerShell
does not. `C:\x`, `C:/x`, `\\server\share\x` and `//server/share/x` are
absolute, and Git Bash's `/c/x` and `/c` are `C:/x` and `C:\`. A path rooted
with a backslash and no drive (`\x`) takes the home's drive, as `ntpath.join`
does, and anything else is taken from the home directory; the result is
normalized. `%VAR%` is not expanded, as Claude Code does not expand it either.
Any other path that starts with a single `/` (`/srv/glidepath`) is refused, with
the same message in both, whose example is the same path on the home's drive
(`D:/srv/glidepath` for a profile on `D:`), the folder older versions used: Git
Bash, which runs the terminal statusLine, rewrites such a value to a folder
under its own install (`C:/Program Files/Git/srv/glidepath`) before python.exe
sees it, while PowerShell passes it as given and the mod read it on the home's
drive, so the line and the band used different folders (and the first one,
under Program Files, is one a user cannot create). The statusline still prints
its bare line and logs nothing (the log lives in the home), the other commands
print the reason and exit 1, `doctor` fails its `GLIDEPATH_HOME` row, and install
and uninstall refuse before writing anything. Under Git Bash the statusLine's
Python only sees the rewritten folder, so install and doctor also check the raw
value in the settings env. `C:x` (relative to that drive's current folder,
which differs from one process to the next: write `C:\x`) is refused the same
way, with the same message in both. The mod refused it while the CLI took it
from the home directory, so the band and the line could use different folders
(found in review). Since `$.fs.read` refuses most network locations, a UNC home
the mod cannot read says so and asks for a local folder.

**D-29. Windows: text, time and long paths.** The statusline writes UTF-8 bytes
(D-16). The other commands print through Python's text streams, and Windows
gives a pipe or a file the ANSI code page, where cp1252 has no ▲ → ░: `now`,
`profile` and `doctor` raised UnicodeEncodeError when piped. So the CLI
switches a stream that is not UTF-8 to UTF-8 (a console already is) and turns
on the console's VT processing for colors. `config.json` is read as bytes, so
JSON's own detection takes UTF-8 with or without a BOM and UTF-16 or UTF-32
with one (Windows PowerShell 5.1's `>` and `Out-File` write UTF-16); a file
that does not parse is still ignored as a whole, and `glidepath config show` says
why. Windows' C runtime reads `TZ` whenever it is set, in its own form only
(`EST5EDT`, `UTC0`): an inherited IANA name (a Cygwin login shell exports
`TZ=Europe/Berlin`) read as zone "Eur" at offset 0 with US daylight saving, and
every local time glidepath showed was off. `bin/glidepath` and `python -m glidepath`
drop such a `TZ` before anything reads local time and have the C runtime read
the zone again (`ucrtbase._tzset`), so times follow the Windows time zone; a
`TZ` in the C runtime's form is still honored. Transcripts deeper than 260
characters were skipped without a word when long paths are not enabled (the
Windows default), so the bootstrap walks from an extended-length root
(`\\?\C:\...`); files and folders it cannot open are counted, and `glidepath
bootstrap` says how many. `errors.log` rotates as on POSIX, but a rename that
fails while another program holds the log without delete sharing no longer
drops the entry being written; a later error rotates it.

## The scavenger hook

**D-20. `glidepath check`.** Exit 0 and `"ok": true` when spare quota can be
harvested now, 1 otherwise, JSON on stdout either way. A 5h window is
harvestable when it is in its tail (`--tail 5h=60m`), its projected unused
below its target (max(0, target - projected), `spare` in the JSON) is at least
`--min-free` (default 10) and at least that much is free below the target now
(target - used), and the weekly window would not run dry or exceed its target
if that spare were spent (priced with the exchange rate; while that is
unknown, at its clamp maximum of 50 weekly points per 5h window, and the JSON
says `"exchange_rate_assumed": true`, only when a positive 5h leftover was
priced that way. A leftover of 0 costs 0 at any rate, and the reason is then
"the weekly budget is already tight", without a price; it said "priced at 50"
next to nothing left, found in review). The weekly window is harvestable when
it is in its tail (`week=2d`) with at least `min-free` projected to expire
below its target and the 5h window has room below its own target, both now and
at the current burn rate (projected unused >= `min-free`): a scavenger must
not starve interactive work. What is spare is measured against each window's
target, not 100, as for green (D-16); at target 100 nothing changes. A window
at or above a target below 100, or with a slow down verdict, is never
harvestable, and the reasons say so ("5h window is at or above its 90%
target", "weekly window is spending faster than its 85% target allows (slow
down)"). check used to say go on the reserve that a target below 100 keeps
while the line was yellow, and one JSON both refused a 5h leftover as "already
tight" and said go on the week's surplus (found in review). `projected_unused`
and `free_now` stay out of 100; each window also reports its `target` and
`spare`, and a go reason adds "below its N% target" when the target is below
100; targets print as set (99.5 printed as "100%", which made "below its 100%
target", found in review). While the 5h burn rate is not known yet (D-11), the
5h leftover is unknown: `projected_unused` and the weekly cost are null,
nothing is priced (`exchange_rate_assumed` is false), the reason is "5h burn
rate not known yet" (beside "already tight" when the week is tight on its own,
without a price), and neither the 5h harvest nor the weekly harvest's 5h room
test passes, whatever `--min-free` (0 included: the unknown leftover was read
as 0, and 0 >= 0 passed it and crashed `check`), so the weekly harvest also
pauses in the first 5 minutes of each 5h window. A leftover or free room
counts only when it is at least `--min-free` and more than 0 (at 0, a maxed 5h
window had "room"). `--min-free` is clamped to 0 to 100, like `min_free` in
the config; nan, inf or a negative duration is an error (JSON with `"error"`,
exit 1). Data older than `--max-age` (30 min) is a no-go. Age is measured from
the newest reading that carries information (FRESH, FIRST or ANOMALY), never
from heartbeats: an open idle session re-renders an old reading every minute,
while headless runs and claude.ai spend without rendering a statusLine.
`glidepath now` reports the same age (`data_age_s` in `--json`) with the last
heartbeat beside it; `doctor` keeps its per-source liveness on all samples,
since a heartbeat proves the statusLine runs. A scavenger has to refresh the
data itself (v2).

## Retention

**D-22.** Samples older than `retention_days` (365) and closed windows older
than that are deleted once a day by the statusline; per-session reader rows
after 7 days.

## Ideas for later

- An 80% band on both projections (claumon's empirical Bayes with a Gamma
  process), with glidepath's profile as the prior mean instead of one global
  rate.
- The scavenger: a queue of jobs run with `claude -p` when `glidepath check` says
  go, which records rate limits from the SDK's usage reports itself.
- Mean reversion in the weekly projection (recent intensity decaying toward
  the typical week over the remaining days).
- A `report` breakdown by project (from transcript token counts).
