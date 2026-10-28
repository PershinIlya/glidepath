import os
import sys

if not __package__:
    # Run as a folder (`py glidepath`, not `py -m glidepath` or bin/glidepath): Python
    # put this folder first on the path, where config.py and profile.py would
    # shadow the standard library's, and relative imports have no package.
    # Import the package from the folder above instead.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != _here]
    sys.path.insert(0, os.path.dirname(_here))
    import runpy

    runpy.run_module("glidepath", run_name="__main__", alter_sys=True)  # exits with glidepath's code
    sys.exit(0)

if os.name == "nt":  # as in bin/glidepath: a TZ the C runtime misreads goes first
    from .clock import windows_tz

    windows_tz()

if len(sys.argv) > 1 and sys.argv[1] == "statusline":
    from .statusline import main

    sys.exit(main(sys.argv[2:]))

from .cli import main

sys.exit(main(sys.argv[1:]))
