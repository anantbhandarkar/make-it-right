"""Generation and destination safety. Run via `python3 init/test_init.py`.

These tests are written against the CLASS of defect, not the six instances that were
reported. The defects all reduce to two root causes:

  A. apply() wrote to a destination it had never inspected, so it could not tell an absent
     file from a symlink into $HOME, a human's AGENTS.md, or a settings.json it could not
     parse. The symlink cases are parametrised over EVERY item plan() emits, so a seventh
     artifact is covered the day it is added.

  B. idempotence was presence-detection: the ownership marker was read as a boolean and the
     content behind it was never compared to the desired state. The reconciliation tests
     therefore assert on the CONTENT after a re-run, never on "the marker is there".

The dangling-symlink case has its own test on purpose: os.path.exists() returns False for a
dangling link, so a fix built on exists() still follows the link and still creates the file
outside the repo -- it looks correct in review and is not.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import generate as gen  # noqa: E402

RESOLVED = ["mir-frontend", "mir-frontend-react", "mir-devsecops"]
ANSWERS = {"frontend": "mir-frontend-react"}
STAMP = "2026-01-01T00:00:00Z"

# A destination whose bytes would pass every ownership test mir has: valid JSON, and it
# carries the ownership marker. Only the symlink check can save it.
OUTSIDE_BODY = json.dumps({"_note": gen.MARK, "hooks": {"PreToolUse": []}}, indent=2) + "\n"


def _repo(tmp, name="proj"):
    p = os.path.join(tmp, name)
    os.makedirs(p)
    return p


def _plan(repo):
    return gen.plan(repo, RESOLVED, ANSWERS, STAMP)


def _tree(root):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for n in list(dirnames) + list(filenames):
            out.add(os.path.relpath(os.path.join(dirpath, n), root))
    return out


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _maybe(p):
    """The bytes at p, or None if it is not there. A test that asserts on a backup must be
    able to report 'no backup was made' as a failure rather than as a traceback that stops
    every check after it."""
    return _read(p) if os.path.isfile(p) else None


def _write(p, body):
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)


def _raises(fn):
    """Return the exception fn() raised, or None. Used instead of asserting on a boolean so
    a test can tell 'refused for the right reason' from 'blew up for another one'."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 -- the type is the thing under test
        return e
    return None


def _load_mutant(tmp, name, replacements):
    """Load a deliberately broken copy of generate.py, to prove a branch is load-bearing."""
    src = _read(os.path.join(HERE, "generate.py"))
    for old, new in replacements:
        assert old in src, "mutation target vanished from generate.py: %r" % old
        src = src.replace(old, new)
    path = os.path.join(tmp, name + ".py")
    _write(path, src)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HERE = HERE          # the mutant must still find guard.py/probe.py to copy
    return mod


def run(check):
    # -- positive control: a clean repo still generates a working harness ------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        items = _plan(repo)
        written = gen.apply(repo, items)
        check("clean repo: every planned artifact lands",
              all(os.path.isfile(os.path.join(repo, w)) for w in written), str(written))
        check("clean repo: the marker-bearing artifacts carry the marker",
              all(gen.MARK in (_maybe(os.path.join(repo, r)) or "")
                  for r in ("AGENTS.md", "CLAUDE.md")))
        check("generated files are regular files, not links",
              not any(os.path.islink(os.path.join(repo, w)) for w in written))

        # re-running must be a no-op for settings: the hook is already in its desired shape
        items2 = _plan(repo)
        paths2 = [it["path"] for it in items2]
        check("re-run drops the settings item once the hook already matches",
              ".claude/settings.json" not in paths2, str(paths2))
        gen.apply(repo, items2)
        settings = json.loads(_read(os.path.join(repo, ".claude", "settings.json")))
        pre = settings["hooks"]["PreToolUse"]
        check("re-run leaves exactly one mir hook entry",
              sum(1 for e in pre if e.get("_mir") == gen.HOOK_TAG) == 1, json.dumps(pre))

    # -- inspect_destination: the classification the whole write path hangs on --------------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        check("absent destination classifies as absent",
              gen.inspect_destination(repo, "AGENTS.md").state == gen.ABSENT)
        _write(os.path.join(repo, "AGENTS.md"), "# mine\n")
        check("a human's AGENTS.md classifies as unowned",
              gen.inspect_destination(repo, "AGENTS.md").state == gen.UNOWNED)
        _write(os.path.join(repo, "AGENTS.md"), "x\n<!-- %s -->\n" % gen.MARK)
        check("a marked AGENTS.md classifies as mir_owned",
              gen.inspect_destination(repo, "AGENTS.md").state == gen.MIR_OWNED)
        os.makedirs(os.path.join(repo, "CLAUDE.md"))
        check("a directory in the way classifies as non_regular",
              gen.inspect_destination(repo, "CLAUDE.md").state == gen.NON_REGULAR)
        _write(os.path.join(repo, ".claude", "settings.json"), '{"hooks":')
        check("a settings.json that will not parse classifies as unparseable, never absent",
              gen.inspect_destination(repo, ".claude/settings.json").state == gen.UNPARSEABLE)

    # -- F1: a symlinked destination is refused, for EVERY item plan() emits ----------------
    with tempfile.TemporaryDirectory() as tmp:
        rels = [it["path"] for it in _plan(_repo(tmp, "probe-plan"))]
    check("the plan still emits every artifact the symlink sweep expects",
          set(rels) >= {".mir/manifest.json", ".mir/guard.py", ".mir/probe.py",
                        ".claude/settings.json", "AGENTS.md", "CLAUDE.md"}, str(rels))

    for rel in rels:
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside.txt")
            _write(outside, OUTSIDE_BODY)
            repo = _repo(tmp)
            dest = os.path.join(repo, rel)
            if os.path.dirname(dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.symlink(outside, dest)
            before = _tree(repo)

            err = _raises(lambda: gen.apply(repo, _plan(repo)))
            check("symlinked %s is refused" % rel,
                  isinstance(err, gen.GenerateError) and "symlink" in str(err),
                  "%r: %s" % (type(err).__name__, err))
            check("symlinked %s: the external file is untouched" % rel,
                  _read(outside) == OUTSIDE_BODY)
            check("symlinked %s: nothing else was written either" % rel,
                  _tree(repo) == before, str(sorted(_tree(repo) - before)))
            check("symlinked %s: --dry-run says REFUSED instead of 'would write'" % rel,
                  any(it["path"] == rel and it["note"].startswith("REFUSED")
                      for it in _plan(repo)))

    # a symlinked PARENT redirects the write just as effectively as a symlinked file
    with tempfile.TemporaryDirectory() as tmp:
        elsewhere = os.path.join(tmp, "elsewhere")
        os.makedirs(elsewhere)
        repo = _repo(tmp)
        os.symlink(elsewhere, os.path.join(repo, ".claude"))
        err = _raises(lambda: gen.apply(repo, _plan(repo)))
        check("a symlinked parent directory is refused too",
              isinstance(err, gen.GenerateError) and "parent" in str(err), repr(err))
        check("a symlinked parent: nothing was written through it",
              os.listdir(elsewhere) == [], str(os.listdir(elsewhere)))

    # -- F1, the trap: exists() is False for a dangling link, lexists() is True -------------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        ghost = os.path.join(tmp, "not-there-yet.md")
        os.symlink(ghost, os.path.join(repo, "AGENTS.md"))
        check("the trap is set up as intended: exists() is False here",
              not os.path.exists(os.path.join(repo, "AGENTS.md"))
              and os.path.lexists(os.path.join(repo, "AGENTS.md")))
        err = _raises(lambda: gen.apply(repo, _plan(repo)))
        check("a DANGLING symlink destination is refused",
              isinstance(err, gen.GenerateError) and "dangling" in str(err), repr(err))
        check("a DANGLING symlink destination is not created outside the repo",
              not os.path.exists(ghost))
        check("dangling refusal is all-or-nothing: no manifest either",
              not os.path.exists(os.path.join(repo, ".mir", "manifest.json")))

    # -- F6: a malformed settings.json is not 'absent' --------------------------------------
    for body in ('{"hooks":', '{"hooks": {"PreToolUse": []},}',
                 '// a JSONC comment\n{"hooks": {}}'):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp)
            sp = os.path.join(repo, ".claude", "settings.json")
            _write(sp, body)
            err = _raises(lambda: gen.apply(repo, _plan(repo)))
            check("unparseable settings (%s...) is refused" % body[:14].replace("\n", " "),
                  isinstance(err, gen.GenerateError), repr(err))
            check("unparseable settings: the original bytes are intact", _read(sp) == body)
            check("unparseable settings: a backup was written",
                  _maybe(sp + ".mir-backup") == body, repr(_maybe(sp + ".mir-backup")))
            check("unparseable settings: no harness was written",
                  not os.path.exists(os.path.join(repo, "AGENTS.md"))
                  and not os.path.exists(os.path.join(repo, ".mir", "manifest.json")))

    # bytes that are not text at all: refuse, never "repair" them into a regeneration
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        raw = b'{"hooks": {"x": "\xff\xfe"}}'
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "settings.json"), "wb") as f:
            f.write(raw)
        with open(os.path.join(repo, "AGENTS.md"), "wb") as f:
            f.write(b"# notes \xff\xfe\n")
        err = _raises(lambda: gen.apply(repo, _plan(repo)))
        check("a settings.json that is not valid UTF-8 is refused, not decoded lossily",
              isinstance(err, gen.GenerateError) and "UTF-8" in str(err), repr(err))
        with open(os.path.join(repo, ".claude", "settings.json"), "rb") as f:
            check("non-UTF-8 settings keeps its exact bytes", f.read() == raw)

    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        sp = os.path.join(repo, ".claude", "settings.json")
        _write(sp, '{"hooks":')
        _write(sp + ".mir-backup", "an earlier backup")
        _raises(lambda: gen.apply(repo, _plan(repo)))
        check("a second refusal never overwrites the first backup",
              _maybe(sp + ".mir-backup") == "an earlier backup"
              and _maybe(sp + ".mir-backup.1") == '{"hooks":',
              repr(_maybe(sp + ".mir-backup.1")))

    # -- F11: a human-owned AGENTS.md/CLAUDE.md is refused and backed up --------------------
    for rel in ("AGENTS.md", "CLAUDE.md"):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp)
            body = "# %s\n\nhand-written notes that took an afternoon\n" % rel
            _write(os.path.join(repo, rel), body)
            err = _raises(lambda: gen.apply(repo, _plan(repo)))
            check("an unowned %s is refused, not silently replaced" % rel,
                  isinstance(err, gen.GenerateError) and "marker" in str(err), repr(err))
            check("an unowned %s keeps its original bytes" % rel,
                  _read(os.path.join(repo, rel)) == body)
            check("an unowned %s is backed up" % rel,
                  _maybe(os.path.join(repo, rel + ".mir-backup")) == body,
                  repr(_maybe(os.path.join(repo, rel + ".mir-backup"))))
            check("an unowned %s aborts the whole run" % rel,
                  not os.path.exists(os.path.join(repo, ".mir", "manifest.json")))

    # -- F3/D2: the marker is an address, so a stale matcher must be rewritten ---------------
    stale = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]},
        {"matcher": "Write", "_mir": gen.HOOK_TAG,
         "hooks": [{"type": "command", "command": "python3 old-path.py"}]},
    ]}}
    merged, changed = gen.merge_settings(stale, ".mir/guard.py")
    mine = [e for e in merged["hooks"]["PreToolUse"] if e.get("_mir") == gen.HOOK_TAG]
    check("a stale matcher behind the tag is rewritten to the desired one",
          len(mine) == 1 and mine[0]["matcher"] == gen.MATCHER, json.dumps(mine))
    check("a stale matcher reports changed=True (silence would mean no new coverage)", changed)
    check("a stale command behind the tag is rewritten too",
          mine[0]["hooks"][0]["command"].endswith('.mir/guard.py"'), json.dumps(mine))
    check("reconciling never disturbs the user's own hook entry",
          any(h.get("command") == "echo user"
              for e in merged["hooks"]["PreToolUse"] for h in e.get("hooks", [])))
    check("merge_settings does not mutate the caller's settings",
          stale["hooks"]["PreToolUse"][1]["matcher"] == "Write")
    _again, changed2 = gen.merge_settings(merged, ".mir/guard.py")
    check("reconciling an already-correct file reports changed=False", not changed2)
    check("reconciling an already-correct file is a fixed point",
          json.dumps(_again, sort_keys=True) == json.dumps(merged, sort_keys=True))

    dupes = {"hooks": {"PreToolUse": [
        {"matcher": "Write", "_mir": gen.HOOK_TAG, "hooks": []},
        {"matcher": "Edit", "_mir": gen.HOOK_TAG, "hooks": []},
    ]}}
    merged_d, changed_d = gen.merge_settings(dupes, ".mir/guard.py")
    check("duplicate mir entries from older runs collapse to exactly one",
          changed_d and len(merged_d["hooks"]["PreToolUse"]) == 1,
          json.dumps(merged_d["hooks"]["PreToolUse"]))

    check("an untagged legacy entry is matched by command, not appended twice",
          len(gen.merge_settings(
              {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                  {"type": "command",
                   "command": 'python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"'}]}]}},
              ".mir/guard.py")[0]["hooks"]["PreToolUse"]) == 1)

    check("settings with no hooks at all still gets the hook, and reports changed",
          gen.merge_settings({}, ".mir/guard.py")[1]
          and gen.merge_settings(None, ".mir/guard.py")[1])

    # -- F10: tail preservation is parametrised over every marker-bearing artifact ----------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        items = _plan(repo)
        tailed = [it["path"] for it in items if gen.TAIL_MARK in it["content"]]
        check("more than one artifact offers a preserved tail (a list of one is not a rule)",
              len(tailed) >= 2, str(tailed))
        gen.apply(repo, items)
        for rel in tailed:
            p = os.path.join(repo, rel)
            _write(p, _read(p) + "\nKEEP-%s\n" % rel.replace("/", "-"))
        gen.apply(repo, gen.plan(repo, ["mir-backend", "mir-devsecops"],
                                {"backend": "mir-backend"}, "2026-02-02T00:00:00Z"))
        for rel in tailed:
            body = _maybe(os.path.join(repo, rel)) or ""
            sentinel = "KEEP-%s" % rel.replace("/", "-")
            check("%s keeps the human's tail across a regeneration" % rel,
                  body.count(sentinel) == 1, body[-160:])
            check("%s still regenerated its owned region" % rel,
                  gen.MARK in body and body.count(gen.TAIL_MARK) == 1)
        check("AGENTS.md's owned region actually changed (the tail did not freeze the file)",
              "mir-backend`" in _read(os.path.join(repo, "AGENTS.md")))

    # -- F9: prune deletes only links that resolve inside this checkout ---------------------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        checkout = os.path.join(tmp, "checkout")
        os.makedirs(os.path.join(checkout, "skills", "mir-inside"))
        os.makedirs(os.path.join(checkout, "skills", "mir-kept"))
        foreign = os.path.join(tmp, "someone-elses-skill")
        os.makedirs(foreign)
        sk = os.path.join(repo, ".claude", "skills")
        os.makedirs(sk)
        os.symlink(os.path.join(checkout, "skills", "mir-inside"), os.path.join(sk, "mir-inside"))
        os.symlink(os.path.join(checkout, "skills", "mir-kept"), os.path.join(sk, "mir-kept"))
        os.symlink(foreign, os.path.join(sk, "mir-private"))
        os.symlink(os.path.join(tmp, "gone"), os.path.join(sk, "mir-dangling-outside"))
        os.makedirs(os.path.join(sk, "mir-realdir"))

        removed = gen.prune_project_skills(repo, ["mir-kept"], repo_dir=checkout)
        check("prune removes a stale link that resolves inside this checkout",
              removed == ["mir-inside"], str(removed))
        check("prune leaves a mir-* link pointing OUTSIDE the checkout alone",
              os.path.islink(os.path.join(sk, "mir-private")))
        check("prune leaves a dangling foreign link alone",
              os.path.lexists(os.path.join(sk, "mir-dangling-outside")))
        check("prune never touches a real directory named mir-*",
              os.path.isdir(os.path.join(sk, "mir-realdir"))
              and not os.path.islink(os.path.join(sk, "mir-realdir")))
        check("prune keeps what it was told to keep",
              os.path.islink(os.path.join(sk, "mir-kept")))

    with tempfile.TemporaryDirectory() as tmp:
        # the default repo_dir must keep the existing two-argument call sites working
        repo = _repo(tmp)
        sk = os.path.join(repo, ".claude", "skills")
        os.makedirs(sk)
        real = os.path.join(os.path.dirname(HERE), "skills", "mir-backend")
        check("the checkout has the skill this default-argument test relies on",
              os.path.isdir(real), real)
        os.symlink(real, os.path.join(sk, "mir-backend"))
        os.symlink(os.path.join(tmp, "outside-target"), os.path.join(sk, "mir-outside"))
        removed = gen.prune_project_skills(repo, [])
        check("prune_project_skills(repo, keep) still works without repo_dir",
              removed == ["mir-backend"], str(removed))
        check("the two-argument call still spares a foreign link",
              os.path.lexists(os.path.join(sk, "mir-outside")))

    # -- mutation: prove the symlink branch is what refuses, not something downstream --------
    with tempfile.TemporaryDirectory() as tmp:
        outside = os.path.join(tmp, "outside.md")
        _write(outside, "# shared\n<!-- %s -->\n" % gen.MARK)

        def _scenario(mod, name):
            repo = _repo(tmp, name)
            os.symlink(outside, os.path.join(repo, "AGENTS.md"))
            return _raises(lambda: mod.apply(repo, mod.plan(repo, RESOLVED, ANSWERS, STAMP)))

        blind = _load_mutant(tmp, "mutant_no_islink",
                             [("os.path.islink(", "_no_links("),
                              ('MARK = "generated by mir init"',
                               'def _no_links(p):\n    return False\n\n\n'
                               'MARK = "generated by mir init"')])
        err = _scenario(blind, "mutant-a")
        check("MUTATION: without the islink branch, the symlink is no longer refused",
              not isinstance(err, blind.GenerateError), repr(err))

        naive = _load_mutant(tmp, "mutant_no_islink_no_nofollow",
                             [("os.path.islink(", "_no_links("),
                              ('getattr(os, "O_NOFOLLOW", 0)', "0"),
                              ('MARK = "generated by mir init"',
                               'def _no_links(p):\n    return False\n\n\n'
                               'MARK = "generated by mir init"')])
        _scenario(naive, "mutant-b")
        check("MUTATION: with O_NOFOLLOW gone too, the external file IS truncated",
              "# shared" not in _read(outside), _read(outside)[:80])
        shutil.rmtree(os.path.join(tmp, "mutant-b"), ignore_errors=True)
