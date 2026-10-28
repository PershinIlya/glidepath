"""Bootstrap the profile prior from Claude Code transcripts.

Reads ~/.claude/projects/**/*.jsonl and keeps, per assistant message, only
its timestamp, message id and token counts. Message content is parsed by
json.loads (the line is one JSON object) and dropped on the spot: it is
never stored, printed or copied.

Each message is weighted by a cost proxy using API price ratios relative
to fresh input tokens (docs/design-decisions.md D-14):
    input + 1.25 * cache_creation + 0.1 * cache_read + 5 * output
Only the distribution over the hours of the week matters for the prior.
"""

import datetime
import json
import math
import os
import stat
import time

from . import clock, config, profile

W_INPUT, W_CACHE_CREATE, W_CACHE_READ, W_OUTPUT = 1.0, 1.25, 0.1, 5.0
FULL_TRUST_DAYS = 14.0
MIN_ACTIVE_DAYS = 4.0


def _ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _n(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        f = float(v)
    except OverflowError:
        return 0.0
    return f if math.isfinite(f) and f > 0 else 0.0


def cost_weight(usage):
    if not isinstance(usage, dict):
        return 0.0
    return (
        W_INPUT * _n(usage.get("input_tokens"))
        + W_CACHE_CREATE * _n(usage.get("cache_creation_input_tokens"))
        + W_CACHE_READ * _n(usage.get("cache_read_input_tokens"))
        + W_OUTPUT * _n(usage.get("output_tokens"))
    )


def _message(line):
    """(id, ts, weight) of an assistant line, None to skip, "bad" if broken.
    Nothing else from the line survives this function."""
    try:
        d = json.loads(line)
    except ValueError:
        return "bad"
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    key = msg.get("id") or d.get("requestId") or d.get("uuid")
    ts = _ts(d.get("timestamp"))
    if not isinstance(key, str) or not key or ts is None:
        return None
    return key, ts, cost_weight(msg.get("usage"))


def long_root(root):
    """`root` to walk from: on Windows in the extended-length form
    (\\\\?\\C:\\..., \\\\?\\UNC\\server\\share\\...), which every path below it
    inherits, so a transcript deeper than 260 characters is still read where
    long paths are not enabled (the default). Claude Code writes them deeper:
    a long project folder name, then <session>\\subagents\\agent-<id>.jsonl."""
    if not config.IS_WINDOWS:
        return root
    import ntpath

    if root.startswith(("\\\\?\\", "\\\\.\\")):
        return root
    root = ntpath.abspath(root)  # absolute, backslashes, no `..`: the form \\?\ needs
    return "\\\\?\\UNC\\" + root[2:] if root.startswith("\\\\") else "\\\\?\\" + root


def scan(root, since, now):
    """Cost weight per message, deduplicated. Returns (events, stats).

    events: list of (ts, weight). A message streamed over several lines
    shares one message id; the largest usage seen for it wins. A file or
    folder that cannot be opened is counted in files_unreadable.
    """
    stats = {"files": 0, "files_skipped_old": 0, "files_unreadable": 0, "lines": 0, "bad_lines": 0, "messages": 0,
             "duplicates": 0}
    best = {}
    top = long_root(root)
    if not os.path.isdir(top):  # no transcripts folder: nothing to read, nothing unreadable
        return [], stats

    def unreadable(_error):
        stats["files_unreadable"] += 1

    for dirpath, dirnames, filenames in os.walk(top, onerror=unreadable):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".jsonl") or fn.startswith("."):
                continue
            path = os.path.join(dirpath, fn)
            try:
                # Non-blocking open, then check it is a regular file: a FIFO
                # named *.jsonl must not hang the scan.
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            except OSError:
                stats["files_unreadable"] += 1
                continue
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    os.close(fd)
                    continue
                if st.st_mtime < since:
                    os.close(fd)
                    stats["files_skipped_old"] += 1
                    continue
                f = os.fdopen(fd, "rb")
            except OSError:
                os.close(fd)
                stats["files_unreadable"] += 1
                continue
            stats["files"] += 1
            with f:
                for line in f:
                    if b'"assistant"' not in line or b'"usage"' not in line:
                        continue
                    stats["lines"] += 1
                    try:
                        found = _message(line)
                    except Exception:  # any malformed line is skipped, never fatal
                        found = "bad"
                    if found == "bad":
                        stats["bad_lines"] += 1
                        continue
                    if found is None:
                        continue
                    key, ts, weight = found
                    if ts < since or ts > now + 300 or weight <= 0:
                        continue
                    prev = best.get(key)
                    if prev is not None:
                        stats["duplicates"] += 1
                        if prev[1] >= weight:
                            continue
                    best[key] = (ts, weight)
    stats["messages"] = len(best)
    return list(best.values()), stats


def build_prior(events, since, now, half_life_days):
    """Decay-weighted mean cost per hour-of-week bin over every hour of the period.

    Hours with no activity count as zeros, so a bin's mean is per calendar
    hour, not per active hour (thevibeworks' per-active-day mean runs high).
    """
    if not events:
        return None, {}
    first = min(t for t, _ in events)
    start = profile.hour_start(max(since, first))
    end = profile.hour_start(now)
    acc = {}
    for t, wgt in events:
        hs = profile.hour_start(t)
        acc[hs] = acc.get(hs, 0.0) + wgt
    num = [0.0] * profile.BINS
    den = [0.0] * profile.BINS
    hl = half_life_days * 86400.0
    active = 0
    for s, e, hs, b in profile.segments(start, end):
        if e - s < 3599:  # DST edge or partial: skip fractional hours
            continue
        a = 0.5 ** ((now - hs) / hl)
        v = acc.get(hs, 0.0)
        if v > 0:
            active += 1
        num[b] += a * v
        den[b] += a
    mean = [n / d if d > 0 else 0.0 for n, d in zip(num, den)]
    if sum(mean) <= 0:
        return None, {}
    shape = profile.normalize(profile.smooth(mean))
    # A few days of transcripts do not describe a week: blend toward uniform
    # until two weeks are covered (one hour of data must not become a spike).
    # Coverage, not span: the span starts where 5% of the cost is behind
    # (a stray old message does not count), and it takes several distinct
    # active days.
    days = (end - start) / 86400.0
    ordered = sorted(events)
    total = sum(w for _, w in ordered)
    cum, trimmed_start = 0.0, start
    for t, wgt in ordered:
        cum += wgt
        if cum >= 0.05 * total:
            trimmed_start = max(start, profile.hour_start(t))
            break
    covered = (end - trimmed_start) / 86400.0
    active_days = len({clock.strftime("%Y-%m-%d", t) for t, _ in ordered})
    mix = max(0.0, min(1.0, covered / FULL_TRUST_DAYS, active_days / MIN_ACTIVE_DAYS))
    prior = [mix * x + (1 - mix) / profile.BINS for x in shape]
    meta = {
        "built_at": now,
        "period_start": start,
        "period_end": end,
        "days": round(days, 1),
        "covered_days": round(covered, 1),
        "active_days": active_days,
        "trust": round(mix, 3),
        "active_hours": active,
        "messages": len(events),
    }
    return prior, meta


def bootstrap(store, cfg, now=None, days=None, root=None, dry_run=False):
    now = now or time.time()
    days = float(days or cfg["profile"]["bootstrap_days"])
    root = root or os.path.join(config.claude_dir(), "projects")
    since = now - days * 86400
    events, stats = scan(root, since, now)
    prior, meta = build_prior(events, since, now, float(cfg["profile"]["half_life_days"]))
    meta.update({"files": stats["files"], "root": root})
    if prior is not None and not dry_run:
        store.begin()
        try:
            prof = profile.load(store)  # read inside the write lock: no lost update
            prof.prior = prior
            prof.prior_meta = meta
            profile.save(store, prof)
            store.meta_set("bootstrap", meta)
            store.commit()
        except Exception:
            store.rollback()
            raise
    return prior, meta, stats
