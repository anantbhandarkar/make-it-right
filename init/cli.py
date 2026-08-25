#!/usr/bin/env python3
"""mir init — prepare a repo's Make It Right harness: detect, confirm, generate, verify.

Flow:
  1. detect the stack from the repo (proposes, never decides)
  2. confirm / fill via --answers, or accept detection ONLY where it decided one skill per
     pillar. Two candidates for a pillar, or a detected stack with no skill, is a hard stop
     (exit 3) in every mode -- see _refuse_undecided.
  3. resolve the chosen options to the skill set + gaps
  4. generate the artifacts (manifest, guard, hooks, thin AGENTS.md, CLAUDE.md)
  5. verify: run the manifest-derived probe against the generated guard, and pass its exit
     code through (0 clean / 1 leak / 2 could not run / 3 inconclusive)

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


def _answers_from_detection(det: dict) -> tuple:
    """Collapse the proposals to one choice per pillar. Returns (answers, undecided).

    `undecided` is {pillar: sorted candidates} for every pillar that collected more than one
    distinct proposal. It is built from a pillar -> SET rather than read off det["conflicts"]
    because those are not the same question: detect.py puts BOTH candidates into `collapsed`
    when it records a conflict, so a pillar can be genuinely undecided here. The previous
    setdefault silently kept whichever proposal dict iteration happened to reach first --
    a stack decision made by hash order and reported to nobody.
    """
    by_pillar: dict = {}
    for p in det["proposals"]:
        by_pillar.setdefault(p["pillar"], set()).add(p["skill"])
    answers, undecided = {}, {}
    for pillar, skills in by_pillar.items():
        if len(skills) == 1:
            answers[pillar] = next(iter(skills))
        else:
            undecided[pillar] = sorted(skills)
    return answers, undecided


def _refuse_undecided(undecided: dict, uncovered: list, answers: dict) -> int:
    """Stop before anything is written, and hand back everything needed to decide.

    Only UNDECIDED INPUT triggers this. A clean single-framework repo has nothing to confirm,
    and demanding --answers there would make the CLI unusable -- refusing always is not
    honesty, it is a different defect. There is deliberately no --accept-detection escape
    hatch: --answers already IS the escape hatch, and a "just guess" flag would re-create the
    silent resolution behind a name that makes it sound approved.
    """
    out = sys.stderr
    cat = catalog.derive_catalog()
    print("mir init: detection did not decide this repo's stack, and mir does not guess.",
          file=out)
    print("A guessed stack loads the wrong gates, which is worse than no gates: it reads as",
          file=out)
    print("verified. Nothing was written.", file=out)

    for pillar, cands in sorted(undecided.items()):
        print(f"\n  {pillar}: detected {', '.join(cands)} -- pick exactly one of:", file=out)
        for o in (cat["pillars"].get(pillar) or {}).get("options", []):
            print(f"    {o['value']}   ({o['label']})", file=out)
    for u in uncovered:
        print(f"\n  (no skill) {u['stack']} -- {u['evidence']}", file=out)
        print(f"    {u['note']}", file=out)
        print("    Name the pillar yourself in --answers; pick its module if one is close,",
              file=out)
        print("    or its __other_<pillar>__ option to record the gap.", file=out)

    stub = dict(answers)
    for pillar in undecided:
        stub[pillar] = "REPLACE_WITH_ONE_VALUE_FROM_THE_LIST_ABOVE"
    print("\nWrite this to answers.json, replace every REPLACE_ value, then re-run:", file=out)
    print("  mir init --answers answers.json", file=out)
    print(json.dumps(stub, indent=2), file=out)
    return 3


# What each probe exit code means to someone reading the end of `mir init`. Branching here is
# not cosmetic: the single old line said "a denied path was not blocked" for ANY nonzero,
# which becomes a false claim the moment the probe can also report "I could not tell". Naming
# a leak that was never found is the same laundering the probe exists to prevent, pointed the
# other way.
_PROBE_MESSAGES = {
    1: ("PROBE FAILED: the guard does not enforce its manifest. Either a denied path reached\n"
        "the target, or the hook is not registered for a tool that writes. Read the LEAKS and\n"
        "Wiring sections above. Do not rely on this harness."),
    2: ("PROBE COULD NOT RUN: it found no manifest or no guard, so nothing was verified.\n"
        "This is not a passing harness; it is an unchecked one."),
    3: ("PROBE INCONCLUSIVE: no leak was found, and no leak could have been found. A positive\n"
        "control was blocked, the guard errored, or its wiring could not be confirmed -- so\n"
        "every BLOCK row above is undiscriminating and the run proves nothing either way.\n"
        "Loosen the policy, wire the hook, or pass --allow-false-blocks to .mir/probe.py if\n"
        "the over-tightening is deliberate."),
}


def probe_message(rc: int):
    """The line to print for a probe exit code, or None when the probe passed."""
    if rc == 0:
        return None
    return _PROBE_MESSAGES.get(
        rc, f"PROBE FAILED with exit code {rc}, which this CLI does not recognise. "
            "Treat the harness as unverified.")


def cmd_init(args) -> int:
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        print(f"no such repo: {repo}", file=sys.stderr)
        return 2

    # 1. detect
    det = detect_mod.detect(repo)
    print(detect_mod.render(det))
    print()

    # 2. answers
    if args.answers:
        answers = json.load(open(args.answers, encoding="utf-8"))
        source = f"--answers {args.answers}"
    else:
        answers, undecided = _answers_from_detection(det)
        source = "detection"
        # A conflict detect.py recorded and a pillar that merely collected two proposals are
        # the same defect seen from two sides, so both feed one refusal. conflicts is merged
        # IN rather than trusted alone, because it is the narrower signal of the two.
        for c in det["conflicts"]:
            undecided.setdefault(c["pillar"], list(c["candidates"]))
        # --noninteractive is deliberately not consulted. It means "never prompt", not "guess
        # quietly": the ambiguity is the user's to resolve in either mode, and the only thing
        # a flag could change is whether mir admits it resolved it for them. An uncovered
        # signal stops here too -- a detected stack with no skill was being read and then
        # dropped, so the harness shipped claiming gates it does not have.
        if undecided or det["uncovered"]:
            return _refuse_undecided(undecided, det["uncovered"], answers)

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

    # 4. generate. A destination mir cannot write safely aborts the whole run before any
    # byte lands: a partial harness is worse than none, because it looks installed.
    stamp = _stamp()
    try:
        items = gen.plan(repo, res["skills"], answers, stamp)
        if args.dry_run:
            print("--dry-run: would write these files (nothing written):")
            for it in items:
                print(f"  {it['path']}  ({it['note']})")
            return 0
        written = gen.apply(repo, items)
    except gen.GenerateError as e:
        print(f"mir init: {e}", file=sys.stderr)
        print("Nothing was written. Resolve the above and re-run.", file=sys.stderr)
        return 3
    print("Wrote:")
    for w in written:
        print(f"  {w}")
    print()

    # 4b. optional: scope this repo's skills locally
    if args.install:
        repo_dir = os.path.dirname(HERE)
        linked = gen.install_project_skills(repo, res["skills"], repo_dir)
        removed = gen.prune_project_skills(repo, res["skills"])
        print(f"Linked {len(linked)} project skill(s) into .claude/skills/:")
        for s in linked:
            print(f"  {s}")
        if removed:
            print(f"Removed {len(removed)} no-longer-resolved skill(s): {', '.join(removed)}")
        print("  (pillars come from the global install; run")
        print("   ./install.sh --scope=pillars to keep that floor small)")
        print()

    # 5. verify
    probe = os.path.join(repo, ".mir", "probe.py")
    print("Verifying the generated guard against its manifest...")
    rc = subprocess.call([sys.executable, probe, "--repo", repo])
    print()
    print("Note: Claude Code snapshots hooks at session start, so the hook does NOT protect")
    print("the current session. Restart Claude Code (and approve the new .claude/settings.json)")
    print("for the guard to take effect.")
    msg = probe_message(rc)
    if msg:
        print("\n" + msg, file=sys.stderr)
        # The probe's own code is passed through rather than flattened to 1, because a caller
        # gating a pipeline has to be able to tell "a denied path leaked" from "the run could
        # not tell" -- collapsing them is the distinction the third code was added to make.
        return rc
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
    p_init.add_argument("--install", action="store_true",
                        help="symlink this repo's tiers/modules into .claude/skills/ "
                             "(pairs with ./install.sh --scope=pillars)")
    p_init.add_argument("--noninteractive", action="store_true",
                        help="never prompt. Ambiguity is a hard stop in BOTH modes; this flag "
                             "only forbids asking, it never licenses a guess")
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
