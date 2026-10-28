"""The SQLite store: opening a new database."""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from glidepath import config
from glidepath.store import SCHEMA_VERSION, Store


class FirstOpen(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glidepath-test-")
        self.path = os.path.join(self.dir, "tempo.db")
        # Another opener of the same new file in the middle of its own first
        # open: it holds the write lock that the switch to WAL needs.
        self.other = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.other.execute("BEGIN IMMEDIATE")

    def tearDown(self):
        self.other.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_concurrent_switch_to_wal_is_waited_out(self):
        # Review 7 (fresh-eyes#1): when several sessions opened a new tempo.db
        # at once (install makes every open session re-render), the losers'
        # PRAGMA journal_mode=WAL got "database is locked" at once instead of
        # waiting the busy timeout: the statusline fell back to the bare line
        # and lost its reading (D-05). Here the other opener is done in 50 ms.
        timer = threading.Timer(0.05, self.other.execute, ("COMMIT",))
        timer.start()
        try:
            st = Store(self.path, timeout_ms=1000)
        finally:
            timer.join()
        try:
            self.assertEqual(st.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(st.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        finally:
            st.close()

    def test_it_waits_no_longer_than_the_busy_timeout(self):
        # Review 7 (fresh-eyes#1): it waits up to the busy timeout, not
        # forever, then fails as before.
        t = time.monotonic()
        opened, real = [], sqlite3.connect

        def connect(*a, **kw):
            opened.append(real(*a, **kw))
            return opened[-1]

        with mock.patch("glidepath.store.sqlite3.connect", connect):
            with self.assertRaises(sqlite3.OperationalError):
                Store(self.path, timeout_ms=100)
        self.assertGreaterEqual(time.monotonic() - t, 0.09)
        self.assertLess(time.monotonic() - t, 2.0)
        # The failed open closed its connection: on Windows an open handle
        # keeps the file from being renamed or deleted.
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


class CloseOnWindows(unittest.TestCase):
    @unittest.skipUnless(hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"), "needs Python 3.12+")
    def test_windows_keeps_the_wal_when_the_last_connection_closes(self):
        # Review 15 CI: on the Windows runner closing the database (a checkpoint into it and
        # deleting the WAL) took 18 ms of a 30 ms render, p95 32 of 44.
        for windows in (True, False):
            with self.subTest(windows=windows), mock.patch.object(config, "IS_WINDOWS", windows):
                d = tempfile.mkdtemp(prefix="glidepath-test-")
                self.addCleanup(shutil.rmtree, d, True)
                path = os.path.join(d, "tempo.db")
                st = Store(path)
                st.meta_set("k", {"v": 1})
                st.close()
                self.assertEqual(os.path.exists(path + "-wal"), windows)
                st = Store(path)
                self.assertEqual(st.meta_get("k"), {"v": 1})
                st.close()

    @unittest.skipUnless(hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"), "needs Python 3.12+")
    def test_a_large_wal_is_emptied_on_close_unless_someone_reads_it(self):
        # Review 15 verify: a lone opener rebuilt the WAL index with nothing marked as copied,
        # so past 1000 pages every recording render copied the whole WAL into the database.
        d = tempfile.mkdtemp(prefix="glidepath-test-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "tempo.db")
        with mock.patch.object(config, "IS_WINDOWS", True), mock.patch("glidepath.store.WAL_RESET_BYTES", 256 * 1024):
            st = Store(path)
            st.meta_set("small", "x")
            st.close()
            self.assertGreater(os.path.getsize(path + "-wal"), 0)  # small: kept
            reader = sqlite3.connect(path, isolation_level=None)  # another glidepath process on Windows
            reader.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM meta").fetchone()  # a snapshot another process holds
            st = Store(path, timeout_ms=10000)  # waiting for the reader would take 10 s
            st.meta_set("big", "y" * 600000)
            t = time.monotonic()
            st.close()
            # Never waits for the reader. A slow CI disk took 0.7 s to copy the WAL,
            # so the bound is far from the copy and far below the 10 s wait.
            self.assertLess(time.monotonic() - t, 5.0)
            self.assertGreater(os.path.getsize(path + "-wal"), 256 * 1024)
            reader.execute("COMMIT")
            reader.close()
            st = Store(path)
            st.meta_set("more", "z")
            st.close()
            self.assertEqual(os.path.getsize(path + "-wal"), 0)  # emptied
            st = Store(path)
            self.assertEqual((st.meta_get("small"), len(st.meta_get("big")), st.meta_get("more")), ("x", 600000, "z"))
            st.close()


if __name__ == "__main__":
    unittest.main()
