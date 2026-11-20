# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

**https://github.com/maplepreneur/omarchy-windscribe/security/advisories/new**

It goes only to the maintainer. Please include what you found, how to
reproduce it, and what you think the impact is. You'll get a reply, and
credit in the fix if you want it.

Please don't open a public issue for anything that could expose a user's
traffic, credentials, or session.

## What this widget does and doesn't do

Short version, the long one is in the README's *Security and privacy*
section:

- It stores no credentials, tokens, or account data. Your password and
  2FA code go straight into the Windscribe CLI's own prompt in a terminal;
  the plugin never sees them. The username you type in the panel is
  allow-listed and single-quoted before it reaches the terminal launcher,
  and anything outside that alphabet is refused, not escaped.
- It makes no network requests of its own, ever. The world map and city
  coordinates are bundled in the plugin; everything live comes from the
  engine's local socket via the CLI.
- It runs every command as an argument list, never through a shell, with
  the one sign-in exception above.
- It writes two files of its own under
  `~/.local/state/omarchy-windscribe/`, and one file that isn't its own —
  Windscribe's `~/.config/Windscribe/windscribe_cli.conf` — and only when
  you flip a switch the CLI has no command for. Those writes go through
  one bundled helper (confedit.py) that reads the file fresh at the
  moment of the edit, changes one line (every other byte comes back as
  found), and writes atomically. An overlapping engine write is detected
  — the file's identity is checked around the read, the write is staged
  aside (temp file, fsync, permissions) before the last identity check so
  that check and the rename sit back to back with no I/O between them,
  and the whole file is compared after the rename — and reconciled by
  re-applying the edit on top of the freshest content, so another
  setting's save is never silently reverted; a race that won't settle is
  reported, with the on-disk value read back, rather than claimed. They
  never happen on a timer, never while a CLI action is running, and the
  app list is encoded with Qt's own string-list rules so a path with a
  comma stays one entry.
- It never asks for root, and it never executes downloaded content.

If you can read QML, the whole widget is a handful of small files; the
security-relevant decisions are commented where they're made.
