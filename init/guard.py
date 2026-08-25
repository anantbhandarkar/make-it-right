#!/usr/bin/env python3
"""PreToolUse guard: the runtime enforcement mir init emits into a project.

A host runs this before a tool call, passing the call as JSON on stdin. The guard reads the
project's .mir/manifest.json and decides whether the write is allowed. It is data-driven on
purpose: mir copies this one file into the project and the policy lives in the manifest, so
regenerating the policy never rewrites the guard.

`--protocol` says WHICH host is asking. It is required, and a missing or unknown value is a
hard error -- exit 3, enforcing nothing and saying so. There is deliberately no default.
Defaulting to `claude` is precisely how a mis-wired Codex hook runs the Claude adapter, finds
no field it recognises, extracts zero write targets, and allows every write while reporting a
clean run. An exit 3 is visible; a silent zero-target parse is not.

The policy engine below is target-neutral and unchanged: decide(), resolve(), canonical(),
_is_under(), find_repo_root() and load_manifest() do not know a protocol exists. What the
protocol selects is only the two ends -- how an event is read, and how a verdict is said:

  claude       stdin fields tool_name / tool_input. BLOCK is exit 2 + stderr.
  cursor       Claude Code's configuration read by a different product, so: Claude's
               protocol, exactly. It has no hook of its own; see targets/cursor.py.
  codex        the same stdin field names as Claude, plus `apply_patch`, whose body names
               its own files in `*** Add File:` / `*** Update File:` / `*** Delete File:`
               headers. BLOCK is exit 2 + stderr. An apply_patch body that will NOT parse
               returns deny -- never `ask`, which Codex treats as unsupported and continues
               past, so `ask` is fail-open wearing a careful decision's clothes.
  antigravity  toolCall.name / toolCall.args. BLOCK is `{"decision": "deny"}` on STDOUT with
               EXIT 0, because the host ignores the exit code entirely. An adapter that
               "just exits 2 as well" here has enforced nothing.

The antigravity adapter is FAIL-CLOSED and every other adapter is fail-open, and that
difference is a fact about the hosts rather than a preference. Claude Code surfaces a hook
error to the user, so failing open there is loud. Antigravity LOGS a hook error and SKIPS the
hook -- nonzero exit, uncompilable matcher, timeout, any of them -- and the write proceeds,
so failing open there is silent. Hence the top-level catch that still emits deny-JSON, no
dependency on anything that may be missing (this file is stdlib-only and standalone), and no
network, subprocess or filesystem work beyond reading one JSON manifest, which keeps it far
under the 30s default hook timeout. A timed-out hook is a skipped hook.

One exception is stated rather than hidden: a manifest version this guard does not understand
fails OPEN on EVERY protocol, antigravity included. See GUARD_MANIFEST_VERSION below.

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

Coverage, stated honestly because both plan reviewers demanded it:
  - Write / Edit / MultiEdit / NotebookEdit: the file path is a structured field; fully covered.
  - Bash: the write target is inside a shell string. The guard splits the command into
    segments on `;`, `&&`, `||`, `|`, a background `&` and newline, tokenises each, and reads
    the destinations per verb (`>`, `>>`, `&>f`, `>&f`, `tee`, `dd of=`, and `cp`/`mv`/
    `install` including `-t DIR` and the per-source path each operand of a multi-source copy
    lands on); whole-string regexes run too and their results are unioned in, so nothing that
    blocked before stops blocking. Bash write coverage is PARTIAL and is reported as such; do
    not read a clean Bash result as proof. Named, because a limitation nobody wrote down is
    indistinguishable from one nobody found -- what this parser still does NOT see:
      * indirection: `eval`, `bash -c`, a script file, a path in `$VAR` or `$(...)`, a
        heredoc whose target is computed. The guard reads tokens, and none of these carry
        the path as a token.
      * `cp a b` where `b` is an existing DIRECTORY: the write is `b/a`, and the guard names
        only `b`. Multi-source (`cp a b dir/`) and a trailing `/` are read as directories
        because the syntax says so; the two-operand form does not, and deciding it means
        stat-ing a path the command has not created yet.
      * verbs that write a file they do not name as a destination operand: `sed -i`,
        `tar -x`, `git checkout`, `python -c`, an editor, any compiler's `-o`-less default.
        Each would need its own flag grammar, and a wrong one over-blocks rather than fails
        quietly, so they are listed rather than guessed at.
      * a shell function, alias, or `$PATH` shadow that renames a verb: `tee` is read as tee
        by its name, and a name is not a promise.
  - apply_patch (codex only): the patch headers name the files, so those ARE read. A body
    that will not parse is denied rather than allowed.
  - antigravity tools OUTSIDE the known write set: reached anyway, because the matcher is
    `*`. The guard reads path-shaped and command-shaped arguments off them and reports
    PARTIAL coverage, which over-blocks rather than allowing an unread call.
  - Everything else (MCP tool writes, specialized tools on the exit-code hosts): NOT covered.

On its own errors -- missing manifest, bad JSON, an unreadable event -- the guard fails OPEN
on the exit-code hosts and says so on stderr. A policy that bricks the agent when it has a
bug is worse than one that is honest about not being loaded, and on those hosts the failure
is visible. On antigravity it fails CLOSED, for the reason at the top of this docstring: there
the same failure is invisible and the write lands.

Manifest version is the same trade, and it is the one place antigravity does NOT fail closed.
This file is COPIED standalone into .mir/ and cannot import schema.py, so it carries
GUARD_MANIFEST_VERSION as a constant and compares it against the manifest's
`mir_manifest_version`. On a mismatch it complains on stderr on EVERY invocation and still
allows -- on every protocol: a frozen v1 guard cannot know what a v2 manifest means, and a
colleague's v2 manifest must not brick a user's agent inside their own repo. Verification is
where a version mismatch fails closed -- the probe owns that. There is deliberately no
hardcoded fallback denylist for the mismatch case, because a denial the manifest does not
describe is a denial the probe cannot see, which is the exact defect commit e1d4ce1 exists
to prevent.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys

ALLOW = 0
BLOCK = 2

# A third code, and it is not a verdict. 3 means "this guard was not told which host is
# asking, so it read nothing and decided nothing". Deliberately NOT 2: 2 is a block, and a
# guard that blocks every write because its command line is wrong would look like a working
# policy while the real finding -- a stale hook registration -- stayed invisible. Only exit 2
# stops a tool call on Claude Code and Codex, so a cached v1 command that exits 3 is fail-open
# until the session restarts. probe.py's `(command)` row is what makes that a wiring FAILURE
# rather than a silent one.
PROTOCOL_ERROR = 3

# The manifest shape this copy of the guard understands. Kept in sync with
# schema.MANIFEST_VERSION by a test, because the guard cannot import schema.
GUARD_MANIFEST_VERSION = 2

# The hosts this guard can answer for. A protocol outside this set is a hard error, never a
# fallback -- see the module docstring for why a default here allows everything quietly.
#
# `cursor` is present and speaks Claude's semantics because Cursor reads Claude Code's
# configuration; it registers no hook of its own, so this entry exists for a hand-wired
# command rather than for anything `mir init` writes.
PROTOCOLS = ("claude", "codex", "antigravity", "cursor")

# Which channel each host reads a verdict off. This mirrors probe.PROTOCOL_CHANNEL and is
# duplicated for the same reason is_glob is: this file is COPIED standalone into .mir/ and has
# nothing to import from. The two are pinned to each other by init/test_guard.py.
STDOUT_PROTOCOLS = ("antigravity",)

# Hosts where a failed hook is a SKIPPED hook, so the adapter must answer deny rather than
# nothing. Listed rather than inferred: inheriting the wrong posture here is silent, and
# silence is the whole failure mode.
FAIL_CLOSED_PROTOCOLS = ("antigravity",)

# Tools whose write target is a clean structured field.
#
# `Update` is KEPT DELIBERATELY, and this is the note that exists so the next person does not
# re-open it. It could not be found as a live tool: it is absent from the Claude Code tools
# reference, and a scan of local session transcripts shows zero events with tool_name
# "Update" (the near miss is `TaskUpdate`, which edits a task, not a file). MCP tools arrive
# namespaced as `mcp__server__tool`, so a bare `Update` cannot come from one either.
#
# It stays because the two failure directions are not symmetric. A key for a tool that never
# fires costs one never-taken alternative in the hook matcher and one always-passing row in
# the probe's wiring table -- it widens the matcher and denies nothing. A MISSING key for a
# tool that does fire is the worst shape in this codebase: targets_from_event returns
# ([], fully_covered=True), so the write is allowed unread AND reported as fully covered.
# Cheap insurance against a rename beats tidiness on a frozen file that is copied into user
# repos and only refreshed by `mir init`.
#
# If a later reader does remove it, four other places name it and go stale together:
# generate.MATCHER, test_verify.GOOD_MATCHER, probe._FALLBACK_PATH_FIELD_TOOLS, and the
# coverage table in README.md.
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
# case of their own. `&` is the exception and is handled in split_segments: it separates as
# `&&` and as a trailing background `&`, but in `2>&1`, `>&2` and `&>log` it is one half of a
# redirection operator and splitting there invents a command out of a file descriptor.
_SEGMENT_OPS = ";|&\n"

# A redirection token: `>f`, `>> f`, `2>f`, `&>f`, `>|f`, `2>&1`, `>&f`. The capture is empty
# when the filename is a separate token, which is the ordinary `> f` spelling.
_REDIR_TOKEN = re.compile(r"^(?:\d+|&)?>{1,2}\|?(.*)$")

# Wrappers that stand in front of the real verb. Stripping them keeps `sudo tee x` readable
# without pretending this is a shell.
_COMMAND_PREFIXES = {"sudo", "command", "nohup", "time", "env", "exec"}

# Verbs that copy operands to a destination. The destination is the last positional operand
# UNLESS `-t`/`--target-directory` names it, which inverts the order.
_COPY_VERBS = {"cp", "mv", "install"}

# Short options of those verbs that swallow the NEXT token. Without this list `install -m 644
# a dir/` reads `644` as a source and invents a write to `dir/644`. Only the arg-taking
# options of cp/mv/install are listed; a flag wrongly listed here eats a real operand, so the
# set stays small and boring: -t/--target-directory, -m mode, -o owner, -g group, -S suffix.
_COPY_OPTS_WITH_ARG = {"-t", "-m", "-o", "-g", "-S"}

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


def split_segments(cmd: str) -> list[str]:
    """Split a command into the individual commands a shell would run.

    Quotes are tracked so an operator inside `"a;b"` is not a split point; a write hidden in
    a quoted string is not the attack this guards against, but splitting there would corrupt
    the surrounding tokens and lose a real target.

    `&` is decided by its neighbours, because the same character is a separator in `cmd &`
    and in `a && b` but part of one operator in `2>&1`, `>&2`, `>&file` and `&>file`. A `&`
    welded to a `>` on either side stays in the segment; every other `&` splits, which keeps
    `&&` splitting (its second `&` is followed by a space, not a `>`) and leaves the empty
    segment between them to be dropped.
    """
    segments: list[str] = []
    cur: list[str] = []
    quote = None
    escaped = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        i += 1
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
            if ch == "&" and ((cur and cur[-1] == ">") or cmd[i:i + 1] == ">"):
                cur.append(ch)   # `2>&1`, `>&2`, `&>log`: one operator, not two commands
                continue
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


def _redirect_dest(rest: str, toks: list[str], i: int):
    """(file written by one redirection, index of its last token). "" when it writes no file.

    `>&` is the fork. After it, a bare number or `-` is a file DESCRIPTOR -- `2>&1` duplicates
    stderr onto stdout and opens nothing -- but any other word is a FILE, because bash reads
    `>&word` as "send both streams to word". Dropping every `&` form, which is what "does it
    start with & ?" did, is exactly what let `echo x >&.git/config` through.
    """
    dup = False
    if not rest or rest == "&":
        dup = rest == "&"
        i += 1                                     # `> f` / `>& f`: filename is its own token
        rest = toks[i] if i < len(toks) else ""
    elif rest.startswith("&"):
        dup, rest = True, rest[1:]
    if dup and (rest == "-" or rest.isdigit()):
        return "", i
    return rest, i


def segment_targets(segment: str) -> list[str]:
    """Write destinations named by one command: its redirections plus its verb's target."""
    toks = _tokens(segment)
    out: list[str] = []
    operands: list[str] = []
    i = 0
    while i < len(toks):
        m = _REDIR_TOKEN.match(toks[i])
        if m:
            dest, i = _redirect_dest(m.group(1), toks, i)
            if dest:
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
    elif verb in _COPY_VERBS:
        out += copy_dests(args)
    return out


def _copy_operands(args: list[str]):
    """(sources, target directory or None) for a cp/mv/install argument list.

    Options are walked rather than filtered out, because an option's ARGUMENT is not an
    operand: `install -m 644 a dir/` has one source, not two. Clusters and attached values
    (`-tDIR`, `-m644`, `-rt DIR`) are handled in the same pass, since a missed `-t` puts the
    destination back at the end of the list where it is not.

    `--target-directory` is the only long option read, because it is the only one that moves
    the destination. A long option given its value as a SEPARATE token (`--suffix bak`) leaves
    that value counted as a source, which can only add a destination that is not written --
    over-blocking, never under -- so it is not worth a table of every option's arity.
    """
    sources: list[str] = []
    target_dir = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":                                  # everything after is an operand
            sources += args[i + 1:]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == "--target-directory":
                if not eq:
                    i += 1
                    val = args[i] if i < len(args) else ""
                target_dir = val
            i += 1
            continue
        if a.startswith("-") and a != "-":
            for pos, letter in enumerate(a[1:]):
                if "-" + letter not in _COPY_OPTS_WITH_ARG:
                    continue
                val = a[pos + 2:]
                if not val:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if letter == "t":
                    target_dir = val
                break
            i += 1
            continue
        sources.append(a)
        i += 1
    return sources, target_dir


def _looks_like_dir(dest: str) -> bool:
    # Only what the SYNTAX settles. `cp a b` where b is an existing directory also writes
    # `b/a`, but reading that off the filesystem means stat-ing a path the command has not
    # created yet, so it is a documented limitation instead of a guess.
    return dest.endswith("/") or os.path.basename(dest) in (".", "..")


def copy_dests(args: list[str]) -> list[str]:
    """Every path a cp/mv/install invocation writes.

    `cp a b dir/` writes `dir/a` and `dir/b`, not `dir`. Naming only the last operand still
    blocks a denied DIRECTORY, because that blocks by prefix -- but it misses a denied GLOB,
    and "at any depth, by filename" is the only shape a secrets rule has, so `cp .env.local
    allowed/` slipped past a `**/.env*` entry that exists to stop exactly that.
    """
    # The floor: exactly what the pre-fix parser named -- the last operand that does not look
    # like a flag. Recomputed rather than replaced, so no command that blocks today stops
    # blocking even where the parse below reads the command more correctly. `cp -t src X`
    # copies X INTO src, so X is a source and reporting it is wrong; it is reported anyway,
    # because over-reporting a source can only over-block, and quietly un-blocking a path is
    # the one direction this file may not move.
    dests: list[str] = []
    legacy = [a for a in args if not a.startswith("-")]
    if len(legacy) >= 2:
        dests.append(legacy[-1])

    sources, target_dir = _copy_operands(args)
    if target_dir is not None:
        # `-t` says "directory" in so many words, so one source is enough: `cp -t build a`
        # writes build/a.
        dest, srcs, is_dir = target_dir, sources, True
    elif len(sources) >= 2:
        # Two operands minimum, so a lone `cp --help`-shaped call names no destination.
        dest, srcs = sources[-1], sources[:-1]
        is_dir = len(srcs) > 1 or _looks_like_dir(dest)
    else:
        return dests
    dests.append(dest)
    if is_dir:
        for s in srcs:
            base = os.path.basename(s.rstrip("/"))
            if base and base not in (".", ".."):
                dests.append(os.path.join(dest, base))
    # Deduplicated because the floor and the parse agree on the ordinary `cp a b`, and a
    # target reported twice is a reason printed twice.
    return [d for i, d in enumerate(dests) if d and d not in dests[:i]]


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


def shell_targets(cmd: str) -> list[str]:
    """Every write destination a shell command names. Split out of targets_from_event so the
    antigravity `run_command` tool reads through exactly the same parser as Bash does -- a
    second copy of this would be a second place to lose a verb family from."""
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
    return out


def targets_from_event(tool_name: str, tool_input: dict) -> tuple[list[str], bool]:
    """Return (write targets, fully_covered). fully_covered=False means partial coverage."""
    if tool_name in PATH_FIELD_TOOLS:
        field = PATH_FIELD_TOOLS[tool_name]
        val = tool_input.get(field)
        return ([val] if isinstance(val, str) else [], True)

    if tool_name == "Bash":
        return (shell_targets(tool_input.get("command", "")), False)

    return ([], True)  # tools that do not write files: nothing to guard here


# ------------------------------------------------------------------ codex: apply_patch
#
# Codex's apply_patch carries its own file list in the patch header, which is the ONLY place
# the write target appears -- there is no structured field to read. The three verbs are
# spelled exactly as the format writes them.
_PATCH_FILE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.M)
# A rename writes a SECOND path. Missing it would let `*** Update File: src/x` +
# `*** Move to: .git/config` land the write somewhere the first header never named.
_PATCH_MOVE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$", re.M)

# Where a patch body turns up. Shape-tolerant because the field name is the host's, not mir's;
# what matters is finding the body, not knowing which key held it.
_PATCH_KEYS = ("patch", "input", "diff", "content", "body", "command", "arguments")
_APPLY_PATCH_TOOLS = ("apply_patch", "applypatch", "apply-patch")
_PATCH_SENTINEL = "*** Begin Patch"


def patch_body(tool_name: str, tool_input: dict):
    """The apply_patch body in this event, or None if this is not an apply_patch event.

    None and "" are different answers and the caller depends on it: None means "some other
    tool, judge it normally", "" means "an apply_patch call whose body is missing", which is
    unparseable and therefore denied. Collapsing them would turn every unreadable patch into
    an ordinary allow.
    """
    named = str(tool_name or "").strip().lower() in _APPLY_PATCH_TOOLS
    if isinstance(tool_input, dict):
        for key in _PATCH_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and _PATCH_SENTINEL in val:
                return val
    if not named:
        return None
    for key in _PATCH_KEYS:
        val = (tool_input or {}).get(key) if isinstance(tool_input, dict) else None
        if isinstance(val, str):
            return val
    return ""


def patch_targets(body: str) -> tuple[list[str], bool]:
    """(paths the patch writes, parsed). parsed=False means the body named no file at all.

    A body that names no file is NOT an empty write set. It is a patch this guard could not
    read, and the caller turns that into a DENY -- never an allow, and never `ask`: Codex
    treats an unsupported decision as "continue", so answering `ask` is allowing the write
    while looking like a careful deferral. An unread write that reads as allowed is exactly
    the laundering this repository exists to prevent.
    """
    paths = _PATCH_FILE.findall(body or "") + _PATCH_MOVE.findall(body or "")
    out, seen = [], set()
    for p in paths:
        p = p.strip().strip("'\"")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out, bool(out)


# --------------------------------------------------------------- antigravity: toolCall
#
# The three obvious write tools, by name. This is NOT the matcher -- the matcher is `*`, for
# the reason in targets/antigravity.py -- it is only the set whose argument shape is known.
# A tool outside it still arrives here and is read generically rather than allowed unseen.
_AG_FILE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
_AG_COMMAND_TOOLS = ("run_command",)

# Argument keys that name a file, and keys that name a command line. Several spellings each,
# because these are the host's names and a rename must degrade to partial coverage rather than
# to silence.
_AG_PATH_KEYS = ("TargetFile", "target_file", "TargetFilePath", "AbsolutePath", "file_path",
                 "FilePath", "notebook_path", "path", "Path")
_AG_COMMAND_KEYS = ("CommandLine", "command_line", "command", "Command")


def _first_str(args: dict, keys) -> str:
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _all_strs(args: dict, keys) -> list[str]:
    out = []
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v)
        elif isinstance(v, list):
            out += [x for x in v if isinstance(x, str) and x.strip()]
    return out


def antigravity_call(event: dict) -> tuple[str, dict]:
    """(tool name, args) from an Antigravity event, falling back to the Claude shape.

    The fallback is not politeness. probe.py fires `tool_name`/`tool_input` events under EVERY
    protocol, so an adapter that only understood `toolCall` would extract zero targets from
    every probe row, allow all of them, and report a table of leaks that named the wrong
    finding. Reading both shapes is what makes the antigravity rows mean something.
    """
    call = event.get("toolCall") or event.get("tool_call")
    if isinstance(call, dict):
        name = call.get("name") or call.get("toolName") or ""
        args = call.get("args") or call.get("arguments") or call.get("input") or {}
        if isinstance(name, str) and name:
            return name, (args if isinstance(args, dict) else {})
    name = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    return (name if isinstance(name, str) else ""), (args if isinstance(args, dict) else {})


def antigravity_targets(event: dict) -> tuple[list[str], bool]:
    """(write targets, fully_covered) for one Antigravity tool call."""
    name, args = antigravity_call(event)
    if name in _AG_FILE_TOOLS:
        return _all_strs(args, _AG_PATH_KEYS), True
    if name in _AG_COMMAND_TOOLS:
        return shell_targets(_first_str(args, _AG_COMMAND_KEYS)), False
    if name in PATH_FIELD_TOOLS or name == "Bash":
        return targets_from_event(name, args)
    # An unrecognised tool, which the `*` matcher guarantees will happen: sed_file,
    # notebook_edit, a delete_file proto field, anything reached through call_mcp_tool. Read
    # its path-shaped and command-shaped arguments and report PARTIAL coverage. This
    # over-blocks -- a read tool naming a denied path is refused too -- and that is the only
    # direction this file may err in.
    found = _all_strs(args, _AG_PATH_KEYS)
    for cmd in _all_strs(args, _AG_COMMAND_KEYS):
        found += shell_targets(cmd)
    seen, out = set(), []
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out, False


# ------------------------------------------------------------------- protocol dispatch

# What a partially-covered parse is called in the block reason, per protocol. Named rather
# than hardcoded to "Bash", because "[partial-coverage tool: Bash]" printed under an
# antigravity `sed_file` call is a message that sends the reader to the wrong parser.
_PARTIAL_NOTE = {
    "claude": " [partial-coverage tool: Bash]",
    "cursor": " [partial-coverage tool: Bash]",
    "codex": " [partial-coverage tool: shell]",
    "antigravity": " [partial-coverage: arguments read generically, not a known write tool]",
}


def parse_event(protocol: str, event: dict) -> tuple[list[str], bool, str]:
    """(write targets, fully_covered, deny_reason) for one event under one host's schema.

    A non-empty deny_reason is a decision, not an error: it is how "this is a write I could
    not read" reaches the caller as a BLOCK. Returning ([], True) for it instead would allow
    the write and report it as fully covered, which is the worst shape in this codebase.
    """
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    if protocol == "antigravity":
        targets, covered = antigravity_targets(event)
        return targets, covered, ""

    if protocol == "codex":
        body = patch_body(tool_name, tool_input)
        if body is not None:
            paths, parsed = patch_targets(body)
            if not parsed:
                return [], False, (
                    "an apply_patch body that names no `*** Add/Update/Delete File:` header "
                    "cannot be read, so the write it performs is unknown. Denying: `ask` is "
                    "not a block on Codex -- an unsupported decision is continued past, so "
                    "answering `ask` would allow this write while looking like a deferral")
            # UNION with the ordinary parse, never a replacement. A patch delivered through a
            # shell heredoc is both an apply_patch AND a command, and reading only the headers
            # would drop the redirect sitting beside it. Union is the same rule the Bash
            # parser already follows against its own regexes: nothing that blocked before can
            # start passing.
            #
            # `covered` comes from the ordinary parse and is the right answer for both
            # shapes: an `apply_patch` tool call is not a shell command, so that arm returns
            # (no targets, fully covered) and the patch headers stand alone; a heredoc
            # delivered through Bash returns partial, which the combined row must inherit.
            more, covered = targets_from_event(tool_name, tool_input)
            return paths + [m for m in more if m not in paths], covered, ""
        return targets_from_event(tool_name, tool_input) + ("",)

    # claude, and cursor which reads Claude Code's configuration
    return targets_from_event(tool_name, tool_input) + ("",)


def emit(protocol: str, verdict: int, message: str) -> int:
    """Say the verdict on the channel this host actually reads, and return the exit code.

    On the stdout hosts the exit code is 0 for BOTH answers, because the host ignores it. That
    is not a bug being papered over: exiting 2 there would be answering on a channel nobody
    reads, which is how a deny becomes an allow with a tidy exit code in front of it. An ALLOW
    is printed too -- an empty stdout is not an allow, it is an adapter that said nothing, and
    probe.py correctly refuses to read it as one.
    """
    if protocol in STDOUT_PROTOCOLS:
        payload = {"decision": "deny" if verdict == BLOCK else "allow"}
        if message:
            payload["reason"] = message
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
        return ALLOW
    if verdict == BLOCK:
        print(message, file=sys.stderr)
        return BLOCK
    if message:
        print(message, file=sys.stderr)
    return ALLOW


def _fallback(protocol: str, message: str) -> int:
    """The answer when the guard could not do its job: allow and complain, or deny and say so.

    The split is a fact about the hosts, not a preference. See the module docstring.
    """
    if protocol in FAIL_CLOSED_PROTOCOLS:
        return emit(protocol, BLOCK,
                    "mir-guard: %s. DENYING: on this host a failed hook is a skipped hook, so "
                    "allowing here would let the write land with nothing recorded." % message)
    print("mir-guard: %s, allowing (policy not enforced)" % message, file=sys.stderr)
    return ALLOW


class _ArgError(Exception):
    """argparse's own failure path exits 2, which is BLOCK. Raise instead, so a bad command
    line becomes exit 3 -- 'not told which host is asking' -- rather than a block that looks
    like a working policy."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)

    def exit(self, status=0, message=None):
        raise _ArgError(message or "")


def parse_protocol(argv=None):
    """(protocol, error). Exactly one of the two is None.

    `--protocol` is an ordinary argparse flag rather than a hand-rolled scan because
    probe.guard_requires_protocol looks for the literal string in this file's syntax tree:
    landing the flag is what arms the wiring check, with nothing hardcoded to a date.
    """
    ap = _Parser(prog="mir-guard", add_help=False)
    ap.add_argument("--protocol", default=None,
                    help="which host is asking: " + ", ".join(PROTOCOLS))
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args, _unknown = ap.parse_known_args(argv)
    except _ArgError as e:
        return None, "mir-guard: %s" % e
    proto = args.protocol
    if not proto:
        return None, (
            "mir-guard: no --protocol given, so this guard does not know which host is asking "
            "and has enforced NOTHING. There is deliberately no default: running the wrong "
            "host's parser finds no field it recognises and allows every write while looking "
            "clean. Re-run `mir init` to migrate the hook registration.")
    if proto not in PROTOCOLS:
        return None, ("mir-guard: unknown --protocol %r; this guard answers for %s. Nothing "
                      "was enforced." % (proto, ", ".join(PROTOCOLS)))
    return proto, None


def _decide_event(protocol: str, event: dict) -> int:
    cwd = event.get("cwd") or os.getcwd()
    repo_root = find_repo_root(cwd)
    if repo_root is None:
        return _fallback(protocol, "no .mir/ found above cwd")
    try:
        manifest = load_manifest(repo_root)
        policy = manifest["policy"]
    except Exception as e:
        return _fallback(protocol, "could not load policy (%s)" % e)

    version = manifest.get("mir_manifest_version")
    if version != GUARD_MANIFEST_VERSION:
        # Loud on every invocation, and still ALLOW -- on every protocol, antigravity
        # included. This guard is a frozen copy; it does not know what a newer manifest means,
        # and refusing every write would brick the agent in a repo whose policy is perfectly
        # valid. The probe is where this fails closed, and targets/antigravity.py names this
        # as the one exception to its fail-closed posture rather than leaving it to be found.
        return emit(protocol, ALLOW,
                    "mir-guard: manifest version %r != guard version %s; NOT ENFORCING. "
                    "Regenerate the guard (`mir init`) -- this write is allowed unchecked."
                    % (version, GUARD_MANIFEST_VERSION))

    targets, covered, deny = parse_event(protocol, event)
    if deny:
        return emit(protocol, BLOCK, "mir-guard: blocked -- %s" % deny)
    for t in targets:
        verdict, reason = decide(resolve(t, repo_root), canonical(t, repo_root),
                                 policy, repo_root)
        if verdict == BLOCK:
            note = "" if covered else _PARTIAL_NOTE.get(protocol, " [partial coverage]")
            return emit(protocol, BLOCK,
                        "mir-guard: blocked write to %s%s -- %s" % (t, note, reason))
    return emit(protocol, ALLOW, "")


def main(argv=None) -> int:
    protocol, err = parse_protocol(argv)
    if err is not None:
        # Nothing is printed on stdout here even though a stdout host may be the caller: with
        # no protocol there is no way to know which host that is, and guessing is the thing
        # this branch exists to refuse. GOAL.md B1.4 records that exit 3 is fail-open on every
        # host until the hook registration is migrated.
        print(err, file=sys.stderr)
        return PROTOCOL_ERROR

    try:
        try:
            event = json.load(sys.stdin)
        except Exception as e:
            return _fallback(protocol, "could not parse hook input (%s)" % e)
        if not isinstance(event, dict):
            return _fallback(protocol, "hook input is not a JSON object")
        return _decide_event(protocol, event)
    except Exception as e:
        # The fail-closed host's last line of defence. An uncaught exception here would exit
        # nonzero with nothing on stdout, which Antigravity logs and skips -- the write lands.
        # So the crash still answers, on the channel the host reads.
        return _fallback(protocol, "crashed (%s: %s)" % (type(e).__name__, e))


if __name__ == "__main__":
    sys.exit(main())
