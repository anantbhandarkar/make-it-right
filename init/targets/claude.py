"""Claude Code: the only target whose write-policy enforcement is `enforced` today.

`merge_settings` lives here rather than in generate.py because it is Claude Code's config
schema and nobody else's -- leaving it in the generator is what made "the generator" and
"the Claude target" the same thing, which is the coupling this package exists to break.
generate.py re-exports it so existing callers keep working.
"""

from __future__ import annotations

import copy
import json
import os

from .base import Capability, Context, GenerateError, Target

# The tools whose writes Claude Code routes to a PreToolUse hook. Kept in sync with
# guard.PATH_FIELD_TOOLS by init/test_guard.py -- a matcher that omits a guarded tool leaves
# that tool's writes completely unguarded while every attack row still says BLOCK.
MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Update|Bash"

HOOK_TAG = "mir-init-guard"

SETTINGS_REL = os.path.join(".claude", "settings.json")


def guard_command(guard_rel: str = ".mir/guard.py") -> str:
    """The command Claude Code registers.

    `--protocol claude` is not decoration: the guard hard-errors (exit 3) without it, so a
    hook registered by an older mir is a v1 command that enforces nothing. probe.py's
    `(command)` row is what turns that from a silent regression into a wiring failure.
    """
    return 'python3 "$CLAUDE_PROJECT_DIR/%s" --protocol claude' % guard_rel


def _mir_hook_entry(entry, cmd: str, guard_rel: str = None) -> bool:
    """Is this PreToolUse entry mir's? By tag first, by the guard it RUNS second.

    The tag is how mir re-finds its own entry after the user has edited around it; the
    command match catches an entry written before the tag existed.

    That second arm matches on the GUARD PATH, not on the whole command string, and the
    `--protocol` migration is why. A v1 command and a v2 command are different strings, so an
    equality test would stop recognising every untagged pre-tag entry the day the flag landed
    -- mir would append a SECOND entry beside it, and the repo would carry one hook that works
    and one that exits 3 on every call. An entry is mir's if it runs mir's guard, whatever
    arguments it passes to it.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("_mir") == HOOK_TAG:
        return True
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        for h in hooks:
            if not isinstance(h, dict) or not isinstance(h.get("command"), str):
                continue
            if h["command"] == cmd:
                return True
            if guard_rel and guard_rel in h["command"]:
                return True
    try:
        return HOOK_TAG in json.dumps(entry)
    except (TypeError, ValueError):
        return False


def merge_settings(existing, guard_rel: str):
    """Reconcile the mir PreToolUse hook to its desired shape. Returns (settings, changed).

    This used to return early the moment it saw the tag, which made the marker a boolean:
    an entry tagged `mir-init-guard` whose matcher had gone stale survived every
    regeneration, and the user gained no coverage while the run reported success. The tag is
    an ADDRESS -- it says WHERE mir's entry is -- so finding it is the start of the work, not
    the end. `changed` is a real before/after comparison, so re-running on a correct file
    still reports no change.

    That property is what carries the v2 migration: an existing v1 entry is found by tag and
    REWRITTEN with the `--protocol claude` command, rather than left in place beside a new
    one. A repo with two entries would have one that works and one that exits 3.
    """
    settings = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    before = json.dumps(settings, sort_keys=True)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise GenerateError(
            "`.claude/settings.json` has a `hooks` key that is not an object, so mir cannot "
            "merge into it without guessing. Fix or move the file and re-run.")
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise GenerateError(
            "`.claude/settings.json` has a `hooks.PreToolUse` that is not a list, so mir "
            "cannot merge into it without guessing. Fix or move the file and re-run.")
    cmd = guard_command(guard_rel)
    desired = {
        "matcher": MATCHER,
        "_mir": HOOK_TAG,
        "hooks": [{"type": "command", "command": cmd}],
    }
    ours = [i for i, entry in enumerate(pre) if _mir_hook_entry(entry, cmd, guard_rel)]
    if ours:
        pre[ours[0]] = desired
        for i in reversed(ours[1:]):
            del pre[i]      # older runs could append a second entry; collapse to exactly one
    else:
        pre.append(desired)
    return settings, json.dumps(settings, sort_keys=True) != before


class ClaudeTarget(Target):
    name = "claude"
    guard_protocol = "claude"
    wiring_files = (".claude/settings.json", ".claude/settings.local.json")
    # Claude Code surfaces a hook error to the user and the run stops being silent, so the
    # adapter may fail open on its own bugs without the write landing unnoticed. Antigravity
    # is the opposite and says so in its own file; neither inherits the other's posture.
    fail_open = True
    summary = "Claude Code: PreToolUse hook in .claude/settings.json, blocks on exit 2"

    capabilities = {
        "skills": Capability(
            level="enforced",
            mechanism="~/.claude/skills (global) and <repo>/.claude/skills (project), "
                      "symlinked by install.sh and `mir init --install`",
            source="https://docs.claude.com/en/docs/claude-code/skills",
            caveats=("personal skills shadow project skills of the same slug",),
        ),
        "subagents": Capability(
            level="enforced",
            mechanism="~/.claude/agents/*.md, frontmatter carries `tools:` so the reviewers' "
                      "read-only restriction survives",
            source="https://docs.claude.com/en/docs/claude-code/sub-agents",
            caveats=(),
        ),
        "always_on_context": Capability(
            level="enforced",
            mechanism="CLAUDE.md imports AGENTS.md with @AGENTS.md",
            source="https://docs.claude.com/en/docs/claude-code/memory",
            caveats=("billed on every request, which is why AGENTS.md stays thin",),
        ),
        "write_policy_enforcement": Capability(
            level="enforced",
            mechanism="PreToolUse hook registered in .claude/settings.json; the guard blocks "
                      "with exit 2 and the reason on stderr",
            source="https://docs.claude.com/en/docs/claude-code/hooks",
            caveats=(
                "hooks are snapshotted at session start, so a freshly generated harness does "
                "not protect the session that generated it -- restart Claude Code",
                "Bash write coverage is PARTIAL: the guard reads tokens, so eval, $VAR and "
                "$(...) targets are not seen",
                "the probe proves the guard decides correctly and that the matcher and "
                "command are registered; it does not watch Claude Code invoke them",
            ),
        ),
    }

    def guard_command(self) -> str:
        return guard_command()

    def plan_items(self, ctx: Context) -> list:
        existing = ctx.read_owned_json(SETTINGS_REL)
        settings, changed = merge_settings(existing, ctx.guard_rel)
        # `changed` travels WITH the item rather than deciding here whether to emit it. An
        # unchanged settings.json still has to be inspected -- a refused destination must be
        # reported even when the content mir would write is identical -- and that inspection
        # is generate.plan()'s, not a target's.
        return [{"path": ".claude/settings.json",
                 "content": json.dumps(settings, indent=2) + "\n",
                 "changed": changed,
                 "note": "PreToolUse hook (merged)" if existing else "PreToolUse hook"}]

    def notes(self, ctx: Context) -> list:
        return ["Claude Code snapshots hooks at session start, so this hook does NOT protect "
                "the current session. Restart Claude Code and approve the new "
                ".claude/settings.json for the guard to take effect."]
