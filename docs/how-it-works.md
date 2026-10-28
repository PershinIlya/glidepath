# How glidepath works

Every number glidepath shows, and how it is computed. [Design decisions](design-decisions.md)
explains why each rule is the way it is, with the cases that shaped it.

**5-hour window**

- current rate = least-squares slope, over the trailing 20 minutes, of the
  level path drawn through fresh readings only (0 at the window start, linear
  between readings, one whole-percent step past the last one at its slope,
  then flat): steady work keeps its rate between steps, a burst that ended
  fades out, idle is 0, and spend in a gap you did not observe (claude.ai
  while Claude Code sat idle) is spread over the gap rather than shown as a
  sudden burst;
- allowed rate = max(0, target − used) / hours to reset (target defaults to
  100);
- multiplier = allowed / current;
- projection = used + rate × hours left, capped at 100, with the time it hits
  100 if that comes first;
- uncertainty: the rate is taken as ±25% (±40% while fewer than 3 readings
  carry the fit), so the spread of where you land grows with the time left
  and narrows toward the reset (the chart's fan). "Runs out" is red only when
  it runs out even 25% slower; an early estimate warns only of what holds at
  its low edge ([D-37](design-decisions.md)).

**Weekly window**

- profile `w(h)`: 168 hour-of-week bins, the expected share of a week's spend
  in each hour. Prior from your transcripts (last 45 days, cost-weighted
  tokens), updated from the observed weekly-% spend of each hour with a 14-day
  half-life, off-hours floored at 0.15 of a typical awake hour. The prior
  itself fades toward uniform with the same half-life from the day it was
  built, so a schedule that changed since then takes over. The hours from a
  week's last reading to its reset are learned as idle, at low confidence,
  unless that week was maxed (by any reading, a new session's first one
  included: blocked hours say nothing about your habits);
- `E` = Σ w(h) from now to the reset: the share of a week's spend still ahead;
- on-pace level = target × (1 − E); after an early reset that keeps the same
  reset time, it is paced from that reset instead: target × the share of the
  span from it to the reset that has passed; delta = used − on-pace;
- intensity = what you spent over this week's last 24 awake hours / what
  the profile expected for those hours (at target pace), pulled toward your
  typical week (or the target, before glidepath has seen a whole week): the
  typical week counts as much as a tenth of a week's spend would, so right
  after a reset it leads, and as the week brings evidence it fades. The count
  never reaches back past the reset: the last days of a week (when quota
  that would go unused gets spent) say little about the next one ([D-37](design-decisions.md)).
  Spend in a long gap you did not observe is spread over the gap by the
  profile, so only its expected part counts. Right after install the count
  starts at the first timed reading, and until the level moves the whole
  week so far stands in (nothing, while it is still at 0);
- uncertainty: the intensity is taken as ±50% while the typical week leads,
  narrowing with the week's evidence (±50% × √(0.1 / (0.1 + its share))).
  The forecast's spread grows with the share of the week still ahead, so it
  is widest after a reset and narrow near the next one (the chart's fan);
- projection = used + intensity × target × E; "dry" is where an hour-by-hour
  walk at that intensity crosses 100%; red only when the walk at the low
  edge crosses it too, and an early estimate (the typical week leading)
  warns only of what holds at its low edge;
- multiplier = max(0, target − used) / (intensity × target × E): 0 once you
  are past a target below 100, as for the 5-hour window.

Readings from idle sessions, the 5-minute wobble of `resets_at`, resets that
move later, the `used_percentage` epoch leak (#52326), resets that keep the
same `resets_at`, and DST weeks are all handled; [design-decisions.md](design-decisions.md) explains each
rule.
