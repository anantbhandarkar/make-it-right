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

## [2.0.3] - 2026-08-29

### Changed

- Added the Make It Right logo to the README.

### Fixed

- Updated the end-to-end CI check to expect the current manifest version, so the latest
  branch can pass its own release checks.

### Upgrading

- No migration is required. Pull the new tag or use a fresh checkout; harness behavior is
  unchanged.

## [2.0.2] - 2026-08-26

### Changed

- Reworked the README for non-technical readers, architects, and maintainers: it now leads
  with the problem, solution, support status, installation path, and project workflow.
- Replaced internal workflow labels in the generated diagrams with plain-language steps and
  kept the deeper architecture diagrams available as expandable detail.
- Updated the skill-tree links and corrected the generated workflow's rejected-plan wording.

### Upgrading

- No migration is required. Pull the new tag or use a fresh checkout; the installer and
  project harness behavior are unchanged.

## [2.0.1] - 2026-08-25

`2.0.0` was tagged but never released: its release workflow failed on `ubuntu-latest`
with `819 passed, 1 failed`, against `820 passed, 0 failed` on macOS. No GitHub Release
was published for it. `2.0.1` is `2.0.0` plus that one fix; everything in the `2.0.0`
entry below is what you get.

The tag is left in place rather than moved. `RELEASING.md` says local verification exists
so a broken release is caught before a tag "most people will treat as durable", and the
rule does not stop applying because the person who skipped the step was the one who wrote
it down.

### Fixed

- **The path-canonicalisation control asserted a macOS fact on every platform.** It checked
  that a `tempfile`-built repo's `realpath` differs from its `abspath`, which is free on
  macOS (`/var` -> `/private/var`) and false on Linux, where `/tmp` is already canonical.
  The control exists because canonicalising the write target while leaving `find_repo_root`
  on `abspath` makes the guard block everything while looking like a security win, and the
  divergence assertion exists so the control cannot silently become vacuous. It was right to
  fail: its premise had stopped holding. The fixture now builds the repo under a symlink it
  creates itself, so the two paths disagree by construction everywhere, and the trap is
  genuinely exercised on Linux instead of skipped there.

## [2.0.0] - 2026-08-25

`mir init` emits a real write-policy harness for Codex and Antigravity, not only for Claude
Code, and Cursor is documented as conditional rather than supported. That is what the
manifest bump pays for: a harness had nowhere to record *which* hosts it was generated for,
and the probe was reading that from whoever last typed a flag. Also here: the six deferred
skills, which empties `.mir-planned` for the first time; 15 generated Mermaid diagrams; and
a probe that reads each host's own verdict channel instead of assuming Claude Code's. Scope,
and what was deliberately kept out of it, is in `GOAL.md`.

Measured on this tree, not quoted from a commit message: `./validate.py` reports **52 skills
(7 pillars, 22 tiers, 23 modules) · 6 reviewers · 0 planned · 15 diagrams — 0 errors, 4
warnings**, and `python3 init/test_init.py` reports **820 passed, 0 failed**.

If you are on the `v1.0.0` tag, read the `[1.1.0]` entry below as well. It is dated, but no
`v1.1.0` tag exists in this repository, so `v1.0.0` → `v2.0.0` is the only upgrade anyone
actually walks and it carries both entries' changes at once.

### Breaking

- **`schema.MANIFEST_VERSION` 1 → 2.** Every existing `.mir/manifest.json` now predates the
  schema. The guard warns on stderr and ALLOWS every write unchecked, on every protocol,
  until you re-run `mir init`. Failing open here is the deliberate choice and it is the
  worse-sounding half of a pair: `.mir/guard.py` is a frozen copy inside your repository, it
  cannot know what a newer manifest means, and refusing every write would brick the agent in
  a project whose policy is perfectly valid. The probe is where the mismatch fails closed —
  it now parses the guard's `GUARD_MANIFEST_VERSION` out of the guard's own source and exits
  1 on drift, naming `mir init`. Until you run either one, you have a harness that reports
  present and enforces nothing.
- **`validate_manifest` still accepts a v1 manifest**, and that is where the break bites
  rather than a softening of it. Rejecting v1 would turn "your harness is stale, regenerate
  it" into "your harness is malformed", which sends the reader to the wrong repair.
- **`.mir/guard.py` requires `--protocol <claude|codex|antigravity|cursor>`.** A missing or
  unknown value is a hard error, exit 3, and never a default to `claude`. Defaulting is
  precisely how a mis-wired Codex hook runs the Claude parser, finds no recognisable fields,
  extracts zero write targets, and allows every write while reporting clean. See "Upgrading":
  a registration written by mir 1.x carries the v1 command, and exit 3 does not block.
- **The manifest carries a `targets` block, and the probe reads the declared set from it**
  rather than from its own flags — so a target cannot be dropped from verification by
  re-running the probe with narrower arguments than the run that installed the harness. A
  declared target whose wiring file has been deleted makes the probe exit 1, not skip.
- **`schema.BASELINE_DENIED` gains repo-local `.codex` and `.agents`** (both wholesale) and
  `~/.gemini`. Cross-agent init hands the agent two more files it could disarm itself
  through. If your project legitimately has the agent edit `.agents/` or `.codex/`, those
  writes now block. `mir init` can still write `.agents/hooks.json`, because the generator
  runs outside the agent's tool loop.
- **`validate.py`'s `DIA001` is an error, not a warning**, so a stale generated diagram
  blocks installation through the gate `install.sh` already had. A fork that carries the
  managed `mir:gen:` blocks and edits the skill tree must now regenerate them, or
  `./install.sh` refuses to install for everyone.

### Added

- **Cross-agent `mir init`.** `--target claude|codex|antigravity|cursor|all` writes
  `.claude/settings.json`, `.codex/hooks.json`, and `.agents/hooks.json` respectively.
  `--target cursor` writes **no** enforcement file, by design: Cursor reads Claude Code's
  configuration and honours exit code 2 behind the user-level *Include third-party Plugins,
  Skills, and other configs* toggle, so it names that toggle in its output instead. Emitting
  a Cursor-specific file nothing reads is how Antigravity support shipped broken for months.
- `init/targets/`, one module per host, over an untouched policy engine — `decide`,
  `resolve`, `_is_under`, `canonical`, `find_repo_root` and `load_manifest` are
  byte-identical, because they were already target-neutral and correct. What is new is
  `--protocol` dispatch over event parsing and verdict emission only. Every target must fill
  a frozen `Capability` table with all four keys (`skills`, `subagents`,
  `always_on_context`, `write_policy_enforcement`), each with a `level`, a `mechanism` and a
  `source`; the `KeyError` is the test, and the package raises at import time on a malformed
  table, so a fifth target added later fails the process that loaded it rather than the
  report that rendered it. A new host has to *state* its enforcement level rather than
  inherit silence.
- **The Antigravity adapter is fail-closed by construction** — top-level catch, unloadable
  policy and unparseable event both deny — because the *host* is fail-open: a hook that
  exits non-zero or carries an uncompilable matcher is logged and skipped, and the write
  proceeds. That is the opposite posture from the Claude adapter, and the difference is
  stated in the target rather than inherited by accident. It blocks with deny-JSON on stdout
  at exit 0, since the host ignores exit codes, and prints allow-JSON too, because empty
  stdout is not an allow. Its matcher is `*` rather than an enumerated list: the binary also
  ships `sed_file`, `notebook_edit`, a `delete_file` proto field, and arbitrary MCP tools via
  `call_mcp_tool`, so naming three is a hole.
- **Codex `apply_patch` parsing** covering `Add`, `Update`, `Delete` and `Move to` — a rename
  lands bytes at a path the first header never named — unioned with the ordinary parse rather
  than replacing it, so a heredoc patch with innocent headers is still blocked for a redirect
  sitting beside it. An unparseable body **denies**. It does not return `ask`: Codex treats
  `ask` as unsupported and continues past it, so `ask` is fail-open wearing a cautious name.
- `.mir/COVERAGE.md`, written by `mir init` beside the guard. It opens with a verdict block
  rather than a table, because the first thing a reader needs is which targets are enforced
  and which are merely emitted. Every target × capability row carries what the probe proved
  and, separately, what it did **not** prove, and the four claims no command in this
  repository can check are listed with their procedure and their owner — so a green probe run
  is not read as covering them.
- `init/agents_export.py`, deriving each host's sub-agent export from `agents/*.md`
  frontmatter rather than a checked-in parallel catalog. Its `LOSSY_FIELDS` surfaces in
  `COVERAGE.md`: Codex sub-agent TOML has no `tools:` equivalent, so the reviewers'
  read-only restriction is a real loss, printed rather than dropped.
- **Six skills: `mir-cloud-aws`, `mir-cloud-gcp`, `mir-cloud-azure`,
  `mir-cloud-cloudflare`, `mir-frontend-angular`, `mir-frontend-react-remix`.** The tree goes
  46 → 52 and **the planned-not-written list is empty for the first time** — `.mir-planned`
  holds only its own instructions, and `validate.py` reports 0 planned. Angular was the only
  stack where the kit admitted in so many words that it had nothing: `mir-frontend` told the
  model to run the pillar alone and record that a tier was unavailable, so an Angular user
  got 1 of 3 layers where everyone else got 2 or 3. `mir-cloud-aws` deliberately ships no CVE
  table and says why — cloud-provider vulnerabilities are overwhelmingly mitigated
  server-side with no customer action, so citing them produces a table an engineer cannot act
  on.
  **How the diagrams render on GitHub is unverified.** No browser ran at any point in this
  release. GitHub does not document its pinned Mermaid version and trails upstream, so the
  blocks target 10.x by documentation rather than observation, and the light and dark themes
  have never been looked at. Everything mechanically checkable is checked — the subset each
  line uses, that no block sets a theme, that every styled node sets `fill`, `stroke` and
  `color` together, that colour is never the only carrier — but "it parses and the contrast
  ratios compute" is not "it reads correctly". Check both themes before relying on them.

- **15 generated Mermaid blocks**, produced by `docs/gen_diagrams.py` from `init/catalog.py`
  and reimplementing none of it, so the derived diagrams were correct at 46 skills and are
  correct at 52. `validate.py` gains `DIA001`–`DIA004` (see "Breaking" for `DIA001`). Every
  block ships three things from one data structure so they cannot disagree: the Mermaid
  source with `accTitle`/`accDescr`, a `<details>` text version, and a link list — Mermaid
  conveys no node relationships to assistive technology, and the README is also read in
  `less`, on mirrors, and in agent context windows where Mermaid never renders. The generator
  carries no clock and no hash, which is the constraint the whole mechanism rests on: a
  generator that copied `generate.py`'s `generated_at` habit would fail `--check` on every
  run for the wrong reason.
- `docs/skill-tree.md` — the full inventory, moved out of the README.
- **A probe that asks each host its own question.** `read_verdict` decides per protocol, and
  on the stdout channel the exit code is not consulted at all: an adapter that exits 2 and
  prints nothing is `GUARD-ERROR`, not `BLOCK`. Empty stdout, unparseable stdout, a missing
  or unrecognised decision, and `ask` are all `GUARD-ERROR`, never `ALLOW`, and an unknown
  protocol gets the stricter stdout reader so a fifth target cannot inherit Claude's
  semantics by silence. The wiring phase now reads the registered **command**, not only the
  matcher — see "Upgrading" for why a correct matcher over a v1 command is the exact shape of
  a harness that reports wired and enforces nothing. It also carries a real manifest-version
  check (`guard.py` had claimed "a version mismatch fails closed — the probe owns that" while
  `probe.py` contained no reference to `mir_manifest_version` at all), Bash attacks per verb
  family (`redirect`, `tee`, `dd`, `cp`) bounded linearly in `denied_paths` rather than one
  redirect shape for everything, and derived sibling controls — `.gitignore` against `.git`,
  expected ALLOW — so replacing `_is_under` with a bare `startswith` yields exit 3 instead of
  a report that is greener for having stopped discriminating.

### Changed

- **The generated `AGENTS.md` stops claiming Claude Code unconditionally.** It names the
  enforcing targets, and when any target is advisory or unverified it says so in the same
  breath. It stays thin — no hook JSON, no TOML — and is tested against 12,000 characters,
  Antigravity's per-rule-file cap, which is tighter than the figure `EXTENDING.md` cites and
  is therefore the binding one.
- `mir init`'s hook-entry recognition matches on the **guard path** rather than the whole
  command string. Without that, every untagged pre-`--protocol` v1 entry would stop being
  recognised the day the flag landed, and `mir init` would append a second entry beside the
  dead one instead of rewriting it.
- The README is shorter and no longer says Claude-Code-only anywhere; "Tool support at a
  glance" reads **conditional** for Cursor rather than yes or no, because a harness that is
  present and inert is harder to document honestly than one that is absent.

### Fixed

- **Codex got zero skills for its entire history.** `install_codex()` linked only `AGENTS.md`
  and told users to register skills through `/skills` — an instruction that could never work,
  because `/skills` only toggles skills already discovered from a root, so `mir-backend` was
  never there to select. The install path was established by probing rather than by reading
  docs: uniquely-named skills were planted in five candidate locations and `codex debug
  prompt-input` was used to see which the model actually receives. `$CODEX_HOME/skills` is
  the one chosen, because it honours the `CODEX_HOME` override `install.sh` already
  documents, and because `~/.agents` is a shared cross-tool directory other installers write
  into. Codex now receives the whole tree and honours `--scope=pillars`. Reviewer agents were
  probed the same way in four candidate directories and appeared in **none**, so mir installs
  nothing there and a test asserts `.codex/agents` is not created — an unverified guess is
  exactly how the Antigravity install target shipped broken.
- Three fixes reach a tagged release for the first time here, documented in full in the
  `[1.1.0]` entry below rather than restated: `install.sh` linked Antigravity skills into a
  directory neither shipped product reads; secrets were denied by three literal paths, so
  `src/.env` was writable while `skills/mir-init/SKILL.md` promised it was blocked; and
  eleven findings from an adversarial review were fixed at five root causes, each with a
  mutation test that reverts the fix and asserts the suite goes red.

### Security

- **Codex and Antigravity write-policy enforcement is `unverified`, and both targets say so
  in `COVERAGE.md`.** The hook file is emitted and the adapter is tested against synthesised
  events, but **no command in this repository proves either host invokes it**. Whether Codex
  requires per-hook trust approval for a repo-local `.codex/hooks.json`, whether Antigravity
  actually reads `.agents/hooks.json` and honours the deny, and whether Antigravity sub-agent
  tool calls route through the same wrapper are three of the four claims listed in
  `COVERAGE.md` with a manual procedure and a named owner. Until a maintainer runs them, do
  not treat a green probe run on those two targets as enforcement. Claude Code is `enforced`;
  Cursor is `advisory` and conditional on the toggle above, which is a fourth unrun
  procedure.
- `BASELINE_DENIED` gains repo-local `.codex` and `.agents` — see "Breaking". `~/.gemini` is
  added too and is honestly worth less: like every `~/`-rooted entry it is deny-by-default by
  the probe's own labelling, so it buys a row that cannot fail. It is a real denial, not
  coverage of itself, and the probe labels it that way rather than counting it.

### Upgrading

- **Re-run `mir init` in every project that has a `.mir/` directory.** Until you do, the
  guard prints a version-mismatch warning on stderr and allows every write unchecked; nothing
  is enforced. `mir` has no `doctor` command yet, so there is no command that will tell you
  which of your projects are in this state — a `.mir/manifest.json` whose
  `mir_manifest_version` is `1` is the whole test.
- **`.mir/guard.py` now requires `--protocol <claude|codex|antigravity|cursor>`.** A
  registration written by mir 1.x carries the v1 command with no flag; that guard exits 3 on
  every call and only exit 2 blocks, so the harness is fail-open with a matcher that still
  looks correct. Re-run `mir init` to rewrite the registration. A running Claude Code session
  holds the cached v1 command until you restart it — so the upgrade is two-phase whether or
  not you notice: regenerate, **then** restart the session, and until both have happened the
  repo is unguarded no matter what the settings file says.
- Run `.mir/probe.py` afterwards and read the wiring phase, not just the attack count. It now
  compares the registered command, so the case above shows up as a command row rather than a
  green matcher row over a dead hook.
- If you install for Codex or Antigravity, run `./install.sh --tool=codex` /
  `--tool=antigravity` again. The Codex skill path and the Antigravity skill path both moved
  (see "Fixed" and the `[1.1.0]` entry); the old locations are directories nothing reads.
  `./install.sh --prune --dry-run` shows what the old targets left behind under `$HOME`.
- If you use Cursor, enable *Include third-party Plugins, Skills, and other configs*. Nothing
  in the harness can turn it on for you, and with it off the guard is present and inert.
- If anything in your project has the agent write to `.agents/` or `.codex/`, those writes
  are now denied. Move the work outside the agent's tool loop, or drop the entry from
  `denied_paths` in your manifest and accept that the agent can unregister its own guard.
- If your fork carries the generated `mir:gen:` diagram blocks, run
  `python3 docs/gen_diagrams.py --check` before `./install.sh`. A stale block is now
  `DIA001`, an error, and `install.sh` refuses to install a tree with errors.

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
