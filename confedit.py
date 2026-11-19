#!/usr/bin/env python3
"""Coordinated single-setting editor for Windscribe's preferences INI.

The engine is a Qt app that watches ~/.config/Windscribe/windscribe_cli.conf,
applies a change "as soon as the file is saved", and rewrites the same file
itself whenever its own state changes. A writer that edits a cached copy of
the file can therefore silently revert whatever the engine saved in between,
and a writer that never checks what landed can claim a change the engine
never saw. This helper closes both gaps as far as a non-Qt process can:

  * the file is read from disk at the moment of the edit, never from a cache,
    so the read-modify-write window is this process's own runtime, not the
    age of a cached copy
  * exactly one Key=value line in one section changes; every other byte --
    section order, unknown keys, spacing, the trailing-newline habit -- is
    handed back exactly as found (no extra newline is ever added)
  * the file is never created: a missing file, section or key is refused
  * the write is atomic: a temp file in the same directory, fsynced, then
    renamed over the original with the original's permissions kept
  * after writing, the key is read back from disk and compared with what
    was asked for; the value actually on disk is printed to stdout either
    way, so the caller can confirm what the engine has rather than assume
    what it was sent

Usage: confedit.py <file> <section> <key> <value>

Exit codes:
  0  written, and the key read back with exactly <value>
  3  file, section or key missing; nothing was written
  4  <value> cannot live on one INI line (contains a newline or NUL);
     nothing was written
  5  written, but the key read back different: another writer (the engine,
     most likely) replaced it between the write and the read-back; the
     current on-disk value is printed
  6  the file could not be read or written
"""
import os
import sys
import tempfile

EXIT_MISSING = 3
EXIT_REFUSED = 4
EXIT_RACED = 5
EXIT_IOERR = 6


def find_key(lines, section, key):
    """Index of the line holding `key` inside `[section]`, or None.

    The same rules the widget's own parser applies: a section header is a
    line whose trimmed form is exactly `[name]`, the first matching section
    wins, and a key line is the first line in it whose trimmed form splits
    at `=` into exactly `key`. The search stops at the next section header,
    so a key is never invented a home for.
    """
    in_section = False
    for i, line in enumerate(lines):
        trimmed = line.strip()
        if trimmed.startswith("["):
            if in_section:
                return None
            in_section = trimmed == "[" + section + "]"
            continue
        if not in_section:
            continue
        eq = trimmed.find("=")
        if eq <= 0:
            continue
        if trimmed[:eq].strip() == key:
            return i
    return None


def value_of(lines, section, key):
    """The current value of `key` (text after the first `=`, trimmed)."""
    i = find_key(lines, section, key)
    if i is None:
        return None
    trimmed = lines[i].strip()
    return trimmed[trimmed.find("=") + 1:].strip()


def rewrite_line(line, key, value):
    """The one changed line: original left padding, then `key=value`.

    Exactly what the widget's previous in-process editor wrote, so the
    engine sees no new kind of edit.
    """
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    return indent + key + "=" + value


def read_lines(path):
    """The file's lines, byte-faithful: surrogateescape keeps any bytes we
    never touch through the decode/encode round-trip, and newline="" keeps
    CRLF files CRLF."""
    with open(path, encoding="utf-8", errors="surrogateescape",
              newline="") as handle:
        return handle.read().split("\n")


def atomic_write(path, text, mode):
    """Write aside in the same directory, fsync, then rename over `path`."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".confedit-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape",
                       newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv):
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_REFUSED
    path, section, key, value = argv[1], argv[2], argv[3], argv[4]
    if "\n" in value or "\r" in value or "\x00" in value:
        return EXIT_REFUSED
    try:
        lines = read_lines(path)
    except FileNotFoundError:
        return EXIT_MISSING
    except OSError:
        return EXIT_IOERR
    at = find_key(lines, section, key)
    if at is None:
        return EXIT_MISSING
    lines[at] = rewrite_line(lines[at], key, value)
    try:
        atomic_write(path, "\n".join(lines), os.stat(path).st_mode & 0o7777)
    except OSError:
        return EXIT_IOERR
    # Confirm against what is actually on disk, not what we sent: the
    # engine may have saved over the file in the same instant.
    try:
        landed = value_of(read_lines(path), section, key)
    except OSError:
        return EXIT_IOERR
    if landed is not None:
        print(landed)
    return 0 if landed == value else EXIT_RACED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
