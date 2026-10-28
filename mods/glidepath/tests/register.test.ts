import type { ProcessRunResult, RenderSurface, SessionRateLimit } from 'claude-code'
import { expect, type MountTarget, mock, type TestBody, test } from 'claude-code/testing'

import { age, barSvg, chartOf, chartSvg, stale, fromGitBash, layout, resolveHome, toPayload } from '../hooks/register'
import type { Chart, Detail, Phrase, Win } from '../types'

type StubOn = Parameters<TestBody>[1]
type Run = { argv: readonly string[]; init?: { stdin?: string; timeoutMs?: number; env?: Record<string, string> } }

const NOW = 1_791_964_800_000 // 2026-10-14T08:00:00Z
const LIMITS: SessionRateLimit[] = [
  { kind: 'five_hour', percentUsed: 62, resetsAt: '2026-10-14T10:20:00.000Z' },
  { kind: 'seven_day', percentUsed: 48.5, resetsAt: '2026-10-16T09:00:00Z' },
]
const CLI = {
  ok: true,
  line: '5h 62% ×1.4▲ →88% @10:20 · wk 49% +3 ×0.8▼ →96% @Fri 09:00',
  segments: [
    { text: '5h 62% ×1.4▲ →88% @10:20', tone: 'dim' },
    { text: ' · ', tone: 'dim' },
    { text: 'wk 49% +3 ×0.8▼ →96% @Fri 09:00', tone: 'warn' },
  ],
  surfaces: ['desktop'],
  refresh_s: 60,
}

function ran(stdout: string): { value: ProcessRunResult } {
  return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
}

type Options = {
  runtime?: string | null
  // Why the read fails when runtime is null.
  denied?: string
  limits?: SessionRateLimit[]
  env?: Record<string, string>
  run?: () => { value: ProcessRunResult }
  // What $.fs.exists answers; left out, nothing answers it (the call rejects).
  exists?: (path: string) => boolean
  // GLIDEPATH_HOME/changed as the CLI leaves it; absent, the file is missing.
  stamp?: { mtimeMs: number; text: string }
  // What $.session.surfaces() answers.
  surfaces?: RenderSurface[]
}

function stub(on: StubOn, options: Options = {}) {
  const runs: Run[] = []
  const clock = mock.clock(on, { now: NOW })
  mock.env(on, options.env ?? { HOME: '/home/u' })
  const reads: string[] = []
  const writes: { path: string; text: string }[] = []
  const runtime =
    options.runtime === undefined
      ? JSON.stringify({ python: '/usr/bin/python3', entry: '/repo/bin/glidepath' })
      : options.runtime
  on('fs.read', (_$, e) => {
    const path = String((e as { path?: string }).path)
    if (/[\\/]changed$/.test(path)) {
      return options.stamp ? { value: options.stamp.text } : { deny: 'ENOENT: no such file' }
    }
    reads.push(path)
    return runtime === null ? { deny: options.denied ?? 'ENOENT: no such file' } : { value: runtime }
  })
  on('fs.stat', (_$, e) => {
    const stamp = options.stamp
    if (!stamp || !/[\\/]changed$/.test(e.path)) return { deny: 'ENOENT: no such file' }
    return { value: { kind: 'file', size: stamp.text.length, mtimeMs: stamp.mtimeMs, isLink: false } }
  })
  on('session.usage', () => ({
    value: { startedAt: NOW - 60_000, context: { window: 200_000 }, rateLimits: options.limits ?? LIMITS },
  }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.surfaces', () => ({ value: options.surfaces ?? ['desktop'] }))
  on('fs.write', (_$, e) => {
    writes.push({ path: slashed(e.path), text: e.text })
    return { value: undefined }
  })
  const exists = options.exists
  if (exists) on('fs.exists', (_$, e) => ({ value: exists(e.path) }))
  on('process.run', (_$, e) => {
    runs.push(e)
    return options.run?.() ?? ran(JSON.stringify(CLI))
  })
  on('session.start', () => ({ cwd: '/work' }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  return { runs, clock, reads, writes }
}

function band(surface: RenderSurface): MountTarget {
  return {
    plugin: 'glidepath',
    component: 'AbovePrompt',
    surface,
    viewport: { columns: 120, rows: 40, isFullscreen: false },
    props: {
      hasSurvey: false,
      isWorking: false,
      maxRows: 5,
      bodyColumns: 115,
      scroll: { offset: 0, bodyRows: 5 },
      view: {},
    },
  }
}

const start = (surface: RenderSurface) => ({ surface, isInteractive: true, cwd: '/work' })

// The test kit resolves e.path against the cwd before a stub sees it, which
// on Windows adds a drive letter and turns `/` into `\`: compare the end of
// the path, both sides with forward slashes.
function slashed(path: string | undefined): string {
  return (path ?? '').replace(/\\/g, '/')
}

function ending(suffix: string): RegExp {
  return new RegExp(`${slashed(suffix).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`)
}

test('converts rate limits to the statusLine payload', async () => {
  const p = toPayload('s', LIMITS)
  expect(p.session_id).toBe('s')
  expect(p.rate_limits?.five_hour).toEqual({ used_percentage: 62, resets_at: 1_791_973_200 })
  expect(p.rate_limits?.seven_day?.resets_at).toBe(1_792_141_200)
  expect(p.kinds).toEqual(['five_hour', 'seven_day'])
  const empty = toPayload('s', [])
  expect(empty.rate_limits).toBeUndefined()
  expect(toPayload('s', [{ kind: 'spend_limit', percentUsed: 120 }]).rate_limits?.spend_limit).toEqual({
    used_percentage: 120,
  })
})

test('draws the glidepath line above the prompt in Desktop', async ($, on) => {
  const { runs, clock } = stub(on)
  await $.session.start(start('desktop'))
  await clock.advance(1)

  expect(runs.length).toBe(1)
  expect(runs[0]?.argv).toEqual([
    '/usr/bin/python3', '-I', '-S', '/repo/bin/glidepath', 'statusline', '--source', 'mod', '--format', 'json',
  ])
  const input = JSON.parse(runs[0]?.init?.stdin ?? '{}')
  expect(input.session_id).toBe('sess-1')
  expect(input.rate_limits.five_hour).toEqual({ used_percentage: 62, resets_at: 1_791_973_200 })

  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: '5h 62% ×1.4▲ →88% @10:20' })).toMatchObject({
    props: { dimColor: true },
  })
  expect(await ui.find({ type: 'Text', text: 'wk 49% +3 ×0.8▼ →96% @Fri 09:00' })).toMatchObject({
    props: { color: 'warning' },
  })
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
})

test('leaves the terminal band alone, where the statusLine already shows it', async ($, on) => {
  const { clock } = stub(on)
  await $.session.start(start('terminal'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('terminal'))
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^5h/ })).toBeUndefined()
})

test('records new readings when a window moves and refreshes every minute', async ($, on) => {
  const options: Options = {}
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const moved: SessionRateLimit[] = [{ kind: 'five_hour', percentUsed: 63, resetsAt: '2026-10-14T10:20:00Z' }]
  options.limits = moved
  await $.session.measure({ context: { window: 200_000 }, rateLimits: moved, changed: ['rateLimits'] })
  await clock.advance(1)
  expect(runs.length).toBe(2)
  expect(JSON.parse(runs[1]?.init?.stdin ?? '{}').rate_limits.five_hour.used_percentage).toBe(63)
  await $.session.measure({ context: { window: 200_000 }, rateLimits: moved, changed: ['context'] })
  expect(runs.length).toBe(2)
  await clock.advance(60_000)
  expect(runs.length).toBe(3)
})

test('explains itself when glidepath is not installed', async ($, on) => {
  const { runs, clock } = stub(on, { runtime: null })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /^glidepath: / })).toMatchObject({ props: { dimColor: true } })
})

test('shows why when the CLI fails, instead of a JSON parse error', async ($, on) => {
  const { clock } = stub(on, {
    run: () => ({
      value: {
        exitCode: 2,
        stdout: '',
        stderr: "python3: can't open file '/old/path/bin/glidepath': [Errno 2] No such file or directory\n",
        isStdoutTruncated: false,
        isStderrTruncated: false,
      },
    }),
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /can't open file/ })).toBeDefined()
})

test('an error reply without surfaces still draws on Desktop', async ($, on) => {
  const { clock } = stub(on, {
    run: () => ran(JSON.stringify({ ok: false, line: 'glidepath: error', segments: [{ text: 'glidepath: error', tone: 'dim' }] })),
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: 'glidepath: error' })).toBeDefined()
})

test('an empty GLIDEPATH_HOME counts as unset, and the CLI gets the same home', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', GLIDEPATH_HOME: '' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/.glidepath/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ GLIDEPATH_HOME: '/home/u/.glidepath' })
})

// Review 4: settings.json env reaches the mod as written, so a `~` or a
// relative GLIDEPATH_HOME would otherwise be read from the session's cwd.
test('expands a leading ~ in GLIDEPATH_HOME and passes the result to the CLI', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', GLIDEPATH_HOME: '~/.glidepath-moved' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/.glidepath-moved/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ GLIDEPATH_HOME: '/home/u/.glidepath-moved' })
})

test('takes a relative GLIDEPATH_HOME from HOME, never from the session cwd', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', GLIDEPATH_HOME: 'data/glidepath' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/home/u/data/glidepath/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ GLIDEPATH_HOME: '/home/u/data/glidepath' })
})

test('uses an absolute GLIDEPATH_HOME as is, even without HOME', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { GLIDEPATH_HOME: '/srv/glidepath' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(slashed(reads[0])).toMatch(ending('/srv/glidepath/runtime.json'))
  expect(runs[0]?.init?.env).toEqual({ GLIDEPATH_HOME: '/srv/glidepath' })
})

test("asks for an absolute path when GLIDEPATH_HOME names another user's ~", async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { HOME: '/home/u', GLIDEPATH_HOME: '~other/glidepath' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(reads.length).toBe(0)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /absolute path/ })).toBeDefined()
})

// Windows: Python takes the home from USERPROFILE (else HOMEDRIVE+HOMEPATH)
// and never from HOME, and `glidepath install` writes runtime.json there.
const WINDOWS = { OS: 'Windows_NT', USERPROFILE: 'C:\\Users\\u' }
const WINDOWS_HOMES: [string, Record<string, string>, string][] = [
  ['USERPROFILE', WINDOWS, 'C:\\Users\\u\\.glidepath'],
  ['USERPROFILE, with HOME ignored', { ...WINDOWS, HOME: '/c/Users/other' }, 'C:\\Users\\u\\.glidepath'],
  [
    'HOMEDRIVE+HOMEPATH without USERPROFILE',
    { OS: 'Windows_NT', USERPROFILE: '', HOMEDRIVE: 'D:', HOMEPATH: '\\Users\\u', HOME: '/home/u' },
    'D:\\Users\\u\\.glidepath',
  ],
  ['~\\ in GLIDEPATH_HOME', { ...WINDOWS, GLIDEPATH_HOME: '~\\glidepath' }, 'C:\\Users\\u\\glidepath'],
  ['~/ in GLIDEPATH_HOME', { ...WINDOWS, GLIDEPATH_HOME: '~/glidepath' }, 'C:\\Users\\u\\glidepath'],
  ['a relative GLIDEPATH_HOME', { ...WINDOWS, GLIDEPATH_HOME: 'data\\glidepath' }, 'C:\\Users\\u\\data\\glidepath'],
  ['an absolute E:\\ GLIDEPATH_HOME', { ...WINDOWS, GLIDEPATH_HOME: 'E:\\data\\glidepath' }, 'E:\\data\\glidepath'],
  ['an absolute E:/ GLIDEPATH_HOME', { ...WINDOWS, GLIDEPATH_HOME: 'E:/data/glidepath' }, 'E:/data/glidepath'],
  ['a rooted \\x, on the home drive', { ...WINDOWS, GLIDEPATH_HOME: '\\data\\glidepath' }, 'C:\\data\\glidepath'],
  ["Git Bash's /c/Users/u/glidepath", { ...WINDOWS, GLIDEPATH_HOME: '/c/Users/u/glidepath' }, 'C:/Users/u/glidepath'],
  ["Git Bash's /d/data, on another drive", { ...WINDOWS, GLIDEPATH_HOME: '/d/data' }, 'D:/data'],
]
for (const [what, env, home] of WINDOWS_HOMES) {
  test(`Windows: finds the glidepath home from ${what}`, async ($, on) => {
    const { runs, clock, reads } = stub(on, { env })
    await $.session.start(start('desktop'))
    await clock.advance(1)
    expect(slashed(reads[0])).toMatch(ending(`${home}/runtime.json`))
    expect(runs[0]?.init?.env).toEqual({ GLIDEPATH_HOME: home })
  })
}

test('Windows: joins runtime.json with a backslash', async ($, on) => {
  const { clock, reads } = stub(on, { env: WINDOWS })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  // The test kit keeps this path as is on Windows; elsewhere it only puts the
  // cwd before it.
  expect(reads[0]).toMatch(/C:\\Users\\u\\\.glidepath\\runtime\.json$/)
})

test('Windows: asks for USERPROFILE, not HOME', async ($, on) => {
  const { runs, clock, reads } = stub(on, { env: { OS: 'Windows_NT', HOME: '/c/Users/u' } })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(reads.length).toBe(0)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /USERPROFILE is unset/ })).toBeDefined()
})

test('Windows: says why a network GLIDEPATH_HOME cannot be read', async ($, on) => {
  const { runs, clock } = stub(on, {
    env: { ...WINDOWS, GLIDEPATH_HOME: '\\\\srv\\share\\glidepath' },
    runtime: null,
    denied: 'a network location is not reached from here',
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(
    await ui.find({ type: 'Text', text: /network path \\\\srv\\share\\glidepath: set GLIDEPATH_HOME to a local folder/ }),
  ).toBeDefined()
})

test('resolves GLIDEPATH_HOME like Python on each platform', async () => {
  const posix = (home: string | undefined) => ({ windows: false, home })
  const windows = (home: string | undefined) => ({ windows: true, home })
  expect(resolveHome(undefined, posix('/home/u/'))).toBe('/home/u/.glidepath')
  expect(resolveHome('C:\\x', posix('/home/u'))).toBe('/home/u/C:\\x')
  expect(() => resolveHome('~\\x', posix('/home/u'))).toThrow(/only ~ and ~\/ are expanded/)
  expect(resolveHome(undefined, windows('C:\\'))).toBe('C:\\.glidepath')
  expect(resolveHome('~', windows('C:\\Users\\u'))).toBe('C:\\Users\\u')
  expect(resolveHome('\\\\srv\\share\\x', windows('C:\\Users\\u'))).toBe('\\\\srv\\share\\x')
  expect(resolveHome('//srv/share/x', windows('C:\\Users\\u'))).toBe('//srv/share/x')
  expect(resolveHome('\\x', windows('\\\\srv\\share\\Users\\u'))).toBe('\\\\srv\\share\\x')
  expect(resolveHome('\\x', windows(undefined))).toBe('\\x')
  expect(resolveHome('C:\\x', windows(undefined))).toBe('C:\\x')
  expect(() => resolveHome('C:x', windows('C:\\Users\\u'))).toThrow(/relative to a drive's current folder/)
  expect(() => resolveHome('~other\\x', windows('C:\\Users\\u'))).toThrow(/only ~, ~\/ and ~\\ are expanded/)
})

// A GLIDEPATH_HOME set from Git Bash (export GLIDEPATH_HOME=/c/Users/me/x) names a
// drive: the CLI reads it so (config.windows_home), and the mod must find the
// same folder, or the band reads a runtime.json the CLI never wrote.
test("Windows: reads Git Bash's /c/Users/x as C:/Users/x, as the CLI does", async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  // config.windows_home's results for the same values (tests/test_statusline.py, TildeHome), separators aside.
  const cli: [string, string][] = [
    ['/d/tempo', 'D:\\tempo'],
    ['/C/Users/me/x', 'C:\\Users\\me\\x'],
    ['/c', 'C:\\'],
    ['/e/', 'E:\\'],
  ]
  for (const [given, folder] of cli) {
    expect(resolveHome(given, windows('C:\\Users\\me')).replace(/\//g, '\\')).toBe(folder)
  }
  // Not a drive: refused, as config.windows_home refuses them (TildeHome).
  for (const given of ['/cc/x', '/1/x', '/c\\x', '/\u00e9/x']) {
    expect(() => resolveHome(given, windows('C:\\Users\\me'))).toThrow(/has no drive/)
  }
  expect(resolveHome('/d/data', windows(undefined))).toBe('D:/data') // a drive of its own needs no home
  expect(fromGitBash('/z/')).toBe('Z:/')
  expect(fromGitBash('/data')).toBe('/data')
  expect(resolveHome('/c/Users/x', { windows: false, home: '/home/u' })).toBe('/c/Users/x') // macOS, Linux: as given
})

test('runs Python with the flags runtime.json lists', async ($, on) => {
  const runtime = JSON.stringify({
    python: 'C:\\Python313\\python.exe',
    args: ['-I', '-S', '-X', 'utf8'],
    entry: 'C:\\repo\\bin\\glidepath',
  })
  const { runs, clock } = stub(on, { env: WINDOWS, runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs[0]?.argv).toEqual([
    'C:\\Python313\\python.exe', '-I', '-S', '-X', 'utf8', 'C:\\repo\\bin\\glidepath',
    'statusline', '--source', 'mod', '--format', 'json',
  ])
})

test('refuses runtime.json args that are not a list of strings', async ($, on) => {
  const runtime = JSON.stringify({ python: '/usr/bin/python3', args: '-I -S', entry: '/repo/bin/glidepath' })
  const { runs, clock } = stub(on, { runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(0)
  const ui = await $.ui.mount(band('desktop'))
  expect(await ui.find({ type: 'Text', text: /args is not a list of strings/ })).toBeDefined()
})

test('reads a runtime.json saved with a byte order mark', async ($, on) => {
  const runtime = `\uFEFF${JSON.stringify({ python: '/usr/bin/python3', entry: '/repo/bin/glidepath' })}`
  const { runs, clock } = stub(on, { runtime })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs[0]?.argv.slice(0, 4)).toEqual(['/usr/bin/python3', '-I', '-S', '/repo/bin/glidepath'])
})

test('follows mod.refresh_s from the CLI', async ($, on) => {
  const { runs, clock } = stub(on, { run: () => ran(JSON.stringify({ ...CLI, refresh_s: 300 })) })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(60_000)
  expect(runs.length).toBe(1)
  await clock.advance(240_000)
  expect(runs.length).toBe(2)
})

// Review 15: Git Bash, which runs the statusLine on Windows, rewrites a
// GLIDEPATH_HOME rooted with a slash and no drive to a folder under its own
// install (C:\Program Files\Git\srv\glidepath) before Python sees it, while the
// mod hands Python the value as given: the band and the line would use 2
// folders. The CLI refuses the same values (tests/test_config.py, NoDriveHome).
// The example is on the home's drive, where older versions put the folder.
const PROFILES: [string, Record<string, string>, string][] = [
  ['a profile on C:', WINDOWS, 'C:'],
  ['a profile on D:', { ...WINDOWS, USERPROFILE: 'D:\\Users\\u' }, 'D:'],
]
for (const value of ['/srv/glidepath', '/glidepath', '/tmp/x', '/']) {
  for (const [what, env, drive] of PROFILES) {
    test(`Windows: refuses the drive-less GLIDEPATH_HOME ${value} with ${what}, naming the fix`, async ($, on) => {
      const { runs, clock, reads } = stub(on, { env: { ...env, GLIDEPATH_HOME: value } })
      await $.session.start(start('desktop'))
      await clock.advance(1)
      expect(reads.length).toBe(0)
      expect(runs.length).toBe(0)
      const ui = await $.ui.mount(band('desktop'))
      const why =
        `GLIDEPATH_HOME ${value} has no drive; Git Bash rewrites such a path to a folder under its own install. ` +
        `Write it with a drive, e.g. ${drive}${value}, or as ~/...`
      expect(await ui.find({ type: 'Text', text: ending(why) })).toBeDefined()
    })
  }
}

test("the drive-less example takes the home's drive, else C:", async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  const example = (drive: string) =>
    'GLIDEPATH_HOME /srv/glidepath has no drive; Git Bash rewrites such a path to a folder under its own install. ' +
    `Write it with a drive, e.g. ${drive}/srv/glidepath, or as ~/...`
  expect(() => resolveHome('/srv/glidepath', windows('D:\\Users\\me'))).toThrow({ message: example('D:') })
  const unc = '\\\\srv\\share'
  expect(() => resolveHome('/srv/glidepath', windows(`${unc}\\me`))).toThrow({ message: example(unc) })
  expect(() => resolveHome('/srv/glidepath', windows('\\Users\\me'))).toThrow({ message: example('C:') })
  expect(() => resolveHome('/srv/glidepath', windows(undefined))).toThrow({ message: example('C:') })
  // C:x as the CLI refuses it, word for word (tests/test_config.py, NoDriveHome).
  for (const given of ['C:x', 'c:glidepath', 'D:']) {
    expect(() => resolveHome(given, windows('C:\\Users\\me'))).toThrow({
      message: `GLIDEPATH_HOME ${given} is relative to a drive's current folder; use an absolute path`,
    })
  }
})

test('the drive-less rule is Windows only, and UNC, \\x and Git Bash drives still work', async () => {
  const windows = (home: string | undefined) => ({ windows: true, home })
  expect(resolveHome('/srv/glidepath', { windows: false, home: '/home/u' })).toBe('/srv/glidepath')
  expect(resolveHome('//srv/share/x', windows('C:\\Users\\u'))).toBe('//srv/share/x')
  expect(resolveHome('/\\srv\\share\\x', windows('C:\\Users\\u'))).toBe('/\\srv\\share\\x')
  expect(resolveHome('\\srv\\glidepath', windows('D:\\Users\\u'))).toBe('D:\\srv\\glidepath')
  expect(resolveHome('/d/srv', windows('C:\\Users\\u'))).toBe('D:/srv')
})

// Review 15: on Windows a pinned python.exe that is gone does not fail to
// start. cmd.exe runs the command instead, and its message names neither
// Python nor the fix: the band said "glidepath: operable program or batch file.".
const GONE = 'C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python311\\python.exe'
const GONE_RUNTIME = JSON.stringify({ python: GONE, args: ['-I', '-S', '-X', 'utf8'], entry: 'C:\\repo\\bin\\glidepath' })
const NOT_RECOGNIZED =
  `'${GONE}' is not recognized as an internal or external command,\r\n` + 'operable program or batch file.\r\n'
const NOT_FOUND = /Python not found at C:\\Users\\u\\.*python\.exe: run `glidepath install --apply`/

function failed(exitCode: number, stderr: string): () => { value: ProcessRunResult } {
  return () => ({ value: { exitCode, stdout: '', stderr, isStdoutTruncated: false, isStderrTruncated: false } })
}

const MISSING: [string, number, string][] = [
  ['its folder still there (exit 9009)', 9009, NOT_RECOGNIZED],
  ['its folder gone', 1, 'The system cannot find the path specified.\r\n'],
]
for (const [what, exitCode, stderr] of MISSING) {
  test(`Windows: names a missing interpreter, ${what}`, async ($, on) => {
    const { runs, clock, reads } = stub(on, {
      env: WINDOWS,
      runtime: GONE_RUNTIME,
      exists: () => false,
      run: failed(exitCode, stderr),
    })
    await $.session.start(start('desktop'))
    await clock.advance(1)
    expect(runs.length).toBe(1)
    const ui = await $.ui.mount(band('desktop'))
    expect(await ui.find({ type: 'Text', text: NOT_FOUND })).toBeDefined()
    // runtime.json is read again on the next refresh: a reinstall needs no new session.
    await clock.advance(60_000)
    expect(reads.length).toBe(2)
  })
}

test("Windows: keeps cmd.exe's whole message when exists() cannot tell", async ($, on) => {
  // No fs.exists answer: the call rejects, which counts as present (a Store alias it cannot stat).
  const { clock, reads } = stub(on, { env: WINDOWS, runtime: GONE_RUNTIME, run: failed(9009, NOT_RECOGNIZED) })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const ui = await $.ui.mount(band('desktop'))
  const whole = /not recognized as an internal or external command, operable program or batch file\.$/
  expect(await ui.find({ type: 'Text', text: whole })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Python not found/ })).toBeUndefined()
  await clock.advance(60_000)
  expect(reads.length).toBe(1)
})

// -- the band ---------------------------------------------------------------

function win(over: Partial<Win> & Pick<Win, 'id'>): Win {
  const week = over.id === 'wk'
  return {
    name: week ? 'Week' : '5h',
    short: week ? 'Wk' : '5h',
    kind: 'run',
    pct: week ? 48 : 55,
    used: week ? 48 : 55.3,
    projected: week ? 96 : 95,
    reset: week ? 'Fri' : '16:30',
    reset_ts: week ? 1_792_123_200 : 1_791_991_800,
    phrase: null,
    sentence: week ? 'Week: 48% used, resets Fri 04:00. On pace, about 96% by then.' : '5-hour: 55% used, resets 16:30. On pace, about 95% by then.',
    ...over,
  }
}

const phrase = (p: Partial<Phrase>): Phrase => ({
  pre: '',
  num: '',
  post: '',
  narrow: p.post ?? '',
  tone: 'inactive',
  attention: false,
  calm: false,
  ...p,
})

// The pace, the same kind for both windows (band.phrase).
const SLOWER = phrase({ num: '2x', post: ' slower', tone: 'error', attention: true })
const FASTER = phrase({ num: '2.7x', post: ' faster', tone: 'success' })
const ROUGH = phrase({ pre: '~', num: '2x', post: ' faster', tone: 'success' })
const ON_PACE = phrase({ pre: 'On pace', tone: 'inactive', calm: true })
const BACK = phrase({ pre: 'Back ', num: 'Fri 04:00', tone: 'error', attention: true })
const ROWS: Detail[] = [
  { label: 'Used', value: '70%', tone: 'text' },
  { label: 'Pace', value: '2x slower', tone: 'error' },
  { label: 'Runs out', value: '15:40, 50 min early', tone: 'error' },
  { label: 'Resets', value: '16:30', tone: 'text' },
]
const WEEK_ROWS: Detail[] = [
  { label: 'Used', value: '48%', tone: 'text' },
  { label: 'Pace', value: '2.7x faster', tone: 'success' },
  { label: 'Will go unused', value: '40%', tone: 'success' },
  { label: 'Resets', value: 'Fri 04:00', tone: 'text' },
]

function cli(windows: Win[], extra: Record<string, unknown> = {}) {
  return ran(JSON.stringify({ ...CLI, windows, updated_ts: 1_791_964_500, now: 1_791_964_680, ...extra }))
}

function sized(surface: RenderSurface, bodyColumns: number): MountTarget {
  const target = band(surface)
  return { ...target, props: { ...(target.props as object), bodyColumns } } as MountTarget
}

async function drawn($: Parameters<TestBody>[0], on: StubOn, windows: Win[], columns = 115, options: Options = {}) {
  const { clock, runs } = stub(on, { run: () => cli(windows), ...options })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  return { ui: await $.ui.mount(sized('desktop', columns)), clock, runs }
}

test('draws a bar, the percent and the reset per window, and no words while on pace', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk' })])
  const bars = await ui.findAll({ type: 'Svg' })
  expect(bars.length).toBe(2)
  expect(bars[0]?.props).toMatchObject({ width: 64, height: 14 })
  // An image, not a frame: a frame paints a white box where the app's and the system's themes differ.
  expect(bars[0]?.props.isInteractive).toBeUndefined()
  expect(String(bars[0]?.props.alt)).toMatch(/^5-hour: 55% used/)
  expect(String(bars[0]?.props.source)).toContain('<title>5-hour: 55% used, resets 16:30.')
  expect(await ui.find({ type: 'Text', text: '55%' })).toMatchObject({ props: { color: 'text', weight: 'medium' } })
  expect(await ui.find({ type: 'Text', text: 'resets 16:30' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'resets Fri' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /faster|pace/ })).toBeUndefined()
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /drawn by Claude Code/ })).toBeDefined()
})

test('a window that needs you says so in color, and the other percent dims', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER }), win({ id: 'wk' })])
  expect(await ui.find({ type: 'Text', text: '2x slower' })).toMatchObject({
    props: { color: 'error', weight: 'medium' },
    children: ['', { type: 'Text', props: { weight: 'semibold', color: 'error' }, children: ['2x'] }, ' slower'],
  })
  expect(await ui.find({ type: 'Text', text: '70%' })).toMatchObject({ props: { color: 'text' } })
  expect(await ui.find({ type: 'Text', text: '48%' })).toMatchObject({ props: { color: 'inactive', weight: 'regular' } })
})

test('both windows say their pace, green with room to spare, grey on pace', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', phrase: ROUGH }), win({ id: 'wk', phrase: FASTER })])
  expect(await ui.find({ type: 'Text', text: '~2x faster' })).toMatchObject({ props: { color: 'success' } })
  expect(await ui.find({ type: 'Text', text: '2.7x faster' })).toMatchObject({ props: { color: 'success' } })
  expect(await ui.find({ type: 'Text', text: 'resets 16:30' })).toBeDefined()
})

test('on pace, both windows say so in grey', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', phrase: ON_PACE }), win({ id: 'wk', phrase: ON_PACE })])
  expect((await ui.findAll({ type: 'Text', text: 'On pace' })).length).toBe(2)
  expect(await ui.find({ type: 'Text', text: 'On pace' })).toMatchObject({ props: { color: 'inactive', weight: 'regular' } })
})

test('narrow: resets go to Details, bars shrink, one quiet window keeps its words', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', phrase: ON_PACE }), win({ id: 'wk', phrase: FASTER })], 80)
  expect(await ui.find({ type: 'Text', text: /resets/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'On pace' })).toBeDefined() // the large bars: both
  expect(await ui.find({ type: 'Text', text: '2.7x faster' })).toBeDefined()
  const narrow = await $.ui.mount(sized('desktop', 55)) // the small bars: one, green before grey
  expect((await narrow.find({ type: 'Svg' }))?.props.width).toBe(44)
  expect(await narrow.find({ type: 'Text', text: '2.7x faster' })).toBeDefined()
  expect(await narrow.find({ type: 'Text', text: 'On pace' })).toBeUndefined()
  expect(await narrow.find({ type: 'Button', text: '⋯' })).toBeDefined() // below 56, a short label
})

test('beside a window that needs you, the quiet one keeps only its percent with the small bars', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER }), win({ id: 'wk', phrase: FASTER })], 55)
  expect(await ui.find({ type: 'Text', text: '2x slower' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /faster/ })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '48%' })).toMatchObject({ props: { color: 'inactive' } })
  const mid = await $.ui.mount(sized('desktop', 80))
  expect(await mid.find({ type: 'Text', text: '2.7x faster' })).toBeDefined()
})

test('tiny: Wk, only the words that need you, the calm window beside them keeps its name and percent', async ($, on) => {
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER }), win({ id: 'wk', phrase: FASTER })]
  const { ui } = await drawn($, on, windows, 48)
  expect(await ui.find({ type: 'Button', text: '⋯' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'Wk' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '2x slower' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /faster/ })).toBeUndefined()
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(1)
  expect(await ui.find({ type: 'Text', text: '48%' })).toBeDefined()
  const wide = await $.ui.mount(sized('desktop', 52))
  expect((await wide.findAll({ type: 'Svg' })).length).toBe(2)
})

test('a window that needs you at 40 cells: its words, no button', async ($, on) => {
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER }), win({ id: 'wk', kind: 'maxed', pct: 100, used: 100, phrase: BACK })]
  const { ui } = await drawn($, on, windows, 40)
  expect(await ui.find({ type: 'Text', text: 'Back Fri 04:00' })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
})

test('narrow, 2 windows that need you get the room: short words, and below 56 no bars', async ($, on) => {
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER }), win({ id: 'wk', kind: 'maxed', pct: 100, used: 100, phrase: BACK })]
  const { ui } = await drawn($, on, windows, 60)
  expect(await ui.find({ type: 'Text', text: '2x slower' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: 'Back Fri 04:00' })).toBeDefined()
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(2)
  const tiny = await $.ui.mount(sized('desktop', 55))
  expect(await tiny.find({ type: 'Svg' })).toBeUndefined()
  expect(await tiny.find({ type: 'Button', text: '⋯' })).toBeDefined()
})

test('a window still waiting beside one with data says so', async ($, on) => {
  const wait = win({ id: 'wk', kind: 'wait', pct: null, used: 0, projected: null, reset: null, reset_ts: null })
  const { ui } = await drawn($, on, [win({ id: 'h5' }), wait])
  expect(await ui.find({ type: 'Text', text: 'waiting' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Waiting for/ })).toBeUndefined()
})

test('the width thresholds', async () => {
  expect(layout(112)).toEqual({ halves: true, W: 64, tiny: false, bare: false })
  expect(layout(111)).toEqual({ halves: false, W: 64, tiny: false, bare: false })
  expect(layout(71)).toEqual({ halves: false, W: 44, tiny: false, bare: false })
  expect(layout(51)).toEqual({ halves: false, W: 44, tiny: true, bare: false })
  expect(layout(43)).toEqual({ halves: false, W: 44, tiny: true, bare: true })
})

test('Details opens a card per window: name, percent and pace on top, then label and value rows', async ($, on) => {
  const windows = [win({ id: 'h5', kind: 'dry', pct: 70, used: 70, phrase: SLOWER, details: ROWS }), win({ id: 'wk', phrase: FASTER, details: WEEK_ROWS })]
  const { ui } = await drawn($, on, windows)
  await ui.press({ key: 'details' })
  const tree = JSON.stringify(await ui.drawn())
  expect(tree.split('"borderStyle":"round"').length - 1).toBe(2) // a card each
  expect(await ui.find({ type: 'Text', text: '5 hours' })).toBeDefined()
  expect((await ui.findAll({ type: 'Text', text: '70%' })).some((t) => t.props.size === 'heading')).toBe(true)
  // Used and Pace are the card's head, not rows.
  expect(await ui.find({ type: 'Text', text: 'Used' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'Pace' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '15:40, 50 min early' })).toMatchObject({ props: { color: 'error' } })
  expect(await ui.find({ type: 'Text', text: 'Fri 04:00' })).toMatchObject({ props: { color: 'text' } })
  expect((await ui.findAll({ type: 'Text', text: 'Resets' })).length).toBe(2) // a row in each card
  // 3 minutes old: no line for it (only from 5 minutes, stale()).
  expect(await ui.find({ type: 'Text', text: /^Updated/ })).toBeUndefined()
  expect(await ui.find({ type: 'Button', text: 'Details' })).toBeDefined() // the same label: the row never shifts
  expect(tree).toContain('"width":"50%"') // side by side
  const narrow = await $.ui.mount(sized('desktop', 71)) // one under the other
  expect(JSON.stringify(await narrow.drawn())).not.toContain('"width":"50%"')
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: /Updated/ })).toBeUndefined()
})

test("a row's note follows its value, quieter", async ($, on) => {
  const rows: Detail[] = [
    { label: 'At reset', value: 'about 60%', tone: 'text', note: '40% unused', note_tone: 'success' },
    { label: 'Resets', value: 'Fri 04:00', tone: 'text', note: '100% Sat 06:00' },
  ]
  const { ui } = await drawn($, on, [win({ id: 'h5' }), win({ id: 'wk', details: rows })])
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: '· 40% unused' })).toMatchObject({ props: { color: 'success' } })
  expect(await ui.find({ type: 'Text', text: '· 100% Sat 06:00' })).toMatchObject({ props: { color: 'inactive' } })
})

test('a card keeps the CLI order of rows; an older CLI shows its sentence', async ($, on) => {
  const mixed: Detail[] = [{ label: 'Resets', value: '16:30', tone: 'text' }, { label: 'At reset', value: 'about 90%', tone: 'text' }]
  const { ui } = await drawn($, on, [win({ id: 'h5', details: mixed }), win({ id: 'wk' })])
  await ui.press({ key: 'details' })
  const order = JSON.stringify(await ui.drawn())
  expect(order.indexOf('"At reset"')).toBeLessThan(order.indexOf('"Resets"'))
  expect(await ui.find({ type: 'Text', text: 'Summary' })).toBeDefined() // the week: no rows from this CLI
  expect(await ui.find({ type: 'Text', text: /^Week: 48% used/ })).toBeDefined()
})

test('details rows that do not read are left out, not the band', async ($, on) => {
  const bad = [{ label: 'Used', value: 5 }, null, { label: 'Resets', value: '16:30', tone: 'text' }] as unknown as Detail[]
  const { ui } = await drawn($, on, [win({ id: 'h5', details: bad }), win({ id: 'wk' })])
  await ui.press({ key: 'details' })
  expect(await ui.find({ type: 'Text', text: 'Resets' })).toBeDefined()
  expect(await ui.find({ type: 'Svg' })).toBeDefined()
})

// The 5 hours from 11:30 to 16:30 (chart.build), now 14:40.
const T0 = 1_791_976_200
const CHART: Chart = {
  t0: T0,
  t1: T0 + 5 * 3600,
  now: T0 + 3 * 3600 + 600,
  actual: [[T0, 0], [T0 + 3600, 20], [T0 + 3 * 3600 + 600, 70]],
  forecast: [[T0 + 3 * 3600 + 600, 70], [T0 + 4 * 3600, 100], [T0 + 5 * 3600, 100]],
  fan: null,
  ghosts: [[[T0 + 2 * 3600, 40], [T0 + 5 * 3600, 90]]],
  grid: [T0 + 1800, T0 + 5400, T0 + 9000, T0 + 12600, T0 + 16200],
  labels: [[T0 + 1800, '12'], [T0 + 5400, '13'], [T0 + 9000, '14'], [T0 + 12600, '15'], [T0 + 16200, '16']],
}

test('Details draws a chart in each card, under its head', async ($, on) => {
  const windows = [win({ id: 'h5', details: ROWS, chart: CHART }), win({ id: 'wk', details: WEEK_ROWS, chart: CHART })]
  const { ui } = await drawn($, on, windows)
  expect((await ui.findAll({ type: 'Svg' })).length).toBe(2) // the bars; the charts are in Details
  await ui.press({ key: 'details' })
  const charts = (await ui.findAll({ type: 'Svg' })).filter((x) => String(x.props.source).includes("class='ctc'"))
  expect(charts.length).toBe(2)
  // No size: the card's slot sets it, the picture scales down to it.
  expect(charts[0]?.props.width).toBeUndefined()
  expect(charts[0]?.props.isInteractive).toBeUndefined()
  expect(String(charts[0]?.props.alt)).toBe('5 hours: what was used over the window and the forecast to its reset')
  expect(String(charts[1]?.props.alt)).toMatch(/^Week: /)
  const tree = JSON.stringify(await ui.drawn())
  expect(tree.indexOf("class='ctc'")).toBeGreaterThan(tree.indexOf('"5 hours"'))
  expect(tree.indexOf("class='ctc'")).toBeLessThan(tree.indexOf('"Runs out"'))
})

test('a broken chart, or none from an older CLI, leaves the card without one', async ($, on) => {
  const broken = { ...CHART, actual: [], t1: CHART.t0 } as Chart
  const { ui } = await drawn($, on, [win({ id: 'h5', details: ROWS, chart: broken }), win({ id: 'wk' })])
  await ui.press({ key: 'details' })
  expect((await ui.findAll({ type: 'Svg' })).some((x) => String(x.props.source).includes("class='ctc'"))).toBe(false)
  expect(await ui.find({ type: 'Text', text: 'Runs out' })).toBeDefined()
})

test('a chart that does not read is none', () => {
  expect(chartOf(CHART)).toBe(CHART)
  expect(chartOf(undefined)).toBeNull()
  expect(chartOf({ ...CHART, t1: CHART.t0 })).toBeNull()
  expect(chartOf({ ...CHART, actual: [] })).toBeNull()
  expect(chartOf({ ...CHART, actual: [[1, 'x']] })).toBeNull()
  expect(chartOf({ ...CHART, fan: [1] })).toBeNull()
  expect(chartOf({ ...CHART, fan: [[], []] })).toBeNull()
  expect(chartOf({ ...CHART, labels: [[1, 2]] })).toBeNull()
  expect(chartOf({ ...CHART, nights: [[1, 'x']] })).toBeNull()
  expect(chartOf({ ...CHART, nights: [[CHART.t0, CHART.t0 + 3600]] })).not.toBeNull()
  expect(chartOf({ ...CHART, fan: [CHART.forecast, CHART.forecast] })).not.toBeNull()
})

test('the chart draws what was used, the forecast in the pace color, earlier forecasts and the grid', () => {
  const svg = chartSvg(CHART, 300, 'alt <x>')
  expect(svg).toMatch(/^<svg xmlns='http:\/\/www.w3.org\/2000\/svg' class='ctc' width='300' height='64'/)
  expect(svg).toContain('<title>alt &lt;x&gt;</title>')
  expect(svg.match(/<line class='g'/g)?.length).toBe(5)
  expect(svg).toMatch(/<path class='ar' d='M1,50L.*L1,50Z'/) // the area, from the start at 0
  expect(svg).toMatch(/<path class='ac'/)
  expect(svg).toMatch(/<path class='gh'/)
  expect(svg).toMatch(/<path class='fc'/)
  expect(svg).not.toMatch(/class='fn'/)
  expect(svg.match(/<text /g)?.length).toBe(5)
  expect(svg).toMatch(/<text x='[\d.]+' y='62' text-anchor='middle'>12<\/text>/)
  expect(chartSvg(CHART, 300, 'a', 'error')).toMatch(/<path class='fx'/)
  expect(chartSvg(CHART, 300, 'a', 'warning')).toMatch(/<path class='fy'/)
  // The fan: from the slower line out and back along the faster one.
  const lo: [number, number][] = [[CHART.now, 70], [CHART.t1, 90]]
  const hi: [number, number][] = [[CHART.now, 70], [CHART.t0 + 4 * 3600, 100], [CHART.t1, 100]]
  expect(chartSvg({ ...CHART, fan: [lo, hi] }, 300, 'a')).toMatch(/<path class='fn' d='M187.8,19.2L296,10.4L296,6L237,6L187.8,19.2Z'\/>/)
  // The end ring in the forecast's color; now a mid-tone grey, never black or white.
  expect(chartSvg(CHART, 300, 'a', 'error')).toContain("<circle class='end ex'")
  expect(svg).toContain("<circle class='end'")
  expect(svg).not.toMatch(/#141413|#ecebe5/)
  // A dot per reading while there are few.
  expect(svg.match(/<circle class='dt'/g)?.length).toBe(3)
  const many = { ...CHART, actual: Array.from({ length: 40 }, (_, i) => [T0 + i * 60, i] as [number, number]) }
  expect(chartSvg(many, 300, 'a').match(/<circle class='dt'/g)?.length).toBe(1)
  // Labels at the edges stay inside.
  const edge = chartSvg({ ...CHART, labels: [[CHART.t0, '11'], [CHART.t1, '16']] }, 300, 'a')
  expect(edge).toContain("<text x='1' y='62' text-anchor='start'>11<")
  expect(edge).toContain("<text x='299' y='62' text-anchor='end'>16<")
})

test('a run-out before the reset is marked: the rest tinted, a dashed line, a dot on the limit, its time', () => {
  const svg = chartSvg({ ...CHART, out: CHART.t0 + 4 * 3600, out_label: '15:30' }, 300, 'a', 'error')
  expect(svg).toContain("<rect class='oz rx' x='237' y='6' width='59' height='44'/>")
  expect(svg).toContain("<line class='ol rx' x1='237' y1='6' x2='237' y2='50'/>")
  expect(svg).toContain("<circle class='rx' cx='237' cy='6' r='2.4'/>")
  expect(svg).toContain("<text class='ot rx' x='241' y='16' text-anchor='start'>15:30</text>")
  // Yellow when it only may; the time on the left when there is no room right.
  const may = chartSvg({ ...CHART, out: CHART.t1 - 600, out_label: '16:20' }, 300, 'a', 'warning')
  expect(may).toMatch(/<text class='ot ry' x='[\d.]+' y='16' text-anchor='end'>16:20<\/text>/)
  expect(chartSvg(CHART, 300, 'a', 'error')).not.toContain("class='oz")
  expect(chartOf({ ...CHART, out: CHART.t1 + 1 })).toBeNull()
})

test("the week's nights are pale bands behind the lines", () => {
  const svg = chartSvg({ ...CHART, nights: [[CHART.t0, CHART.t0 + 1800], [CHART.t0 + 3 * 3600, CHART.t0 + 4 * 3600]] }, 300, 'a')
  expect(svg.match(/<rect class='nt'/g)?.length).toBe(2)
  expect(svg).toContain("<rect class='nt' x='1' y='6' width='29.5' height='44'/>")
  // A thin rounded strip under the axis marks each night.
  expect(svg).toContain("<rect class='ns' x='1' y='52' width='29.5' height='2' rx='1'/>")
  expect(svg.indexOf("class='nt'")).toBeLessThan(svg.indexOf("<line class='g'"))
  expect(chartSvg(CHART, 300, 'a')).not.toContain("class='nt'") // the 5 hours, or an older CLI
})

test('a chart label with no room after the one before is left out, as in the menu bar', () => {
  // A narrow card (220 px): a day's name 2 hours after another's has no room.
  const week = { ...CHART, t1: CHART.t0 + 7 * 86400, labels: [[CHART.t0 + 4 * 3600, 'Tue'], [CHART.t0 + 6 * 3600, 'Wed'], [CHART.t0 + 30 * 3600, 'Thu']] as [number, string][] }
  const svg = chartSvg(week, 220, 'a')
  expect(svg.match(/<text [^>]*>[A-Za-z]+</g)?.map((t) => t.slice(-4, -1))).toEqual(['Tue', 'Thu'])
  // A Friday 06:00 reset: the CLI lists the whole days, then the first
  // Friday's 18 hours; at 160 px that name has no room beside Saturday's.
  const whole = ['Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu']
  const labels = [...whole.map((n, i) => [CHART.t0 + 18 * 3600 + i * 86400 + 43200, n]), [CHART.t0 + 9 * 3600, 'Fri']]
  const fri = { ...week, labels: labels as [number, string][] }
  const names = (W: number) => chartSvg(fri, W, 'a').match(/<text [^>]*>[A-Za-z]+</g)?.map((t) => t.slice(-4, -1))
  expect(names(160)?.[0]).toBe('Sat')
  expect(names(160)).not.toContain('Fri')
  expect(names(300)).toEqual([...whole, 'Fri'])
})

test('waiting for the first reading', async ($, on) => {
  const waiting = (id: 'h5' | 'wk') =>
    win({ id, kind: 'wait', pct: null, used: 0, projected: null, reset: null, reset_ts: null, sentence: 'waiting' })
  const { ui } = await drawn($, on, [waiting('h5'), waiting('wk')])
  expect(await ui.find({ type: 'Text', text: 'Waiting for the first reading' })).toBeDefined()
  expect(await ui.find({ type: 'Button' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /%/ })).toBeUndefined()
  expect(String((await ui.find({ type: 'Svg' }))?.props.source)).toContain('<animate')
  // Wide, the windows are not halves while waiting, so they keep a gap between them and the words.
  expect(JSON.stringify(await ui.drawn())).toContain('"columnGap":"md"')
})

test('an older CLI without windows, or broken ones, gets the text line', async ($, on) => {
  const { ui } = await drawn($, on, [win({ id: 'h5' })])
  expect(await ui.find({ type: 'Svg' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: '5h 62% ×1.4▲ →88% @10:20' })).toBeDefined()
})

test('the bars draw what each kind means', async () => {
  const svg = (over: Partial<Win>) => barSvg(win({ id: 'h5', ...over }), 64)
  expect(svg({})).toMatch(/class='ctb-bf'/)
  expect(svg({})).not.toMatch(/class='ctb-b[wdmx]'|linearGradient/)
  expect(svg({ kind: 'dry' })).toMatch(/class='ctb-bx'.*class='ctb-bw'\/>/)
  const soft = svg({ kind: 'dry', soft: true })
  expect(soft).toMatch(/linearGradient id='ctb[0-9a-f]{5}'.*stop-color:var\(--r\)/)
  expect(soft).toMatch(/class='ctb-bw' style='opacity:.45'/)
  expect(svg({ kind: 'dry', used: 95 })).not.toMatch(/class='ctb-bx'/) // no room left: the wall alone
  expect(svg({ kind: 'expire', used: 40, projected: 60 })).toMatch(/class='ctb-bd'/)
  expect(svg({ kind: 'maxed', used: 100 })).toMatch(/class='ctb-bm'/)
  expect(svg({ kind: 'idle' })).toMatch(/class='ctb-bi'/)
  expect(svg({ kind: 'idle' })).not.toMatch(/class='ctb-bf'/)
  const rough = svg({ kind: 'rough', used: 8, projected: 58 })
  const id = /linearGradient id='(ctb[0-9a-f]{5})'/.exec(rough)?.[1]
  expect(id).toBeDefined()
  expect(rough).toContain(`fill='url(#${id})'`)
  expect(svg({ projected: null })).not.toMatch(/class='ctb-bf'/)
  expect(svg({ sentence: 'a < b & c' })).toContain('<title>a &lt; b &amp; c</title>')
  expect(svg({}).length).toBeLessThan(131_072)
})

test('says how old the reading is', async () => {
  expect(age(1000, 1030)).toBe('Updated just now.')
  expect(age(1000, 1000 + 180)).toBe('Updated 3 min ago.')
  expect([stale(1000, 1000 + 299), stale(1000, 1000 + 300), stale(null, 1000)]).toEqual([false, true, false])
  expect(age(1000, 1000 + 7200)).toBe('Updated 2 h ago.')
  expect(age(null, 1000)).toBe('')
})

test('runs Python at once when this session reads a new level, and not when nothing moved', async ($, on) => {
  // The stub reads options.limits at each call.
  const options: Options = { run: () => cli([win({ id: 'h5' }), win({ id: 'wk' })]) }
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(15_000)
  expect(runs.length).toBe(1)
  // A tenth of a point: session.measure waits for a whole one, the poll does not.
  options.limits = [{ kind: 'five_hour', percentUsed: 62.1, resetsAt: '2026-10-14T10:20:00.000Z' }, LIMITS[1] as SessionRateLimit]
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  expect(JSON.parse(runs[1]?.init?.stdin ?? '{}').rate_limits.five_hour.used_percentage).toBe(62.1)
  await clock.advance(10_000)
  expect(runs.length).toBe(2)
})

test("redraws when another chat's reading touches the stamp, not after its own", async ($, on) => {
  const stamp = { mtimeMs: 1000, text: '1791964000.000\n' }
  const options: Options = { stamp }
  const { runs, clock } = stub(on, {
    ...options,
    run: () => {
      // This run records a fresh reading: the CLI touches the stamp with its own time.
      stamp.mtimeMs += 1
      stamp.text = '1791964680.000\n'
      return cli([win({ id: 'h5' }), win({ id: 'wk' })])
    },
  })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  await clock.advance(15_000)
  expect(runs.length).toBe(1)
  stamp.mtimeMs = 5000 // another chat
  stamp.text = '1791964700.000\n'
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  await clock.advance(15_000)
  expect(runs.length).toBe(2)
})

test('a failing run is not retried by the poll, only by the refresh timer', async ($, on) => {
  const options: Options = { run: failed(2, 'Traceback: boom\n') }
  const { runs, clock } = stub(on, options)
  await $.session.start(start('desktop'))
  await clock.advance(1)
  expect(runs.length).toBe(1)
  options.limits = [{ kind: 'five_hour', percentUsed: 63, resetsAt: '2026-10-14T10:20:00.000Z' }]
  await clock.advance(5_000)
  expect(runs.length).toBe(2)
  await clock.advance(30_000)
  expect(runs.length).toBe(2)
  await clock.advance(30_000)
  expect(runs.length).toBe(3)
})

test('logs which surfaces the session draws on and whether one asked for the band', async ($, on) => {
  const { clock, writes } = stub(on, { surfaces: [], run: () => cli([win({ id: 'h5' }), win({ id: 'wk' })]) })
  await $.session.start(start('desktop'))
  await clock.advance(1)
  const log = () => writes.filter((w) => w.path.endsWith('/home/u/.glidepath/mod.log')).pop()?.text ?? ''
  expect(log()).toMatch(/sess-1 start: surfaces \[\]\n$/)
  await $.ui.mount(band('desktop'))
  expect(log()).toMatch(/band asked on desktop: drawn\n$/)
  await $.ui.mount(band('terminal'))
  expect(log()).toMatch(/band asked on terminal: not drawn, not in mod.surfaces \[desktop\]\n$/)
})
