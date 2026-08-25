#!/usr/bin/env python3
"""Generate the repository's Mermaid diagrams from the skill tree, and prove they are current.

    python3 docs/gen_diagrams.py --list          # every diagram id, its kind and node count
    python3 docs/gen_diagrams.py --stdout <id>   # one block, markers included, ready to paste
    python3 docs/gen_diagrams.py --check         # exit 1 with a unified diff on drift
    python3 docs/gen_diagrams.py --write         # the ONLY mutating mode

Why a generator at all. Three documents in this repo already disagreed about how many
skills exist, because three humans counted by hand at three different times. A Mermaid
tree drawn by hand joins that list within one commit. So the diagrams that reflect the
tree are derived from `init/catalog.py` -- the same module `mir init` resolves against --
and `validate.py` re-derives them on every run (the `DIA` family), which means a stale
diagram fails `install.sh` before it can be installed.

THE INVARIANT THIS FILE EXISTS TO PROTECT: **no timestamp, no content hash and no version
string ever goes inside a managed block.** `init/generate.py` stamps `generated_at` into
its artifacts, and that is correct there -- those files are written once per project. These
blocks are re-derived on every `validate.py` run and compared byte-for-byte, so a stamp
would make `--check` fail on an unmodified tree, every single time, for a reason that has
nothing to do with the tree. The mechanism would become noise inside a week and then be
switched off. `init/test_diagrams.py` proves this by mutation: it patches a stamp into a
copy of this file and asserts `--check` then fails on a clean checkout.

Everything else follows from wanting byte-exact comparison: `sorted()` everywhere, no
`set` iteration order in output, `\\n` line endings, and no clock, network, or environment
read anywhere in the module.

Rendering rules, which are where this kind of work usually ships broken:

  A. Never set `theme:`. GitHub auto-detects the reader's light/dark preference and
     re-colours a Mermaid block ONLY if the block does not pin a theme. Pinning one
     guarantees that one of the two modes is unreadable.
  B. For unstyled nodes and for every edge, style nothing. Auto-theming already handles
     them correctly in both modes.
  C. When a node must carry meaning by colour, set `fill`, `stroke` AND `color` together,
     with a mid-tone fill. Setting `fill` without `color` is the specific bug that yields
     light-on-light text in dark mode. Hex only -- Mermaid rejects colour names.

  Colour is never the ONLY carrier: every styled node also says in words what its colour
  means, because the text version below the diagram has no colour at all.

Targeting Mermaid 10.x, not 11.x. GitHub does not document its pinned version and
consistently trails upstream, so the intersection is what is safe: `flowchart` and
`sequenceDiagram` only, no `-beta` types, no YAML frontmatter config, no backticks in
labels (v11 parses labels as Markdown, v10 does not).

Node ids are sanitised. Slugs contain hyphens and `mir-backend --> mir-backend-python`
is ambiguous to the flowchart parser, so the slug appears only inside a quoted label and
the id is a role prefix plus underscores: `p_` pillar, `t_` tier, `m_` module, `x_`
planned-but-unwritten. The prefix keeps ids unique across shards as well as within one.

Every block ships three things built from ONE `Diagram` value, so they cannot disagree:
the Mermaid block with `accTitle`/`accDescr`, a `<details>` text version, and a link list.
The text version is not optional -- Mermaid conveys no node relationships to assistive
technology at all, and this repository is also read in `less`, on mirrors that do not run
Mermaid, and in agent context windows where it never renders. The link list exists
separately because node links INSIDE a diagram do not work on GitHub (`securityLevel:
strict` plus the CSP on the rendering iframe), so `click` directives would be dead ink.

Sharding is enforced here, not left to an author's judgement: `SOFT_NODES` warns and
`MAX_NODES` refuses. The backend runtime grouping below is the one hand-authored datum in
this file, and a runtime that lands in no group raises -- silently omitting a runtime from
the only picture of the tree is exactly the drift the generator was written to stop.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Set as module globals rather than derived at call sites so a test can point a copy of
# this module at the real checkout. Nothing at import time touches the filesystem.
ROOT = Path(__file__).resolve().parent.parent
INIT_DIR = ROOT / "init"

SRC_REL = "docs/gen_diagrams.py"

# A shard past ~16 nodes stops being readable on a phone; past ~24 it is a hairball that
# nobody reads at any width. The soft limit is a warning (DIA003) because a 17-node shard
# is a judgement call; the hard limit refuses, because "the author will reshard it later"
# is how the 46-node tree the plan rejected would get built one node at a time.
SOFT_NODES = 16
MAX_NODES = 24

# The ONLY hard-coded slug in this file, and it is asserted to exist before it is drawn.
WORKED_EXAMPLE = "mir-backend-python-fastapi"

# The one hand-authored datum. `mir-backend` carries 30 tiers and modules; one shard of 31
# nodes is unreadable, and there is no property of a slug that says "Go and Rust belong
# together". Split by how the runtime executes, which is the axis a reader is choosing on.
# Every backend runtime MUST appear in exactly one group -- see _check_backend_groups.
BACKEND_RUNTIME_GROUPS = [
    ("dynamic", "dynamic runtimes", ["bun", "node", "php", "python", "ruby"]),
    ("compiled", "compiled and VM runtimes", ["beam", "dotnet", "go", "jvm", "rust"]),
]

# Rule C. Mid-tone fills, so the same hex is legible against a white canvas and a near-black
# one; explicit `color` on every one, because that is the bug this table exists to prevent.
# Contrast of each fill against #ffffff text is >= 4.5:1 (WCAG AA for normal text).
CLASSDEFS = {
    "planned": "fill:#4b5563,stroke:#9ca3af,color:#ffffff",
    "usergate": "fill:#8a6116,stroke:#e3bd6b,color:#ffffff",
    "denied": "fill:#8c2f2f,stroke:#f0a3a3,color:#ffffff",
    "allowed": "fill:#1f6f4a,stroke:#8fd0b0,color:#ffffff",
}

BEGIN_RE = re.compile(r"^<!-- mir:gen:begin id=([a-z0-9][a-z0-9-]*) src=(\S+) -->$")
END_RE = re.compile(r"^<!-- mir:gen:end id=([a-z0-9][a-z0-9-]*) -->$")
MARKER_HINT = "mir:gen"


def _is_marker_line(stripped: str) -> bool:
    """A marker is an HTML comment alone on its line. Prose that merely NAMES the marker --
    `GOAL.md` says "every `mir:gen:` block" -- is not a malformed marker, and reporting it as
    one is the kind of false positive that gets a whole check family switched off. The hint
    is the shorter `mir:gen`, not `mir:gen:`, so a genuinely broken `<!-- mir:gen begin -->`
    is still caught rather than skipped for missing the colon it was supposed to have.
    """
    return stripped.startswith("<!--") and MARKER_HINT in stripped

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mir"}


class DiagramError(Exception):
    """The tree cannot be drawn truthfully, so nothing is drawn.

    Raised for a shard over MAX_NODES, a backend runtime in no group, or a missing worked
    example. All three are cases where carrying on would produce a diagram that is quietly
    incomplete, which is worse than no diagram: a picture is read as exhaustive.
    """


# -- catalog access ---------------------------------------------------------------------
# Imported lazily so that import time does no filesystem work and INIT_DIR stays
# overridable. catalog.py is the single source of truth for what the tree contains; this
# module reimplements none of it.

_CATALOG = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        p = str(INIT_DIR)
        if p not in sys.path:
            sys.path.insert(0, p)
        import catalog as _c
        _CATALOG = _c
    return _CATALOG


def _tree():
    """(known, planned, labels, pillar_labels) -- every read of the tree goes through here.

    `planned` is `.mir-planned` MINUS what is on disk. A slug can legitimately be in both
    for one commit (validate.py's PLN001 warns about exactly that window), and during it the
    disk is the truth: drawing a skill that exists as "planned, not written" would be a
    picture that is wrong rather than merely stale.
    """
    c = catalog()
    overlay = c.load_overlay()
    known = c.scan_skills()
    return (known, c.load_planned() - known,
            overlay.get("labels", {}), overlay.get("pillar_labels", {}))


def _depth(slug: str) -> int:
    return slug.count("-")


def _nid(slug: str, planned: set) -> str:
    """Sanitised node id. `mir-backend-python` -> `t_mir_backend_python`.

    The role prefix is not decoration: it keeps a pillar's id distinct from a tier's in a
    shard where both are drawn, and it keeps ids unique if two shards are ever concatenated.
    """
    if slug in planned:
        role = "x"
    else:
        role = {1: "p", 2: "t"}.get(_depth(slug), "m")
    return "%s_%s" % (role, slug.replace("-", "_"))


def _label_for(slug: str, labels: dict, pillar_labels: dict) -> str:
    if _depth(slug) == 1:
        return pillar_labels.get(slug.split("-", 1)[1], "")
    return labels.get(slug, "")


# -- the data structure every block is built from ----------------------------------------


@dataclass(frozen=True)
class Node:
    nid: str
    label: str          # what goes inside the quotes in Mermaid
    cls: str = ""       # "" means unstyled, which is rule B and the default
    text: str = ""      # the text version's wording; falls back to `label`
    shape: str = "box"  # "box" -> ["..."], "decision" -> {"..."}


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    label: str = ""
    dotted: bool = False


@dataclass(frozen=True)
class Step:
    src: str
    dst: str
    text: str
    reply: bool = False


@dataclass(frozen=True)
class Link:
    text: str
    target: str = ""    # repo-relative path, "" for an unlinked entry
    note: str = ""


@dataclass
class Diagram:
    did: str
    title: str
    intro: str
    acc_descr: str
    kind: str = "flowchart"          # "flowchart" | "sequence"
    direction: str = "LR"
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    links: list = field(default_factory=list)
    legend: str = ""                 # what the colours mean, in words

    def node_count(self) -> int:
        return len(self.nodes)


# -- derived diagrams --------------------------------------------------------------------


def _check_backend_groups() -> None:
    """A backend runtime in no shard must be LOUD, never silently absent.

    The failure this prevents: someone adds `mir-backend-zig`, every check stays green, and
    the only picture of the backend pillar simply does not contain it. Nobody notices,
    because a diagram has no row count to re-sum.
    """
    known, planned, _, _ = _tree()
    runtimes = sorted({s.split("-")[2] for s in (known | planned)
                       if catalog().pillar_of(s) == "backend" and len(s.split("-")) >= 3})
    grouped = []
    for _key, _title, members in BACKEND_RUNTIME_GROUPS:
        grouped.extend(members)
    missing = [r for r in runtimes if r not in grouped]
    if missing:
        raise DiagramError(
            "backend runtime(s) %s are in no shard of BACKEND_RUNTIME_GROUPS in %s, so they "
            "would be missing from the only diagram of the backend pillar. Add each one to a "
            "group (and reshard if the group then exceeds SOFT_NODES=%d)."
            % (", ".join(missing), SRC_REL, SOFT_NODES))
    dupes = sorted({r for r in grouped if grouped.count(r) > 1})
    if dupes:
        raise DiagramError("backend runtime(s) %s appear in more than one shard of "
                           "BACKEND_RUNTIME_GROUPS in %s" % (", ".join(dupes), SRC_REL))
    unknown = [r for r in sorted(grouped) if r not in runtimes]
    if unknown:
        raise DiagramError(
            "BACKEND_RUNTIME_GROUPS in %s names runtime(s) %s that no skill and no "
            ".mir-planned entry provides -- a shard would be drawn around nothing."
            % (SRC_REL, ", ".join(unknown)))


def _pillars() -> list:
    known, planned, _, _ = _tree()
    return sorted({catalog().pillar_of(s) for s in (known | planned)} - {""})


def _shard_ids() -> list:
    out = []
    for pillar in _pillars():
        if pillar == "backend":
            out.extend("tree-backend-%s" % key for key, _t, _m in BACKEND_RUNTIME_GROUPS)
        else:
            out.append("tree-%s" % pillar)
    return sorted(out)


def _members(pillar: str, runtimes=None) -> list:
    known, planned, _, _ = _tree()
    out = []
    for slug in sorted(known | planned):
        if catalog().pillar_of(slug) != pillar:
            continue
        parts = slug.split("-")
        if len(parts) == 2:
            if slug in known:
                out.append(slug)     # the bare pillar; a planned pillar has nothing to root
            continue
        if runtimes is not None and parts[2] not in runtimes:
            continue
        out.append(slug)
    return out


def _tree_shard(did: str, pillar: str, runtimes=None, group_title: str = "") -> Diagram:
    known, planned, labels, pillar_labels = _tree()
    universe = known | planned
    members = _members(pillar, runtimes)

    title = "The %s pillar" % pillar
    if group_title:
        title = "The %s pillar -- %s" % (pillar, group_title)

    nodes, edges, links = [], [], []
    member_set = set(members)
    for slug in members:
        is_planned = slug in planned
        nodes.append(Node(
            nid=_nid(slug, planned),
            label=slug + (" (planned)" if is_planned else ""),
            cls="planned" if is_planned else "",
            text=slug + (" -- planned, not written yet" if is_planned else ""),
        ))
        chain = catalog().chain_of(slug, universe)
        parents = [c for c in chain if c != slug and c in member_set]
        if parents:
            edges.append(Edge(_nid(parents[-1], planned), _nid(slug, planned),
                              dotted=is_planned))
        label = _label_for(slug, labels, pillar_labels)
        links.append(Link(
            text=slug,
            target="" if is_planned else "skills/%s/SKILL.md" % slug,
            note=label if not is_planned else (label + " -- planned" if label else "planned"),
        ))

    written = sum(1 for s in members if s not in planned)
    n_planned = len(members) - written
    intro = ("%d written, %d listed in `.mir-planned`. Derived from `skills/` on disk, so a "
             "new skill appears here the moment its directory does."
             % (written, n_planned))
    acc = ("Tree of the %s pillar showing %d written skills and %d planned ones, each edge "
           "running from a skill to the more specific skill that extends it"
           % (pillar, written, n_planned))
    legend = ""
    if n_planned:
        legend = ("Grey nodes with a dotted edge are listed in `.mir-planned`: referenced by "
                  "name, deliberately not written yet. They are also marked *(planned)* in "
                  "the label and in the text version, so the colour is never the only signal.")
    return Diagram(did=did, title=title, intro=intro, acc_descr=acc, direction="LR",
                   nodes=nodes, edges=edges, links=links, legend=legend)


def _pillar_map() -> Diagram:
    known, planned, _, pillar_labels = _tree()
    nodes = [Node("r_mir", "Make It Right", text="Make It Right")]
    edges, links = [], []
    for pillar in _pillars():
        slug = "mir-%s" % pillar
        below = sum(1 for s in (known | planned)
                    if catalog().pillar_of(s) == pillar and _depth(s) > 1)
        label = "%s<br/>%d below" % (slug, below)
        human = pillar_labels.get(pillar, "")
        nodes.append(Node(_nid(slug, planned), label,
                          text="%s -- %d tiers and modules below it" % (slug, below)))
        edges.append(Edge("r_mir", _nid(slug, planned)))
        links.append(Link(slug, "skills/%s/SKILL.md" % slug if slug in known else "", human))
    intro = ("%d pillars, %d skills in total. A pillar is the coarse gate; it loads on a "
             "matching task and hands off to a tier and then a module."
             % (len(_pillars()), len(known)))
    acc = ("Map of the %d Make It Right pillars, each labelled with how many tiers and "
           "modules sit below it" % len(_pillars()))
    return Diagram("pillar-map", "Pillar map", intro, acc, direction="LR",
                   nodes=nodes, edges=edges, links=links)


def _chain_example() -> Diagram:
    known, planned, labels, pillar_labels = _tree()
    if WORKED_EXAMPLE not in known:
        raise DiagramError(
            "the worked example %r does not exist under skills/. It is the one slug this "
            "generator hard-codes, so it is asserted rather than assumed -- pick a slug that "
            "exists and change WORKED_EXAMPLE in %s." % (WORKED_EXAMPLE, SRC_REL))
    chain = catalog().chain_of(WORKED_EXAMPLE, known)
    nodes, edges, links = [], [], []
    roles = {1: "pillar", 2: "tier", 3: "module"}
    prev = None
    for slug in chain:
        nid = _nid(slug, planned)
        role = roles.get(_depth(slug), "module")
        nodes.append(Node(nid, "%s<br/>%s" % (slug, role),
                          text="%s -- the %s" % (slug, role)))
        if prev:
            edges.append(Edge(prev, nid, label="narrows to"))
        prev = nid
        links.append(Link(slug, "skills/%s/SKILL.md" % slug,
                          _label_for(slug, labels, pillar_labels)))
    always = catalog().ALWAYS
    if always in known:
        nodes.append(Node(_nid(always, planned), "%s<br/>always on" % always,
                          text="%s -- resolved for every stack, never a question" % always))
        edges.append(Edge(nodes[0].nid, _nid(always, planned), dotted=True))
        links.append(Link(always, "skills/%s/SKILL.md" % always,
                          _label_for(always, labels, pillar_labels)))
    intro = ("`catalog.resolve()` turns one answer into this chain, ordered coarse to fine, "
             "so the general constraints are in context before the framework mechanics are.")
    acc = ("Chain for %s running from the pillar through the runtime tier to the framework "
           "module, plus the always-on security pillar" % WORKED_EXAMPLE)
    return Diagram("chain-example", "Coarse to fine, worked", intro, acc, direction="LR",
                   nodes=nodes, edges=edges, links=links)


_FM_DESC = re.compile(r'^description:\s*"(.*)"\s*$', re.M)


def _skill_text(slug: str):
    """(description chars, body lines) for one skill. Not catalog's job -- it derives the
    picker, not file sizes -- so it is nine lines here rather than a new catalog API."""
    p = ROOT / "skills" / slug / "SKILL.md"
    if not p.exists():
        return 0, 0
    lines = p.read_text(encoding="utf-8").splitlines()
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
    fm = "\n".join(lines[1:close])
    m = _FM_DESC.search(fm)
    return (len(m.group(1)) if m else 0), len(lines) - close - 1


def _tokens(chars: int) -> int:
    """~4 chars per token, rounded to the nearest 100 so a one-word edit does not move it."""
    return int(round(chars / 4.0 / 100.0) * 100)


def _disclosure() -> Diagram:
    known, planned, _, _ = _tree()
    if WORKED_EXAMPLE not in known:
        raise DiagramError("the worked example %r does not exist under skills/" % WORKED_EXAMPLE)
    desc_chars = sum(_skill_text(s)[0] for s in sorted(known))
    chain = catalog().chain_of(WORKED_EXAMPLE, known)
    roles = {1: "pillar", 2: "tier", 3: "module"}

    nodes = [Node("d_idle", "%d descriptions resident<br/>~%d tokens"
                            % (len(known), _tokens(desc_chars)),
                  text="Host idle -- all %d skill descriptions are resident, about %d tokens"
                       % (len(known), _tokens(desc_chars))),
             Node("d_match", "Task text matches<br/>one description",
                  text="A task arrives whose wording matches one description's TRIGGER clause")]
    edges = [Edge("d_idle", "d_match")]
    links = []
    prev = "d_match"
    for slug in chain:
        lines, chars = _skill_text(slug)[1], 0
        chars = len((ROOT / "skills" / slug / "SKILL.md").read_text(encoding="utf-8"))
        nid = "d_%s" % slug.replace("-", "_")
        role = roles.get(_depth(slug), "module")
        nodes.append(Node(nid, "%s loads<br/>%d lines ~%d tokens" % (role, lines, _tokens(chars)),
                          text="%s loads whole -- %s, %d body lines, about %d tokens"
                               % (slug, role, lines, _tokens(chars))))
        edges.append(Edge(prev, nid))
        prev = nid
        links.append(Link(slug, "skills/%s/SKILL.md" % slug, "%s, %d body lines" % (role, lines)))
    nodes.append(Node("d_refs", "references/ load<br/>only when a gate says read them",
                      text="reference files load last, and only when a gate tells the model "
                           "to read one"))
    edges.append(Edge(prev, "d_refs"))
    intro = ("Nothing below the first box is in context until it is earned. The token figures "
             "are measured from the files on disk at generation time, not estimated.")
    acc = ("Progressive disclosure -- descriptions are always resident, then a matching task "
           "loads the pillar, then the tier, then the module, and reference files load last")
    return Diagram("disclosure", "Progressive disclosure and what it costs", intro, acc,
                   direction="TD", nodes=nodes, edges=edges, links=links)


# -- stable diagrams ---------------------------------------------------------------------
# Hand-authored because there is nothing on disk to derive them from. They still go through
# the same Diagram value, the same size enforcement and the same DIA001 comparison, so an
# edit here is still a generator change and still has to be regenerated.


GATES = [
    ("0", "Intent and Triage", False),
    ("1", "Constraint Interrogation", True),
    ("2", "Assumption Ledger", True),
    ("3", "Invariants and Failure Modes", False),
    ("4", "Risk Register", False),
    ("5", "Design Review", True),
    ("6", "Implementation", False),
    ("7", "Production-Readiness Review", False),
]


def _gates() -> Diagram:
    nodes, edges, links = [], [], []
    prev = None
    for num, name, is_user in GATES:
        nid = "g%s" % num
        nodes.append(Node(nid, "Gate %s<br/>%s" % (num, name), cls="usergate" if is_user else "",
                          text="Gate %s -- %s%s" % (num, name,
                                                    " (stops for the user)" if is_user else "")))
        if prev:
            edges.append(Edge(prev, nid))
        prev = nid
    edges.append(Edge("g5", "g1", label="rejected", dotted=True))
    links = [Link("EXTENDING.md -- what each gate is for", "EXTENDING.md", "")]
    intro = ("Every pillar runs the same eight gates. No implementation code is written "
             "before Gate 6, and the three amber gates stop and wait for a human.")
    acc = ("The eight Make It Right gates in order from Gate 0 Intent to Gate 7 "
           "Production-Readiness, with Gates 1, 2 and 5 marked as stopping for the user, and "
           "a rejected design review returning from Gate 5 to Gate 1")
    legend = ("Amber nodes are the three `[USER GATE]` stops -- the model must not proceed "
              "past them on its own. They are also named as user gates in the text version.")
    return Diagram("gates", "The eight gates", intro, acc, direction="LR",
                   nodes=nodes, edges=edges, links=links, legend=legend)


def _init_flow() -> Diagram:
    nodes = [
        Node("u", "You", text="You"),
        Node("cli", "mir init", text="mir init (init/cli.py)"),
        Node("det", "detect.py", text="init/detect.py"),
        Node("cat", "catalog.py", text="init/catalog.py"),
        Node("gen", "generate.py", text="init/generate.py"),
        Node("repo", "your repo", text="your repository"),
    ]
    steps = [
        Step("u", "cli", "mir init ."),
        Step("cli", "det", "detect(repo)"),
        Step("det", "cli", "proposals and conflicts, never a decision", reply=True),
        Step("cli", "u", "if a pillar is undecided, refuse and list the options", reply=True),
        Step("u", "cli", "--answers answers.json"),
        Step("cli", "cat", "resolve(answers)"),
        Step("cat", "cli", "chain-ordered skills plus recorded gaps", reply=True),
        Step("cli", "gen", "plan(repo, skills, answers)"),
        Step("gen", "cli", "one item per destination, each classified first", reply=True),
        Step("cli", "gen", "apply(repo, items) -- all destinations or none"),
        Step("gen", "repo", "write .mir/, AGENTS.md, CLAUDE.md, merge .claude/settings.json"),
        Step("cli", "repo", "run .mir/probe.py against the manifest"),
        Step("repo", "u", "exit 0 only if the guard actually blocked a denied write", reply=True),
    ]
    links = [
        Link("init/cli.py", "init/cli.py", "the flow above, in order"),
        Link("init/detect.py", "init/detect.py", "proposes, never decides"),
        Link("init/generate.py", "init/generate.py", "classifies every destination before writing"),
    ]
    intro = ("Detection proposes and never decides, and the run is all-or-nothing: a harness "
             "that is half-installed looks installed and enforces nothing.")
    acc = ("Sequence of a mir init run -- you invoke the CLI, it detects the stack and refuses "
           "to guess, you supply answers, catalog resolves the skill chain, generate plans and "
           "then writes every destination or none, and the probe verifies the guard blocks")
    return Diagram("init-flow", "What `mir init` actually does", intro, acc, kind="sequence",
                   nodes=nodes, steps=steps, links=links)


def _trust_boundary() -> Diagram:
    nodes = [
        Node("w_call", "Agent asks to write a file", text="An agent asks to write a file"),
        Node("w_hook", "PreToolUse hook<br/>.mir/guard.py",
             text="The PreToolUse hook runs .mir/guard.py"),
        Node("w_policy", "Read .mir/manifest.json", text="The guard reads .mir/manifest.json"),
        Node("w_deny", "Under a denied path", shape="decision",
             text="Is the target under a denied path (secrets, .git, .mir, the hook "
                  "registration, home config)"),
        Node("w_root", "Under an allowed write root", shape="decision",
             text="Is the target under an allowed write root"),
        Node("w_blocked", "BLOCKED<br/>exit 2, reason on stderr", cls="denied",
             text="BLOCKED -- denied paths win over allowed roots"),
        Node("w_default", "BLOCKED by default<br/>no root matched", cls="denied",
             text="BLOCKED -- deny by default, nothing outside an allowed root is writable"),
        Node("w_write", "Write proceeds", cls="allowed", text="ALLOWED -- the write proceeds"),
    ]
    edges = [
        Edge("w_call", "w_hook"), Edge("w_hook", "w_policy"), Edge("w_policy", "w_deny"),
        Edge("w_deny", "w_blocked", label="yes"), Edge("w_deny", "w_root", label="no"),
        Edge("w_root", "w_write", label="yes"), Edge("w_root", "w_default", label="no"),
    ]
    links = [
        Link("init/schema.py", "init/schema.py", "the baseline denied set, with the reason for each"),
        Link("init/guard.py", "init/guard.py", "the hook that decides"),
        Link("init/probe.py", "init/probe.py", "proves the guard really blocks"),
    ]
    intro = ("Deny by default, and denied paths beat allowed roots. The policy, the guard and "
             "the probe all live under `.mir/`, which is itself denied, so an agent cannot "
             "widen its own permissions.")
    acc = ("Write policy decision flow -- a tool call reaches the PreToolUse guard, which "
           "reads the manifest, blocks anything under a denied path, allows what is under an "
           "allowed write root, and blocks everything else by default")
    legend = ("Red nodes are refusals and the green node is the only path that writes. Both "
              "outcomes are spelled out in words in the text version.")
    return Diagram("trust-boundary", "The write policy, end to end", intro, acc, direction="TD",
                   nodes=nodes, edges=edges, links=links, legend=legend)


def _placement() -> Diagram:
    nodes = [
        Node("q_start", "You want to add a skill", text="You want to add a skill"),
        Node("q_domain", "Does it apply to every task in a whole domain", shape="decision",
             text="Does it apply to every task in a whole domain"),
        Node("q_pillar", "Write a pillar<br/>mir-DOMAIN",
             text="Write a pillar -- mir-DOMAIN"),
        Node("q_runtime", "Is it a runtime or reactivity layer under a pillar", shape="decision",
             text="Is it a runtime or reactivity layer under an existing pillar"),
        Node("q_tier", "Write a tier<br/>mir-DOMAIN-RUNTIME",
             text="Write a tier -- mir-DOMAIN-RUNTIME"),
        Node("q_fw", "Is it one framework on top of a tier", shape="decision",
             text="Is it one framework on top of an existing tier"),
        Node("q_module", "Write a module<br/>mir-DOMAIN-RUNTIME-FRAMEWORK",
             text="Write a module -- mir-DOMAIN-RUNTIME-FRAMEWORK"),
        Node("q_ref", "Not a skill<br/>add it to references/ of the nearest skill",
             text="Not a skill -- add it to references/ of the nearest existing skill"),
    ]
    edges = [
        Edge("q_start", "q_domain"),
        Edge("q_domain", "q_pillar", label="yes"), Edge("q_domain", "q_runtime", label="no"),
        Edge("q_runtime", "q_tier", label="yes"), Edge("q_runtime", "q_fw", label="no"),
        Edge("q_fw", "q_module", label="yes"), Edge("q_fw", "q_ref", label="no"),
    ]
    links = [
        Link("EXTENDING.md", "EXTENDING.md", "the naming convention and the size budgets"),
        Link("validate.py", "validate.py", "enforces the chain the name implies"),
    ]
    intro = ("The name is the chain: `validate.py` reads pillar, tier and module straight out "
             "of the hyphens, so putting a skill at the wrong depth breaks it loudly.")
    acc = ("Decision tree for placing a new skill -- a whole domain becomes a pillar, a "
           "runtime layer becomes a tier, a single framework becomes a module, and anything "
           "narrower becomes a reference file on the nearest existing skill")
    return Diagram("placement", "Where a new skill goes", intro, acc, direction="TD",
                   nodes=nodes, edges=edges, links=links)


# -- registry ----------------------------------------------------------------------------

_BUILDERS = {
    "gates": _gates,
    "pillar-map": _pillar_map,
    "chain-example": _chain_example,
    "disclosure": _disclosure,
    "init-flow": _init_flow,
    "trust-boundary": _trust_boundary,
    "placement": _placement,
}

# Fixed order, so --list and --write are stable output and a diff is about content.
_ORDER = ["gates", "pillar-map", "chain-example", "disclosure",
          "init-flow", "trust-boundary", "placement"]


def _shard_builder(did: str):
    pillar = did[len("tree-"):]
    for key, _t, members in BACKEND_RUNTIME_GROUPS:
        if pillar == "backend-%s" % key:
            title = dict((k, t) for k, t, _m in BACKEND_RUNTIME_GROUPS)[key]
            return lambda: _tree_shard(did, "backend", members, title)
    return lambda: _tree_shard(did, pillar)


def diagram_ids() -> list:
    """Every id this generator produces. Raises if the backend grouping has gone stale."""
    _check_backend_groups()
    return _ORDER[:2] + _shard_ids() + _ORDER[2:]


def build(did: str) -> Diagram:
    _check_backend_groups()
    if did in _BUILDERS:
        d = _BUILDERS[did]()
    elif did in _shard_ids():
        d = _shard_builder(did)()
    else:
        raise DiagramError("no diagram with id %r" % did)
    n = d.node_count()
    if n > MAX_NODES:
        raise DiagramError(
            "diagram %r has %d nodes, over the hard ceiling of MAX_NODES=%d. Refusing to "
            "emit it: past this size a flowchart is a hairball nobody reads, and shipping "
            "one anyway is how the single 46-node tree the plan rejected gets built one node "
            "at a time. Shard it." % (did, n, MAX_NODES))
    return d


# -- rendering ----------------------------------------------------------------------------


def _mermaid(d: Diagram) -> list:
    out = []
    if d.kind == "sequence":
        out.append("sequenceDiagram")
    else:
        out.append("flowchart %s" % d.direction)
    # Backticks are stripped, not merely avoided: a title is Markdown in the `###` heading
    # above and in the <summary>, but Mermaid 11 parses labels as Markdown while 10 does not,
    # so a backtick inside the diagram renders differently depending on which version GitHub
    # has pinned this month. accTitle/accDescr are plain text on purpose.
    out.append("    accTitle: %s" % d.title.replace("`", ""))
    out.append("    accDescr: %s" % d.acc_descr.replace("`", ""))

    if d.kind == "sequence":
        for n in d.nodes:
            out.append("    participant %s as %s" % (n.nid, n.label))
        for s in d.steps:
            out.append("    %s%s%s: %s" % (s.src, "-->>" if s.reply else "->>", s.dst, s.text))
        return out

    for n in d.nodes:
        open_b, close_b = ("{\"", "\"}") if n.shape == "decision" else ("[\"", "\"]")
        out.append("    %s%s%s%s" % (n.nid, open_b, n.label, close_b))
    for e in d.edges:
        arrow = "-.->" if e.dotted else "-->"
        mid = "|%s|" % e.label if e.label else ""
        out.append("    %s %s%s %s" % (e.src, arrow, mid, e.dst))
    # Rule B: only nodes that carry meaning by colour are styled at all.
    used = sorted({n.cls for n in d.nodes if n.cls})
    for cls in used:
        out.append("    classDef %s %s" % (cls, CLASSDEFS[cls]))
        members = [n.nid for n in d.nodes if n.cls == cls]
        out.append("    class %s %s" % (",".join(members), cls))
    return out


def _text_version(d: Diagram) -> list:
    if d.kind == "sequence":
        by_id = {n.nid: n.text or n.label for n in d.nodes}
        return ["%d. %s %s %s: %s" % (i, by_id[s.src],
                                      "replies to" if s.reply else "calls",
                                      by_id[s.dst], s.text)
                for i, s in enumerate(d.steps, 1)]

    by_id = {n.nid: n for n in d.nodes}
    children = {}
    incoming = set()
    for e in d.edges:
        children.setdefault(e.src, []).append(e)
        incoming.add(e.dst)
    roots = [n.nid for n in d.nodes if n.nid not in incoming]

    lines = []

    def flat_label(nid):
        return (by_id[nid].text or by_id[nid].label).replace("<br/>", " -- ")

    # A diagram that is one unbranching chain is rendered as a NUMBERED LIST, not as eight
    # levels of nesting. Nesting a linear sequence is not merely ugly: a screen reader
    # announces the list depth at every step, so an eight-step chain becomes eight "list,
    # nesting level N" announcements carrying no information at all.
    order, seen_l, cur = [], set(), roots[0] if len(roots) == 1 else None
    while cur is not None:
        order.append(cur)
        seen_l.add(cur)
        nxt = [e for e in children.get(cur, []) if e.dst not in seen_l]
        if len(nxt) > 1:
            order = []
            break
        cur = nxt[0].dst if nxt else None
    if order and len(order) == len(d.nodes):
        pos = {n: i for i, n in enumerate(order)}
        lines = ["%d. %s" % (i, flat_label(n)) for i, n in enumerate(order, 1)]
        backs = [e for e in d.edges if pos.get(e.dst, 0) <= pos.get(e.src, 0)]
        if backs:
            lines.append("")
            for e in backs:
                lines.append("Step %d can return to step %d%s."
                             % (pos[e.src] + 1, pos[e.dst] + 1,
                                " when %s" % e.label if e.label else ""))
        return lines

    def walk(nid, depth, seen, prefix=""):
        # The edge label is folded into the child's own bullet rather than given a nesting
        # level of its own. A "yes:" or "narrows to:" line with nothing on it is a rung of
        # indentation that carries no information, and screen-reader users pay for every one.
        lines.append("%s- %s%s" % ("  " * depth, prefix, flat_label(nid)))
        for e in children.get(nid, []):
            child_prefix = "%s: " % e.label if e.label else ""
            if e.dst in seen:
                lines.append("%s- %sback to %s"
                             % ("  " * (depth + 1), child_prefix, flat_label(e.dst)))
                continue
            walk(e.dst, depth + 1, seen | {nid, e.dst}, child_prefix)

    for r in roots:
        walk(r, 0, {r})
    return lines


def _rel(target: str, doc_dir: Path) -> str:
    """A link is rendered relative to the document that carries it, so moving a block from
    docs/ to README.md and regenerating produces correct paths instead of broken ones."""
    return Path(os.path.relpath(ROOT / target, doc_dir)).as_posix()


def render_body(did: str, doc_path) -> str:
    """The bytes between the two markers. No timestamp, no hash, no version -- see the
    module docstring; this is the invariant the whole mechanism rests on."""
    d = build(did)
    doc_dir = Path(doc_path).resolve().parent
    # MUTATION-TEST ANCHOR -- init/test_diagrams.py injects a stamp at exactly this line to
    # prove that a stamped block makes --check fail on an unmodified tree.
    out: list = []
    out.append("### %s" % d.title)
    out.append("")
    out.append(d.intro)
    out.append("")
    out.append("```mermaid")
    out.extend(_mermaid(d))
    out.append("```")
    out.append("")
    if d.legend:
        out.append(d.legend)
        out.append("")
    out.append("<details>")
    out.append("<summary>Text version -- %s</summary>" % d.title)
    out.append("")
    out.extend(_text_version(d))
    out.append("")
    out.append("</details>")
    if d.links:
        out.append("")
        out.append("<details>")
        out.append("<summary>Links -- %s</summary>" % d.title)
        out.append("")
        # GitHub renders Mermaid with securityLevel strict inside a CSP'd iframe, so a
        # `click` directive inside the diagram is dead ink. This list is the working
        # substitute, and it is built from the same node list as the diagram above it.
        for link in d.links:
            text = "[%s](%s)" % (link.text, _rel(link.target, doc_dir)) if link.target \
                else "`%s`" % link.text
            out.append("- %s%s" % (text, " -- %s" % link.note if link.note else ""))
        out.append("")
        out.append("</details>")
    return "\n".join(out)


def begin_marker(did: str) -> str:
    return "<!-- mir:gen:begin id=%s src=%s -->" % (did, SRC_REL)


def end_marker(did: str) -> str:
    return "<!-- mir:gen:end id=%s -->" % did


# -- documents ----------------------------------------------------------------------------


@dataclass
class Block:
    did: str
    src: str
    begin: int          # 1-based line number of the begin marker
    end: int            # 1-based line number of the end marker
    body: str


def scan_docs() -> list:
    out = []
    for p in sorted(ROOT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        out.append(p)
    return out


def parse_blocks(text: str):
    """(blocks, marker_problems). Every marker problem is a DIA002 in validate.py.

    A malformed marker is not a cosmetic issue: an unclosed block means everything after it
    to the end of the file is inside the managed region, and --write would delete it.

    Markers inside a fenced code block are DOCUMENTATION, not markers. This is not
    hypothetical: `docs/v2-plan.md` specifies the marker format by showing an example pair
    inside an ```html fence, and the first version of this parser dutifully generated a full
    diagram into the middle of the plan. A generated block's own body contains balanced
    fences (```mermaid ... ```), so the same counter works inside a live block too.
    """
    blocks, problems = [], []
    open_b = None
    in_fence = False
    lines = text.splitlines()
    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if open_b is not None:
                open_b["body"].append(raw)
            continue
        if in_fence or not _is_marker_line(line.lstrip()):
            if open_b is not None:
                open_b["body"].append(raw)
            continue
        mb, me = BEGIN_RE.match(line), END_RE.match(line)
        if mb:
            if open_b is not None:
                problems.append((i, "a new begin marker for id=%s opens while id=%s is still "
                                    "open" % (mb.group(1), open_b["did"])))
                blocks.append(Block(open_b["did"], open_b["src"], open_b["begin"], i - 1,
                                    "\n".join(open_b["body"])))
            open_b = {"did": mb.group(1), "src": mb.group(2), "begin": i, "body": []}
        elif me:
            if open_b is None:
                problems.append((i, "end marker for id=%s with no matching begin" % me.group(1)))
            elif me.group(1) != open_b["did"]:
                problems.append((i, "end marker id=%s does not match the open begin id=%s"
                                 % (me.group(1), open_b["did"])))
                blocks.append(Block(open_b["did"], open_b["src"], open_b["begin"], i,
                                    "\n".join(open_b["body"])))
                open_b = None
            else:
                blocks.append(Block(open_b["did"], open_b["src"], open_b["begin"], i,
                                    "\n".join(open_b["body"])))
                open_b = None
        else:
            problems.append((i, "malformed mir:gen marker: %r" % line.strip()))
            if open_b is not None:
                open_b["body"].append(raw)
    if open_b is not None:
        problems.append((open_b["begin"], "begin marker for id=%s is never closed"
                         % open_b["did"]))
    return blocks, problems


def check_all() -> dict:
    """Re-derive every embedded block and report what disagrees. Performs no writes.

    validate.py consumes this and turns each list into a DIA code. The mapping lives there
    so the code numbers stay in one place, next to the rest of the check family.
    """
    ids = diagram_ids()
    known_ids = set(ids)
    out = {"ids": ids, "drift": [], "markers": [], "oversize": [], "unembedded": [],
           "embedded": {}, "docs": 0}

    for did in ids:
        n = build(did).node_count()
        if n > SOFT_NODES:
            out["oversize"].append((did, n))

    seen = {}
    for doc in scan_docs():
        text = doc.read_text(encoding="utf-8")
        if MARKER_HINT not in text:
            continue
        rel = doc.relative_to(ROOT).as_posix()
        blocks, problems = parse_blocks(text)
        if blocks or problems:
            out["docs"] += 1     # a doc that only SHOWS the marker format is not a host
        for line, msg in problems:
            out["markers"].append((rel, line, msg))
        for b in blocks:
            if b.did not in known_ids:
                out["markers"].append((rel, b.begin, "unknown diagram id %r -- %s produces "
                                                     "no such diagram" % (b.did, SRC_REL)))
                continue
            if b.src != SRC_REL:
                out["markers"].append((rel, b.begin, "marker for id=%s names src=%s, but this "
                                                     "generator is %s" % (b.did, b.src, SRC_REL)))
            if b.did in seen:
                out["markers"].append((rel, b.begin, "id %s is already embedded in %s -- a "
                                                     "duplicated block cannot be kept in sync"
                                       % (b.did, seen[b.did])))
                continue
            seen[b.did] = rel
            want = render_body(b.did, doc)
            if b.body != want:
                diff = "\n".join(difflib.unified_diff(
                    b.body.splitlines(), want.splitlines(),
                    fromfile="%s (committed, id=%s)" % (rel, b.did),
                    tofile="%s (generated, id=%s)" % (SRC_REL, b.did), lineterm=""))
                out["drift"].append((rel, b.did, diff))
    out["embedded"] = seen
    out["unembedded"] = [d for d in ids if d not in seen]
    return out


def write_all() -> list:
    """Rewrite every managed block in place. The only mutating path in this module."""
    changed = []
    for doc in scan_docs():
        text = doc.read_text(encoding="utf-8")
        if MARKER_HINT not in text:
            continue
        blocks, problems = parse_blocks(text)
        if problems:
            raise DiagramError(
                "%s has malformed mir:gen markers, so --write will not touch it (an unclosed "
                "block would swallow the rest of the file):\n  %s"
                % (doc.relative_to(ROOT).as_posix(),
                   "\n  ".join("line %d: %s" % p for p in problems)))
        lines = text.splitlines()
        for b in sorted(blocks, key=lambda b: b.begin, reverse=True):
            body = render_body(b.did, doc)
            lines[b.begin:b.end - 1] = body.splitlines()
        new = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        if new != text:
            doc.write_text(new, encoding="utf-8")
            changed.append(doc.relative_to(ROOT).as_posix())
    return changed


# -- CLI -----------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate and verify the repo's Mermaid diagrams.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list every diagram id")
    g.add_argument("--stdout", metavar="ID", help="print one block, markers included")
    g.add_argument("--check", action="store_true", help="exit 1 with a diff on drift; writes nothing")
    g.add_argument("--write", action="store_true", help="rewrite every managed block in place")
    ap.add_argument("--for", dest="for_doc", default="README.md",
                    help="render --stdout links relative to this document (default README.md)")
    args = ap.parse_args(argv)

    try:
        if args.list:
            for did in diagram_ids():
                d = build(did)
                flag = "  OVER SOFT_NODES" if d.node_count() > SOFT_NODES else ""
                print("%-26s %-16s %2d %s%s"
                      % (did, d.kind if d.kind == "sequence" else "flowchart " + d.direction,
                         d.node_count(), "node" if d.node_count() == 1 else "nodes", flag))
            return 0

        if args.stdout:
            did = args.stdout
            print(begin_marker(did))
            print(render_body(did, ROOT / args.for_doc))
            print(end_marker(did))
            return 0

        if args.check:
            r = check_all()
            bad = False
            for rel, line, msg in r["markers"]:
                print("MARKER  %s:%d  %s" % (rel, line, msg), file=sys.stderr)
                bad = True
            for rel, did, diff in r["drift"]:
                print("DRIFT   %s  id=%s" % (rel, did), file=sys.stderr)
                print(diff, file=sys.stderr)
                bad = True
            for did, n in r["oversize"]:
                print("WARN    %s has %d nodes, over SOFT_NODES=%d" % (did, n, SOFT_NODES))
            for did in r["unembedded"]:
                print("WARN    %s is embedded in no document -- it will never be seen" % did)
            if bad:
                print("\n%d diagram(s) drifted, %d marker problem(s). Run `python3 %s --write`."
                      % (len(r["drift"]), len(r["markers"]), SRC_REL), file=sys.stderr)
                return 1
            print("%d diagrams, %d embedded across %d document(s), all current."
                  % (len(r["ids"]), len(r["embedded"]), r["docs"]))
            return 0

        if args.write:
            changed = write_all()
            for rel in changed:
                print("wrote %s" % rel)
            if not changed:
                print("no change")
            r = check_all()
            for did, n in r["oversize"]:
                print("WARN  %s has %d nodes, over SOFT_NODES=%d -- consider sharding it"
                      % (did, n, SOFT_NODES), file=sys.stderr)
            for did in r["unembedded"]:
                print("WARN  %s is embedded in no document -- it will never be seen" % did,
                      file=sys.stderr)
            return 0
    except DiagramError as e:
        print("gen_diagrams: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
