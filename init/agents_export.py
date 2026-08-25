"""Derive per-target sub-agent definitions from `agents/*.md` frontmatter. One source only.

The reviewer agents in `agents/*.md` stay the single source of truth. This module parses their
frontmatter and converts it; it must never become -- and must never require -- a checked-in
`agents/codex/*.toml` catalog beside them. A parallel catalog violates derive-from-frontmatter
exactly the way a parallel skill catalog would: two files that must agree, one check that
notices when they stop, and no reason to believe the check exists.

What this module actually finds today, stated first because it is the finding:

  * **Codex has no directory for standalone agent definitions.** Agent `.md` files planted in
    four candidate locations appeared in none of them. So `export_items("codex", ...)` returns
    NOTHING and says why. Emitting a file at a guessed path would be defect D6 again -- 46
    skills linked into `~/.gemini/antigravity/skills` for a whole release because a path that
    LOOKED right was never scanned. A file that exists is not a file that is read.
  * **The conversion is lossy even if a path is found later.** Codex sub-agent TOML has no
    `tools:` equivalent, so the reviewers' read-only restriction -- `tools: Read, Grep, Glob,
    Bash`, which is what makes a "reviewer" a reviewer and not an editor -- cannot be
    expressed. That is a real capability loss, not a formatting difference, and LOSSY_FIELDS
    exists so it is PRINTED in COVERAGE.md rather than dropped.

`to_codex_toml` is written and tested even though nothing installs its output. It is what
makes the loss demonstrable: a reader can see the `tools:` line go in and no `tools` key come
out. A documented loss nobody can reproduce is a claim, and this repository does not ship
those.

Stdlib only, and no yaml dependency: the frontmatter is parsed with a small reader that
handles the flat `key: value` shape these six files actually use, because adding a
third-party parse step to a generator that must run on stock macOS python3 is a dependency
for six files with no nesting in them.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(os.path.dirname(HERE), "agents")

# Every field that does not survive conversion, per target, with what it costs. Surfaced in
# .mir/COVERAGE.md by generate.py. A loss recorded here and not rendered there is a loss that
# is documented to nobody.
LOSSY_FIELDS = {
    "codex": [
        {
            "field": "tools",
            "cost": "Codex sub-agent TOML has no `tools:` equivalent, so the reviewers' "
                    "read-only restriction cannot be expressed. A reviewer converted to "
                    "Codex could edit the code it was spawned to review. This is a real "
                    "capability loss, not a formatting difference.",
        },
        {
            "field": "(the whole file)",
            "cost": "no directory for standalone agent definitions could be found, so mir "
                    "emits nothing for Codex agents rather than inventing a path. The "
                    "reviewer sub-agents are UNAVAILABLE on Codex, not merely degraded.",
        },
    ],
    "antigravity": [
        {
            "field": "(the whole file)",
            "cost": "no path for standalone sub-agent definitions has been confirmed, so mir "
                    "emits nothing. Whether subagent tool calls even route through the same "
                    "hook wrapper is also unresolved -- if they do not, a subagent's writes "
                    "bypass the guard entirely.",
        },
    ],
    "cursor": [
        {
            "field": "(the whole file)",
            "cost": "no sub-agent definition format mir can emit.",
        },
    ],
}

# Claude Code is absent from LOSSY_FIELDS on purpose: nothing is lost there. install.sh
# symlinks agents/*.md into ~/.claude/agents unchanged, frontmatter and all, so the `tools:`
# restriction survives. An empty entry would read as "checked, found nothing", which is true
# here -- but so would a missing one, and only one of the two stays true if that changes.


def parse_frontmatter(text: str) -> dict:
    """The flat `key: value` frontmatter of one agent file.

    Deliberately small. These six files have no nesting, no lists, and no block scalars, and
    a hand-rolled reader that silently mangled one of those would be worse than a dependency.
    So anything it cannot read it does NOT guess at: a line without a colon is skipped, and
    the caller checks for the keys it needs rather than trusting a default.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[:1].isspace() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key.strip()] = val
    return out


def read_agents(agents_dir: str = None) -> list:
    """[{name, description, tools, model, body, file}] for every agent .md, sorted by name.

    An agent whose frontmatter carries no `name` is SKIPPED and reported by `problems()`
    rather than being given its filename as a name. A generated definition addressed by a
    guessed name is a sub-agent nobody can spawn, which is the silent-no-content failure this
    tree already fixed once at the skill layer.
    """
    agents_dir = agents_dir or AGENTS_DIR
    out = []
    if not os.path.isdir(agents_dir):
        return out
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(agents_dir, fn)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fm = parse_frontmatter(text)
        if not fm.get("name"):
            continue
        body = text.split("---", 2)[2].lstrip("\n") if text.count("---") >= 2 else ""
        out.append({
            "name": fm["name"],
            "description": fm.get("description", ""),
            "tools": fm.get("tools", ""),
            "model": fm.get("model", ""),
            "body": body,
            "file": os.path.relpath(path, os.path.dirname(agents_dir)),
        })
    return sorted(out, key=lambda a: a["name"])


def problems(agents_dir: str = None) -> list:
    """Agent files this module could not derive from. Empty is the expected state."""
    agents_dir = agents_dir or AGENTS_DIR
    bad = []
    if not os.path.isdir(agents_dir):
        return ["no agents/ directory at %s" % agents_dir]
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(agents_dir, fn), encoding="utf-8") as f:
            fm = parse_frontmatter(f.read())
        if not fm.get("name"):
            bad.append("%s has no `name` in its frontmatter, so nothing can address it" % fn)
    return bad


def _toml_str(s: str) -> str:
    """A TOML basic string. Only the escapes TOML requires, because a hand-rolled quoter that
    over-escapes produces a file that parses and says the wrong thing."""
    return '"%s"' % (s.replace("\\", "\\\\").replace('"', '\\"')
                     .replace("\n", "\\n").replace("\t", "\\t"))


def to_codex_toml(agent: dict) -> str:
    """One agent as Codex sub-agent TOML.

    `tools` is NOT emitted, and that omission is the point of this function existing: there
    is no key to put it in. The comment goes into the generated text so that a human who ever
    does find a path to install these reads the loss in the artifact rather than in a
    changelog.
    """
    lines = [
        "# Derived from %s by init/agents_export.py. Do not edit: edit the .md." % agent["file"],
        "# LOSS: the source declares `tools: %s`. Codex sub-agent TOML has no `tools:`"
        % (agent["tools"] or "(none)"),
        "# equivalent, so this agent is NOT restricted to those tools here.",
        "[agents.%s]" % agent["name"],
        "description = %s" % _toml_str(agent["description"]),
    ]
    if agent["model"]:
        lines.append("model = %s" % _toml_str(agent["model"]))
    lines.append('instructions = """')
    lines.append(agent["body"].rstrip("\n").replace('"""', '\\"\\"\\"'))
    lines.append('"""')
    return "\n".join(lines) + "\n"


# Why each target gets no emitted agent file. A target absent from this table and absent from
# the emitters below would silently produce nothing, which is the one outcome this module
# refuses: "nothing was emitted" and "nothing needed emitting" must not look the same.
NO_EXPORT_REASON = {
    "claude": "install.sh symlinks agents/*.md into ~/.claude/agents unchanged, so `mir init` "
              "has nothing to convert and nothing is lost.",
    "codex": "no directory for standalone Codex agent definitions could be found (four "
             "candidate locations, none loaded). mir does not invent a path.",
    "antigravity": "no path for standalone Antigravity sub-agent definitions has been "
                   "confirmed. mir does not invent a path.",
    "cursor": "Cursor has no sub-agent definition format mir can emit.",
}


def export_items(target_name: str, agents_dir: str = None) -> list:
    """[{path, content, note}] of agent files to emit for one target.

    EMPTY for every target today, and that is a finding rather than a stub. `notes()` carries
    the reason, and generate.py prints it in COVERAGE.md, so the emptiness is on the page
    instead of in the gap between two functions.
    """
    return []


def notes(target_name: str, agents_dir: str = None) -> list:
    """Lines for COVERAGE.md: what was not emitted for this target, and what it cost."""
    out = []
    reason = NO_EXPORT_REASON.get(target_name)
    if reason:
        out.append(reason)
    for loss in LOSSY_FIELDS.get(target_name, []):
        out.append("LOSS `%s`: %s" % (loss["field"], loss["cost"]))
    return out


if __name__ == "__main__":
    import sys

    bad = problems()
    for b in bad:
        print("PROBLEM", b, file=sys.stderr)
    for a in read_agents():
        print("#", a["name"], "--", a["tools"] or "(no tools restriction)")
    print()
    for t in sorted(NO_EXPORT_REASON):
        print("%s:" % t)
        for n in notes(t):
            print("  -", n)
    sys.exit(1 if bad else 0)
