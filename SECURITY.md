# Security

glidepath edits `~/.claude/settings.json`, writes launchers and runs a command on
every status line render, so security reports matter.

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a
vulnerability** on this repository (a private security advisory). Do not
open a public issue. Include what an attacker needs, what they get, and the
steps to reproduce. You should hear back within a week.

## What glidepath promises

- It never reads OAuth tokens or other credentials, and makes no network
  requests of its own (the optional `/usage` fetch runs Claude Code, which
  makes the request with its own login).
- It never stores, prints or logs message content from your transcripts:
  only each assistant message's time and token counts.
- It changes only the `statusLine` in `settings.json`, after a backup, and
  puts the previous one back on uninstall.
- Its data folder (`~/.glidepath`) is readable by you alone.

A way to break any of these is a vulnerability.
