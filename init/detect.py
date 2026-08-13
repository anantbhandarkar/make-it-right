"""Detect the stack from the repo, then PROPOSE it -- never silently decide.

Both plan reviews ranked "wrong stack locked in early" as a top failure and gave the same
fix: detection must lead with evidence and require confirmation. A lockfile proves a
dependency exists, not that it is the target of the work, so every proposal carries the
file and token it came from ("detected X because Y"), and an ambiguous repo (two frontend
frameworks, a monorepo) produces a conflict the caller must resolve rather than a guess.

Output is a list of proposals: {pillar, skill, evidence, confidence}. The CLI pre-fills the
picker with the high-confidence ones and asks about the rest. --noninteractive callers should
treat any `conflict` as a hard stop.
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
from catalog import load_overlay, pillar_of, scan_skills  # noqa: E402  (same-dir import)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _package_json_deps(repo: str) -> dict:
    raw = _read(os.path.join(repo, "package.json"))
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    deps = {}
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        if isinstance(obj.get(k), dict):
            deps.update(obj[k])
    return deps


def detect(repo: str) -> dict:
    """Return {proposals: [...], conflicts: [...], uncovered: [...]}."""
    overlay = load_overlay()
    rules = overlay.get("detect", {})
    known = scan_skills()
    uncovered_notes = overlay.get("uncovered_note", {})

    proposals: list[dict] = []
    uncovered: list[dict] = []

    def propose(skill, evidence, confidence="high"):
        proposals.append({"pillar": pillar_of(skill), "skill": skill,
                          "evidence": evidence, "confidence": confidence})

    # package.json: parsed dependency names are high confidence
    deps = _package_json_deps(repo)
    if deps:
        for dep, skill in rules.get("package.json", {}).get("deps", {}).items():
            if dep in deps:
                if skill in known:
                    propose(skill, f"package.json depends on '{dep}' ({deps[dep]})")

    # text-contains rules for the manifest formats without a cheap parser
    for fname, rule in rules.items():
        if fname == "package.json":
            continue
        text = _read(os.path.join(repo, fname))
        if not text:
            continue
        for token, skill in rule.get("contains", {}).items():
            if re.search(re.escape(token), text):
                if skill in known:
                    propose(skill, f"{fname} mentions '{token}'", "medium")
        for token, tag in rule.get("gap", {}).items():
            if re.search(re.escape(token), text):
                uncovered.append({"stack": tag, "evidence": f"{fname} mentions '{token}'",
                                  "note": uncovered_notes.get(tag, "no skill for this stack")})
        for token, skill in rule.get("gap_signal", {}).items():
            if re.search(re.escape(token), text) and skill in known:
                propose(skill, f"{fname} mentions '{token}'", "low")

    # xcode project = iOS
    if any(n.endswith(".xcodeproj") for n in os.listdir(repo)) if os.path.isdir(repo) else False:
        if "mir-mobile-ios" in known:
            propose("mir-mobile-ios", "an .xcodeproj is present")

    # Collapse chains, then flag only genuine conflicts. mir-frontend-react and
    # mir-frontend-react-next are the SAME chain (Next extends React) -- the specific one
    # wins, it is not a conflict. mir-frontend-react vs mir-frontend-vue share no chain --
    # that is a real conflict the user must resolve.
    by_pillar: dict[str, list] = {}
    for p in proposals:
        by_pillar.setdefault(p["pillar"], []).append(p)

    collapsed: list[dict] = []
    conflicts: list[dict] = []
    for pill, cands in by_pillar.items():
        skills = {c["skill"] for c in cands}
        longest = max(skills, key=len)
        one_chain = all(s == longest or longest.startswith(s + "-") for s in skills)
        if one_chain:
            # keep the proposal for the most specific skill on the chain
            collapsed.append(next(c for c in cands if c["skill"] == longest))
        else:
            collapsed.extend(cands)
            conflicts.append({"pillar": pill, "candidates": sorted(skills)})

    return {"proposals": collapsed, "conflicts": conflicts, "uncovered": uncovered}


def render(det: dict) -> str:
    lines = ["# mir detect -- what the repo looks like", ""]
    if not det["proposals"] and not det["uncovered"]:
        return "no stack detected from lockfiles; the picker will ask."
    for p in det["proposals"]:
        lines.append(f"- **{p['skill']}** ({p['confidence']}) -- {p['evidence']}")
    for u in det["uncovered"]:
        lines.append(f"- (no skill) {u['stack']} -- {u['evidence']}: {u['note']}")
    if det["conflicts"]:
        lines.append("")
        lines.append("## Conflicts -- must be confirmed, not guessed")
        for c in det["conflicts"]:
            lines.append(f"- {c['pillar']}: {', '.join(c['candidates'])}")
    lines.append("")
    lines.append("Detection proposes. Nothing is chosen until you confirm.")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    print(render(detect(os.path.abspath(repo))))
