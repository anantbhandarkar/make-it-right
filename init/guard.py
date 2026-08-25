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

Paths are canonicalised with realpath, not normpath, and on BOTH sides of every comparison
-- the repo root, the allowed roots, the denied entries, and the target. `open(path, "w")`
follows symlinks, so the guard has to judge the bytes that get overwritten rather than the
name that was typed: without this, `src/link -> ../.mir/guard.py` is approved as an ordinary
`src` write and the guard is overwritten through the alias. The leaf is resolved for that
reason; the parents are resolved because that is what makes a symlinked home directory or a
symlinked source tree compare equal instead of silently failing every containment check.
Canonicalising one side only is the dangerous half-fix: on macOS `tempfile.mkdtemp()` hands
back `/var/...` whose realpath is `/private/var/...`, so a canonical target measured against
an abspath root is under nothing at all and the guard blocks every write while looking like
a security win.

Because canonicalisation moves paths, the deny check runs on the UNION of both spellings --
it blocks if EITHER the lexical (normpath) or the canonical form matches a denied entry --
so no path denied before this change can start slipping through. The allow check is
canonical-only, which is strictly narrower for a symlink pointing out of an allowed root.
The one verdict that intentionally flips from block to allow is a genuinely symlinked
allowed root, which is the case this change exists to make work.

A denied_paths entry is a path prefix, unless it contains a glob metacharacter (`*`, `?`,
`[`), in which case it is matched with fnmatch. Both live in the one list because a prefix
cannot say "at any depth", and that is the only shape a secrets rule has: `.env` as a prefix
denies the repo-root `.env` and nothing else, leaving `src/.env` writable. Literal entries
are still compared exactly as before, so an older manifest denies exactly what it used to.

Blocking is exit code 2 with a reason on stderr, which is the documented Claude Code way to
stop a tool call and feed the reason back to the model.

Coverage, stated honestly because both plan reviewers demanded it:
  - Write / Edit / MultiEdit / NotebookEdit: the file path is a structured field; fully covered.
  - Bash: the write target is inside a shell string. The guard splits the command into
    segments on `;`, `&&`, `||`, `|` and newline, tokenises each, and reads the destinations
    per verb (`>`, `>>`, `tee`, `dd of=`, `cp`, `mv`, `install`); whole-string regexes run
    too and their results are unioned in, so nothing that blocked before stops blocking. A
    shell can still hide a write in ways no parser here will see (eval, a script, a heredoc
    to a variable path). Bash write coverage is PARTIAL and is reported as such; do not read
    a clean Bash result as proof.
  - Everything else (MCP tool writes, apply_patch, specialized tools): NOT covered here.

The guard fails OPEN on its own errors (missing manifest, bad JSON) rather than blocking all
work, and says so on stderr. A policy that bricks the agent when it has a bug is worse than
one that is honest about not being loaded. Fail-closed is a deliberate later option.

Manifest version is the same trade. This file is COPIED standalone into .mir/ and cannot
import schema.py, so it carries GUARD_MANIFEST_VERSION as a constant and compares it against
the manifest's `mir_manifest_version`. On a mismatch it complains on stderr on EVERY
invocation and still allows: a frozen v1 guard cannot know what a v2 manifest means, and a
colleague's v2 manifest must not brick a user's agent inside their own repo. Verification is
where a version mismatch fails closed -- the probe owns that. There is deliberately no
hardcoded fallback denylist for the mismatch case, because a denial the manifest does not
describe is a denial the probe cannot see, which is the exact defect commit e1d4ce1 exists
to prevent.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys

ALLOW = 0
BLOCK = 2

# The manifest shape this copy of the guard understands. Kept in sync with
# schema.MANIFEST_VERSION by a test, because the guard cannot import schema.
GUARD_MANIFEST_VERSION = 1

# Tools whose write target is a clean structured field.
PATH_FIELD_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Update": "file_path",
}

# Best-effort shell write targets, matched against the WHOLE command string. Kept only so
# that the segment parser below can be unioned with them: anything these caught before must
# still be caught. They are not sufficient on their own -- `tee a b` loses `b` because a
# regex captures one group, and the `\s*$` anchor on the cp/mv/install pattern reads the last
# token of the whole line rather than of the copy, so `cp safe .git/config; echo done`
# yielded `done`.
_SHELL_WRITE_PATTERNS = [
    re.compile(r">>?\s*([^\s;|&>]+)"),          # > file , >> file
    re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)"),
    re.compile(r"\bdd\b[^\n]*\bof=([^\s;|&]+)"),
    re.compile(r"\b(?:cp|mv|install)\s+[^\n]*?\s([^\s;|&]+)\s*$"),
]

# Where one command ends and the next begins. `&&` and `||` fall out of splitting on the
# single characters, and an empty segment is dropped, so the two-character operators need no
# case of their own.
_SEGMENT_OPS = ";|&\n"

# A redirection token: `>f`, `>> f`, `2>f`, `&>f`, `>|f`. The capture is empty when the
# filename is a separate token, which is the ordinary `> f` spelling.
_REDIR_TOKEN = re.compile(r"^(?:\d+|&)?>{1,2}\|?(.*)$")

# Wrappers that stand in front of the real verb. Stripping them keeps `sudo tee x` readable
# without pretending this is a shell.
_COMMAND_PREFIXES = {"sudo", "command", "nohup", "time", "env", "exec"}

# Verbs whose destination is the last positional operand.
_DEST_LAST_VERBS = {"cp", "mv", "install"}

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


def split_segments(cmd: str) -> list[str]:
    """Split a command into the individual commands a shell would run.

    Quotes are tracked so an operator inside `"a;b"` is not a split point; a write hidden in
    a quoted string is not the attack this guards against, but splitting there would corrupt
    the surrounding tokens and lose a real target.
    """
    segments: list[str] = []
    cur: list[str] = []
    quote = None
    escaped = False
    for ch in cmd:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            cur.append(ch)
            escaped = True
            continue
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch in _SEGMENT_OPS:
            segments.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    segments.append("".join(cur))
    return [s for s in segments if s.strip()]


def _tokens(segment: str) -> list[str]:
    # shlex removes the quoting so a target comes out as the path the shell would open. An
    # unbalanced quote is a command the shell would reject anyway; fall back to a whitespace
    # split rather than dropping the segment, because dropping it drops its targets.
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return segment.split()


def segment_targets(segment: str) -> list[str]:
    """Write destinations named by one command: its redirections plus its verb's target."""
    toks = _tokens(segment)
    out: list[str] = []
    operands: list[str] = []
    i = 0
    while i < len(toks):
        m = _REDIR_TOKEN.match(toks[i])
        if m:
            dest = m.group(1)
            if not dest:
                i += 1
                dest = toks[i] if i < len(toks) else ""
            # `2>&1` and `>&2` redirect to a descriptor, not to a file called `&1`.
            if dest and not dest.startswith("&"):
                out.append(dest)
            i += 1
            continue
        operands.append(toks[i])
        i += 1

    while operands and (_ASSIGNMENT.match(operands[0])
                        or os.path.basename(operands[0]) in _COMMAND_PREFIXES):
        operands.pop(0)
    if not operands:
        return out

    # basename so an absolute `/usr/bin/tee` is read as `tee`.
    verb = os.path.basename(operands[0])
    args = operands[1:]
    if verb == "tee":
        # EVERY non-flag operand, not the first: `tee allowed.txt .mir/guard.py` writes both.
        out += [a for a in args if not a.startswith("-")]
    elif verb == "dd":
        out += [a[3:] for a in args if a.startswith("of=") and len(a) > 3]
    elif verb in _DEST_LAST_VERBS:
        positional = [a for a in args if not a.startswith("-")]
        # Two operands minimum, so a lone `cp --help`-shaped call names no destination.
        if len(positional) >= 2:
            out.append(positional[-1])
    return out


def find_repo_root(start: str) -> str | None:
    # canonical, not abspath: every comparison downstream is canonical, and a root spelled
    # differently from the targets measured against it is a guard that blocks everything.
    d = canonical(start, os.getcwd())
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


def canonical(p: str, repo_root: str) -> str:
    """The one canonicaliser: the real path a write to `p` would land on.

    realpath resolves the parents AND the leaf, and every comparison in this file puts both
    sides through it. The leaf matters because `open(path, "w")` follows it, so a symlink
    named inside an allowed root must be judged by what it points at. The parents matter
    because resolving them is what makes a symlinked home or a symlinked source tree compare
    equal at all. A nonexistent path is left as written, which is the common case: the guard
    runs before the file is created.
    """
    return os.path.realpath(os.path.join(repo_root, os.path.expanduser(p)))


def resolve(target: str, repo_root: str) -> str:
    """The lexical spelling: absolute and `..`-collapsed, but symlinks left alone.

    Kept because the deny check runs on the union of this and canonical(). It is what the
    guard compared before symlinks were resolved, so keeping it in the union is what makes
    the change structurally incapable of un-blocking something it used to block.
    """
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        target = os.path.join(repo_root, target)
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


def is_denied(target_lex: str, target_can: str, policy: dict, repo_root: str):
    """The deny half, on the UNION of both spellings. Returns a reason or None.

    Both sides of each comparison are canonicalised together or not at all -- lexical target
    against lexical entry, canonical target against canonical entry. Mixing them compares a
    `/private/var` path with a `/var` one and silently matches nothing. The union is what
    makes this monotonically stronger: the lexical arm is exactly the check that shipped, so
    a path denied before is still denied whatever realpath does to it.
    """
    for entry in policy.get("denied_paths", []):
        # A pattern entry is matched, a literal entry is still compared as a prefix. Keeping
        # the literal branch byte-identical is what lets an older manifest deny exactly the
        # set it denied before this split existed.
        if is_glob(entry):
            lex_pats = glob_patterns(entry, repo_root)
            can_pats = [canonical(p, repo_root) for p in lex_pats]
            if matches_glob(target_lex, lex_pats) or matches_glob(target_can, can_pats):
                return f"{target_can} matches a denied pattern ({entry})"
            continue
        if (_is_under(target_lex, resolve(entry, repo_root))
                or _is_under(target_can, canonical(entry, repo_root))):
            return f"{target_can} is under a denied path ({entry})"
    return None


def decide(target_lex: str, target_can: str, policy: dict, repo_root: str) -> tuple[int, str]:
    reason = is_denied(target_lex, target_can, policy, repo_root)
    if reason is not None:
        return BLOCK, reason

    # Allow on the canonical form only. A symlink out of an allowed root must be judged by
    # where it lands, so this arm is narrower than the lexical one it replaces, never wider.
    roots = [canonical(r, repo_root) for r in policy.get("allowed_write_roots", [])]
    if not roots:
        return BLOCK, "no allowed_write_roots in policy; every write is denied"
    for r in roots:
        if _is_under(target_can, r):
            return ALLOW, ""
    return BLOCK, f"{target_can} is outside every allowed_write_root"


def targets_from_event(tool_name: str, tool_input: dict) -> tuple[list[str], bool]:
    """Return (write targets, fully_covered). fully_covered=False means partial coverage."""
    if tool_name in PATH_FIELD_TOOLS:
        field = PATH_FIELD_TOOLS[tool_name]
        val = tool_input.get(field)
        return ([val] if isinstance(val, str) else [], True)

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        found: list[str] = []
        # Union, not replacement: the segment parser finds targets the regexes lose (a second
        # `tee` operand, a `cp` that is not the last command on the line), and the regexes are
        # kept so no command that blocked before this parser existed can start passing.
        for pat in _SHELL_WRITE_PATTERNS:
            found += [m.group(1) for m in pat.finditer(cmd)]
        for seg in split_segments(cmd):
            found += segment_targets(seg)
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

    version = manifest.get("mir_manifest_version")
    if version != GUARD_MANIFEST_VERSION:
        # Loud on every invocation, and still allow. This guard is a frozen copy; it does not
        # know what a newer manifest means, and refusing every write would brick the agent in
        # a repo whose policy is perfectly valid. `mir verify` is where this fails closed.
        print(f"mir-guard: manifest version {version!r} != guard version "
              f"{GUARD_MANIFEST_VERSION}; NOT ENFORCING. Regenerate the guard "
              f"(`mir init`) -- this write is allowed unchecked.", file=sys.stderr)
        return ALLOW

    targets, covered = targets_from_event(tool_name, tool_input)
    for t in targets:
        verdict, reason = decide(resolve(t, repo_root), canonical(t, repo_root),
                                 policy, repo_root)
        if verdict == BLOCK:
            note = "" if covered else " [partial-coverage tool: Bash]"
            print(f"mir-guard: blocked write to {t}{note} -- {reason}", file=sys.stderr)
            return BLOCK
    return ALLOW


if __name__ == "__main__":
    sys.exit(main())
