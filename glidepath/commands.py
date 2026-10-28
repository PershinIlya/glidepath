"""The on-demand commands: now, report, profile, check, doctor, config.

These fail loud (they are run by hand), unlike the statusline hot path.
"""

import json
import math
import os
import sqlite3
import sys
import time

from . import config, pace, profile, render, weekly
from .config import FIVE_HOUR, SEVEN_DAY
from .store import HEARTBEAT, Store


def _open(create=False):
    try:
        return Store(timeout_ms=2000, create=create)
    except FileNotFoundError:
        return None


def views(st, cfg, now):
    prof = profile.load(st)
    return pace.five_hour(st, now, cfg), weekly.weekly(st, now, cfg, prof=prof), prof


def last_reading(st):
    """Time of the newest reading that carries information, or None. A
    heartbeat repeats an old one (D-06), so an idle open session must not
    make old data look new."""
    return st.db.execute("SELECT MAX(ts) FROM samples WHERE flag != ?", (HEARTBEAT,)).fetchone()[0]


def dur(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return "%dd %02dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


VERDICT_TEXT = {
    "speed_up": "speed up: there is room to go faster",
    "on_pace": "on pace",
    "slow_down": "slow down",
    "idle": "idle",
    "unknown": "not enough data yet",
    "maxed": "limit reached",
    "fresh": "no open window (the next message starts one)",
}


# -- now -------------------------------------------------------------------------------
def public(v):
    """A view without the keys only its chart reads (_weights, _path_k)."""
    return {k: x for k, x in v.items() if not k.startswith("_")}


def cmd_now(args, cfg):
    now = time.time()
    st = _open()
    if st is None:
        print("No data yet: %s does not exist. Is the statusLine installed (glidepath install)?" % config.db_path())
        return 1
    try:
        v5, vw, prof = views(st, cfg, now)
        count = st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        last = last_reading(st)  # the same data age as check (D-20)
        beat = st.db.execute("SELECT MAX(ts) FROM samples WHERE flag = ?", (HEARTBEAT,)).fetchone()[0]
        age = now - last if last else None
        ppw = weekly.exchange_rate(st, now)[0]
        if args.json:
            print(json.dumps({"now": now, "data_age_s": age, "five_hour": public(v5), "seven_day": public(vw),
                              "exchange_rate": ppw}, indent=2, default=str))
            return 0
        from . import band

        segs = band.line(v5, vw, cfg, now)
        print(render.to_ansi(segs, render.use_color(cfg) and sys.stdout.isatty()))
        print()
        _print_5h(v5, cfg)
        print()
        _print_week(vw, ppw, now)
        print()
        print(
            "Data: %d samples, last reading %s, last heartbeat %s. Profile: %s."
            % (count, "%s ago" % dur(age) if age is not None else "never",
               "%s ago" % dur(now - beat) if beat else "never", vw.get("profile_source", "n/a"))
        )
    finally:
        st.close()
    return 0


def _print_5h(v, cfg):
    print("5-hour window: %s" % VERDICT_TEXT.get(v.get("verdict"), v.get("verdict")))
    if not v.get("present"):
        return
    t = v["target"]
    print("  used           %5.1f%%   (target %g%%)" % (v["used"], t))
    print("  resets         %s, in %s" % (render.hm(v["reset_ts"]), dur(v["reset_ts"] - time.time())))
    if v.get("rate") is None:
        # The fit waits for min_span after the window's start, or after its
        # first real reading once a second one follows (D-11).
        span = cfg["rate_min_span_min"]
        if v["rate_since"] > v["start_ts"]:
            why = "less than %s min since work started in this window, at %s" % (span, render.hm(v["rate_since"]))
        else:
            why = "the window opened less than %s min ago" % span
        print("  burn rate      unknown (%s)" % why)
    else:
        how = {"lsq": "least squares over the last %s min" % cfg["rate_window_min"],
               "interpolated": "least squares over the last %s min, mostly interpolated between "
                               "sparse readings" % cfg["rate_window_min"]}
        print("  burn rate      %5.1f %%/h  %s" % (v["rate"], how.get(v["rate_source"], "")))
    print("  allowed rate   %5.1f %%/h  = (%g - %.1f) / %.2f h" % (v["allowed"], t, v["used"], v["remaining_h"]))
    if v.get("multiplier") is not None:
        m = v["multiplier"]
        print(
            "  multiplier     x%.2f     %s"
            % (m, "you can go %.1fx faster" % m if m >= 1 else "spend at %.0f%% of your current rate" % (m * 100))
        )
    if v.get("projected") is not None:
        if v.get("cap_ts"):
            print("  projection     hits 100%% at %s, %s before the reset" % (render.hm(v["cap_ts"]), dur(v["reset_ts"] - v["cap_ts"])))
        else:
            print("  projection     %5.1f%% at reset, ~%.0f%% would expire" % (v["projected"], v["unused_at_reset"]))


def _print_week(v, ppw, now):
    print("Weekly window: %s" % VERDICT_TEXT.get(v.get("verdict"), v.get("verdict")))
    if not v.get("present"):
        return
    t = v["target"]
    print("  used           %5.1f%%   (target %g%%)" % (v["used"], t))
    print("  resets         %s, in %s" % (render.dhm(v["reset_ts"]), dur(v["reset_ts"] - now)))
    print("  profile        %.0f%% of a week's expected spend is still ahead (E = %.3f, %s)" % (v["E"] * 100, v["E"], v["profile_source"]))
    print(
        "  on-pace level  %5.1f%%   you are %+.1f points %s"
        % (v["on_pace"], v["delta"], "ahead (spending faster than your profile)" if v["delta"] > 0 else "behind (surplus)")
    )
    # This week's evidence, pulled toward the typical week (D-37).
    cut = ""
    if v["lookback_cut"]:
        # A lookback cut short at the first timed reading knows nothing
        # until the level moves (D-15).
        cut = ("no timed reading yet" if v["lookback_start"] >= now
               else "the level has not moved since the first timed reading, %s" % render.dhm(v["lookback_start"]))
    if v["evidence"] == "none":
        seen = "no evidence from this week yet (%s)" % (cut or "it has just begun")
    elif v["evidence"] == "window":
        seen = "this week so far: %.1f pts, profile expected %.1f (%s)" % (
            v["lookback_actual"], v["lookback_expected"], cut)
    else:
        seen = "this week's last %s: spent %.1f pts, profile expected %.1f" % (
            dur(now - v["lookback_start"]), v["lookback_actual"], v["lookback_expected"])
    prior = "your typical week" if v["prior_source"] == "typical" else "the target (no typical week yet)"
    print("  intensity      %5.2f     %s" % (v["intensity"], seen))
    print("                           pulled toward %s, %.2f: this week counts %d%%; uncertain by +/-%d%%"
          % (prior, v["intensity_prior"], round(v["intensity_weight"] * 100), round(v["spread"] * 100)))
    if v.get("dry_ts"):
        print("  projection     runs dry ~%s, %s before the reset" % (render.dhm(v["dry_ts"]), dur(v["reset_ts"] - v["dry_ts"])))
    else:
        print("  projection     %5.1f%% at reset, ~%.0f%% will expire" % (v["projected"], v["expire"]))
        if ppw:
            print("                 (~%.1f full 5-hour windows' worth)" % (v["expire"] / ppw))
    if v.get("multiplier") is not None:
        print("  multiplier     x%.2f" % v["multiplier"])
    if ppw:
        print("  exchange rate  one full 5-hour window = %.1f%% of the week" % ppw)


# -- report ----------------------------------------------------------------------------
def cmd_report(args, cfg):
    now = time.time()
    st = _open()
    if st is None:
        print("No data yet.")
        return 1
    try:
        since = now - args.days * 86400
        st.begin()
        st.close_expired(now)
        st.commit()
        ppw = weekly.exchange_rate(st, now, max(28, args.days))[0]
        out = {"days": args.days, "exchange_rate": ppw}
        for w in (SEVEN_DAY, FIVE_HOUR):
            out[w] = _closed(st, w, since)
        if args.json:
            print(json.dumps(out, indent=2))
            return 0
        print("Waste ledger, last %d days (unused = 100 - peak observed level)" % args.days)
        print()
        wk = out[SEVEN_DAY]
        print("Weekly windows closed: %d" % len(wk))
        if wk:
            print("  %-18s %6s %7s  %s" % ("reset", "peak", "unused", "note"))
            for i in wk:
                print("  %-18s %5.0f%% %6.0f%%  %s" % (render.dhm(i["reset_ts"]), i["max_used"], i["unused_at_close"] or 0, _note(i, ppw)))
        f = out[FIVE_HOUR]
        print()
        print("5-hour windows closed: %d" % len(f))
        if f:
            peaks = [i["max_used"] for i in f]
            capped = sum(1 for p in peaks if p >= 99.5)
            print("  average peak %.0f%%, average unused %.0f%%, %d hit the limit" % (
                sum(peaks) / len(peaks), sum(100 - p for p in peaks) / len(peaks), capped))
            if args.verbose:
                for i in f:
                    print("  %-18s %5.0f%% %6.0f%%  %s" % (render.dhm(i["reset_ts"]), i["max_used"], i["unused_at_close"] or 0, i["close_reason"]))
        print()
        if wk:
            tot = sum(i["unused_at_close"] or 0 for i in wk)
            line = "Totals: %.0f weekly points unused over %d weeks (avg %.0f%% per week)" % (tot, len(wk), tot / len(wk))
            if ppw:
                line += ", about %.1f full 5-hour windows" % (tot / ppw)
            print(line)
        else:
            print("Totals: no weekly window has closed yet.")
    finally:
        st.close()
    return 0


def _closed(st, window, since):
    """Window instances that really ended. One closed by a 'shift' was opened
    by a bogus later resets_at and superseded by the real window (D-10): it
    never ended, and its "unused" points never expired."""
    return [i for i in st.instances(window, since=since) if i["closed_at"] is not None and i["close_reason"] != "shift"]


def _note(i, ppw):
    notes = []
    if i["close_reason"] == "drop":
        notes.append("reset detected by drop")
    if i["last_seen"] < i["reset_ts"] - 3600 and i["close_reason"] != "drop":
        notes.append("last seen %s before reset" % dur(i["reset_ts"] - i["last_seen"]))
    if ppw and i["unused_at_close"]:
        notes.append("~%.1f 5h windows" % (i["unused_at_close"] / ppw))
    return "; ".join(notes)


# -- profile ---------------------------------------------------------------------------
SHADES = " ░▒▓█"


def cmd_profile(args, cfg):
    now = time.time()
    st = _open(create=True)
    try:
        if args.rebuild:
            st.begin()
            try:
                prof = profile.load(st)
                prof.S, prof.W, prof.T = [0.0] * 168, [0.0] * 168, [0.0] * 168
                prof.processed_until, prof.learned_hours = None, 0
                n = profile.learn(st, prof, now, cfg, profile.current_weights(st, prof, now, cfg)[0])
                profile.save(st, prof)
                st.commit()
            except Exception:
                st.rollback()
                raise
            print("Rebuilt from stored samples: %d hours learned." % n)
        prof = profile.load(st)
        w, awake, info = profile.current_weights(st, prof, now, cfg)
        if args.json:
            print(json.dumps({"weights": w, "awake": awake, "info": info, "prior_meta": prof.prior_meta}, indent=2))
            return 0
        rows = profile.heatmap_rows(w)
        top = max(max(r) for r in rows) or 1.0
        low = min(min(r) for r in rows)
        flat = top - low < 0.25  # within 25% of uniform everywhere: nothing to show
        print("Hour-of-week profile (%s); darker = larger share of a week's spend" % info["source"])
        print("      " + "".join("%-3d" % h if h % 3 == 0 else "" for h in range(24)))
        for d, r in enumerate(rows):
            cells = "".join("·" if flat else SHADES[min(4, int((x - low) / (top - low) * 4.999))] for x in r)
            print("  %s %s  %4.1f%%" % (profile.DAYS[d], cells, sum(r) / 168 * 100))
        print("  (each cell = 1 hour; day totals at the right; uniform would be 14.3%% a day%s)" % (
            "; flat so far: no hour stands out yet" if flat else ""))
        print("Awake hours per week: %d of 168" % info["awake_hours"])
        if prof.prior_meta:
            m = prof.prior_meta
            trust = m.get("trust") or 0
            print("Prior: transcripts, %s days, %s messages, %s active hours, trusted %.0f%% when built (full at 14 days)" % (
                m.get("days"), m.get("messages"), m.get("active_hours"), 100 * trust))
            if isinstance(cfg["profile"].get("override"), dict):
                # Profile.rates takes the override instead of the transcript prior.
                print("  unused: profile.override replaces it (weight now 0%; set the override to null to use it)")
            else:
                # It fades with the half-life from its build (D-14), as the weights use it.
                hl = float(cfg["profile"]["half_life_days"]) * 86400
                print("  built %s ago, weight now %.0f%% (it halves every %g days; `glidepath bootstrap` builds a fresh one)" % (
                    dur(now - float(m.get("built_at", now))), 100 * trust * prof.prior_fade(now, hl), hl / 86400))
        else:
            ov = cfg["profile"].get("override")
            if isinstance(ov, dict):
                # The override is the prior (Profile.rates); a transcript one would be unused.
                wake = ov.get("wake_hours") or ("?", "?")
                print("Prior: profile.override (work days %s, wake %s-%s, mode %s)" % (
                    ",".join(profile.DAYS[d] for d in ov.get("work_days", ()) if 0 <= d < 7),
                    wake[0], wake[1], ov.get("mode", "prior")))
            else:
                print("Prior: none (run `glidepath bootstrap`)")
        # Mode replace takes the override's shape alone (Profile.rates).
        replaced = (cfg["profile"].get("override") or {}).get("mode") == "replace"
        print("Learned from usage %%: %d hours %s, typical week %s" % (
            prof.learned_hours, "unused (profile.override mode replace ignores them)" if replaced else "folded in",
            ("%.0f%% of the limit" % info["weekly_total"]) if info["weekly_total"] else "unknown yet"))
        _surplus(st, cfg, w, awake, flat, now)
    finally:
        st.close()
    return 0


def _surplus(st, cfg, w, awake, flat, now):
    """Where surplus usually sits: the lightest awake hours, and how far
    behind pace past weeks were on each day before their reset."""
    blocks = []
    for d in range(7):
        for h in range(0, 24, 3):
            hours = [d * 24 + h + k for k in range(3)]
            if all(awake[b] for b in hours):  # a block half asleep only looks light
                blocks.append((sum(w[b] for b in hours), d, h))
    # A flat profile has no lightest block: any three would be a tie.
    awake_blocks = [] if flat else sorted(blocks)[:3]
    if awake_blocks:
        print("Lightest awake blocks: " + ", ".join("%s %02d-%02d" % (profile.DAYS[d], h, h + 3) for _, d, h in awake_blocks))
    # A window an early reset cut short (a drop, D-10, or a rollover well
    # before its reset) never reached its reset: the window that replaced it
    # holds the rest of that week, so each week counts once.
    closed = [i for i in _closed(st, SEVEN_DAY, now - 56 * 86400)
              if i["close_reason"] != "drop" and i["closed_at"] >= i["reset_ts"] - 3600]
    if not closed:
        print("Surplus by day before reset: needs at least one closed weekly window.")
        return
    tgt = config.target(cfg, SEVEN_DAY)
    sums = [[] for _ in range(7)]
    for i in closed:
        # Paced from the window's own start, as the line does (D-15): a drop
        # reset opens it late, and its on-pace level counts from there.
        start = max(i["reset_ts"] - 7 * 86400, i["start_ts"])
        span = profile.share_between(w, start, i["reset_ts"]) or 1.0
        for k in range(7):
            t = i["reset_ts"] - k * 86400
            used = st.max_at(i["id"], t) if t >= start else None
            if used is None:
                continue
            on = tgt * profile.share_between(w, start, t) / span
            sums[k].append(used - on)
    parts = ["%s %+.0f" % ("reset" if k == 0 else "-%dd" % k, sum(v) / len(v)) for k, v in enumerate(sums) if v]
    print("Points vs pace, averaged over %d past weeks: %s" % (len(closed), ", ".join(reversed(parts))))
    # Each column is the level minus the on-pace level for the target, so at
    # the reset it is peak - target: what expired (100 - peak) only at 100.
    if tgt >= 100.0:
        note = "the reset column is what expired"
    else:
        note = "the reset column is the peak versus your %g%% target; `glidepath report` shows what expired" % tgt
    print("  (negative = surplus building up; %s)" % note)


# -- check -----------------------------------------------------------------------------
def parse_duration(s):
    s = s.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    x = float(s[:-1]) * units[s[-1]] if s and s[-1] in units else float(s)
    if not math.isfinite(x) or x < 0:  # nan would make every age "fresh"
        raise ValueError("bad duration %r (want e.g. 30m, 2h, 2d)" % s)
    return x


def parse_tail(spec, cfg):
    tail = {FIVE_HOUR: float(cfg["tail"]["five_hour_min"]) * 60, SEVEN_DAY: float(cfg["tail"]["seven_day_hours"]) * 3600}
    if spec:
        for part in spec.split(","):
            if "=" not in part:
                raise ValueError("bad --tail part %r (want 5h=60m,week=2d)" % part)
            k, v = part.split("=", 1)
            k = k.strip().lower()
            key = FIVE_HOUR if k in ("5h", "five_hour", "session") else SEVEN_DAY if k in ("week", "wk", "7d", "seven_day") else None
            if key is None:
                raise ValueError("unknown window %r in --tail" % k)
            tail[key] = parse_duration(v)
    return tail


def parse_min_free(value, cfg):
    """--min-free (default: the config's), clamped to the config's range
    (config.RANGES) like min_free in config.json; nan or inf is an error."""
    x = float(cfg["min_free"] if value is None else value)
    if not math.isfinite(x):
        raise ValueError("bad --min-free %r (want a number of points)" % value)
    lo, hi = config.RANGES[("min_free",)]
    return float(min(hi, max(lo, x)))


# The weekly price of a 5h leftover while the exchange rate is not known yet:
# its clamp maximum (D-23), so a tight week still blocks the harvest.
ASSUMED_EXCHANGE_RATE = 50.0


def _held(name, v):
    """Why a window has nothing to harvest whatever its leftover, or None: at
    or above a target below 100, or spending faster than its target allows
    (slow down). The line shows both yellow or red (D-16)."""
    if pace.over_target(v):
        return "%s window is at or above its %g%% target" % (name, v["target"])
    if v["verdict"] == "slow_down":
        return "%s window is spending faster than its %g%% target allows (slow down)" % (name, v["target"])
    return None


def _below(target):
    return " below its %g%% target" % target if target < 100.0 else ""


def check_result(st, cfg, now, min_free, tail, max_age):
    v5, vw, _ = views(st, cfg, now)
    ppw = weekly.exchange_rate(st, now)[0]
    last = last_reading(st)
    age = (now - last) if last else None
    reasons = []
    tgt5, tgtw = config.target(cfg, FIVE_HOUR), config.target(cfg, SEVEN_DAY)
    f = {"present": v5.get("present", False), "target": tgt5}
    if f["present"]:
        f.update(
            used=v5["used"], resets_at=v5["reset_ts"], seconds_to_reset=v5["reset_ts"] - now,
            free_now=100.0 - v5["used"], rate=v5.get("rate"), projected=v5.get("projected"),
            projected_unused=v5["unused_at_reset"], verdict=v5["verdict"],
            in_tail=(v5["reset_ts"] - now) <= tail[FIVE_HOUR],
        )
    else:
        f.update(free_now=100.0, in_tail=False, projected_unused=None, verdict="fresh")
    k = {"present": vw.get("present", False), "target": tgtw}
    if k["present"]:
        k.update(
            used=vw["used"], resets_at=vw["reset_ts"], seconds_to_reset=vw["reset_ts"] - now,
            free_now=100.0 - vw["used"], delta=vw["delta"], intensity=vw["intensity"],
            projected=vw["projected"], projected_unused=vw["expire"], dry_at=vw.get("dry_ts"),
            verdict=vw["verdict"], in_tail=(vw["reset_ts"] - now) <= tail[SEVEN_DAY],
        )
    else:
        k.update(free_now=100.0, in_tail=False, projected_unused=None, verdict="fresh")
    # What is spare is measured against each window's target, not 100, as on
    # the line (green): a target below 100 keeps a reserve. `spare` is the
    # projected unused below the target, and the 5h room now is what is left
    # below it (projected_unused and free_now stay out of 100).
    f["spare"] = pace.below_target(f["projected_unused"], tgt5)
    k["spare"] = pace.below_target(k["projected_unused"], tgtw)
    room5 = pace.below_target(f["free_now"], tgt5)
    held5 = _held("5h", v5) if f["present"] else None
    heldw = _held("weekly", vw) if k["present"] else None

    stale = age is None or age > max_age
    if stale:
        reasons.append("no fresh reading in the last %s" % dur(max_age) if age is not None else "no fresh readings at all")
    # No 5h burn rate yet (D-11): its leftover is unknown (projected_unused
    # null), so neither harvest may count on it, whatever min_free (taken as
    # `None or 0`, it passed min_free 0), and nothing is priced in weekly points.
    rate_unknown = f["present"] and f["projected_unused"] is None
    leftover_ok = pace.spare(f["spare"], min_free)
    week_cost = None
    if f["present"] and not rate_unknown:
        week_cost = f["spare"] * (ppw or ASSUMED_EXCHANGE_RATE) / 100.0
    # Only a positive leftover is priced: 0 costs 0 at any rate (D-20).
    assumed = bool(week_cost) and ppw is None
    # Without a cost (no 5h window, or no rate yet) this asks whether the
    # week is tight on its own.
    week_ok = (not k["present"]) or (k.get("dry_at") is None and (k["projected"] + (week_cost or 0)) <= tgtw)
    f["harvestable"] = bool(f["present"] and f["in_tail"] and not held5 and leftover_ok
                            and pace.spare(room5, min_free) and week_ok)
    if f["in_tail"] and held5:
        reasons.append(held5)
    if f["present"] and f["in_tail"] and not week_ok:
        if f["spare"] == 0:  # no leftover: the week is tight on its own
            reasons.append("the weekly budget is already tight")
        else:
            reasons.append("5h leftover would eat into a weekly budget that is already tight" + (
                " (exchange rate not known yet: priced at %.0f weekly points per 5h window)" % ASSUMED_EXCHANGE_RATE
                if assumed else ""))
    # Harvesting weekly surplus must not starve your own 5h window: it needs
    # room now and at the current burn rate.
    five_room = pace.spare(room5, min_free) and (not f["present"] or leftover_ok)
    week_surplus = k["present"] and k["in_tail"] and not heldw and pace.spare(k["spare"], min_free)
    k["harvestable"] = bool(week_surplus and five_room)
    if k["in_tail"] and heldw:
        reasons.append(heldw)
    if rate_unknown and (f["in_tail"] or week_surplus):
        reasons.append("5h burn rate not known yet")
    elif week_surplus and not five_room:
        reasons.append("weekly surplus is there, but the 5h window has no room at the current burn")
    if f["harvestable"]:
        reasons.append("5h window ends in %s with ~%.0f%% projected unused%s" % (dur(f["seconds_to_reset"]), f["spare"], _below(tgt5)))
    if k["harvestable"]:
        reasons.append("weekly window ends in %s with ~%.0f%% projected to expire%s" % (dur(k["seconds_to_reset"]), k["spare"], _below(tgtw)))
    if not (f["harvestable"] or k["harvestable"]) and not reasons:
        reasons.append("no window is in its tail with at least %.0f%% to spare" % min_free)
    ok = (f["harvestable"] or k["harvestable"]) and not stale
    return {
        "ok": ok,
        "now": now,
        "data_age_s": age,
        "stale": stale,
        "reasons": reasons,
        "five_hour": f,
        "seven_day": k,
        "exchange_rate": ppw,
        "exchange_rate_assumed": assumed,
        "weekly_cost_of_5h_leftover": week_cost,
        "params": {"min_free": min_free, "tail_5h_s": tail[FIVE_HOUR], "tail_week_s": tail[SEVEN_DAY], "max_age_s": max_age},
    }


def cmd_check(args, cfg):
    now = time.time()
    try:
        tail = parse_tail(args.tail, cfg)
        max_age = parse_duration(args.max_age)
        min_free = parse_min_free(args.min_free, cfg)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    st = _open()
    if st is None:
        print(json.dumps({"ok": False, "reasons": ["no database yet"]}))
        return 1
    try:
        res = check_result(st, cfg, now, min_free, tail, max_age)
    finally:
        st.close()
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


# -- bootstrap -------------------------------------------------------------------------
def cmd_bootstrap(args, cfg):
    from . import transcripts

    st = _open(create=True)
    try:
        t = time.time()
        prior, meta, stats = transcripts.bootstrap(st, cfg, days=args.days, root=args.root, dry_run=args.dry_run)
        el = time.time() - t
    finally:
        st.close()
    print("Scanned %d transcript files in %.1fs (%d skipped as older than the period)." % (stats["files"], el, stats["files_skipped_old"]))
    if stats.get("files_unreadable"):
        print("Could not open %d transcript files or folders: skipped." % stats["files_unreadable"])
    print("Assistant messages counted: %d (%d duplicate lines merged, %d unreadable lines)." % (stats["messages"], stats["duplicates"], stats["bad_lines"]))
    print("Only timestamps, message ids and token counts were read; no content was kept.")
    if prior is None:
        print("No usable messages in the period: the profile prior was not changed.")
        return 1
    print("Period: %s days, %s active hours; prior trusted %.0f%% (full at 14 days, the rest is uniform)." % (
        meta["days"], meta["active_hours"], 100 * meta["trust"]))
    if args.dry_run:
        print("DRY RUN: profile not saved.")
    else:
        print("Saved as the profile prior. See it with `glidepath profile`.")
    return 0


# -- doctor ----------------------------------------------------------------------------
def cmd_doctor(args, cfg):
    from . import install

    now = time.time()
    results = []

    def add(level, what, detail=""):
        results.append((level, what, detail))

    add("ok", "glidepath %s" % _version(), "repo %s" % install.repo_root())
    home_err = None
    try:
        config.home()
    except config.HomeError as e:  # Windows: no home, so no runtime.json, database or log below
        home_err = str(e)
        add("fail", "GLIDEPATH_HOME", home_err)
    rt = None
    try:
        with open(config.runtime_path(), encoding="utf-8-sig") as f:
            rt = json.load(f)
    except (OSError, ValueError):
        pass
    if rt:
        ok, ver, sq, ms, note = install.probe(rt.get("python"))
        add("ok" if ok else "fail", "pinned interpreter %s" % rt.get("python"),
            ("Python %s, sqlite %s, %.0f ms startup" % (ver, sq, ms)) if ok else note)
        add("ok" if os.path.exists(rt.get("entry", "")) else "fail", "entry point %s" % rt.get("entry"))
    elif not home_err:
        add("warn", "no runtime.json", "run `glidepath install --apply` (the mod reads it)")

    sp = install.settings_path(args.settings)
    try:
        obj, raw, _ = install.read_settings(sp)
        sl = obj.get("statusLine")
        cmd = sl.get("command") if isinstance(sl, dict) else None
        if install.is_ours(cmd):
            add("ok", "statusLine runs glidepath", cmd)
            ri = sl.get("refreshInterval")
            add("ok" if ri else "warn", "refreshInterval %s" % ri, "" if ri else "set it to 60 so idle sessions keep updating")
            comp = install.compose_of(cmd)
            if comp:
                add("ok", "composing with your previous statusLine", comp)
        elif cmd:
            add("warn", "statusLine is not glidepath", cmd)
        else:
            add("warn", "no statusLine in %s" % sp)
        if config.IS_WINDOWS:
            _doctor_shell(add, sp, obj, cmd)
            _doctor_compose(add, cmd)
            # Claude Code passes the settings env's GLIDEPATH_HOME as written, but doctor run
            # from Git Bash sees only the folder MSYS rewrote it to: check the raw one.
            bad = install.home_problem(obj)
            if bad and bad != home_err:
                add("fail", "GLIDEPATH_HOME in the settings env", bad)
        if obj.get("disableAllHooks"):
            add("warn", "disableAllHooks is set", "the statusLine and mods do not run")
    except ValueError as e:
        add("fail", "settings unreadable", str(e))

    code, so, se = install.run_claude(["plugin", "list", "--json"]) if install.claude_bin() else (127, "", "")
    if code == 0:
        try:
            plugs = json.loads(so)
        except ValueError:
            plugs = []
        mine = [p for p in plugs if isinstance(p, dict) and str(p.get("id", "")).startswith(install.PLUGIN + "@")]
        if mine:
            p = mine[0]
            add("ok" if p.get("enabled", True) else "warn", "mod %s installed" % p.get("id"),
                "enabled" if p.get("enabled", True) else "disabled")
        else:
            add("warn", "mod not installed", "glidepath install --apply, or claude plugin install %s" % install.PLUGIN_ID)
    else:
        add("warn", "could not list plugins", (se or "claude not on PATH").strip()[:120])

    st = None if home_err else _open()
    if st is None and not home_err:
        add("warn", "no database yet", config.db_path())
    elif st is not None:
        try:
            jm = st.db.execute("PRAGMA journal_mode").fetchone()[0]
            qc = st.db.execute("PRAGMA quick_check").fetchone()[0]
            add("ok" if jm == "wal" else "warn", "database %s" % config.db_path(), "journal_mode=%s, quick_check=%s" % (jm, qc))
            counts = st.sample_counts()
            last = st.last_by_source()
            if counts:
                for src, n in sorted(counts.items()):
                    age = now - last[src]
                    lvl = "ok" if age < 3600 else "warn"
                    add(lvl, "samples from %s: %d" % (src, n), "last %s ago" % dur(age))
            else:
                add("warn", "no samples recorded yet", "they appear after the first API response of a session")
            for src in ("statusline", "mod"):
                lr = st.meta_get("last_render:" + src)
                if lr:
                    add("ok", "last %s line (%s ago): %s" % (src, dur(now - lr["ts"]), lr["line"]),
                        "rate_limits received: %s" % json.dumps(lr.get("rate_limits"), separators=(",", ":")))
            every = cfg.get("fetch_usage_min") or 0
            if every:
                ok_at, note = st.meta_get("usage_cmd_ok_at"), st.meta_get("usage_cmd_note")
                add("warn" if note else "ok", "/usage every %g min" % every,
                    note or ("last answered %s ago" % dur(now - ok_at) if ok_at else "not run yet"))
            kinds = st.kinds()
            if kinds:
                add("ok", "rate-limit kinds seen", ", ".join("%s (%s, %d)" % (k, s, c) for k, s, _, _, c in kinds))
            rej = [(k, v) for k, v in st.db.execute("SELECT key, value FROM meta WHERE key LIKE 'rejected:%'").fetchall()]
            if rej:
                add("ok", "readings rejected by the guards", ", ".join("%s=%s" % (k[9:], v) for k, v in rej))
            prof = profile.load(st)
            overridden = isinstance(cfg["profile"].get("override"), dict)
            if prof.prior:
                m = prof.prior_meta
                add("ok", "profile prior from transcripts", "%s days, %s messages%s" % (
                    m.get("days"), m.get("messages"), "; unused: profile.override replaces it" if overridden else ""))
            elif overridden:
                add("ok", "profile prior from profile.override")
            else:
                add("warn", "no transcript prior", "run `glidepath bootstrap`")
            add("ok", "profile hours learned from usage %%: %d" % prof.learned_hours,
                "unused: profile.override mode replace ignores them"
                if (cfg["profile"].get("override") or {}).get("mode") == "replace" else "")
        except sqlite3.Error as e:
            add("fail", "database error", str(e))
        finally:
            st.close()
    if not home_err and os.path.exists(config.log_path()):
        with open(config.log_path(), encoding="utf-8", errors="replace") as f:
            n = sum(1 for line in f if line[:2] == "20")
        add("warn" if n else "ok", "errors.log has %d entries" % n, config.log_path())
    modlog = None if home_err else os.path.join(config.home(), "mod.log")
    if modlog and os.path.exists(modlog):
        # The mod's own log: the surfaces each session draws on, and whether one asked for the band.
        with open(modlog, encoding="utf-8", errors="replace") as f:
            tail = [line.rstrip("\n") for line in f if line.strip()][-6:]
        add("ok", "mod log, last %d lines" % len(tail), modlog)
        for line in tail:
            add("ok", "  " + line)
    if args.json:
        print(json.dumps([{"level": a, "check": b, "detail": c} for a, b, c in results], indent=2))
    else:
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
        for lvl, what, detail in results:
            print("%s %s%s" % (mark[lvl], what, ("  (" + detail + ")") if detail else ""))
    return 1 if any(r[0] == "fail" for r in results) else 0


def _doctor_shell(add, spath, obj, cmd):
    """Windows: the shell Claude Code will run the statusLine in (Git Bash if
    it finds one, else PowerShell), and whether glidepath's command was written
    for the other one (Git Bash installed or removed since install)."""
    from . import install, winshell

    # The settings env install predicted with: the user file's, then this file's own.
    kind, path = winshell.predict_shell(os.environ, install.settings_env(obj))
    if path is None:
        add("warn", "statusLine shell: no Git Bash and no PowerShell found", "Claude Code cannot run the statusLine")
        return
    name = winshell.SHELLS[kind]
    if not install.is_ours(cmd):
        add("ok", "statusLine shell: %s" % name, path)
        return
    rec = install.find_record(install.load_state(), spath)[1]
    was = rec.get(winshell.RECORD_KEY) if isinstance(rec, dict) else None
    was = was if isinstance(was, str) and was in winshell.SHELLS else None
    if winshell.reads_alike(cmd):
        # Bare words: the same command whichever shell runs it.
        note = "; install assumed %s, and the command reads the same in both" % winshell.SHELLS[was] if (
            was and was != kind) else ""
        add("ok", "statusLine shell: %s" % name, path + note)
        return
    need = winshell.written_for(cmd) or was
    # Inside Git Bash, whose PATH puts its own git first, the prediction can differ from what
    # Claude Code finds when started from the Desktop app or PowerShell: a note, not a repin.
    msys = ""
    if winshell.getenv(os.environ, "MSYSTEM"):
        msys = ("; checked from Git Bash, so Claude Code started from the Desktop app or PowerShell may pick "
                "another shell: run `glidepath doctor` from PowerShell to check")
    if need and need != kind:
        fix = path + msys if msys else "run `glidepath install --apply` to rewrite it (%s)" % path
        add("warn", "statusLine command is written for %s, but Claude Code would run it in %s" % (
            winshell.SHELLS[need], name), fix)
    else:
        add("ok", "statusLine shell: %s" % name, path + msys)


def _doctor_compose(add, cmd):
    """Windows: a previous statusLine that an older install left as plain text after
    --compose, where Windows PowerShell 5.1 or Git Bash hands it to Python changed."""
    from . import install, winshell

    argv = install.split_command(cmd) if install.is_ours(cmd) else None
    plain = None  # the value glidepath statusline composes with, when a plain --compose gave it
    if argv and "statusline" in argv:
        it = iter(argv[argv.index("statusline") + 1:])
        for a in it:  # the last flag wins, as in statusline.parse_args
            if a in ("--compose", "--compose-b64"):
                value = next(it, None)
                plain = value if a == "--compose" else None
            elif a in ("--source", "--format", "--now"):
                next(it, None)
    if plain is not None and winshell.needs_encoding(plain):
        add("warn", "your previous statusLine can reach glidepath changed in Windows PowerShell 5.1 or Git Bash",
            "run `glidepath install --apply` to store it encoded")


def _version():
    from . import __version__

    return __version__


# -- config ----------------------------------------------------------------------------
def cmd_config(args, cfg):
    if args.action == "path":
        print(config.config_path())
    elif args.action == "init":
        p = config.write_defaults()
        print("Config at %s" % p)
    else:
        path = config.config_path()
        if not os.path.exists(path):
            note = " (not created; defaults shown)"
        else:  # a file that does not parse is ignored as a whole: say so
            why = config.unreadable(path)
            note = " (not used: %s; defaults shown)" % why if why else ""
        print("# %s%s" % (path, note))
        print(json.dumps(cfg, indent=2))
    return 0
