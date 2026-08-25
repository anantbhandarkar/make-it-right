#!/usr/bin/env python3
"""Enforcement-core tests: guard.py and schema.py. Loaded and tallied by test_init.py.

Defects are pinned here by the CLASS they belong to rather than by the one command that
happened to reproduce them:

  F2  a symlink inside an allowed root aliased a denied file, because the guard compared
      the name the tool typed instead of the bytes `open(path, "w")` would land on.
  F8  the Bash write parser was four regexes over the whole command string, so a second
      `tee` operand and a `cp` that was not the last command on the line both vanished.
  F8b the parser that replaced them approximated two pieces of shell grammar it did not
      implement, and each approximation was a live evasion:
        * every `&` split a command, so `>&file` (bash's "both streams to file") was torn
          into a redirection with no filename and a command named after the file; and
        * cp/mv/install named only their LAST operand, so `cp .env.local allowed/` reported
          `allowed/` -- under a denied DIRECTORY that still blocks by prefix, but the write
          it actually performs is `allowed/.env.local`, and a denied GLOB is the only shape
          a secrets rule has.
  D3  the guard never read `mir_manifest_version`, so a manifest it cannot interpret was
      enforced as if it could.
  U1  `PATH_FIELD_TOOLS` carries `Update`, a tool nobody could find. The decision to keep it
      is pinned as the asymmetry that justifies it, not as a fact about Claude Code.

`check` is injected by test_init.py; this module imports no test framework and has no
__main__ block, because it is one suite inside one tally.
"""

from __future__ import annotations

import io
import json
import os
import re
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

def _mk_repo(tmp, roots, name="repo", version=None, extra_denied=None):
    """A repo built the way test_init.py builds them: under tempfile, so the root is a
    /var path whose realpath is /private/var. That difference is the whole trap in F2."""
    repo = os.path.join(tmp, name)
    os.makedirs(os.path.join(repo, ".mir"), exist_ok=True)
    m = schema.project_manifest(name, allowed_write_roots=roots, extra_denied=extra_denied)
    if version is not None:
        m["mir_manifest_version"] = version
    with open(os.path.join(repo, ".mir", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    shutil.copy(GUARD_SRC, os.path.join(repo, ".mir", "guard.py"))
    return repo


def _run(repo, tool_name, tool_input, protocol="claude"):
    """Drive the real guard.main() in-process with the real hook event.

    In-process because the tables below fire a few hundred events and a subprocess each
    would turn one suite into a coffee break; main() rather than decide() because the
    wiring between the two is where a fix like this actually gets dropped.

    `argv` is passed EXPLICITLY rather than left to sys.argv. main() now requires
    --protocol, and an in-process call that inherited the test runner's argv would be a
    guard told nothing about its host: exit 3 on every row, and a suite that failed for a
    reason of its own making rather than for the property it names.
    """
    ev = {"tool_name": tool_name, "tool_input": tool_input, "cwd": repo}
    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = (io.StringIO(json.dumps(ev)), io.StringIO(),
                                         io.StringIO())
    try:
        rc = guard.main(["--protocol", protocol])
        return rc, sys.stderr.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err


def _run_stdout(repo, event, protocol):
    """(exit code, stdout) for one raw event. The stdout hosts answer on stdout ONLY, so a
    helper that returned stderr would be reading the channel the host ignores."""
    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = (io.StringIO(json.dumps(event)), io.StringIO(),
                                         io.StringIO())
    try:
        rc = guard.main(["--protocol", protocol])
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err


def _write(repo, path):
    return _run(repo, "Write", {"file_path": path, "content": "x"})


def _bash(repo, cmd):
    return _run(repo, "Bash", {"command": cmd})


def _subproc(guard_path, repo, path):
    """Out-of-process, for the mutant: a modified guard is a different FILE, not a monkeypatch."""
    ev = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "cwd": repo}
    p = subprocess.run([sys.executable, guard_path, "--protocol", "claude"],
                       input=json.dumps(ev), text=True, capture_output=True, cwd=repo)
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


def _diverging_root(tmp):
    """A directory whose realpath and abspath differ, by construction, on every platform.

    macOS gets this divergence for free: tempfile.mkdtemp() hands back a /var/... path
    whose realpath is /private/var/.... That accident is exactly the F2 trap -- canonicalise
    the write TARGET but leave find_repo_root on abspath, and every write resolves outside
    its own root while looking like a security win.

    Linux's /tmp is ordinarily already canonical, so the same accident does not happen
    there, and a control that checks for a macOS-specific rewrite is checking a platform
    fact, not a property of the guard. Rather than make the control conditional on that
    fact (and silently trust it on the platform where it doesn't hold), build the
    divergence directly with a symlink layer: `link` and `real` are two different names for
    the same directory, so abspath(path-through-link) and realpath(path-through-link)
    necessarily disagree, on macOS and Linux alike. The guard cannot tell -- and must not
    care -- whether the divergence came from the OS or from the fixture.
    """
    real = os.path.join(tmp, "canon-real")
    os.makedirs(real, exist_ok=True)
    link = os.path.join(tmp, "canon-link")
    os.symlink(real, link)
    return link


def _f2(check, tmp):
    repo = _alias_repo(_diverging_root(tmp))

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

    # THE CONTROL. `repo` is spelled through the `canon-link` symlink built by
    # _diverging_root, so its realpath and abspath disagree on every platform, not just
    # where a tempdir happens to canonicalise away from abspath. If this ever comes back
    # equal, the fixture stopped constructing the trap and everything below is vacuous --
    # the guard would look like it passed while never having been tested against a
    # canonicalising root at all.
    check("F2 control: repo root's realpath diverges from its abspath (the trap is real)",
          os.path.realpath(repo) != os.path.abspath(repo),
          f"repo={repo!r} realpath={os.path.realpath(repo)!r} -- divergence should be "
          "guaranteed by the canon-link symlink regardless of platform")
    rc, err = _write(repo, "src/app.ts")
    check("F2 control: an ordinary src/app.ts write in a canonicalising-root repo is still "
          "ALLOWED", rc == guard.ALLOW, f"rc={rc} err={err.strip()[-200:]}")
    rc, err = _write(repo, os.path.join(repo, "tests", "t.spec.ts"))
    check("F2 control: an ABSOLUTE pre-canonicalisation-spelled target under an allowed "
          "root is ALLOWED", rc == guard.ALLOW, f"rc={rc} err={err.strip()[-200:]}")

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

# `&` joins the list as a separator in its own right: a background `&` ends a command exactly
# as `;` does, and it is the character the splitter now has to read two ways.
_SEPS = [";", "&&", "||", "|", "&", "\n"]
_POSITIONS = ("first", "middle", "last")


def _verb_payloads(target):
    """One command per write verb, all naming `target` as their destination."""
    return {
        ">": "echo x > %s" % target,
        ">>": "echo x >> %s" % target,
        "&>": "echo x &> %s" % target,                 # both streams to a file
        ">&": "echo x >& %s" % target,                 # the same thing, spelled the csh way
        "tee": "tee allowed.txt %s" % target,          # 2nd operand: the lost-target bug
        "dd of=": "dd if=safe of=%s" % target,
        "cp": "cp safe %s" % target,
        "mv": "mv safe %s" % target,
        "install": "install -m 644 safe %s" % target,
        # `-t` puts the destination FIRST, so a parser that reads the last operand reports a
        # source. The write here is `<target>/safe`, which is under `<target>` either way.
        "cp -t": "cp -t %s safe" % target,
        # Three operands: the last one is a directory by grammar, not by guess.
        "cp multi": "cp safe safe2 %s/" % target,
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


# -- F8b: the two pieces of shell grammar the parser was approximating ------------------

def _f8b_ampersand(check, tmp):
    """`&` is a separator, half of a redirection operator, or half of `&&`, by position."""
    repo = _mk_repo(tmp, ["."], name="amprepo")

    # The evasion. `>&word` sends BOTH streams to word when word is not a descriptor, and a
    # splitter that cut at every `&` turned it into a redirection with no filename followed
    # by a command named after the file -- so nothing at all was reported.
    leaks = ["echo x >&.git/config", "echo x >& .git/config", "cat a 2>&1 >&.git/config",
             "echo x &>.git/config", "echo x &>>.git/config", "echo x 2>&1 1>.git/config"]
    open_ = [c for c in leaks if _bash(repo, c)[0] != guard.BLOCK]
    check("F8b amp: every `>&file` / `&>file` spelling names its file and BLOCKS",
          not open_, str(open_))

    # A background `&` ends a command; it must not swallow the next one, and the next one is
    # where the write is.
    rc, _ = _bash(repo, "sleep 1 & tee .git/config")
    check("F8b amp: a background `&` ends a command without eating the next one's target",
          rc == guard.BLOCK, f"rc={rc}")
    rc, _ = _bash(repo, "echo alpha && echo x > .git/config")
    check("F8b amp: `&&` still splits, so the second command is still parsed",
          rc == guard.BLOCK, f"rc={rc}")

    # THE NEGATIVE HALF, under narrow roots so an invented target would land outside `src`
    # and block. A descriptor is not a file: `2>&1` opens nothing called `1`.
    narrow = _mk_repo(tmp, ["src"], name="ampnarrow")
    quiet = ["echo hi 2>&1", "cat log >&2", "cmd 1>&2", "make 2>&1 | grep -i error",
             "sleep 1 &", "npm test & npm run lint", "echo a && echo b", "echo a || echo b",
             "exec 3>&1", "cmd >&-", "echo x > src/ok.txt", "cp a src/b"]
    noisy = [c for c in quiet if _bash(narrow, c)[0] != guard.ALLOW]
    check("F8b amp: descriptor dups, background jobs and `&&` invent no file target "
          "(narrow roots, where an invented one would block)", not noisy, str(noisy))

    check("F8b amp: `2>&1` survives tokenisation as one word, not two commands",
          guard.split_segments("echo hi 2>&1") == ["echo hi 2>&1"],
          str(guard.split_segments("echo hi 2>&1")))
    check("F8b amp: `a && b` is still two commands",
          len(guard.split_segments("a && b")) == 2, str(guard.split_segments("a && b")))
    check("F8b amp: a trailing background `&` is still a terminator",
          guard.split_segments("sleep 1 & wait") == ["sleep 1 ", " wait"],
          str(guard.split_segments("sleep 1 & wait")))


def _f8b_copy(check, tmp):
    """cp/mv/install write one path PER SOURCE, and `-t` moves the destination to the front."""
    # `build/*.js` is the sharp case on purpose: neither `build` nor `build/` nor the source
    # name is denied, so the ONLY way these block is by naming the per-source destination.
    repo = _mk_repo(tmp, ["."], name="copyrepo", extra_denied=["build/*.js"])

    blocked = [
        "cp app.js vendor.js build/",        # 3 operands: last is a directory by grammar
        "cp -t build app.js",                # -t inverts the order
        "cp --target-directory=build app.js",
        "mv app.js vendor.js build/",
        "install -m 644 app.js build/",      # -m swallows 644; app.js is the only source
        "cp -tbuild app.js",                 # value attached to the short option
        "cp -rt build app.js",               # ... and inside a cluster
    ]
    open_ = [c for c in blocked if _bash(repo, c)[0] != guard.BLOCK]
    check("F8b copy: a copy whose per-source destination matches a denied glob BLOCKS",
          not open_, str(open_))

    # The secrets shape, on the baseline policy rather than a test-only entry: `**/.env*`
    # exists to catch a secrets file at any depth, and a copy INTO an allowed directory is
    # how one gets there.
    rc, _ = _bash(repo, "cp .env.local src/")
    check("F8b copy: `cp .env.local src/` blocks -- the write is src/.env.local, not src/",
          rc == guard.BLOCK, f"rc={rc}")

    # THE NEGATIVE HALF. Enumerating destinations must not turn every copy into a block.
    fine = ["cp app.css build/", "cp a b src/", "cp a b c src/", "cp -t src a b",
            "install -m 644 app.css build/", "cp README.md docs/README.md",
            "mv old.css new.css", "cp -r src/ dist/"]
    noisy = [c for c in fine if _bash(repo, c)[0] != guard.ALLOW]
    check("F8b copy: ordinary copies whose destinations are allowed still pass",
          not noisy, str(noisy))

    # Operand parsing, asserted directly: an option's ARGUMENT is not a source, or every
    # `install -m 644 a dir/` invents a write to dir/644.
    d = guard.copy_dests(["-m", "644", "a", "dir/"])
    check("F8b copy: `-m 644` is an option argument, not a source",
          "dir/a" in d and "dir/644" not in d, str(d))
    d = guard.copy_dests(["a", "b", "dir/"])
    check("F8b copy: a multi-source copy names one destination per source",
          {"dir/a", "dir/b"} <= set(d), str(d))
    d = guard.copy_dests(["a", "b"])
    check("F8b copy: a two-operand copy still names exactly its destination",
          d == ["b"], str(d))
    d = guard.copy_dests(["--help"])
    check("F8b copy: a copy with no operands names no destination", d == [], str(d))
    d = guard.copy_dests(["--", "-weird.txt", "dir/"])
    check("F8b copy: `--` ends the options, so a dash-named file is a source",
          "dir/-weird.txt" in d, str(d))
    d = guard.copy_dests(["-t", "dir", "a", "b"])
    check("F8b copy: with -t, the last operand is reported too (the pre-fix floor is kept, "
          "because un-blocking is the one direction this file may not move)",
          {"dir", "dir/a", "dir/b", "b"} <= set(d), str(d))


# -- F8b: the Bash parser may only gain targets, never lose them -----------------------
#
# The pre-fix parser, written out here rather than imported, for the same reason _v1_decide
# is: a reference that drifts with guard.py proves nothing. Every target this found must
# still be found, or a command that blocks today stops blocking.

def _prev_split_segments(cmd):
    segments, cur, quote, escaped = [], [], None, False
    for ch in cmd:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            cur.append(ch)
            escaped = True
        elif quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch in ";|&\n":
            segments.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segments.append("".join(cur))
    return [s for s in segments if s.strip()]


def _prev_segment_targets(segment):
    toks = guard._tokens(segment)
    out, operands, i = [], [], 0
    while i < len(toks):
        m = guard._REDIR_TOKEN.match(toks[i])
        if m:
            dest = m.group(1)
            if not dest:
                i += 1
                dest = toks[i] if i < len(toks) else ""
            if dest and not dest.startswith("&"):
                out.append(dest)
            i += 1
            continue
        operands.append(toks[i])
        i += 1
    while operands and (guard._ASSIGNMENT.match(operands[0])
                        or os.path.basename(operands[0]) in guard._COMMAND_PREFIXES):
        operands.pop(0)
    if not operands:
        return out
    verb, args = os.path.basename(operands[0]), operands[1:]
    if verb == "tee":
        out += [a for a in args if not a.startswith("-")]
    elif verb == "dd":
        out += [a[3:] for a in args if a.startswith("of=") and len(a) > 3]
    elif verb in ("cp", "mv", "install"):
        positional = [a for a in args if not a.startswith("-")]
        if len(positional) >= 2:
            out.append(positional[-1])
    return out


def _normalise(found):
    seen = []
    for t in found:
        t = t.strip().strip("'\"")
        if t and t not in seen and not t.startswith("-"):
            seen.append(t)
    return set(seen)


def _prev_bash_targets(cmd):
    found = []
    for pat in guard._SHELL_WRITE_PATTERNS:
        found += [m.group(1) for m in pat.finditer(cmd)]
    for seg in _prev_split_segments(cmd):
        found += _prev_segment_targets(seg)
    return _normalise(found)


_CMD_CORPUS = [
    "echo x > .git/config", "echo x >> .env", "tee allowed.txt .mir/guard.py",
    "dd if=safe of=.git/config", "cp safe .git/config", "mv safe .git/config",
    "install -m 644 safe .git/config", "cp safe .git/config; echo done",
    "cp a b dir/", "cp -t dir a b", "cp --target-directory=dir a b", "cp -tdir a b",
    "cp -rt dir a b", "install -m 644 a dir/", "cp a b", "cp --help", "mv a b c d dir",
    "echo hi 2>&1", "cat log >&2", "echo x >&file", "echo x >& file", "echo x &>file",
    "echo x &>>file", "sleep 1 &", "a & b", "a && b", "a || b", "a | b",
    "npm run build && git status", 'echo "a;b" > .git/config', "cat n | sudo tee -a x",
    "FOO=bar tee out.txt", "exec 3>&1", "cmd >&-", "echo a &&>f", "grep -r TODO . | wc -l",
    "cp -t src .git/config", "echo x > src/ok.txt", "cp .env.local src/",
]


def _f8b_property(check, tmp):
    lost = []
    for cmd in _CMD_CORPUS:
        prev = _prev_bash_targets(cmd)
        now = _normalise(guard.targets_from_event("Bash", {"command": cmd})[0])
        missing = prev - now
        if missing:
            lost.append((cmd, sorted(missing)))
    check(f"F8b property: every target the pre-fix Bash parser found is still found "
          f"({len(_CMD_CORPUS)} commands)", not lost, str(lost[:4]))

    gained = sum(1 for c in _CMD_CORPUS
                 if _normalise(guard.targets_from_event("Bash", {"command": c})[0])
                 - _prev_bash_targets(c))
    check("F8b property: the corpus actually exercises the change (some commands now name "
          "MORE targets than they did)", gained >= 5, f"{gained} commands gained a target")


# -- F8b: mutation tests, one per fix ---------------------------------------------------

def _mutant(tmp, name, old, new, expect_count=1):
    """Write a copy of guard.py with one fix reverted. Returns (path, applied?)."""
    src = open(GUARD_SRC, encoding="utf-8").read()
    mutated = src.replace(old, new)
    path = os.path.join(tmp, name)
    open(path, "w", encoding="utf-8").write(mutated)
    return path, (mutated != src and src.count(old) == expect_count)


def _bash_subproc(guard_path, repo, cmd):
    ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": repo}
    p = subprocess.run([sys.executable, guard_path, "--protocol", "claude"],
                       input=json.dumps(ev), text=True, capture_output=True, cwd=repo)
    return p.returncode, p.stderr


def _f8b_mutation(check, tmp):
    """Revert each fix in a copied module and prove the evasion comes back.

    A regression test that passes against the broken implementation is decoration; this is
    the same shape as the F2 normpath mutant above.
    """
    repo = _mk_repo(tmp, ["."], name="mutantbash", extra_denied=["build/*.js"])

    # 1. the `&` weld: without it, every `&` splits and `>&file` loses its filename.
    m, applied = _mutant(
        tmp, "amp-guard.py",
        'if ch == "&" and ((cur and cur[-1] == ">") or cmd[i:i + 1] == ">"):',
        "if False:")
    check("F8b mutant: the `&`-weld patch applied", applied)
    rc, _ = _bash_subproc(GUARD_SRC, repo, "echo x >& .git/config")
    check("F8b mutant: the real guard blocks `>& .git/config` out of process too",
          rc == guard.BLOCK, f"rc={rc}")
    rc, err = _bash_subproc(m, repo, "echo x >& .git/config")
    check("F8b mutant: splitting on every `&` again, `>& .git/config` LEAKS (exit 0)",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-160:]}")
    rc, _ = _bash_subproc(m, repo, "echo x > .git/config")
    check("F8b mutant: that mutant is otherwise alive (a plain redirect still blocks)",
          rc == guard.BLOCK, f"rc={rc}")

    # 2. the descriptor/file split: without it, every `&` form is read as a descriptor.
    m, applied = _mutant(
        tmp, "dup-guard.py",
        'if dup and (rest == "-" or rest.isdigit()):',
        "if dup:")
    check("F8b mutant: the descriptor-vs-file patch applied", applied)
    rc, err = _bash_subproc(m, repo, "echo x >&.git/config")
    check("F8b mutant: treating every `&` form as a descriptor, `>&.git/config` LEAKS",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-160:]}")
    rc, _ = _bash_subproc(m, repo, "echo x &>.git/config")
    check("F8b mutant: that mutant is otherwise alive (`&>file` still blocks)",
          rc == guard.BLOCK, f"rc={rc}")

    # 3. per-source destinations: without them, only the directory is named.
    m, applied = _mutant(
        tmp, "copy-guard.py",
        "    if is_dir:\n        for s in srcs:",
        "    if False:\n        for s in srcs:")
    check("F8b mutant: the per-source-destination patch applied", applied)
    rc, err = _bash_subproc(m, repo, "cp app.js vendor.js build/")
    check("F8b mutant: naming only the directory, a copy onto a denied glob LEAKS",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-160:]}")
    rc, err = _bash_subproc(m, repo, "cp .env.local src/")
    check("F8b mutant: and so does copying a secrets file into an allowed directory",
          rc == guard.ALLOW, f"rc={rc} err={err.strip()[-160:]}")
    rc, _ = _bash_subproc(m, repo, "cp a .git/config")
    check("F8b mutant: that mutant is otherwise alive (a named denied destination blocks)",
          rc == guard.BLOCK, f"rc={rc}")


# -- U1: the tool-name table, and why `Update` stays in it ------------------------------

def _u1(check, tmp):
    """`Update` could not be found as a live Claude Code tool. It is kept anyway.

    The reasoning is pinned as an ASYMMETRY, not as a claim about which tools exist -- the
    tool list is not this repo's to know, and a test that asserted `Update` is real would be
    asserting something no one here can verify. What is verifiable is the cost of each
    mistake, and that is what these checks state.
    """
    repo = _mk_repo(tmp, ["src"], name="toolsrepo")

    check("U1: `Update` is in PATH_FIELD_TOOLS and reads file_path",
          guard.PATH_FIELD_TOOLS.get("Update") == "file_path",
          str(sorted(guard.PATH_FIELD_TOOLS)))

    rc, _ = _run(repo, "Update", {"file_path": ".git/config", "content": "x"})
    check("U1 keep: a key that never fires costs nothing but denies everything it should "
          "if it ever does", rc == guard.BLOCK, f"rc={rc}")
    rc, _ = _run(repo, "Update", {"file_path": "src/app.ts", "content": "x"})
    check("U1 keep: and it does not over-block an ordinary path", rc == guard.ALLOW,
          f"rc={rc}")

    # The other direction, which is why the key stays: a write tool that is NOT in the table
    # is allowed unread AND reported as fully covered, so the probe cannot see the hole.
    targets, covered = guard.targets_from_event("Rewrite", {"file_path": ".git/config"})
    check("U1 asymmetry: an unlisted write tool yields no targets and still claims FULL "
          "coverage -- a missing key is silent, a spare key is merely unused",
          targets == [] and covered is True, f"targets={targets} covered={covered}")
    rc, _ = _run(repo, "Rewrite", {"file_path": ".git/config", "content": "x"})
    check("U1 asymmetry: so an unlisted write tool is ALLOWED into a denied path",
          rc == guard.ALLOW, f"rc={rc}")

    # The wiring cost of keeping it, paid out loud: the hook matcher must fire for every key
    # in the table, or the guard covers a tool that never reaches it. If a later change drops
    # `Update` from PATH_FIELD_TOOLS, generate.MATCHER must drop it in the same commit.
    import generate  # noqa: E402  -- test-only import; guard.py itself imports nothing local
    uncovered = [t for t in list(guard.PATH_FIELD_TOOLS) + ["Bash"]
                 if not re.fullmatch(generate.MATCHER, t)]
    check("U1 wiring: generate.MATCHER fires for every tool the guard claims to cover "
          "(fix whichever side is wrong -- they are one decision in two files)",
          not uncovered, f"MATCHER={generate.MATCHER!r} misses {uncovered}")


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


# -- B1.4 / B1.5: the protocol dispatch -------------------------------------
#
# Three properties, and each one is a way the multi-protocol guard could be green and inert:
#
#   P1  a missing or unknown --protocol must be exit 3, never a default to claude. Defaulting
#       is how a mis-wired Codex hook runs the Claude parser, finds no field it recognises,
#       extracts zero write targets, and allows every write while reporting clean.
#   P2  an unparseable Codex apply_patch must DENY. Not allow, and specifically not `ask`:
#       Codex continues past an unsupported decision, so `ask` is fail-open in a careful
#       decision's clothes.
#   P3  Antigravity BLOCK is exit 0 plus deny-JSON on STDOUT, and the adapter is fail-closed
#       by construction because the host is fail-open. An adapter that exits 2 and prints
#       nothing has enforced nothing there.

def _proto_subproc(guard_path, repo, event, argv):
    p = subprocess.run([sys.executable, guard_path] + list(argv), input=json.dumps(event),
                       text=True, capture_output=True, cwd=repo)
    return p.returncode, p.stdout, p.stderr


def _protocols(check, tmp):
    repo = _mk_repo(tmp, ["src"], name="protorepo")
    gp = os.path.join(repo, ".mir", "guard.py")
    ev = {"tool_name": "Write", "tool_input": {"file_path": ".git/config", "content": "x"},
          "cwd": repo}

    # ---- P1: no default, ever
    rc, out, err = _proto_subproc(gp, repo, ev, [])
    check("P1: a MISSING --protocol is exit 3 -- not 2 (which would look like a working "
          "policy) and not 0", rc == guard.PROTOCOL_ERROR, f"rc={rc}")
    check("P1: and it says it enforced nothing, rather than reporting a block it did not make",
          "enforced NOTHING" in err, err.strip()[-160:])
    rc, _out, err = _proto_subproc(gp, repo, ev, ["--protocol", "borg"])
    check("P1: an UNKNOWN --protocol is exit 3 too, and names the protocols it does answer for",
          rc == guard.PROTOCOL_ERROR and "claude" in err, f"rc={rc} {err.strip()[-120:]}")
    rc, _out, _err = _proto_subproc(gp, repo, ev, ["--protocol", ""])
    check("P1: an EMPTY --protocol is exit 3 -- an empty string is not a host",
          rc == guard.PROTOCOL_ERROR, f"rc={rc}")
    check("P1: the flag is a real argparse flag, which is what arms probe.guard_requires_"
          "protocol -- nothing about the wiring check is hardcoded to a date",
          '"--protocol"' in open(GUARD_SRC, encoding="utf-8").read())

    # THE CONTROL for P1. Without it, "always exit 3" passes every check above.
    rc, _out, _err = _proto_subproc(gp, repo, ev, ["--protocol", "claude"])
    check("P1 control: told which host is asking, the same event BLOCKS (exit 2)",
          rc == guard.BLOCK, f"rc={rc}")

    # MUTATION: give --protocol a default of claude and the hard error disappears. This is
    # the mutation because a default is the ONLY way this branch gets softened in practice --
    # it always looks like a convenience.
    m, applied = _mutant(tmp, "default-proto.py",
                         'ap.add_argument("--protocol", default=None,',
                         'ap.add_argument("--protocol", default="claude",')
    check("P1 mutation: the default-protocol patch applied", applied)
    rc, _out, _err = _proto_subproc(m, repo, ev, [])
    check("P1 MUTATION: with a default, a guard told nothing about its host answers anyway "
          "-- exit 2 here, and on a Codex event it would be exit 0 with zero targets read",
          rc == guard.BLOCK, f"rc={rc}")

    # ---- P2: codex apply_patch
    good_patch = ("*** Begin Patch\n*** Update File: .git/config\n@@\n+x\n*** End Patch\n")
    rc, _o, err = _proto_subproc(gp, repo, {"tool_name": "apply_patch",
                                            "tool_input": {"patch": good_patch}, "cwd": repo},
                                 ["--protocol", "codex"])
    check("P2: a parseable apply_patch is read for its own file headers and BLOCKS a denied "
          "target", rc == guard.BLOCK and ".git/config" in err, f"rc={rc} {err.strip()[-140:]}")
    ok_patch = ("*** Begin Patch\n*** Add File: src/new.ts\n+x\n*** End Patch\n")
    rc, _o, _e = _proto_subproc(gp, repo, {"tool_name": "apply_patch",
                                           "tool_input": {"patch": ok_patch}, "cwd": repo},
                                ["--protocol", "codex"])
    check("P2 control: a patch that writes inside an allowed root is ALLOWED, so the deny "
          "above is the policy and not a refusal to read patches at all",
          rc == guard.ALLOW, f"rc={rc}")
    rc, out, err = _proto_subproc(gp, repo, {"tool_name": "apply_patch",
                                             "tool_input": {"patch": "*** Begin Patch\ngibberish\n"},
                                             "cwd": repo}, ["--protocol", "codex"])
    check("P2: an UNPARSEABLE apply_patch body is DENIED (exit 2), never allowed",
          rc == guard.BLOCK, f"rc={rc} {err.strip()[-140:]}")
    check("P2: and it is not `ask` on any channel -- Codex continues past an unsupported "
          "decision, so `ask` would be this write landing unread",
          "ask" not in out.lower().replace("asked", ""), out.strip()[:160])
    check("P2: the reason says why `ask` was not used, so the next reader does not 'soften' "
          "it back", "continued past" in err or "not a block on Codex" in err,
          err.strip()[-200:])
    rc, _o, _e = _proto_subproc(gp, repo, {"tool_name": "apply_patch", "tool_input": {},
                                           "cwd": repo}, ["--protocol", "codex"])
    check("P2: an apply_patch call with NO body at all is denied too -- a missing body is a "
          "write this guard could not read, not an empty one", rc == guard.BLOCK, f"rc={rc}")
    rc, _o, _e = _proto_subproc(gp, repo, {"tool_name": "Write",
                                           "tool_input": {"file_path": "src/a.ts"},
                                           "cwd": repo}, ["--protocol", "codex"])
    check("P2 control: an ordinary Write under codex is NOT treated as an unreadable patch",
          rc == guard.ALLOW, f"rc={rc}")
    for _verb, _patch in (
            ("Delete File", "*** Begin Patch\n*** Delete File: .git/config\n*** End Patch\n"),
            ("Move to", "*** Begin Patch\n*** Update File: src/a.ts\n"
                        "*** Move to: .env\n*** End Patch\n")):
        rc, _o, _e = _proto_subproc(gp, repo, {"tool_name": "apply_patch",
                                               "tool_input": {"patch": _patch}, "cwd": repo},
                                    ["--protocol", "codex"])
        check(f"P2: `*** {_verb}:` is a write target too -- a rename lands bytes at a path "
              "the first header never named", rc == guard.BLOCK, f"rc={rc}")
    # UNION, not replacement. A patch delivered through a shell heredoc is both a patch and a
    # command; reading only the headers would drop the redirect sitting beside it.
    _here = ("apply_patch <<EOF > .git/config\n*** Begin Patch\n*** Add File: src/x.ts\n"
             "+y\n*** End Patch\nEOF")
    rc, _o, err = _proto_subproc(gp, repo, {"tool_name": "Bash",
                                            "tool_input": {"command": _here}, "cwd": repo},
                                 ["--protocol", "codex"])
    check("P2: a heredoc patch whose OWN headers are innocent is still blocked for the "
          "redirect beside it -- the patch parse is unioned with the shell parse, never "
          "substituted for it", rc == guard.BLOCK and ".git/config" in err,
          f"rc={rc} {err.strip()[-140:]}")

    # MUTATION: answer `ask` instead of denying. The guard still 'decides', the report still
    # looks careful, and the write lands.
    m, applied = _mutant(tmp, "ask-patch.py",
                         'return [], False, (\n                    "an apply_patch body',
                         'return [], True, ""  # MUTATION\n            if False: return [], False, (\n                    "an apply_patch body')
    check("P2 mutation: the ask-instead-of-deny patch applied", applied)
    rc, _o, _e = _proto_subproc(m, repo, {"tool_name": "apply_patch",
                                          "tool_input": {"patch": "*** Begin Patch\ngibberish\n"},
                                          "cwd": repo}, ["--protocol", "codex"])
    check("P2 MUTATION: a guard that does not deny an unreadable patch ALLOWS it (exit 0) -- "
          "the write lands unread and the run reports clean", rc == guard.ALLOW, f"rc={rc}")

    # ---- P3: antigravity answers on stdout, and fails closed
    ag_deny = {"toolCall": {"name": "write_to_file",
                            "args": {"TargetFile": ".git/config", "CodeContent": "x"}},
               "cwd": repo}
    rc, out, _e = _proto_subproc(gp, repo, ag_deny, ["--protocol", "antigravity"])
    check("P3: an Antigravity deny is EXIT 0 -- the host ignores the exit code, so exiting 2 "
          "would be answering on a channel nobody reads", rc == guard.ALLOW, f"rc={rc}")
    check("P3: and the verdict is deny-JSON on stdout",
          json.loads(out or "{}").get("decision") == "deny", out.strip()[:200])
    ag_ok = {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "src/a.ts"}},
             "cwd": repo}
    rc, out, _e = _proto_subproc(gp, repo, ag_ok, ["--protocol", "antigravity"])
    check("P3: an allow is PRINTED too -- an empty stdout is not an allow, it is an adapter "
          "that said nothing, and the probe correctly refuses to read it as one",
          rc == guard.ALLOW and json.loads(out or "{}").get("decision") == "allow",
          out.strip()[:200])
    ag_cmd = {"toolCall": {"name": "run_command",
                           "args": {"CommandLine": "echo x > .git/config"}}, "cwd": repo}
    rc, out, _e = _proto_subproc(gp, repo, ag_cmd, ["--protocol", "antigravity"])
    check("P3: run_command routes through the same shell parser as Bash",
          json.loads(out or "{}").get("decision") == "deny", out.strip()[:200])
    # The `*` matcher's whole reason: the write surface is wider than three tool names.
    ag_odd = {"toolCall": {"name": "sed_file", "args": {"TargetFile": ".git/config"}},
              "cwd": repo}
    rc, out, _e = _proto_subproc(gp, repo, ag_odd, ["--protocol", "antigravity"])
    check("P3: a tool OUTSIDE the three known write tools still has its path arguments read "
          "-- the matcher is `*` because sed_file, notebook_edit and call_mcp_tool all write",
          json.loads(out or "{}").get("decision") == "deny", out.strip()[:200])

    # Fail-closed by construction, on a repo whose policy cannot be loaded.
    broken = _mk_repo(tmp, ["src"], name="brokenpolicy")
    with open(os.path.join(broken, ".mir", "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    bgp = os.path.join(broken, ".mir", "guard.py")
    ev_b = dict(ev, cwd=broken)
    rc, out, _e = _proto_subproc(bgp, broken, ev_b, ["--protocol", "antigravity"])
    check("P3: an unloadable policy DENIES on antigravity -- the host logs a hook error and "
          "continues, so failing open there is a write that lands with nothing recorded",
          json.loads(out or "{}").get("decision") == "deny", out.strip()[:200])
    rc, _o, err = _proto_subproc(bgp, broken, ev_b, ["--protocol", "claude"])
    check("P3 control: the SAME failure fails OPEN on claude, where a hook error is visible "
          "-- the two postures are a fact about the hosts, not a preference",
          rc == guard.ALLOW and "allowing" in err, f"rc={rc} {err.strip()[-120:]}")

    # MUTATION: strip the stdout write. The adapter still runs the policy, still exits 0, and
    # now says nothing -- which is precisely what a probe reading exit codes alone would have
    # scored as a clean allow-path run.
    m, applied = _mutant(tmp, "mute-ag.py",
                         'sys.stdout.write(json.dumps(payload) + "\\n")',
                         "pass  # MUTATION: the stdout write is gone")
    check("P3 mutation: the mute-stdout patch applied", applied)
    rc, out, _e = _proto_subproc(m, repo, ag_deny, ["--protocol", "antigravity"])
    check("P3 MUTATION: a muted adapter exits 0 with EMPTY stdout -- on this host that is an "
          "allowed write, and it is why the probe treats empty stdout as GUARD-ERROR",
          rc == guard.ALLOW and out.strip() == "", f"rc={rc} out={out!r}")

    # MUTATION: make the fail-closed host fail open like the others.
    m, applied = _mutant(tmp, "failopen-ag.py",
                         "    if protocol in FAIL_CLOSED_PROTOCOLS:",
                         "    if False:")
    check("P3 mutation: the fail-open patch applied", applied)
    rc, out, _e = _proto_subproc(m, broken, ev_b, ["--protocol", "antigravity"])
    check("P3 MUTATION: with the fail-closed branch gone, an unloadable policy answers on no "
          "channel at all and the host proceeds",
          out.strip() == "", f"rc={rc} out={out!r}")


def _protocol_probe(check, tmp):
    """The same three properties, seen END TO END through a generated harness and its probe.

    Mutating the GENERATED copy rather than the source, because the defect class this repo
    exists against is a frozen `.mir/guard.py` rotting inside a user's project while the
    maintainer's tree stays green. And asserting the exit CODE, not just "nonzero": 1 (leak),
    2 (could not run) and 3 (inconclusive) are three different findings.
    """
    import generate as gen
    import targets as tp

    repo = os.path.join(tmp, "e2e")
    os.makedirs(repo)
    gen.apply(repo, gen.plan(repo, ["mir-devsecops"], {}, "2026-01-01T00:00:00Z",
                             [tp.BY_NAME["antigravity"]]))
    probe_py = os.path.join(repo, ".mir", "probe.py")
    guard_copy = os.path.join(repo, ".mir", "guard.py")

    def _probe(guard=None):
        cmd = [sys.executable, probe_py, "--repo", repo]
        if guard:
            cmd += ["--guard", guard]
        return subprocess.run(cmd, capture_output=True, text=True).returncode

    check("an antigravity-only harness verifies CLEAN end to end (the control)",
          _probe() == 0, str(_probe()))

    src = open(guard_copy, encoding="utf-8").read()
    mute = os.path.join(tmp, "e2e-mute.py")
    mutated = src.replace('sys.stdout.write(json.dumps(payload) + "\\n")',
                          "pass  # MUTATION")
    open(mute, "w", encoding="utf-8").write(mutated)
    check("MUTATION applied to the GENERATED guard, not the source", mutated != src)
    check("MUTATION: a generated antigravity guard that stops writing to stdout is "
          "INCONCLUSIVE (exit 3) -- not clean, and not a leak the run never found",
          _probe(mute) == 3, str(_probe(mute)))


# -- entry point ------------------------------------------------------------

def run(check):
    with tempfile.TemporaryDirectory() as tmp:
        _f2(check, tmp)
        _f2_mutation(check, tmp)
        _f2_property(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _f8_table(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _f8b_ampersand(check, tmp)
        _f8b_copy(check, tmp)
        _f8b_property(check, tmp)
        _f8b_mutation(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _d3(check, tmp)
        _u1(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _protocols(check, tmp)
    with tempfile.TemporaryDirectory() as tmp:
        _protocol_probe(check, tmp)
