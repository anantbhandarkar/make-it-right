# Changelog

All notable changes to Make It Right are documented in this file.

The format is [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), plus two
sections this project adds on top of the standard:

- **`### Breaking`** — present, and non-empty, **if and only if** the release is a MAJOR
  version. This is mechanical, not a style preference: `.github/workflows/release.yml`
  fails a `*.0.0` tag whose `### Breaking` section is empty or missing, and fails a
  non-major tag whose `### Breaking` section has content. The section decides the version
  number; it does not just describe it after the fact.
- **`### Upgrading`** — free-form, per release, for what a person on the previous version
  needs to *do*, not just what changed. Removal/prune instructions live here, because
  reading "what changed" and missing "and here is what to do about it" is its own defect.

This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). `VERSION`
holds the current version as one line with no `v` prefix; the `v` belongs to the git tag
(see `RELEASING.md`).

## [Unreleased]

## [1.1.0] - 2026-08-25

Release engineering: a version file, a changelog, CI that has never existed before, and the
backlog of correctness/security fixes found by an adversarial review of the v1.0.0 tree.
Scope argued out in `docs/v2-plan.md` ("The version question"): every item below is
additive to the public surface, or a fix to a defect the shipped v1.0.0 tree already had —
nothing here changes `mir_manifest_version` (it stays 1) or removes/renames a skill slug,
which are what the plan reserves for a `2.0.0`.

### Added

- Root `VERSION` file (one line, no `v` prefix) as the single source of truth for the
  project's version, and `init/_version.py`, the only Python that parses it.
  `read_version()` never raises — a missing or unreadable `VERSION` falls back to a
  placeholder rather than stopping `mir init` from generating a harness, because the
  version is metadata, not a precondition of the security tooling.
- `mir --version`, reporting the `VERSION` file's contents augmented with `git describe`
  when available (install.sh symlinks rather than copies, so the installed version is a
  property of the working tree, not a release artifact). Regression-tested in
  `init/test_release.py`, because it works despite the `init`/`catalog`/`detect` subparser
  being `required=True` only by argparse's internal ordering (the `version` action exits
  before the required-subcommand check runs), which is not a documented contract.
- `install.sh --prune`, `--prune-only`, and `--dry-run` — opt-in removal of stale symlinks
  this checkout can prove it owns. A plain `./install.sh` still never deletes anything; it
  now also warns when it detects links that look like leftovers from a rename, a removed
  skill, or a narrower `--scope`.
- `probe.py` gained a wiring phase (does the hook-registration file exist, parse, and route
  to the guard with the right matcher) and a third exit code, `3` (`INCONCLUSIVE`) —
  distinct from `1` (a leak was found), because a guard that blocks everything, including
  its own positive controls, was previously reported as a clean run.
- `.github/workflows/ci.yml` and `.github/workflows/release.yml`. There was no CI in this
  repository before this release; `init/test_init.py`'s assertions (36 at v1.0.0, 246+ as of
  this release) had never been run by anything but a human, on any Python but the author's.
- `CHANGELOG.md` (this file) and `RELEASING.md`.

### Fixed

- **Antigravity installs never worked.** `install_antigravity()` linked skills into
  `~/.gemini/antigravity/skills` since the initial commit; neither shipped Antigravity
  product reads that path (verified against the shipped binaries' own generated
  `read_file` allowlist, not their docs). Every `--tool=antigravity` install had put 46
  symlinks somewhere never scanned. Fixed target: `~/.gemini/config/skills`.
- **Symlink escape, on generation and on enforcement.** `generate.apply()` followed a
  symlinked `AGENTS.md` or `.claude/settings.json` and wrote through it to whatever it
  pointed at, outside the repo; the guard compared paths with `normpath`, not `realpath`,
  so an allowed alias (`src/link -> ../.mir/guard.py`) was approved and the guard
  overwritten through it. One `canonical()` now runs on both sides of every comparison
  (deny on the union of lexical and canonical forms, allow on canonical only, so nothing
  that blocked before can start allowing), and writes go through `O_NOFOLLOW`.
- **A stale tagged hook survived every re-run.** `merge_settings` returned the moment it
  saw its own ownership tag, so widening the registered matcher never reached an
  already-initialized repo — `mir init` reported success and the user gained no coverage.
  The ownership marker is now an address, not a flag: `mir init` locates the owned region,
  diffs it against the desired shape, and rewrites in place.
- **`generate.apply()` wrote to destinations it had never inspected.** A `settings.json`
  that failed to parse (truncated, or JSONC) was treated as absent and had every existing
  hook replaced; an unowned, human-written `AGENTS.md`/`CLAUDE.md` got no refusal and no
  backup. One `inspect_destination()` precheck now classifies every write target before any
  byte lands, and refusal is all-or-nothing — a partial harness looks installed, which is
  worse than none.
- **`CLAUDE.md` was overwritten wholesale on every re-run**, unlike `AGENTS.md`, despite
  being documented as having room for repository notes. It now preserves a marked tail the
  same way `AGENTS.md` does.
- **The Bash write-target parser modeled one command with one target.**
  `tee allowed.txt .mir/guard.py` and `cp safe .git/config; echo done` both evaded it — it
  checked exactly one destination in exactly one segment. Now segment-split, tokenised, and
  matched per verb.
- **`install.sh` had no removal logic anywhere in the file.** A skill renamed or removed
  left a dangling symlink, and reducing `--scope` was a no-op that reported success
  (`--scope=pillars` wrote 7 links and silently left the other 39 in place). Fixed by
  `--prune` above, gated on an ownership test: the link's name looks like ours, it is
  actually a symlink, and it resolves under this checkout — never a guess.
- **`prune_project_skills()` could delete a `mir-*` symlink it did not own** — another
  checkout's, or a user's own edited copy wearing the same slug. Gated on the same
  ownership test as `install.sh --prune`.
- **The CLI silently resolved genuine stack ambiguity.** A repo detecting both React and
  Vue had its pillar decided by `dict.setdefault` — effectively hash-order — and
  `mir init` proceeded and exited 0. It now refuses (exit 3) before writing anything, and
  prints a paste-ready `--answers` stub naming exactly what is undecided. See "Upgrading":
  this is the one fix most likely to change an existing script's exit code.
- **`init/guard.py` never read `mir_manifest_version`.** No version negotiation existed
  between a frozen `.mir/guard.py` copy and the manifest it enforces. It now compares
  versions and reports drift loudly on stderr every invocation, and still allows on
  mismatch — deliberately fails open, because failing closed would brick every existing
  user's agent on their next upgrade; the blocking gate belongs in `mir doctor`/`mir init`,
  where a human sees it, not in a running session.

### Security

- **The `.env` family was denied only at the repository root.** `skills/mir-init/SKILL.md`
  stated "`src/.env` is blocked though `src` is writable"; it was not. `BASELINE_DENIED`
  held the three literals `.env`, `.env.local`, `.env.production`, matched by path prefix
  from the repo root, so `src/.env` and `.env.development` were writable. A false security
  claim about the flagship feature, shipped in v1.0.0. `denied_paths` entries may now carry
  glob metacharacters (matched with `fnmatch`, at any depth); `BASELINE_DENIED` carries
  `**/.env*` (plus `.envrc` and `.env.example`, both real secret vectors) in place of the
  three literals — a strict superset. `MANIFEST_VERSION` stays `1`: this widens the
  baseline, it does not change the schema, and a v1 manifest with only literal
  `denied_paths` still validates unchanged.
- **`.claude/settings.json` was denied; `.claude/settings.local.json` was not**, though
  Claude Code reads both and either can carry a `PreToolUse` hook. The deny pattern is now
  `.claude/settings*.json`.
- `probe.py` now instantiates each `denied_paths` glob across depths and suffixes instead
  of attacking the pattern's literal text (which previously meant a glob-blind guard could
  pass the probe by treating the pattern as an inert literal string). A permanent assertion
  requires every `BASELINE_DENIED` entry to yield at least one metacharacter-free attack
  path distinct from the entry itself, closing the class of defect where a security fix
  ships invisible to its own prober.

No `### Breaking` section: this release is MINOR, and `.github/workflows/release.yml` fails
a non-major tag whose `### Breaking` section is non-empty, so the section is omitted here
rather than filled with "None." `MANIFEST_VERSION` stays 1 (verified: `init/schema.py` and
its tests assert this explicitly, and a v1 manifest with only literal `denied_paths` still
validates against the widened baseline above), and no skill slug was renamed or removed —
see `docs/v2-plan.md`, "The version question," for the full argument. See "Upgrading" below
for the one behaviour change worth knowing about even though it does not break a documented
contract.

### Upgrading

- Run `./install.sh --prune --dry-run` after pulling this release to see what a rename, a
  removed skill, or a scope change left behind under your `$HOME`; add `--prune` to remove
  it. This is opt-in and always was — a plain `./install.sh` still deletes nothing.
- `mir init` now refuses (exit 3) on a repo where stack detection is genuinely ambiguous
  (for example, both React and Vue detected for the frontend pillar) instead of silently
  picking one and proceeding. This restores `mir init`'s own documented behaviour (confirm
  before generating) rather than changing it, but a script that depended on unconditional
  success on such a repo will now see exit 3 and must supply `--answers`.
- If anything wraps `.mir/probe.py` and checks `rc == 1` specifically for "not safe", widen
  that check: any nonzero exit (`1` leak found, `2` could not run, `3` inconclusive — new in
  this release) means "do not trust this harness."
- If you rely on the `.env` family staying denied outside the repository root, re-run
  `mir init` in any project harness generated before this release — see "Security," above.

## [1.0.0] - 2026-08-13

First tagged release.

### Added

- 46 skills across 7 pillars, 17 runtime/reactivity tiers, and 22 framework/platform
  modules, plus 6 skills recorded in `.mir-planned` as roadmap, not yet written.
- 6 read-only reviewer sub-agents in `agents/` (`a11y-reviewer`, `constraint-interrogator`,
  `frontend-perf-reviewer`, `migration-reviewer`, `reliability-reviewer`,
  `security-reviewer`).
- `mir init`: detect → confirm → resolve → generate → verify, emitting a manifest-derived
  write policy, a `PreToolUse` guard, and a self-verifying probe.
- `validate.py`: enforces 0 errors at install time (naming/frontmatter contract, reference
  and chain integrity, security-section presence, context budget); `.mir-planned` keeps a
  roadmap entry a warning instead of letting a broken reference chain read as clean.
- Progressive install: `install.sh --scope=pillars` plus `mir init --install` keeps a
  repo's global skill floor at the 7 pillars (~2k tokens) instead of all 46 skills
  (~15k tokens), with the runtime tiers and framework modules resolved per project.
- `install.sh --tool=claude|cursor|codex|antigravity|all`.

### Breaking

None — this is the first tagged release; there is no prior public version to be
incompatible with.
