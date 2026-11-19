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
  * an overlapping write is detected, never silently reverted: the file's
    identity (inode, mtime, size) is checked around the read and again
    immediately before the rename, and after the write the WHOLE file is
    re-read and compared byte for byte with what was meant to land. Any
    sign that another writer (the engine, most likely) touched the file in
    that window makes the helper drop its snapshot, re-read the freshest
    content, and re-apply its one-line edit on top of it, so the other
    writer's setting -- a firewall or routing key, say -- always survives
  * after the coordinated write, the key is read back from disk and
    compared with what was asked for; the value actually on disk is
    printed to stdout either way, so the caller can confirm what the
    engine has rather than assume what it was sent

Usage: confedit.py <file> <section> <key> <value>

Exit codes:
  0  written, and the whole file verified to be exactly the intended edit
  3  file, section or key missing; nothing was written
  4  <value> cannot live on one INI line (contains a newline or NUL);
     nothing was written
  5  another writer kept replacing the file around the edit, so the asked
     value could not be confirmed on disk; nothing was silently reverted,
     and the current on-disk value is printed
  6  the file could not be read or written
"""
import os
import sys
import tempfile

EXIT_MISSING = 3
EXIT_REFUSED = 4
EXIT_RACED = 5
EXIT_IOERR = 6

# How many times an edit is re-applied on top of fresher content before
# the race is reported instead. Engine saves are user-paced; two or three
# tries is far past any real burst.
ATTEMPTS = 5


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


def identity(st):
    """What makes a stat "the same file": inode plus content clock.

    The engine saves through rename, so a save always lands on a new
    inode; an in-place editor is still caught by mtime and size.
    """
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def main(argv):
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_REFUSED
    path, section, key, value = argv[1], argv[2], argv[3], argv[4]
    if "\n" in value or "\r" in value or "\x00" in value:
        return EXIT_REFUSED
    for attempt in range(ATTEMPTS):
        # Everything from here to the rename is this process's own
        # runtime; a writer landing inside it is the race the loop
        # reconciles, not a silent revert.
        try:
            before = os.stat(path)
            lines = read_lines(path)
            after_read = os.stat(path)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        if identity(after_read) != identity(before):
            continue  # written during the read: don't trust the copy
        at = find_key(lines, section, key)
        if at is None:
            return EXIT_MISSING
        lines[at] = rewrite_line(lines[at], key, value)
        text = "\n".join(lines)
        try:
            if identity(os.stat(path)) != identity(before):
                continue  # fresher content exists: re-read, don't clobber
            atomic_write(path, text, before.st_mode & 0o7777)
            landed_lines = read_lines(path)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        # Verify the whole file, not just our key: if a writer landed
        # during the rename, our snapshot reverted its change, and the
        # fix is to re-apply ours on top of what is there now.
        if "\n".join(landed_lines) != text:
            continue
        print(value_of(landed_lines, section, key))
        return 0
    # Another writer kept winning the file through every attempt. Its
    # content is what stands (nothing of ours is left to revert it), so
    # report the race and the value actually there instead of claiming
    # the save.
    try:
        lines = read_lines(path)
    except OSError:
        return EXIT_IOERR
    landed = value_of(lines, section, key)
    if landed is not None:
        print(landed)
    return EXIT_RACED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
