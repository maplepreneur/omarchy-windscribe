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
    provable. If it is the engine's newer save instead, that save is
    published back onto the active name before the edit is re-applied on
    top of it -- the active file Windscribe reloads, not a side file it
    ignores. A side file is only kept when it could not be read or could
    not be published, and it is recorded in a journal next to the conf
    file. The next run publishes that snapshot to the active file before
    it is allowed to report success, so a retry cannot claim an older
    preferences file while the newest save sits unread beside it. A
    displaced copy is never deleted if the read of it failed
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
     of the newest snapshot observed; no newer save left only in a side file
  3  file, section or key missing; nothing was written
  4  <value> cannot live on one INI line (contains a newline or NUL);
     nothing was written
  5  another writer kept replacing the file around the edit, so the asked
     value could not be confirmed on disk; nothing was silently reverted,
     and the current on-disk value is printed. The active file is the
     newest snapshot the helper could publish, not an older one
  6  the file could not be read or written. A displaced copy that could
     not be read is left in place, not deleted
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

# How many times an edit is re-applied on top of fresher content before
# the race is reported instead. Engine saves are user-paced; two or three
# tries is far past any real burst.
ATTEMPTS = 5

# One line per outstanding exchange, rewritten atomically and fsynced
# before the swap it describes:
#   ours <TAB> displaced <TAB> phase <TAB> expect <TAB> file
# ours: hash of the text that swap installed (or was about to).
# displaced: hash of the snapshot the swap removed, or "-" until read.
# phase: "swap" (forward exchange) or "publish" (about to put the
#   snapshot back on the active name).
# expect: hash of the active file a publish is allowed to replace, or
#   "-". Recorded and fsynced before the publish swap, so a crash cannot
#   be mistaken for "the active file moved on" and the snapshot dropped,
#   and a later engine save (active hash no longer expect) is not
#   overwritten by the older snapshot.
# A later run publishes a hold back onto the active name before it can
# report success. The engine does not reload side files.
JOURNAL_NAME = ".confedit-journal"


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


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


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


def _journal_path(conf_path):
    return os.path.join(os.path.dirname(os.path.abspath(conf_path)) or ".",
                        JOURNAL_NAME)


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_journal(conf_path):
    """Outstanding exchanges, or [] if this conf has none.

    A malformed journal is an error: guessing could delete a displaced
    save or ignore one. Missing is not an error.
    """
    path = _journal_path(conf_path)
    try:
        text = read_text(path)
    except FileNotFoundError:
        return []
    records = []
    for line in text.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 5 or not parts[0] or not parts[4]:
            raise OSError(errno.EINVAL, "torn settings journal", path)
        if parts[2] not in ("swap", "publish"):
            raise OSError(errno.EINVAL, "torn settings journal", path)
        records.append({
            "ours": parts[0],
            "displaced": None if parts[1] == "-" else parts[1],
            "phase": parts[2],
            "expect": None if parts[3] == "-" else parts[3],
            "file": parts[4],
        })
    return records


def save_journal(conf_path, records):
    """Atomically replace the journal, or remove it when nothing is outstanding.

    fsync before the rename, then the directory, so a crash cannot leave
    a swap unrecorded or a torn record that a later run would misread.
    """
    path = _journal_path(conf_path)
    directory = os.path.dirname(path) or "."
    if not records:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        _fsync_dir(directory)
        return
    lines = []
    for rec in records:
        displaced = rec["displaced"] if rec["displaced"] else "-"
        phase = rec.get("phase") or "swap"
        expect = rec.get("expect") or "-"
        lines.append("\t".join((rec["ours"], displaced, phase, expect, rec["file"])))
    payload = ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape")
    fd, tmp = tempfile.mkstemp(prefix=".confedit-journal-", dir=directory)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        _fsync_dir(directory)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _journaled(conf_path, file_path):
    """True if this file may hold a displaced save. An unreadable journal
    counts as yes: the safe side is not deleting."""
    try:
        records = load_journal(conf_path)
    except OSError:
        return True
    return any(rec["file"] == file_path for rec in records)


def _add_record(conf_path, file_path, ours_hash):
    records = load_journal(conf_path)
    records = [rec for rec in records if rec["file"] != file_path]
    records.append({
        "ours": ours_hash, "displaced": None, "phase": "swap",
        "expect": None, "file": file_path,
    })
    save_journal(conf_path, records)


def _set_displaced(conf_path, file_path, displaced_hash, phase=None, expect=None):
    records = load_journal(conf_path)
    for rec in records:
        if rec["file"] == file_path:
            rec["displaced"] = displaced_hash
            if phase:
                rec["phase"] = phase
            if expect is not None:
                rec["expect"] = expect
    save_journal(conf_path, records)


def _drop_record(conf_path, file_path):
    records = load_journal(conf_path)
    save_journal(conf_path, [rec for rec in records if rec["file"] != file_path])


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _lock_editor(conf_path):
    """Serialize our own writers so two helpers can't drop each other's journal.

    The engine does not take this lock; overlapping engine saves are the
    exchange's job. The lock lives in the runtime dir, not beside the conf
    file, so a clean save leaves the engine's directory untouched.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    digest = hashlib.sha256(os.path.abspath(conf_path).encode()).hexdigest()[:32]
    lock_path = os.path.join(runtime, ".omarchy-windscribe-confedit-" + digest + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def publish_snapshot(conf_path, hold_path, ours_text, exchange, allow_any_active=False):
    """Put the hold's snapshot back on the active name.

    Exchange, not replace: a save that lands on the active name during
    the publish is swapped into the hold and published on the next pass,
    never overwritten. Does not unlink the hold -- the caller does that
    only after it has read the active file and confirmed the snapshot
    (or a newer one) is there. Returns "published" or "io".
    """
    if exchange is None:
        return "io"
    # The first pass may only replace our stale install, or the exact
    # file an unfinished publish already committed to replacing. After
    # that swap, the hold may contain a newer save the engine landed in
    # the gap; later passes must publish that, or it would be dropped as
    # "moved" and the newer setting lost.
    engaged = False
    for _ in range(ATTEMPTS):
        try:
            snapshot = read_text(hold_path)
        except OSError:
            return "io"
        try:
            active = read_text(conf_path)
        except FileNotFoundError:
            try:
                os.replace(hold_path, conf_path)
            except OSError:
                return "io"
            return "published"
        except OSError:
            return "io"
        if active == snapshot:
            return "published"
        active_hash = text_sha(active)
        ours_hash = text_sha(ours_text) if ours_text is not None else None
        # Only replace the active file when it is still our stale install
        # or the exact file this publish was already committed to
        # replacing. Anything else landed later and must not be overwritten
        # by this snapshot.
        try:
            records = load_journal(conf_path)
        except OSError:
            return "io"
        expect = None
        for rec in records:
            if rec["file"] == hold_path:
                expect = rec.get("expect")
        if (not engaged and not allow_any_active
                and active_hash != ours_hash and active_hash != expect):
            return "moved"
        engaged = True
        try:
            # phase and expect hit disk before the swap. A crash leaves
            # the snapshot in the hold and the active file unchanged;
            # the next run publishes if the active file is still `expect`,
            # and leaves a newer engine save alone if it is not.
            _set_displaced(conf_path, hold_path, text_sha(snapshot),
                           phase="publish", expect=active_hash)
            exchange(hold_path, conf_path)
        except OSError:
            return "io"
        try:
            came = read_text(hold_path)
            now = read_text(conf_path)
        except OSError:
            return "io"
        if now == snapshot and (came == active or came == ours_text):
            return "published"
        if now != snapshot and came in (active, ours_text):
            # The swap put the snapshot on the active name, then a writer
            # replaced it. The hold has the previous active file, so the
            # bytes now at the active name are the newer save. Do not
            # treat "hold still has the snapshot" as published: that is
            # the only copy, and deleting it would lose the save.
            return "published"
        # A newer snapshot was at the active name and is now in the hold,
        # or the swap did not land. Publish whatever the hold holds now.
        continue
    return "io"


def reconcile(conf_path, exchange):
    """Make the active file the newest snapshot a previous run displaced.

    Called at the start of every edit, so a later retry cannot report
    success with an older preferences file while the newest save remains
    in a side file Windscribe does not reload.

    Returns "ok" or "io". Never deletes a file it could not read, and
    never deletes a displaced snapshot until the active file has been
    read and contains that snapshot or a newer one.
    """
    try:
        records = load_journal(conf_path)
    except OSError:
        return "io"
    if not records:
        return "ok"

    displaced = []
    for rec in records:
        if not os.path.exists(rec["file"]):
            continue
        try:
            content = read_text(rec["file"])
        except OSError:
            return "io"
        if text_sha(content) == rec["ours"]:
            # Our staged text: the swap never happened, or a restore
            # already put the displaced snapshot back on the active name
            # and left our text here. Safe to drop.
            _unlink_quiet(rec["file"])
            continue
        displaced.append((rec, content))

    try:
        save_journal(conf_path, [rec for rec, _content in displaced])
    except OSError:
        return "io"
    if not displaced:
        return "ok"

    try:
        active = read_text(conf_path) if os.path.exists(conf_path) else None
    except OSError:
        return "io"

    newest_rec, newest_content = displaced[-1]

    def keep():
        return "io"

    if active is None:
        try:
            os.replace(newest_rec["file"], conf_path)
        except OSError:
            return keep()
        for rec, _content in displaced[:-1]:
            _unlink_quiet(rec["file"])
        try:
            save_journal(conf_path, [])
        except OSError:
            return "io"
        return "ok"

    active_hash = text_sha(active)
    if active == newest_content and all(
            content == active or text_sha(content) == rec["ours"]
            for rec, content in displaced):
        for rec, _content in displaced:
            _unlink_quiet(rec["file"])
        try:
            save_journal(conf_path, [])
        except OSError:
            return "io"
        return "ok"

    # Which hold must become the active file before success is allowed.
    # Our stale install, the exact file an unfinished publish was about
    # to replace, or a hold that has since picked up a later snapshot.
    # A hold that still matches the snapshot it displaced, while the
    # active file is neither of those, was superseded by a later full
    # save and is not published over it.
    chosen = None
    for rec, content in displaced:
        content_hash = text_sha(content)
        changed = (rec["displaced"] is not None
                   and content_hash != rec["displaced"]
                   and content_hash != rec["ours"])
        unfinished = (rec.get("phase") == "publish"
                      and rec.get("expect")
                      and active_hash == rec["expect"]
                      and (rec["displaced"] is None or content_hash == rec["displaced"]))
        if active_hash == rec["ours"] or unfinished or changed:
            chosen = (rec, content)
    if chosen is None:
        for rec, _content in displaced:
            _unlink_quiet(rec["file"])
        try:
            save_journal(conf_path, [])
        except OSError:
            return "io"
        return "ok"

    chosen_rec, chosen_content = chosen
    content_hash = text_sha(chosen_content)
    captured_later = (chosen_rec["displaced"] is not None
                      and content_hash != chosen_rec["displaced"]
                      and content_hash != chosen_rec["ours"])
    status = publish_snapshot(
        conf_path, chosen_rec["file"], active, exchange,
        allow_any_active=captured_later)
    if status == "moved":
        # The active file is no longer the install this hold may replace,
        # so the hold is the older snapshot. Drop it and reconsider; do
        # not overwrite the newer active file.
        _unlink_quiet(chosen_rec["file"])
        if os.path.exists(chosen_rec["file"]):
            return keep()
        return reconcile(conf_path, exchange)
    if status != "published":
        return keep()
    try:
        now = read_text(conf_path)
    except OSError:
        return keep()
    if now != chosen_content and text_sha(now) == active_hash:
        # Publish did not move the active file off the stale snapshot.
        return keep()
    # Drop a hold only when its bytes are now the active file, our staged
    # text, or the previous active file the swap left behind. Anything
    # else may be a newer save and stays, blocking success.
    for rec, _content in displaced:
        try:
            if not os.path.exists(rec["file"]):
                continue
            left = read_text(rec["file"])
        except OSError:
            return keep()
        if left == now or text_sha(left) == rec["ours"] or left == active:
            _unlink_quiet(rec["file"])
            continue
        return keep()
    try:
        save_journal(conf_path, [])
    except OSError:
        return "io"
    return "ok"


def _restore_unread(path, hold_path, our_text, exchange):
    """The displaced file could not be parsed. Do not delete it.

    Swap it back onto the active name when that is possible, so
    Windscribe reloads those bytes instead of our stale install. If the
    swap itself fails, leave the file and the journal and refuse to
    continue. Returns "retry" or EXIT_IOERR.
    """
    if exchange is None:
        return EXIT_IOERR
    try:
        exchange(hold_path, path)
    except OSError:
        return EXIT_IOERR
    try:
        came = read_text(hold_path)
    except OSError:
        # Active name has the unread bytes (Windscribe can reload them).
        # The hold has whatever was active; we could not classify it, so
        # it stays on disk and blocks a later success until it can be read.
        return EXIT_IOERR
    if text_sha(came) == text_sha(our_text):
        try:
            _drop_record(path, hold_path)
        except OSError:
            return EXIT_IOERR
        _unlink_quiet(hold_path)
        return "retry"
    # A newer snapshot landed on the active name between the two swaps
    # and is now in the hold. Publish it; do not drop it unread.
    if publish_snapshot(path, hold_path, our_text, exchange) != "published":
        return EXIT_IOERR
    try:
        now = read_text(path)
        left = read_text(hold_path)
    except OSError:
        return EXIT_IOERR
    if now == our_text or (left != our_text and left != came and now != came):
        return EXIT_IOERR
    try:
        _drop_record(path, hold_path)
    except OSError:
        return EXIT_IOERR
    _unlink_quiet(hold_path)
    return "retry"


def _commit(path, section, key, value, before, believed, our_text, mode, state):
    """Install `our_text` if the active file is still `believed`.

    Returns 0, an exit code, or "retry". A conflicting swap publishes the
    displaced snapshot back to the active name before the edit is staged
    again, so a failed merge leaves the engine's save where Windscribe
    reloads it, not only in a side file.
    """
    staged = None
    try:
        staged = stage_write(path, our_text, mode)
        for _rebase in range(ATTEMPTS):
            exchange = state["exchange"]
            if exchange is None:
                try:
                    if identity(os.stat(path)) != identity(before):
                        return "retry"
                    os.replace(staged, path)
                    staged = None
                except FileNotFoundError:
                    return EXIT_MISSING
                except OSError:
                    return EXIT_IOERR
                landed = _confirm(path, section, key, our_text, state)
                if landed == "retry":
                    return "retry"
                return landed
            ours_hash = text_sha(our_text)
            try:
                _add_record(path, staged, ours_hash)
                exchange(staged, path)
            except OSError as error:
                if error.errno in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                    state["exchange"] = None
                    try:
                        _drop_record(path, staged)
                    except OSError:
                        return EXIT_IOERR
                    continue
                return EXIT_IOERR
            # The name `staged` now holds whatever the swap displaced.
            # It is journaled, so a failure below must not unlink it.
            try:
                displaced = read_lines(staged)
            except OSError:
                return _restore_unread(path, staged, our_text, state["exchange"])
            if displaced == believed:
                try:
                    _drop_record(path, staged)
                except OSError:
                    return EXIT_IOERR
                _unlink_quiet(staged)
                staged = None
                return _confirm(path, section, key, our_text, state)
            try:
                displaced_text = read_text(staged)
                _set_displaced(path, staged, text_sha(displaced_text))
            except OSError:
                return EXIT_IOERR
            status = publish_snapshot(path, staged, our_text, state["exchange"])
            if status == "moved":
                # A newer engine save landed on the active name before we
                # could put the displaced snapshot back. That snapshot is
                # older than the save now active. Drop it and edit the
                # active file; do not leave it as the only copy of a
                # newer setting, and do not edit our stale install.
                try:
                    _drop_record(path, staged)
                except OSError:
                    return EXIT_IOERR
                _unlink_quiet(staged)
                staged = None
                try:
                    now_lines = read_lines(path)
                except OSError:
                    return EXIT_IOERR
                installed = edit_line(now_lines, section, key, value)
                if installed is None:
                    return EXIT_MISSING
                believed = now_lines
                our_text = "\n".join(installed)
                try:
                    before = os.stat(path)
                    staged = stage_write(path, our_text, mode)
                except OSError:
                    return EXIT_IOERR
                continue
            if status != "published":
                return EXIT_IOERR
            try:
                now_text = read_text(path)
            except OSError:
                return EXIT_IOERR
            if now_text == our_text:
                # The engine's save did not make it back to the active
                # name. Keep the hold; do not claim and do not retry on
                # top of our stale install.
                return EXIT_IOERR
            try:
                _drop_record(path, staged)
            except OSError:
                return EXIT_IOERR
            _unlink_quiet(staged)
            staged = None
            try:
                now_lines = read_lines(path)
            except OSError:
                return EXIT_IOERR
            installed = edit_line(now_lines, section, key, value)
            if installed is None:
                return EXIT_MISSING
            believed = now_lines
            our_text = "\n".join(installed)
            try:
                before = os.stat(path)
            except OSError:
                return EXIT_IOERR
            try:
                staged = stage_write(path, our_text, mode)
            except OSError:
                return EXIT_IOERR
        # Rebase budget spent. The active file must not stay an older
        # install while a newer save sits in a side file; reconcile
        # publishes it or refuses. The caller reports the race from
        # whatever the active file then holds.
        return "retry"
    finally:
        if staged is not None and not _journaled(path, staged):
            _unlink_quiet(staged)


def _confirm(path, section, key, our_text, state):
    """Success only when the active file matches and no hold remains."""
    if reconcile(path, state["exchange"]) == "io":
        return EXIT_IOERR
    try:
        landed_lines = read_lines(path)
    except FileNotFoundError:
        return EXIT_MISSING
    except OSError:
        return EXIT_IOERR
    if "\n".join(landed_lines) != our_text:
        return "retry"
    try:
        if load_journal(path):
            return "retry"
    except OSError:
        return EXIT_IOERR
    print(value_of(landed_lines, section, key))
    return 0


def _edit(path, section, key, value, state):
    if reconcile(path, state["exchange"]) == "io":
        return EXIT_IOERR
    for _attempt in range(ATTEMPTS):
        try:
            before = os.stat(path)
            lines = read_lines(path)
            after_read = os.stat(path)
        except FileNotFoundError:
            return EXIT_MISSING
        except OSError:
            return EXIT_IOERR
        if identity(after_read) != identity(before):
            continue
        installed = edit_line(lines, section, key, value)
        if installed is None:
            return EXIT_MISSING
        our_text = "\n".join(installed)
        outcome = _commit(
            path, section, key, value, before, lines, our_text,
            before.st_mode & 0o7777, state)
        if outcome == "retry":
            if reconcile(path, state["exchange"]) == "io":
                return EXIT_IOERR
            continue
        return outcome
    if reconcile(path, state["exchange"]) == "io":
        return EXIT_IOERR
    # Another writer kept winning the file. Its content is what stands
    # (reconcile has published any newer save we displaced back onto the
    # active name), so report the race and the value actually there.
    try:
        lines = read_lines(path)
    except OSError:
        return EXIT_IOERR
    landed = value_of(lines, section, key)
    if landed is not None:
        print(landed)
    return EXIT_RACED


def main(argv):
    if len(argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        return EXIT_REFUSED
    path, section, key, value = argv[1], argv[2], argv[3], argv[4]
    if "\n" in value or "\r" in value or "\x00" in value:
        return EXIT_REFUSED
    try:
        lock_fd = _lock_editor(path)
    except OSError:
        return EXIT_IOERR
    try:
        # Captured after the caller may have patched exchange_paths
        # (the race tests do). Demotion sticks for the rest of this run.
        state = {"exchange": exchange_paths}
        return _edit(path, section, key, value, state)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
