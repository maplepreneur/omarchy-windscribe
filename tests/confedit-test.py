#!/usr/bin/env python3
"""Race-focused tests for the bundled confedit.py settings helper.

The node suite (tests/model-test.mjs) covers the helper end to end; this
file proves the coordination property the helper exists for: a write
from the Windscribe engine overlapping the helper's own
read-modify-write window — during the read, while the write is staged,
in the instant after the rename, or continuously — is reconciled on top
of the freshest content, never silently reverted. A later retry must not
report success with an older preferences file while a newer save remains
in a side file, and a displaced copy must not be deleted if the read of
it fails.

Each scenario patches a module-level function to stand in for the engine
saving the file at the worst possible moment, then restores it. Run:

    python3 tests/confedit-test.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import confedit

pass_count = 0
fail_count = 0


def ok(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print("ok   " + name)
    else:
        fail_count += 1
        print("FAIL " + name + ((" -- " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got " + repr(got) + ", want " + repr(want))


def make_conf(tmp):
    """A fresh conf dir holding the engine's file; returns its path."""
    path = os.path.join(tmp, "windscribe_cli.conf")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\n"
            "FirewallMode=Auto\nSplitTunnelingEnabled=false\n\n[Advanced]\nDNSManager=Auto\n"
        )
    os.chmod(path, 0o600)
    return path


def run(path, key, value, section="Connection"):
    with contextlib.redirect_stdout(io.StringIO()):
        return confedit.main(["confedit.py", path, section, key, value])


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def engine_save(path, text):
    """What the engine's own save looks like: a whole-file rename."""
    fd, tmp = tempfile.mkstemp(prefix=".engine-", dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def patch_once(module, name, side_effect):
    """Wrap `name` so `side_effect` runs inside the first call only."""
    original = getattr(module, name)
    state = {"done": False}

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state["done"]:
            state["done"] = True
            side_effect()
        return result

    setattr(module, name, wrapper)
    return original


def restore(module, name, original):
    setattr(module, name, original)


def patch_exchange(when, side_effect):
    """Wrap confedit.exchange_paths so side_effect runs before or after
    the first exchange call only. Returns the original; restore with
    restore(confedit, "exchange_paths", original)."""
    original = confedit.exchange_paths
    state = {"done": False}

    def wrapper(old, new):
        if when == "pre" and not state["done"]:
            state["done"] = True
            side_effect()
        result = original(old, new)
        if when == "post" and not state["done"]:
            state["done"] = True
            side_effect()
        return result

    confedit.exchange_paths = wrapper
    return original

print("confedit.py: sanity, one setting, one write")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    original = read(path)
    result = run(path, "FirewallMode", "Always On")
    eq("exit 0 on confirmed write", result, 0)
    after = read(path)
    eq("one line changed, byte-exact", after,
       original.replace("FirewallMode=Auto", "FirewallMode=Always On"))
    ok("no extra trailing newline", after.endswith("\n") and not after.endswith("\n\n"))
    eq("file mode kept", os.stat(path).st_mode & 0o777, 0o600)

    absent = os.path.join(tmp, "absent.conf")
    eq("missing file refused", confedit.main(
        ["confedit.py", absent, "Connection", "FirewallMode", "Auto"]), 3)
    ok("missing file never created", not os.path.exists(absent))

    eq("multi-line value refused", run(path, "FirewallMode", "a\nb"), 4)
    eq("refused value leaves the file alone", read(path), after)
finally:
    shutil.rmtree(tmp)

print("confedit.py: an engine save during the helper's read is kept, not reverted")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
    # The engine saves in the middle of the helper's first read: the copy
    # in hand is already stale, and a whole-file writer would put it back
    # on disk, silently reverting Autoconnect=true.
    saved = patch_once(confedit, "read_lines", lambda: engine_save(path, engine_text))
    try:
        result = run(path, "FirewallMode", "Always On")
    finally:
        restore(confedit, "read_lines", saved)
    eq("retry lands the edit", result, 0)
    final = read(path)
    ok("engine's newer setting survived", "Autoconnect=true" in final)
    ok("own key written", "FirewallMode=Always On" in final)
    ok("everything else byte-exact",
       final == engine_text.replace("FirewallMode=Auto", "FirewallMode=Always On"))
finally:
    shutil.rmtree(tmp)

print("confedit.py: an engine save completing just before the commit is merged, not lost")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        # The engine's rename completes in the instant before the
        # helper's commit -- the interleaving no identity check can see.
        # The atomic exchange must displace the engine's newer file into
        # the helper's hands and re-apply the edit on top of it.
        saved = patch_exchange("pre", lambda: engine_save(path, engine_text))
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", saved)
        eq("edit re-applied on top of the engine's file", result, 0)
        final = read(path)
        ok("engine's newer setting survived", "Autoconnect=true" in final)
        ok("own key written", "FirewallMode=Always On" in final)
        ok("everything else byte-exact",
           final == engine_text.replace("FirewallMode=Auto", "FirewallMode=Always On"))
finally:
    shutil.rmtree(tmp)

print("confedit.py: an engine save right after the commit is reconciled, not lost")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        engine_text = read(path).replace("DNSManager=Auto", "DNSManager=custom")
        # The engine saves in the instant after the commit: the whole-file
        # read-back must see it and re-apply on top, not claim our edit.
        saved = patch_exchange("post", lambda: engine_save(path, engine_text))
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", saved)
        eq("edit re-applied on top of the engine's file", result, 0)
        final = read(path)
        ok("engine's newer setting survived", "DNSManager=custom" in final)
        ok("own key written", "FirewallMode=Always On" in final)
        ok("everything else byte-exact",
           final == engine_text.replace("FirewallMode=Auto", "FirewallMode=Always On"))
finally:
    shutil.rmtree(tmp)

print("confedit.py: a failed merge keeps the newer save on disk, not destroyed")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        # The engine saves during the first staging, forcing a rebase;
        # the staging of the merged file then fails (a full disk, say).
        # The newer save must survive somewhere on disk -- the previous
        # commit unlinked it at this exact point and lost it for good.
        fires = {"n": 0}
        real_fsync = os.fsync
        def fsync_hook(fd):
            r = real_fsync(fd)
            if fires["n"] == 0:
                fires["n"] += 1
                engine_save(path, engine_text)
            return r
        real_stage = confedit.stage_write
        def failing_stage(path_arg, text_arg, mode_arg):
            if fires["n"] == 1:
                raise OSError(28, "No space left on device")
            return real_stage(path_arg, text_arg, mode_arg)
        os.fsync = fsync_hook
        confedit.stage_write = failing_stage
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            os.fsync = real_fsync
            confedit.stage_write = real_stage
        eq("merge failure reported as I/O error", result, 6)
        survivors = [read(os.path.join(tmp, name))
                     for name in os.listdir(tmp)]
        ok("newer save still on disk after the failure",
           any("Autoconnect=true" in text for text in survivors))
        ok("newer save is the active file, not only a side file",
           "Autoconnect=true" in read(path), read(path))
finally:
    shutil.rmtree(tmp)

print("confedit.py: rebase exhaustion gives up without destroying the newest save")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        original = read(path)
        # A pathological engine that saves again before every commit,
        # forever: the helper must not claim success, and must not delete
        # the newest save. A later retry, once the engine stops, must not
        # report success with an older preferences file while that newest
        # save remains in a side file Windscribe does not reload.
        fires = {"n": 0}
        real = confedit.exchange_paths
        def storm(old, new):
            engine_save(path, original.replace(
                "DNSManager=Auto", "DNSManager=Storm%d" % fires["n"]))
            fires["n"] += 1
            return real(old, new)
        confedit.exchange_paths = storm
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", real)
        ok("unbounded race not claimed as success", result != 0, "exit " + str(result))
        newest = "DNSManager=Storm%d" % (fires["n"] - 1)
        survivors = [read(os.path.join(tmp, name))
                     for name in os.listdir(tmp)
                     if not name.startswith(".confedit-journal")]
        ok("newest save still on disk after giving up",
           any(newest in text for text in survivors))
        if result == 0:
            ok("success would have to be the newest active file",
               newest in read(path))
        else:
            retry = run(path, "FirewallMode", "Always On")
            eq("later retry confirms the edit", retry, 0)
            final = read(path)
            ok("later retry did not keep an older active file",
               newest in final, "active is " + final)
            ok("own key written on top of the newest save",
               "FirewallMode=Always On" in final)
            stale_side = []
            for name in os.listdir(tmp):
                if name == "windscribe_cli.conf":
                    continue
                text = read(os.path.join(tmp, name))
                if "DNSManager=Storm" in text and newest not in text:
                    # An older side file is harmless. A newer one is not.
                    pass
                if newest not in text:
                    continue
                if name != "windscribe_cli.conf" and newest in text and newest not in final:
                    stale_side.append(name)
            ok("newest save is not stranded in a side file",
               newest in final and not stale_side)
finally:
    shutil.rmtree(tmp)

class Crash(BaseException):
    """Stands in for SIGKILL / power loss: unwinds past every except
    OSError, so only what is already on disk survives."""


def ident_of(path):
    return confedit.identity(os.stat(path))


def edited(text):
    return text.replace("FirewallMode=Auto", "FirewallMode=Always On")


def journal_open(path):
    return os.path.exists(os.path.join(os.path.dirname(path), ".confedit-journal"))


def litter(tmp):
    return sorted(name for name in os.listdir(tmp) if name != "windscribe_cli.conf")


def quiet_retry(path, key="FirewallMode", value="Always On"):
    """A later edit with no engine activity; returns its exit code."""
    return run(path, key, value)


def hooks(fsync=None, pre=None, post=None, read=None):
    """Install engine/crash hooks at the helper's I/O points. Returns an
    undo function. Each hook gets a running call index."""
    real_fsync = os.fsync
    real_exchange = confedit.exchange_paths
    real_read = confedit.read_lines
    counts = {"fsync": 0, "exchange": 0, "read": 0}

    def fsync_hook(fd):
        r = real_fsync(fd)
        counts["fsync"] += 1
        if fsync:
            fsync(counts["fsync"])
        return r

    def exchange_hook(old, new):
        counts["exchange"] += 1
        n = counts["exchange"]
        if pre:
            pre(n)
        r = real_exchange(old, new)
        if post:
            post(n)
        return r

    def read_hook(target):
        lines = real_read(target)
        counts["read"] += 1
        if read:
            read(counts["read"])
        return lines

    os.fsync = fsync_hook
    confedit.exchange_paths = exchange_hook
    confedit.read_lines = read_hook

    def undo():
        os.fsync = real_fsync
        confedit.exchange_paths = real_exchange
        confedit.read_lines = real_read

    return undo


class Engine:
    """The engine's saves, numbered: save n writes DNSManager=E<n>."""

    def __init__(self, path, base):
        self.path = path
        self.base = base
        self.n = 0

    def save(self, *_ignored):
        self.n += 1
        engine_save(self.path, self.newest())

    def newest(self):
        if self.n == 0:
            return self.base
        return self.base.replace("DNSManager=Auto", "DNSManager=E%d" % self.n)


def exhausted_state(tmp):
    """Drive a real run into publish exhaustion: the engine saves before
    every swap, forever. Returns (path, engine) with a journal and a hold
    outstanding and the newest save held aside."""
    path = make_conf(tmp)
    engine = Engine(path, read(path))
    undo = hooks(pre=engine.save)
    try:
        result = run(path, "FirewallMode", "Always On")
    finally:
        undo()
    return path, engine, result


if confedit.exchange_paths is None:
    print("skip -- the remaining scenarios need renameat2")
else:
    print("confedit.py: a later retry adopts a journaled newer save before succeeding")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        # The state publish exhaustion leaves: the active file is an
        # older file the helper put there, and the hold has a newer save
        # that came back from the last swap.
        older = original.replace("FirewallMode=Auto", "FirewallMode=Always On")
        newest = original.replace("Autoconnect=false", "Autoconnect=true").replace(
            "DNSManager=Auto", "DNSManager=Newest")
        engine_save(path, older)
        hold = os.path.join(tmp, ".confedit-held")
        with open(hold, "w", encoding="utf-8") as handle:
            handle.write(newest)
        unrelated = os.path.join(tmp, ".confedit-prev")
        with open(unrelated, "w") as handle:
            handle.write("x")
        confedit.save_journal(path, {"hold": hold, "ours": False,
                                     "put": ident_of(path), "prev": ident_of(unrelated)})
        os.unlink(unrelated)
        result = run(path, "SplitTunnelingEnabled", "true")
        eq("later retry confirms on top of the journaled save", result, 0)
        final = read(path)
        ok("newest engine setting is active", "DNSManager=Newest" in final, final)
        ok("newest autoconnect is active", "Autoconnect=true" in final)
        ok("retried key written", "SplitTunnelingEnabled=true" in final)
        eq("no side file or journal left", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a journaled hold is dropped only when a later save proves it older")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        older = original.replace("DNSManager=Auto", "DNSManager=Held")
        hold = os.path.join(tmp, ".confedit-held")
        with open(hold, "w", encoding="utf-8") as handle:
            handle.write(older)
        unrelated = os.path.join(tmp, ".confedit-prev")
        with open(unrelated, "w") as handle:
            handle.write("x")
        confedit.save_journal(path, {"hold": hold, "ours": False,
                                     "put": ident_of(path), "prev": ident_of(unrelated)})
        os.unlink(unrelated)
        # The engine saves after the hold left the active name: that save
        # is the newer one, whatever its bytes look like.
        later = original.replace("Autoconnect=false", "Autoconnect=true")
        engine_save(path, later)
        eq("edit lands", run(path, "FirewallMode", "Always On"), 0)
        eq("the later save is kept, the older hold is not published",
           read(path), edited(later))
        eq("obsolete hold removed", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a failed look at the displaced file keeps it and publishes it next run")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        real_ident = confedit.Run.ident
        state = {"armed": False, "failed": False}

        def ident_fail_hold(self, target):
            if state["armed"] and not state["failed"] and target != self.conf:
                state["failed"] = True
                raise OSError(5, "Input/output error")
            return real_ident(self, target)

        def arm(_n):
            if not state["armed"]:
                state["armed"] = True

        undo = hooks(pre=lambda n: engine_save(path, engine_text) if n == 1 else None,
                     post=arm)
        confedit.Run.ident = ident_fail_hold
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            confedit.Run.ident = real_ident
            undo()
        eq("unclassifiable hold is an I/O error, not success", result, 6)
        held = [name for name in os.listdir(tmp)
                if "Autoconnect=true" in read(os.path.join(tmp, name))]
        ok("displaced save kept on disk", bool(held), str(os.listdir(tmp)))
        ok("journal kept", journal_open(path))
        eq("later retry publishes the preserved save", quiet_retry(path), 0)
        eq("preserved save active, edit on top", read(path), edited(engine_text))
        eq("no litter", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a failed restore swap keeps the newer save and publishes it next run")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        real_exchange = confedit.exchange_paths
        calls = {"n": 0}

        def exchange_then_fail(old, new):
            calls["n"] += 1
            if calls["n"] == 1:
                engine_save(path, engine_text)
                return real_exchange(old, new)
            raise OSError(5, "Input/output error")

        confedit.exchange_paths = exchange_then_fail
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            confedit.exchange_paths = real_exchange
        eq("failed restore is an I/O error", result, 6)
        ok("newer save still on disk",
           any("Autoconnect=true" in read(os.path.join(tmp, n)) for n in os.listdir(tmp)))
        eq("later retry", quiet_retry(path), 0)
        eq("newer save active with the edit", read(path), edited(engine_text))
        eq("no litter", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

print("confedit.py: three overlapping saves merge in order and leave no litter")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        saves = [read(path).replace("Autoconnect=false", "Autoconnect=true"),
                 read(path).replace("DNSManager=Auto", "DNSManager=one"),
                 read(path).replace("DNSManager=Auto", "DNSManager=two")]
        fires = {"n": 0}
        real_fsync = os.fsync
        real_exchange = confedit.exchange_paths
        def fsync_hook(fd):
            r = real_fsync(fd)
            if fires["n"] == 0:
                fires["n"] += 1
                engine_save(path, saves[0])
            return r
        def exchange_hook(old, new):
            if fires["n"] in (1, 2):
                engine_save(path, saves[fires["n"]])
                fires["n"] += 1
            return real_exchange(old, new)
        os.fsync = fsync_hook
        confedit.exchange_paths = exchange_hook
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            os.fsync = real_fsync
            restore(confedit, "exchange_paths", real_exchange)
        eq("three saves reconciled", result, 0)
        final = read(path)
        ok("newest engine save merged", "DNSManager=two" in final)
        ok("own key written", "FirewallMode=Always On" in final)
        want = saves[2].replace("FirewallMode=Auto", "FirewallMode=Always On")
        ok("byte-exact edit of the newest save", final == want)
        ok("no displaced litter after a clean commit", os.listdir(tmp) == ["windscribe_cli.conf"])
finally:
    shutil.rmtree(tmp)

print("confedit.py: two helper instances editing different keys land both")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    import subprocess
    landed = 0
    for _ in range(15):
        open(path, "w").write(
            "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\n"
            "FirewallMode=Auto\nSplitTunnelingEnabled=false\n\n[Advanced]\nDNSManager=Auto\n")
        a = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(confedit.__file__), "confedit.py"),
                              path, "Connection", "FirewallMode", "Always On"], stdout=subprocess.DEVNULL)
        b = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(confedit.__file__), "confedit.py"),
                              path, "Connection", "SplitTunnelingEnabled", "true"], stdout=subprocess.DEVNULL)
        a.wait()
        b.wait()
        final = read(path)
        if "FirewallMode=Always On" in final and "SplitTunnelingEnabled=true" in final:
            landed += 1
    ok("both edits landed in all 15 concurrent pairs", landed == 15,
       str(landed) + "/15")
finally:
    shutil.rmtree(tmp)

print("confedit.py: a no-op edit is a confirmed no-op")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    result = run(path, "FirewallMode", "Auto")  # already Auto
    eq("no-op exit 0", result, 0)
    ok("no-op leaves the file alone", read(path) ==
       "[General]\nLanguage=en\n\n[Connection]\nAutoconnect=false\n"
       "FirewallMode=Auto\nSplitTunnelingEnabled=false\n\n[Advanced]\nDNSManager=Auto\n")
finally:
    shutil.rmtree(tmp)

print("confedit.py: an unavailable exchange demotes to the fallback, still commits")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        original = confedit.exchange_paths
        calls = {"n": 0}

        def unsupported(old, new):
            calls["n"] += 1
            raise OSError(22, "Invalid argument", old)  # EINVAL, as an old fs gives

        confedit.exchange_paths = unsupported
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", original)
        eq("fallback commit succeeds", result, 0)
        eq("fallback used after demotion", calls["n"], 1)
        ok("own key written", "FirewallMode=Always On" in read(path))
finally:
    shutil.rmtree(tmp)

print("confedit.py: an engine save while the write is staged is caught before the rename")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
    # The engine saves while the helper's write is being staged (the
    # fsync window). The last identity check must come AFTER the staging,
    # back to back with the rename, or the rename overwrites the engine's
    # file and the read-back — which verifies what replaced the file, not
    # what the file was — still finds the helper's text and reports
    # success while the engine's setting is lost.
    saved = patch_once(os, "fsync", lambda: engine_save(path, engine_text))
    try:
        result = run(path, "FirewallMode", "Always On")
    finally:
        restore(os, "fsync", saved)
    eq("edit lands on top of the engine's file", result, 0)
    final = read(path)
    ok("engine's newer setting survived", "Autoconnect=true" in final)
    ok("own key written", "FirewallMode=Always On" in final)
    ok("everything else byte-exact",
       final == engine_text.replace("FirewallMode=Auto", "FirewallMode=Always On"))
finally:
    shutil.rmtree(tmp)

print("confedit.py: a writer that never settles is reported, never clobbered")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    engine_text = read(path).replace("FirewallMode=Auto", "FirewallMode=EngineChoice")

    original_read_lines = confedit.read_lines

    def engine_wins_every_read(path_arg):
        lines = original_read_lines(path_arg)
        engine_save(path_arg, engine_text)
        return lines

    confedit.read_lines = engine_wins_every_read
    try:
        result = run(path, "FirewallMode", "Always On")
    finally:
        confedit.read_lines = original_read_lines
    eq("race reported, not claimed", result, 5)
    eq("engine's file stands untouched", read(path), engine_text)
finally:
    shutil.rmtree(tmp)

if confedit.exchange_paths is not None:
    # ---- The reviewer's last finding, at 5153110: a Windscribe save that
    # overlaps the recovery of an earlier give-up must never be discarded
    # as obsolete, and several overlapping saves must never leave an older
    # version active, whether the earlier run reported 5, 6, or crashed.

    def leftover_state(tmp, kind):
        """An earlier run that did not finish. Returns (path, engine)."""
        path = make_conf(tmp)
        engine = Engine(path, read(path))
        if kind == "exhausted":
            undo = hooks(pre=engine.save)
        elif kind == "io-error":
            real = confedit.exchange_paths
            calls = {"n": 0}

            def failing(old, new):
                calls["n"] += 1
                if calls["n"] == 1:
                    engine.save()
                    return real(old, new)
                raise OSError(5, "Input/output error")

            confedit.exchange_paths = failing
            undo = lambda: setattr(confedit, "exchange_paths", real)
        else:  # crash-<when>-<n>: crash around the n-th swap
            _crash, when, nth = kind.split("-")
            nth = int(nth)

            def boom(n):
                if n == nth:
                    raise Crash()

            undo = hooks(pre=lambda n: (engine.save() if n == 1 else None,
                                        boom(n) if when == "pre" else None),
                         post=lambda n: boom(n) if when == "post" else None)
        try:
            first = run(path, "FirewallMode", "Always On")
        except Crash:
            first = "crash"
        finally:
            undo()
        return path, engine, first

    overlaps = {
        "none": lambda engine: hooks(),
        "save before a recovery swap": lambda engine: hooks(
            pre=lambda n: engine.save() if n == 1 else None),
        "save after a recovery swap": lambda engine: hooks(
            post=lambda n: engine.save() if n == 1 else None),
        "save during the recovery journal write": lambda engine: hooks(
            fsync=lambda n: engine.save() if n == 1 else None),
        "three saves across recovery": lambda engine: hooks(
            pre=lambda n: engine.save() if n <= 3 else None),
        "six saves across recovery": lambda engine: hooks(
            pre=lambda n: engine.save() if n <= 6 else None,
            post=lambda n: engine.save() if n in (2, 4) else None),
    }
    kinds = ["exhausted", "io-error", "crash-pre-1", "crash-post-1",
             "crash-pre-2", "crash-post-2", "crash-pre-3", "crash-post-3"]

    print("confedit.py: overlapping saves during recovery never lose the newest preferences")
    matrix_ok = 0
    matrix_total = 0
    for kind in kinds:
        for label, install in overlaps.items():
            tmp = tempfile.mkdtemp(prefix="ws-confedit-")
            try:
                path, engine, first = leftover_state(tmp, kind)
                matrix_total += 1
                problems = []
                if first == 0 and read(path) != edited(engine.newest()):
                    problems.append("first run claimed success over an older file")
                undo = install(engine)
                try:
                    second = run(path, "FirewallMode", "Always On")
                finally:
                    undo()
                if second == 0 and read(path) != edited(engine.newest()):
                    problems.append("overlapped retry reported success with older settings")
                if second != 0 and not journal_open(path) \
                        and read(path) not in (engine.newest(), edited(engine.newest())):
                    problems.append("older version left active with nothing journaled")
                newest_marker = engine.newest()
                if not any(read(os.path.join(tmp, n)) in (newest_marker, edited(newest_marker))
                           for n in os.listdir(tmp) if not n.startswith(".confedit-journal")):
                    problems.append("newest save lost")
                third = quiet_retry(path)
                if third != 0:
                    problems.append("quiet retry exit %r" % third)
                if read(path) != edited(engine.newest()):
                    problems.append("final file is not the newest save plus the edit")
                if litter(tmp):
                    problems.append("litter %r" % litter(tmp))
                if problems:
                    print("     %s / %s: %s" % (kind, label, "; ".join(problems)))
                else:
                    matrix_ok += 1
            finally:
                shutil.rmtree(tmp)
    ok("every give-up x overlap combination keeps the newest save (%d/%d)"
       % (matrix_ok, matrix_total), matrix_ok == matrix_total)

    print("confedit.py: a recovery overlapped forever is reported, never claimed")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path, engine, first = exhausted_state(tmp)
        eq("first run reports the race", first, 5)
        ok("journal outstanding after the give-up", journal_open(path))
        undo = hooks(pre=engine.save)
        try:
            second = run(path, "FirewallMode", "Always On")
        finally:
            undo()
        eq("overlapped-forever recovery reports the race", second, 5)
        ok("the race leaves the journal, so the newest save is not orphaned",
           journal_open(path))
        eq("quiet retry", quiet_retry(path), 0)
        eq("newest save plus edit", read(path), edited(engine.newest()))
        eq("no litter", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a crash at any durable step loses nothing")
    crash_ok = 0
    crash_total = 0
    for engine_at in range(0, 6):
        for crash_at in range(1, 16):
            tmp = tempfile.mkdtemp(prefix="ws-confedit-")
            try:
                path = make_conf(tmp)
                engine = Engine(path, read(path))

                def on_fsync(n, engine=engine, engine_at=engine_at, crash_at=crash_at):
                    if n == engine_at:
                        engine.save()
                    if n == crash_at:
                        raise Crash()

                undo = hooks(fsync=on_fsync,
                             pre=lambda n, engine=engine, engine_at=engine_at:
                                 engine.save() if engine_at and n == 1 else None)
                try:
                    try:
                        first = run(path, "FirewallMode", "Always On")
                    except Crash:
                        first = "crash"
                finally:
                    undo()
                crash_total += 1
                problems = []
                if first == 0 and read(path) != edited(engine.newest()):
                    problems.append("success over an older file")
                retry = quiet_retry(path)
                if retry != 0:
                    problems.append("retry exit %r" % retry)
                if read(path) != edited(engine.newest()):
                    problems.append("final is not newest + edit")
                if litter(tmp):
                    problems.append("litter %r" % litter(tmp))
                if problems:
                    print("     engine@%d crash@%d: %s" % (engine_at, crash_at, "; ".join(problems)))
                else:
                    crash_ok += 1
            finally:
                shutil.rmtree(tmp)
    ok("crash sweep (%d/%d)" % (crash_ok, crash_total), crash_ok == crash_total)

    print("confedit.py: randomized interleavings of saves, faults and crashes")
    import random
    fuzz_ok = 0
    fuzz_runs = int(os.environ.get("CONFEDIT_FUZZ_RUNS", "400"))
    for seed in range(fuzz_runs):
        rng = random.Random(seed)
        tmp = tempfile.mkdtemp(prefix="ws-confedit-")
        try:
            path = make_conf(tmp)
            engine = Engine(path, read(path))
            p_save = rng.choice([0.1, 0.3, 0.6, 0.9])
            p_crash = rng.choice([0.0, 0.0, 0.03, 0.08])
            problems = []

            def maybe(_n):
                if rng.random() < p_save:
                    engine.save()
                if rng.random() < p_crash:
                    raise Crash()

            for round_ in range(rng.randint(1, 4)):
                undo = hooks(fsync=maybe, pre=maybe, post=maybe, read=maybe)
                try:
                    try:
                        result = run(path, "FirewallMode", "Always On")
                    except Crash:
                        result = "crash"
                finally:
                    undo()
                newest = engine.newest()
                if result == 0 and read(path) != edited(newest):
                    problems.append("round %d: success over an older file" % round_)
                if result == 0 and journal_open(path):
                    problems.append("round %d: success with a journal outstanding" % round_)
                if not any(read(os.path.join(tmp, n)) in (newest, edited(newest))
                           for n in os.listdir(tmp) if not n.startswith(".confedit-journal")):
                    problems.append("round %d: newest save lost" % round_)
                if not journal_open(path) and \
                        read(path) not in (newest, edited(newest)):
                    problems.append("round %d: older file active, nothing journaled" % round_)
            final = quiet_retry(path)
            if final != 0 or read(path) != edited(engine.newest()) or litter(tmp):
                problems.append("final exit %r litter %r" % (final, litter(tmp)))
            if problems:
                print("     seed %d: %s" % (seed, "; ".join(problems)))
            else:
                fuzz_ok += 1
        finally:
            shutil.rmtree(tmp)
    ok("randomized interleavings (%d/%d)" % (fuzz_ok, fuzz_runs), fuzz_ok == fuzz_runs)

    print("confedit.py: live stress against a real concurrent renaming writer")
    import threading
    import time
    live_ok = 0
    live_runs = 40
    for i in range(live_runs):
        tmp = tempfile.mkdtemp(prefix="ws-confedit-")
        try:
            path = make_conf(tmp)
            engine = Engine(path, read(path))
            stop = threading.Event()

            def writer():
                while not stop.is_set():
                    engine.save()
                    time.sleep(0.0002 * (i % 5))

            thread = threading.Thread(target=writer)
            thread.start()
            try:
                result = run(path, "FirewallMode", "Always On")
            finally:
                stop.set()
                thread.join()
            newest = engine.newest()
            good = True
            if not journal_open(path):
                # Nothing held aside: the engine's last save, or our edit
                # of it, must be what is active.
                good = read(path) in (newest, edited(newest))
            retry = quiet_retry(path)
            good = good and retry == 0 and read(path) == edited(newest) and not litter(tmp)
            if good:
                live_ok += 1
            else:
                print("     live run %d: exit %r, retry %r" % (i, result, retry))
        finally:
            shutil.rmtree(tmp)
    ok("live stress (%d/%d)" % (live_ok, live_runs), live_ok == live_runs)

    print("confedit.py: a leftover v1.0.6 journal is finished, not ignored")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        import hashlib
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
        older = edited(original)
        newest = original.replace("DNSManager=Auto", "DNSManager=Legacy")
        engine_save(path, older)
        hold = os.path.join(tmp, ".confedit-legacyhold")
        with open(hold, "w") as handle:
            handle.write(newest)
        with open(os.path.join(tmp, ".confedit-journal"), "w") as handle:
            handle.write("\t".join((sha(older), sha(newest), "swap", "-", hold)) + "\n")
        eq("edit after a legacy journal", run(path, "SplitTunnelingEnabled", "true"), 0)
        final = read(path)
        ok("legacy held save published", "DNSManager=Legacy" in final, final)
        eq("legacy journal and hold cleared", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: an ambiguous v1.0.6 journal refuses instead of claiming success on older prefs")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        import hashlib
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
        ours = edited(original)
        older_active = original.replace("DNSManager=Auto", "DNSManager=OlderActive")
        newer_held = original.replace("DNSManager=Auto", "DNSManager=NewerHeld")
        engine_save(path, older_active)
        hold = os.path.join(tmp, ".confedit-legacyambig")
        with open(hold, "w") as handle:
            handle.write(newer_held)
        with open(os.path.join(tmp, ".confedit-journal"), "w") as handle:
            handle.write("\t".join((sha(ours), "-", "swap", "-", hold)) + "\n")
        eq("ambiguous legacy state refused as I/O error",
           run(path, "SplitTunnelingEnabled", "true"), 6)
        ok("legacy journal preserved", journal_open(path))
        ok("newer held file preserved", os.path.exists(hold) and read(hold) == newer_held)
        eq("older active file not edited", read(path), older_active)
        eq("subsequent retry still refuses rather than succeeding on older prefs",
           run(path, "SplitTunnelingEnabled", "true"), 6)
        ok("journal still preserved on retry", journal_open(path))
        ok("held file still preserved on retry", os.path.exists(hold))
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a v1.0.6 publish journal with a captured newer save publishes it")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        import hashlib
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
        ours = edited(original)
        mid = original.replace("DNSManager=Auto", "DNSManager=Mid")
        newest = original.replace("DNSManager=Auto", "DNSManager=Captured")
        engine_save(path, mid)
        hold = os.path.join(tmp, ".confedit-legacypub")
        with open(hold, "w") as handle:
            handle.write(newest)
        with open(os.path.join(tmp, ".confedit-journal"), "w") as handle:
            handle.write("\t".join((sha(ours), sha(mid), "publish", sha(ours), hold)) + "\n")
        eq("captured newer save published and edited",
           run(path, "SplitTunnelingEnabled", "true"), 0)
        final = read(path)
        ok("captured newer save is active", "DNSManager=Captured" in final, final)
        ok("requested edit applied on top", "SplitTunnelingEnabled=true" in final)
        eq("no litter left", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a v1.0.6 completed publish journal drops the obsolete expect hold")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        import hashlib
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
        ours = edited(original)
        prev = original.replace("DNSManager=Auto", "DNSManager=Prev")
        newest = original.replace("DNSManager=Auto", "DNSManager=Published")
        engine_save(path, newest)
        hold = os.path.join(tmp, ".confedit-legacydone")
        with open(hold, "w") as handle:
            handle.write(prev)
        with open(os.path.join(tmp, ".confedit-journal"), "w") as handle:
            handle.write("\t".join((sha(ours), sha(newest), "publish", sha(prev), hold)) + "\n")
        eq("completed legacy publish settles cleanly",
           run(path, "SplitTunnelingEnabled", "true"), 0)
        final = read(path)
        ok("published save kept active", "DNSManager=Published" in final, final)
        ok("requested edit applied", "SplitTunnelingEnabled=true" in final)
        eq("no litter left", litter(tmp), [])
    finally:
        shutil.rmtree(tmp)

    print("confedit.py: a v1.0.6 journal with multiple unresolved holds refuses")
    tmp = tempfile.mkdtemp(prefix="ws-confedit-")
    try:
        path = make_conf(tmp)
        original = read(path)
        import hashlib
        sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
        ours = edited(original)
        s1 = original.replace("DNSManager=Auto", "DNSManager=Hold1")
        s2 = original.replace("DNSManager=Auto", "DNSManager=Hold2")
        engine_save(path, ours)
        h1 = os.path.join(tmp, ".confedit-h1")
        h2 = os.path.join(tmp, ".confedit-h2")
        with open(h1, "w") as handle:
            handle.write(s1)
        with open(h2, "w") as handle:
            handle.write(s2)
        with open(os.path.join(tmp, ".confedit-journal"), "w") as handle:
            handle.write("\t".join((sha(ours), sha(s1), "swap", "-", h1)) + "\n")
            handle.write("\t".join((sha(ours), sha(s2), "swap", "-", h2)) + "\n")
        eq("multiple unresolved legacy holds refused",
           run(path, "SplitTunnelingEnabled", "true"), 6)
        ok("journal preserved", journal_open(path))
        ok("both holds preserved", os.path.exists(h1) and os.path.exists(h2))
    finally:
        shutil.rmtree(tmp)

print("")
print(str(pass_count) + " passed, " + str(fail_count) + " failed")
sys.exit(0 if fail_count == 0 else 1)
