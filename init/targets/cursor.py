"""Cursor: the genuine advisory-only case, and the one target that emits NO enforcement file.

Cursor has no documented pre-tool hook of its own. What it has is the ability to read Claude
Code's configuration, behind a user-level setting -- *Include third-party Plugins, Skills, and
other configs* -- which is off until a human turns it on, per user, not per repo.

So the honest output here is nothing. Emitting a `.cursor/`-shaped file that no product reads
would be defect D6 repeated: `install.sh` linked 46 skills into
`~/.gemini/antigravity/skills` for an entire release because a path that LOOKED right was
never scanned, and a populated directory was mistaken for evidence that something read it.
A file that exists is not a file that is read.

`write_policy_enforcement` is therefore `advisory` rather than `unverified`. The distinction
matters: `unverified` means a mechanism exists and nobody has watched it work, `advisory`
means there is no mechanism. Collapsing them would let Cursor's row inherit the shape of a
claim that might still come true.

probe.py's TARGET_WIRING maps cursor to an EMPTY tuple, and its row renders as `n/a` and is
never counted as coverage -- a green row that cannot fail is worse than no row.
"""

from __future__ import annotations

from .base import Capability, Context, Target

# The exact setting a user has to enable, spelled as the product spells it. B1.2 requires the
# CLI to name it, because "enable third-party configs" is not something a user can find.
TOGGLE = "Include third-party Plugins, Skills, and other configs"


class CursorTarget(Target):
    name = "cursor"
    # Cursor reads Claude Code's configuration when the toggle is on, so if anything fires
    # here it is Claude's hook speaking Claude's protocol. The target's name and the protocol
    # it speaks are deliberately allowed to differ; probe.declared_targets reads the spec's
    # `protocol` key for exactly this case.
    guard_protocol = "claude"
    #: EMPTY BY DESIGN, and this comment is the reason it must stay empty: an empty tuple
    #: means "registers nothing", which is a finding. It must never come to mean "we did not
    #: look" -- probe.py refuses to count this row as coverage on that basis.
    wiring_files = ()
    fail_open = True
    summary = ("Cursor: advisory only. No enforcement file is written; it can read Claude "
               "Code's config behind a user-level toggle")

    capabilities = {
        "skills": Capability(
            level="advisory",
            mechanism="reads Claude Code's skills directory only when the user enables "
                      "'%s'" % TOGGLE,
            source="https://cursor.com/docs",
            caveats=("the toggle is per-user, not per-repo, so a checked-in harness cannot "
                     "turn it on for a teammate",),
        ),
        "subagents": Capability(
            level="none",
            mechanism="no sub-agent definition format mir can emit",
            source="https://cursor.com/docs",
            caveats=(),
        ),
        "always_on_context": Capability(
            level="enforced",
            mechanism="root AGENTS.md",
            source="https://cursor.com/docs",
            caveats=(),
        ),
        "write_policy_enforcement": Capability(
            level="advisory",
            mechanism="NONE. There is no documented pre-tool hook. The manifest is written "
                      "down and nothing enforces it",
            source="https://cursor.com/docs",
            caveats=(
                "mir writes NO Cursor-specific file. A path emitted where nothing reads it "
                "is defect D6, which cost a release",
                "if the user enables '%s', Cursor may read Claude Code's PreToolUse hook -- "
                "that is Claude's row, not Cursor's, and it is unverified in both "
                "directions" % TOGGLE,
                "NOT PROVEN: whether enforcement actually follows the toggle. GOAL.md B1.10 "
                "names the toggle-off / toggle-on procedure",
            ),
        ),
    }

    def guard_command(self) -> str:
        # There is nothing to register. Saying so is the answer, not raising: a caller asking
        # "what command does cursor run" deserves the finding, not a traceback.
        return ""

    def plan_items(self, ctx: Context) -> list:
        return []

    def notes(self, ctx: Context) -> list:
        return ["Cursor: NO enforcement file was written, because there is nothing that reads "
                "one. The manifest is ADVISORY here. Cursor can read Claude Code's config "
                "only if you enable the setting '%s' -- and even then, that mir's guard "
                "actually fires is unverified. See .mir/COVERAGE.md." % TOGGLE]
