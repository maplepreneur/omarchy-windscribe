#!/usr/bin/env python3
"""Race-focused tests for the bundled confedit.py settings helper.

The node suite (tests/model-test.mjs) covers the helper end to end; this
file proves the coordination property the helper exists for: a write
from the Windscribe engine overlapping the helper's own
read-modify-write window — during the read, while the write is staged,
in the instant after the rename, or continuously — is reconciled on top
of the freshest content, never silently reverted. The staging scenario
fails against a helper whose last identity check precedes the fsync:
the rename then overwrites the engine's file and the read-back, which
verifies what replaced the file rather than what the file was, reports
success while the engine's setting is lost.

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

print("confedit.py: an engine save right after the rename is reconciled, not lost")
tmp = tempfile.mkdtemp(prefix="ws-confedit-")
try:
    path = make_conf(tmp)
    engine_text = read(path).replace("DNSManager=Auto", "DNSManager=custom")
    # The engine saves in the instant after the helper's rename: the
    # helper's snapshot is on disk, then the engine's file replaces it
    # wholesale, taking the helper's edit down with it.
    saved = patch_once(os, "replace", lambda: engine_save(path, engine_text))
    try:
        result = run(path, "FirewallMode", "Always On")
    finally:
        restore(os, "replace", saved)
    eq("edit re-applied on top of the engine's file", result, 0)
    final = read(path)
    ok("engine's newer setting survived", "DNSManager=custom" in final)
    ok("own key written", "FirewallMode=Always On" in final)
    ok("everything else byte-exact",
       final == engine_text.replace("FirewallMode=Auto", "FirewallMode=Always On"))
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
