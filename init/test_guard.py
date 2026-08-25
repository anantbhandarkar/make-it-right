#!/usr/bin/env python3
"""Enforcement-core tests: guard.py and schema.py. Loaded and tallied by test_init.py.

Three defects are pinned here, each by the CLASS it belongs to rather than by the one
command that happened to reproduce it:

  F2  a symlink inside an allowed root aliased a denied file, because the guard compared
      the name the tool typed instead of the bytes `open(path, "w")` would land on.
  F8  the Bash write parser was four regexes over the whole command string, so a second
      `tee` operand and a `cp` that was not the last command on the line both vanished.
  D3  the guard never read `mir_manifest_version`, so a manifest it cannot interpret was
      enforced as if it could.

`check` is injected by test_init.py; this module imports no test framework and has no
__main__ block, because it is one suite inside one tally.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import guard  # noqa: E402
import schema  # noqa: E402

GUARD_SRC = os.path.join(HERE, "guard.py")


# -- fixtures ---------------------------------------------------------------

def _mk_repo(tmp, roots, name="repo", version=None):
    """A repo built the way test_init.py builds them: under tempfile, so the root is a
    /var path whose realpath is /private/var. That difference is the whole trap in F2."""
    repo = os.path.join(tmp, name)
    os.makedirs(os.path.join(repo, ".mir"), exist_ok=True)
    m = schema.project_manifest(name, allowed_write_roots=roots)
    if version is not None:
        m["mir_manifest_version"] = version
    with open(os.path.join(repo, ".mir", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    shutil.copy(GUARD_SRC, os.path.join(repo, ".mir", "guard.py"))
    return repo


def _run(repo, tool_name, tool_input):
    """Drive the real guard.main() in-process with the real hook event.

    In-process because the tables below fire a few hundred events and a subprocess each
    would turn one suite into a coffee break; main() rather than decide() because the
    wiring between the two is where a fix like this actually gets dropped.
    """
    ev = {"tool_name": tool_name, "tool_input": tool_input, "cwd": repo}
    old_in, old_err = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = io.StringIO(json.dumps(ev)), io.StringIO()
    try:
        rc = guard.main()
        return rc, sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stderr = old_in, old_err


def _write(repo, path):
    return _run(repo, "Write", {"file_path": path, "content": "x"})


def _bash(repo, cmd):
    return _run(repo, "Bash", {"command": cmd})


def _subproc(guard_path, repo, path):
    """Out-of-process, for the mutant: a modified guard is a different FILE, not a monkeypatch."""
    ev = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "cwd": repo}
    p = subprocess.run([sys.executable, guard_path], input=json.dumps(ev),
                       text=True, capture_output=True, cwd=repo)
    return p.returncode, p.stderr


# -- F2: symlink escape -----------------------------------------------------

def _alias_repo(tmp, name="repo"):
    """A repo whose allowed `src` holds symlinks pointing at things src may not write."""
    repo = _mk_repo(tmp, ["src", "tests"], name=name)
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    open(os.path.join(repo, ".env"), "w").write("SECRET=1")
    os.symlink("../.mir/guard.py", os.path.join(repo, "src", "alias"))
    os.symlink("../.env", os.path.join(repo, "src", "envlink"))
    os.symlink(tempfile.gettempdir(), os.path.join(repo, "src", "outlink"))
    return repo


def _f2(check, tmp):
    repo = _alias_repo(tmp)

    rc, err = _write(repo, "src/alias")
    check("F2 alias: a symlink in an allowed root to the guard is BLOCKED",
          rc == guard.BLOCK, f"rc={rc} err={err.strip()[-160:]}")
    check("F2 alias: the block names a denied path, not deny-by-default",
          "denied" in err, err.strip()[-160:])

    rc, err = _write(repo, "src/envlink")
    check("F2 alias: a symlink in an allowed root to .env is BLOCKED",
          rc == guard.BLOCK, f"rc={rc} err={err.strip()[-160:]}")

    rc, err = _write(repo, os.path.join("src", "outlink", "escape.txt"))
    check("F2 alias: a symlink out of the repo is BLOCKED",
          rc == guard.BLOCK, f"rc={rc} err={err.strip()[-160:]}")

    # THE CONTROL. tempfile hands back /var/... whose realpath is /private/var/..., so a
    # canonical target measured against an abspath root is under nothing and the guard
    # blocks everything while looking like a security win. If this check fails, the fix is
    # worse than the bug.
    check("F2 control: tempfile repo root canonicalises away from abspath (the trap is real)",
          os.path.realpath(repo) != os.path.abspath(repo),
          "no /private prefix on this platform; the control still has to pass")
    rc, err = _write(repo, "src/app.ts")
    check("F2 control: an ordinary src/app.ts write in a tempfile repo is still ALLOWED",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-200:]}")
    rc, err = _write(repo, os.path.join(repo, "tests", "t.spec.ts"))
    check("F2 control: an ABSOLUTE /var-spelled target under an allowed root is ALLOWED",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-200:]}")

    # Same attack through Bash, because the parser and the resolver are separate holes.
    rc, err = _bash(repo, "echo pwned > src/alias")
    check("F2 alias: the same alias via a Bash redirect is BLOCKED",
          rc == guard.BLOCK, f"rc={rc} err={err.strip()[-160:]}")


def _f2_mutation(check, tmp):
    """Patch the canonicaliser back to normpath and prove the alias attack LEAKS.

    A regression test that passes against the broken implementation is decoration. This is
    the same shape as test_init.py's is_glob mutant.
    """
    repo = _alias_repo(tmp, name="mutantrepo")
    src = open(GUARD_SRC, encoding="utf-8").read()
    old = "    return os.path.realpath(os.path.join(repo_root, os.path.expanduser(p)))"
    mutated = src.replace(old, old.replace("realpath", "normpath"))
    check("F2 mutant: the normpath patch actually applied (a no-op mutant proves nothing)",
          mutated != src and mutated.count("os.path.normpath(os.path.join(repo_root") == 1)

    mutant = os.path.join(tmp, "normpath-guard.py")
    open(mutant, "w", encoding="utf-8").write(mutated)

    rc, _ = _subproc(GUARD_SRC, repo, "src/alias")
    check("F2 mutant: the real guard blocks the alias out of process too", rc == guard.BLOCK,
          f"rc={rc}")
    rc, err = _subproc(mutant, repo, "src/alias")
    check("F2 mutant: reverted to normpath, the alias attack LEAKS (exit 0)",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-160:]}")
    rc, _ = _subproc(mutant, repo, "src/app.ts")
    check("F2 mutant: the mutant is otherwise alive (src/app.ts still allowed)",
          rc == guard.ALLOW, f"rc={rc}")


def _v1_decide(target_abs, policy, repo_root):
    """The verdict the guard reached BEFORE canonicalisation: normpath on both sides.

    Written out rather than imported so the property below compares against a fixed
    reference; if guard.py drifts, the reference does not drift with it.
    """
    if _v1_denied(target_abs, policy, repo_root):
        return guard.BLOCK
    roots = [guard.resolve(r, repo_root) for r in policy.get("allowed_write_roots", [])]
    if not roots:
        return guard.BLOCK
    return guard.ALLOW if any(guard._is_under(target_abs, r) for r in roots) else guard.BLOCK


def _v1_denied(target_abs, policy, repo_root):
    for entry in policy.get("denied_paths", []):
        if guard.is_glob(entry):
            if guard.matches_glob(target_abs, guard.glob_patterns(entry, repo_root)):
                return True
            continue
        if guard._is_under(target_abs, guard.resolve(entry, repo_root)):
            return True
    return False


_CORPUS = [
    "src/app.ts", "src/deep/nested/x.ts", "tests/t.spec.ts", "README.md",
    ".git", ".git/config", ".git/hooks/pre-commit", ".mir", ".mir/manifest.json",
    ".mir/guard.py", ".env", ".env.local", ".env.production", ".envrc",
    "src/.env", "a/b/.env.development", "src/environment.ts", "src/env.ts",
    "env/config.ts", "../outside.txt", "src/../.git/config", "src/../../escape.txt",
    "~/.ssh/id_rsa", "~/.aws/credentials", "~/.claude/settings.json",
    "/tmp/absolute-escape.txt", "src/alias", "src/envlink", "src/outlink/x",
]


def _f2_property(check, tmp):
    """Non-weakening, asserted rather than asserted-in-a-comment."""
    repo = _alias_repo(tmp, name="propertyrepo")
    root = guard.canonical(repo, os.getcwd())
    policy = json.load(open(os.path.join(repo, ".mir", "manifest.json")))["policy"]

    deny_regressions, verdict_regressions = [], []
    for p in _CORPUS:
        lex, can = guard.resolve(p, root), guard.canonical(p, root)
        if _v1_denied(lex, policy, root) and guard.is_denied(lex, can, policy, root) is None:
            deny_regressions.append(p)
        if (_v1_decide(lex, policy, root) == guard.BLOCK
                and guard.decide(lex, can, policy, root)[0] != guard.BLOCK):
            verdict_regressions.append(p)

    check("F2 property: every path the v1 deny check matched is still denied "
          f"({len(_CORPUS)} paths)", not deny_regressions, str(deny_regressions))
    check("F2 property: no v1 BLOCK became an ALLOW (no symlinked allowed root here)",
          not verdict_regressions, str(verdict_regressions))

    # The one flip this change is FOR, stated out loud rather than hidden: when an allowed
    # root is itself a symlink, the resolved spelling of a file under it now compares equal.
    # v1 blocked it, which is why symlinked source trees did not work.
    real = os.path.join(tmp, "elsewhere")
    os.makedirs(real, exist_ok=True)
    linked = _mk_repo(tmp, ["src"], name="linkedroot")
    os.symlink(real, os.path.join(linked, "src"))
    rc, err = _write(linked, os.path.join(real, "a.ts"))
    check("F2 property: the intended flip -- a file under a SYMLINKED allowed root, named "
          "by its resolved path, is now allowed", rc == guard.ALLOW,
          f"rc={rc} err={err.strip()[-160:]}")


# -- F8: the Bash write parser ----------------------------------------------

_SEPS = [";", "&&", "||", "|", "\n"]
_POSITIONS = ("first", "middle", "last")


def _verb_payloads(target):
    """One command per write verb, all naming `target` as their destination."""
    return {
        ">": "echo x > %s" % target,
        ">>": "echo x >> %s" % target,
        "tee": "tee allowed.txt %s" % target,          # 2nd operand: the lost-target bug
        "dd of=": "dd if=safe of=%s" % target,
        "cp": "cp safe %s" % target,
        "mv": "mv safe %s" % target,
        "install": "install -m 644 safe %s" % target,
    }


def _compose(payload, sep, position):
    others = ["echo alpha", "echo beta"]
    if position == "first":
        parts = [payload] + others
    elif position == "middle":
        parts = [others[0], payload, others[1]]
    else:
        parts = others + [payload]
    joiner = "\n" if sep == "\n" else " %s " % sep
    return joiner.join(parts)


def _f8_table(check, tmp):
    # allowed_write_roots=["."] on purpose. Under ["src"] the stray tokens these commands
    # carry (`safe`, `alpha`, `allowed.txt`) would block as deny-by-default, and the
    # negative half of the table would pass without proving anything about the parser.
    repo = _mk_repo(tmp, ["."], name="bashrepo")
    denied, allowed = ".git/config", "src/ok.txt"

    missed, false_blocked = [], []
    cells = 0
    for target, bucket, want in ((denied, missed, guard.BLOCK),
                                 (allowed, false_blocked, guard.ALLOW)):
        for verb, payload in _verb_payloads(target).items():
            for sep in _SEPS:
                for pos in _POSITIONS:
                    cmd = _compose(payload, sep, pos)
                    cells += 1
                    rc, err = _bash(repo, cmd)
                    if rc != want:
                        bucket.append((verb, "newline" if sep == "\n" else sep, pos,
                                       rc, err.strip()[-80:]))

    check(f"F8 table: every {{sep}}x{{verb}}x{{position}} cell BLOCKS the denied target "
          f"({cells // 2} cells)", not missed, f"{len(missed)} missed, e.g. {missed[:4]}")
    check(f"F8 table: the same {cells // 2} cells ALLOW an ordinary target "
          "(a guard that blocks everything is not a fix)",
          not false_blocked, f"{len(false_blocked)} over-blocked, e.g. {false_blocked[:4]}")

    # The two commands that were reproduced live, kept as named instances of the class.
    rc, _ = _bash(repo, "tee allowed.txt .mir/guard.py")
    check("F8 instance: `tee allowed.txt .mir/guard.py` blocks (2nd operand no longer lost)",
          rc == guard.BLOCK, f"rc={rc}")
    rc, _ = _bash(repo, "cp safe .git/config; echo done")
    check("F8 instance: `cp safe .git/config; echo done` blocks (end-anchor no longer wins)",
          rc == guard.BLOCK, f"rc={rc}")

    # Union, not replacement: whatever the old regexes caught must still be caught.
    old_catches = ["echo K=v > .env", "echo x >> .git/config", "tee .git/config",
                   "dd if=safe of=.git/config", "cp safe .git/config"]
    still = [c for c in old_catches if _bash(repo, c)[0] != guard.BLOCK]
    check("F8 union: every command the whole-string regexes caught before still blocks",
          not still, str(still))

    # Quoting and descriptor redirects, the two ways a naive splitter corrupts a command.
    rc, _ = _bash(repo, 'echo "a;b" > .git/config')
    check("F8 parser: an operator inside quotes does not lose the real target",
          rc == guard.BLOCK, f"rc={rc}")
    # Narrow roots on purpose: under ["."] an invented target like `1` or `status` lands
    # inside the repo and is allowed anyway, so over-blocking would not show.
    narrow = _mk_repo(tmp, ["src"], name="narrowrepo")
    quiet = ["echo hi 2>&1", "cat log >&2", "npm run build && git status",
             "grep -r TODO . | wc -l", "echo x > src/ok.txt"]
    noisy = [c for c in quiet if _bash(narrow, c)[0] != guard.ALLOW]
    check("F8 parser: descriptor redirects and non-writing commands invent no target "
          "(checked under narrow allowed roots, where an invented one would block)",
          not noisy, str(noisy))
    rc, _ = _bash(repo, "cat notes | sudo /usr/bin/tee -a .git/config")
    check("F8 parser: an absolute verb behind `sudo` is still read as tee",
          rc == guard.BLOCK, f"rc={rc}")


# -- D3: manifest version negotiation ---------------------------------------

def _d3(check, tmp):
    check("D3 drift: GUARD_MANIFEST_VERSION tracks schema.MANIFEST_VERSION",
          guard.GUARD_MANIFEST_VERSION == schema.MANIFEST_VERSION,
          f"guard={guard.GUARD_MANIFEST_VERSION} schema={schema.MANIFEST_VERSION}")

    matched = _mk_repo(tmp, ["src"], name="v1repo")
    rc, err = _write(matched, "src/app.ts")
    check("D3 match: a current manifest says nothing about versions",
          rc == guard.ALLOW and "manifest version" not in err, f"rc={rc} err={err.strip()}")

    future = _mk_repo(tmp, ["src"], name="v2repo", version=guard.GUARD_MANIFEST_VERSION + 1)
    rc1, err1 = _write(future, ".git/config")
    rc2, err2 = _write(future, "src/app.ts")
    check("D3 mismatch: a newer manifest fails OPEN, it does not brick the repo",
          rc1 == guard.ALLOW and rc2 == guard.ALLOW, f"rc={rc1},{rc2}")
    check("D3 mismatch: the reason is on stderr, naming both versions",
          "manifest version" in err1 and "2" in err1 and "NOT ENFORCING" in err1,
          err1.strip())
    check("D3 mismatch: it complains on EVERY invocation, not once",
          "manifest version" in err2, err2.strip())
    check("D3 mismatch: no invisible fallback denylist -- a denial the manifest does not "
          "describe is a denial the probe cannot see", rc1 == guard.ALLOW, f"rc={rc1}")

    bad = _mk_repo(tmp, ["src"], name="badversion")
    mpath = os.path.join(bad, ".mir", "manifest.json")
    m = json.load(open(mpath))
    del m["mir_manifest_version"]
    json.dump(m, open(mpath, "w"))
    rc, err = _write(bad, ".git/config")
    check("D3 missing: a manifest with no version field is treated as a mismatch",
          rc == guard.ALLOW and "manifest version" in err, f"rc={rc} err={err.strip()}")


# -- entry point ------------------------------------------------------------

def run(check):
    with tempfile.TemporaryDirectory() as tmp:
        _f2(check, tmp)
        _f2_mutation(check, tmp)
        _f2_property(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _f8_table(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _d3(check, tmp)
