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
    check("a stale command behind the tag is rewritten too -- including onto the v2 command, "
          "which is the migration every existing harness needs and the one a tag-as-boolean "
          "check would have skipped",
          '.mir/guard.py' in mine[0]["hooks"][0]["command"]
          and mine[0]["hooks"][0]["command"].endswith("--protocol claude"), json.dumps(mine))
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

    # -- G1: install_project_skills applies the same _owned_by predicate prune does ---------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        checkout = os.path.join(tmp, "checkout")
        os.makedirs(os.path.join(checkout, "skills", "mir-frontend-react"))
        foreign_target = os.path.join(tmp, "someone-elses-checkout", "mir-frontend-react")
        os.makedirs(foreign_target)
        sk = os.path.join(repo, ".claude", "skills")
        os.makedirs(sk)
        os.symlink(foreign_target, os.path.join(sk, "mir-frontend-react"))
        os.makedirs(os.path.join(sk, "mir-devsecops"))  # a real directory, never touched

        linked = gen.install_project_skills(
            repo, ["mir-frontend", "mir-frontend-react", "mir-devsecops"], checkout)
        check("install does not link a slug whose name already points OUTSIDE the checkout",
              "mir-frontend-react" not in linked, str(linked))
        check("the foreign link survives install untouched",
              os.path.realpath(os.path.join(sk, "mir-frontend-react"))
              == os.path.realpath(foreign_target))
        check("a real directory named mir-devsecops is never replaced by a symlink",
              os.path.isdir(os.path.join(sk, "mir-devsecops"))
              and not os.path.islink(os.path.join(sk, "mir-devsecops")))

        # a link that already points INSIDE the checkout is replaced normally (re-run
        # idempotence). Two dashes -- a tier/module, not a pillar -- so install does not skip
        # it the way it skips depth-1 slugs.
        os.makedirs(os.path.join(checkout, "skills", "mir-backend-go"))
        os.symlink(os.path.join(checkout, "skills", "mir-backend-go"),
                   os.path.join(sk, "mir-backend-go"))
        linked2 = gen.install_project_skills(repo, ["mir-backend-go"], checkout)
        check("a link already pointing inside the checkout is replaced normally",
              linked2 == ["mir-backend-go"], str(linked2))

        # a brand-new slug is linked normally: the predicate does not block absent destinations
        os.makedirs(os.path.join(checkout, "skills", "mir-backend-python"))
        linked3 = gen.install_project_skills(repo, ["mir-backend-python"], checkout)
        check("installing a slug with no existing destination still links it",
              linked3 == ["mir-backend-python"]
              and os.path.islink(os.path.join(sk, "mir-backend-python")))

        check("install_project_skills(repo, resolved) still works without repo_dir",
              gen.install_project_skills(_repo(tmp, "proj2"), []) == [])

    # -- G1 mutation: prove the _owned_by check is what spares the foreign link -------------
    with tempfile.TemporaryDirectory() as tmp:
        checkout = os.path.join(tmp, "checkout")
        os.makedirs(os.path.join(checkout, "skills", "mir-frontend-react"))
        foreign_target = os.path.join(tmp, "elsewhere", "mir-frontend-react")
        os.makedirs(foreign_target)
        repo = _repo(tmp)
        sk = os.path.join(repo, ".claude", "skills")
        os.makedirs(sk)
        os.symlink(foreign_target, os.path.join(sk, "mir-frontend-react"))

        mutant = _load_mutant(
            tmp, "mutant_install_no_owned_by",
            [("if os.path.islink(dst) and not _owned_by(dst, repo_dir):",
              "if False:")])
        linked = mutant.install_project_skills(
            repo, ["mir-frontend", "mir-frontend-react"], checkout)
        check("MUTATION: without the _owned_by guard, the foreign link IS clobbered",
              "mir-frontend-react" in linked
              and os.path.realpath(os.path.join(sk, "mir-frontend-react"))
              == os.path.realpath(os.path.join(checkout, "skills", "mir-frontend-react")),
              str(linked))

    # -- G2: settings.local.json is inspected for a stale mir hook, never written -----------
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp)
        check("absent settings.local.json: no warnings, no crash",
              gen.local_settings_warnings(repo) == [])

        # Built from the target's own command builder, never spelled inline. A literal here
        # went stale the moment `--protocol` landed, and a stale fixture makes this test pass
        # or fail for a reason of its own making rather than for the property it names.
        good_cmd = gen.claude_guard_command(".mir/guard.py")
        _write(os.path.join(repo, ".claude", "settings.local.json"), json.dumps({
            "hooks": {"PreToolUse": [
                {"matcher": gen.MATCHER, "_mir": gen.HOOK_TAG,
                 "hooks": [{"type": "command", "command": good_cmd}]},
            ]},
        }))
        check("present but already-correct mir hook in settings.local.json: no warnings",
              gen.local_settings_warnings(repo) == [])

        _write(os.path.join(repo, ".claude", "settings.local.json"), json.dumps({
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "_mir": gen.HOOK_TAG,
                 "hooks": [{"type": "command", "command": good_cmd}]},
            ]},
        }))
        warnings = gen.local_settings_warnings(repo)
        check("a stale matcher behind the tag in settings.local.json is reported",
              len(warnings) == 1 and "STALE" in warnings[0] and "settings.local.json" in warnings[0],
              str(warnings))

        check("settings.local.json is never written by mir_settings_warnings",
              json.loads(_read(os.path.join(repo, ".claude", "settings.local.json")))
              ["hooks"]["PreToolUse"][0]["matcher"] == "Bash")

        # an entry with no mir tag/command at all is not mir's business
        _write(os.path.join(repo, ".claude", "settings.local.json"), json.dumps({
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]},
            ]},
        }))
        check("a non-mir hook in settings.local.json is not flagged",
              gen.local_settings_warnings(repo) == [])

        # unreadable/unparseable is reported, not raised -- mir does not own this file
        _write(os.path.join(repo, ".claude", "settings.local.json"), "{not json")
        warnings = gen.local_settings_warnings(repo)
        check("an unparseable settings.local.json is reported, not a crash",
              len(warnings) == 1 and "could not be read" in warnings[0], str(warnings))

        # a full plan()/apply() run never touches settings.local.json even when it is stale
        _write(os.path.join(repo, ".claude", "settings.local.json"), json.dumps({
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "_mir": gen.HOOK_TAG,
                 "hooks": [{"type": "command", "command": good_cmd}]},
            ]},
        }))
        before = _read(os.path.join(repo, ".claude", "settings.local.json"))
        gen.apply(repo, _plan(repo))
        check("apply() never rewrites settings.local.json",
              _read(os.path.join(repo, ".claude", "settings.local.json")) == before)
        check("settings.local.json is not among plan()'s write targets",
              gen.LOCAL_SETTINGS_REL not in [it["path"] for it in _plan(repo)])

    _p3b_targets(check)
    with tempfile.TemporaryDirectory() as tmp:
        _p3b_generation(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _p3b_migration(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _p3b_skill_dirs(check, tmp)
    _p3b_cli(check)


# -- P3b: cross-agent init --------------------------------------------------------------
#
# The properties below are the ones that decide whether "supports Codex and Antigravity"
# means anything. Every one of them is a way the feature could ship green and inert:
#
#   B1.1  a target that does not STATE its enforcement level inherits silence
#   B1.2  a file emitted where nothing reads it (defect D6, which cost a release)
#   B1.6  a self-protection gap opened by the very files this feature adds
#   B1.7  a coverage table whose "what was NOT proven" cell is empty for an unverified target
#   B1.8  a generated AGENTS.md that claims enforcement no target provides

def _p3b_targets(check):
    import targets as tp
    from targets.base import Capability, Target, check_capabilities

    check("B1.1: every target supplies all four capability keys",
          all(set(tp.CAPABILITY_KEYS) <= set(t.capabilities) for t in tp.ALL),
          str({t.name: sorted(t.capabilities) for t in tp.ALL}))
    check("B1.1: every level is one of the four, and every claim names a mechanism and a "
          "source -- 'supported' with nothing behind it is the defect this release closes",
          not [e for t in tp.ALL for e in check_capabilities(t)],
          str([e for t in tp.ALL for e in check_capabilities(t)]))

    # THE KeyError IS THE TEST. A fifth target must state its enforcement level in its own
    # file, in front of a reviewer, rather than rendering four blank rows.
    class _Fifth(Target):
        name = "fifth"
        guard_protocol = "fifth"
        capabilities = {"skills": Capability("none", "m", "s")}

    raised = False
    try:
        _Fifth().capability("write_policy_enforcement")
    except KeyError:
        raised = True
    check("B1.1: a target with no write_policy_enforcement raises KeyError -- it does not "
          "default to a level nobody chose", raised)
    check("B1.1: and check_capabilities names every key it is missing, so the failure is "
          "actionable rather than a bare traceback",
          len(check_capabilities(_Fifth())) == 3,
          str(check_capabilities(_Fifth())))

    # A PIN, not a mutation, and labelled as one. The behaviour is already asserted above;
    # what this catches is the edit that would soften it -- `capabilities.get(key)` reads as
    # a tidy-up, passes every behavioural test that does not ask for a missing key, and hands
    # the fifth target a level nobody chose.
    check("B1.1 pin: capability() indexes, it does not .get() -- a default here is exactly "
          "the silence the KeyError exists to forbid",
          "self.capabilities[key]" in
          open(os.path.join(HERE, "targets", "base.py"), encoding="utf-8").read())

    check("B1.2: claude registers .claude/settings.json, codex .codex/hooks.json, "
          "antigravity .agents/hooks.json",
          tp.BY_NAME["claude"].wiring_files[0] == os.path.join(".claude", "settings.json")
          and tp.BY_NAME["codex"].wiring_files == (".codex/hooks.json",)
          and tp.BY_NAME["antigravity"].wiring_files == (".agents/hooks.json",),
          str([(t.name, t.wiring_files) for t in tp.ALL]))
    check("B1.2: cursor registers NOTHING -- an emitted file nothing reads is defect D6",
          tp.BY_NAME["cursor"].wiring_files == ())
    check("B1.2: cursor's output names the exact toggle, because 'enable third-party "
          "configs' is not something a user can find",
          any("Include third-party Plugins, Skills, and other configs" in n
              for n in tp.BY_NAME["cursor"].notes(None)))
    check("cursor speaks CLAUDE's protocol, not its own -- it reads Claude Code's config",
          tp.BY_NAME["cursor"].guard_protocol == "claude")
    check("the fail-open posture is DECLARED per target, and antigravity's is the inverted "
          "one -- inheriting it silently is the whole failure mode",
          tp.BY_NAME["antigravity"].fail_open is False
          and tp.BY_NAME["claude"].fail_open is True)

    # The two files that must agree about where a host registers a hook, and about what a
    # manifest with no targets block means. probe.py is COPIED into projects, so a
    # disagreement here is a target that verifies clean by being unlooked-at.
    import guard as guard_mod
    import probe
    check("every target is in probe.TARGET_WIRING -- an unknown target is a wiring FAILURE, "
          "so a target missing from that table can never verify",
          all(t.name in probe.TARGET_WIRING for t in tp.ALL),
          str([t.name for t in tp.ALL if t.name not in probe.TARGET_WIRING]))
    check("and the files agree, both ways",
          all(tuple(probe.TARGET_WIRING[t.name]) == tuple(t.wiring_files) for t in tp.ALL),
          str({t.name: (t.wiring_files, probe.TARGET_WIRING[t.name]) for t in tp.ALL}))
    check("targets.DEFAULT and probe.DEFAULT_TARGET agree: two files disagreeing about the "
          "implicit target is an old harness verifying nothing while reporting clean",
          tp.DEFAULT == probe.DEFAULT_TARGET)
    check("every target's protocol is one the guard answers for -- an unanswerable protocol "
          "is exit 3 on every call",
          all(t.guard_protocol in guard_mod.PROTOCOLS for t in tp.ALL))
    check("guard and probe agree about which host reads stdout",
          set(guard_mod.STDOUT_PROTOCOLS) ==
          {p for p, c in probe.PROTOCOL_CHANNEL.items() if c == probe.CHANNEL_STDOUT})
    from targets import antigravity as ag_mod
    check("the antigravity matcher is `*`, not an enumerated list: matchers compile anchored "
          "and the write surface is wider than the three obvious tools -- sed_file, "
          "notebook_edit and call_mcp_tool all write, so enumerating three names is a hole",
          ag_mod.MATCHER == "*"
          and ag_mod.hooks_document()["hooks"]["PreToolUse"][0]["matcher"] == "*")
    check("and the emitted hooks file carries an ownership marker in its bytes, so a "
          "hand-written .codex/.agents hooks file is refused rather than clobbered",
          gen.MARK in json.dumps(ag_mod.hooks_document()))

    check("resolve() takes a comma list and `all`",
          [t.name for t in tp.resolve("codex,cursor")] == ["codex", "cursor"]
          and [t.name for t in tp.resolve("all")] == list(tp.NAMES))
    check("resolve() defaults to claude alone",
          [t.name for t in tp.resolve("")] == ["claude"])
    check("resolve() dedupes rather than emitting a target twice",
          [t.name for t in tp.resolve("codex,codex")] == ["codex"])
    _refused = False
    try:
        tp.resolve("codx")
    except ValueError:
        _refused = True
    check("an unknown --target is REFUSED, never dropped: `--target codx` must not quietly "
          "produce a claude-only harness the user believes covers Codex", _refused)


def _p3b_generation(check, tmp):
    import agents_export
    import schema
    import targets as tp

    check("B1.6: the self-protection gap this feature opens is closed -- .codex, .agents "
          "and ~/.gemini are denied",
          {".codex", ".agents", "~/.gemini"} <= set(schema.BASELINE_DENIED),
          str(schema.BASELINE_DENIED))
    check("B1.6: .agents is denied WHOLESALE, not just .agents/hooks.json -- denying the one "
          "file leaves the skills beside it rewritable",
          ".agents" in schema.BASELINE_DENIED
          and ".agents/hooks.json" not in schema.BASELINE_DENIED)
    check("B1.9: MANIFEST_VERSION is 2 and generated manifests carry it",
          schema.MANIFEST_VERSION == 2)

    repo = _repo(tmp, "all-targets")
    items = gen.plan(repo, RESOLVED, ANSWERS, STAMP, tp.ALL)
    paths = [it["path"] for it in items]
    check("B1.2 end to end: --target all writes all three enforcement files",
          {".claude/settings.json", ".codex/hooks.json", ".agents/hooks.json"} <= set(paths),
          str(paths))
    check("B1.2 end to end: and NO cursor-specific file",
          not [p for p in paths if "cursor" in p.lower()], str(paths))
    gen.apply(repo, items)

    manifest = json.load(open(os.path.join(repo, ".mir", "manifest.json"), encoding="utf-8"))
    check("B1.3: the resolved target list is written into the MANIFEST, so the probe reads "
          "the declared set from the harness and not from whoever last typed a flag",
          set(manifest["targets"]) == set(tp.NAMES), str(manifest.get("targets")))
    check("B1.3: each entry names the protocol its adapter answers on",
          manifest["targets"]["cursor"]["protocol"] == "claude", str(manifest["targets"]))
    check("B1.9: the generated manifest carries version 2",
          manifest["mir_manifest_version"] == 2)
    check("the generated manifest validates against its own schema",
          schema.validate_manifest(manifest) == [], str(schema.validate_manifest(manifest)))

    # B1.6's second half: .agents is denied AND mir still writes .agents/hooks.json, because
    # the generator runs outside the agent's tool loop. If this were impossible the denial
    # would have had to be narrowed, which is how the hole gets reopened.
    check("B1.6: mir can still write .agents/hooks.json even though .agents is denied -- the "
          "generator is not the agent",
          os.path.isfile(os.path.join(repo, ".agents", "hooks.json")))

    # ---- B1.7: COVERAGE.md
    cov = open(os.path.join(repo, ".mir", "COVERAGE.md"), encoding="utf-8").read()
    check("B1.7: COVERAGE.md exists and opens with a VERDICT BLOCK, not a table -- a table "
          "invites the reader to find their own row and stop",
          cov.index("ENFORCED   (guard decides") < cov.index("| target | capability"),
          "verdict block is not before the table")
    check("B1.7: the verdict block names claude enforced, codex+antigravity unverified, and "
          "cursor advisory",
          "ENFORCED   (guard decides, host hook registered): claude" in cov
          and "UNVERIFIED (hook file emitted, host invocation not proven): antigravity, codex" in cov
          and "ADVISORY ONLY — the manifest is NOT enforced for: cursor" in cov, cov[:900])

    rows = [ln for ln in cov.splitlines()
            if ln.startswith("| ") and "write_policy_enforcement" in ln]
    check("B1.7: there is one write-policy row per target", len(rows) == len(tp.ALL), str(len(rows)))
    _empty = [r for r in rows if "**unverified**" in r and "| — |" in r]
    check("B1.7: every UNVERIFIED row carries a non-empty 'what the probe did NOT prove' "
          "cell -- for an unverified target that cell IS the finding",
          not _empty, str(_empty))
    check("B1.7: and every row carries a manual command to confirm it",
          all("record" in r or "confirm" in r or "restart" in r for r in rows), str(rows))

    check("B1.7: agents_export's LOSSY_FIELDS surfaces in COVERAGE.md -- a loss recorded and "
          "not rendered is a loss documented to nobody",
          agents_export.LOSSY_FIELDS["codex"][0]["cost"][:60] in cov)
    check("B1.7: and the known loss is named: Codex sub-agent TOML has no `tools:`, so the "
          "reviewers' read-only restriction is a real capability loss",
          "no `tools:` equivalent" in cov)
    check("B1.7: there is NO checked-in agents/codex parallel catalog",
          not os.path.exists(os.path.join(os.path.dirname(HERE), "agents", "codex")))
    check("B1.7: agents_export derives from the .md frontmatter and finds every reviewer",
          {a["name"] for a in agents_export.read_agents()} ==
          {f[:-3] for f in os.listdir(os.path.join(os.path.dirname(HERE), "agents"))
           if f.endswith(".md")},
          str([a["name"] for a in agents_export.read_agents()]))
    check("B1.7: every agent file is parseable, so nothing is silently dropped from the export",
          agents_export.problems() == [], str(agents_export.problems()))
    _tools = [a for a in agents_export.read_agents() if a["tools"]]
    check("B1.7: the loss is DEMONSTRABLE -- the source declares tools, the Codex TOML has "
          "no tools key, and the conversion says so in the artifact",
          _tools and "tools" not in agents_export.to_codex_toml(_tools[0]).split("[agents.")[1]
          and "LOSS" in agents_export.to_codex_toml(_tools[0]))
    check("B1.7: and nothing is emitted for Codex agents, because no directory for them was "
          "found -- mir does not invent a path",
          agents_export.export_items("codex") == [])

    # ---- B1.8: AGENTS.md stops claiming Claude Code unconditionally
    agents_md = open(os.path.join(repo, "AGENTS.md"), encoding="utf-8").read()
    check("B1.8: AGENTS.md is under Antigravity's 12,000-char per-rule-file cap -- over it "
          "the file is TRUNCATED, and a truncated write-policy paragraph documents a rule "
          "the agent never reads",
          len(agents_md) < gen.AGENTS_MD_CAP, str(len(agents_md)))
    check("B1.8: it names the ENFORCING target rather than claiming enforcement flatly",
          "ENFORCED for claude" in agents_md, agents_md[-700:])
    check("B1.8: and it says an advisory target is not enforced IN THE SAME BREATH -- an "
          "agent that stops reading early must not stop after the reassuring half",
          "NOT ENFORCED for cursor" in agents_md, agents_md[-700:])
    check("B1.8: the old unconditional claim is gone",
          "registered in `.claude/settings.json`)" not in agents_md)
    check("B1.8: AGENTS.md stays THIN -- no hook JSON, no TOML, no per-tool config",
          "PreToolUse" not in agents_md and "[agents." not in agents_md
          and '"matcher"' not in agents_md)
    check("B1.8: and it defers the depth to COVERAGE.md", ".mir/COVERAGE.md" in agents_md)

    # A claude-only harness must not inherit the multi-target wording, or the paragraph is
    # just as unconditional as the one it replaced, pointed the other way.
    solo = _repo(tmp, "solo")
    gen.apply(solo, gen.plan(solo, RESOLVED, ANSWERS, STAMP, [tp.BY_NAME["claude"]]))
    solo_md = open(os.path.join(solo, "AGENTS.md"), encoding="utf-8").read()
    check("B1.8 control: a claude-only harness says ENFORCED and names no advisory target",
          "ENFORCED for claude" in solo_md and "NOT ENFORCED" not in solo_md, solo_md[-500:])
    check("a claude-only run writes no .codex or .agents file",
          not os.path.exists(os.path.join(solo, ".codex"))
          and not os.path.exists(os.path.join(solo, ".agents")))
    check("a claude-only manifest still declares its target explicitly",
          json.load(open(os.path.join(solo, ".mir", "manifest.json"),
                         encoding="utf-8"))["targets"] == {"claude": {
              "protocol": "claude",
              "wiring_files": list(tp.BY_NAME["claude"].wiring_files),
              "write_policy_enforcement": "enforced"}})

    # A cursor-only harness is the sharpest case: it writes NO enforcement file at all, and
    # the AGENTS.md must not imply otherwise.
    cur = _repo(tmp, "cursoronly")
    cur_items = gen.plan(cur, RESOLVED, ANSWERS, STAMP, [tp.BY_NAME["cursor"]])
    check("a cursor-only run emits no enforcement file whatsoever",
          not [it for it in cur_items if "hooks.json" in it["path"]
               or "settings.json" in it["path"]], str([it["path"] for it in cur_items]))
    gen.apply(cur, cur_items)
    cur_md = open(os.path.join(cur, "AGENTS.md"), encoding="utf-8").read()
    check("and its AGENTS.md says so in the first sentence of the write-policy paragraph",
          "No target here enforces anything" in cur_md, cur_md[-600:])


def _p3b_migration(check, tmp):
    """The v1 -> v2 hook migration, which is EVERY existing user's path through this release.

    A v1 harness carries `python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"` with no --protocol. The
    guard now exits 3 on that command -- and only exit 2 blocks, so until the registration is
    rewritten the harness is fail-open with a matcher that still looks correct. That is Trap
    2, and this is the test that the rewrite actually happens rather than a second entry being
    appended beside the dead one.

    The entry here is deliberately UNTAGGED as well as stale, because that is the harder half:
    the tag is how mir normally re-finds its own entry, and an entry written before the tag
    existed can only be recognised by the guard it runs.
    """
    repo = _repo(tmp, "v1harness")
    _write(os.path.join(repo, ".claude", "settings.json"), json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]},
        {"matcher": "Write|Edit", "hooks": [
            {"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"'}]},
    ]}}))
    gen.apply(repo, _plan(repo))
    pre = json.loads(_read(os.path.join(repo, ".claude", "settings.json")))["hooks"]["PreToolUse"]
    mine = [e for e in pre if "guard.py" in json.dumps(e)]
    check("migration: the untagged v1 entry is REWRITTEN, not joined by a second one -- two "
          "entries would mean one hook that works and one that exits 3 on every call",
          len(mine) == 1, json.dumps(pre))
    check("migration: the rewritten command carries --protocol claude",
          mine[0]["hooks"][0]["command"].endswith("--protocol claude"), json.dumps(mine))
    check("migration: and the stale matcher is widened in the same pass",
          mine[0]["matcher"] == gen.MATCHER, json.dumps(mine))
    check("migration: the user's own unrelated hook survives untouched",
          any(h.get("command") == "echo user" for e in pre for h in e.get("hooks", [])),
          json.dumps(pre))


def _p3b_skill_dirs(check, tmp):
    import targets as tp

    checkout = os.path.join(tmp, "checkout")
    os.makedirs(os.path.join(checkout, "skills", "mir-backend-go"))
    os.makedirs(os.path.join(checkout, "skills", "mir-backend-rust"))
    repo = _repo(tmp, "skills-repo")

    dirs = gen.skill_dirs_for([tp.BY_NAME["claude"], tp.BY_NAME["antigravity"]])
    check("both hosts with a project skill directory are covered",
          set(dirs) == {os.path.join(".claude", "skills"), os.path.join(".agents", "skills")},
          str(dirs))
    check("a target with no project skill directory contributes none rather than a guessed "
          "path", gen.skill_dirs_for([tp.BY_NAME["codex"]]) == list(gen.DEFAULT_SKILL_DIRS))

    linked = gen.install_project_skills(repo, ["mir-backend-go"], checkout, dest_dirs=dirs)
    check("install writes into every target's skill directory",
          all(os.path.islink(os.path.join(repo, d, "mir-backend-go")) for d in dirs))
    check("and reports SLUGS, deduplicated: one skill in two hosts is one skill made "
          "available, not two", linked == ["mir-backend-go"], str(linked))

    # THE POINT of dest_dirs. Prune matters more than install here: without it a repo
    # accumulates a dead stack's skills in .agents/skills forever, loading gates for a
    # framework it no longer uses, in a directory nobody thinks to look in.
    gen.install_project_skills(repo, ["mir-backend-rust"], checkout, dest_dirs=dirs)
    removed = gen.prune_project_skills(repo, ["mir-backend-rust"], checkout, dest_dirs=dirs)
    check("prune reaches .agents/skills too", removed == ["mir-backend-go"], str(removed))
    check("and the dead stack's skill is gone from BOTH directories",
          not any(os.path.lexists(os.path.join(repo, d, "mir-backend-go")) for d in dirs))
    check("while the live one survives in both",
          all(os.path.islink(os.path.join(repo, d, "mir-backend-rust")) for d in dirs))

    # MUTATION: prune only the default directory, which is the shape this had before.
    gen.install_project_skills(repo, ["mir-backend-go"], checkout, dest_dirs=dirs)
    gen.prune_project_skills(repo, ["mir-backend-rust"], checkout)
    check("MUTATION: a prune that ignores dest_dirs leaves the dead skill in .agents/skills, "
          "which is the accumulation this argument exists to stop",
          os.path.lexists(os.path.join(repo, ".agents", "skills", "mir-backend-go")))


def _p3b_cli(check):
    import targets as tp

    src = open(os.path.join(HERE, "cli.py"), encoding="utf-8").read()
    check("the CLI exposes --target", '"--target"' in src)
    check("and it passes the resolved targets into plan(), so the manifest records them",
          "gen.plan(repo, res[\"skills\"], answers, stamp, targets)" in src)
    check("its default is the single implicit target every v1 harness had",
          tp.DEFAULT == "claude")
    check("--target all resolves to every known target, in registry order",
          [t.name for t in tp.resolve("all")] == list(tp.NAMES))
