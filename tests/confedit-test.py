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

print("confedit.py: a later retry adopts a journaled newer save before succeeding")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        original = read(path)
        # The state left when merge attempts exhaust: the active file is
        # an older install of ours, and a side file holds the engine's
        # newest save. A later retry must not report success until that
        # save is the active file.
        older = original.replace("FirewallMode=Auto", "FirewallMode=Always On")
        newest = original.replace("Autoconnect=false", "Autoconnect=true").replace(
            "DNSManager=Auto", "DNSManager=Newest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(older)
        hold = os.path.join(tmp, ".confedit-held")
        with open(hold, "w", encoding="utf-8") as handle:
            handle.write(newest)
        confedit._add_record(path, hold, confedit.text_sha(older))
        confedit._set_displaced(path, hold, confedit.text_sha(newest))
        result = run(path, "SplitTunnelingEnabled", "true")
        eq("later retry confirms on top of the journaled save", result, 0)
        final = read(path)
        ok("newest engine setting is active", "DNSManager=Newest" in final, final)
        ok("newest autoconnect is active", "Autoconnect=true" in final)
        ok("retried key written", "SplitTunnelingEnabled=true" in final)
        ok("no newer save left only in a side file",
           not any(name.startswith(".confedit-") and name != ".confedit.lock"
                   for name in os.listdir(tmp)))
finally:
    shutil.rmtree(tmp)

print("confedit.py: a failed read of the displaced file does not delete it")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        # The engine saves in the instant before the commit, so the swap
        # displaces the newer preferences. The first read of that displaced
        # file fails. The helper must not unlink it -- the previous code
        # deleted it in the cleanup that runs on any exception.
        real_read = confedit.read_lines
        real_exchange = confedit.exchange_paths
        state = {"swapped": False, "failed": False}

        def exchange_once(old, new):
            if not state["swapped"]:
                state["swapped"] = True
                engine_save(path, engine_text)
            return real_exchange(old, new)

        def read_fail_displaced(target):
            lines = real_read(target)
            if (not state["failed"] and target != path
                    and "Autoconnect=true" in "\n".join(lines)):
                state["failed"] = True
                raise OSError(5, "Input/output error")
            return lines

        confedit.exchange_paths = exchange_once
        confedit.read_lines = read_fail_displaced
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", real_exchange)
            restore(confedit, "read_lines", real_read)
        ok("failed displaced read did not report success without the save",
           result != 0 or "Autoconnect=true" in read(path),
           "exit " + str(result))
        present = []
        for name in os.listdir(tmp):
            text = read(os.path.join(tmp, name))
            if "Autoconnect=true" in text:
                present.append(name)
        ok("displaced save was not deleted when its read failed",
           bool(present), "dir " + str(os.listdir(tmp)))
        if result != 0:
            retry = run(path, "FirewallMode", "Always On")
            eq("later retry after a failed displaced read", retry, 0)
            final = read(path)
            ok("newer save is the active file after the retry",
               "Autoconnect=true" in final, final)
            ok("own key written on top of the restored save",
               "FirewallMode=Always On" in final)
        else:
            ok("success left the newer save active",
               "Autoconnect=true" in read(path))
finally:
    shutil.rmtree(tmp)

print("confedit.py: a failed read still keeps the displaced file if restore fails")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    if confedit.exchange_paths is None:
        print("skip -- no renameat2 on this platform")
    else:
        path = make_conf(tmp)
        engine_text = read(path).replace("Autoconnect=false", "Autoconnect=true")
        # Forward swap displaces the newer save, the read of it fails, and
        # the swap that would put it back also fails. The previous cleanup
        # unlinked the displaced file on that exception. It must survive,
        # and a later retry must publish it to the active file before
        # reporting success.
        real_read = confedit.read_lines
        real_exchange = confedit.exchange_paths
        state = {"n": 0, "failed": False}

        def exchange_then_fail(old, new):
            state["n"] += 1
            if state["n"] == 1:
                engine_save(path, engine_text)
                return real_exchange(old, new)
            raise OSError(5, "Input/output error")

        def read_fail_displaced(target):
            lines = real_read(target)
            if (not state["failed"] and target != path
                    and "Autoconnect=true" in "\n".join(lines)):
                state["failed"] = True
                raise OSError(5, "Input/output error")
            return lines

        confedit.exchange_paths = exchange_then_fail
        confedit.read_lines = read_fail_displaced
        try:
            result = run(path, "FirewallMode", "Always On")
        finally:
            restore(confedit, "exchange_paths", real_exchange)
            restore(confedit, "read_lines", real_read)
        eq("unreadable displaced save is an I/O error, not success", result, 6)
        held = [name for name in os.listdir(tmp)
                if "Autoconnect=true" in read(os.path.join(tmp, name))]
        ok("displaced copy survived the failed read and the failed restore",
           bool(held), "dir " + str(os.listdir(tmp)))
        retry = run(path, "FirewallMode", "Always On")
        eq("later retry publishes the preserved save", retry, 0)
        final = read(path)
        ok("preserved save is active after the retry",
           "Autoconnect=true" in final, final)
        ok("own key written on the preserved save",
           "FirewallMode=Always On" in final)
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
                              path, "Connection", "FirewallMode", "Always On"])
        b = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(confedit.__file__), "confedit.py"),
                              path, "Connection", "SplitTunnelingEnabled", "true"])
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

print("")
print(str(pass_count) + " passed, " + str(fail_count) + " failed")
sys.exit(0 if fail_count == 0 else 1)
