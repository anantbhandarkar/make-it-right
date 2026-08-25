"""The targets `mir init` can emit a harness for.

`ALL` is the registry. Adding a fifth host means adding a module here and appending it --
and the import-time validation below is what makes that addition state its enforcement level
instead of inheriting silence. A target with a missing or malformed capability raises on
IMPORT, not on render: a capability table is a claim about a security surface, and finding it
empty while writing COVERAGE.md is finding it one layer too late.

Two facts a new target has to be told, because neither is discoverable from this file:

  - probe.TARGET_WIRING must gain the target's registration file, or the wiring phase FAILS
    it by design. "The probe does not know where this host registers a hook" is a finding,
    not a skip -- "we did not look" reads identically to "it is fine" in a green report.
  - guard.py must gain a protocol adapter, or `--protocol <new>` exits 3. That is also by
    design: defaulting an unknown protocol to claude is how a mis-wired hook runs the wrong
    parser, extracts zero write targets, and allows everything while reporting clean.
"""

from __future__ import annotations

from .antigravity import AntigravityTarget
from .base import CAPABILITY_KEYS, Capability, Context, GenerateError, LEVELS, Target, \
    check_capabilities
from .claude import ClaudeTarget
from .codex import CodexTarget
from .cursor import CursorTarget

ALL = (ClaudeTarget(), CodexTarget(), AntigravityTarget(), CursorTarget())

# The target a bare `mir init` writes a harness for, and the target every manifest written
# before cross-agent init declared implicitly. Kept equal to probe.DEFAULT_TARGET by
# init/test_generate.py, because two files disagreeing about the implicit default is how an
# older harness ends up verifying nothing while reporting a clean run.
DEFAULT = "claude"

_errs = [e for t in ALL for e in check_capabilities(t)]
if _errs:      # a claim about a security surface, checked where it is cheapest to fix
    raise ImportError("init/targets is malformed:\n  " + "\n  ".join(_errs))
del _errs

BY_NAME = {t.name: t for t in ALL}
NAMES = tuple(t.name for t in ALL)


def resolve(spec) -> list:
    """Target objects for a name, a comma list, `all`, or an iterable of names.

    An unknown name raises ValueError naming what IS available, rather than being dropped:
    `mir init --target codx` must not quietly produce a Claude-only harness that the user
    then believes covers Codex.
    """
    if isinstance(spec, str):
        names = [s.strip() for s in spec.split(",") if s.strip()]
    else:
        names = [str(s).strip() for s in (spec or []) if str(s).strip()]
    if not names:
        names = [DEFAULT]

    out, seen = [], set()
    for n in names:
        if n == "all":
            for t in ALL:
                if t.name not in seen:
                    seen.add(t.name)
                    out.append(t)
            continue
        if n not in BY_NAME:
            raise ValueError("unknown --target %r; known targets are %s (or `all`)"
                             % (n, ", ".join(NAMES)))
        if n not in seen:
            seen.add(n)
            out.append(BY_NAME[n])
    return out


def manifest_block(targets) -> dict:
    """The manifest's `targets` block: {name: {protocol, wiring_files, level}}.

    Written into the manifest so probe.py reads the declared set from the HARNESS rather than
    from the flags of whoever last ran the probe -- a target cannot be dropped from
    verification by re-running with narrower arguments. `level` is carried along so a reader
    of the manifest alone can see which targets are unverified without cross-referencing
    COVERAGE.md.
    """
    return {t.name: {
        "protocol": t.guard_protocol,
        "wiring_files": list(t.wiring_files),
        "write_policy_enforcement":
            t.capability("write_policy_enforcement").level,
    } for t in targets}


def enforcement_groups(targets) -> dict:
    """{level: [names]} for the COVERAGE.md verdict block and the AGENTS.md paragraph.

    Grouping by the DECLARED level rather than by "did we write a file" is deliberate: Codex
    and Antigravity both get a file, and neither is enforced. The file is not the claim.
    """
    groups: dict = {}
    for t in targets:
        groups.setdefault(t.capability("write_policy_enforcement").level, []).append(t.name)
    return groups


__all__ = [
    "ALL", "BY_NAME", "CAPABILITY_KEYS", "Capability", "Context", "DEFAULT", "GenerateError",
    "LEVELS", "NAMES", "Target", "check_capabilities", "enforcement_groups", "manifest_block",
    "resolve",
]
