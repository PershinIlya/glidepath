"""Local time, in one place.

Everything glidepath reads in local time (hour-of-week bins and local hours,
reset clocks on the line, active days in the transcripts, the error log's
time stamps) goes through `localtime`, which is the time module's, so the
zone is the process's own. Tests pin a zone by replacing it, on every OS:
time.tzset does not exist on Windows, and its C runtime knows no IANA zone
names (tests/helpers.py: utc() and zone()).
"""

import os
import time

localtime = time.localtime  # (ts) -> struct_time in local time


def strftime(fmt, ts):
    """ts formatted in local time."""
    return time.strftime(fmt, localtime(ts))


def windows_tz(environ=None, tzset=None):
    """Windows: drop an inherited TZ the C runtime misreads, so local time is
    the system's zone, as the taskbar clock shows it. Returns True if dropped.

    The C runtime reads TZ whenever it is set, in its own form only: 3
    letters, then the hours (EST5EDT, UTC0, GST-1GDT). An IANA name (a
    Cygwin login shell exports TZ=Europe/Berlin) reads as zone "Eur" at
    offset 0 with US daylight saving, and every local time glidepath shows
    is off. The C runtime has read TZ by the time glidepath runs (zipimport
    imports the time module at startup), so its _tzset runs again once TZ
    is gone. bin/glidepath calls this before anything reads local time."""
    environ = os.environ if environ is None else environ
    tz = environ.get("TZ")
    if not tz:
        return False
    import re

    if re.match(r"[A-Za-z]{3}[+-]?[0-9]", tz):
        return False
    del environ["TZ"]  # os.environ unsets it in the C runtime's copy too
    try:
        if tzset is None:
            import ctypes

            tzset = ctypes.cdll.ucrtbase._tzset
        tzset()
    except Exception:
        pass
    return True
