"""Installs made under glidepath's earlier names, ctempo and then pacer: what
`glidepath install --apply` moves over, once. Until then they keep working:
bin/ctempo and bin/pacer still run glidepath, CTEMPO_HOME and PACER_HOME are
still read, and an old folder is used while the new one does not exist
(config.home).

  ~/.pacer or ~/.claude-tempo  -> ~/.glidepath (moved, readings and all)
  the pacer@pacer and           -> uninstalled, their marketplaces removed (the
  ctempo@claude-tempo mods         glidepath mod is installed in their place)
  SwiftBar's pacer.1m.sh or     -> glidepath.1m.sh
  ctempo.1m.sh
  the statusLine and backups    -> recognized as glidepath's (install.is_ours,
                                   install.BACKUP_SUFFIX) and rewritten
"""

import collections
import os

from . import config

Old = collections.namedtuple("Old", "name dir env plugin_id marketplace menubar mark")

# Newest first: when two old folders are left, the newer one's readings move.
OLD = (
    Old("pacer", ".pacer", "PACER_HOME", "pacer@pacer", "pacer", "pacer.1m.sh", "# pacer menu bar plugin"),
    Old("ctempo", ".claude-tempo", "CTEMPO_HOME", "ctempo@claude-tempo", "claude-tempo", "ctempo.1m.sh",
        "# ctempo menu bar plugin"),
)


def default_home(name):
    """The default folder `name` in the user's home, where this OS puts it."""
    if config.IS_WINDOWS:
        import ntpath

        return ntpath.join(ntpath.expanduser("~"), name)
    return os.path.join(os.path.expanduser("~"), name)


def pending_move():
    """(Old, its folder, the new folder) when move_home would move a folder: nothing
    names one (GLIDEPATH_HOME, PACER_HOME, CTEMPO_HOME), the new one does not exist
    yet, and an old one does (the newest). Else None."""
    if any(os.environ.get(k) for k in (config.ENV,) + config.OLD_ENVS):
        return None
    new = default_home(config.DIR)
    if os.path.exists(new):
        return None
    for o in OLD:
        old = default_home(o.dir)
        if os.path.isdir(old) and os.path.normcase(old) != os.path.normcase(new):
            return o, old, new
    return None


def move_home(out=print):
    """Move the old default folder to the new one (pending_move)."""
    pending = pending_move()
    if pending is None:
        return False
    o, old, new = pending
    try:
        os.rename(old, new)
    except OSError as e:
        out("  could not move %s to %s: %s (glidepath keeps using the old folder)" % (old, new, e))
        return False
    if not config.IS_WINDOWS:
        try:
            os.chmod(new, 0o700)  # as a new data folder is made (config.private_dir)
        except OSError:
            pass
    out("Moved %s to %s (glidepath was called %s)" % (old, new, o.name))
    return True


def old_mods_listed(us):
    """The old mods that the user settings file `us` names (by the mod or its marketplace)."""
    from . import install

    try:
        obj, _, _ = install.read_settings(us)
    except (OSError, ValueError, TypeError):
        return ()
    ep, km = obj.get("enabledPlugins"), obj.get("extraKnownMarketplaces")
    return tuple(o for o in OLD if (isinstance(ep, dict) and o.plugin_id in ep)
                 or (isinstance(km, dict) and o.marketplace in km))


def remove_old_mod(old, run, out=print):
    """Uninstall the old mod `old` (an Old) and its marketplace.
    `run(args)` runs claude with args and returns (code, stdout, stderr)."""
    code, so, se = run(["plugin", "uninstall", old.plugin_id, "--scope", "user"])
    if code != 0:
        return False  # not installed (or claude says why): nothing of ours to remove
    out("  removed the %s mod (glidepath was called %s)" % (old.name, old.name))
    run(["plugin", "marketplace", "remove", old.marketplace])
    return True


def move_menubar(out=print):
    """Replace SwiftBar's old plugin (pacer.1m.sh, ctempo.1m.sh) with glidepath.1m.sh
    in the same folder."""
    from . import install, menubar

    state = install.load_state()
    path = state.get("menubar")
    if not isinstance(path, str):
        return False
    old = next((o for o in OLD if os.path.basename(path) == o.menubar), None)
    if old is None or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if old.mark not in f.read(4096):
                return False
        os.remove(path)
    except OSError:
        return False
    out("  SwiftBar: %s replaced (glidepath was called %s)" % (old.menubar, old.name))
    menubar.install(os.path.dirname(path))
    return True
