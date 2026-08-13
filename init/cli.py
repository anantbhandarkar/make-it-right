#!/usr/bin/env python3
"""mir init — prepare a repo's Make It Right harness: detect, confirm, generate, verify.

Flow:
  1. detect the stack from the repo (proposes, never decides)
  2. confirm / fill via the derived picker  (interactive) or --answers (scripted)
  3. resolve the chosen options to the skill set + gaps
  4. generate the artifacts (manifest, guard, hooks, thin AGENTS.md, CLAUDE.md)
  5. verify: run the manifest-derived probe against the generated guard

Nothing is installed. --dry-run writes nothing. Gaps (stacks with no skill) are reported and
route to the pillar; the quarantined generative path is a later slice, not this one.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import catalog  # noqa: E402
import detect as detect_mod  # noqa: E402
import generate as gen  # noqa: E402


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _answers_from_detection(det: dict) -> dict:
    answers: dict = {}
    for p in det["proposals"]:
        answers.setdefault(p["pillar"], p["skill"])
    return answers


def cmd_init(args) -> int:
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        print(f"no such repo: {repo}", file=sys.stderr)
        return 2

    # 1. detect
    det = detect_mod.detect(repo)

    # 2. answers
    if args.answers:
        answers = json.load(open(args.answers, encoding="utf-8"))
        source = f"--answers {args.answers}"
    else:
        answers = _answers_from_detection(det)
        source = "detection"
        if det["conflicts"] and args.noninteractive:
            print("mir init: detection is ambiguous and --noninteractive was set:", file=sys.stderr)
            for c in det["conflicts"]:
                print(f"  {c['pillar']}: {', '.join(c['candidates'])}", file=sys.stderr)
            print("Pass --answers with an explicit choice.", file=sys.stderr)
            return 3

    print(detect_mod.render(det))
    print()
    if not answers:
        print("Nothing detected and no --answers given. Provide --answers to proceed.", file=sys.stderr)
        return 3
    print(f"Using answers from {source}:")
    for k, v in answers.items():
        print(f"  {k}: {v}")
    print()

    # 3. resolve
    res = catalog.resolve(answers)
    print("Resolved skills (coarse → fine):")
    for s in res["skills"]:
        print(f"  {s}")
    if res["gaps"]:
        print("\nGaps (no skill; route to the pillar, generative path deferred):")
        for g in res["gaps"]:
            print(f"  {g['pillar']}/{g['choice']} — {g['reason']}")
    print()

    # 4. generate
    stamp = _stamp()
    items = gen.plan(repo, res["skills"], answers, stamp)
    if args.dry_run:
        print("--dry-run: would write these files (nothing written):")
        for it in items:
            print(f"  {it['path']}  ({it['note']})")
        return 0
    written = gen.apply(repo, items)
    print("Wrote:")
    for w in written:
        print(f"  {w}")
    print()

    # 5. verify
    probe = os.path.join(repo, ".mir", "probe.py")
    print("Verifying the generated guard against its manifest...")
    rc = subprocess.call([sys.executable, probe, "--repo", repo])
    print()
    print("Note: Claude Code snapshots hooks at session start, so the hook does NOT protect")
    print("the current session. Restart Claude Code (and approve the new .claude/settings.json)")
    print("for the guard to take effect.")
    if rc != 0:
        print("\nPROBE FAILED: a denied path was not blocked. Do not rely on this harness.",
              file=sys.stderr)
        return 1
    return 0


def cmd_catalog(args) -> int:
    print(json.dumps(catalog.derive_catalog(), indent=2))
    return 0


def cmd_detect(args) -> int:
    print(detect_mod.render(detect_mod.detect(os.path.abspath(args.repo))))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mir", description="Make It Right project harness tools.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="prepare a repo's harness")
    p_init.add_argument("repo", nargs="?", default=".")
    p_init.add_argument("--answers", help="JSON file of {pillar: choice} to skip the picker")
    p_init.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    p_init.add_argument("--noninteractive", action="store_true", help="fail on ambiguity, don't guess")
    p_init.set_defaults(func=cmd_init)

    p_cat = sub.add_parser("catalog", help="print the derived picker")
    p_cat.set_defaults(func=cmd_catalog)

    p_det = sub.add_parser("detect", help="show what the repo looks like")
    p_det.add_argument("repo", nargs="?", default=".")
    p_det.set_defaults(func=cmd_detect)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
