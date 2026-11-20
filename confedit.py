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
  * the commit is an atomic compare-and-swap where the platform allows
    it (Linux renameat2 with RENAME_EXCHANGE): the staged file and the
    conf file swap names atomically, and the helper ends up holding the
    exact file its edit replaced. If that displaced file is still the
    content the edit was based on, no writer interleaved and success is
    provable; if it is the engine's newer save instead, that save is
    kept on disk -- not unlinked -- until the edit re-applied on top of
    it has been committed and verified, so a crash or a full disk mid-
    merge cannot destroy it: the settings survive in that dotfile, and
    even the give-up paths leave it rather than drop a newer save. Only
    a verified clean commit removes the displaced copies. Either way an
    overlapping save is never overwritten unseen, so another writer's
    setting -- a firewall or routing key, say -- cannot be lost while
    the helper reports success. A save landing after the commit is
    caught by the whole-file read-back and reconciled the same way
  * where the exchange is unavailable (other OS, other architecture,
    older kernel, unsupporting filesystem) the helper falls back to a
    last identity check back to back with the rename: a microsecond gap
    remains there, no wider than the prepare-then-rename window the
    engine's own QSettings writer accepts on every save, and the
    read-back reconciliation still applies
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
import ctypes
import errno
import os
import platform
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


def stage_write(path, text, mode):
    """Stage `text` aside in the same directory: temp file, fsynced,
    permissions kept. The commit is a rename of this staged file, so all
    slow work is finished before the exchange: a writer landing during
    the staging is not a torn commit but a whole save the exchange will
    displace into the caller's hands. Returns the temp path.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".confedit-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape",
                       newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def identity(st):
    """What makes a stat "the same file": inode plus content clock.

    The engine saves through rename, so a save always lands on a new
    inode; an in-place editor is still caught by mtime and size.
    """
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _exchange_paths_impl():
    """renameat2(RENAME_EXCHANGE) as an atomic commit, or None.

    The exchange swaps two existing names atomically, so the helper ends
    up holding the exact file its edit replaced -- the one property a
    plain rename can never give a writer the engine does not cooperate
    with. Linux-only, via the stable syscall numbers for the two
    architectures Omarchy ships on; anywhere else, or on a kernel or
    filesystem that rejects the flag, the helper falls back (and its
    docstring says what that leaves open).
    """
    if platform.system() != "Linux":
        return None
    number = {"x86_64": 316, "aarch64": 276}.get(platform.machine())
    if number is None:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p,
                        ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    at_fdcwd = -100
    rename_exchange = 2

    def exchange(old, new):
        if syscall(number, at_fdcwd, os.fsencode(old),
                   at_fdcwd, os.fsencode(new), rename_exchange) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), old)

    return exchange


# None where the atomic exchange is unavailable; main() then uses the
# fallback commit for every attempt (and demotes permanently if the
# first exchange call reports the kernel or fs lacks support).
exchange_paths = _exchange_paths_impl()


def edit_line(lines, section, key, value):
    """`lines` with the one `key` line in `section` rewritten, or None
    if the key has no home (the caller refuses the edit)."""
    at = find_key(lines, section, key)
    if at is None:
        return None
    lines = list(lines)
    lines[at] = rewrite_line(lines[at], key, value)
    return lines


def main(argv):
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_REFUSED
    path, section, key, value = argv[1], argv[2], argv[3], argv[4]
    if "\n" in value or "\r" in value or "\x00" in value:
        return EXIT_REFUSED
    exchange = exchange_paths
    for attempt in range(ATTEMPTS):
        # Everything from here to the commit is this process's own
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
        installed = edit_line(lines, section, key, value)
        if installed is None:
            return EXIT_MISSING
        text = "\n".join(installed)
        # `believed`: the content known to be at path right now. The
        # commit loop compares what its exchange displaces against this,
        # so a writer is never overwritten unseen.
        believed = lines
        tmp = None
        pending = []  # displaced files kept until the merged commit lands
        committed = False
        stale = False
        try:
            tmp = stage_write(path, text, before.st_mode & 0o7777)
            for _rebase in range(ATTEMPTS):
                if exchange is None:
                    # Fallback for no renameat2: the last identity check
                    # back to back with the rename. What it cannot see
                    # is a save completing between the check and the
                    # rename -- the gap the docstring is honest about.
                    if identity(os.stat(path)) != identity(before):
                        stale = True
                        break  # fresher content: re-read, don't clobber
                    os.replace(tmp, path)
                    tmp = None
                    committed = True
                    break
                try:
                    exchange(tmp, path)
                except OSError as error:
                    if error.errno in (errno.ENOSYS, errno.EINVAL,
                                       errno.EOPNOTSUPP):
                        exchange = None  # no atomic commit here: demote
                        continue
                    raise
                # The displaced file now sits at tmp. Reading it answers
                # the question a plain rename never can: what exactly did
                # our edit replace?
                displaced = read_lines(tmp)
                if displaced == believed:
                    # Nothing interleaved: our text is installed. The
                    # displaced copy holds only the content it was built
                    # from, so every pending displaced file -- each one
                    # superseded by what just landed -- is safe to drop.
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass  # commit landed; a leftover dotfile is harmless
                    tmp = None
                    for spare in pending:
                        try:
                            os.unlink(spare)
                        except OSError:
                            pass
                    pending = []
                    committed = True
                    break
                # Keep the displaced file until the merged commit lands:
                # it is the only on-disk copy of the newer save. If the
                # process dies or the staging fails mid-rebase, the
                # settings survive in this dotfile instead of vanishing
                # with our unlink; the engine ignores dotfiles here.
                pending.append(tmp)
                tmp = None
                # Path currently holds our previous staged text, so that
                # is what the next exchange must displace for the round
                # to be clean. Each rebase consumes one save; saves are
                # user-paced, so this ends.
                believed = text.split("\n")
                installed = edit_line(displaced, section, key, value)
                if installed is None:
                    return EXIT_MISSING
                text = "\n".join(installed)
                tmp = stage_write(path, text, before.st_mode & 0o7777)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if stale or not committed:
            # Deliberately leaving any pending displaced files behind:
            # each holds a newer save whose merge did not land, and the
            # engine's settings are worth more than a tidy directory.
            continue  # fresher content, or too hot: fresh read, retry
        try:
            landed_lines = read_lines(path)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        # A writer landing after the commit is caught here: the file is
        # not what we installed, so re-apply on top instead of claiming.
        if "\n".join(landed_lines) != text:
            continue
        print(value_of(landed_lines, section, key))
        return 0
    # Another writer kept winning the file through every attempt. Its
    # content is what stands (nothing of ours is left to revert it), so
    # report the race and the value actually there instead of claiming
    # the save. Any pending displaced dotfiles are left in the directory
    # on purpose: they preserve newer saves whose merge never landed.
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
