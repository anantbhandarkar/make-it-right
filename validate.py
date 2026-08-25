#!/usr/bin/env python3
"""Validate the Make It Right skill tree.

Checks the things the naming convention promises but nothing enforced:
a skill that references another skill by name must reference one that exists.

Run it before you commit, and in CI:

    ./validate.py            # human output
    ./validate.py --quiet    # errors only
    ./validate.py --json     # machine output

Exit codes: 0 clean (warnings allowed), 1 errors found, 2 the tree is unreadable.

Planned-but-unwritten skills go in .mir-planned, one slug per line. They are
reported as warnings instead of errors, so a roadmap stays visible and
deliberate rather than silently rotting into a broken chain.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / "skills"
AGENTS = ROOT / "agents"
PLANNED_FILE = ROOT / ".mir-planned"
GEN_DIAGRAMS = ROOT / "docs" / "gen_diagrams.py"

# A skill reference in prose. Trailing '-' means it was a <placeholder> such as
# mir-backend-<runtime>, which is documentation, not a reference.
SKILL_REF = re.compile(r"\bmir-[a-z0-9]+(?:-[a-z0-9]+)*\b")

# references/foo.md mentioned in the body
REF_FILE = re.compile(r"references/([A-Za-z0-9._-]+\.md)")

# subagent_type:"security-reviewer"  /  subagent_type: 'x'  /  the **`name`** form
SUBAGENT = re.compile(r"subagent_type\s*:\s*[\"']([a-z0-9-]+)[\"']")

FRONTMATTER_KEYS = ["name", "description", "trigger", "argument-hint", "allowed-tools"]

# A SKILL.md is loaded whole once its description matches. Past this it competes
# with the task for context. ~4 chars/token, so 900 lines of markdown is roughly
# 5k tokens -- the documented ceiling in EXTENDING.md.
BODY_LINE_WARN = 380
DESC_MIN_CHARS = 200
DESC_MAX_CHARS = 2400


@dataclass
class Problem:
    level: str  # "error" | "warn"
    skill: str
    code: str
    message: str

    def __str__(self) -> str:
        tag = "ERROR" if self.level == "error" else "warn "
        return f"{tag}  {self.skill:<34} {self.code}  {self.message}"


@dataclass
class Skill:
    slug: str
    path: Path
    front: dict = field(default_factory=dict)
    body: str = ""
    body_lines: int = 0


def parse_frontmatter(text: str, slug: str, problems: list[Problem]) -> tuple[dict, str]:
    """Parse the small YAML subset these files use. Not a general YAML parser."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        problems.append(Problem("error", slug, "FM001", "SKILL.md does not start with '---'"))
        return {}, text

    try:
        close = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        problems.append(Problem("error", slug, "FM002", "frontmatter is never closed with '---'"))
        return {}, text

    front: dict = {}
    key = None
    for raw in lines[1:close]:
        if not raw.strip():
            continue
        if raw.lstrip().startswith("- ") and key:
            front.setdefault(key, [])
            if isinstance(front[key], list):
                front[key].append(raw.lstrip()[2:].strip())
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1]
        front[key] = value if value else []

    return front, "\n".join(lines[close + 1:])


def load_planned() -> set[str]:
    if not PLANNED_FILE.exists():
        return set()
    out = set()
    for line in PLANNED_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def check_chain(slug: str, known: set[str], planned: set[str], problems: list[Problem]) -> None:
    """mir-a-b-c must have mir-a-b and mir-a present.

    The tree is flat on disk; the name is the only thing expressing the chain.
    A module whose parent tier is missing will never get the tier's content
    loaded, and no host will tell you.
    """
    parts = slug.split("-")
    if len(parts) < 3:
        return
    for depth in range(2, len(parts)):
        parent = "-".join(parts[:depth])
        if parent in known:
            continue
        if parent in planned:
            problems.append(Problem("warn", slug, "CHN002", f"parent tier '{parent}' is planned but not written"))
        else:
            problems.append(Problem("error", slug, "CHN001", f"parent tier '{parent}' does not exist — this skill's chain is broken"))


def check_diagrams(problems: list[Problem]) -> int:
    """Re-derive every `mir:gen` diagram block and report what disagrees. Returns the count.

    The whole point of putting this in validate.py rather than in a standalone script is the
    enforcement chain that already exists and costs nothing to reuse: install.sh runs
    `validate.py --quiet` and refuses to install on any error, so DIA001 being an error means
    a stale diagram blocks installation. No new plumbing, no new CI step, no new habit to
    remember.

    The generator is loaded with importlib rather than subprocess: in-process, stdlib, no
    interpreter start-up per run, and -- the reason that actually matters -- an ImportError
    surfaces here as a message instead of as an opaque non-zero exit code. It is wrapped so
    that a DELETED or broken generator is LOUD. A diagram checker that silently passes when
    its generator is missing is worse than no checker, because every block in the tree then
    reads as verified and none of them are.
    """
    if not GEN_DIAGRAMS.exists():
        problems.append(Problem("error", "docs/gen_diagrams.py", "DIA001",
                                "the diagram generator is missing, so no mir:gen block in this "
                                "repo can be verified — restore it or delete the blocks"))
        return 0
    try:
        spec = importlib.util.spec_from_file_location("mir_gen_diagrams", GEN_DIAGRAMS)
        gen = importlib.util.module_from_spec(spec)
        # Registered BEFORE exec_module, which is the documented recipe and not optional
        # here: @dataclass resolves a string annotation via sys.modules[cls.__module__], so
        # a module executed while absent from sys.modules dies on its first dataclass with
        # an AttributeError that says nothing about the real cause.
        sys.modules[spec.name] = gen
        spec.loader.exec_module(gen)
        report = gen.check_all()
    except Exception as e:                                  # noqa: BLE001 - deliberately broad
        # Broad on purpose. Any failure to run the generator means every block is unverified,
        # and which exception it was does not change that verdict -- only the message.
        problems.append(Problem("error", "docs/gen_diagrams.py", "DIA001",
                                f"the diagram generator did not run ({e.__class__.__name__}: "
                                f"{e}), so no mir:gen block could be verified"))
        return 0

    for rel, line, msg in report["markers"]:
        problems.append(Problem("error", rel, "DIA002", f"line {line}: {msg}"))
    for rel, did, _diff in report["drift"]:
        problems.append(Problem("error", rel, "DIA001",
                                f"the committed block id={did} is not what the generator "
                                f"produces from the current tree — run "
                                f"`python3 docs/gen_diagrams.py --write` (use --check for the diff)"))
    for did, n in report["oversize"]:
        problems.append(Problem("warn", did, "DIA003",
                                f"diagram has {n} nodes, over SOFT_NODES={gen.SOFT_NODES}; "
                                f"past this it stops being readable on a narrow screen"))
    for did in report["unembedded"]:
        problems.append(Problem("warn", did, "DIA004",
                                "the generator produces this diagram but no document embeds "
                                "it — it will never be seen"))
    return len(report["ids"])


def validate() -> tuple[list[Problem], dict]:
    problems: list[Problem] = []

    if not SKILLS.is_dir():
        print(f"no skills/ directory at {SKILLS}", file=sys.stderr)
        sys.exit(2)

    planned = load_planned()
    dirs = sorted(d for d in SKILLS.iterdir() if d.is_dir() and not d.name.startswith("."))
    known = {d.name for d in dirs}
    agent_names = {p.stem for p in AGENTS.glob("*.md")} if AGENTS.is_dir() else set()

    skills: list[Skill] = []
    for d in dirs:
        md = d / "SKILL.md"
        if not md.exists():
            problems.append(Problem("error", d.name, "SK001", "directory has no SKILL.md"))
            continue
        text = md.read_text(encoding="utf-8")
        front, body = parse_frontmatter(text, d.name, problems)
        skills.append(Skill(d.name, d, front, body, len(body.splitlines())))

    for s in skills:
        slug, front = s.slug, s.front

        # -- frontmatter contract -------------------------------------------
        for key in FRONTMATTER_KEYS:
            if key not in front:
                problems.append(Problem("error", slug, "FM003", f"frontmatter is missing '{key}'"))

        if front.get("name") and front["name"] != slug:
            problems.append(Problem("error", slug, "FM004", f"frontmatter name '{front['name']}' != directory '{slug}'"))

        if front.get("trigger") and front["trigger"] != f"/{slug}":
            problems.append(Problem("error", slug, "FM005", f"trigger '{front['trigger']}' should be '/{slug}'"))

        # -- the description is the router (EXTENDING.md Rule 1) -------------
        desc = front.get("description", "")
        if isinstance(desc, str) and desc:
            if "TRIGGER" not in desc:
                problems.append(Problem("error", slug, "RTR001", "description has no TRIGGER clause — the host cannot route to it"))
            if "SKIP" not in desc:
                problems.append(Problem("error", slug, "RTR002", "description has no SKIP clause — it will over-trigger onto sibling tasks"))
            if len(desc) < DESC_MIN_CHARS:
                problems.append(Problem("warn", slug, "RTR003", f"description is {len(desc)} chars; siblings run {DESC_MIN_CHARS}+ and route better"))
            if len(desc) > DESC_MAX_CHARS:
                problems.append(Problem("warn", slug, "RTR004", f"description is {len(desc)} chars — it is billed at every request on some hosts"))

        # -- chain integrity -------------------------------------------------
        check_chain(slug, known, planned, problems)

        # -- every mir-* it names must exist ---------------------------------
        seen_refs = set()
        for m in SKILL_REF.finditer(s.body):
            ref = m.group(0)
            if ref == slug or ref in seen_refs:
                continue
            seen_refs.add(ref)
            if ref in known:
                continue
            if ref in planned:
                problems.append(Problem("warn", slug, "REF002", f"references '{ref}', which is planned but not written"))
            else:
                problems.append(Problem("error", slug, "REF001", f"references '{ref}', which does not exist"))

        # -- every references/x.md it tells the model to read must exist -----
        for m in REF_FILE.finditer(s.body):
            name = m.group(1)
            if not (s.path / "references" / name).exists():
                # It may legitimately point at a sibling's reference file.
                if not any((o.path / "references" / name).exists() for o in skills if o.slug != slug):
                    problems.append(Problem("error", slug, "REF003", f"tells the model to read references/{name}, which does not exist"))

        # -- reviewer sub-agents it dispatches must be installed -------------
        for m in SUBAGENT.finditer(s.body):
            name = m.group(1)
            if agent_names and name not in agent_names and name != "general-purpose":
                problems.append(Problem("error", slug, "AGT001", f"dispatches subagent_type '{name}', which is not in agents/"))

        # -- security section is mandatory -----------------------------------
        if not re.search(r"^#{1,3}\s+.*security", s.body, re.IGNORECASE | re.MULTILINE):
            problems.append(Problem("error", slug, "SEC001", "no Security section"))

        # -- context budget ---------------------------------------------------
        if s.body_lines > BODY_LINE_WARN:
            problems.append(Problem("warn", slug, "CTX001", f"body is {s.body_lines} lines; it loads whole and competes with the task"))

    # -- orphaned reference files -------------------------------------------
    for s in skills:
        refdir = s.path / "references"
        if not refdir.is_dir():
            continue
        for f in sorted(refdir.glob("*.md")):
            if f.name not in s.body:
                problems.append(Problem("warn", s.slug, "REF004", f"references/{f.name} is never read by SKILL.md — it will not load"))

    planned_but_present = sorted(planned & known)
    for slug in planned_but_present:
        problems.append(Problem("warn", slug, "PLN001", "listed in .mir-planned but now exists — remove it from that file"))

    # -- generated diagrams (DIA family) -------------------------------------
    # Beside REF004/PLN001 because it is the same kind of check: cross-cutting, about the
    # repository rather than about one skill, and only answerable once the whole tree is read.
    n_diagrams = check_diagrams(problems)

    stats = {
        "skills": len(skills),
        "pillars": sum(1 for s in skills if s.slug.count("-") == 1),
        "tiers": sum(1 for s in skills if s.slug.count("-") == 2),
        "modules": sum(1 for s in skills if s.slug.count("-") >= 3),
        "reviewers": len(agent_names),
        "planned": len(planned),
        "diagrams": n_diagrams,
        "errors": sum(1 for p in problems if p.level == "error"),
        "warnings": sum(1 for p in problems if p.level == "warn"),
    }
    return problems, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate the Make It Right skill tree.")
    ap.add_argument("--quiet", action="store_true", help="print errors only")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    problems, stats = validate()
    errors = [p for p in problems if p.level == "error"]
    warns = [p for p in problems if p.level == "warn"]

    if args.json:
        print(json.dumps({
            "stats": stats,
            "problems": [{"level": p.level, "skill": p.skill, "code": p.code, "message": p.message} for p in problems],
        }, indent=2))
        return 1 if errors else 0

    shown = errors if args.quiet else errors + warns
    for p in sorted(shown, key=lambda p: (p.level != "error", p.skill, p.code)):
        print(p)

    if not args.quiet:
        print()
        print(f"{stats['skills']} skills  "
              f"({stats['pillars']} pillars, {stats['tiers']} tiers, {stats['modules']} modules)  "
              f"· {stats['reviewers']} reviewers · {stats['planned']} planned "
              f"· {stats['diagrams']} diagrams")
    print(f"{stats['errors']} errors, {stats['warnings']} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
