// Segment tones of the text line: the classic line's, and the bars line's
// words and bar parts (glidepath/render.py ANSI). An unknown one draws plain.
export type Tone = string

export type Segment = { text: string; tone: Tone }

// What a window's bar draws (glidepath/band.py).
export type BarKind = 'run' | 'rough' | 'dry' | 'expire' | 'maxed' | 'idle' | 'wait'

// The words after the percent: at most one number, drawn bold.
export type Phrase = {
  pre: string
  num: string
  post: string
  // `pre` and `post` in a narrow band ("Out 15:40" for "Runs out 15:40").
  narrow_pre?: string
  narrow: string
  tone: 'inactive' | 'success' | 'warning' | 'error'
  // The window needs you (slower, reached, over a target): its words always
  // show, the other percent dims.
  attention: boolean
  // Grey words ("On pace", "Idle"): the first to go when room is short.
  calm: boolean
}

// A row of Details, the same as the menu bar's menu (band.details).
// `note`: a second, quieter fact on the same row (in `note_tone`, grey by default).
export type Detail = { label: string; value: string; tone: string; note?: string; note_tone?: string }

// A window's chart for its Details card (chart.py): epoch seconds, percents.
export type Chart = {
  t0: number
  t1: number
  now: number
  actual: [number, number][]
  forecast: [number, number][]
  // An early 5-hour estimate: the lines at its rate 40% lower and higher.
  fan: [[number, number][], [number, number][]] | null
  ghosts: [number, number][][]
  // The week's night hours, as pale bands (an older CLI has none).
  nights?: [number, number][]
  // When it runs out before the reset, and its time (an older CLI has none).
  out?: number | null
  out_label?: string | null
  grid: number[]
  labels: [number, string][]
}

// One window of the band (band.window).
export type Win = {
  id: 'h5' | 'wk'
  name: string
  short: string
  kind: BarKind
  pct: number | null
  // A milder run-out (yellow): the red forecast fades.
  soft?: boolean
  used: number
  projected: number | null
  reset: string | null
  reset_ts: number | null
  phrase: Phrase | null
  sentence: string
  details?: Detail[]
  chart?: Chart | null
}

// What `glidepath statusline --format json` prints, plus the mod's own error.
export type View = {
  line: string
  segments: Segment[]
  surfaces: string[]
  refresh_s: number
  windows?: Win[] | null
  // When a reading last changed, and when the CLI ran: epoch seconds.
  updated_ts?: number | null
  now?: number
  error?: string
}

declare module 'claude-code' {
  interface PluginState {
    glidepath: { view: View | null; details: boolean }
  }
}
