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


def _settings(matcher, tag="mir-init-guard"):
    return {"hooks": {"PreToolUse": [{
        "matcher": matcher, "_mir": tag,
        "hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"'}],
    }]}}


def _mk_repo(tmp, roots=("src", "tests"), settings=None, name="repo"):
    """A repo with a manifest, a real guard, and (optionally) a hand-built settings.json."""
    import shutil
    repo = os.path.join(tmp, name)
    os.makedirs(os.path.join(repo, ".mir"), exist_ok=True)
    with open(os.path.join(repo, ".mir", "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(schema.project_manifest(name, allowed_write_roots=list(roots)), f)
    shutil.copy(os.path.join(HERE, "guard.py"), os.path.join(repo, ".mir", "guard.py"))
    if settings is not None:
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    return repo


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


def _stub_guard(tmp, body, name):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


def _report(leaks=(), false_blocks=(), guard_errors=(), wiring_status="wired"):
    """A minimal report shaped like probe.probe()'s, for testing exit_code in isolation."""
    return {"leaks": list(leaks), "false_blocks": list(false_blocks),
            "guard_errors": list(guard_errors),
            "wiring": {"status": wiring_status, "rows": [], "failures": [], "tools": []}}


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
              w["status"] == "unconfirmed" and len(w["failures"]) == len(w["tools"]), str(w))
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
        check("one shell row per pattern, not one per instantiation: the count stays linear "
              "in denied_paths",
              len([a for a in _atk if a["kind"] == "bash"]) <= len(_m["policy"]["denied_paths"]),
              str([a["path"] for a in _atk if a["kind"] == "bash"]))

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
