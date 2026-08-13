"""Derive the picker from the skill tree, not from a hand-maintained catalog.

Both plan reviews said the same thing: the slug already encodes pillar -> tier -> module
(EXTENDING.md's naming convention), so a second file listing option -> skills is a drift
liability with nothing to gain. This module reads skills/ on disk, parses the chain out of
each slug, and produces the options. overlay.json adds only what the tree cannot express:
human labels and lockfile detection signals. Add a covered stack by writing a skill; its
option appears here automatically.

resolve(answers) turns a set of chosen options into the deduped, chain-ordered skill set to
scope, plus the gaps (an "Other" choice, or a detected stack with no skill) that route to the
pillar today and to the quarantined generative path later.
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILLS = os.path.join(REPO, "skills")
OVERLAY = os.path.join(HERE, "overlay.json")
PLANNED = os.path.join(REPO, ".mir-planned")

# devsecops is security-by-default: always in the resolved set, never a question.
ALWAYS = "mir-devsecops"


def load_overlay() -> dict:
    with open(OVERLAY, encoding="utf-8") as f:
        return json.load(f)


def scan_skills() -> set[str]:
    if not os.path.isdir(SKILLS):
        return set()
    return {d for d in os.listdir(SKILLS)
            if os.path.isdir(os.path.join(SKILLS, d)) and d.startswith("mir-")}


def load_planned() -> set[str]:
    if not os.path.exists(PLANNED):
        return set()
    out = set()
    for line in open(PLANNED, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def pillar_of(slug: str) -> str:
    # mir-<pillar>-...  -> pillar
    parts = slug.split("-")
    return parts[1] if len(parts) >= 2 else ""


def chain_of(slug: str, known: set[str]) -> list[str]:
    """A skill plus every ancestor tier/pillar that exists, ordered pillar -> leaf."""
    parts = slug.split("-")
    chain = []
    for depth in range(2, len(parts) + 1):
        prefix = "-".join(parts[:depth])
        if prefix in known:
            chain.append(prefix)
    return chain


def is_leaf(slug: str, known: set[str]) -> bool:
    """A slug no other skill extends -- the most specific choice a user would pick."""
    return not any(other != slug and other.startswith(slug + "-") for other in known)


def derive_catalog() -> dict:
    """{pillar: [ {value, label, resolves:[chain]} ]} plus an Other option per pillar."""
    known = scan_skills()
    overlay = load_overlay()
    labels = overlay.get("labels", {})
    pillar_labels = overlay.get("pillar_labels", {})

    pillars: dict[str, list] = {}
    for slug in sorted(known):
        p = pillar_of(slug)
        if not p or p == "devsecops":       # always-on, not a menu
            continue
        pillars.setdefault(p, [])

    for slug in sorted(known):
        p = pillar_of(slug)
        if not p or p == "devsecops":
            continue
        if slug == f"mir-{p}":               # the bare pillar is not itself an option
            continue
        # Offer every tier and module, not only leaves: a bare-runtime choice (React with
        # no meta-framework, Python with no web framework yet) is legitimate and distinct
        # from its extensions. is_leaf stays available for callers that want the narrow set.
        pillars[p].append({
            "value": slug,
            "label": labels.get(slug, slug.replace("mir-", "").replace("-", " ")),
            "resolves": chain_of(slug, known),
        })

    catalog = {"pillars": {}, "always": ALWAYS}
    for p, opts in pillars.items():
        opts.append({"value": f"__other_{p}__", "label": "Something else (I'll name it)",
                     "resolves": [f"mir-{p}"], "generate": True})
        catalog["pillars"][p] = {
            "label": pillar_labels.get(p, p),
            "options": opts,
        }
    return catalog


def resolve(answers: dict) -> dict:
    """answers: {pillar: value or [values]}. Returns resolved skills + gaps.

    A value of __other_*__ (or an unknown value) is a gap: it resolves to the pillar today
    and is recorded for the quarantined generative path, never silently dropped.
    """
    known = scan_skills()
    catalog = derive_catalog()
    resolved: list[str] = []
    gaps: list[dict] = []

    def add_chain(chain):
        for s in chain:
            if s not in resolved:
                resolved.append(s)

    for pillar, choice in answers.items():
        choices = choice if isinstance(choice, list) else [choice]
        pill = catalog["pillars"].get(pillar)
        if not pill:
            gaps.append({"pillar": pillar, "choice": choice, "reason": "no such pillar"})
            continue
        by_value = {o["value"]: o for o in pill["options"]}
        for c in choices:
            opt = by_value.get(c)
            if opt is None:
                gaps.append({"pillar": pillar, "choice": c, "reason": "no skill for this choice",
                             "resolves": [f"mir-{pillar}"]})
                add_chain([f"mir-{pillar}"])
                continue
            if opt.get("generate"):
                gaps.append({"pillar": pillar, "choice": c, "reason": "user chose 'something else'",
                             "resolves": opt["resolves"]})
            add_chain(opt["resolves"])

    # security by default
    if ALWAYS in known:
        add_chain([ALWAYS])

    # order pillar -> tier -> module by slug depth, then name, so context loads coarse-first
    resolved.sort(key=lambda s: (s.count("-"), s))
    return {"skills": resolved, "gaps": gaps, "pillars_touched": sorted(answers.keys())}


if __name__ == "__main__":
    import sys
    cat = derive_catalog()
    if len(sys.argv) > 1 and sys.argv[1] == "--catalog":
        print(json.dumps(cat, indent=2))
    else:
        # demo resolution
        demo = {"frontend": "mir-frontend-react-next", "database": "mir-database-postgres",
                "backend": "__other_backend__"}
        print(json.dumps(resolve(demo), indent=2))
