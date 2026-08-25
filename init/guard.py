#!/usr/bin/env python3
"""PreToolUse guard: the runtime enforcement mir init emits into a project.

Claude Code runs this before a tool call, passing the call as JSON on stdin. The guard
reads the project's .mir/manifest.json and decides whether the write is allowed. It is
data-driven on purpose: mir copies this one file into the project and the policy lives in
the manifest, so regenerating the policy never rewrites the guard.

Decision, matching schema.py's model:
  - block if the target is under any denied_path (even inside an allowed root)
  - else allow if the target is under an allowed_write_root
  - else block (deny-by-default outside the allowed roots)

A denied_paths entry is a path prefix, unless it contains a glob metacharacter (`*`, `?`,
`[`), in which case it is matched with fnmatch. Both live in the one list because a prefix
cannot say "at any depth", and that is the only shape a secrets rule has: `.env` as a prefix
denies the repo-root `.env` and nothing else, leaving `src/.env` writable. Literal entries
are still compared exactly as before, so an older manifest denies exactly what it used to.

Blocking is exit code 2 with a reason on stderr, which is the documented Claude Code way to
stop a tool call and feed the reason back to the model.

Coverage, stated honestly because both plan reviewers demanded it:
  - Write / Edit / MultiEdit / NotebookEdit: the file path is a structured field; fully covered.
  - Bash: the write target is inside a shell string. The guard catches the common explicit
    forms (`> f`, `>> f`, `tee f`, `dd of=f`) but a shell can hide a write in ways a regex
    will not see (eval, a script, a heredoc to a variable path). Bash write coverage is
    PARTIAL and is reported as such; do not read a clean Bash result as proof.
  - Everything else (MCP tool writes, apply_patch, specialized tools): NOT covered here.

The guard fails OPEN on its own errors (missing manifest, bad JSON) rather than blocking all
work, and says so on stderr. A policy that bricks the agent when it has a bug is worse than
one that is honest about not being loaded. Fail-closed is a deliberate later option.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys

ALLOW = 0
BLOCK = 2

# Tools whose write target is a clean structured field.
PATH_FIELD_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Update": "file_path",
}

# Best-effort shell write targets. Documented as partial.
_SHELL_WRITE_PATTERNS = [
    re.compile(r">>?\s*([^\s;|&>]+)"),          # > file , >> file
    re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)"),
    re.compile(r"\bdd\b[^\n]*\bof=([^\s;|&]+)"),
    re.compile(r"\b(?:cp|mv|install)\s+[^\n]*?\s([^\s;|&]+)\s*$"),
]


def find_repo_root(start: str) -> str | None:
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".mir")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_manifest(repo_root: str):
    path = os.path.join(repo_root, ".mir", "manifest.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve(target: str, repo_root: str) -> str:
    """Absolute, symlink-and-.. -collapsed path for a manifest entry or a tool target."""
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        target = os.path.join(repo_root, target)
    # normpath (not realpath) so we judge the path the tool named, and a nonexistent
    # target still resolves. A `..` escape collapses here and is caught by the root checks.
    return os.path.normpath(target)


def _is_under(path: str, root: str) -> bool:
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    if path == root:
        return True
    return path.startswith(root + os.sep)


# The characters that turn a denied_paths entry from a prefix into a pattern. `[` counts
# because fnmatch reads it as a character class, so an entry holding one is already a
# pattern whether or not the author meant it that way.
_GLOB_META = "*?["


def is_glob(entry: str) -> bool:
    return any(c in entry for c in _GLOB_META)


def glob_patterns(entry: str, repo_root: str) -> list[str]:
    """Absolute fnmatch patterns for one glob entry.

    `**/` is emitted both as written and elided, because it has to mean "at any depth
    INCLUDING none" and plain fnmatch requires the literal `/` to be present -- without the
    elided form `**/.env*` would catch `src/.env` but miss the repo-root `.env`.
    """
    pat = os.path.expanduser(entry)
    if not os.path.isabs(pat):
        pat = os.path.join(repo_root, pat)
    pats = [pat]
    if "**/" in pat:
        pats.append(pat.replace("**/", ""))
    return pats


def matches_glob(target_abs: str, pats: list[str]) -> bool:
    """True if the target or any ancestor directory matches one of the patterns.

    Ancestors are walked so a matched directory denies everything beneath it, which is the
    same containment a prefix entry gets from _is_under; without it a pattern would block
    `secrets.d` but not `secrets.d/key`.
    """
    cur = os.path.normpath(target_abs)
    while True:
        for p in pats:
            if fnmatch.fnmatch(cur, p):
                return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def decide(target_abs: str, policy: dict, repo_root: str) -> tuple[int, str]:
    for entry in policy.get("denied_paths", []):
        # A pattern entry is matched, a literal entry is still compared as a prefix. Keeping
        # the literal branch byte-identical is what lets an older manifest deny exactly the
        # set it denied before this split existed.
        if is_glob(entry):
            if matches_glob(target_abs, glob_patterns(entry, repo_root)):
                return BLOCK, f"{target_abs} matches a denied pattern ({entry})"
            continue
        d = resolve(entry, repo_root)
        if _is_under(target_abs, d):
            return BLOCK, f"{target_abs} is under a denied path ({d})"

    roots = [resolve(r, repo_root) for r in policy.get("allowed_write_roots", [])]
    if not roots:
        return BLOCK, "no allowed_write_roots in policy; every write is denied"
    for r in roots:
        if _is_under(target_abs, r):
            return ALLOW, ""
    return BLOCK, f"{target_abs} is outside every allowed_write_root"


def targets_from_event(tool_name: str, tool_input: dict) -> tuple[list[str], bool]:
    """Return (write targets, fully_covered). fully_covered=False means partial coverage."""
    if tool_name in PATH_FIELD_TOOLS:
        field = PATH_FIELD_TOOLS[tool_name]
        val = tool_input.get(field)
        return ([val] if isinstance(val, str) else [], True)

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        found: list[str] = []
        for pat in _SHELL_WRITE_PATTERNS:
            found += [m.group(1) for m in pat.finditer(cmd)]
        # deduplicate, drop obvious non-paths
        seen, out = set(), []
        for t in found:
            t = t.strip().strip("'\"")
            if t and t not in seen and not t.startswith("-"):
                seen.add(t)
                out.append(t)
        return (out, False)  # Bash is never "fully covered"

    return ([], True)  # tools that do not write files: nothing to guard here


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception as e:
        print(f"mir-guard: could not parse hook input, allowing ({e})", file=sys.stderr)
        return ALLOW

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    cwd = event.get("cwd") or os.getcwd()

    repo_root = find_repo_root(cwd)
    if repo_root is None:
        print("mir-guard: no .mir/ found above cwd, allowing (policy not loaded)", file=sys.stderr)
        return ALLOW
    try:
        manifest = load_manifest(repo_root)
        policy = manifest["policy"]
    except Exception as e:
        print(f"mir-guard: could not load policy, allowing ({e})", file=sys.stderr)
        return ALLOW

    targets, covered = targets_from_event(tool_name, tool_input)
    for t in targets:
        target_abs = resolve(t, repo_root)
        verdict, reason = decide(target_abs, policy, repo_root)
        if verdict == BLOCK:
            note = "" if covered else " [partial-coverage tool: Bash]"
            print(f"mir-guard: blocked write to {t}{note} -- {reason}", file=sys.stderr)
            return BLOCK
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
