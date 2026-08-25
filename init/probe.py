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
the actual guard.py, judged by its exit code. The report also prints what was NOT tested
(Bash beyond simple redirects, MCP writes, apply_patch), because a probe that hides its
blind spots is the laundering both reviewers warned about.

A WIRING phase runs alongside the attacks. Every attack above invokes guard.py DIRECTLY, so
none of them reads .claude/settings.json -- a stale matcher would leave structured writes
completely unguarded while every attack row still said BLOCK. The wiring phase reads the
project's settings and checks that the registered matcher fullmatches every tool the guard
claims to cover, so matcher/tool-coverage drift cannot pass unseen.

Exit codes, and why there are four:
  0  clean
  1  LEAK -- a denied path reaches the target, either because the guard allowed it or
     because the guard is never invoked for the tool that would write it. Same shipping
     fact, so the same gate.
  2  the probe could not run at all (no manifest, no guard)
  3  INCONCLUSIVE -- a positive control was blocked, the guard returned a code outside
     {0,2}, or the wiring could not be confirmed.

3 exists because a guard that blocks EVERYTHING used to exit 0 here: false blocks were
computed and thrown away. That is not fail-safe, it is fail-uninformative. When the positive
control is blocked, every BLOCK row becomes undiscriminating -- you cannot tell "blocked
because denied" from "blocked because the guard is broken" -- and a green report built out of
uninformative rows is exactly the laundering this file's design was written against. Pass
--allow-false-blocks when the over-tightening is deliberate.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")

ALLOW, BLOCK = 0, 2

# Probe exit codes. Kept separate from ALLOW/BLOCK above, which are the GUARD's codes: the
# two vocabularies collide on 0 and 2 and reading one as the other is how "no guard found"
# once looked like "everything blocked".
EXIT_CLEAN, EXIT_LEAK, EXIT_NO_TARGET, EXIT_INCONCLUSIVE = 0, 1, 2, 3


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


def _class_member(body: str) -> str:
    """One character an fnmatch `[...]` class matches, for instantiating it into a real path.

    A negated class needs a character that is NOT listed, so `x` is used unless the class
    excludes it. `a-z` yields `a` because the first character of a range is always in it.
    """
    if body[:1] in ("!", "^"):
        excluded = set(body[1:])
        return next((c for c in "xyz0" if c not in excluded), "x")
    return body[0] if body else "["


def _literalise(stem: str, star: str) -> str:
    """Replace every metacharacter in `stem` with one literal that the pattern matches.

    Substitution happens AT THE METACHARACTER'S OWN POSITION, wherever that is. An earlier
    version only expanded a trailing `*`, so `.claude/settings*.json` produced nothing but
    its own literal spelling and the probe passed while never once attacking
    `.claude/settings.local.json` -- a manifest-derived prober inheriting the manifest's
    blind spot, which is the defect class this file was written to catch.

    `*` becomes one value of the suffix axis, `?` becomes one character, and `[...]` becomes
    one member of the class. An unclosed `[` is left as written because fnmatch reads it
    literally too; the caller drops anything still holding a metacharacter.
    """
    out = []
    i = 0
    while i < len(stem):
        c = stem[i]
        if c == "*":
            out.append(star)
            i += 1
        elif c == "?":
            out.append("x")
            i += 1
        elif c == "[":
            close = stem.find("]", i + 1)
            if close == -1:
                out.append(c)
                i += 1
                continue
            out.append(_class_member(stem[i + 1:close]))
            i = close + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def expand_glob_attacks(entry: str) -> list[str]:
    """Concrete paths a denied pattern must block: the pattern instantiated at several
    depths and with several suffixes.

    `**/` is the depth axis and is expanded wherever it appears, including to the empty
    depth, because it has to mean "at any depth INCLUDING none" -- a `**/.env*` rule only
    ever attacked under `src/` would not notice a guard that leaves the repo-root `.env`
    writable. Every other metacharacter is instantiated in place by _literalise.

    Anything still holding a metacharacter after substitution is dropped rather than fired
    as a literal, because a Write to a path containing `*` is not an attack any agent would
    make and a pass there proves nothing.
    """
    prefix, sep, rest = entry.partition("**/")
    stems = [prefix + d + rest for d in _ATTACK_DEPTHS] if sep else [entry]

    out = []
    for stem in stems:
        for suf in _ATTACK_SUFFIXES:
            out.append(_literalise(stem, suf))
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


# ---------------------------------------------------------------- wiring phase
#
# generate.py's HOOK_TAG, duplicated rather than imported for the same reason is_glob is:
# probe.py is COPIED standalone into .mir/, where there is no generate.py to import from.
HOOK_TAG = "mir-init-guard"

# Claude Code merges the project's settings.json with settings.local.json, so a guard
# registered in either one is genuinely wired. It also merges ~/.claude/settings.json, which
# the probe deliberately does NOT read -- see wiring_report for what that costs.
_SETTINGS_FILES = ("settings.json", "settings.local.json")

# The floor used when the guard's own source cannot be parsed. It is a floor, not the answer:
# the point of reading the guard is that the check self-updates.
_FALLBACK_PATH_FIELD_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Update")


def _string_keys(node) -> list:
    """The string keys of a literal dict/set/list/tuple AST node, or [] if it is not one.

    Keys are read straight off the syntax tree rather than evaluated: the probe must be able
    to inspect a BROKEN guard, and running any part of one is how a guard whose body is
    `sys.exit(0)` would take the probe down with it and report nothing.
    """
    if isinstance(node, ast.Dict):
        elts = node.keys
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elts = node.elts
    else:
        return []
    return [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]


def guarded_tools(guard_path: str) -> list[str]:
    """Every tool the guard claims to cover: its PATH_FIELD_TOOLS keys, plus Bash.

    Read out of the guard's SOURCE by parsing it, never by importing it -- see _string_keys
    for why running the file under test is not an option. Parsing also keeps probe.py
    standalone under .mir/, where there is no shared module to import from.

    Deriving the list from the guard instead of hardcoding it here is what makes the wiring
    check self-updating: add a tool to PATH_FIELD_TOOLS and the matcher is checked against it
    on the next run, with no edit to this file.
    """
    names: list[str] = []
    try:
        with open(guard_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:
        tree = None
    if tree is not None:
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PATH_FIELD_TOOLS" for t in node.targets):
                names = _string_keys(node.value)
                break
    if not names:
        names = list(_FALLBACK_PATH_FIELD_TOOLS)
    # Bash is added unconditionally: its coverage is only partial, but a matcher that does
    # not fire for Bash at all turns that partial coverage into none.
    return names if "Bash" in names else names + ["Bash"]


def matcher_covers(matcher, tool: str) -> bool:
    """Does this hook matcher fire for this tool name?

    fullmatch, not search, because a matcher of `Wri` must not count as covering `Write`;
    a partial match would report coverage the hook does not have. A missing/empty matcher and
    a bare `*` mean "every tool" in Claude Code's config, so they are honoured as such -- and
    `*` has to be special cased anyway, since `re.fullmatch("*", ...)` is not a valid pattern.
    """
    if matcher is None or matcher == "" or matcher == "*":
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False  # an unparseable matcher fires for nothing, so it covers nothing


def find_wiring_entries(repo_root: str) -> list[dict]:
    """Every mir PreToolUse entry in the project's readable settings files.

    An entry counts as mir's if it carries the tag or invokes a guard.py; the second form is
    accepted because a hand-wired or pre-tag harness is still wired, and reporting it as
    missing would send a user to fix something that already works.
    """
    found: list[dict] = []
    for name in _SETTINGS_FILES:
        path = os.path.join(repo_root, ".claude", name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception as e:
            found.append({"file": name, "error": f"{name} did not parse ({e})", "entry": None})
            continue
        hooks = settings.get("hooks") if isinstance(settings, dict) else None
        pre = (hooks or {}).get("PreToolUse") or []
        for entry in pre:
            if not isinstance(entry, dict):
                continue
            blob = json.dumps(entry)
            if HOOK_TAG in blob or "guard.py" in blob:
                found.append({"file": name, "error": None, "entry": entry})
    return found


def wiring_report(repo_root: str, guard_path: str) -> dict:
    """Is the guard actually registered for every tool that can write?

    Every attack in this file pipes an event to guard.py directly, so not one of them reads
    .claude/settings.json. A stale matcher therefore leaves structured writes entirely
    unguarded while every attack row still reports BLOCK. This phase is the only thing that
    asks whether the hook fires at all.

    Three outcomes, and none of them is a skip -- a declared target with no wiring entry is a
    finding, because "we did not look" reads identically to "it is fine" in a green report:

      wired        every guarded tool fullmatches a registered matcher
      unwired      a matcher IS registered and does not fire for some tool -- a proven gap
      unconfirmed  no readable project entry at all. Not called `unwired`, because Claude
                   Code also merges ~/.claude/settings.json, which the probe cannot claim to
                   have read. The probe says what it checked and no more; it still refuses
                   to exit 0.
    """
    tools = guarded_tools(guard_path)
    entries = find_wiring_entries(repo_root)
    live = [e for e in entries if e["entry"] is not None]

    if not live:
        why = "; ".join(e["error"] for e in entries if e["error"]) or (
            "no mir PreToolUse entry in .claude/settings.json or .claude/settings.local.json")
        rows = [{"tool": t, "matcher": None, "ok": False, "why": why} for t in tools]
        return {"status": "unconfirmed", "tools": tools, "rows": rows,
                "failures": rows, "why": why}

    matchers = [(e["file"], e["entry"].get("matcher")) for e in live]
    # On a miss the row still shows what IS registered, because "no matcher fires for Bash"
    # is only actionable next to the matcher that failed to.
    registered = " / ".join("(none)" if m[1] is None else str(m[1]) for m in matchers)
    rows = []
    for t in tools:
        hit = next((m for m in matchers if matcher_covers(m[1], t)), None)
        rows.append({
            "tool": t,
            "matcher": str(hit[1]) if hit else registered,
            "ok": hit is not None,
            "why": "" if hit else "no registered matcher fullmatches this tool",
        })
    failures = [r for r in rows if not r["ok"]]
    return {"status": "unwired" if failures else "wired", "tools": tools, "rows": rows,
            "failures": failures, "why": ""}


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

    # Three disjoint buckets, because they are three different claims and collapsing them is
    # what made a broken guard readable as a clean one:
    #   leak         a denied path reached the target. The shipping blocker.
    #   false_block  a positive control was blocked. NOT "merely too tight": it makes every
    #                BLOCK row above undiscriminating, since a guard that blocks the control
    #                blocks denied and allowed paths alike and the table can no longer tell
    #                the two apart. Inconclusive, not clean.
    #   guard_error  the guard returned a code outside {0,2}. It neither allowed nor blocked,
    #                so the row proves nothing either way.
    guard_errors = [r for r in results if r["got"] == "GUARD-ERROR"]
    leaks = [r for r in results
             if r["expect"] == BLOCK and not r["ok"] and r["got"] != "GUARD-ERROR"]
    false_blocks = [r for r in results
                    if r["expect"] == ALLOW and not r["ok"] and r["got"] != "GUARD-ERROR"]

    return {
        "results": results,
        "leaks": leaks,
        "false_blocks": false_blocks,
        "guard_errors": guard_errors,
        "wiring": wiring_report(repo_root, guard),
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
        lines.append("## INCONCLUSIVE -- a positive control was blocked")
        for r in report["false_blocks"]:
            lines.append(f"- `{r['path']}` ({r['why']}) got {r['got']}")
        lines.append("")
        lines.append("The positive control is the only row in the table that discriminates.")
        lines.append("With it blocked, no BLOCK above distinguishes 'blocked because denied'")
        lines.append("from 'blocked because the guard is broken'. Read the table as unproven.")
        lines.append("")
    if report.get("guard_errors"):
        lines.append("## INCONCLUSIVE -- the guard returned a code outside {0, 2}")
        for r in report["guard_errors"]:
            lines.append(f"- `{r['path']}` ({r['why']}) -- neither allowed nor blocked")
        lines.append("")

    wiring = report.get("wiring") or {}
    if wiring:
        lines.append(f"## Wiring -- is the hook registered? ({wiring['status']})")
        lines.append("")
        lines.append("Every attack above pipes an event to guard.py directly, so none of them")
        lines.append("read .claude/settings.json. These rows are the only ones that do.")
        lines.append("")
        lines.append("| tool | matcher | fires |")
        lines.append("|---|---|---|")
        for r in wiring["rows"]:
            mark = "yes" if r["ok"] else "**NO**"
            # a matcher is an alternation, so its `|` has to be escaped or it splits the row
            shown = str(r["matcher"]).replace("|", "\\|")
            lines.append(f"| {r['tool']} | `{shown}` | {mark} |")
        lines.append("")
        if wiring["failures"]:
            for r in wiring["failures"]:
                lines.append(f"- {r['tool']}: {r['why']}")
            lines.append("")

    lines.append("## Not tested by this probe")
    for n in report["not_tested"]:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("A clean run proves the guard enforces THESE paths. It does not prove the")
    lines.append("untested paths above are safe. Read the two lists together.")
    return "\n".join(lines)


def exit_code(report: dict, allow_false_blocks: bool = False) -> int:
    """Turn a report into the probe's verdict. See the module docstring for the code table.

    LEAK outranks INCONCLUSIVE because a proven leak is the stronger finding: knowing the
    guard is also too tight does not make the leak less real.

    An unwired tool is a LEAK, not a lesser code. The guard is simply never asked about that
    tool's writes, so a denied path reaches the target exactly as if the guard had allowed
    it -- the same shipping fact, so the same gate. `unconfirmed` is the softer code because
    the probe could not read every settings layer Claude Code merges, and reporting "not
    wired" when the truth is "not visible from here" would be its own false claim.
    """
    wiring = report.get("wiring") or {}
    if report["leaks"] or wiring.get("status") == "unwired":
        return EXIT_LEAK
    if report.get("guard_errors") or wiring.get("status") == "unconfirmed":
        return EXIT_INCONCLUSIVE
    if report["false_blocks"] and not allow_false_blocks:
        return EXIT_INCONCLUSIVE
    return EXIT_CLEAN


def main() -> int:
    ap = argparse.ArgumentParser(description="Attack a generated guard with its own manifest.")
    ap.add_argument("--manifest", help="path to .mir/manifest.json (default: <repo>/.mir/manifest.json)")
    ap.add_argument("--repo", default=".", help="repo root the guard runs in")
    ap.add_argument("--guard", default=None, help="path to guard.py (default: the repo's generated guard)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--allow-false-blocks", action="store_true",
                    help="a blocked positive control is deliberate over-tightening here; "
                         "report it but do not return 3")
    args = ap.parse_args()

    repo_root = os.path.abspath(args.repo)
    manifest_path = args.manifest or os.path.join(repo_root, ".mir", "manifest.json")
    if not os.path.exists(manifest_path):
        print(f"no manifest at {manifest_path}", file=sys.stderr)
        return EXIT_NO_TARGET

    guard = resolve_guard(args.guard, repo_root)
    if guard is None:
        # A missing guard must be a loud error, never a silent pass. Without this check a
        # probe with no guard reads every write as blocked (python exits 2 == BLOCK) and
        # reports a clean run that verified nothing.
        print("no guard found (looked for .mir/guard.py and the in-tree copy)",
              file=sys.stderr)
        return EXIT_NO_TARGET

    report = probe(manifest_path, guard, repo_root)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return exit_code(report, args.allow_false_blocks)


if __name__ == "__main__":
    sys.exit(main())
