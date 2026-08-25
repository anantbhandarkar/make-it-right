"""Verification + CLI gating: init/probe.py and init/cli.py.

Loaded by init/test_init.py, which injects `check`. No test framework, no __main__.

The properties here are the ones that decide whether a green probe means anything:

  - a manifest-derived prober must be able to INSTANTIATE every pattern the manifest holds.
    If it cannot, it attacks the pattern's own literal spelling, the guard blocks that
    trivially, and the probe passes while never firing the attack the entry exists for.
  - a guard that blocks everything must not verify clean. Too-tight is fail-safe for the
    filesystem and fail-USELESS for the report: with the positive control blocked, no BLOCK
    row distinguishes "denied" from "broken".
  - the probe invokes guard.py directly, so nothing else in it ever reads the hook
    registration. A stale matcher is invisible to every attack row.
  - the CLI must refuse an undecided stack, and must not refuse a decided one.

Each mutation test answers "would this check still pass if the thing it checks were removed?"
A check that survives its own mutation is decoration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import guard as guard_mod  # noqa: E402
import probe  # noqa: E402
import schema  # noqa: E402
import cli  # noqa: E402

PROBE = os.path.join(HERE, "probe.py")
CLI = os.path.join(HERE, "cli.py")

# The matcher generate.py registers. Hand-built here on purpose: importing gen.plan() would
# make this suite fail whenever generation is mid-change, and a wiring test that depends on
# the generator cannot tell "the matcher is wrong" from "the generator is mid-edit".
GOOD_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Update|Bash"


# The command generate.py registers today: no `--protocol`. Named as V1_COMMAND rather than
# spelled inline, because once --protocol is required this exact string is every existing
# user's migration case -- a correct matcher over a guard that exits 3 on every call.
V1_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"'


def _settings(matcher, tag="mir-init-guard", cmd=V1_COMMAND):
    return {"hooks": {"PreToolUse": [{
        "matcher": matcher, "_mir": tag,
        "hooks": [{"type": "command", "command": cmd}],
    }]}}


def _mk_repo(tmp, roots=("src", "tests"), settings=None, name="repo", denied=None,
             targets=None, files=None, manifest_version=None):
    """A repo with a manifest, a real guard, and (optionally) a hand-built settings.json.

    `denied` overrides the baseline denied_paths. Used to TRIM a fixture, never to widen one:
    a 14-entry policy costs ~50 guard subprocesses per probe run, and a test about the verdict
    CHANNEL learns nothing from the 45th denied path. `.mir` stays in every trimmed list --
    build_attacks always fires the self-protection row at `.mir/manifest.json`, so dropping it
    makes that row leak and every mutation below would "fail" for a reason of the test's own
    making.

    `targets` writes the manifest's targets block; `files` writes extra JSON wiring files.
    """
    import shutil
    repo = os.path.join(tmp, name)
    os.makedirs(os.path.join(repo, ".mir"), exist_ok=True)
    manifest = schema.project_manifest(name, allowed_write_roots=list(roots))
    if denied is not None:
        manifest["policy"]["denied_paths"] = list(denied)
    if targets is not None:
        manifest["targets"] = targets
    if manifest_version is not None:
        manifest["mir_manifest_version"] = manifest_version
    with open(os.path.join(repo, ".mir", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    shutil.copy(os.path.join(HERE, "guard.py"), os.path.join(repo, ".mir", "guard.py"))
    if settings is not None:
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    for rel, obj in (files or {}).items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    return repo


# The wiring file an Antigravity target registers its hook in. Hand-built rather than
# generated, for the same reason GOOD_MATCHER is: a verdict-channel test that depends on the
# generator cannot tell "the probe misread the channel" from "the generator is mid-edit".
AGENTS_HOOKS = {"hooks": {"PreToolUse": [{"_mir": "mir-init-guard", "hooks": [
    {"type": "command", "command": 'python3 "$PROJECT_DIR/.mir/guard.py"'}]}]}}

# An Antigravity adapter, in the shape the host actually reads: the exit code is ignored, so
# the verdict is JSON on stdout and the process exits 0 either way. It delegates the policy
# decision to the real guard, so this fixture tests the CHANNEL and nothing else.
#
# `{emit}` is the whole point. The mutation strips the stdout write and changes nothing else,
# which is exactly the adapter bug a probe that only read exit codes would score as a clean
# allow-path run.
_ADAPTER = '''\
import json, subprocess, sys
data = sys.stdin.read()
p = subprocess.run([sys.executable, {guard!r}], input=data, text=True, capture_output=True)
payload = ({{"decision": "deny", "reason": p.stderr.strip()[:200]}} if p.returncode == 2
           else {{"decision": "allow"}})
{emit}
sys.exit(0)
'''


def _adapter(tmp, guard, name, emit="print(json.dumps(payload))"):
    return _stub_guard(tmp, _ADAPTER.format(guard=guard, emit=emit), name)


# A guard that REQUIRES --protocol and otherwise delegates to the real one. It stands in for
# the post-bump guard so the wiring rules can be tested before that guard exists -- and it is
# a fixture, not a prediction: probe.guard_requires_protocol reads the flag off whatever guard
# it is handed, so the same rules apply unchanged to the real one the day it takes the flag.
_PROTOCOL_GUARD = '''\
import argparse, subprocess, sys
ap = argparse.ArgumentParser()
ap.add_argument("--protocol", required=True)
ap.parse_args()
sys.exit(subprocess.run([sys.executable, {guard!r}], input=sys.stdin.read(),
                        text=True).returncode)
'''

# A guard that enforces exactly as the real one does and differs from it in ONE respect: the
# version it declares. It is the only shape that makes the version row discriminating -- a
# real version mismatch also makes the guard fail open, so every row leaks and the run would
# exit 1 with or without a version check.
_DELEGATE = '''\
import subprocess, sys
sys.exit(subprocess.run([sys.executable, {guard!r}], input=sys.stdin.read(),
                        text=True).returncode)
'''


def _protocol_guard(tmp, guard, name="protocol-guard.py"):
    return _stub_guard(tmp, _PROTOCOL_GUARD.format(guard=guard), name)


def _delegate(tmp, guard, name, version=None):
    return _stub_guard(tmp, _DELEGATE.format(guard=guard), name, version=version)


def _run_probe(repo, guard=None, extra=(), env=None):
    cmd = [sys.executable, PROBE, "--repo", repo]
    if guard:
        cmd += ["--guard", guard]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _json_probe(repo, guard=None, env=None):
    """The probe's own report object. Read from a subprocess rather than by calling probe()
    in-process, because the ~-expansion under test reads $HOME and a test that mutates this
    process's environment to steer it would be steering the assertion too."""
    r = _run_probe(repo, guard=guard, extra=["--json"], env=env)
    return json.loads(r.stdout)


def _rows_for(report, entry):
    """Every row the prober generated FOR one denied_paths entry, matched on the `why` the
    prober stamped rather than on the path, so the assertion does not have to know how the
    entry gets spelled."""
    return [r for r in report["results"] if r["why"] == "denied_path " + entry]


def _tree(root):
    """Every path under root, relative and sorted. Recursive on purpose: a write the probe
    should never make would land inside a subdirectory, not at the top level."""
    out = []
    for base, dirs, files in os.walk(root):
        for n in dirs + files:
            out.append(os.path.relpath(os.path.join(base, n), root))
    return sorted(out)


def _ancestors(path, repo_root):
    """Every proper ancestor directory of an attack path, absolute."""
    cur = os.path.normpath(path if os.path.isabs(path) else os.path.join(repo_root, path))
    out = []
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        out.append(parent)
        cur = parent


def _stub_guard(tmp, body, name, version=None):
    """A stand-in guard.

    Every stub declares GUARD_MANIFEST_VERSION, because the probe's version row (correctly)
    refuses to call a run clean against a guard it cannot version -- and a stub that failed
    for THAT reason would have stopped testing the one property it names. `version` is
    overridable so a stub can declare a WRONG one on purpose.
    """
    p = os.path.join(tmp, name)
    v = schema.MANIFEST_VERSION if version is None else version
    with open(p, "w", encoding="utf-8") as f:
        f.write("GUARD_MANIFEST_VERSION = %r\n" % (v,) + body)
    return p


def _report(leaks=(), false_blocks=(), guard_errors=(), wiring_status="wired",
            version_status="ok"):
    """A minimal report shaped like probe.probe()'s, for testing exit_code in isolation."""
    return {"leaks": list(leaks), "false_blocks": list(false_blocks),
            "guard_errors": list(guard_errors),
            "version": {"status": version_status, "manifest": 1, "guard": 1, "why": ""},
            "wiring": {"status": wiring_status, "rows": [], "failures": [], "tools": []}}


# --------------------------------------------------------------- guard mutations
#
# Every mutation below rewrites the GENERATED guard -- the copy under <repo>/.mir/ -- and never
# init/guard.py. The defect class is a frozen guard rotting inside a user's project while the
# maintainer's tree stays green, so mutating the source and re-running this repo's own tests
# asks a strictly weaker question than the one that ships.
#
# A valid pattern that matches nothing. Deleting the regex outright would shift the list and
# leave a dangling comment; neutering it in place keeps the mutant a one-token edit.
_NEVER = 're.compile(r"(?!x)x")'

# Each verb needs BOTH arms of the guard's union removed -- the whole-command regex AND the
# segment parser's branch -- because guard.targets_from_event unions them, so either arm alone
# still names the target and the mutant would stay green for the wrong reason.
_VERB_MUTATIONS = [
    ("redirect", [
        (r'''re.compile(r">>?\s*([^\s;|&>]+)")''', _NEVER),
        (r'''_REDIR_TOKEN = re.compile(r"^(?:\d+|&)?>{1,2}\|?(.*)$")''',
         "_REDIR_TOKEN = " + _NEVER),
    ]),
    ("tee", [
        (r'''re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)")''', _NEVER),
        ('    if verb == "tee":', '    if verb == "__mutated_away__":'),
    ]),
    ("dd", [
        (r'''re.compile(r"\bdd\b[^\n]*\bof=([^\s;|&]+)")''', _NEVER),
        ('    elif verb == "dd":', '    elif verb == "__mutated_away__":'),
    ]),
    ("cp", [
        (r'''re.compile(r"\b(?:cp|mv|install)\s+[^\n]*?\s([^\s;|&]+)\s*$")''', _NEVER),
        ("    elif verb in _COPY_VERBS:", '    elif verb in ("__mutated_away__",):'),
    ]),
]

# R6, the risk nobody had a test for: `.mir-probe-canary` is not under `.mir` only because of
# the `+ os.sep`.
_IS_UNDER_MUTATION = [("    return path.startswith(root + os.sep)",
                       "    return path.startswith(root)")]


def _mutate_guard(repo, tmp, name, pairs):
    """(path to the mutant, anchors that did not apply exactly once).

    A mutation that does not apply is a FAILING check, never a silent pass: an anchor that has
    drifted turns every assertion below it into a tautology run against an unmutated guard,
    which is the shape this whole file exists to refuse.
    """
    with open(os.path.join(repo, ".mir", "guard.py"), encoding="utf-8") as f:
        src = f.read()
    missed = [old for old, _new in pairs if src.count(old) != 1]
    for old, new in pairs:
        src = src.replace(old, new)
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return path, missed


def _real_attacks(entry):
    """Attack paths that are real filesystem paths: no metacharacter, and not the entry
    itself. Firing the entry verbatim proves nothing -- the guard blocks a literal `*` path
    trivially, and no agent would ever write one."""
    return [p for p in probe.denied_attack_paths(entry)
            if not probe.is_glob(p) and p != entry]


def run(check):
    # ---------------------------------------------------------------- F3a: instantiation
    #
    # The permanent generalised form. It is not "does .claude/settings*.json work now" but
    # "can this prober instantiate ANY pattern the manifest is allowed to hold", so a future
    # entry with a metacharacter anywhere but the end cannot re-open the same hole.
    unattacked = [e for e in schema.BASELINE_DENIED if not _real_attacks(e)]
    check("every BASELINE_DENIED entry yields at least one real, non-identical attack path",
          not unattacked, f"no real attack for: {unattacked}")

    settings_paths = probe.denied_attack_paths(".claude/settings*.json")
    check("an INTERIOR `*` is instantiated, not left as a literal",
          any(p == ".claude/settings.local.json" for p in settings_paths), str(settings_paths))
    check("the interior-`*` expansion still covers the pattern's bare form",
          ".claude/settings.json" in settings_paths, str(settings_paths))

    env_paths = probe.denied_attack_paths("**/.env*")
    check("**/.env* does not regress: still 9 paths across depth and suffix",
          len(env_paths) == 9, f"{len(env_paths)}: {env_paths}")
    check("**/.env* still spans the depth axis including no depth",
          {".env", "src/.env", "a/b/.env"} <= set(env_paths), str(env_paths))
    check("**/.env* still spans the suffix axis",
          {".env.local", ".env.development"} <= set(env_paths), str(env_paths))

    # An explicit repo_root, because a literal entry's shape is a fact about a tree: with the
    # cwd's spelling this check would silently change meaning depending on where it was run.
    with tempfile.TemporaryDirectory() as _t:
        check("a literal entry is attacked at itself and one level inside",
              probe.denied_attack_paths(".git", _t) == [".git", ".git/child-file"])

    # MUTATION: with instantiation removed, denied_attack_paths falls back to the literal
    # entry -- which is exactly what the broken version did. If the generalised check above
    # can survive that, it was never testing instantiation at all.
    _orig = probe.expand_glob_attacks
    try:
        probe.expand_glob_attacks = lambda entry: []
        mutated = [e for e in schema.BASELINE_DENIED if not _real_attacks(e)]
        check("MUTATION: with expand_glob_attacks gutted, some entry reports zero real attacks",
              bool(mutated), "the generalised check passed against a prober that expands nothing")
    finally:
        probe.expand_glob_attacks = _orig

    # ---------------------------------------------------------------- F4: exit codes
    check("exit_code: a clean report is 0", probe.exit_code(_report()) == probe.EXIT_CLEAN)
    check("exit_code: a leak is 1 (the contract CI already gates on is unchanged)",
          probe.exit_code(_report(leaks=[{"path": "x"}])) == probe.EXIT_LEAK)
    check("exit_code: a blocked positive control is 3, not 0",
          probe.exit_code(_report(false_blocks=[{"path": "x"}])) == probe.EXIT_INCONCLUSIVE)
    check("exit_code: --allow-false-blocks makes deliberate over-tightening clean",
          probe.exit_code(_report(false_blocks=[{"path": "x"}]), True) == probe.EXIT_CLEAN)
    check("exit_code: a guard code outside {0,2} is 3, never a leak",
          probe.exit_code(_report(guard_errors=[{"path": "x"}])) == probe.EXIT_INCONCLUSIVE)
    check("exit_code: a real leak outranks an inconclusive row",
          probe.exit_code(_report(leaks=[{"path": "x"}],
                                  false_blocks=[{"path": "y"}])) == probe.EXIT_LEAK)
    check("exit_code: --allow-false-blocks does not silence a leak",
          probe.exit_code(_report(leaks=[{"path": "x"}],
                                  false_blocks=[{"path": "y"}]), True) == probe.EXIT_LEAK)

    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp, settings=_settings(GOOD_MATCHER))
        r = _run_probe(repo)
        check("the real guard, correctly wired, still verifies clean (exit 0)",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-400:]}")

        # THE F4 REGRESSION. A guard that blocks every write used to exit 0 here: false
        # blocks were computed and discarded, so the positive control's verdict was thrown
        # away and the remaining BLOCK rows read as proof.
        block_all = _stub_guard(tmp, "import sys\nsys.exit(2)\n", "block-all.py")
        r = _run_probe(repo, guard=block_all)
        check("a block-everything guard is INCONCLUSIVE (exit 3), not clean",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")
        check("the block-everything report names the blocked positive control",
              "INCONCLUSIVE -- a positive control was blocked" in r.stdout, r.stdout[-300:])
        r = _run_probe(repo, guard=block_all, extra=["--allow-false-blocks"])
        check("--allow-false-blocks accepts a deliberately over-tight guard",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode}")

        # the mirror case, asserted so the new third code cannot swallow the old second one
        allow_all = _stub_guard(tmp, "import sys\nsys.exit(0)\n", "allow-all.py")
        r = _run_probe(repo, guard=allow_all)
        check("an allow-everything guard is still a LEAK (exit 1)",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode}")

        # a guard that neither allows nor blocks proves nothing in either direction
        crasher = _stub_guard(tmp, "import sys\nsys.exit(7)\n", "crasher.py")
        r = _run_probe(repo, guard=crasher)
        check("a guard returning a code outside {0,2} is INCONCLUSIVE, not a leak",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")

    # ---------------------------------------------- Trap 5: the version claim, made true
    #
    # guard.py's docstring says "a version mismatch fails closed -- the probe owns that".
    # probe.py contained zero references to mir_manifest_version, so that was a doc claim the
    # code did not support, inside the security tooling. At MANIFEST_VERSION 2 the mismatch
    # stops being hypothetical: it becomes every existing user's migration path.
    check("the guard's version is read from its SOURCE, matching the constant it declares",
          probe.guard_manifest_version(os.path.join(HERE, "guard.py")) ==
          guard_mod.GUARD_MANIFEST_VERSION,
          str(probe.guard_manifest_version(os.path.join(HERE, "guard.py"))))

    with tempfile.TemporaryDirectory() as tmp:
        # The property that forced ast over import: a guard whose body is `sys.exit(0)` must
        # still be versionable, because the probe has to be able to inspect a BROKEN guard.
        dead = _stub_guard(tmp, "import sys\nsys.exit(0)\n", "dead-guard.py", version=7)
        check("a guard that cannot be run can still be versioned: parsed, never imported",
              probe.guard_manifest_version(dead) == 7)
        silent = os.path.join(tmp, "no-version.py")
        with open(silent, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(0)\n")
        check("a guard declaring no version reports None, not a guessed one",
              probe.guard_manifest_version(silent) is None)

        _v = probe.version_report
        check("agreeing versions are ok",
              _v({"mir_manifest_version": 7}, dead)["status"] == probe.VERSION_OK)
        _m = _v({"mir_manifest_version": 2}, dead)
        check("a mismatch is reported as a mismatch and names `mir init` as the fix",
              _m["status"] == probe.VERSION_MISMATCH and "mir init" in _m["why"], str(_m))
        check("the mismatch row prints both sides, so the reader knows which one to move",
              _m["manifest"] == 2 and _m["guard"] == 7, str(_m))
        check("an undeclared version is UNKNOWN, not a mismatch: they are different findings",
              _v({"mir_manifest_version": 2}, silent)["status"] == probe.VERSION_UNKNOWN)

    check("exit_code: a version mismatch is a LEAK (1) -- the guard fails open on it, so "
          "every write lands unchecked",
          probe.exit_code(_report(version_status=probe.VERSION_MISMATCH)) == probe.EXIT_LEAK)
    check("exit_code: an unversionable guard is INCONCLUSIVE (3), not a leak the run never "
          "found",
          probe.exit_code(_report(version_status=probe.VERSION_UNKNOWN))
          == probe.EXIT_INCONCLUSIVE)

    with tempfile.TemporaryDirectory() as tmp:
        # THE ASYMMETRY, both halves asserted on the same repo. The manifest is one version
        # ahead of the guard, which is exactly the state `mir init` leaves behind after the
        # bump if a user does not regenerate.
        ahead = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="ahead",
                         denied=[".git", ".mir"],
                         manifest_version=schema.MANIFEST_VERSION + 1)
        gp = os.path.join(ahead, ".mir", "guard.py")
        ev = {"tool_name": "Write", "tool_input": {"file_path": ".git/config", "content": "x"},
              "cwd": ahead}
        p = subprocess.run([sys.executable, gp], input=json.dumps(ev), text=True,
                           capture_output=True)
        check("RUNTIME fails OPEN on a version mismatch: the guard allows a denied write and "
              "says so on stderr, rather than bricking the agent",
              p.returncode == 0 and "NOT ENFORCING" in p.stderr, f"rc={p.returncode}")
        r = _run_probe(ahead)
        check("VERIFICATION fails CLOSED on the same repo: the probe exits 1",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode}")
        check("and it names the version, so the report does not blame the denied paths for a "
              "guard that was never enforcing",
              "Manifest version (mismatch)" in r.stdout and "mir init" in r.stdout,
              r.stdout[-600:])

        # MUTATION: the version is the ONLY thing wrong. This guard enforces exactly as the
        # real one does -- it delegates to it -- and declares a version the manifest does not
        # carry. Without the version row the run is clean; with it, the run is a leak. A real
        # mismatch cannot make this point, because it also makes every attack row leak.
        good = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="agree",
                        denied=[".git", ".mir"])
        real = os.path.join(good, ".mir", "guard.py")
        r = _run_probe(good, guard=_delegate(tmp, real, "delegate-ok.py"))
        check("a delegating guard declaring the RIGHT version verifies clean (the control)",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-400:]}")
        r = _run_probe(good, guard=_delegate(tmp, real, "delegate-stale.py",
                                             version=schema.MANIFEST_VERSION + 1))
        check("MUTATION: the same guard, enforcing identically, declaring a stale version, "
              "exits 1 -- the version row is the only thing that changed the verdict",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode} {r.stdout[-400:]}")

        unversioned = os.path.join(tmp, "unversioned.py")
        with open(unversioned, "w", encoding="utf-8") as f:
            f.write(_DELEGATE.format(guard=real))
        r = _run_probe(good, guard=unversioned)
        check("a guard that declares no version at all cannot verify clean either, and is 3 "
              "rather than 1: nothing was proven in either direction",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")

    # ------------------------------------------------- B1.5: protocol-aware verdicts
    #
    # Trap 3, asserted as a property of the READER rather than of any one adapter. Antigravity
    # BLOCK is exit 0 plus deny-JSON on stdout and the host ignores the exit code, so a probe
    # that judged by exit code alone would read every BLOCK there as an ALLOW, turn every
    # denied row into a LEAK, and make "have the adapter exit 2 as well" look like the fix --
    # green probe, real host allowing every write.
    check("claude blocks on exit 2 and allows on exit 0",
          probe.read_verdict("claude", 2, "")[0] == probe.V_BLOCK
          and probe.read_verdict("claude", 0, "")[0] == probe.V_ALLOW)
    check("an exit-code host returning something outside {0,2} is GUARD-ERROR",
          probe.read_verdict("claude", 7, "")[0] == probe.V_ERROR)

    check("antigravity BLOCKs on exit 0 with deny-JSON on stdout: the channel its host reads",
          probe.read_verdict("antigravity", 0, '{"decision": "deny"}')[0] == probe.V_BLOCK)
    check("antigravity allows on exit 0 with an allow decision",
          probe.read_verdict("antigravity", 0, '{"decision": "allow"}')[0] == probe.V_ALLOW)
    check("exit 0 with EMPTY stdout is GUARD-ERROR on the stdout channel, never ALLOW -- a "
          "crashed adapter must not read as a clean allow-path run",
          probe.read_verdict("antigravity", 0, "")[0] == probe.V_ERROR)
    check("exit 0 with UNPARSEABLE stdout is GUARD-ERROR, never ALLOW",
          probe.read_verdict("antigravity", 0, "blocked!")[0] == probe.V_ERROR)
    check("JSON without a `decision` is GUARD-ERROR, never ALLOW",
          probe.read_verdict("antigravity", 0, '{"reason": "no"}')[0] == probe.V_ERROR)
    check("THE TRAP: exit 2 with no stdout is NOT a block on antigravity -- the host ignores "
          "the exit code, so an adapter that only exits 2 has enforced nothing",
          probe.read_verdict("antigravity", 2, "")[0] == probe.V_ERROR)
    check("an unrecognised decision string fails closed as GUARD-ERROR",
          probe.read_verdict("antigravity", 0, '{"decision": "maybe"}')[0] == probe.V_ERROR)
    check("`ask` is not a block: an unsupported decision is continued past, so nothing was "
          "enforced (B1.5, the Codex apply_patch case)",
          probe.read_verdict("antigravity", 0, '{"decision": "ask"}')[0] == probe.V_ERROR
          and probe.read_verdict("codex", 0, '{"decision": "ask"}')[0] == probe.V_ERROR)
    check("an UNKNOWN protocol gets the stricter reader, not Claude's semantics by default",
          probe.channel_for("some-future-host") == probe.CHANNEL_STDOUT
          and probe.read_verdict("some-future-host", 2, "")[0] == probe.V_ERROR)

    check("a manifest with no targets block declares Claude Code, not nothing",
          probe.declared_targets({}) == [{"name": "claude", "protocol": "claude"}])
    check("a targets DICT is read, and a spec may name a protocol other than its own name",
          probe.declared_targets({"targets": {"cursor": {"protocol": "claude"}}}) ==
          [{"name": "cursor", "protocol": "claude"}])
    check("a targets LIST of names is read too: this file outlives the generator that wrote "
          "the manifest",
          probe.declared_targets({"targets": ["codex"]}) ==
          [{"name": "codex", "protocol": "codex"}])
    check("attack_protocols dedupes: two targets on one channel are not two tables",
          probe.attack_protocols(probe.declared_targets(
              {"targets": {"claude": {}, "cursor": {"protocol": "claude"}}})) == ["claude"])

    with tempfile.TemporaryDirectory() as tmp:
        takes = _stub_guard(tmp, 'import argparse\nap = argparse.ArgumentParser()\n'
                                 'ap.add_argument("--protocol")\n', "takes-protocol.py")
        never = _stub_guard(tmp, "import sys\nsys.exit(0)\n", "no-protocol.py")
        check("guard_requires_protocol reads the flag off the guard's own source, so the "
              "check self-updates the moment --protocol lands",
              probe.guard_requires_protocol(takes)
              and not probe.guard_requires_protocol(never))

    # end to end, on a TRIMMED policy: the question here is the channel, not the coverage
    with tempfile.TemporaryDirectory() as tmp:
        ag = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="antigravity",
                      denied=[".git", ".mir"], targets={"antigravity": {}},
                      files={".agents/hooks.json": AGENTS_HOOKS})
        guard_copy = os.path.join(ag, ".mir", "guard.py")

        # A guard with no stdout verdict at all, judged as antigravity would judge it. This is
        # the state of the tree BEFORE an antigravity adapter exists, and GUARD-ERROR is the
        # correct finding: the guard answers on a channel this host does not read.
        r = _run_probe(ag)
        rep = _json_probe(ag)
        check("an exit-code-only guard facing a declared antigravity target is INCONCLUSIVE "
              "(exit 3), not clean and not a leak",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")
        check("not one denied row reads BLOCK off an exit code the antigravity host ignores",
              not [x for x in rep["results"] if x["got"] == probe.V_BLOCK],
              str([x["path"] for x in rep["results"] if x["got"] == probe.V_BLOCK]))
        check("and not one of them reads ALLOW either: a silent exit is neither verdict",
              not [x for x in rep["results"] if x["got"] == probe.V_ALLOW]
              and not rep["leaks"], str(rep["leaks"]))

        # THE CONTROL. Without it, "report GUARD-ERROR for everything" passes every check
        # above -- a reader that never says BLOCK is not a protocol-aware reader.
        good = _adapter(tmp, guard_copy, "ag-adapter.py")
        r = _run_probe(ag, guard=good)
        check("a real antigravity adapter -- exit 0, deny-JSON on stdout -- verifies clean",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-500:]}")

        # MUTATION: strip the stdout write and change nothing else. The adapter still exits 0,
        # still runs the policy, and now says nothing -- which is precisely the shape a probe
        # that read exit codes alone would have scored as a clean allow-path run.
        mute = _adapter(tmp, guard_copy, "ag-mute.py",
                        emit="pass  # MUTATION: the stdout write is gone")
        r = _run_probe(ag, guard=mute)
        muted = _json_probe(ag, guard=mute)
        check("MUTATION: an adapter that stops writing to stdout does NOT report clean",
              r.returncode != probe.EXIT_CLEAN, f"rc={r.returncode}")
        check("MUTATION: it is INCONCLUSIVE (exit 3) -- the adapter neither allowed nor "
              "blocked, so calling it a leak would name a finding the run did not make",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")
        check("MUTATION: every row is GUARD-ERROR, and not one of them was read as ALLOW",
              all(x["got"] == probe.V_ERROR for x in muted["results"])
              and not muted["leaks"] and not muted["false_blocks"],
              str([x["got"] for x in muted["results"][:4]]))

    # ---------------------------------------------------------------- F3b: wiring
    #
    # Nothing else in the probe reads .claude/settings.json: every attack pipes an event to
    # guard.py directly. Without this phase the matcher could name no tool at all and every
    # attack row would still say BLOCK.
    check("guarded_tools derives the tool list from the guard's own source",
          set(probe.guarded_tools(os.path.join(HERE, "guard.py"))) ==
          set(guard_mod.PATH_FIELD_TOOLS) | {"Bash"},
          str(probe.guarded_tools(os.path.join(HERE, "guard.py"))))

    with tempfile.TemporaryDirectory() as tmp:
        gp = os.path.join(HERE, "guard.py")

        repo = _mk_repo(tmp, settings=_settings(GOOD_MATCHER), name="wired")
        w = probe.wiring_report(repo, gp)
        check("a correct matcher wires every guarded tool", w["status"] == "wired",
              str(w["failures"]))
        check("the wiring phase covers Bash as well as the structured-field tools",
              "Bash" in [r["tool"] for r in w["rows"]])

        # MUTATION: a matcher that fires for one tool only. Without this phase the probe is
        # blind to it -- the guard file is untouched, so every attack still blocks.
        bad = _mk_repo(tmp, settings=_settings("Notebook.*"), name="stale-matcher")
        w = probe.wiring_report(bad, gp)
        check("MUTATION: a stale 'Notebook.*' matcher is caught as unwired",
              w["status"] == "unwired", str(w))
        check("MUTATION: the stale matcher is reported per tool, naming Write",
              "Write" in [f["tool"] for f in w["failures"]], str(w["failures"]))
        check("MUTATION: the tool the stale matcher DOES cover is not reported",
              "NotebookEdit" not in [f["tool"] for f in w["failures"]], str(w["failures"]))
        r = _run_probe(bad)
        check("a stale matcher fails the probe end to end, though every attack blocks",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode}")

        # a partial matcher: the drift shape that appears when a tool is added to
        # PATH_FIELD_TOOLS and the matcher is not updated with it
        partial = _mk_repo(tmp, settings=_settings("Write|Edit"), name="partial")
        w = probe.wiring_report(partial, gp)
        check("a matcher missing Bash is unwired, not 'close enough'",
              w["status"] == "unwired" and "Bash" in [f["tool"] for f in w["failures"]],
              str(w["failures"]))

        # a prefix must not count as coverage: fullmatch, not search
        prefix = _mk_repo(tmp, settings=_settings("Wri"), name="prefix")
        w = probe.wiring_report(prefix, gp)
        check("a matcher that only prefixes a tool name does not count as covering it",
              "Write" in [f["tool"] for f in w["failures"]], str(w["failures"]))

        # a declared target with NO wiring entry is a finding, never a skip
        none = _mk_repo(tmp, settings=None, name="unwired-at-all")
        w = probe.wiring_report(none, gp)
        check("no settings.json at all is a wiring FAIL, not a skip",
              w["status"] == "unconfirmed"
              and {r["tool"] for r in w["failures"]} == set(w["tools"]) | {"(command)"}, str(w))
        check("an unconfirmed wiring cannot exit 0",
              probe.exit_code(_report(wiring_status="unconfirmed")) == probe.EXIT_INCONCLUSIVE)

        empty = _mk_repo(tmp, settings={"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}]}},
            name="foreign-hook-only")
        w = probe.wiring_report(empty, gp)
        check("a settings.json holding only a FOREIGN hook is not mistaken for mir's wiring",
              w["status"] == "unconfirmed", str(w))

        # settings.local.json counts: Claude Code merges it, so a guard registered there is
        # genuinely wired and calling it missing would send a user to fix a working setup
        local = _mk_repo(tmp, settings=None, name="local-only")
        os.makedirs(os.path.join(local, ".claude"), exist_ok=True)
        with open(os.path.join(local, ".claude", "settings.local.json"), "w",
                  encoding="utf-8") as f:
            json.dump(_settings(GOOD_MATCHER), f)
        check("a guard registered in settings.local.json counts as wired",
              probe.wiring_report(local, gp)["status"] == "wired")

        broken = _mk_repo(tmp, settings=None, name="unparseable")
        os.makedirs(os.path.join(broken, ".claude"), exist_ok=True)
        with open(os.path.join(broken, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        w = probe.wiring_report(broken, gp)
        check("an unparseable settings.json is unconfirmed, and says why",
              w["status"] == "unconfirmed" and "did not parse" in w["why"], str(w["why"]))

        # the probe must survive inspecting a guard it must never run
        exiting = _stub_guard(tmp, "import sys\nsys.exit(0)\n", "self-exiting-guard.py")
        check("guarded_tools falls back rather than executing the guard it is reading",
              set(probe.guarded_tools(exiting)) == set(probe._FALLBACK_PATH_FIELD_TOOLS) | {"Bash"})

    # ------------------------------------------------ Trap 2: the COMMAND, not the matcher
    #
    # wiring_report used to build every row from entry["matcher"] and never read what the hook
    # RUNS. Once --protocol is required, a repo carrying the v1 command has a correct matcher
    # and a guard that exits 3 on every call: status `wired`, enforcement zero. The first green
    # run after the bump would mean nothing.
    check("command_protocol reads both spellings of the flag",
          probe.command_protocol("guard.py --protocol claude") == "claude"
          and probe.command_protocol("guard.py --protocol=codex") == "codex"
          and probe.command_protocol(V1_COMMAND) is None)

    _cv = probe.command_verdict
    check("a registered command that does not invoke guard.py fails, matcher or no matcher",
          not _cv(["echo user"], "claude", False)[0])
    check("before --protocol exists, the v1 command is correct and must not be failed: "
          "demanding a flag the guard would reject fails a harness that works",
          _cv([V1_COMMAND], "claude", False)[0])
    _ok, _why = _cv([V1_COMMAND], "claude", True)
    check("TRAP 2: once --protocol is required, the v1 command is a wiring FAILURE", not _ok)
    check("and the failure names the migration rather than crashing on the missing flag",
          "mir init" in _why and "--protocol" in _why, _why)
    check("a command carrying the right --protocol passes",
          _cv([V1_COMMAND + " --protocol claude"], "claude", True)[0])
    _ok, _why = _cv([V1_COMMAND + " --protocol codex"], "claude", True)
    check("a command carrying the WRONG protocol fails and says which one is registered",
          not _ok and "codex" in _why, _why)

    with tempfile.TemporaryDirectory() as tmp:
        stale = _mk_repo(tmp, settings=_settings(GOOD_MATCHER), name="v1-command")
        pg = _protocol_guard(tmp, os.path.join(stale, ".mir", "guard.py"))
        w = probe.wiring_report(stale, pg)
        check("TRAP 2 end to end: a correct matcher over the v1 command is UNWIRED, not wired",
              w["status"] == "unwired", str(w["status"]))
        check("the failing row is the command row, not a tool row -- every matcher still fires",
              [f["tool"] for f in w["failures"]] == ["(command)"], str(w["failures"]))
        r = _run_probe(stale, guard=pg)
        check("and the probe exits 1: an unenforcing hook is the same shipping fact as a leak",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode} {r.stdout[-400:]}")

        # THE CONTROL. Without it, "fail whenever the guard takes --protocol" passes above.
        fixed = _mk_repo(tmp, settings=_settings(GOOD_MATCHER, cmd=V1_COMMAND +
                                                 " --protocol claude"), name="v2-command")
        pg2 = _protocol_guard(tmp, os.path.join(fixed, ".mir", "guard.py"), "protocol-guard2.py")
        check("the migrated command is wired",
              probe.wiring_report(fixed, pg2)["status"] == "wired",
              str(probe.wiring_report(fixed, pg2)["failures"]))
        r = _run_probe(fixed, guard=pg2)
        check("and the migrated harness verifies clean end to end",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-400:]}")

    # ------------------------------------------ B1.3: the declared set comes from the manifest
    with tempfile.TemporaryDirectory() as tmp:
        gp = os.path.join(HERE, "guard.py")
        codex_hooks = {"hooks": {"PreToolUse": [{"command": V1_COMMAND}]}}

        missing = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="no-codex",
                           denied=[".git", ".mir"], targets={"codex": {}})
        w = probe.wiring_report(missing, gp, {"targets": {"codex": {}}})
        check("a declared target whose wiring file is missing is UNWIRED, never a skip",
              w["status"] == "unwired", str(w))
        check("the row names the file the target needs",
              ".codex/hooks.json" in w["failures"][0]["why"], str(w["failures"]))
        r = _run_probe(missing)
        check("a declared target with no wiring file exits 1, not 0 and not 3",
              r.returncode == probe.EXIT_LEAK, f"rc={r.returncode} {r.stdout[-400:]}")

        wired = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="with-codex",
                         denied=[".git", ".mir"], targets={"codex": {}},
                         files={".codex/hooks.json": codex_hooks})
        r = _run_probe(wired)
        check("the same repo WITH .codex/hooks.json verifies clean: the check is the file, "
              "not the target's name",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-400:]}")

        w = probe.wiring_report(wired, gp, {"targets": {"unheard-of": {}}})
        check("a target the probe has no wiring table entry for FAILS: an unknown host must "
              "not verify clean by being unlooked-at (the B1.1 KeyError, at the wiring layer)",
              w["status"] == "unwired" and "TARGET_WIRING" in w["failures"][0]["why"], str(w))

        w = probe.wiring_report(wired, gp, {"targets": {"cursor": {"protocol": "claude"}}})
        _cursor = w["rows"][0]
        check("cursor registers no enforcement file, so its row is a statement and NOT "
              "coverage -- a green row that cannot fail is worse than no row",
              w["status"] == "wired" and _cursor["coverage"] is False, str(w["rows"]))

    check("matcher_covers: a missing matcher means every tool (Claude Code's own rule)",
          probe.matcher_covers(None, "Write") and probe.matcher_covers("", "Write"))
    check("matcher_covers: a bare * means every tool and does not raise",
          probe.matcher_covers("*", "Bash"))
    check("matcher_covers: an unparseable matcher covers nothing",
          not probe.matcher_covers("Write|[", "Write"))

    # ---------------------------------------------------------------- F5: CLI hard-stop
    check("probe_message: exit 0 prints nothing", cli.probe_message(0) is None)
    check("probe_message: exit 1 is the only one that claims a denied path got through",
          "denied path reached" in cli.probe_message(1))
    check("probe_message: exit 3 does NOT claim a leak was found",
          "INCONCLUSIVE" in cli.probe_message(3)
          and "denied path reached" not in cli.probe_message(3), cli.probe_message(3))
    check("probe_message: exit 2 says the harness is unchecked, not that it failed a check",
          "COULD NOT RUN" in cli.probe_message(2))
    check("probe_message: an unrecognised code is not silently reported as success",
          cli.probe_message(9) is not None and "9" in cli.probe_message(9))

    det_conflict = {"proposals": [
        {"pillar": "frontend", "skill": "mir-frontend-react", "evidence": "e", "confidence": "high"},
        {"pillar": "frontend", "skill": "mir-frontend-vue", "evidence": "e", "confidence": "high"},
    ], "conflicts": [], "uncovered": []}
    answers, undecided = cli._answers_from_detection(det_conflict)
    # Not redundant with det["conflicts"]: detect.py puts BOTH candidates into `collapsed`
    # when it records a conflict, so the pillar is undecided here even with conflicts empty.
    # The old setdefault resolved that by dict order and reported nothing.
    check("two proposals for one pillar are UNDECIDED even when conflicts is empty",
          undecided.get("frontend") == ["mir-frontend-react", "mir-frontend-vue"], str(undecided))
    check("an undecided pillar contributes no answer at all",
          "frontend" not in answers, str(answers))

    det_clean = {"proposals": [
        {"pillar": "frontend", "skill": "mir-frontend-react", "evidence": "e", "confidence": "high"},
        {"pillar": "frontend", "skill": "mir-frontend-react", "evidence": "e2", "confidence": "high"},
    ], "conflicts": [], "uncovered": []}
    answers, undecided = cli._answers_from_detection(det_clean)
    check("two proposals naming the SAME skill are decided, not a conflict",
          answers == {"frontend": "mir-frontend-react"} and not undecided, str(undecided))

    def _cli(repo, *extra):
        return subprocess.run([sys.executable, CLI, "init", repo, *extra],
                              capture_output=True, text=True)

    def _untouched(repo):
        return not any(os.path.exists(os.path.join(repo, p))
                       for p in ("AGENTS.md", "CLAUDE.md", ".mir", ".claude"))

    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "two-frameworks")
        os.makedirs(repo)
        with open(os.path.join(repo, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"dependencies":{"react":"19","vue":"3"}}')
        r = _cli(repo)
        check("react+vue is a hard stop (exit 3) with no flags at all",
              r.returncode == 3, f"rc={r.returncode} {r.stderr[-300:]}")
        check("the refusal writes nothing", _untouched(repo), str(os.listdir(repo)))
        check("the refusal lists the pillar's options",
              "mir-frontend-vue" in r.stderr and "mir-frontend-react-next" in r.stderr,
              r.stderr[-400:])
        check("the refusal emits a paste-ready --answers stub",
              "--answers answers.json" in r.stderr and "REPLACE_WITH_ONE_VALUE" in r.stderr,
              r.stderr[-400:])
        r = _cli(repo, "--noninteractive")
        check("--noninteractive does not change the verdict; it never licensed a guess",
              r.returncode == 3, f"rc={r.returncode}")
        r = _cli(repo, "--dry-run")
        check("--dry-run does not sneak past the refusal", r.returncode == 3,
              f"rc={r.returncode}")

    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "uncovered")
        os.makedirs(repo)
        # flutter is a detected stack with no skill. det["uncovered"] was read NOWHERE in the
        # CLI, so this repo used to generate a harness that claimed gates it does not have.
        with open(os.path.join(repo, "pubspec.yaml"), "w", encoding="utf-8") as f:
            f.write("dependencies:\n  flutter:\n    sdk: flutter\n")
        r = _cli(repo)
        check("a detected stack with no skill is a hard stop (exit 3)",
              r.returncode == 3, f"rc={r.returncode} {r.stderr[-300:]}")
        check("the uncovered stop writes nothing", _untouched(repo), str(os.listdir(repo)))
        check("the uncovered stop names the stack and its note",
              "flutter" in r.stderr.lower(), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        # THE OVER-REFUSAL CONTROL. Without it, "refuse always" passes every check above.
        # --dry-run is used so the control cannot fail for a reason that has nothing to do
        # with refusing: the refusal happens BEFORE the dry-run branch, so a CLI that always
        # refused would still be caught here.
        repo = os.path.join(tmp, "single-framework")
        os.makedirs(repo)
        with open(os.path.join(repo, "package.json"), "w", encoding="utf-8") as f:
            f.write('{"dependencies":{"react":"19"}}')
        r = _cli(repo, "--dry-run")
        check("a clean single-framework repo is NOT refused (exit 0)",
              r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
        check("the clean repo proceeds on the detected answer",
              "frontend: mir-frontend-react" in r.stdout, r.stdout[-300:])
        check("--dry-run still writes nothing", _untouched(repo), str(os.listdir(repo)))

    # ------------------------------------------------ P: attack-surface completeness
    #
    # Four defects with one shape: a row that is present, green, and unable to fail. The
    # count it inflates gets read as coverage, so it is worse than no row at all.
    #
    # Every assertion below is the CLASS form -- a question about how the prober CONSTRUCTS
    # attacks, not about how one entry happens to be spelled today. The instance form
    # ("`**/.env*` has a Bash row") goes green the moment someone adds a third pattern, which
    # is precisely how the first of these shipped.

    # ---- the strongest form: does a row distinguish a working guard from a broken one?
    #
    # A row that passes against an allow-everything guard proves nothing about the real one.
    # The mutant is the discriminator: an entry is only covered if some row PASSES the real
    # guard and FAILS the mutant. Anything else is a tautology wearing a checkmark.
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp, roots=("src", "tests"), settings=_settings(GOOD_MATCHER),
                        name="mutant")
        real = _json_probe(repo)
        allow_any = _stub_guard(tmp, "import sys\nsys.exit(0)\n", "allow-any.py")
        mutant = _json_probe(repo, guard=allow_any)

        survivors = [r["path"] for r in mutant["results"]
                     if r["expect_label"] == "BLOCK" and r["ok"]]
        check("no BLOCK row survives an allow-everything guard: not one of them passes for "
              "a reason other than the guard having blocked it",
              not survivors, f"tautological rows: {survivors}")

        blind = []
        for _e in schema.BASELINE_DENIED:
            _good = {r["path"]: r["ok"] for r in _rows_for(real, _e)}
            _bad = {r["path"]: r["ok"] for r in _rows_for(mutant, _e)}
            if not any(_good[p] and not _bad.get(p, True) for p in _good):
                blind.append(_e)
        check("every denied entry yields at least one attack that PASSES the real guard and "
              "FAILS an allow-everything one",
              not blind, f"entries with no discriminating attack: {blind}")

    # ---- 1: a shell redirect for EVERY denied pattern, not just the first one
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="shell")
        rep = _json_probe(repo)
        _bash = [r for r in rep["results"] if r["kind"] == "bash"]
        _uncovered = [d for d in schema.BASELINE_DENIED
                      if probe.is_glob(d) and not any(d in r["why"] for r in _bash)]
        check("every denied PATTERN gets a shell-redirect attack, not only the first",
              not _uncovered, f"patterns with no Bash row: {_uncovered}")

        # MUTATION: the defect was a `break` after the first pattern, which pins the count at
        # one however many patterns the manifest holds. Scaling is the property; a fixed
        # expected number would go stale the moment BASELINE_DENIED changes.
        _m = schema.project_manifest("scale", allowed_write_roots=["."])
        _m["policy"]["denied_paths"] = ["a/p1*", "b/p2*", "c/p3*"]
        _atk = probe.build_attacks(_m, os.path.join(tmp, "nowhere"))
        _covered = {d for d in _m["policy"]["denied_paths"]
                    for a in _atk if a["kind"] == "bash" and d in a["why"]}
        check("MUTATION: shell-redirect coverage SCALES with the pattern count (a `break` "
              "pins it at one)", len(_covered) == 3, str(sorted(_covered)))
        # One row per verb per pattern, never one per INSTANTIATION. Four verbs is a constant
        # factor; nine near-identical redirects into one rule would be the combinatorial shape
        # this bound exists to forbid. `**/.env*` alone instantiates to 9 paths, so an
        # instantiation-driven loop would put 36 rows here instead of 12.
        _shell = [a for a in _atk if a["kind"] == "bash"]
        check("the shell row count stays LINEAR in denied_paths: one question per verb per "
              "rule, not per instantiation",
              len(_shell) <= len(probe._BASH_VERBS) * len(_m["policy"]["denied_paths"]),
              str([a["path"] for a in _shell]))
        check("and every rule still gets each verb exactly once",
              all(len([a for a in _shell if a["path"] == p]) == len(probe._BASH_VERBS)
                  for p in {a["path"] for a in _shell}), str([a["why"] for a in _shell]))

    # ---- B5.1: one Bash attack per verb family, mutation-tested on the GENERATED guard
    #
    # Every generated harness fired `echo x > {path}` and nothing else, so the guard's `tee`,
    # `dd of=` and cp/mv/install branches were exercised only by this repo's own tests against
    # its own in-tree copy. A user's frozen guard could lose all three and their probe would
    # still report clean -- which is the defect class exactly: the maintainer's tree is fine
    # and the installed one has rotted.
    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="verbs",
                        denied=[".git", ".mir"])
        rep = _json_probe(repo)
        _bash = [r for r in rep["results"] if r["kind"] == "bash"]
        check("a generated harness fires every verb family, not just the redirect",
              {r["verb"] for r in _bash} == {v for v, _c in probe._BASH_VERBS},
              str(sorted({r["verb"] for r in _bash})))
        check("each Bash row is labelled with the verb it fired, so a failure names the "
              "branch that lost the target",
              all(r["verb"] in r["why"] for r in _bash), str([r["why"] for r in _bash]))
        r = _run_probe(repo)
        check("THE CONTROL: the real generated guard blocks all four verbs",
              r.returncode == probe.EXIT_CLEAN, f"rc={r.returncode} {r.stdout[-400:]}")

        for _verb, _pairs in _VERB_MUTATIONS:
            mutant, missed = _mutate_guard(repo, tmp, f"blind-{_verb}.py", _pairs)
            check(f"the `{_verb}` mutation still applies to the generated guard -- an anchor "
                  "that has drifted would make the mutation below a tautology",
                  not missed, str(missed))
            r = _run_probe(repo, guard=mutant)
            check(f"MUTATION: a guard blind to `{_verb}` LEAKS and the probe exits 1",
                  r.returncode == probe.EXIT_LEAK, f"rc={r.returncode}")
            _mrep = _json_probe(repo, guard=mutant)
            check(f"MUTATION: and the leak is the `{_verb}` row itself, not collateral -- the "
                  "other three verbs still block",
                  {x.get("verb") for x in _mrep["leaks"]} == {_verb},
                  str([(x["path"], x.get("verb")) for x in _mrep["leaks"]]))

    # ---- B5.2: the prefix over-match nobody tested
    #
    # guard._is_under is `path == root or path.startswith(root + os.sep)`, so `.gitignore` is
    # correctly not under `.git`. Nothing pinned it. A "simplification" to a bare
    # `startswith(root)` would deny `.gitignore`, `.mirror` and `.envelope` -- and the probe
    # would go GREENER, because a guard that blocks more looks safer to a leak-only gate.
    _NOWHERE = os.path.join(tempfile.gettempdir(), "mir-probe-nowhere")
    _base = {"allowed_write_roots": ["."], "denied_paths": list(schema.BASELINE_DENIED)}
    _sibs = probe.sibling_controls(_base, _NOWHERE)
    check("a literal repo-local denied entry yields a derived sibling control",
          {s["path"] for s in _sibs} ==
          {".git" + probe.SIBLING_SUFFIX, ".mir" + probe.SIBLING_SUFFIX},
          str([s["path"] for s in _sibs]))
    check("every sibling control is a POSITIVE control: an over-block is exit 3, not a "
          "quieter pass",
          all(s["expect"] == probe.ALLOW for s in _sibs))
    check("a GLOB entry yields none: a pattern has no single spelling to extend, and "
          "extending one instantiation would test that instantiation, not the rule",
          not [s for s in _sibs if "env" in s["path"] or "settings" in s["path"]])
    check("a ~-rooted entry yields none, deliberately: `~/.sshx` is outside every allowed "
          "root, so deny-by-default blocks it whether or not the prefix over-matches, and "
          "asserting ALLOW there would fail correctly-working code",
          not [s for s in _sibs if "ssh" in s["path"] or s["path"].startswith("~")])
    check("the general form of that exclusion: with narrow roots, an in-repo sibling outside "
          "them is dropped too",
          not probe.sibling_controls(
              {"allowed_write_roots": ["src"], "denied_paths": [".git"]}, _NOWHERE))
    check("a derived sibling the policy denies on its own account is dropped, so the probe "
          "never asserts ALLOW on a path the guard is right to block",
          not probe.sibling_controls(
              {"allowed_write_roots": ["."], "denied_paths": [".env", "**/.env*"]}, _NOWHERE))

    with tempfile.TemporaryDirectory() as tmp:
        repo = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="over-match",
                        denied=[".git", ".mir"])
        rep = _json_probe(repo)
        _fired = [r for r in rep["results"] if "sibling of denied" in r["why"]]
        check("the sibling controls reach the generated harness and pass against the real "
              "guard (the control)",
              _fired and all(r["ok"] for r in _fired), str([r["path"] for r in _fired]))
        check("a sibling control carries no `proves` claim: it is the control, not a row "
              "about a denied entry", all(r["proves"] == "" for r in _fired))

        mutant, missed = _mutate_guard(repo, tmp, "over-match.py", _IS_UNDER_MUTATION)
        check("the _is_under mutation still applies to the generated guard", not missed,
              str(missed))
        r = _run_probe(repo, guard=mutant)
        check("MUTATION: a bare `startswith(root)` exits 3 -- not 0, and not 1",
              r.returncode == probe.EXIT_INCONCLUSIVE, f"rc={r.returncode}")
        _mrep = _json_probe(repo, guard=mutant)
        check("MUTATION: a sibling control is among the blocked positives, so the report "
              "names the over-match rather than an unexplained tightening",
              any("sibling of denied" in x["why"] for x in _mrep["false_blocks"]),
              str([x["path"] for x in _mrep["false_blocks"]]))
        check("MUTATION: nothing leaked -- an over-matching guard blocks MORE, which is "
              "exactly why a leak-only gate would have read it as an improvement",
              not _mrep["leaks"], str([x["path"] for x in _mrep["leaks"]]))

    # ---- 2 and 3: ~-rooted entries, file-shaped entries, and the no-write invariant
    #
    # HOME is redirected at a fixture rather than read from the machine, so the assertions
    # are about the prober's rule and not about whichever dotfiles this developer happens to
    # have. `.ssh` is a directory here and `.gitconfig` a regular file, which is the only
    # distinction the rule turns on.
    with tempfile.TemporaryDirectory() as tmp:
        fake_home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(fake_home, ".ssh"))
        with open(os.path.join(fake_home, ".gitconfig"), "w", encoding="utf-8") as f:
            f.write("[user]\n")
        # PYTHONDONTWRITEBYTECODE, because CPython caches .pyc under ~/Library on macOS when
        # the stdlib is not writable. That is the interpreter writing to HOME, not the probe,
        # and leaving it in would make the no-write assertion below fail for a reason that has
        # nothing to do with the property it exists to check.
        env = dict(os.environ, HOME=fake_home, PYTHONDONTWRITEBYTECODE="1")
        before = _tree(fake_home)

        # NOT named "home": _mk_repo builds tmp/<name>, so a collision would fill the fixture
        # home with the repo's own .mir/ and .claude/ and the no-write assertion below would
        # fail on the test's own doing.
        repo = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="home-repo")
        rep = _json_probe(repo, env=env)
        paths = [r["path"] for r in rep["results"]]

        _tildes = [p for p in paths if p.startswith("~")]
        check("no attack is fired as a literal `~` spelling: the guard expands `~` on both "
              "sides, so a tilde-vs-tilde row proves only that the two expansions agree",
              not _tildes, str(_tildes))
        check("a ~-rooted denied entry is attacked at its EXPANDED absolute path",
              os.path.join(fake_home, ".ssh") in paths, str(paths))

        check("a denied entry that is a regular FILE gets no child-path attack: the path is "
              "unreachable, so the row could never have failed",
              os.path.join(fake_home, ".gitconfig", "child-file") not in paths, str(paths))
        check("a denied entry that is a DIRECTORY keeps its child-path attack",
              os.path.join(fake_home, ".ssh", "child-file") in paths, str(paths))
        _impossible = [p for p in paths
                       if any(os.path.isfile(a) for a in _ancestors(p, repo))]
        check("no attack path is routed through a regular file, so none of them can pass by "
              "being unreachable rather than by being blocked",
              not _impossible, str(_impossible))

        # THE SAFETY INVARIANT. The probe fires paths under a real home directory; it is only
        # safe to do that because it judges paths and never opens one. Asserted against the
        # fixture home, which is the same code path the user's real one takes.
        after = _tree(fake_home)
        check("the probe writes NOTHING into the home it attacks -- an attack is a JSON event "
              "and an exit code, never an open()",
              after == before, f"{before} -> {after}")

    # ---- 4: what a BLOCK proves, stated per row instead of pooled into one number
    with tempfile.TemporaryDirectory() as tmp:
        narrow = _mk_repo(tmp, roots=("src", "tests"), settings=_settings(GOOD_MATCHER),
                          name="narrow")
        wide = _mk_repo(tmp, roots=(".",), settings=_settings(GOOD_MATCHER), name="wide")
        n, w = _json_probe(narrow), _json_probe(wide)

        check("with the repo root NOT an allowed root, the `.git` rows are labelled "
              "deny-by-default: the fallback blocks them whether or not the entry works",
              all(r["proves"] == probe.PROVES_DEFAULT for r in _rows_for(n, ".git")),
              str(_rows_for(n, ".git")))
        check("with the repo root allowed, the same `.git` rows are labelled rule: only the "
              "denied entry can have blocked them",
              all(r["proves"] == probe.PROVES_RULE for r in _rows_for(w, ".git")),
              str(_rows_for(w, ".git")))
        check("a ~-rooted entry is never labelled rule -- a home path cannot be moved inside "
              "the repo, and the report says so rather than implying coverage it lacks",
              all(r["proves"] == probe.PROVES_DEFAULT for r in _rows_for(w, "~/.ssh")),
              str(_rows_for(w, "~/.ssh")))
        check("no BASELINE_DENIED row falls back to firing a pattern's own spelling",
              not [r for r in w["results"] if r["proves"] == probe.PROVES_LITERAL],
              str([r["path"] for r in w["results"] if r["proves"] == probe.PROVES_LITERAL]))
        check("a positive control carries no `proves` claim; it is the control, not a row "
              "about a denied entry",
              all(r["proves"] == "" for r in w["results"] if r["expect_label"] == "ALLOW"))
        rendered = _run_probe(wide).stdout
        check("the rendered report warns, under the headline count, about the rows that count "
              "cannot support",
              "name a denied_paths entry they do" in rendered, rendered[:400])
