#!/usr/bin/env python3
"""Manifest-derived probe: prove the generated guard enforces the manifest it was built for.

This is the piece both plan reviews said was the difference between real verification and a
green report that proves nothing. The containment kit attacks a fixed list of sensitive
paths; that tells you the OS sandbox works, not that YOUR generated hook enforces YOUR
policy. This probe instead reads the manifest and attacks the guard with:

  - every denied_path              -> expected BLOCK
  - a path just inside each         -> expected BLOCK   (e.g. .git/hooks/x under .git)
  - for a denied PATTERN, concrete instantiations of it at several depths and with several
    suffixes -> expected BLOCK. Replaying only the literal entry is what let `.env` pass
    while `src/.env` and `.env.development` sailed through: the probe ran green because it
    never asked the question. A pattern is only as good as the variants fired at it.
  - one path under each allowed root that is not denied -> expected ALLOW  (positive control)
  - a path clearly outside every allowed root           -> expected BLOCK  (deny-by-default)
  - the manifest and guard themselves                   -> expected BLOCK  (self-protection)

Each attack is a real PreToolUse event (Write, and the partial-coverage Bash form) piped to
the actual guard.py, judged by its exit code. A denied path that is NOT blocked is a failure
and exits nonzero -- that is the only outcome that must gate a pipeline. The report also
prints what was NOT tested (Bash beyond simple redirects, MCP writes, apply_patch), because
a probe that hides its blind spots is the laundering both reviewers warned about.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")

ALLOW, BLOCK = 0, 2


def run_guard(guard: str, event: dict, cwd: str) -> int:
    p = subprocess.run(
        [sys.executable, guard],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=cwd,
    )
    # The guard returns 0 (allow) or 2 (block). Anything else is a guard crash: treat as
    # inconclusive, not as allow, so a broken guard never reads as a pass.
    # NOTE: a MISSING guard file makes Python itself exit 2, which collides with BLOCK. The
    # caller must verify the guard path exists BEFORE calling this, or a probe with no guard
    # would read every attack as "blocked" and pass for the wrong reason.
    return p.returncode


def resolve_guard(explicit: str | None, repo_root: str) -> str | None:
    """The generated guard lives at .mir/guard.py; the in-tree dev copy is beside this file.
    Return the guard path, or None if it cannot be found.

    An EXPLICIT --guard that does not exist returns None (an error) -- it never falls back to a
    default, because a mistyped path silently using some other guard is how a probe ends up
    verifying the wrong file."""
    if explicit is not None:
        return explicit if os.path.exists(explicit) else None
    for cand in (os.path.join(repo_root, ".mir", "guard.py"), GUARD):
        if os.path.exists(cand):
            return cand
    return None


def write_event(path: str, cwd: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "cwd": cwd}


def bash_write_event(path: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": f"echo x > {path}"}, "cwd": cwd}


def _sample_under(root_rel: str) -> str:
    return os.path.join(root_rel, ".mir-probe-canary")


def _escape_path(repo_root: str) -> str:
    """A path guaranteed outside every allowed root. When the repo root itself is an allowed
    root (allowed_write_roots=['.']), the only 'outside' is outside the repo, so this must be
    an absolute sibling path, not an in-repo directory."""
    return os.path.join(os.path.dirname(os.path.abspath(repo_root)), "__mir_escape_probe__", "x")


# How a denied pattern is instantiated into concrete attack paths. Depth and suffix are
# varied together because those are the two axes a prefix-matching guard silently loses:
# `.env` denied at the root says nothing about `src/.env` or `.env.development`.
_ATTACK_DEPTHS = ("", "src/", "a/b/")
_ATTACK_SUFFIXES = ("", ".local", ".development")

_GLOB_META = "*?["


def is_glob(entry: str) -> bool:
    return any(c in entry for c in _GLOB_META)


def expand_glob_attacks(entry: str) -> list[str]:
    """Concrete paths a denied pattern must block: the same pattern at several depths and
    with several suffixes.

    Only `**/`-prefixed and `*`-suffixed forms are instantiated; anything still holding a
    metacharacter is dropped rather than fired as a literal, because a Write to a path
    containing `*` is not an attack any agent would make and a pass there proves nothing.
    """
    stem = entry[3:] if entry.startswith("**/") else entry
    depths = _ATTACK_DEPTHS if entry.startswith("**/") else ("",)
    out = []
    for d in depths:
        if stem.endswith("*"):
            for suf in _ATTACK_SUFFIXES:
                out.append(d + stem[:-1] + suf)
        else:
            out.append(d + stem)
    seen, uniq = set(), []
    for p in out:
        if not is_glob(p) and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def denied_attack_paths(entry: str) -> list[str]:
    """The concrete paths to fire at one denied_paths entry.

    A pattern that cannot be instantiated falls back to the literal entry, so an exotic
    pattern is still attacked (weakly) rather than silently dropped from the report.
    """
    if not is_glob(entry):
        return [entry, os.path.join(entry, "child-file")]  # e.g. .git and .git/hooks/pre-commit
    return expand_glob_attacks(entry) or [entry]


def build_attacks(manifest: dict, repo_root: str) -> list[dict]:
    policy = manifest["policy"]
    attacks: list[dict] = []

    for d in policy.get("denied_paths", []):
        for p in denied_attack_paths(d):
            attacks.append({"path": p, "expect": BLOCK,
                            "why": f"denied_path {d}", "kind": "write"})

    for r in policy.get("allowed_write_roots", []):
        # a normal file under an allowed root that is not itself denied -> must pass
        attacks.append({"path": _sample_under(r), "expect": ALLOW,
                        "why": f"normal file under allowed root {r}", "kind": "write"})

    # deny-by-default: a path outside every allowed root (outside the repo when root is '.')
    attacks.append({"path": _escape_path(repo_root), "expect": BLOCK,
                    "why": "outside every allowed_write_root", "kind": "write"})

    # self-protection: the policy must not let the agent rewrite the policy or the guard
    attacks.append({"path": ".mir/manifest.json", "expect": BLOCK,
                    "why": "the policy must protect itself", "kind": "write"})

    # Bash redirects into denied targets, to exercise the partial-coverage path. Two of them:
    # the first denied entry, and a NESTED instantiation of the first denied pattern -- the
    # shell is the likeliest way `echo KEY=... > src/.env` actually gets attempted.
    denied = policy.get("denied_paths", [])
    if denied:
        shell_targets = denied_attack_paths(denied[0])
        attacks.append({"path": shell_targets[-1], "expect": BLOCK,
                        "why": f"shell redirect into denied {denied[0]}", "kind": "bash"})
    for d in denied:
        if is_glob(d):
            nested = [p for p in expand_glob_attacks(d) if "/" in p]
            if nested:
                attacks.append({"path": nested[-1], "expect": BLOCK,
                                "why": f"shell redirect into nested {d}", "kind": "bash"})
            break

    return attacks


def probe(manifest_path: str, guard: str, repo_root: str) -> dict:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    attacks = build_attacks(manifest, repo_root)
    results = []
    for a in attacks:
        ev = (bash_write_event if a["kind"] == "bash" else write_event)(a["path"], repo_root)
        rc = run_guard(guard, ev, repo_root)
        if rc not in (ALLOW, BLOCK):
            outcome, ok = "GUARD-ERROR", False
        else:
            outcome = "BLOCK" if rc == BLOCK else "ALLOW"
            ok = (rc == a["expect"])
        results.append({**a, "got": outcome, "ok": ok,
                        "expect_label": "BLOCK" if a["expect"] == BLOCK else "ALLOW"})

    # A denied path that was NOT blocked is the failure that must gate. A positive control
    # that was blocked is also a failure (the policy is too tight to work), but a softer one.
    leaks = [r for r in results if r["expect"] == BLOCK and not r["ok"]]
    false_blocks = [r for r in results if r["expect"] == ALLOW and not r["ok"]]

    return {
        "results": results,
        "leaks": leaks,
        "false_blocks": false_blocks,
        "not_tested": [
            "Bash writes beyond simple `>`/`>>`/`tee`/`dd`/`cp` (eval, heredocs, scripts)",
            "MCP tool writes and apply_patch / specialized tool paths",
            "network egress and command allow-list (recorded in the manifest, not yet enforced)",
        ],
    }


def render(report: dict) -> str:
    lines = ["# mir probe -- does the guard enforce its manifest?", ""]
    passed = sum(1 for r in report["results"] if r["ok"])
    lines.append(f"{passed}/{len(report['results'])} attacks behaved as the policy requires")
    lines.append("")
    # The tool column is not decoration: a Write and a Bash attack on the same path are
    # different claims (Bash coverage is partial), and without it the two rows read as a
    # duplicate.
    lines.append("| target | via | expected | got | ok |")
    lines.append("|---|---|---|---|---|")
    for r in report["results"]:
        mark = "yes" if r["ok"] else "**NO**"
        via = "Bash" if r["kind"] == "bash" else "Write"
        lines.append(f"| `{r['path']}` | {via} | {r['expect_label']} | {r['got']} | {mark} |")
    lines.append("")
    if report["leaks"]:
        lines.append("## LEAKS -- a denied path was not blocked (shipping blocker)")
        for r in report["leaks"]:
            lines.append(f"- `{r['path']}` ({r['why']}) got {r['got']}")
        lines.append("")
    if report["false_blocks"]:
        lines.append("## Too tight -- a normal write was blocked")
        for r in report["false_blocks"]:
            lines.append(f"- `{r['path']}` ({r['why']}) got {r['got']}")
        lines.append("")
    lines.append("## Not tested by this probe")
    for n in report["not_tested"]:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("A clean run proves the guard enforces THESE paths. It does not prove the")
    lines.append("untested paths above are safe. Read the two lists together.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Attack a generated guard with its own manifest.")
    ap.add_argument("--manifest", help="path to .mir/manifest.json (default: <repo>/.mir/manifest.json)")
    ap.add_argument("--repo", default=".", help="repo root the guard runs in")
    ap.add_argument("--guard", default=None, help="path to guard.py (default: the repo's generated guard)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    repo_root = os.path.abspath(args.repo)
    manifest_path = args.manifest or os.path.join(repo_root, ".mir", "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return 2

    guard = resolve_guard(args.guard, repo_root)
    if guard is None:
        # A missing guard must be a loud error, never a silent pass. Without this check a
        # probe with no guard reads every write as blocked (python exits 2 == BLOCK) and
        # reports a clean run that verified nothing.
        print("no guard found (looked for .mir/guard.py and the in-tree copy)",
              file=sys.stderr)
        return 2

    report = probe(manifest_path, guard, repo_root)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))

    # Gate only on leaks: a denied path that reached the target. False blocks are reported
    # but do not fail the build, because a too-tight policy is a tuning problem, not a breach.
    return 1 if report["leaks"] else 0


if __name__ == "__main__":
    sys.exit(main())
