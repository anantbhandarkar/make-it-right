#!/usr/bin/env python3
"""Manifest-derived probe: prove the generated guard enforces the manifest it was built for.

This is the piece both plan reviews said was the difference between real verification and a
green report that proves nothing. The containment kit attacks a fixed list of sensitive
paths; that tells you the OS sandbox works, not that YOUR generated hook enforces YOUR
policy. This probe instead reads the manifest and attacks the guard with:

  - every denied_path              -> expected BLOCK. A `~`-rooted or absolute entry is fired
    EXPANDED (`/Users/you/.ssh`, not the four characters `~/.ssh`), because the guard expands
    `~` on both sides and a tilde-vs-tilde row proves only that two expansions cancel -- not
    that a real absolute write into $HOME is stopped.
  - a path just inside each         -> expected BLOCK   (e.g. .git/hooks/x under .git), but
    only where a child could exist. An entry that is a regular file on this machine gets no
    child row: `~/.gitconfig/child-file` is unreachable, so the guard blocks it by prefix and
    the row passes without ever being able to fail.
  - for a denied PATTERN, concrete instantiations of it at several depths and with several
    suffixes -> expected BLOCK. Replaying only the literal entry is what let `.env` pass
    while `src/.env` and `.env.development` sailed through: the probe ran green because it
    never asked the question. A pattern is only as good as the variants fired at it.
  - one path under each allowed root that is not denied -> expected ALLOW  (positive control)
  - a path clearly outside every allowed root           -> expected BLOCK  (deny-by-default)
  - the manifest and guard themselves                   -> expected BLOCK  (self-protection)

Each attack is a real PreToolUse event (Write, and the partial-coverage Bash form) piped to
the actual guard.py. The Bash form is fired once per VERB FAMILY -- redirect, `tee`, `dd of=`,
and the cp/mv/install copy verbs -- because the guard's shell parser has a separate branch for
each and a frozen guard can lose any one of them alone. The report also prints what was NOT
tested (Bash beyond those four, MCP writes, apply_patch, and whether the HOST honours a deny),
because a probe that hides its blind spots is the laundering both reviewers warned about.

NOTHING HERE EVER OPENS A TARGET FOR WRITING. An attack is a JSON event and an exit code;
the only filesystem call made on an attack path is a read-only stat, used to ask whether an
entry is a file. That is what makes it safe to fire `/Users/you/.ssh/child-file` at the
guard: the path is judged, never touched. Any future edit that opens an attack path breaks
the one property that lets this file attack a user's real home.

Each BLOCK row carries what it PROVES, because "blocked" is not one claim:

  rule             the target is inside an allowed_write_root, so only the denied entry can
                   have blocked it. The row isolates the rule it names.
  deny-by-default  the target is outside every allowed_write_root, so the fallback blocks it
                   whether or not the entry works. The row is real coverage of deny-by-default
                   and NO coverage of the rule printed beside it. Every `~/...` entry is this
                   shape by construction and cannot be made otherwise -- a home path cannot be
                   moved inside the repo. Saying so is the point: an over-determined row read
                   as rule coverage is exactly the number-inflation this file exists against.
  pattern-literal  the entry could not be instantiated, so its own spelling was fired. No
                   agent writes a path containing `*`; the row is a placeholder, not a test.

Each attack is judged on the CHANNEL ITS HOST ACTUALLY READS, not on the exit code alone.
Claude Code and Codex block on exit 2. Antigravity ignores the exit code entirely and reads
a deny decision off stdout as JSON, so an adapter there BLOCKS with exit 0 plus stdout. A
probe that only understood exit codes would read every Antigravity BLOCK as an ALLOW, turn
every denied row into a LEAK, and make "just exit 2 as well" look like the fix -- which would
turn this report green while the real host allowed every write. So the verdict reader is
per-protocol and, on the stdout channel, the exit code is not consulted at all:

  exit 0 with empty or unparseable stdout is GUARD-ERROR, never ALLOW.

A crashed adapter must not read as a clean allow-path run. The same rule points the other
way for the exit-code hosts: `ask` is not a block, because Codex treats an unsupported
decision as "continue past", so an adapter that answers `ask` has enforced nothing.

A VERSION phase runs before any of it. guard.py carries GUARD_MANIFEST_VERSION and compares it
against the manifest's `mir_manifest_version`; on a mismatch it warns and ALLOWS, because a
frozen guard cannot know what a newer manifest means and bricking a colleague's agent inside
their own repo is worse than not enforcing. Runtime fails open; verification fails closed.
The probe reads the guard's constant out of its SOURCE and refuses to call a mismatched pair
clean -- otherwise the mismatch is caught only by accident, as a table full of leaks that
names the wrong finding, which at MANIFEST_VERSION 2 is every existing user's upgrade.

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
import fnmatch
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

# The three things a row can report. GUARD_ERROR is not a third verdict the host understands
# -- it is the probe saying the adapter answered on no channel at all, which is why it can
# never be collapsed into ALLOW.
V_ALLOW, V_BLOCK, V_ERROR = "ALLOW", "BLOCK", "GUARD-ERROR"

# Which channel each host reads a verdict off. This is a fact about the HOST, not about the
# guard: it decides how the probe interprets an answer, so an adapter cannot make a row green
# by answering on a channel its host ignores.
CHANNEL_EXIT, CHANNEL_STDOUT = "exit-code", "stdout-json"
PROTOCOL_CHANNEL = {
    "claude": CHANNEL_EXIT,        # PreToolUse hook: exit 2 blocks
    "codex": CHANNEL_EXIT,         # hooks block on exit 2
    "antigravity": CHANNEL_STDOUT, # exit code IGNORED by the host; deny-JSON on stdout blocks
    "cursor": CHANNEL_EXIT,        # reads Claude Code's config, so it reads Claude's channel
}

# The target every manifest written before cross-agent init declared implicitly. A manifest
# with no `targets` block is not a manifest with no targets -- reading it that way would make
# an older harness verify nothing at all while reporting a clean run.
DEFAULT_TARGET = "claude"

# Decisions read off the stdout channel. Deliberately a small closed set: an unrecognised
# decision string is GUARD-ERROR, never ALLOW, so a typo'd or invented verdict fails closed.
# `ask` is in NEITHER set on purpose -- Codex treats it as unsupported and continues past, so
# an adapter that answers `ask` has allowed the write while looking like it deferred.
_DENY_DECISIONS = {"deny", "denied", "block", "blocked"}
_ALLOW_DECISIONS = {"allow", "allowed", "approve", "approved", "permit"}
_UNSUPPORTED_DECISIONS = {"ask", "confirm", "prompt"}


def run_guard(guard: str, event: dict, cwd: str, protocol: str = None):
    """Fire one event at the guard. Returns (exit code, stdout).

    stdout is returned rather than discarded because on the Antigravity channel it is the
    ONLY place a verdict appears; a runner that returned the exit code alone could not tell
    a deny from a crash there. See read_verdict for who decides which one matters.

    `--protocol` is passed only when the guard's own source declares the flag. Before that
    flag exists, passing it would hand an argument to a guard that would reject it, and the
    probe would be testing its own invocation rather than the policy.
    """
    cmd = [sys.executable, guard]
    if protocol is not None:
        cmd += ["--protocol", protocol]
    p = subprocess.run(
        cmd,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=cwd,
    )
    # NOTE: a MISSING guard file makes Python itself exit 2, which collides with BLOCK. The
    # caller must verify the guard path exists BEFORE calling this, or a probe with no guard
    # would read every attack as "blocked" and pass for the wrong reason.
    return p.returncode, p.stdout


def channel_for(protocol: str) -> str:
    """The channel a protocol answers on. An UNKNOWN protocol gets the stdout channel, which
    is the stricter reader: it refuses to call anything a BLOCK unless a decision was actually
    printed. Defaulting an unknown host to the exit-code reader would let a fifth target
    inherit Claude's semantics silently, which is the shape B1.1 exists to forbid."""
    return PROTOCOL_CHANNEL.get(protocol, CHANNEL_STDOUT)


def read_verdict(protocol: str, rc: int, stdout: str):
    """(verdict, why) for one guard invocation under one host's rules.

    On the stdout channel the exit code is not consulted AT ALL. That is deliberate and it is
    the whole point of this function: Antigravity's host ignores it, so honouring an exit 2
    here would let an adapter that exits 2 and prints nothing read as a BLOCK while the real
    host allowed the write. The probe must fail exactly where the host would.
    """
    if channel_for(protocol) == CHANNEL_STDOUT:
        text = (stdout or "").strip()
        if not text:
            return V_ERROR, (f"exit {rc} with EMPTY stdout: on {protocol} the host reads the "
                             "decision off stdout, so this answered on no channel at all")
        try:
            obj = json.loads(text)
        except ValueError:
            return V_ERROR, f"stdout is not JSON, so {protocol} would read no decision from it"
        if not isinstance(obj, dict):
            return V_ERROR, "stdout JSON is not an object, so it carries no `decision`"
        # `permissionDecision` is read as a fallback because that is the key Claude Code's own
        # deny-JSON uses, and an adapter that emits it has clearly decided; refusing to read it
        # would report GUARD-ERROR on a guard that blocked.
        decision = obj.get("decision", obj.get("permissionDecision"))
        if not isinstance(decision, str):
            return V_ERROR, "stdout JSON carries no string `decision`"
        d = decision.strip().lower()
        if d in _DENY_DECISIONS:
            return V_BLOCK, ""
        if d in _ALLOW_DECISIONS:
            return V_ALLOW, ""
        if d in _UNSUPPORTED_DECISIONS:
            return V_ERROR, (f"decision {decision!r} is not a block: the host continues past an "
                             "unsupported decision, so nothing was enforced")
        return V_ERROR, f"decision {decision!r} is neither an allow nor a deny"

    if rc == BLOCK:
        return V_BLOCK, ""
    if rc == ALLOW:
        # An exit-code host still reads a JSON decision where it offers one (Codex's
        # apply_patch), and `ask` there is not a block -- Codex treats it as unsupported and
        # continues past. Exit 0 plus `ask` is therefore an unenforced write, not an allow the
        # policy chose.
        try:
            obj = json.loads((stdout or "").strip() or "null")
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            d = obj.get("decision", obj.get("permissionDecision"))
            if isinstance(d, str) and d.strip().lower() in _UNSUPPORTED_DECISIONS:
                return V_ERROR, (f"exit 0 with decision {d!r}: {protocol} continues past an "
                                 "unsupported decision, so this enforced nothing")
        return V_ALLOW, ""
    return V_ERROR, f"exit {rc} is outside {{{ALLOW}, {BLOCK}}}: neither allowed nor blocked"


def declared_targets(manifest: dict) -> list:
    """[{name, protocol}] the manifest declares, read from the manifest and never from flags.

    Shape-tolerant on purpose -- a dict of name -> spec and a list of names both parse --
    because this file is COPIED into a project and has to keep reading manifests written by
    older and newer generators alike. A manifest with no `targets` block declares
    DEFAULT_TARGET: those manifests predate cross-agent init and named Claude Code implicitly,
    and reading "no block" as "no targets" would silently skip the whole attack phase.
    """
    raw = manifest.get("targets")
    out: list = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(t, {}) if isinstance(t, str)
                 else (t.get("name"), t) for t in raw if isinstance(t, (str, dict))]
    else:
        items = []
    for name, spec in items:
        if not isinstance(name, str) or not name:
            continue
        proto = spec.get("protocol") if isinstance(spec, dict) else None
        out.append({"name": name, "protocol": proto if isinstance(proto, str) and proto else name})
    return out or [{"name": DEFAULT_TARGET, "protocol": DEFAULT_TARGET}]


def attack_protocols(targets: list) -> list:
    """The distinct protocols to fire the attack suite under, in declaration order.

    Distinct PROTOCOLS, not targets: two targets that read the same channel would produce two
    identical tables, and a row printed twice is one question counted twice.
    """
    seen, out = set(), []
    for t in targets:
        if t["protocol"] not in seen:
            seen.add(t["protocol"])
            out.append(t["protocol"])
    return out


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


# One Bash attack per VERB FAMILY, because the guard's shell parser has a separate branch for
# each and a frozen guard can lose any one of them alone. Every generated harness fired
# `echo x > {path}` and nothing else, so `tee`, `dd of=`, and the cp/mv/install copy verbs --
# including copy_dests' per-source expansion -- were exercised only by this repo's own tests
# against its own in-tree guard. That is the wrong tree: the defect class is a frozen
# .mir/guard.py rotting inside a user's project while the maintainer's CI stays green.
#
# The axis is VERB x RULE and never verb x instantiation. Nine near-identical redirects into
# one rule pad the count without asking anything new, so the row total stays LINEAR in
# denied_paths rather than combinatorial in it.
#
# The `cp` source is a name, never a file: nothing here is opened, on either operand.
_BASH_SOURCE = "mir-probe-source"
_BASH_VERBS = (
    ("redirect", "echo x > {path}"),
    ("tee", "echo x | tee {path}"),
    ("dd", "dd if=/dev/null of={path}"),
    ("cp", "cp " + _BASH_SOURCE + " {path}"),
)
_BASH_COMMANDS = dict(_BASH_VERBS)


def bash_write_event(path: str, cwd: str, verb: str = "redirect") -> dict:
    cmd = _BASH_COMMANDS.get(verb, _BASH_COMMANDS["redirect"]).format(path=path)
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}


def _sample_under(root_rel: str) -> str:
    return os.path.join(root_rel, ".mir-probe-canary")


def _abs(path: str, repo_root: str) -> str:
    """The absolute path a probe string names, for arithmetic only.

    `~` is expanded and a relative path is joined to the repo root, so both sides of every
    containment question below are spelled the same way. normpath, not realpath: this is a
    label, not a verdict, and the guard owns canonicalisation.
    """
    p = os.path.expanduser(path)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(repo_root, p))


def _is_under(path: str, root: str) -> bool:
    """Lexical containment, `root + os.sep` so `.mir-probe-canary` is not read as under
    `.mir`. Duplicated from guard.py for the same reason is_glob is: probe.py is copied
    standalone into .mir/ and has nothing to import from."""
    path, root = os.path.normpath(path), os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


def _inside_allowed(path: str, policy: dict, repo_root: str) -> bool:
    """Would deny-by-default alone block this path? If it is inside an allowed root, no --
    which is what makes a BLOCK there attributable to the denied entry rather than to the
    fallback."""
    return any(_is_under(_abs(path, repo_root), _abs(r, repo_root))
               for r in policy.get("allowed_write_roots", []))


def _attack_spelling(entry: str, repo_root: str) -> str:
    """How a denied entry is written into the event it is fired as.

    A `~`-rooted or absolute entry is fired EXPANDED, because that is the write an agent
    would actually attempt and it is the only spelling that tests an absolute path landing in
    $HOME -- the guard expands `~` on both the entry side and the target side, so firing the
    tilde back at it proves only that the two expansions agree. A repo-relative entry stays
    relative: that is how a tool reports its own target, and it keeps the report readable.
    """
    return _abs(entry, repo_root) if entry.startswith("~") or os.path.isabs(entry) else entry


def _can_have_children(entry: str, repo_root: str) -> bool:
    """Could a write ever land BELOW this entry?

    Not if the entry is a regular file on this machine: `~/.gitconfig/child-file` is a path
    the filesystem cannot produce, so the guard blocks it by prefix and the row passes
    without being able to fail. A row that cannot fail is worse than no row, because the
    count it inflates gets read as coverage.

    A path that does not exist keeps its child attack. It could still be created as a
    directory, and guessing "file" would drop a row that CAN fail -- the one error worse than
    keeping one that cannot. So this reports what is true on the machine the probe ran on,
    which is also the machine whose home directory is at stake.

    os.path.isfile is a read-only stat. It is the only filesystem call this file makes on an
    attack path, and it must stay that way; see the module docstring.
    """
    return not os.path.isfile(_abs(entry, repo_root))


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


def denied_attack_paths(entry: str, repo_root: str = ".") -> list[str]:
    """The concrete paths to fire at one denied_paths entry.

    A pattern that cannot be instantiated falls back to the literal entry, so an exotic
    pattern is still attacked (weakly) rather than silently dropped from the report; the row
    is labelled `pattern-literal` so the weakness is on the page rather than in the count.

    repo_root is what `~` and a relative entry resolve against. It defaults to the cwd's
    spelling so the function stays callable for pure pattern questions, but the prober always
    passes the repo it is attacking -- an entry's shape is a fact about a specific tree.
    """
    if is_glob(entry):
        return [_attack_spelling(p, repo_root) for p in expand_glob_attacks(entry)] or [entry]
    target = _attack_spelling(entry, repo_root)
    if not _can_have_children(entry, repo_root):
        return [target]
    return [target, os.path.join(target, "child-file")]  # e.g. .git and .git/hooks/pre-commit


# ---------------------------------------------------------------- wiring phase
#
# generate.py's HOOK_TAG, duplicated rather than imported for the same reason is_glob is:
# probe.py is COPIED standalone into .mir/, where there is no generate.py to import from.
HOOK_TAG = "mir-init-guard"

# Claude Code merges the project's settings.json with settings.local.json, so a guard
# registered in either one is genuinely wired. It also merges ~/.claude/settings.json, which
# the probe deliberately does NOT read -- see wiring_report for what that costs.
_SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")

# Where each declared target registers its hook, relative to the repo root.
#
# An EMPTY tuple means the target registers no enforcement file AT ALL by design -- Cursor
# reads Claude Code's configuration behind a user-level toggle, so emitting a Cursor-specific
# file nothing reads would be a path written where nothing reads it. Its row says so and is
# never counted as coverage.
#
# A declared target that is NOT in this table is a wiring FAILURE, not a skip. The probe does
# not know where a fifth host registers a hook, and "we did not look" reads identically to
# "it is fine" in a green report -- the same reason B1.1 makes a missing Capability key raise
# rather than default.
TARGET_WIRING = {
    "claude": _SETTINGS_FILES,
    "codex": (".codex/hooks.json",),
    "antigravity": (".agents/hooks.json",),
    "cursor": (),
}

# `--protocol claude`, `--protocol=claude`. The value stops at whitespace or a quote, so a
# command wrapped in shell quoting still yields the bare protocol name.
_PROTOCOL_ARG = re.compile(r"--protocol[=\s]+([^\s\"']+)")

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


def _guard_ast(guard_path: str):
    """The guard's syntax tree, or None if it cannot be read or parsed.

    Parsed, never imported or executed: the probe has to be able to inspect a BROKEN guard,
    and a guard whose body is `sys.exit(0)` would otherwise take the probe down with it and
    report nothing.
    """
    try:
        with open(guard_path, encoding="utf-8") as f:
            return ast.parse(f.read())
    except Exception:
        return None


def _assigned(tree, name: str):
    """The value node of a module-level `name = ...`, or None."""
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    return None


def guard_requires_protocol(guard_path: str) -> bool:
    """Does this guard take a `--protocol` flag?

    Read off the guard's own source, so the check self-updates the moment the flag lands
    rather than on a date someone remembered. Before the flag exists, passing it -- or
    demanding it in a registered command -- would fail a harness the guard itself would
    reject; after it exists, its absence from a registered command is the v1 command, which
    exits 3 on every call. Both readings are wrong at the other time, so neither is hardcoded.
    """
    tree = _guard_ast(guard_path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "--protocol":
            return True
    return False


# The three states of the version row. `unknown` is deliberately not folded into `mismatch`:
# they are different findings and they get different exit codes.
VERSION_OK, VERSION_MISMATCH, VERSION_UNKNOWN = "ok", "mismatch", "unknown"


def guard_manifest_version(guard_path: str):
    """The manifest version this guard was built to understand, read off its SOURCE.

    Parsed with ast, exactly the way guarded_tools reads PATH_FIELD_TOOLS and for the same
    reason: the probe must be able to version a guard whose body is `sys.exit(0)`, and running
    the file under test to ask it a question about itself is how a broken guard takes the
    probe down with it and reports nothing.

    None means the guard declares no version. That is a different finding from a mismatch and
    is not reported as one.
    """
    node = _assigned(_guard_ast(guard_path), "GUARD_MANIFEST_VERSION")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)):
        return node.value
    return None


def version_report(manifest: dict, guard_path: str) -> dict:
    """Does this guard understand this manifest? The claim guard.py makes, made true.

    guard.py's own docstring says "a version mismatch fails closed -- the probe owns that",
    and until this function existed the probe contained no reference to mir_manifest_version
    at all: a doc claim the code did not support, inside the security tooling, which is the
    category this release closes.

    The asymmetry is the whole point. At RUNTIME a mismatch fails open by design -- a frozen
    guard cannot know what a newer manifest means, and bricking a colleague's agent inside
    their own repo is worse than not enforcing. At VERIFICATION it fails closed, because
    "enforcing nothing" is precisely what a verifier exists to refuse to call clean. Without
    this row the mismatch is caught only by ACCIDENT: the guard allows everything, so every
    denied row reads as a leak and the report names the wrong finding. At MANIFEST_VERSION 2
    that accident becomes every existing user's migration path.
    """
    want = manifest.get("mir_manifest_version")
    got = guard_manifest_version(guard_path)
    if got is None:
        return {"manifest": want, "guard": None, "status": VERSION_UNKNOWN,
                "why": ("this guard declares no GUARD_MANIFEST_VERSION, so the probe cannot "
                        "establish that it understands this manifest. Not a mismatch -- an "
                        "unanswerable question, which is not the same as a clean answer.")}
    if got == want:
        return {"manifest": want, "guard": got, "status": VERSION_OK, "why": ""}
    return {"manifest": want, "guard": got, "status": VERSION_MISMATCH,
            "why": (f"the manifest is version {want!r} and the guard understands {got!r}. "
                    "The guard fails OPEN on that mismatch by design: it warns on stderr and "
                    "allows every write unchecked, so this harness enforces NOTHING. "
                    "Run `mir init` to regenerate the guard against this manifest.")}


def guarded_tools(guard_path: str) -> list[str]:
    """Every tool the guard claims to cover: its PATH_FIELD_TOOLS keys, plus Bash.

    Read out of the guard's SOURCE by parsing it, never by importing it -- see _string_keys
    for why running the file under test is not an option. Parsing also keeps probe.py
    standalone under .mir/, where there is no shared module to import from.

    Deriving the list from the guard instead of hardcoding it here is what makes the wiring
    check self-updating: add a tool to PATH_FIELD_TOOLS and the matcher is checked against it
    on the next run, with no edit to this file.
    """
    names = _string_keys(_assigned(_guard_ast(guard_path), "PATH_FIELD_TOOLS"))
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
        path = os.path.join(repo_root, name)
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


def command_protocol(cmd: str):
    """The `--protocol` value a registered command passes, or None."""
    m = _PROTOCOL_ARG.search(cmd)
    return m.group(1) if m else None


def _json_strings(obj) -> list:
    """Every string anywhere inside a decoded JSON value.

    Shape-agnostic on purpose. The probe has to read a hooks file whose schema it does not
    own -- Codex's and Antigravity's are the host's, not mir's -- and a hardcoded key path
    would report "unwired" the day the host renames a field. What it needs from that file is
    one thing: does some registered command invoke the guard, with the right protocol.
    """
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _json_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _json_strings(v)]
    return []


def command_verdict(cmds: list, protocol: str, needs_protocol: bool):
    """(ok, why) for the commands one target registers. The row the matcher cannot produce.

    A correct matcher over a dead command is status `wired` and enforcement zero. Once
    `--protocol` is required, a repo carrying the v1 command -- `python3
    "$CLAUDE_PROJECT_DIR/.mir/guard.py"`, no flag -- has exactly that shape: the matcher
    fullmatches every tool, and the guard exits 3 on every call. The first green run after the
    bump would mean nothing, so the command is compared, not just the matcher it sits beside.

    `needs_protocol` is read off the guard's own source rather than assumed. Demanding a flag
    the guard does not take would fail a harness that works; ignoring one it does take would
    pass a harness that does not. Neither reading is right at both times, so neither is fixed.
    """
    naming = [c for c in cmds if "guard.py" in c]
    if not naming:
        return False, ("no registered command invokes guard.py: the matcher can fullmatch "
                       "every tool and the hook still runs nothing")
    if not needs_protocol:
        return True, ""
    if any(command_protocol(c) == protocol for c in naming):
        return True, ""
    seen = sorted({command_protocol(c) or "(none)" for c in naming})
    if seen == ["(none)"]:
        return False, ("the registered command carries no --protocol, so it is the v1 command: "
                       "this guard exits 3 on every call and enforces NOTHING while the "
                       "matcher still looks correct. Re-run `mir init` to migrate it.")
    return False, (f"the registered command passes --protocol {'/'.join(seen)}, not "
                   f"{protocol!r}: the guard is answering for the wrong host")


def _claude_rows(repo_root: str, protocol: str, tools: list, needs_protocol: bool) -> list:
    """Claude Code's rows: one per guarded tool for the matcher, plus one for the command.

    `soft` marks a failure the probe cannot call proven. Claude Code also merges
    ~/.claude/settings.json, which the probe deliberately does not read, so "no project entry"
    is "not visible from here" rather than "not wired" -- and reporting the stronger claim
    would be its own false statement. A soft failure still refuses to exit 0.
    """
    entries = find_wiring_entries(repo_root)
    live = [e for e in entries if e["entry"] is not None]

    if not live:
        why = "; ".join(e["error"] for e in entries if e["error"]) or (
            "no mir PreToolUse entry in .claude/settings.json or .claude/settings.local.json")
        rows = [{"target": DEFAULT_TARGET, "tool": t, "matcher": None, "ok": False,
                 "soft": True, "coverage": True, "why": why} for t in tools]
        rows.append({"target": DEFAULT_TARGET, "tool": "(command)", "matcher": None,
                     "ok": False, "soft": True, "coverage": True, "why": why})
        return rows

    matchers = [(e["file"], e["entry"].get("matcher")) for e in live]
    # On a miss the row still shows what IS registered, because "no matcher fires for Bash"
    # is only actionable next to the matcher that failed to.
    registered = " / ".join("(none)" if m[1] is None else str(m[1]) for m in matchers)
    rows = []
    for t in tools:
        hit = next((m for m in matchers if matcher_covers(m[1], t)), None)
        rows.append({
            "target": DEFAULT_TARGET,
            "tool": t,
            "matcher": str(hit[1]) if hit else registered,
            "ok": hit is not None,
            "soft": False,
            "coverage": True,
            "why": "" if hit else "no registered matcher fullmatches this tool",
        })

    cmds = [h.get("command") for e in live for h in (e["entry"].get("hooks") or [])
            if isinstance(h, dict) and isinstance(h.get("command"), str)]
    ok, why = command_verdict(cmds, protocol, needs_protocol)
    rows.append({"target": DEFAULT_TARGET, "tool": "(command)", "coverage": True,
                 "matcher": " / ".join(cmds) if cmds else "(none)",
                 "ok": ok, "soft": False, "why": why})
    return rows


def _file_target_rows(repo_root: str, name: str, protocol: str, files, needs_protocol: bool):
    """A non-Claude target's row: does its wiring file exist, and does it run the guard?

    A declared target whose wiring file is DELETED is a hard failure, never a skip. The
    asymmetry with Claude's soft `unconfirmed` is deliberate and is a fact about the hosts:
    Claude Code merges a user-level settings file the probe cannot read, while
    `.codex/hooks.json` and `.agents/hooks.json` are repo-local only -- absent there means
    absent, and nothing routes a write to the guard.
    """
    seen: list = []
    errors: list = []
    present = False
    for rel in files:
        path = os.path.join(repo_root, rel)
        if not os.path.exists(path):
            continue
        present = True
        try:
            with open(path, encoding="utf-8") as f:
                seen += _json_strings(json.load(f))
        except Exception as e:
            errors.append(f"{rel} did not parse ({e})")

    if not present:
        return [{"target": name, "tool": "(registration)", "matcher": None, "ok": False,
                 "soft": False, "coverage": True,
                 "why": (f"{name} is declared in the manifest and {' / '.join(files)} does not "
                         "exist: nothing routes a write to the guard. Re-run `mir init`.")}]
    if errors:
        return [{"target": name, "tool": "(registration)", "matcher": None, "ok": False,
                 "soft": False, "coverage": True, "why": "; ".join(errors)}]

    ok, why = command_verdict(seen, protocol, needs_protocol)
    return [{"target": name, "tool": "(command)", "coverage": True,
             "matcher": " / ".join(c for c in seen if "guard.py" in c) or "(none)",
             "ok": ok, "soft": False, "why": why}]


def _target_rows(repo_root: str, target: dict, tools: list, needs_protocol: bool) -> list:
    name, protocol = target["name"], target["protocol"]
    if name not in TARGET_WIRING:
        return [{"target": name, "tool": "(registration)", "matcher": None, "ok": False,
                 "soft": False, "coverage": True,
                 "why": (f"the probe does not know where target {name!r} registers a hook. "
                         "Add it to probe.TARGET_WIRING -- an unknown target must not verify "
                         "clean by being unlooked-at.")}]
    files = TARGET_WIRING[name]
    if not files:
        return [{"target": name, "tool": "(registration)", "matcher": "(none by design)",
                 "ok": True, "soft": False, "coverage": False,
                 "why": (f"{name} registers no enforcement file of its own, so this row is a "
                         "statement, not coverage: nothing here was verified.")}]
    if name == DEFAULT_TARGET:
        return _claude_rows(repo_root, protocol, tools, needs_protocol)
    return _file_target_rows(repo_root, name, protocol, files, needs_protocol)


def wiring_report(repo_root: str, guard_path: str, manifest: dict = None) -> dict:
    """Is the guard actually registered, for every declared target, for every tool that writes?

    Every attack in this file pipes an event to guard.py directly, so not one of them reads a
    settings file. A stale matcher -- or a live matcher over a dead command -- therefore leaves
    structured writes entirely unguarded while every attack row still reports BLOCK. This phase
    is the only thing that asks whether the hook fires at all.

    The declared set comes from the MANIFEST, not from flags: a target is verified because the
    harness claims it, so deleting the flag cannot delete the check.

    Three outcomes, and none of them is a skip -- a declared target with no wiring entry is a
    finding, because "we did not look" reads identically to "it is fine" in a green report:

      wired        every guarded tool fullmatches a registered matcher, and the registered
                   command invokes the guard for the right protocol
      unwired      a proven gap: a matcher that does not fire, a command that does not run the
                   guard, a v1 command with no --protocol, or a declared target whose wiring
                   file is missing
      unconfirmed  nothing readable to judge, and only where the probe genuinely cannot see
                   the whole picture. It still refuses to exit 0.
    """
    tools = guarded_tools(guard_path)
    needs_protocol = guard_requires_protocol(guard_path)
    targets = declared_targets(manifest or {})

    rows: list = []
    for t in targets:
        rows += _target_rows(repo_root, t, tools, needs_protocol)

    failures = [r for r in rows if not r["ok"]]
    hard = [r for r in failures if not r.get("soft")]
    status = "unwired" if hard else ("unconfirmed" if failures else "wired")
    why = "; ".join(dict.fromkeys(r["why"] for r in failures if r["why"]))
    return {"status": status, "tools": tools, "targets": targets, "rows": rows,
            "failures": failures, "why": why}


PROVES_RULE = "rule"
PROVES_DEFAULT = "deny-by-default"
PROVES_LITERAL = "pattern-literal"


def proves(attack: dict, policy: dict, repo_root: str) -> str:
    """What a BLOCK on this row would actually establish. See the module docstring for the
    three values and why the distinction is the report's whole job."""
    if attack["expect"] != BLOCK:
        return ""
    if is_glob(attack["path"]):
        return PROVES_LITERAL
    return PROVES_RULE if _inside_allowed(attack["path"], policy, repo_root) else PROVES_DEFAULT


# How a sibling control is spelled: the denied entry with this appended to its last component.
# DERIVED from the entry rather than listed, because a hardcoded list of siblings inherits
# exactly the blind spot the derivation avoids -- the probe would only ever test the
# over-matches someone already thought of, which was D7's root cause.
#
# A synthetic suffix, not a plausible one. `.gitignore` reads better in a report, but it is
# right for `.git` and meaningless for `.kube`, so a natural suffix is a list wearing a
# derivation's clothes. The property under test -- would a bare `startswith(root)` deny this?
# -- is identical for either spelling, and this one cannot collide with a real filename.
SIBLING_SUFFIX = "-mir-sibling"


def _denied_by_policy(path: str, policy: dict, repo_root: str) -> bool:
    """Would some denied entry match this path, read lexically?

    A FILTER, never a verdict: its only job is to drop a derived sibling that the policy
    genuinely denies, so the probe does not assert ALLOW on a path correctly-working code
    blocks. Erring toward dropping costs one control; erring the other way fails working code,
    so the matching here is deliberately generous. The guard owns the real decision.
    """
    target = _abs(path, repo_root)
    for entry in policy.get("denied_paths", []):
        if is_glob(entry):
            pat = _abs(entry, repo_root)
            pats = [pat] + ([pat.replace("**/", "")] if "**/" in pat else [])
            cur = target
            while True:
                if any(fnmatch.fnmatch(cur, p) for p in pats):
                    return True
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        elif _is_under(target, _abs(entry, repo_root)):
            return True
    return False


def sibling_controls(policy: dict, repo_root: str) -> list[dict]:
    """A derived over-match control per LITERAL denied entry, expected ALLOW.

    guard._is_under is `path == root or path.startswith(root + os.sep)`, so `.gitignore` is
    correctly not under `.git` -- today. Nothing pinned it. A "simplification" to a bare
    `startswith(root)` would deny `.gitignore`, `.mirror` and `.envelope`, and the probe would
    go GREENER, because a guard that blocks more looks safer to a leak-only gate. Counting the
    sibling as a POSITIVE CONTROL is what turns that into exit 3 instead of a quieter pass.

    Three exclusions, each because the row could not fail for the right reason:

      a GLOB entry has no single spelling to extend, and extending one instantiation would
      test that instantiation rather than the rule.

      a `~`-rooted or absolute entry lives outside every allowed root, so deny-by-default
      blocks its sibling whether or not the prefix over-matches: `~/.sshx` is denied by
      correctly-working code, and asserting ALLOW there would fail the guard for being right.
      This is the same reasoning the `proves` column already carries, pointed the other way.

      a sibling the policy denies on its own account -- `.env` next to a `**/.env*` rule --
      is dropped for the same reason.
    """
    out: list[dict] = []
    for entry in policy.get("denied_paths", []):
        if is_glob(entry) or entry.startswith("~") or os.path.isabs(entry):
            continue
        sib = entry.rstrip("/") + SIBLING_SUFFIX
        # The general form of the ~-rooted exclusion above: whatever the entry's spelling, a
        # sibling outside every allowed root is blocked by the fallback, not by the prefix.
        if not _inside_allowed(sib, policy, repo_root):
            continue
        if _denied_by_policy(sib, policy, repo_root):
            continue
        out.append({"path": sib, "expect": ALLOW, "names_entry": False, "kind": "write",
                    "why": f"sibling of denied {entry}: a prefix must not over-match"})
    return out


def build_attacks(manifest: dict, repo_root: str) -> list[dict]:
    policy = manifest["policy"]
    attacks: list[dict] = []

    for d in policy.get("denied_paths", []):
        for p in denied_attack_paths(d, repo_root):
            attacks.append({"path": p, "expect": BLOCK, "names_entry": True,
                            "why": f"denied_path {d}", "kind": "write"})

    for r in policy.get("allowed_write_roots", []):
        # a normal file under an allowed root that is not itself denied -> must pass
        attacks.append({"path": _sample_under(r), "expect": ALLOW, "names_entry": False,
                        "why": f"normal file under allowed root {r}", "kind": "write"})

    # deny-by-default: a path outside every allowed root (outside the repo when root is '.')
    attacks.append({"path": _escape_path(repo_root), "expect": BLOCK, "names_entry": False,
                    "why": "outside every allowed_write_root", "kind": "write"})

    # self-protection: the policy must not let the agent rewrite the policy or the guard
    attacks.append({"path": ".mir/manifest.json", "expect": BLOCK, "names_entry": False,
                    "why": "the policy must protect itself", "kind": "write"})

    # Bash writes into denied targets, to exercise the partial-coverage path: the first denied
    # entry, plus a NESTED instantiation of EVERY denied pattern, because the shell is the
    # likeliest way `echo KEY=... > src/.env` actually gets attempted.
    #
    # Two axes, and only two. The RULE is one -- each pattern is a separate entry that may or
    # may not be wired into the shell parser -- and the VERB FAMILY is the other, because the
    # parser has a different branch for each and can lose them one at a time. The
    # INSTANTIATION is not an axis: nine near-identical writes into the same rule would pad
    # the count without asking a new question. So the row total stays linear in the manifest's
    # own denied_paths, never combinatorial in it.
    #
    # A `break` used to end this loop after the FIRST pattern. With two patterns in the
    # baseline policy that left `**/.env*` -- the one entry whose whole reason for existing is
    # the shell redirect named above -- with no Bash attack at all.
    denied = policy.get("denied_paths", [])
    shell: list = []
    if denied:
        shell.append((denied_attack_paths(denied[0], repo_root)[-1],
                      f"shell write into denied {denied[0]}"))
    for d in denied:
        if not is_glob(d):
            continue
        nested = [p for p in denied_attack_paths(d, repo_root) if "/" in p.strip("/")]
        if not nested:
            continue
        # Prefer an instantiation INSIDE an allowed root: outside one, deny-by-default blocks
        # the redirect whatever the pattern does and the row stops isolating the rule.
        inside = [p for p in nested if _inside_allowed(p, policy, repo_root)]
        shell.append(((inside or nested)[-1], f"shell write into nested {d}"))

    seen = set()
    for path, why in shell:
        # Deduplicated on the path: when denied[0] is itself a pattern its two rows collide,
        # and the same command fired twice is one question printed twice.
        if path in seen:
            continue
        seen.add(path)
        # One row per verb family. Four rows per rule is a CONSTANT factor on a count that is
        # linear in denied_paths, and each of the four asks a different branch of the guard's
        # shell parser -- which is what makes them four questions rather than one repeated.
        for verb, _cmd in _BASH_VERBS:
            attacks.append({"path": path, "expect": BLOCK, "names_entry": True, "verb": verb,
                            "why": f"{why} (via {verb})", "kind": "bash"})

    attacks += sibling_controls(policy, repo_root)

    for a in attacks:
        a["proves"] = proves(a, policy, repo_root)
    return attacks


def probe(manifest_path: str, guard: str, repo_root: str) -> dict:
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    targets = declared_targets(manifest)
    protocols = attack_protocols(targets)
    # The flag is passed only where the guard declares it; the VERDICT READER is chosen by the
    # protocol either way. Those are two different questions: whether the guard can be told
    # which host it is speaking for, and how that host reads the answer. A guard with no
    # protocol support facing a declared antigravity target answers on no channel that host
    # reads, and the GUARD-ERROR rows that follow are the correct finding, not a probe bug.
    flag = guard_requires_protocol(guard)

    attacks = build_attacks(manifest, repo_root)
    results = []
    for proto in protocols:
        for a in attacks:
            ev = (bash_write_event(a["path"], repo_root, a.get("verb", "redirect"))
                  if a["kind"] == "bash" else write_event(a["path"], repo_root))
            rc, out = run_guard(guard, ev, repo_root, proto if flag else None)
            verdict, why = read_verdict(proto, rc, out)
            ok = verdict != V_ERROR and (BLOCK if verdict == V_BLOCK else ALLOW) == a["expect"]
            results.append({**a, "protocol": proto, "got": verdict, "ok": ok, "got_why": why,
                            "expect_label": "BLOCK" if a["expect"] == BLOCK else "ALLOW"})

    # Three disjoint buckets, because they are three different claims and collapsing them is
    # what made a broken guard readable as a clean one:
    #   leak         a denied path reached the target. The shipping blocker.
    #   false_block  a positive control was blocked. NOT "merely too tight": it makes every
    #                BLOCK row above undiscriminating, since a guard that blocks the control
    #                blocks denied and allowed paths alike and the table can no longer tell
    #                the two apart. Inconclusive, not clean.
    #   guard_error  the guard answered on no channel its host reads -- a code outside {0,2},
    #                empty or unparseable stdout where stdout IS the channel, or a decision the
    #                host continues past. It neither allowed nor blocked, so the row proves
    #                nothing either way. Never folded into ALLOW: a crashed adapter must not
    #                read as a clean allow-path run.
    guard_errors = [r for r in results if r["got"] == V_ERROR]
    leaks = [r for r in results
             if r["expect"] == BLOCK and not r["ok"] and r["got"] != V_ERROR]
    false_blocks = [r for r in results
                    if r["expect"] == ALLOW and not r["ok"] and r["got"] != V_ERROR]

    return {
        "results": results,
        "targets": targets,
        "protocols": protocols,
        "leaks": leaks,
        "false_blocks": false_blocks,
        "guard_errors": guard_errors,
        "version": version_report(manifest, guard),
        "wiring": wiring_report(repo_root, guard, manifest),
        "not_tested": [
            "Bash writes beyond simple `>`/`>>`/`tee`/`dd`/`cp` (eval, heredocs, scripts)",
            "MCP tool writes and apply_patch / specialized tool paths",
            "network egress and command allow-list (recorded in the manifest, not yet enforced)",
            "whether each declared target's HOST actually invokes the hook and honours the "
            "deny. This probe proves the guard answers correctly on that host's channel; it "
            "cannot prove the host asked.",
        ],
    }


def render(report: dict) -> str:
    lines = ["# mir probe -- does the guard enforce its manifest?", ""]
    passed = sum(1 for r in report["results"] if r["ok"])
    lines.append(f"{passed}/{len(report['results'])} attacks behaved as the policy requires")
    lines.append("")

    targets = report.get("targets") or []
    if targets:
        shown = ", ".join(
            t["name"] if t["name"] == t["protocol"] else f"{t['name']} (as {t['protocol']})"
            for t in targets)
        lines.append(f"Targets declared by the manifest: {shown}. Each attack is judged on the")
        lines.append("channel that host reads -- exit code for Claude Code and Codex, parsed")
        lines.append("stdout JSON for Antigravity, whose host ignores the exit code entirely.")
        lines.append("")

    # The count above is the number people read as coverage, so the rows it cannot support
    # are named immediately under it rather than in a footnote.
    named = [r for r in report["results"] if r.get("names_entry")]
    weak = [r for r in named if r.get("proves") != PROVES_RULE]
    if weak:
        lines.append(f"{len(weak)} of those {len(named)} rows name a denied_paths entry they do")
        lines.append("not isolate. See the `proves` column: `deny-by-default` means the target")
        lines.append("is outside every allowed_write_root, so the fallback blocks it whether or")
        lines.append("not the entry works, and `pattern-literal` means the entry could not be")
        lines.append("instantiated and its own spelling was fired. Read those as coverage of")
        lines.append("deny-by-default and of nothing else.")
        lines.append("")

    # The tool column is not decoration: a Write and a Bash attack on the same path are
    # different claims (Bash coverage is partial), and without it the two rows read as a
    # duplicate. Neither is `proves`: two BLOCK rows that look identical can be one real test
    # and one tautology.
    # The protocol column is not decoration either: the same event fired at the same guard is
    # a different question per host, because each host reads the verdict off a different
    # channel. Two rows with the same target and different protocols are two real tests.
    lines.append("| target | via | host | expected | got | proves | ok |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in report["results"]:
        mark = "yes" if r["ok"] else "**NO**"
        # The verb is on the row because four Bash rows on one path are four different
        # questions -- one per branch of the guard's shell parser -- not one row repeated.
        via = f"Bash `{r.get('verb', 'redirect')}`" if r["kind"] == "bash" else "Write"
        shows = r.get("proves") or "--"
        lines.append(f"| `{r['path']}` | {via} | {r.get('protocol', '--')} "
                     f"| {r['expect_label']} | {r['got']} | {shows} | {mark} |")
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
        lines.append("## INCONCLUSIVE -- the guard answered on no channel its host reads")
        for r in report["guard_errors"]:
            lines.append(f"- `{r['path']}` ({r['why']}) -- {r.get('got_why') or 'neither allowed nor blocked'}")
        lines.append("")
        lines.append("None of these rows is an ALLOW. A guard that crashes, prints nothing, or")
        lines.append("answers `ask` has enforced nothing -- but it has also not permitted")
        lines.append("anything on the record, so the row proves nothing in either direction.")
        lines.append("")

    version = report.get("version") or {}
    if version:
        lines.append(f"## Manifest version ({version['status']})")
        lines.append("")
        lines.append("| manifest | guard understands | agrees |")
        lines.append("|---|---|---|")
        agrees = "yes" if version["status"] == VERSION_OK else "**NO**"
        lines.append(f"| {version['manifest']!r} | {version['guard']!r} | {agrees} |")
        lines.append("")
        if version["why"]:
            lines.append(version["why"])
            lines.append("")
            lines.append("The guard fails open here and this probe fails closed. That is not")
            lines.append("an inconsistency: a policy that bricks the agent when it has a bug")
            lines.append("is worse than one that is honest about not being loaded, and a")
            lines.append("verifier that calls 'enforcing nothing' clean is worth nothing.")
            lines.append("")

    wiring = report.get("wiring") or {}
    if wiring:
        lines.append(f"## Wiring -- is the hook registered? ({wiring['status']})")
        lines.append("")
        lines.append("Every attack above pipes an event to guard.py directly, so none of them")
        lines.append("read a settings file. These rows are the only ones that do -- and the")
        lines.append("`(command)` row is the only one that reads what the hook actually RUNS.")
        lines.append("A correct matcher over a stale command is enforcement zero.")
        lines.append("")
        lines.append("| target | tool | registered | fires |")
        lines.append("|---|---|---|---|")
        for r in wiring["rows"]:
            mark = "yes" if r["ok"] else "**NO**"
            if not r.get("coverage", True):
                mark = "n/a"      # stated, never counted: see TARGET_WIRING's empty tuple
            # a matcher is an alternation, so its `|` has to be escaped or it splits the row
            shown = str(r["matcher"]).replace("|", "\\|")
            lines.append(f"| {r.get('target', '--')} | {r['tool']} | `{shown}` | {mark} |")
        lines.append("")
        if wiring["failures"]:
            for r in wiring["failures"]:
                lines.append(f"- {r.get('target', '--')} / {r['tool']}: {r['why']}")
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
    it -- the same shipping fact, so the same gate. A version MISMATCH is a LEAK for the same
    reason: the guard fails open on it, so every write lands unchecked. A guard that declares
    no version at all is INCONCLUSIVE, not a leak -- nothing was proven either way, and
    naming a leak the run did not find is the laundering this file exists against, pointed
    backwards.

    `unconfirmed` is the softer code because the probe could not read every settings layer
    Claude Code merges, and reporting "not wired" when the truth is "not visible from here"
    would be its own false claim.
    """
    wiring = report.get("wiring") or {}
    version = report.get("version") or {}
    if (report["leaks"] or wiring.get("status") == "unwired"
            or version.get("status") == VERSION_MISMATCH):
        return EXIT_LEAK
    if (report.get("guard_errors") or wiring.get("status") == "unconfirmed"
            or version.get("status") == VERSION_UNKNOWN):
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
