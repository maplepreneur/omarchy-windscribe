#!/usr/bin/env python3
"""Coordinated single-setting editor for Windscribe's preferences INI.

The engine is a Qt app that watches ~/.config/Windscribe/windscribe_cli.conf,
applies a change "as soon as the file is saved", and rewrites the same file
itself (a whole-file rename) whenever its own state changes. A writer that
edits a cached copy can silently revert whatever the engine saved in
between, and a writer that never checks what landed can claim a change the
engine never saw. This helper closes both gaps as far as a non-Qt process
can:

  * the file is read from disk at the moment of the edit, never from a cache
  * exactly one Key=value line in one section changes; every other byte is
    handed back exactly as found (no extra newline is ever added)
  * the file is never created: a missing file, section or key is refused
  * the commit is an atomic exchange (Linux renameat2 RENAME_EXCHANGE):
    the staged file and the conf file swap names, so the helper ends up
    holding the exact file its edit replaced

Ordering by identity, not by content
------------------------------------
Every decision about which preferences file is newer is made from file
identity (device, inode, mtime, size), never by comparing or hashing
contents. Two facts make that sound:

  1. An exchange is atomic, so the file it hands back was the file at the
     active name at that instant: every engine save before the exchange is
     either that file or was replaced by it.
  2. The engine saves by rename, so every save is a new inode. Every inode
     the helper reasons about is held open for the whole run, so no inode
     number can be recycled under it.

So after any swap, the file in the side ("hold") name is the newest
preferences the engine has written, and the file just placed on the
active name is older -- unless the hold got back exactly the file the
helper itself placed on the active name one swap earlier, which proves no
save landed in between. "Publishing" a hold is therefore a short loop:
swap the hold onto the active name; if what comes back is the file we put
there last time, the active file is the newest save and the hold is
deleted; otherwise what came back is a newer save, and it is swapped in
next. If, before a swap, the active name holds anything other than the
file the helper last put there, the engine saved after the hold left it:
that save is newer, so the hold is obsolete and is deleted unread.

Nothing is ever decided by reading a displaced file, so a displaced file
that cannot be read can never be mistaken for an older one, and there is
no content (and no ABA on identical bytes) to guess from.

Crashes and give-ups
--------------------
Before every swap the helper writes, fsyncs and directory-fsyncs a
one-line journal next to the conf file naming the hold, the identity it is
about to place on the active name ("put") and the identity it expects to
displace ("prev"). Every run resolves an outstanding journal first, with
the same rules -- the hold still holding "put" means the swap never ran;
the hold holding "prev" means it ran cleanly; anything else is a newer
save -- and is not allowed to report success while a hold may be newer
than the active file. A run that runs out of attempts leaves the journal
and the hold in place and exits 5; the next run finishes the publish
before it edits anything.

Where the exchange is unavailable (other OS, other architecture, older
kernel, unsupporting filesystem) the helper falls back to a last identity
check back to back with the rename: a microsecond gap remains there, no
wider than the prepare-then-rename window the engine's own QSettings
writer accepts on every save. Omarchy (Linux, x86_64/aarch64, btrfs or
ext4) always takes the exchange path.

Usage: confedit.py <file> <section> <key> <value>

Exit codes:
  0  the active file was verified to be exactly the newest engine save
     plus the one changed line, with no newer save held aside
  3  file, section or key missing; nothing was written
  4  <value> cannot live on one INI line (contains a newline or NUL);
     nothing was written
  5  another writer kept replacing the file, so the value could not be
     confirmed; nothing was silently reverted and the current on-disk
     value is printed. A newer save still held aside is journaled and is
     published by the next run before it edits
  6  the file could not be read or written. Anything that might be a
     newer save is left on disk and journaled, never deleted
"""
import ctypes
import errno
import fcntl
import hashlib
import os
import platform
import sys
import tempfile

EXIT_MISSING = 3
EXIT_REFUSED = 4
EXIT_RACED = 5
EXIT_IOERR = 6

# How many times an edit is re-read and re-applied on top of a newer save
# before the race is reported instead. Engine saves are user-paced.
ATTEMPTS = 5

# How many swaps one publish may take. Each extra swap means the engine
# saved again inside a few microseconds of the previous one.
PUBLISH_ATTEMPTS = 8

JOURNAL_NAME = ".confedit-journal"
JOURNAL_VERSION = "v2"
# Staged edits. A swap only ever happens to a staged file after the
# journal names it, so once the journal is clear (and with the editor
# lock held) any file left with this prefix is an edit that never got
# swapped in -- the helper's own text, never a save -- and is removed.
STAGE_PREFIX = ".confedit-stage-"


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


def read_text(path):
    """The file's exact bytes, decoded the same way read_lines writes back."""
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8", "surrogateescape")


def stage_write(path, text, mode):
    """Stage `text` aside in the same directory: temp file, fsynced,
    permissions kept. All slow work is finished before the exchange.
    Returns the temp path."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=STAGE_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape",
                       newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    return tmp


def identity(st):
    """What makes a stat "the same file". The engine saves through rename,
    so a save is always a new inode; an in-place writer is still caught by
    mtime and size. A rename or exchange changes none of these."""
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _exchange_paths_impl():
    """renameat2(RENAME_EXCHANGE) as an atomic commit, or None.

    Linux-only, via the stable syscall numbers for the two architectures
    Omarchy ships on; anywhere else, or on a kernel or filesystem that
    rejects the flag, the helper falls back.
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


# None where the atomic exchange is unavailable. Captured per run in
# main(), so the race tests can patch it.
exchange_paths = _exchange_paths_impl()

_UNSUPPORTED = (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP)


def edit_line(lines, section, key, value):
    """`lines` with the one `key` line in `section` rewritten, or None
    if the key has no home (the caller refuses the edit)."""
    at = find_key(lines, section, key)
    if at is None:
        return None
    lines = list(lines)
    lines[at] = rewrite_line(lines[at], key, value)
    return lines


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- journal ---------------------------------------------------------------
#
# At most one outstanding swap, on one line:
#   v2 <TAB> hold <TAB> ours <TAB> put <TAB> prev
# hold: the side file's path (always in the conf file's directory).
# ours: "1" if `put` is the helper's own staged edit, "0" if it is a save
#   being published back.
# put:  identity of the file the swap places on the active name.
# prev: identity of the file the swap expects to take off it.
# An identity is dev:ino:mtime_ns:size.


class LegacyJournal(Exception):
    """A journal written by v1.0.6, which ordered snapshots by hash."""


def _journal_path(conf_path):
    return os.path.join(os.path.dirname(conf_path) or ".", JOURNAL_NAME)


def _ident_text(ident):
    return ":".join(str(part) for part in ident)


def _ident_parse(text, path):
    parts = text.split(":")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise OSError(errno.EINVAL, "torn settings journal", path)
    return tuple(int(p) for p in parts)


def load_journal(conf_path):
    """The outstanding swap, or None. Torn is an error: guessing could
    delete a newer save or ignore one."""
    path = _journal_path(conf_path)
    try:
        text = read_text(path)
    except FileNotFoundError:
        return None
    lines = [line for line in text.split("\n") if line]
    if not lines:
        return None
    if not lines[0].startswith(JOURNAL_VERSION + "\t"):
        raise LegacyJournal(lines)
    parts = lines[0].split("\t")
    if len(lines) != 1 or len(parts) != 5 or parts[2] not in ("0", "1"):
        raise OSError(errno.EINVAL, "torn settings journal", path)
    hold = parts[1]
    if os.path.dirname(hold) != os.path.dirname(conf_path):
        raise OSError(errno.EINVAL, "settings journal names a foreign file", path)
    return {
        "hold": hold,
        "ours": parts[2] == "1",
        "put": _ident_parse(parts[3], path),
        "prev": _ident_parse(parts[4], path),
    }


def save_journal(conf_path, record):
    """Atomically replace the journal (fsync, rename, fsync the directory),
    or remove it when `record` is None."""
    path = _journal_path(conf_path)
    directory = os.path.dirname(path) or "."
    if record is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        _fsync_dir(directory)
        return
    line = "\t".join((JOURNAL_VERSION, record["hold"],
                      "1" if record["ours"] else "0",
                      _ident_text(record["put"]),
                      _ident_text(record["prev"]))) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".confedit-journal-", dir=directory)
    try:
        try:
            os.write(fd, line.encode("utf-8", "surrogateescape"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        _fsync_dir(directory)
    finally:
        if tmp is not None:
            _unlink_quiet(tmp)


# --- one run ---------------------------------------------------------------


class Run:
    """One invocation's view of the conf file.

    Every file whose identity is taken is held open until the run ends,
    so an inode the helper compares against cannot be freed and recycled
    as a different file mid-run.
    """

    def __init__(self, conf, exchange):
        self.conf = conf
        self.conf_dir = os.path.dirname(conf) or "."
        self.exchange = exchange
        self._pins = []

    def stat(self, path):
        """Open `path` and keep it open; return its fstat."""
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self._pins.append(fd)
        return os.fstat(fd)

    def ident(self, path):
        return identity(self.stat(path))

    def close(self):
        for fd in self._pins:
            try:
                os.close(fd)
            except OSError:
                pass
        self._pins = []


def _settle(run, hold):
    """The hold is obsolete or already published: delete it, then the
    journal. In that order, so a crash in between leaves a journal whose
    hold is gone, which the next run simply clears."""
    try:
        os.unlink(hold)
    except FileNotFoundError:
        pass
    except OSError:
        return EXIT_IOERR
    try:
        save_journal(run.conf, None)
    except OSError:
        return EXIT_IOERR
    return "ok"


def publish(run, hold, expect):
    """Make the newest save the active file.

    Precondition: `hold` holds a file that left the active name after
    the file identified by `expect` was placed there, i.e. it is newer
    than `expect`. Returns "ok" (the active file is now the newest save,
    nothing held aside), EXIT_RACED (still racing; journal and hold
    kept), or an exit code for an error (journal and hold kept).
    """
    if run.exchange is None:
        return EXIT_IOERR
    for _ in range(PUBLISH_ATTEMPTS):
        try:
            active = run.ident(run.conf)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        if active != expect:
            # The engine saved after the hold left the active name. That
            # save is a whole file and is newer than the hold.
            return _settle(run, hold)
        try:
            held = run.ident(hold)
            save_journal(run.conf, {"hold": hold, "ours": False,
                                    "put": held, "prev": expect})
            run.exchange(hold, run.conf)
        except OSError:
            return EXIT_IOERR
        try:
            _fsync_dir(os.path.dirname(run.conf) or ".")
            back = run.ident(hold)
        except OSError:
            return EXIT_IOERR
        if back == expect:
            # Nothing landed between the two swaps: the active file is
            # the newest save, and what came back is the older file.
            return _settle(run, hold)
        # A newer save was on the active name and is now in the hold;
        # the file we just put there is older. Publish the newer one.
        expect = held
    return EXIT_RACED


def _recover_legacy(run, records):
    """Finish what a v1.0.6 journal left, as far as its own records allow.

    That format ordered snapshots by content hash. Its one unambiguous
    case is kept: if the active file is still a file the old helper
    installed (or was about to replace), nobody saved after it, so the
    newest held snapshot that is not that file is newer and is published.
    Any other side file is left on disk untouched, never deleted.
    """
    def sha(text):
        return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()

    parsed = []
    for line in records:
        parts = line.split("\t")
        if len(parts) != 5:
            return EXIT_IOERR
        parsed.append({"ours": parts[0], "expect": parts[3], "file": parts[4]})
    try:
        active_stat = run.stat(run.conf)
        active_sha = sha(read_text(run.conf))
    except FileNotFoundError:
        return EXIT_MISSING
    except OSError:
        return EXIT_IOERR
    candidate = None
    if any(active_sha in (rec["ours"], rec["expect"]) for rec in parsed):
        for rec in parsed:
            if os.path.dirname(rec["file"]) != run.conf_dir:
                continue
            try:
                held_sha = sha(read_text(rec["file"]))
            except FileNotFoundError:
                continue
            except OSError:
                return EXIT_IOERR
            if held_sha not in (rec["ours"], active_sha):
                candidate = rec["file"]
    if candidate is not None:
        status = publish(run, candidate, identity(active_stat))
        if status != "ok":
            return status
    for rec in parsed:
        try:
            if sha(read_text(rec["file"])) == rec["ours"]:
                _unlink_quiet(rec["file"])  # the old helper's own staged text
        except OSError:
            pass
    try:
        save_journal(run.conf, None)
    except OSError:
        return EXIT_IOERR
    return "ok"


def recover(run):
    status = _recover(run)
    if status == "ok":
        _sweep(run)
    return status


def _sweep(run):
    """Remove staged edits and journal temp files a crash left behind.
    Only called with the lock held and no journal outstanding."""
    try:
        names = os.listdir(run.conf_dir)
    except OSError:
        return
    for name in names:
        if name.startswith(STAGE_PREFIX) or name.startswith(JOURNAL_NAME + "-"):
            _unlink_quiet(os.path.join(run.conf_dir, name))


def _recover(run):
    """Resolve a swap a previous run left outstanding. Returns "ok" or an
    exit code; on anything but "ok" the journal and the hold are kept."""
    try:
        record = load_journal(run.conf)
    except LegacyJournal as legacy:
        return _recover_legacy(run, legacy.args[0])
    except OSError:
        return EXIT_IOERR
    if record is None:
        return "ok"
    hold = record["hold"]
    try:
        held = run.ident(hold)
    except FileNotFoundError:
        # Already settled; only the journal removal was lost.
        try:
            save_journal(run.conf, None)
        except OSError:
            return EXIT_IOERR
        return "ok"
    except OSError:
        return EXIT_IOERR
    if held == record["put"]:
        # The swap never ran.
        if record["ours"]:
            return _settle(run, hold)       # our unused staged edit
        return publish(run, hold, record["prev"])
    if held == record["prev"]:
        # The swap ran and displaced exactly what it expected.
        return _settle(run, hold)
    # The swap ran and displaced a newer save.
    return publish(run, hold, record["put"])


def _confirm(run, ours, our_text, section, key):
    """Success only when the active file is, right now, the edit we
    installed: same inode, same mtime, same size."""
    try:
        active = run.ident(run.conf)
    except FileNotFoundError:
        return EXIT_MISSING
    except OSError:
        return EXIT_IOERR
    if active != ours:
        return "retry"  # a save landed after the commit; re-apply on it
    print(value_of(our_text.split("\n"), section, key))
    return 0


def _commit_fallback(run, base, our_text, mode, section, key):
    """No exchange: last identity check back to back with the rename."""
    try:
        staged = stage_write(run.conf, our_text, mode)
    except OSError:
        return EXIT_IOERR
    try:
        ours = run.ident(staged)
        if run.ident(run.conf) != base:
            return "retry"
        os.replace(staged, run.conf)
        staged = None
    except FileNotFoundError:
        return EXIT_MISSING
    except OSError:
        return EXIT_IOERR
    finally:
        if staged is not None:
            _unlink_quiet(staged)
    return _confirm(run, ours, our_text, section, key)


def _commit(run, base, our_text, mode, section, key):
    """Install `our_text` (an edit of the file identified by `base`).

    Returns 0, "retry" (re-read and re-apply on the newest save), or an
    exit code.
    """
    if run.exchange is None:
        return _commit_fallback(run, base, our_text, mode, section, key)
    try:
        staged = stage_write(run.conf, our_text, mode)
    except OSError:
        return EXIT_IOERR
    try:
        ours = run.ident(staged)
        save_journal(run.conf, {"hold": staged, "ours": True,
                                "put": ours, "prev": base})
    except OSError:
        _unlink_quiet(staged)
        return EXIT_IOERR
    try:
        run.exchange(staged, run.conf)
    except OSError as error:
        # Nothing moved: `staged` still holds our text.
        if _settle(run, staged) != "ok":
            return EXIT_IOERR
        if error.errno in _UNSUPPORTED:
            run.exchange = None
            return _commit_fallback(run, base, our_text, mode, section, key)
        if error.errno == errno.ENOENT:
            return EXIT_MISSING
        return EXIT_IOERR
    try:
        _fsync_dir(os.path.dirname(run.conf) or ".")
        displaced = run.ident(staged)
    except OSError:
        return EXIT_IOERR  # journaled: the next run classifies the hold
    if displaced == base:
        # The edit replaced exactly the file it was made from.
        if _settle(run, staged) != "ok":
            return EXIT_IOERR
        return _confirm(run, ours, our_text, section, key)
    # The edit displaced a newer save and our stale-based text is active.
    # Put the newer save back before anything else, then re-apply.
    status = publish(run, staged, ours)
    return "retry" if status == "ok" else status


def _report_race(run, section, key):
    try:
        landed = value_of(read_lines(run.conf), section, key)
    except OSError:
        return EXIT_IOERR
    if landed is not None:
        print(landed)
    return EXIT_RACED


def _edit(run, section, key, value):
    status = recover(run)
    if status == EXIT_RACED:
        return _report_race(run, section, key)
    if status != "ok":
        return status
    for _attempt in range(ATTEMPTS):
        try:
            before = run.stat(run.conf)
            lines = read_lines(run.conf)
            after = os.stat(run.conf)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        base = identity(before)
        if identity(after) != base:
            continue  # a save landed mid-read; the copy may be stale
        installed = edit_line(lines, section, key, value)
        if installed is None:
            return EXIT_MISSING
        if installed == lines:
            print(value_of(lines, section, key))  # already so; nothing to write
            return 0
        outcome = _commit(run, base, "\n".join(installed),
                          before.st_mode & 0o7777, section, key)
        if outcome == EXIT_RACED:
            return _report_race(run, section, key)
        if outcome != "retry":
            return outcome
    return _report_race(run, section, key)


def _lock_editor(conf_path):
    """Serialize our own writers so two helpers never interleave swaps.

    The engine does not take this lock; overlapping engine saves are the
    exchange's job. The lock lives in the runtime dir, so a clean save
    leaves the engine's directory untouched.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    digest = hashlib.sha256(conf_path.encode("utf-8", "surrogateescape")).hexdigest()[:32]
    lock_path = os.path.join(runtime, ".omarchy-windscribe-confedit-" + digest + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def main(argv):
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_REFUSED
    path, section, key, value = argv[1], argv[2], argv[3], argv[4]
    if "\n" in value or "\r" in value or "\x00" in value:
        return EXIT_REFUSED
    # Swaps act on names. A symlinked conf is edited where it really
    # lives, so the link itself is never replaced by a regular file.
    path = os.path.realpath(path)
    try:
        lock_fd = _lock_editor(path)
    except OSError:
        return EXIT_IOERR
    run = Run(path, exchange_paths)
    try:
        return _edit(run, section, key, value)
    finally:
        run.close()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
