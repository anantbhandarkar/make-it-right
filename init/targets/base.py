"""What a `mir init` target IS: a host, its capabilities, and the files it needs emitted.

A target is not a boolean "supported / not supported". Every claim this repository makes
about a host has to name the mechanism it rests on and the document that mechanism came
from, because "supported" with nothing behind it is the doc-claim-the-code-does-not-support
defect that v2 exists to close, wearing a feature's clothes.

So `Capability` carries four fields and a target must supply four capabilities. The KeyError
is the test: a fifth target added later cannot inherit silence about whether it enforces a
write policy -- it has to say `enforced`, `advisory`, `unverified`, or `none` in its own
file, in front of a reviewer. `capability()` therefore indexes a plain dict and does NOT
`.get()` with a default; a default here would be exactly the silence the design forbids.

`level` is deliberately four values, not three:

  enforced     the guard decides AND the host's hook registration is proven to route to it
  advisory     the manifest is written down and nothing enforces it. Documented, not armed.
  unverified   the file is emitted and the guard answers correctly on the host's channel,
               but nobody has watched the HOST invoke it and honour the deny. This is not a
               softer "enforced": until the manual procedure in GOAL.md B1.10 is run, the
               honest claim is that a path was written where nothing has been SEEN to read.
  none         the host has no such surface at all.

`plan_items(ctx)` returns the same `{path, content, note}` shape generate.plan() already
speaks, so a target adds files by returning them rather than by editing the generator.
"""

from __future__ import annotations

from dataclasses import dataclass

# The levels a Capability may claim. A level outside this set is a typo that would read as a
# claim, so targets/__init__.py validates against it at import time rather than at use time.
LEVELS = ("enforced", "advisory", "unverified", "none")

# The four questions every target answers. Ordered for the COVERAGE.md table, and named here
# rather than in the renderer so adding a fifth question fails every target at once -- which
# is the point: a new question nobody answered must not render as a blank cell.
CAPABILITY_KEYS = ("skills", "subagents", "always_on_context", "write_policy_enforcement")


class GenerateError(Exception):
    """A destination could not be written safely, so nothing was written.

    Defined here rather than in generate.py so a target module can raise it without importing
    the generator. generate.py re-exports the same class object, which is what keeps
    `except gen.GenerateError` in cli.py catching a refusal raised inside targets/claude.py --
    two classes with one name would let a refusal escape as an unhandled traceback.
    """


@dataclass(frozen=True)
class Capability:
    """One claim about one host surface, with the evidence attached.

    `caveats` is a tuple rather than a list because this is a frozen dataclass that gets put
    in dicts and compared; a list field makes it unhashable and turns an ordinary equality
    check into a TypeError at the worst moment. Tuples cost one pair of parentheses.
    """

    level: str
    mechanism: str
    source: str
    caveats: tuple = ()


class Context:
    """Everything a target needs to compute its files, and nothing it needs to reach for.

    `read_owned_json` is injected by generate.py rather than imported, because deciding
    whether a destination is safe to read back is generate.py's job (inspect_destination),
    and a target that opened the file itself would bypass every symlink and ownership check
    that layer exists to perform.
    """

    def __init__(self, repo, repo_name, resolved, answers, stamp, guard_rel,
                 target_names=(), read_owned_json=None):
        self.repo = repo
        self.repo_name = repo_name
        self.resolved = list(resolved)
        self.answers = dict(answers)
        self.stamp = stamp
        self.guard_rel = guard_rel
        self.target_names = tuple(target_names)
        self._read_owned_json = read_owned_json

    def read_owned_json(self, rel):
        """The parsed JSON already at `rel`, or None when there is nothing mir may read.

        None means "absent, or a destination mir does not own" -- deliberately the same
        answer, because a target must not be able to tell the two apart and act on it. The
        refusal for an unowned destination is generate.apply()'s to make, once, for every
        item at the same time.
        """
        if self._read_owned_json is None:
            return None
        return self._read_owned_json(rel)


class Target:
    """A host `mir init` can emit a harness for.

    Subclasses set `name`, `guard_protocol`, `capabilities`, and implement `plan_items`.
    Nothing here has a default that could be inherited by accident: `capabilities` is an
    empty dict on the base class, so a subclass that forgets it raises KeyError on the first
    capability read rather than reporting four blank rows.
    """

    name = ""
    #: Which adapter in guard.py answers for this host. Not always the target's own name:
    #: Cursor reads Claude Code's configuration, so it speaks Claude's protocol.
    guard_protocol = ""
    #: The files the host reads a hook registration from, repo-relative. An EMPTY tuple means
    #: the host registers nothing by design; it must not mean "we did not look".
    wiring_files: tuple = ()
    #: Whether this target's adapter allows a write when the guard itself fails. Stated per
    #: target because the two hosts disagree and inheriting the wrong one is silent: Claude
    #: Code surfaces a hook error to the user, Antigravity logs it and proceeds.
    fail_open = True
    #: One-line summary printed by the CLI and COVERAGE.md.
    summary = ""
    #: {capability key -> Capability}. Empty on the base class DELIBERATELY: a subclass that
    #: forgets it raises KeyError on the first read instead of rendering four blank rows.
    capabilities: dict = {}

    def capability(self, key):
        """The Capability for `key`. A missing key raises KeyError, on purpose -- see the
        module docstring. Do not add a default."""
        return self.capabilities[key]

    def guard_command(self) -> str:
        """The command the host must run for the guard to fire. Subclasses override."""
        raise NotImplementedError

    def plan_items(self, ctx: Context) -> list:
        """[{path, content, note}] this target contributes. May be empty -- see cursor.py,
        where empty is the whole finding."""
        return []

    def notes(self, ctx: Context) -> list:
        """Lines the CLI prints after a run. Where a target has a manual step the generator
        cannot take, this is where it is named."""
        return []


def check_capabilities(target: Target) -> list:
    """Errors in one target's capability table. Empty list means it is well formed.

    Run at import time by targets/__init__.py so a malformed target fails the process that
    loaded it, not the report that rendered it. A capability table is a claim about a
    security surface; discovering it is empty while writing COVERAGE.md is discovering it
    one layer too late.
    """
    errs = []
    caps = target.capabilities
    if not isinstance(caps, dict):
        return ["%s.capabilities is not a dict" % target.name]
    for key in CAPABILITY_KEYS:
        if key not in caps:                       # the B1.1 KeyError, surfaced as a message
            errs.append("%s declares no capability for %r" % (target.name, key))
            continue
        cap = caps[key]
        if not isinstance(cap, Capability):
            errs.append("%s.%s is not a Capability" % (target.name, key))
            continue
        if cap.level not in LEVELS:
            errs.append("%s.%s.level is %r, expected one of %s"
                        % (target.name, key, cap.level, LEVELS))
        if not cap.mechanism:
            errs.append("%s.%s names no mechanism" % (target.name, key))
        if not cap.source:
            errs.append("%s.%s cites no source" % (target.name, key))
    return errs
