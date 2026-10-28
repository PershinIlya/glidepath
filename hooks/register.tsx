// glidepath band: one quiet row above the prompt, for the Desktop Code tab where
// no statusLine renders. Each window is a dim name, a small bar (used, then a
// forecast to the reset), the percent and, only when there is something to
// say, a few words; a reset when the band is wide. Details (d) adds a
// sentence per window.
//
// Thin on purpose: all the math and all the words come from the Python CLI.
// The mod hands it the session's rate limits in the statusLine payload shape
// and draws what it prints. It is also the collector where no statusLine
// runs: each call records a sample (source "mod").

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit, Timer } from 'claude-code'

import type { Chart, Detail, Phrase, View, Win } from '../types'

const view = atom({ plugin: 'glidepath', key: 'view' } as const, null)
const details = atom({ plugin: 'glidepath', key: 'details' } as const, false)

const RUN_TIMEOUT_MS = 10_000
const REFRESH_MS = 60_000
// How often the mod looks for a new reading: this session's own, or another
// chat's (the CLI touches GLIDEPATH_HOME/changed). Both are cheap; Python runs
// only when one moved.
const POLL_MS = 5_000
// The interpreter flags of a runtime.json written before it listed them.
const DEFAULT_ARGS: readonly string[] = ['-I', '-S']
// With no `glidepath install` here (the plugin alone): the CLI shipped beside
// this module, run by the first of these that answers its --version. The
// Desktop app may start with a short PATH, so the usual places on a Mac are
// asked by name too. Flags as `glidepath install` pins them (winshell).
const PROBES: Record<'posix' | 'windows', readonly (readonly string[])[]> = {
  posix: [['python3'], ['/opt/homebrew/bin/python3'], ['/usr/local/bin/python3'], ['/usr/bin/python3'], ['python']],
  windows: [['py', '-3'], ['python'], ['python3']],
}
const PROBE_ARGS: Record<'posix' | 'windows', readonly string[]> = {
  posix: DEFAULT_ARGS,
  windows: ['-I', '-S', '-X', 'utf8'],
}
const PROBE_TIMEOUT_MS = 15_000
export const NO_PYTHON =
  'needs Python 3.8 or newer and found none: install it (python.org; on Windows ' +
  '`winget install Python.Python.3.12`), then run /reload-plugins'

// pinned: from runtime.json (`glidepath install`), else found by asking (PROBES).
type Runtime = {
  python: string
  args: readonly string[]
  entry: string
  home: string
  windows: boolean
  pinned: boolean
}

// Desktop paints theme keys; the terminal maps them to its palette. Tones of
// the classic line, then the bars line's words and bar parts.
const TONE: Record<string, { dimColor?: boolean; color?: string }> = {
  dim: { dimColor: true },
  plain: {},
  opportunity: { color: 'success' },
  warn: { color: 'warning' },
  alert: { color: 'error' },
  text: {},
  name: { dimColor: true },
  inactive: { dimColor: true },
  success: { color: 'success' },
  warning: { color: 'warning' },
  error: { color: 'error' },
  used: { color: 'claude' },
  forecast: { dimColor: true },
  track: { dimColor: true },
  idle: { dimColor: true },
  over: { color: 'error' },
  spare: { color: 'success' },
}

// The statusLine stdin shape: windows keyed by kind, resets_at in epoch seconds.
export function toPayload(sessionId: string, limits: readonly SessionRateLimit[]) {
  const rateLimits: Record<string, { used_percentage: number; resets_at?: number }> = {}
  for (const limit of limits) {
    const resetsAt = limit.resetsAt ? Date.parse(limit.resetsAt) : Number.NaN
    rateLimits[limit.kind] = {
      used_percentage: limit.percentUsed,
      ...(Number.isFinite(resetsAt) ? { resets_at: Math.floor(resetsAt / 1000) } : {}),
    }
  }
  return {
    session_id: sessionId,
    ...(limits.length > 0 ? { rate_limits: rateLimits } : {}),
    kinds: limits.map((limit) => limit.kind),
  }
}

// What a refresh compares: the readings, not the objects that carry them.
function limitsKey(limits: readonly SessionRateLimit[]): string {
  return JSON.stringify(limits.map((limit) => [limit.kind, limit.percentUsed, limit.resetsAt ?? null]))
}

// Module state: a reload starts it over, which is what we want.
let runtime: Runtime | null = null
let running = false
let timer: Timer | null = null
let periodMs = REFRESH_MS
// A refresh asked for while one runs: the newest limits win, null = re-read.
let queued: readonly SessionRateLimit[] | null | undefined
// What the last run saw: this session's limits, and the stamp's time.
let seenLimits: string | null = null
let seenStamp: number | null = null

// A short log for `glidepath doctor` (GLIDEPATH_HOME/mod.log): which surfaces the
// session draws on, and whether one asked for the band. Written on those
// events only, never on a timer; the last 40 lines.
let logged: string[] = []
const seenRenders = new Set<string>()
async function surfacesOf($: EngineInterface): Promise<string> {
  const list = await $.session.surfaces().catch(() => null)
  return list === null ? '?' : `[${list.join(', ')}]`
}

async function trace($: EngineInterface, line: string) {
  try {
    const rt = await loadRuntime($)
    const id = (await $.session.id()).slice(0, 8)
    const at = new Date(await $.clock.now()).toISOString()
    logged = [...logged.slice(-39), `${at} ${id} ${line}`]
    await $.fs.write(join(rt.home, 'mod.log', rt.windows), `${logged.join('\n')}\n`)
  } catch {
    // no runtime yet: the band says why
  }
}

// The user's home as Python's os.path.expanduser finds it, and whether
// Windows path rules apply.
export type UserHome = { windows: boolean; home: string | undefined }

// No $ API names the OS; Windows sets OS=Windows_NT system-wide. There Python
// ignores HOME (Git Bash sets one of its own) and takes USERPROFILE, else
// HOMEDRIVE+HOMEPATH; an empty value counts as unset.
async function userHome($: EngineInterface): Promise<UserHome> {
  if ((await $.env.get('OS')) !== 'Windows_NT') return { windows: false, home: await $.env.get('HOME') }
  const profile = await $.env.get('USERPROFILE')
  if (profile) return { windows: true, home: profile }
  const homePath = await $.env.get('HOMEPATH')
  return { windows: true, home: homePath ? `${(await $.env.get('HOMEDRIVE')) ?? ''}${homePath}` : undefined }
}

// One separator between the two, as os.path.join puts it.
function join(base: string, name: string, windows: boolean): string {
  const ends = windows ? /[\\/]$/ : /\/$/
  return ends.test(base) ? `${base}${name}` : `${base}${windows ? '\\' : '/'}${name}`
}

// A UNC path (\\server\share): $.fs.read refuses most of them.
function isNetwork(path: string, windows: boolean): boolean {
  return windows && /^[\\/]{2}/.test(path)
}

// The drive of a Windows path as ntpath.splitdrive reads it: `C:`, or
// `\\server\share`; '' when it has none.
function driveOf(path: string | undefined): string {
  return path?.match(/^(?:[A-Za-z]:|[\\/]{2}[^\\/]+[\\/][^\\/]+)/)?.[0] ?? ''
}

// Git Bash's spelling of a drive path: `/c/Users/x` is `C:/Users/x`, and `/d`
// is `D:/` (a slash, one ASCII letter, then a slash or the end), as the CLI's
// config.windows_home reads it. Anything else is returned as given.
export function fromGitBash(path: string): string {
  const drive = /^\/([A-Za-z])(?:\/|$)/.exec(path)?.[1]
  return drive ? `${drive.toUpperCase()}:/${path.slice(3)}` : path
}

// The same rules as the CLI: an empty GLIDEPATH_HOME counts as unset, a leading
// `~` is the user's home, and a relative path is taken from it, never from the
// session's cwd (Claude Code copies settings.json env values as written). On
// Windows `~\` works like `~/`, Git Bash's `/c/Users/x` is `C:/Users/x`,
// `C:\`, `C:/` and `\\server\share` are absolute, a path rooted with a
// backslash and no drive (`\x`) takes the drive of the user's home, as
// Python's ntpath.join does, and `C:x` (relative to that drive's current
// folder) is refused. So is any other path rooted with a slash and no drive
// (`/x`): Git Bash, which runs the statusLine, rewrites it to a folder under
// its own install before Python sees it, so the band and the line would use
// different folders. Its message names `/x` on the home's drive, the folder
// older versions used. The CLI refuses both with the same messages
// (config.windows_home).
export function resolveHome(value: string | undefined, user: UserHome): string {
  const { windows, home } = user
  const given = windows && value ? fromGitBash(value) : value
  if (given && (windows ? /^(?:[A-Za-z]:[\\/]|[\\/]{2})/ : /^\//).test(given)) return given
  if (windows && given && /^[A-Za-z]:/.test(given)) {
    throw new Error(`GLIDEPATH_HOME ${given} is relative to a drive's current folder; use an absolute path`)
  }
  if (windows && given?.startsWith('/')) {
    throw new Error(
      `GLIDEPATH_HOME ${given} has no drive; Git Bash rewrites such a path to a folder under its own install. ` +
        `Write it with a drive, e.g. ${driveOf(home) || 'C:'}${given}, or as ~/...`,
    )
  }
  if (windows && given && /^\\/.test(given)) return `${driveOf(home)}${given}`
  const tilde = windows ? /^~(?:[\\/]|$)/ : /^~(?:\/|$)/
  if (given?.startsWith('~') && !tilde.test(given)) {
    const forms = windows ? '~, ~/ and ~\\' : '~ and ~/'
    throw new Error(`GLIDEPATH_HOME ${given}: only ${forms} are expanded; use an absolute path`)
  }
  if (!home) {
    const name = windows ? 'USERPROFILE' : 'HOME'
    throw new Error(`${name} is unset: set GLIDEPATH_HOME to an absolute path, or run \`glidepath install --apply\``)
  }
  if (!given) return join(home, '.glidepath', windows)
  const rest = given.replace(windows ? /^~[\\/]?/ : /^~\/?/, '')
  return rest ? join(home, rest, windows) : home
}

// The CLI shipped with this module: bin/glidepath in the plugin's folder.
function ownEntry($: EngineInterface, windows: boolean): string {
  return join($.plugin.root, windows ? 'bin\\glidepath' : 'bin/glidepath', windows)
}

// The first interpreter that runs `entry --version` as glidepath, or null.
async function findPython($: EngineInterface, entry: string, windows: boolean) {
  const os = windows ? 'windows' : 'posix'
  for (const probe of PROBES[os]) {
    const [python, ...pre] = probe
    const args = [...pre, ...PROBE_ARGS[os]]
    try {
      const result = await $.process.run([python, ...args, entry, '--version'], { timeoutMs: PROBE_TIMEOUT_MS })
      if (result.exitCode === 0 && /^glidepath \d/.test(result.stdout.trim())) return { python, args }
    } catch {
      // cannot start: not there
    }
  }
  return null
}

async function loadRuntime($: EngineInterface): Promise<Runtime> {
  if (runtime !== null) return runtime
  const user = await userHome($)
  const home = resolveHome(await $.env.get('GLIDEPATH_HOME'), user)
  let text: string
  try {
    text = await $.fs.read(join(home, 'runtime.json', user.windows))
  } catch (error) {
    const why = error instanceof Error ? error.message : String(error)
    if (isNetwork(home, user.windows)) {
      throw new Error(`cannot read runtime.json from network path ${home}: set GLIDEPATH_HOME to a local folder (${why})`)
    }
    if (!/ENOENT|no such file|cannot find/i.test(why)) throw error
    // No `glidepath install` here: the CLI beside this module.
    const entry = ownEntry($, user.windows)
    const found = await findPython($, entry, user.windows)
    if (found === null) throw new Error(NO_PYTHON)
    runtime = { ...found, entry, home, windows: user.windows, pinned: false }
    return runtime
  }
  // A byte order mark (a Windows editor's) would make JSON.parse throw.
  const parsed = JSON.parse(text.replace(/^﻿/, '')) as { python?: unknown; args?: unknown; entry?: unknown }
  if (typeof parsed.python !== 'string' || typeof parsed.entry !== 'string') {
    throw new Error('runtime.json has no python/entry; run `glidepath install --apply`')
  }
  const args = parsed.args === undefined ? DEFAULT_ARGS : parsed.args
  if (!Array.isArray(args) || !args.every((arg) => typeof arg === 'string')) {
    throw new Error('runtime.json args is not a list of strings; run `glidepath install --apply`')
  }
  // A clone that is gone: the CLI beside this module, with the pinned Python.
  const entry = (await $.fs.exists(parsed.entry).catch(() => true)) ? parsed.entry : ownEntry($, user.windows)
  runtime = { python: parsed.python, args, entry, home, windows: user.windows, pinned: true }
  return runtime
}

// The stamp the CLI touches when a reading changes what is shown
// (config.stamp_path): its time, and the run time written in it. Absent, 0.
async function stamp($: EngineInterface, rt: Runtime): Promise<{ mtime: number; ts: number }> {
  const path = join(rt.home, 'changed', rt.windows)
  const mtime = await $.fs
    .stat(path)
    .then((s) => s.mtimeMs)
    .catch(() => 0)
  if (mtime === 0) return { mtime, ts: Number.NaN }
  const ts = await $.fs
    .read(path)
    .then((text) => Number.parseFloat(text))
    .catch(() => Number.NaN)
  return { mtime, ts }
}

// The CLI's mod.refresh_s, clamped; re-arms the timer when it changes.
function rearm($: EngineInterface, seconds: unknown) {
  const s = typeof seconds === 'number' && Number.isFinite(seconds) ? seconds : 60
  const ms = Math.min(3600, Math.max(15, s)) * 1000
  if (ms === periodMs || timer === null) return
  timer.cancel()
  periodMs = ms
  timer = $.clock.every(periodMs, () => {
    void refresh($)
  })
}

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)
const isStr = (x: unknown): x is string => typeof x === 'string'

function isPhrase(x: unknown): x is Phrase {
  const p = x as Phrase
  return (
    typeof p === 'object' && p !== null && isStr(p.pre) && isStr(p.num) && isStr(p.post) && isStr(p.narrow) &&
    (p.narrow_pre === undefined || isStr(p.narrow_pre)) && isStr(p.tone) && typeof p.attention === 'boolean' &&
    typeof p.calm === 'boolean'
  )
}

// The order of Details' rows (band.details); a label the mod does not know goes last.
const ORDER = ['Used', 'Pace', 'Limit', 'At reset', 'Will go unused', 'Runs out', 'Opens', 'Resets', 'Rate']
// Rows a card shows in its head: the percent and the pace.
const HEAD = ['Used', 'Pace', 'Limit']
const rank = (label: string) => (ORDER.indexOf(label) + ORDER.length + 1) % (ORDER.length + 1)

const isPoints = (x: unknown): x is [number, number][] =>
  Array.isArray(x) && x.every((p) => Array.isArray(p) && p.length === 2 && isNum(p[0]) && isNum(p[1]))

// A chart from the CLI that draws; anything else, no chart (an older CLI has none).
export function chartOf(x: unknown): Chart | null {
  const c = x as Chart
  const ok =
    typeof c === 'object' && c !== null && isNum(c.t0) && isNum(c.t1) && c.t1 > c.t0 && isNum(c.now) &&
    isPoints(c.actual) && c.actual.length > 0 && isPoints(c.forecast) &&
    (c.fan === null || (Array.isArray(c.fan) && c.fan.length === 2 && c.fan.every((p) => isPoints(p) && p.length > 0))) &&
    Array.isArray(c.ghosts) && c.ghosts.every(isPoints) && Array.isArray(c.grid) && c.grid.every(isNum) &&
    (c.nights === undefined || isPoints(c.nights)) &&
    (c.out == null || (isNum(c.out) && c.out > c.t0 && c.out < c.t1)) && (c.out_label == null || isStr(c.out_label)) &&
    Array.isArray(c.labels) && c.labels.every((l) => Array.isArray(l) && isNum(l[0]) && isStr(l[1]))
  return ok ? c : null
}

const isRow = (r: unknown): r is Detail =>
  typeof r === 'object' && r !== null && isStr((r as Detail).label) && isStr((r as Detail).value) &&
  isStr((r as Detail).tone) && ((r as Detail).note === undefined || isStr((r as Detail).note)) &&
  ((r as Detail).note_tone === undefined || isStr((r as Detail).note_tone))

// The CLI's windows, when every one is whole; else the text line is drawn.
function windowsOf(x: unknown): Win[] | null {
  if (!Array.isArray(x) || x.length !== 2) return null
  const ok = x.every((w: Win) => {
    return (
      typeof w === 'object' && w !== null && (w.id === 'h5' || w.id === 'wk') && isStr(w.name) && isStr(w.short) &&
      isStr(w.kind) && (w.pct === null || isNum(w.pct)) && isNum(w.used) &&
      (w.projected === null || isNum(w.projected)) && (w.reset === null || isStr(w.reset)) &&
      (w.phrase === null || isPhrase(w.phrase)) && isStr(w.sentence)
    )
  })
  if (!ok) return null
  // Details rows from an older CLI, or broken ones, are left out, not the band.
  return (x as Win[]).map((w) => ({
    ...w,
    details: Array.isArray(w.details) ? w.details.filter(isRow) : [],
    chart: chartOf(w.chart),
  }))
}

async function runOnce($: EngineInterface, limits: readonly SessionRateLimit[] | null) {
  const rt = await loadRuntime($)
  const current = limits ?? (await $.session.usage()).rateLimits
  const payload = toPayload(await $.session.id(), current)
  const before = await stamp($, rt)
  // Seen even when the run fails: the poll must not retry a failing run every
  // 5 s; the refresh timer does, at its own pace.
  seenLimits = limitsKey(current)
  seenStamp = before.mtime
  const result = await $.process.run(
    [rt.python, ...rt.args, rt.entry, 'statusline', '--source', 'mod', '--format', 'json'],
    { stdin: JSON.stringify(payload), timeoutMs: RUN_TIMEOUT_MS, env: { GLIDEPATH_HOME: rt.home } },
  )
  if (result.exitCode !== 0 || result.stdout.trim() === '') {
    // On Windows a missing interpreter does not fail to start: the spawn falls
    // back to cmd.exe, which says only "...operable program or batch file."
    // (exit 9009) or "The system cannot find the path specified.". Asked only
    // after a failed run, so a Store alias exists() cannot see is not
    // misreported while it works.
    if (result.exitCode !== 0 && !(await $.fs.exists(rt.python).catch(() => true))) {
      runtime = null // read runtime.json again on the next refresh, after a reinstall
      throw new Error(
        rt.pinned ? `Python not found at ${rt.python}: run \`glidepath install --apply\`` : NO_PYTHON,
      )
    }
    const lines = result.stderr.trim().split(/\r?\n/)
    // cmd.exe's own message spans 2 lines.
    const why = result.exitCode === 9009 ? lines.join(' ') : lines.pop()
    throw new Error(why || `glidepath exited ${result.exitCode}`)
  }
  const parsed = JSON.parse(result.stdout) as Partial<View>
  if (!Array.isArray(parsed.segments)) {
    throw new Error(typeof parsed.line === 'string' ? parsed.line : 'unexpected output from glidepath')
  }
  // This run's own touch of the stamp is no news; another chat's since is.
  const after = await stamp($, rt)
  if (isNum(parsed.now) && Math.abs(after.ts - parsed.now) < 0.002) seenStamp = after.mtime
  const segments = parsed.segments
  await update($, view, () => ({
    line: typeof parsed.line === 'string' ? parsed.line : '',
    segments,
    surfaces: Array.isArray(parsed.surfaces) ? parsed.surfaces : ['desktop'],
    refresh_s: typeof parsed.refresh_s === 'number' ? parsed.refresh_s : 60,
    windows: windowsOf(parsed.windows),
    updated_ts: isNum(parsed.updated_ts) ? parsed.updated_ts : null,
    now: isNum(parsed.now) ? parsed.now : undefined,
  }))
  rearm($, parsed.refresh_s)
}

async function refresh($: EngineInterface, limits: readonly SessionRateLimit[] | null = null) {
  if (running) {
    queued = limits ?? queued ?? null
    return
  }
  running = true
  let pending: readonly SessionRateLimit[] | null | undefined = limits
  try {
    while (pending !== undefined) {
      queued = undefined
      try {
        await runOnce($, pending)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        await update($, view, (old) => ({
          ...(old ?? {}),
          line: old?.line ?? '',
          segments: old?.segments ?? [],
          surfaces: old?.surfaces ?? ['desktop'],
          refresh_s: old?.refresh_s ?? 60,
          error: message.slice(0, 200),
        }))
      }
      pending = queued
    }
  } finally {
    running = false
  }
}

// Runs Python only when a reading moved: this session's limits (any change,
// where session.measure waits for a whole point), or the stamp another
// chat's run touched.
async function poll($: EngineInterface) {
  // No runtime yet, or it is being read again after a failure: the refresh
  // timer does that, and reports why.
  const rt = runtime
  if (running || seenLimits === null || rt === null) return
  const limits = (await $.session.usage()).rateLimits
  const now = await stamp($, rt)
  if (limitsKey(limits) !== seenLimits || now.mtime !== seenStamp) await refresh($, limits)
}

// -- drawing ----------------------------------------------------------------

// Widths in the band's cells (bodyColumns), checked in Chromium at 7 px a
// cell from 40 to 140 columns: halves with resets from 112, 64 px bars from
// 72, "Wk" and only the words that need you below 52, where a calm window
// beside one that needs you keeps only its name and percent.
export const layout = (body: number) => ({
  halves: body >= 112,
  W: body >= 72 ? 64 : 44,
  tiny: body < 52,
  bare: body < 44,
})

const SVG_H = 14
const Y = 4.5
const BH = 5
const R = 2.5
const GAP = 1.5
// One style per bar, scoped to its root: Claude's orange in both themes, the
// rest lifted for a dark background. Track, forecast, idle fill and pill are
// color plus opacity, so they tint whatever is behind them.
const STYLE =
  '<style>.ctb-s{--t:#8a867c;--a:#d97757;--r:#d0564c;--g:#5b9c64}' +
  '@media(prefers-color-scheme:dark){.ctb-s{--t:#b4afa4;--r:#e5695e;--g:#74b97c}}' +
  '.ctb-s .ctb-bt{fill:var(--t);opacity:.22}.ctb-s .ctb-bu{fill:var(--a)}.ctb-s .ctb-bf{fill:var(--a);opacity:.32}' +
  '.ctb-s .ctb-bx{fill:var(--r);opacity:.38}.ctb-s .ctb-bm{fill:var(--r)}.ctb-s .ctb-bi{fill:var(--t);opacity:.6}' +
  '.ctb-s .ctb-bp{fill:var(--t);opacity:.45}.ctb-s line{stroke-width:2;stroke-linecap:round}' +
  '.ctb-s .ctb-bw{stroke:var(--r)}.ctb-s .ctb-bd{stroke:var(--g);stroke-dasharray:0 3.5}</style>'

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

// The chart's style, scoped to its root like the bars': the text, the grid
// and now grey, Claude's orange, the forecast red or yellow in the pace's
// color. Mid-tones only, as the bars': an image follows the system's scheme,
// not the app's, so each mark reads on a light card and a dark one (D-30).
const CHART_STYLE =
  '<style>.ctc{--t:#8a867c;--a:#d97757;--r:#d0564c;--y:#c08a1e}' +
  '@media(prefers-color-scheme:dark){.ctc{--t:#a3a19a;--r:#e5695e;--y:#e0b252}}' +
  '.ctc .nt{fill:var(--t);opacity:.06}.ctc .ns{fill:var(--t);opacity:.45}' +
  '.ctc .g{stroke:var(--t);stroke-width:1;opacity:.2}.ctc .lim{stroke:var(--t);stroke-width:.8;stroke-dasharray:2 3;opacity:.6}' +
  '.ctc .ar{fill:var(--a);opacity:.13}.ctc .ac{fill:none;stroke:var(--a);stroke-width:1.8;stroke-linejoin:round}' +
  '.ctc .dt{fill:var(--a)}.ctc .fn{fill:var(--a);opacity:.1}' +
  '.ctc .fc{fill:none;stroke:var(--a);stroke-width:1.6;stroke-dasharray:4 3;opacity:.75}' +
  '.ctc .fx{fill:none;stroke:var(--r);stroke-width:1.6;stroke-dasharray:4 3}' +
  '.ctc .fy{fill:none;stroke:var(--y);stroke-width:1.6;stroke-dasharray:4 3}' +
  '.ctc .gh{fill:none;stroke:var(--t);stroke-width:1;stroke-dasharray:3 3;opacity:.6}' +
  '.ctc .nw{stroke:var(--t);stroke-width:.8;opacity:.75}.ctc .end{fill:none;stroke:var(--a);stroke-width:1.2}' +
  '.ctc .ex{stroke:var(--r)}.ctc .ey{stroke:var(--y)}' +
  '.ctc .rx{fill:var(--r);stroke:var(--r)}.ctc .ry{fill:var(--y);stroke:var(--y)}' +
  '.ctc .oz{opacity:.09;stroke:none}.ctc .ol{stroke-width:1;stroke-dasharray:2.5 2}.ctc .ot{font-weight:600;stroke:none}' +
  '.ctc text{font:9px -apple-system,"Segoe UI",system-ui,sans-serif;fill:var(--t)}</style>'

export const CHART_H = 64
// A label's character at 9 px, with some to spare (the system font's widest
// day names, "Wed", run about 5.5 px a character).
const LABEL_PX = 6

// A window's chart, W px wide (chart.py): the week's nights as pale bands,
// what was used (a line over a pale
// area, a dot per reading while there are few), the forecast to the reset
// (dashed, in the pace's color when it needs you: red when it runs out or
// is blocked, yellow when it may or passes the target), an early 5-hour
// estimate's fan, the forecasts made earlier (grey, dashed), the grid and
// its hours or days (in the CLI's order, one that would touch one placed
// before is left out, as in the menu bar), the limit (dotted) and now (a
// hairline).
export function chartSvg(c: Chart, W: number, alt: string, tone?: string): string {
  const H = CHART_H
  const pad = { l: 1, r: 4, t: 6, b: 14 }
  const sx = (t: number) => r1(pad.l + ((t - c.t0) / (c.t1 - c.t0)) * (W - pad.l - pad.r))
  const sy = (u: number) => r1(pad.t + (1 - Math.min(100, Math.max(0, u)) / 100) * (H - pad.t - pad.b))
  const d = (pts: [number, number][]) => pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p[0])},${sy(p[1])}`).join('')
  const base = sy(0)
  const out = [
    `<svg xmlns='http://www.w3.org/2000/svg' class='ctc' width='${W}' height='${H}' viewBox='0 0 ${W} ${H}'>`,
    CHART_STYLE,
    `<title>${esc(alt)}</title>`,
  ]
  // The week's nights: a faint tint, and a thin rounded strip under the axis
  // that reads on any screen.
  for (const [a, b] of c.nights ?? []) {
    const x = sx(a)
    const w = r1(sx(b) - x)
    out.push(`<rect class='nt' x='${x}' y='${pad.t}' width='${w}' height='${r1(base - pad.t)}'/>`)
    out.push(`<rect class='ns' x='${x}' y='${base + 2}' width='${w}' height='2' rx='1'/>`)
  }
  for (const t of c.grid) out.push(`<line class='g' x1='${sx(t)}' y1='${pad.t}' x2='${sx(t)}' y2='${base}'/>`)
  out.push(`<line class='lim' x1='${pad.l}' y1='${sy(100)}' x2='${W - pad.r}' y2='${sy(100)}'/>`)
  for (const g of c.ghosts) out.push(`<path class='gh' d='${d(g)}'/>`)
  const a = c.actual
  const last = a[a.length - 1] as [number, number]
  out.push(`<path class='ar' d='${d(a)}L${sx(last[0])},${base}L${sx(a[0]![0])},${base}Z'/>`)
  out.push(`<path class='ac' d='${d(a)}'/>`)
  if (a.length <= 30) for (const p of a.slice(1)) out.push(`<circle class='dt' cx='${sx(p[0])}' cy='${sy(p[1])}' r='1.4'/>`)
  const f = c.forecast
  if (c.fan) {
    // Between the slower line and the faster one, back along it.
    const [lo, hi] = c.fan
    out.push(`<path class='fn' d='${d(lo)}${d([...hi].reverse()).replace('M', 'L')}Z'/>`)
  }
  if (f.length) {
    const end = f[f.length - 1] as [number, number]
    const k = tone === 'error' ? 'x' : tone === 'warning' ? 'y' : ''
    out.push(`<path class='${k ? `f${k}` : 'fc'}' d='${d(f)}'/>`)
    out.push(`<circle class='end${k ? ` e${k}` : ''}' cx='${r1(sx(end[0]) - 1.5)}' cy='${sy(end[1])}' r='2.2'/>`)
  }
  if (c.out != null) {
    // It runs out before the reset: the rest of the window tinted, a dashed
    // mark down from a dot on the limit, and its time beside it.
    const h = tone === 'warning' ? 'ry' : 'rx'
    const x = sx(c.out)
    out.push(`<rect class='oz ${h}' x='${x}' y='${pad.t}' width='${r1(Math.max(0, W - pad.r - x))}' height='${r1(base - pad.t)}'/>`)
    out.push(`<line class='ol ${h}' x1='${x}' y1='${pad.t}' x2='${x}' y2='${base}'/>`)
    out.push(`<circle class='${h}' cx='${x}' cy='${pad.t}' r='2.4'/>`)
    if (c.out_label) {
      const room = W - pad.r - x > c.out_label.length * LABEL_PX + 6
      out.push(`<text class='ot ${h}' x='${r1(room ? x + 4 : x - 4)}' y='${pad.t + 10}' text-anchor='${room ? 'start' : 'end'}'>${esc(c.out_label)}</text>`)
    }
  }
  out.push(`<line class='nw' x1='${sx(c.now)}' y1='${pad.t}' x2='${sx(c.now)}' y2='${base}'/>`)
  out.push(`<circle class='dt' cx='${sx(last[0])}' cy='${sy(last[1])}' r='2.4'/>`)
  // In the CLI's order (whole days before the week's edge days): a label
  // that would touch one placed before it is left out, as in the menu bar.
  const placed: [number, number][] = []
  for (const [t, label] of c.labels) {
    const x = sx(t)
    const w = label.length * LABEL_PX
    const anchor = x - w / 2 < pad.l ? 'start' : x + w / 2 > W - 1 ? 'end' : 'middle'
    const left = anchor === 'start' ? pad.l : anchor === 'end' ? W - 1 - w : x - w / 2
    if (placed.some(([a, b]) => left < b + 3 && a < left + w + 3)) continue
    placed.push([left, left + w])
    out.push(`<text x='${anchor === 'start' ? pad.l : anchor === 'end' ? W - 1 : x}' y='${H - 2}' text-anchor='${anchor}'>${esc(label)}</text>`)
  }
  out.push('</svg>')
  return out.join('')
}
const r1 = (n: number) => Math.round(n * 10) / 10

// 5 hex digits of FNV-1a: a gradient id of its own per bar.
function hash(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0').slice(0, 5)
}

// The bar of one window, W px wide: used in orange, then after a hairline gap
// a pale forecast to where it lands at the reset (fading while the estimate
// is rough); red into a wall when it runs out (fading when it only may);
// green dots over what will go unused; all red when maxed; grey when idle; a
// pill while waiting. Drawn as an image: an interactive frame paints an
// opaque white box where the app's theme and the system's differ (Chromium's
// color-scheme rule), so the tooltip's words are in the row and in Details.
export function barSvg(w: Win, W: number): string {
  const X = (p: number) => (Math.min(100, Math.max(0, p)) / 100) * W
  const pill = (x0: number, x1: number, c: string) =>
    `<rect x='${r1(x0)}' y='${Y}' width='${r1(Math.max(x1 - x0, BH))}' height='${BH}' rx='${R}' ` +
    (c.startsWith('url') ? `fill='${c}'/>` : `class='ctb-b${c}'/>`)
  const out = [
    `<svg xmlns='http://www.w3.org/2000/svg' class='ctb-s' width='${W}' height='${SVG_H}' viewBox='0 0 ${W} ${SVG_H}'>`,
    STYLE,
    `<title>${esc(w.sentence)}</title>`,
    pill(0, W, 't'),
  ]
  if (w.kind === 'wait') {
    out.push(
      `<rect y='${Y}' width='16' height='${BH}' rx='${R}' class='ctb-bp'>` +
        `<animate attributeName='x' values='0;${W - 16};0' dur='2.4s' repeatCount='indefinite'/></rect></svg>`,
    )
    return out.join('')
  }
  const u = X(w.used)
  const p = X(w.projected ?? w.used)
  const g0 = u > 0 ? Math.max(u, BH) + GAP : 0
  if (w.kind === 'maxed') out.push(pill(0, W, 'm'))
  else {
    if (w.kind === 'dry') {
      // Red into a wall; a milder run-out (soft) fades, and so does its wall.
      if (W - g0 >= BH && w.soft) {
        const id = `ctb${hash(`${w.id}|soft|${w.used}|${W}`)}`
        out.push(
          `<linearGradient id='${id}' gradientUnits='userSpaceOnUse' x1='${r1(g0)}' x2='${W}'>` +
            `<stop offset='.2' style='stop-color:var(--r);stop-opacity:.45'/>` +
            `<stop offset='1' style='stop-color:var(--r);stop-opacity:.08'/></linearGradient>`,
          pill(g0, W, `url(#${id})`),
        )
      } else if (W - g0 >= BH) out.push(pill(g0, W, 'x'))
      out.push(
        `<line x1='${W - 1}' y1='1.5' x2='${W - 1}' y2='12.5' class='ctb-bw'${w.soft ? " style='opacity:.45'" : ''}/>`,
      )
    } else if (w.kind !== 'idle' && w.projected !== null && p - g0 >= BH) {
      if (w.kind === 'rough') {
        const id = `ctb${hash(`${w.id}|${w.kind}|${w.used}|${w.projected}|${W}`)}`
        out.push(
          `<linearGradient id='${id}' gradientUnits='userSpaceOnUse' x1='${r1(g0)}' x2='${r1(p)}'>` +
            `<stop offset='.2' style='stop-color:var(--a);stop-opacity:.45'/>` +
            `<stop offset='1' style='stop-color:var(--a);stop-opacity:0'/></linearGradient>`,
          pill(g0, p, `url(#${id})`),
        )
      } else out.push(pill(g0, p, 'f'))
    }
    if (w.kind === 'expire' && W - 2.5 > p + 4) {
      out.push(`<line x1='${r1(p + 4)}' y1='7' x2='${r1(W - 2.5)}' y2='7' class='ctb-bd'/>`)
    }
    if (u > 0) out.push(pill(0, u, w.kind === 'idle' ? 'i' : 'u'))
  }
  out.push('</svg>')
  return out.join('')
}

// A reading 5 min old or more: Details says how old.
export const stale = (updated: number | null | undefined, now: number | undefined): boolean =>
  typeof updated === 'number' && typeof now === 'number' && now - updated >= 300

// "Updated 3 min ago." from the CLI's own clock, so a stale view says so.
export function age(updated: number | null | undefined, now: number | undefined): string {
  if (!isNum(updated) || !isNum(now)) return ''
  const min = Math.max(0, Math.floor((now - updated) / 60))
  if (min < 1) return 'Updated just now.'
  if (min < 60) return `Updated ${min} min ago.`
  const h = Math.round(min / 60)
  return h < 48 ? `Updated ${h} h ago.` : `Updated ${Math.round(h / 24)} days ago.`
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    $.clock.after(0, () => {
      void (async () => trace($, `start: surfaces ${await surfacesOf($)}`))()
    })
    // Off the start path: the first prompt never waits for Python.
    $.clock.after(0, () => {
      void refresh($)
    })
    timer = $.clock.every(periodMs, () => {
      void refresh($)
    })
    $.clock.every(POLL_MS, () => {
      void poll($).catch(() => undefined)
    })
    return started
  })

  on('session.measure', async ($, e, next) => {
    const measured = await next(e)
    if (e.changed.includes('rateLimits') && limitsKey(e.rateLimits) !== seenLimits) {
      void refresh($, e.rateLimits)
    }
    return measured
  })

  on('session.attach', async ($, e, next) => {
    const attached = await next(e)
    void (async () => trace($, `attach: ${e.surface}, surfaces ${await surfacesOf($)}`))()
    return attached
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const current = await read($, view)
    const why =
      current === null ? 'no reading yet' : e.props.hasSurvey ? 'a survey' :
      !current.surfaces.includes(e.surface) ? `not in mod.surfaces [${current.surfaces.join(', ')}]` : ''
    const key = `${e.surface}|${why}`
    if (!seenRenders.has(key)) {
      seenRenders.add(key)
      void trace($, `band asked on ${e.surface}: ${why ? `not drawn, ${why}` : 'drawn'}`)
    }
    if (why) return next(e)
    if (current === null) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const wins = current.windows
    let ours
    if (current.error) {
      ours = (
        <Text dimColor wrap="truncate-end">
          glidepath: {current.error}
        </Text>
      )
    } else if (wins && e.surface === 'desktop') {
      const { Svg } = $.ui.resolve(e)
      const L = layout(e.props.bodyColumns)
      const waiting = wins.every((w) => w.pct === null)
      const focus = wins.some((w) => w.phrase?.attention)
      const both = wins.filter((w) => w.phrase?.attention).length > 1
      // Every window says its pace ("1.4x faster", "On pace", "2x slower").
      // With the small bars, below 72 cells, only one window that does not
      // need you keeps its words (green before grey, the 5 hours first), and
      // none beside one that does; below 52 only the words that need you.
      const quiet = wins.filter((w) => w.phrase && !w.phrase.attention)
      const kept = quiet.find((w) => !w.phrase?.calm) ?? quiet[0]
      const said = (w: Win) => {
        const p = w.phrase
        if (!p || p.attention || L.W === 64) return p
        return !focus && !L.tiny && w === kept ? p : null
      }
      // The short lead ("Back 16:30", "Over 80% target") below the halves.
      const words = (p: Phrase) => (L.halves ? [p.pre, p.num, p.post] : [p.narrow_pre ?? p.pre, p.num, p.narrow])
      // Below 60 cells, 2 windows that need you fit only without the bars
      // (measured in Chromium at 7 px a cell, 40 to 140).
      const squeezed = e.props.bodyColumns < 56
      const nobars = both && e.props.bodyColumns < 60
      const button = !waiting && !(L.bare && focus)
      // A short label below 56 cells, or when 2 windows need you with the small bars.
      const short = squeezed || (both && L.W === 44)
      const open = (await read($, details)) && button
      const toggle = () => update($, details, (v) => !v)
      // Details: a card per window, side by side from 72 cells, one under the
      // other below: its name, its percent large and its pace in color, then
      // the CLI's other rows (band.details, the menu bar's too), the label on
      // the left and the value on the right, so each number stays with its row.
      const rows = (w: Win): Detail[] =>
        w.details?.length ? w.details : [{ label: 'Summary', value: w.sentence, tone: 'inactive' }]
      const shown = (w: Win) =>
        rows(w)
          .filter((r) => !HEAD.includes(r.label))
          .sort((a, b) => rank(a.label) - rank(b.label))
      const title = (w: Win) => (w.id === 'h5' ? '5 hours' : 'Week')
      // The pace as the band says it; without words (a maxed week blocks the
      // 5 hours), the CLI's Pace row.
      const pace = (w: Win) => {
        if (w.phrase) return { text: w.phrase.pre + w.phrase.num + w.phrase.post, tone: w.phrase.tone }
        const r = rows(w).find((d) => d.label === 'Pace')
        return r ? { text: r.value, tone: r.tone } : null
      }
      const side = e.props.bodyColumns >= 72
      // The chart fills the card's inside (7 px a cell, less the border and
      // the padding); drawn wider than its slot, the surface scales it down.
      const cw = Math.max(120, (side ? Math.floor((e.props.bodyColumns * 7 - 12) / 2) : e.props.bodyColumns * 7) - 26)
      const card = (w: Win) => {
        const pc = pace(w)
        const about = `${title(w)}: what was used over the window and the forecast to its reset`
        return (
          <Box
            key={`card-${w.id}`}
            flexDirection="column"
            flexGrow={1}
            flexShrink={1}
            width={side ? '50%' : undefined}
            borderStyle="round"
            borderDimColor
            paddingX="md"
            paddingY="sm"
            rowGap="xs"
          >
            <Box key={`ch-${w.id}`} flexDirection="row" justifyContent="space-between" alignItems="center" columnGap="md">
              <Box flexDirection="row" alignItems="center" columnGap="sm">
                <Text size="footnote" color="inactive" weight="medium">
                  {title(w)}
                </Text>
                <Text size="heading" weight="semibold" tabular>
                  {w.pct === null ? 'waiting' : `${w.pct}%`}
                </Text>
              </Box>
              {pc ? (
                <Text size="footnote" weight="medium" color={pc.tone} wrap="truncate-end">
                  {pc.text}
                </Text>
              ) : null}
            </Box>
            {w.chart ? <Svg key={`cc-${w.id}`} source={chartSvg(w.chart, cw, about, pc?.tone)} alt={about} /> : null}
            {shown(w).map((r) => (
              <Box key={`cr-${w.id}-${r.label}`} flexDirection="row" justifyContent="space-between" columnGap="md">
                <Text size="footnote" color="inactive">
                  {r.label}
                </Text>
                <Box flexDirection="row" flexShrink={1} columnGap="xs">
                  <Text size="footnote" tabular wrap="truncate-end" color={r.tone === 'text' ? 'text' : r.tone}>
                    {r.value}
                  </Text>
                  {r.note ? (
                    <Text size="footnote" tabular wrap="truncate-end" color={r.note_tone ?? 'inactive'}>
                      {`· ${r.note}`}
                    </Text>
                  ) : null}
                </Box>
              </Box>
            ))}
          </Box>
        )
      }
      const table = (
        <Box flexDirection={side ? 'row' : 'column'} columnGap="md" rowGap="sm">
          {wins.map(card)}
        </Box>
      )
      const row = (
        <Box flexDirection="row" alignItems="center" columnGap="sm">
          <Box
            flexDirection="row"
            alignItems="center"
            flexGrow={1}
            flexShrink={1}
            columnGap={L.halves && !waiting ? 0 : 'md'}
          >
            {wins.map((w) => {
              const att = Boolean(w.phrase?.attention)
              const dim = focus && !att
              const p = said(w)
              const [pre, num, post] = p ? words(p) : ['', '', '']
              const plain = p?.tone === 'inactive'
              return (
                <Box
                  key={`ctb-${w.id}`}
                  width={L.halves && !waiting ? '50%' : undefined}
                  flexShrink={1}
                  flexDirection="row"
                  alignItems="center"
                  columnGap="xs"
                >
                  <Text size="footnote" color="inactive">
                    {L.tiny && w.id === 'wk' ? w.short : w.name}
                  </Text>
                  {(L.tiny && focus && !att) || nobars ? null : (
                    <Svg source={barSvg(w, L.W)} alt={w.sentence} width={L.W} height={SVG_H} />
                  )}
                  {w.pct === null ? (
                    waiting ? null : (
                      <Text size="footnote" color="inactive">
                        waiting
                      </Text>
                    )
                  ) : (
                    <Text
                      size="footnote"
                      tabular
                      weight={dim ? 'regular' : 'medium'}
                      color={dim ? 'inactive' : 'text'}
                    >
                      {`${w.pct}%`}
                    </Text>
                  )}
                  {p ? (
                    // A little more air between the percent and the words than inside them.
                    <Box marginLeft={squeezed ? undefined : 'xs'} flexShrink={1}>
                      <Text size="footnote" wrap="truncate-end" color={p.tone} weight={plain ? 'regular' : 'medium'}>
                        {pre}
                        {num ? (
                          <Text weight="semibold" tabular color={p.tone}>
                            {num}
                          </Text>
                        ) : null}
                        {post}
                      </Text>
                    </Box>
                  ) : null}
                  {L.halves && w.reset && w.pct !== null && w.kind !== 'maxed' ? (
                    <Box marginLeft="sm">
                      <Text size="footnote" color="inactive" tabular>
                        {`resets ${w.reset}`}
                      </Text>
                    </Box>
                  ) : null}
                </Box>
              )
            })}
            {waiting ? (
              <Text size="footnote" color="inactive">
                {L.halves ? 'Waiting for the first reading' : 'Waiting for data'}
              </Text>
            ) : null}
          </Box>
          {button ? (
            // One label in both states, so the row never shifts when it toggles;
            // below 56 cells a short one, so the words that need you keep their room.
            <Button key="details" plain dimColor hotkey="d" label={short ? '⋯' : 'Details'} onPress={toggle} />
          ) : null}
        </Box>
      )
      ours = open ? (
        <Box flexDirection="column" rowGap="sm">
          {row}
          {table}
          {/* How old the reading is, once that is worth a line (5 min). */}
          {stale(current.updated_ts, current.now) ? (
            <Text size="footnote" color="inactive">
              {age(current.updated_ts, current.now)}
            </Text>
          ) : null}
        </Box>
      ) : (
        row
      )
    } else {
      // The terminal (when asked for), or an older CLI: the text line.
      ours = (
        <Box flexDirection="row">
          {current.segments.map((segment, index) => (
            <Text key={`s${index}`} wrap="truncate-end" {...(TONE[segment.tone] ?? {})}>
              {segment.text}
            </Text>
          ))}
        </Box>
      )
    }
    // Keep what other mods draw in the band below ours.
    const below = await next(e)
    return below ? (
      <Box flexDirection="column">
        {ours}
        {below}
      </Box>
    ) : (
      ours
    )
  })
}
