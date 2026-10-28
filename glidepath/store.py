"""SQLite store: samples, window instances, per-session readers, profile.

WAL mode, short busy timeout, one short IMMEDIATE transaction per ingest.
Callers on the hot path treat any sqlite3.Error as "skip the write" (fail
open): the line still renders from what is already stored.
"""

import json
import os
import sqlite3
import time

from . import config

SCHEMA_VERSION = 1
# Windows keeps the WAL between processes (Store.__init__); one this large is emptied on close.
WAL_RESET_BYTES = 1000 * (4096 + 24)  # wal_autocheckpoint's 1000 pages of the default page size

# Sample flags.
FRESH = 0  # the reporting session's reading changed: new information
HEARTBEAT = 1  # same reading as that session's previous one: time passed, no news
ANOMALY = 2  # inconsistent with the open window (drop or earlier reset); pending
FIRST = 3  # a session's first-ever reading: a level, but its timing may be stale

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS windows(
  id INTEGER PRIMARY KEY,
  window TEXT NOT NULL,          -- five_hour | seven_day
  start_ts REAL NOT NULL,        -- reset_ts minus the window length, or the drop time
  reset_ts INTEGER NOT NULL,     -- resets_at as first seen for this instance
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,       -- last FRESH reading (heartbeats prove nothing)
  last_used REAL NOT NULL,
  max_used REAL NOT NULL,        -- usage is monotone within an instance
  closed_at REAL,                -- NULL while open
  unused_at_close REAL,          -- 100 - max_used, set when closed
  close_reason TEXT              -- rollover | expired | drop | shift
);
CREATE INDEX IF NOT EXISTS windows_by_reset ON windows(window, reset_ts);
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  window TEXT NOT NULL,
  used_pct REAL NOT NULL,
  resets_at INTEGER NOT NULL,
  source TEXT NOT NULL,          -- statusline | mod | test
  session_id TEXT,
  flag INTEGER NOT NULL DEFAULT 0,
  window_id INTEGER
);
CREATE INDEX IF NOT EXISTS samples_by_window_ts ON samples(window, ts);
CREATE INDEX IF NOT EXISTS samples_by_instance ON samples(window_id, ts);
CREATE TABLE IF NOT EXISTS readers(
  session_id TEXT NOT NULL,
  window TEXT NOT NULL,
  used_pct REAL NOT NULL,
  resets_at INTEGER NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY(session_id, window)
);
CREATE TABLE IF NOT EXISTS kinds(
  kind TEXT PRIMARY KEY,
  source TEXT,
  first_seen REAL,
  last_seen REAL,
  count INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path=None, timeout_ms=250, create=True):
        self.path = path or config.db_path()
        if not create and not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        d = os.path.dirname(self.path)
        if d:
            config.private_dir(d)
        self.timeout_ms = timeout_ms
        self.keeps_wal = False
        self.db = sqlite3.connect(self.path, timeout=timeout_ms / 1000.0, isolation_level=None)
        try:
            self.db.execute("PRAGMA busy_timeout=%d" % int(timeout_ms))
            if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                self._migrate()
            self.db.execute("PRAGMA synchronous=NORMAL")
            if config.IS_WINDOWS and hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):  # Python 3.12+
                # Closing the last connection checkpointed the WAL into the database and deleted
                # it, with the file syncs that takes on Windows: 18 ms of a 30 ms render on CI
                # (p95 32 of 44). The WAL stays instead, until close() empties it once it is
                # large; synchronous=NORMAL keeps it as safe.
                self.db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                self.keeps_wal = True
        except BaseException:
            # A failed open must not hold the file: on Windows an open
            # handle keeps it from being renamed or deleted.
            self.close()
            raise

    def _migrate(self):
        # Several openers of a new file (every open session re-renders at
        # install) race to switch it to WAL, and SQLite answers the losers
        # "locked" at once, without the busy timeout: wait it out here. WAL
        # is persistent, so the winner's switch holds for everyone.
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    def close(self):
        if self.keeps_wal:
            # A process that opens the database alone rebuilds the WAL index as if nothing
            # in the WAL had been copied yet, so once it held 1000 pages every commit copied
            # all of it again (until one process committed twice). One that large is emptied
            # here, at once or not at all: another process reading it means a later close.
            try:
                if os.path.getsize(self.path + "-wal") >= WAL_RESET_BYTES:
                    self.db.execute("PRAGMA busy_timeout=0")
                    self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except (OSError, sqlite3.Error):
                pass
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # -- transactions ------------------------------------------------------
    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- meta ----------------------------------------------------------------
    def meta_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def meta_set(self, key, value):
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    def meta_incr(self, key, by=1):
        self.meta_set(key, int(self.meta_get(key, 0) or 0) + by)

    # -- readers (per-session last reading, for freshness) -------------------
    # Each session's reading before last lives in a second row, keyed by the
    # session id plus this suffix (no schema change). A session id can be
    # shared by 2 processes, and the idle one re-renders the value the
    # session left (D-06).
    BEFORE_LAST = "\x1fbefore"

    def reader_get(self, session_id, window):
        return self.db.execute(
            "SELECT used_pct, resets_at, ts FROM readers WHERE session_id=? AND window=?",
            (session_id, window),
        ).fetchone()

    def reader_set(self, session_id, window, used, resets_at, ts):
        self.db.execute(
            "INSERT INTO readers(session_id, window, used_pct, resets_at, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(session_id, window) DO UPDATE SET used_pct=excluded.used_pct, "
            "resets_at=excluded.resets_at, ts=excluded.ts",
            (session_id, window, used, resets_at, ts),
        )

    # -- window instances ----------------------------------------------------
    _WCOLS = "id, window, start_ts, reset_ts, first_seen, last_seen, last_used, max_used, closed_at, unused_at_close, close_reason"

    def _wrow(self, row):
        if row is None:
            return None
        keys = [k.strip() for k in self._WCOLS.split(",")]
        return dict(zip(keys, row))

    def open_instance(self, window):
        return self._wrow(
            self.db.execute(
                "SELECT %s FROM windows WHERE window=? AND closed_at IS NULL ORDER BY id DESC LIMIT 1" % self._WCOLS,
                (window,),
            ).fetchone()
        )

    def instance(self, wid):
        return self._wrow(self.db.execute("SELECT %s FROM windows WHERE id=?" % self._WCOLS, (wid,)).fetchone())

    def new_instance(self, window, start_ts, reset_ts, now, used):
        cur = self.db.execute(
            "INSERT INTO windows(window, start_ts, reset_ts, first_seen, last_seen, last_used, max_used) "
            "VALUES(?,?,?,?,?,?,?)",
            (window, start_ts, int(reset_ts), now, now, used, used),
        )
        return self.instance(cur.lastrowid)

    def touch_instance(self, wid, now, used):
        self.db.execute(
            "UPDATE windows SET last_seen=?, last_used=?, max_used=MAX(max_used, ?) WHERE id=?",
            (now, used, used, wid),
        )

    def reopen_instance(self, wid):
        self.db.execute(
            "UPDATE windows SET closed_at=NULL, close_reason=NULL, unused_at_close=NULL WHERE id=?", (wid,)
        )

    def set_first_seen(self, wid, ts):
        self.db.execute("UPDATE windows SET first_seen=? WHERE id=?", (ts, wid))

    def state_at(self, wid, t):
        """(running max, time of the latest sample) at or before t, or (None, None)."""
        row = self.db.execute(
            "SELECT MAX(used_pct), MAX(ts) FROM samples WHERE window_id=? AND flag!=? AND ts<=?",
            (wid, ANOMALY, t),
        ).fetchone()
        return (row[0], row[1]) if row and row[1] is not None else (None, None)

    def close_instance(self, wid, closed_at, reason):
        self.db.execute(
            "UPDATE windows SET closed_at=?, close_reason=?, unused_at_close=MAX(0, 100 - max_used) "
            "WHERE id=? AND closed_at IS NULL",
            (closed_at, reason, wid),
        )

    def close_expired(self, now):
        """Close every open instance whose reset time has passed."""
        self.db.execute(
            "UPDATE windows SET closed_at=reset_ts, close_reason='expired', "
            "unused_at_close=MAX(0, 100 - max_used) WHERE closed_at IS NULL AND reset_ts <= ?",
            (now,),
        )

    def recompute_max(self, wid):
        row = self.db.execute(
            "SELECT MAX(used_pct) FROM samples WHERE window_id=? AND flag!=?", (wid, ANOMALY)
        ).fetchone()
        if row and row[0] is not None:
            self.db.execute("UPDATE windows SET max_used=? WHERE id=?", (row[0], wid))

    def instances(self, window, since=None, until=None):
        q = "SELECT %s FROM windows WHERE window=?" % self._WCOLS
        args = [window]
        if since is not None:
            q += " AND reset_ts > ?"
            args.append(since)
        if until is not None:
            q += " AND start_ts < ?"
            args.append(until)
        q += " ORDER BY reset_ts, id"
        return [self._wrow(r) for r in self.db.execute(q, args).fetchall()]

    # -- samples ---------------------------------------------------------------
    def add_sample(self, ts, window, used, resets_at, source, session_id, flag, wid):
        self.db.execute(
            "INSERT INTO samples(ts, window, used_pct, resets_at, source, session_id, flag, window_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ts, window, used, int(resets_at), source, session_id, flag, wid),
        )

    def last_sample(self, window):
        return self.db.execute(
            "SELECT ts, used_pct, resets_at, flag, window_id, source, session_id FROM samples "
            "WHERE window=? ORDER BY ts DESC, id DESC LIMIT 1",
            (window,),
        ).fetchone()

    def last_fresh(self, window, before_id=None):
        """Latest fresh-or-anomaly sample of a window (heartbeats skipped)."""
        return self.db.execute(
            "SELECT id, ts, used_pct, resets_at, flag, window_id FROM samples "
            "WHERE window=? AND flag IN (0, 2) ORDER BY ts DESC, id DESC LIMIT 1",
            (window,),
        ).fetchone()

    def anomalies(self, wid):
        return self.db.execute(
            "SELECT id, ts, used_pct, resets_at FROM samples WHERE window_id=? AND flag=? ORDER BY ts",
            (wid, ANOMALY),
        ).fetchall()

    def withdraw_anomalies(self, wid, reader, used, resets_at, jitter):
        """Delete a session's pending anomalies of 1 reading in an instance."""
        self.db.execute(
            "DELETE FROM samples WHERE window_id=? AND flag=? AND COALESCE(session_id, '~' || source)=? "
            "AND ABS(used_pct - ?) < 1e-9 AND ABS(resets_at - ?) <= ?",
            (wid, ANOMALY, reader, used, resets_at, jitter),
        )

    def move_samples(self, ids, wid, flag=FRESH):
        for sid in ids:
            self.db.execute("UPDATE samples SET window_id=?, flag=? WHERE id=?", (wid, flag, sid))

    def instance_series(self, wid, since=None, until=None):
        """(ts, used, flag) of an instance in time order, anomalies excluded."""
        q = "SELECT ts, used_pct, flag FROM samples WHERE window_id=? AND flag!=?"
        args = [wid, ANOMALY]
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        if until is not None:
            q += " AND ts <= ?"
            args.append(until)
        q += " ORDER BY ts, id"
        return self.db.execute(q, args).fetchall()

    def fresh_levels(self, wid, flag=FRESH):
        """(ts, used) of an instance's fresh (or FIRST) readings, in time order."""
        return self.db.execute(
            "SELECT ts, used_pct FROM samples WHERE window_id=? AND flag=? ORDER BY ts, id", (wid, flag)
        ).fetchall()

    def max_at(self, wid, t):
        row = self.db.execute(
            "SELECT MAX(used_pct) FROM samples WHERE window_id=? AND flag!=? AND ts<=?",
            (wid, ANOMALY, t),
        ).fetchone()
        return row[0] if row else None

    def fresh_series(self, window, since, until=None):
        """Fresh readings (ts, used, window_id) for profile learning."""
        q = "SELECT ts, used_pct, window_id FROM samples WHERE window=? AND flag=? AND ts>?"
        args = [window, FRESH, since]
        if until is not None:
            q += " AND ts<=?"
            args.append(until)
        q += " ORDER BY ts, id"
        return self.db.execute(q, args).fetchall()

    def first_sample_ts(self, window):
        row = self.db.execute("SELECT MIN(ts) FROM samples WHERE window=?", (window,)).fetchone()
        return row[0] if row else None

    def sample_counts(self):
        return dict(self.db.execute("SELECT source, COUNT(*) FROM samples GROUP BY source").fetchall())

    def last_by_source(self):
        return dict(self.db.execute("SELECT source, MAX(ts) FROM samples GROUP BY source").fetchall())

    # -- kinds (rate-limit window names seen at runtime) -----------------------
    def note_kind(self, kind, source, now):
        self.db.execute(
            "INSERT INTO kinds(kind, source, first_seen, last_seen, count) VALUES(?,?,?,?,1) "
            "ON CONFLICT(kind) DO UPDATE SET last_seen=excluded.last_seen, count=count+1, "
            "source=excluded.source",
            (str(kind)[:64], source, now, now),
        )

    def kinds(self):
        return self.db.execute("SELECT kind, source, first_seen, last_seen, count FROM kinds ORDER BY kind").fetchall()

    # -- retention -------------------------------------------------------------
    def prune(self, now, retention_days):
        cutoff = now - retention_days * 86400
        self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM windows WHERE closed_at IS NOT NULL AND closed_at < ?", (cutoff,))
        self.db.execute("DELETE FROM readers WHERE ts < ?", (now - 7 * 86400,))
