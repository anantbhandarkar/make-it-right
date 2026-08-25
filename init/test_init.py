#!/usr/bin/env python3
"""Tests for the mir init tooling. Stdlib only; run: python3 init/test_init.py

The point of these tests is the properties the plan reviews said were make-or-break:
the probe must catch a broken guard (not launder it), detection must distinguish a chain
from a real conflict, and the guard must enforce deny-by-default plus self-protection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import schema
import catalog
import detect as detect_mod
import generate as gen

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def _mk_repo(roots, tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, ".mir"), exist_ok=True)
    m = schema.project_manifest("repo", allowed_write_roots=roots)
    json.dump(m, open(os.path.join(repo, ".mir", "manifest.json"), "w"))
    import shutil
    shutil.copy(os.path.join(HERE, "guard.py"), os.path.join(repo, ".mir", "guard.py"))
    # Register the hook, because the probe now checks that it IS registered. A repo with a
    # manifest and a guard but no hook entry is not a passing harness -- nothing routes a
    # write to the guard, so it enforces nothing. The fixture has to look like an installed
    # harness or it tests a state no real user is ever in.
    #
    # Built from generate.py's own constants rather than a literal: if MATCHER changes and
    # this string does not, the fixture goes stale in exactly the way the wiring phase was
    # added to catch, and the test would start passing for the wrong reason.
    os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
    settings, _ = gen.merge_settings(None, ".mir/guard.py")
    json.dump(settings, open(os.path.join(repo, ".claude", "settings.json"), "w"))
    return repo


def _probe(repo, guard=None):
    cmd = [sys.executable, os.path.join(HERE, "probe.py"), "--repo", repo]
    if guard:
        cmd += ["--guard", guard]
    return subprocess.run(cmd, capture_output=True, text=True)


print("schema")
check("example_manifest validates", schema.validate_manifest(schema.example_manifest()) == [])
check("empty allowed_write_roots is rejected",
      any("allowed_write_roots is empty" in e for e in
          schema.validate_manifest({"mir_manifest_version": 1, "kind": "task-policy",
                                    "policy": {**schema.empty_policy()}})))

print("catalog (derived from the tree)")
cat = catalog.derive_catalog()
check("frontend offers bare React and React+Next",
      {"mir-frontend-react", "mir-frontend-react-next"} <=
      {o["value"] for o in cat["pillars"]["frontend"]["options"]})
check("every pillar has an 'Other' generate option",
      all(any(o.get("generate") for o in p["options"]) for p in cat["pillars"].values()))
res = catalog.resolve({"frontend": "mir-frontend-react-next", "database": "mir-database-postgres"})
check("resolve chains Next -> React -> pillar",
      {"mir-frontend", "mir-frontend-react", "mir-frontend-react-next"} <= set(res["skills"]))
check("devsecops is always resolved", "mir-devsecops" in res["skills"])
check("resolved order is coarse -> fine",
      res["skills"] == sorted(res["skills"], key=lambda s: (s.count("-"), s)))
gap = catalog.resolve({"backend": "__other_backend__"})
check("an 'Other' choice is recorded as a gap, not dropped",
      any(g["choice"] == "__other_backend__" for g in gap["gaps"]))

print("detection (proposes, distinguishes chain from conflict)")
with tempfile.TemporaryDirectory() as tmp:
    open(os.path.join(tmp, "package.json"), "w").write('{"dependencies":{"next":"16","react":"19","pg":"8"}}')
    d = detect_mod.detect(tmp)
    check("next+react collapses to react-next, no conflict",
          d["conflicts"] == [] and any(p["skill"] == "mir-frontend-react-next" for p in d["proposals"]))
with tempfile.TemporaryDirectory() as tmp:
    open(os.path.join(tmp, "package.json"), "w").write('{"dependencies":{"react":"19","vue":"3"}}')
    d = detect_mod.detect(tmp)
    check("react+vue is a real conflict", len(d["conflicts"]) == 1)

print("guard + probe (the make-or-break)")
with tempfile.TemporaryDirectory() as tmp:
    repo = _mk_repo(["src", "tests"], tmp)
    r = _probe(repo)
    check("real guard passes its manifest probe (exit 0)", r.returncode == 0, r.stderr[-200:])

    # a guard that allows everything must be CAUGHT, not laundered
    bad = os.path.join(tmp, "bad-guard.py")
    open(bad, "w").write("import sys\nsys.exit(0)\n")
    r = _probe(repo, guard=bad)
    check("probe catches an allow-everything guard (exit 1)", r.returncode == 1)

    # a missing guard must be a hard error, never a silent pass
    missing = os.path.join(tmp, "does-not-exist.py")
    # point at a repo with no .claude guard and an explicit missing --guard
    empty_repo = os.path.join(tmp, "empty")
    os.makedirs(os.path.join(empty_repo, ".mir"))
    json.dump(schema.project_manifest("e", ["src"]),
              open(os.path.join(empty_repo, ".mir", "manifest.json"), "w"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "probe.py"),
                        "--repo", empty_repo, "--guard", missing],
                       capture_output=True, text=True)
    check("missing guard is a hard error (exit 2), not a pass",
          r.returncode == 2, f"got {r.returncode}")

print("secrets denial (the .env family, at any depth, inside an allowed root)")
with tempfile.TemporaryDirectory() as tmp:
    # allowed_write_roots=["."] on purpose: it is what mir init generates, and it is the only
    # setup where a BLOCK proves the DENY rule fired. Under ["src"] a root-level `.env.x`
    # would block as deny-by-default and the test would pass while the denial stayed broken.
    repo = _mk_repo(["."], tmp)
    guard_py = os.path.join(repo, ".mir", "guard.py")

    def _guard(path):
        ev = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "cwd": repo}
        p = subprocess.run([sys.executable, guard_py], input=json.dumps(ev),
                           text=True, capture_output=True)
        return p.returncode, p.stderr

    def _denied(path):
        """Blocked AND blocked for the right reason -- 'outside every allowed root' would be
        the wrong one here, and would hide a denial that never fired."""
        rc, err = _guard(path)
        return rc == 2 and "denied" in err, f"rc={rc} err={err.strip()[-120:]}"

    for _p in ("src/.env", ".env.development", "a/b/.env.local", ".env"):
        ok, detail = _denied(_p)
        check(f"{_p} is denied inside the allowed root", ok, detail)
    check(".env.local and .env.production still denied (no regression on the old literals)",
          all(_denied(p)[0] for p in (".env.local", ".env.production")))
    check(".envrc is denied (direnv exports real credentials from it)", _denied(".envrc")[0])
    check("src/app.ts is allowed (positive control)", _guard("src/app.ts")[0] == 0,
          _guard("src/app.ts")[1])
    check("a name merely containing 'env' is not over-blocked",
          all(_guard(p)[0] == 0 for p in ("src/environment.ts", "src/env.ts", "env/config.ts")),
          "environment.ts / env.ts / env/ must stay writable")

    # the probe must now FAIL a guard that only honours the literal prefix -- the whole point
    # of extending it is that this class of defect cannot ship green again
    prefix_only = os.path.join(tmp, "prefix-only-guard.py")
    src = open(guard_py, encoding="utf-8").read().replace(
        "def is_glob(entry: str) -> bool:\n    return any(c in entry for c in _GLOB_META)",
        "def is_glob(entry: str) -> bool:\n    return False")
    open(prefix_only, "w").write(src)
    r = _probe(repo, guard=prefix_only)
    check("probe catches a guard that treats a denied pattern as a literal (exit 1)",
          r.returncode == 1, f"rc={r.returncode}")

print("schema stays v1 (a manifest written before globs must not start failing)")
_v1 = {"mir_manifest_version": 1, "kind": "project-policy",
       "policy": {**schema.empty_policy(), "allowed_write_roots": ["."],
                  "denied_paths": [".git", ".env", ".env.local"]}}
check("a v1 manifest with only literal denied_paths still validates",
      schema.validate_manifest(_v1) == [], str(schema.validate_manifest(_v1)))
check("MANIFEST_VERSION is unchanged at 1", schema.MANIFEST_VERSION == 1)

print("generate (thin AGENTS.md, merged settings)")
with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "proj")
    os.makedirs(repo)
    # pre-existing settings with a user hook that must survive the merge
    os.makedirs(os.path.join(repo, ".claude"))
    json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user"}]}]}},
              open(os.path.join(repo, ".claude", "settings.json"), "w"))
    items = gen.plan(repo, ["mir-frontend", "mir-frontend-react", "mir-devsecops"],
                     {"frontend": "mir-frontend-react"}, "2026-01-01T00:00:00Z")
    gen.apply(repo, items)
    agents = open(os.path.join(repo, "AGENTS.md")).read()
    check("AGENTS.md names the pillar", "mir-frontend`" in agents)
    check("AGENTS.md does not inline skill content", "Gate 0" not in agents and "TRIGGER" not in agents)
    settings = json.load(open(os.path.join(repo, ".claude", "settings.json")))
    cmds = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
    check("merge preserves the pre-existing user hook", "echo user" in cmds)
    check("merge adds the mir guard hook", any("guard.py" in c for c in cmds))

print("progressive install (pillars global + modules per project)")
REPO_DIR = os.path.dirname(HERE)
with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "proj")
    os.makedirs(repo)
    res = catalog.resolve({"frontend": "mir-frontend-react-next",
                           "database": "mir-database-postgres"})
    linked = gen.install_project_skills(repo, res["skills"], REPO_DIR)
    check("only tiers/modules are linked locally, never pillars",
          linked and all(s.count("-") > 1 for s in linked), str(linked))
    check("pillars are excluded from the project link set",
          not any(s in linked for s in ("mir-frontend", "mir-database", "mir-devsecops")))
    sk_dir = os.path.join(repo, ".claude", "skills")
    check("links are real symlinks into the repo",
          all(os.path.islink(os.path.join(sk_dir, s)) for s in linked))

    # switching stack must prune the old modules, not accumulate them
    res2 = catalog.resolve({"frontend": "mir-frontend-vue", "database": "mir-database-mongo"})
    gen.install_project_skills(repo, res2["skills"], REPO_DIR)
    removed = gen.prune_project_skills(repo, res2["skills"])
    remaining = sorted(os.listdir(sk_dir))
    check("stale skills are pruned on a stack change",
          "mir-frontend-react-next" in removed and "mir-frontend-react-next" not in remaining,
          f"removed={removed} remaining={remaining}")
    check("the new stack's skills remain",
          "mir-frontend-vue" in remaining and "mir-database-mongo" in remaining, str(remaining))

print("description cap (Claude Code truncates at 1536 chars)")
CAP = 1536
_S = os.path.join(REPO_DIR, "skills")


def _desc(slug):
    """Parse the description properly.

    A regex like description:\\s*"(.*)"\\s*$ with re.DOTALL is GREEDY: it runs past the
    description's closing quote and swallows the argument-hint value too, inflating every
    measurement by 50-95 chars. That bug caused an earlier count here to be wrong. Parse the
    frontmatter as YAML; fall back to a NON-greedy match only if pyyaml is unavailable.
    """
    t = open(os.path.join(_S, slug, "SKILL.md"), encoding="utf-8").read()
    L = t.splitlines()
    c = next(i for i in range(1, len(L)) if L[i].strip() == "---")
    fm = "\n".join(L[1:c])
    try:
        import yaml
        return (yaml.safe_load(fm) or {}).get("description") or ""
    except ImportError:
        import re as _re
        m = _re.search(r'^description:\s*"(.*?)"\s*$', fm, _re.S | _re.M)
        return m.group(1) if m else ""


_all = sorted(d for d in os.listdir(_S) if os.path.isdir(os.path.join(_S, d)))
_cut = [d for d in _all if "SKIP" not in _desc(d)[:CAP]]
check("every skill's SKIP clause survives the 1536-char truncation",
      not _cut, f"{len(_cut)} skills lose SKIP: {_cut[:6]}")
_notrig = [d for d in _all if "TRIGGER" not in _desc(d)[:CAP]]
check("every skill's TRIGGER clause survives the truncation",
      not _notrig, f"{len(_notrig)} lose TRIGGER: {_notrig[:6]}")
_over = [d for d in _all if len(_desc(d)) > CAP]
check("no description exceeds the cap at all", not _over, f"{len(_over)} over: {_over[:6]}")

# -- per-area suites --------------------------------------------------------
# This file stays the single entry point and the single tally. Each area owns exactly
# one init/test_<area>.py defining `run(check)`, so several of them can be written in
# parallel without anyone editing a shared file. `check` is injected rather than
# imported, so a submodule pulls in no test framework and counts into the same total.
#
# Reserved names, one owner each:
#   test_guard.py          enforcement core   (guard.py, schema.py)
#   test_generate.py       generation         (generate.py)
#   test_verify.py         verification + CLI (probe.py, cli.py)
#   test_install_prune.py  installer prune    (install.sh)
#   test_cross.py          end-to-end spans
#
# A test_*.py without run() is a FAIL, not a skip: a suite that loads nothing and says
# nothing is the exact failure this repository exists to prevent. Each module is
# isolated so one that raises costs its own FAIL instead of zeroing everyone else's.
import importlib  # noqa: E402

for _name in sorted(f[:-3] for f in os.listdir(HERE)
                    if f.startswith("test_") and f.endswith(".py") and f != "test_init.py"):
    print(_name)
    try:
        _mod = importlib.import_module(_name)
        _run = getattr(_mod, "run", None)
        check(f"{_name} defines run(check)", _run is not None)
        if _run:
            _run(check)
    except Exception as _e:
        check(f"{_name} ran without raising", False, repr(_e))

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
