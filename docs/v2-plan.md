# Make It Right — v2 plan

Status: draft, 2026-08-25. Written after `v1.0.0` was tagged and pushed.

This plan covers four workstreams, five verified defects in the shipped tree, and a
recommendation on the version number. Every claim about the current code was checked
against the tree at `863af85`; where something could not be checked, it says so.

Baseline at time of writing:

```
46 skills  (7 pillars, 17 tiers, 22 modules)  · 6 reviewers · 6 planned
0 errors, 7 warnings
25 passed, 0 failed        (init/test_init.py)
```

The 7 warnings are 4× `CTX001` (bodies at 383/383/388/383 against a 380 threshold) and
3× `REF002` (`.mir-planned` entries working as designed).

---

## Contents

- [The version question](#the-version-question)
- [Verified defects in v1.0.0](#verified-defects-in-v100)
- [Workstream A — release engineering](#workstream-a--release-engineering)
- [Workstream B — visual documentation](#workstream-b--visual-documentation)
- [Workstream C — the six deferred skills](#workstream-c--the-six-deferred-skills)
- [Workstream D — cross-agent `mir init`](#workstream-d--cross-agent-mir-init)
- [Sequencing](#sequencing)
- [Open questions](#open-questions)

---

## The version question

**On the scope as originally framed, `2.0.0` is not honest. It is `1.1.0`.**

Every item is additive:

| Item | Semver impact |
|---|---|
| 6 new skills | Additive. The tree is flat; slugs are independent. |
| Mermaid diagrams + generator | Additive. |
| `VERSION`, `CHANGELOG`, CI, `--prune`, `mir doctor` | Additive. CI and CHANGELOG are not public surface at all. |
| `validate.py --max-warnings` / exit 3 | Additive by construction — a caller that never passes the flag can never receive a 3. |
| "Codex and Antigravity support" | **Neither new nor breaking.** See below. |

That last row was the original premise of v2 and it does not survive checking.
`git log -S "install_antigravity" -- install.sh` returns exactly one commit: `1a45da5`,
the initial release. Both `install_codex` and `install_antigravity` have shipped since
day one and have their own README sections.

What is genuinely Claude-Code-only is **`mir init`**, the harness *generator*.
`init/generate.py` emits `.claude/settings.json` with a Claude Code `PreToolUse` hook
and nothing else.

### What earns a major bump

Cross-agent `mir init` has nowhere to record which tool a harness targets, so it forces
`schema.MANIFEST_VERSION` from 1 to 2. Every existing `.mir/manifest.json` then predates
the schema. That is a real breaking change.

Other candidates, any one of which carries a MAJOR on its own:

1. Renaming or removing a skill slug. Users type these (`/mir-backend-python-fastapi`)
   and third-party `AGENTS.md` files name them. No tooling can repair a rename.
2. Bumping `MANIFEST_VERSION` to 2.
3. Changing `install.sh`'s default `--scope` from `all` to `pillars` — a behaviour
   change on an unchanged command line, regardless of it being an improvement.
4. Adding a required `SKILL.md` frontmatter key. `EXTENDING.md` publishes that contract
   as a stable extension interface; a new required key fails every third-party skill,
   and `install.sh` then refuses to install the whole tree.
5. Moving `mir init`'s output paths. Existing `.claude/settings.json` files hold a
   command string pointing at `.mir/guard.py`.

### Recommendation: two releases

- **`1.1.0`** — Workstreams A, B, C. All additive. Ships sooner and gets CI green on a
  known-good tree before any behaviour changes land.
- **`2.0.0`** — Workstream D, carrying `MANIFEST_VERSION = 2` as its stated breaking
  change.

The alternative — one `2.0.0` containing everything — is fine, but then the CHANGELOG's
`### Breaking` section must lead with the manifest bump and not with "lots of new
skills." A repository whose entire product is a claim about rigor cannot afford a
semver violation on its own front page.

**Mechanism, not intention:** the release workflow must fail a `*.0.0` tag whose
`### Breaking` section is empty. That turns the argument above into a rule, which is the
same move `validate.py` made for the naming convention.

---

## Verified defects in v1.0.0

These are latent in the shipped tree right now, independent of any v2 scope decision.
Each was confirmed by running the code or reading the exact line cited.

### D1 — `validate.py` cannot see three of the four cloud references

`validate.py:36`:

```python
SKILL_REF = re.compile(r"\bmir-[a-z0-9]+(?:-[a-z0-9]+)*\b")
```

`skills/mir-cloud/SKILL.md` writes its Gate 5 handoff as:

```
Load the provider module (`mir-cloud-aws` / `-gcp` / `-azure` / `-cloudflare`) now
```

`-gcp`, `-azure` and `-cloudflare` are bare suffixes with no `mir-` prefix, so the regex
never matches them. Confirmed: `validate.py` emits exactly **one** `REF002` for
`mir-cloud`, not four.

The consequence bites later. The moment `mir-cloud-aws` ships, `mir-cloud` goes
completely silent while three modules its Gate 5 still names remain unwritten — and
nothing then stops someone pruning them from `.mir-planned` as unreferenced.

This is the failure class `validate.py` exists to catch, reproduced inside the file that
documents it.

**Fix:** spell all four slugs in full at three sites in `skills/mir-cloud/SKILL.md`
(Gate 5 ~line 191, "Composing with your other skills" ~line 317, and the edit map
~line 322). Cost is about 60 characters of body. This must land **before** any cloud
module is written.

### D2 — `merge_settings` silently no-ops on a matcher change

`init/generate.py:84`:

```python
if h.get("command") == cmd or HOOK_TAG in json.dumps(entry):
    return settings, False  # already present
```

The `HOOK_TAG` arm matches on the tag alone. If v2 widens `MATCHER` (currently
`Write|Edit|MultiEdit|NotebookEdit|Update|Bash`), re-running `mir init` finds the tagged
entry, returns `changed=False`, and the **old matcher persists**. The user re-runs init,
sees success, and gains no coverage.

**Fix:** update the tagged entry in place rather than skipping it. Add a `test_init.py`
case asserting a stale matcher gets rewritten.

### D3 — the guard has no version negotiation, and fails open

`schema.py:114` validates `mir_manifest_version`. `init/guard.py` contains **no
reference to it at all** — not to `mir_manifest_version`, not to `MANIFEST_VERSION`, not
to `validate_manifest`. It reads `manifest["policy"]` inside a bare `try/except` that
returns `ALLOW` on any exception.

`generate.py` **copies** `guard.py` and `probe.py` into each project's `.mir/`, so a v1
project holds a v1 guard forever. If v2 relies on a policy key a v1 manifest lacks,
`policy.get(key, [])` returns the default, that dimension is silently unenforced, and
the guard reports a clean run.

D2 and D3 compound into the repository's own nightmare: a v2 security fix does not reach
existing projects (frozen copy), re-running `mir init` does not update the matcher
(silent no-op), the guard cannot detect the drift (no version read), and it fails open.
Four defensible behaviours combining into "reports clean, enforces nothing."

**Fix:** have `guard.py` compare `manifest.get("mir_manifest_version")` against its own
and emit a loud stderr line on mismatch, then **continue**. Do not fail closed on a
version mismatch — that bricks every existing user's agent on upgrade, which is worse
than the drift. The blocking gate belongs in `mir doctor` and `mir init`, where a human
sees it.

### D4 — `install.sh` never removes anything

There is no `rm`, no `unlink`, no removal logic anywhere in the file.
`install_skills_to()` and `install_agents_to()` only ever call `ln -sfn`.

Three concrete failures:

1. A skill renamed or removed in v2 leaves a **dangling symlink** at
   `~/.claude/skills/<old-name>`. The user sees a skill name that loads nothing — the
   same silent-no-content failure `d9a9609` was written to eliminate, reappearing one
   layer down at the install boundary. `validate.py` cannot see it; it validates the
   repo, not the user's `$HOME`.
2. **Reducing scope is a no-op that reports success.** A user who ran the default
   `--scope=all` (46 links) and then runs `--scope=pillars` gets 7 links written and 39
   left in place, then reads `NOTE  scope=pillars: linked 7 pillar(s) globally.` They
   believe they cut the global floor and cut nothing.
3. Moving or re-cloning the repo orphans every link with no command to repair it.

**Fix:** `install.sh --prune` / `--prune-only` / `--dry-run`. Ownership test: remove a
link only if its basename starts with `mir-` (or matches a file in `agents/`) **and** its
link text resolves under `$REPO_DIR`. A `mir-*` link pointing at a different checkout
belongs to someone else's setup — report and refuse, never guess. Never touch a real
file or directory. Dry-run is the default posture.

### D6 — `install.sh` linked Antigravity skills into a directory nothing reads

**Fixed 2026-08-25.** `install_antigravity()` targeted
`${GEMINI_HOME:-$HOME/.gemini}/antigravity/skills`. Neither installed Antigravity
product reads that path, so every `--tool=antigravity` install since `1a45da5` linked 46
skills into a directory that is never scanned. Antigravity support has never worked.

The decisive evidence is Antigravity's own runtime behaviour, not its docs. The app
generates a `read_file` permission allowlist into its system prompt and persists it at
`~/.gemini/antigravity-{cli,ide}/brain/<id>/.system_generated/logs/transcript.jsonl`:

| Path | CLI | IDE |
|---|---|---|
| `~/.gemini/config/skills` | allowed | allowed |
| `~/.gemini/antigravity-cli/skills` | allowed | — |
| `~/.gemini/antigravity-ide/skills` | — | allowed |
| `~/.gemini/antigravity/skills` | **absent** | **absent** |
| `~/.gemini/skills` | **absent** | **absent** |

`~/.gemini/config` itself is `denied` while `config/skills` is `allowed`, so this is a
deliberately enumerated allowlist rather than a prefix rule. `antigravity/` is the
legacy Antigravity 2.0 product directory; `~/.gemini/config/.migrated` (May 21) and
`~/.gemini/antigravity/mcp_config.json` symlinked into `config/` record the move.

Two traps this defect illustrates, both worth remembering:

1. **A populated directory is not evidence that the app reads it.** Both
   `~/.gemini/skills` and `~/.gemini/antigravity/skills` contain the same 9 skills —
   independent copies written by an unrelated third-party installer, same mtime to the
   second. Their existence was mistaken for confirmation that the path was live.
2. **Negative runtime evidence settled it.** Across roughly two months of heavy use,
   grepping every log, conversation and brain transcript for those 9 skill names returns
   zero hits. They have sat in both candidate directories since June and have never
   loaded.

Fix: `install_skills_to "$base/config/skills"`. Verified by installing into a throwaway
`GEMINI_HOME` — 46 skills linked, all resolving.

Still unresolved: the precedence between product-local `<product>/skills` and global
`config/skills`. Shipped docs list only global discovery; product-local appears in the
path table but not the discovery doc. This does not affect the fix.

### D7 — the guard did not protect the `.env` file it named — **FIXED 2026-08-25**

`skills/mir-init/SKILL.md` stated: "denied paths win even inside an allowed root, so
`src/.env` is blocked though `src` is writable." That named example was **not blocked**.
`BASELINE_DENIED` held the three literals `.env`, `.env.local`, `.env.production`, and
matching was path-prefix from the repository root, so only those exact root files were
denied. Measured against a generated harness before the fix:

```
src/.env             exit=0   ALLOWED
.env.development     exit=0   ALLOWED
.env                 exit=2   blocked
```

A false security claim, shipped in `v1.0.0`, about the flagship feature.

**Why the probe did not catch it.** `probe.py` derives its attacks from the manifest —
which is the right architecture, and is exactly why it missed this. The manifest said
`.env`, so the probe attacked `.env`, passed, and reported clean. A manifest-derived
prober inherits the manifest's blind spots. Fixing the denylist alone would have left
the next instance of this class equally invisible.

**Fix.** `denied_paths` entries may now contain glob metacharacters, matched with
`fnmatch` at any depth; literal entries keep exact prefix semantics unchanged.
`BASELINE_DENIED` carries `**/.env*` in place of the three literals — a strict superset.
This needed no schema change, so `MANIFEST_VERSION` stays 1 and existing manifests keep
validating. A separate `denied_globs` key was rejected because `validate_manifest`
requires every `_POLICY_KEYS` entry to be present, which would have failed every v1
manifest; a hardcoded basename list in the guard was rejected because a denial invisible
to the manifest is invisible to the probe, which is the failure mode that let this ship.

`probe.py` now instantiates each glob pattern across depths and suffixes — the `.env`
family generates 9 attacks instead of 2 — and `test_init.py` carries a mutation test
that patches the generated guard's `is_glob()` to return `False` and asserts the probe
exits 1. The probe now catches this regression class rather than the denylist merely
being longer.

Verified on Python 3.9.6 and 3.14.6: 36 passed, 0 failed on both. `src/.env`,
`.env.development`, `a/b/.env.local` and `.envrc` block; `src/environment.ts` and
`env/config.ts` still write.

Deliberate call: `.env.example` is now denied too. A template filled in place is a common
way a real key reaches a repository, and a blocked write costs a human one line while a
leaked key does not.

### D8 — open findings from the Codex review, 2026-08-25

An adversarial review by `gpt-5.6-sol` at `xhigh` over the working tree. Six false claims
it found in the new README were corrected before commit; D7 above was found the same way.

**All eleven were fixed on 2026-08-25.** The table below is kept as the record of what was
wrong and why. Root-cause analysis first collapsed them into five causes plus one
standalone, and the fixes were made against the causes rather than the symptoms:

| Root cause | Findings it produced | The change |
|---|---|---|
| **A** — no canonical notion of path identity; every decision made lexically | 2, 9, D4 | One `canonical()` applied to **both sides** of every comparison. Deny runs on the union of lexical and canonical, allow on canonical only, so the deny predicate is provably monotone and nothing that blocked before can start allowing. |
| **B** — idempotence implemented as presence-detection, never reconciliation | 3, 10, D2 | The ownership marker becomes an *address*, not a flag: locate the owned region, diff it against desired, rewrite in place, return `changed` from a real before/after comparison. |
| **C** — `apply()` writes to a destination it has never inspected | 1, 6, 11 | One `inspect_destination()` precheck over every item before any byte lands, plus `O_NOFOLLOW` to close the TOCTOU gap between inspection and write. Refusal is all-or-nothing. |
| **D** — every component exits green on a narrower question than the one asked | 4, 5, 3b, D3 | No component may exit 0 on a question it did not ask. The probe gains a wiring phase and exit 3; the CLI refuses undecided input; the guard reports version drift. |
| **E** — the Bash parser models one command with one target | 8 | Segment-split, tokenise, per-verb destination extraction, unioned with the original regexes. |
| standalone | 7 | One token: `.claude/settings.json` → `.claude/settings*.json`. The glob mechanism from `e1d4ce1` already covered it. |

Test count went from 36 to **246**, green on Python 3.9.6 and 3.14.

Two things worth recording because they nearly went wrong:

- **Finding 7 could not land until the probe could instantiate an interior `*`.**
  `denied_attack_paths(".claude/settings*.json")` returned `['.claude/settings*.json']` — a
  literal path containing a star. Adding the pattern would have produced a probe that fired
  at nonsense, blocked trivially, passed, and never once attacked `settings.local.json`.
  That is D7 reproduced inside its own fix. The probe's glob expansion was fixed first, and
  a permanent generalised assertion now covers the class: for every entry in
  `BASELINE_DENIED`, `denied_attack_paths(entry)` must yield at least one metacharacter-free
  path that is not byte-identical to the entry.
- **Canonicalising naively would have turned the whole suite red while looking like a
  security win.** `tempfile` returns `/var/folders/...` and `realpath` returns
  `/private/var/folders/...`. Canonicalise the target but leave `find_repo_root` on
  `abspath` and every write resolves outside its own root, so the guard blocks everything.
  A control assertion now pins it, and the assertion checks that the two paths really do
  differ, so it cannot silently become vacuous if macOS changes.

Line numbers below refer to the state at the review.

| # | Severity | Finding |
|---|---|---|
| 1 | High | **Symlink escape on generation.** If `AGENTS.md` or `.claude/settings.json` is a symlink to an external file, `generate.apply()` follows it and truncates the target. Needs no-follow semantics and a canonical-path check that the destination stays under the repository. |
| 2 | High | **Symlink escape on enforcement.** `resolve()` uses `normpath`, not `realpath`, so an allowed path such as `src/guard-link -> ../.mir/guard.py` is approved and the guard is overwritten through the alias. Canonicalise existing components and add alias attacks to the probe. |
| 3 | High | **A stale tagged hook leaves structured writes unguarded while the probe passes.** Compounds [D2](#d2--merge_settings-silently-no-ops-on-a-matcher-change): the probe invokes `guard.py` directly, so it never checks that the registered matcher actually routes `Write`/`Edit` to it. Wiring verification is already planned in Workstream D; this raises its priority. |
| 4 | High | **A block-everything guard verifies clean.** `probe.py` gates only on leaks (`return 1 if report["leaks"] else 0`); false blocks are reported but do not affect status. Defensible as written — a too-tight policy is fail-safe — but it means positive controls do not gate the result, and the docs must not claim they do. README corrected. |
| 5 | High | **The CLI never confirms.** `cli.py` has no picker: `_answers_from_detection()` keeps the first framework per pillar and ignores `uncovered`. The interactive confirmation lives in the `/mir-init` skill, so running the CLI directly silently resolves conflicts it documents as requiring a decision. README corrected; the CLI should hard-stop on conflict unless `--answers` is given. |
| 6 | High | **Malformed `.claude/settings.json` is overwritten, not merged.** If `json.load()` rejects the file — truncated, or JSONC — generation treats it as absent and replaces every existing hook. Should abort without writing, and back up. |
| 7 | Med | **`.claude/` is not self-protected; only `.claude/settings.json` is.** `.claude/settings.local.json` is read by the host and is writable. `.claude/skills/` must stay writable for `mir init --install`, so this needs a pattern like `.claude/settings*.json` rather than denying the directory. |
| 8 | Med | **The Bash parser misses multi-target and multi-segment writes.** `tee allowed.txt .mir/guard.py` and `cp safe .git/config; echo done` both evade it — it checks one destination and one segment. Parse every segment and every destination, or block ambiguously. |
| 9 | Med | **`prune_project_skills()` deletes unrelated `mir-*` symlinks**, including a user's own or one from another checkout. Needs an ownership test — prune only links resolving beneath this checkout. |
| 10 | Med | **`CLAUDE.md` is overwritten wholesale.** `AGENTS.md` preserves a marked tail; `CLAUDE.md` does not, despite being documented as having room for repository notes. |
| 11 | Med | **Unowned `AGENTS.md`/`CLAUDE.md` get no refusal or backup** when `mir init` runs on a repository that already has them. |

Three corrections to this plan's own Workstream D design, from the same review:

- **Codex `writable_roots` is additive, not restrictive.** Under `workspace-write` the
  workspace stays writable and configured roots are *additional*. So it is an outer
  boundary, not a 1:1 mapping of `allowed_write_roots`, and hooks remain necessary for
  intra-repository allow/deny. The claim above that it maps "nearly 1:1" is wrong.
- **Returning `ask` on an unparseable Codex `apply_patch` fails open.** Codex treats
  `permissionDecision: "ask"` as unsupported and continues. Return `deny`, or exit 2.
- **Making `--protocol` required creates a fail-open upgrade window.** A running Claude
  session holds the v1 hook command with no `--protocol`; a v2 guard would exit 3, and
  only exit 2 blocks, so writes proceed until restart. The migration needs to be
  versioned and two-phase, and tested against the cached v1 command.

Also corrected: **Cursor can enforce, conditionally.** It loads hooks configured for
Claude Code and honours exit code 2, gated on *Include third-party Plugins, Skills, and
other configs*. So Cursor is not the pure advisory case this plan assumed — it is a
conditional one, which is arguably worse to document, because the harness is present and
inert rather than absent.

### D5 — the Python floor is 3.9, and nothing tests it

`/usr/bin/python3` on stock macOS is **3.9.6** (verified on this machine). `install.sh`
invokes bare `python3`; `generate.py` registers the hook as
`python3 "$CLAUDE_PROJECT_DIR/.mir/guard.py"`. Every macOS user without a pyenv or
homebrew Python runs the guard on 3.9.6.

There are **zero workflow files** in the repository. `init/test_init.py`'s 25 assertions
have never been run by anything but a human.

**Fix:** Workstream A's CI, with 3.9 as the floor on both Linux and macOS.

---

## Workstream A — release engineering

### Version source of truth: a root `VERSION` file

Rejected alternatives, on evidence:

- **`pyproject.toml`** — advertises pip-installability the repo does not have. There is
  no `init/__init__.py`; `cli.py` and `test_init.py` import via
  `sys.path.insert(0, HERE)` then `import catalog` as top-level modules. Reading TOML
  from bash needs `tomllib`, which is 3.11+, above the 3.9 floor.
- **`init/__init__.py`** — wrong home. `init/` is ~1,500 lines; `skills/` + `agents/` is
  ~16,000 and is 95% of the product. Putting the product version inside the CLI
  subdirectory inverts what this repo is.

`VERSION` is one line, no `v` prefix — the `v` belongs to the git tag. One `cat`, no
parser, works before `install.sh` has confirmed `python3` exists (which matters:
`validate_tree()` already has a documented path where python3 is absent and it
proceeds anyway).

`init/_version.py` is the only Python that parses it. `read_version()` never raises; a
missing `VERSION` returns a fallback rather than stopping `mir init` from generating a
harness. The version is metadata, not a precondition, and making a cosmetic file a hard
dependency of the security tooling would be a poor trade.

### `mir --version` and `install.sh --version`

`install.sh` **symlinks rather than copies**, so the installed version is a property of
the working tree, not of a release artifact. A bare `2.0.0` is a lie in exactly the case
where a bug report needs the truth — a user on `main`, three commits past the tag, with
local edits. Report `git describe --tags --always --dirty` alongside it.

Regression test needed: bare `mir --version` works despite `required=True` on the
subparser, because argparse's `version` action calls `parser.exit()` before the
required-argument check runs. It works by ordering, not by design.

### Stamping generated artifacts

**`.mir/manifest.json`: yes.** Add `generated_by_version` beside the existing
`generated_by`. The generated `guard.py` and `probe.py` are copies frozen at generation
time; `generated_at` tells you *when* a guard was frozen, not *whether* it contains a
given fix. Answering that from a timestamp requires knowing the repo's commit dates.

`MANIFEST_VERSION` is a *schema* version and will diverge from the *producer* version —
v2 can rewrite `_SHELL_WRITE_PATTERNS` (a real security change) without touching the
schema. Two independent facts need two fields.

**Thin `AGENTS.md`: yes, but only inside the HTML comment.** The file's own text says
never to paste content that is billed on every request. Extend the existing ownership
marker; keep `MARK` as the stable version-free prefix so marker detection keeps
matching.

**Not `skills/*/SKILL.md` frontmatter.** That is 46 files per release and would require
a new `FRONTMATTER_KEYS` entry, instantly failing every third-party skill written
against the published v1 contract — after which `install.sh` refuses to install.

### CI

`.github/workflows/ci.yml` with jobs: `validate`, `test` (matrix: 3.9/3.11/3.13 on
ubuntu, 3.9/3.13 on macOS), `test-with-pyyaml`, `install-smoke` (into a throwaway
`$HOME` via `CLAUDE_HOME`/`CODEX_HOME`/`GEMINI_HOME`), and `init-e2e`.

Windows is excluded deliberately, not forgotten: `install.sh` is bash,
`generate.py` registers `python3`, and `install_project_skills()` uses `os.symlink`,
which needs Developer Mode or elevation on Windows.

Pin every action to a full SHA. Shipping a floating tag from a repository whose README
lists action pinning as covered guidance is the first thing a skeptical reader finds.

### Should warnings fail the build?

**No blanket rule. Freeze the count with a ratchet.** The 7 warnings are two species:

- **3× `REF002` must never fail.** `.mir-planned` exists precisely so a roadmap reads as
  a warning. Failing on it creates a perverse incentive: the way to get green is to
  delete the `.mir-planned` line, which promotes the reference to `REF001` — so the real
  way to get green is to delete the reference from the body. That is the silent
  broken-chain defect, reintroduced by CI policy.
- **4× `CTX001` is debt, and unfailed debt grows.** Three to eight lines over
  threshold. Failing the build makes the first v2 PR a forced refactor of four unrelated
  skills for a rounding error; letting it float means the next skill lands at 500 lines
  and nobody notices.

So: `errors == 0` hard, `warnings <= 7` ratchet, implemented in `validate.py` as
`--max-warnings` returning **exit 3** — not 1, because `install.sh` treats any nonzero as
refusal-to-install and a budget overrun must not refuse installs on user machines.
Document exit 3 in the README table and `EXTENDING.md`.

Residual hole worth naming and not fixing: a count-only budget lets a trimmed `CTX001`
be swapped for a fresh `REF004` under the same number. Per-code budgets are
over-engineering at 46 skills. Put the current seven in a comment beside the number so a
reviewer sees the swap in the diff.

### CHANGELOG

Keep a Changelog 1.1.0 plus two additions:

- **`### Breaking`** — must be non-empty iff the version is MAJOR. This is what makes
  the semver claim checkable from the file, and it is the mechanism that decides the
  version number.
- **`### Upgrading`** — free-form, per release. The prune instructions live here so a
  user reading "what changed" cannot miss "and here is what to do about it."

Raw `git log` is rejected: this repo's commit bodies are 25-line design essays aimed at a
maintainer. The changelog's reader is someone with a v1 install asking "does upgrading
break me" — a question commits never address, because they are written before the
upgrade path is known.

### `mir doctor`

Reports version drift between a project's frozen `.mir/` copies and the installed repo,
dangling global symlinks, and a stale hook matcher; then runs the project's own probe.
Exits nonzero on drift so a team can put it in their own CI.

This is the consumer that makes `generated_by_version` earn its place, and it fits the
repo's stated philosophy directly: `validate.py` exists because a convention with no
check behind it is not a rule, and "re-run `mir init` after upgrading" is currently
exactly such a convention.

---

## Workstream B — visual documentation

Format decision: **Mermaid in fenced Markdown**. GitHub renders it natively, no build
step, diffable.

### The drift problem is already real

Not hypothetical. Verified today:

| Source | Claim |
|---|---|
| `README.md:142` | "45 directories under `skills/`" … "6 pillars" |
| `EXTENDING.md:41` | "45 skills: 6 pillars, 13 middle tiers, 26 leaf modules" |

**Fixed 2026-08-25.** The root cause was not a stale number: `mir-init` was missing from the
README's inventory *tables*, so the total had no row to rest on and correcting the headline
alone would have left it unsupported. The table now carries an `### Init — 1 skill` section,
every per-section heading was re-summed against `ls skills/`, and both documents state 46
skills / 7 pillars / 17 tiers / 22 modules.

Worth recording, because it explains why the two documents disagreed in a way that looked
like carelessness and was not: `EXTENDING.md`'s "13 middle tiers, 26 leaf modules" was
internally consistent with its own matrix, which classifies by *domain sense*.
`validate.py` classifies by *hyphen count*, so a two-tier pillar's direct leaf
(`mir-database-postgres`, `mir-mobile-ios`) counts as a tier rather than a module. Two
defensible schemes, no note saying which was in use. Both files now say which one they mean.
| `validate.py` | **46 skills — 7 pillars, 17 tiers, 22 modules** |

Three sources, three different numbers, none matching. Also: `README.md` contains **zero
mentions** of `mir init`, `mir-init`, or `--scope=pillars` — the flagship v1.0.0 feature
is invisible in the README. And `assets/logo.png` is 5.5 MB at 2272×1884, referenced
nowhere since `d9a9609` rewrote the README.

A hand-written Mermaid tree would join that list within one commit. So the diagrams that
reflect the tree are **generated**, and the generation is enforced.

### Inventory: 8 diagrams, 4 stable and 4 derived

| # | Diagram | Type | Class |
|---|---|---|---|
| D1 | The eight gates | `flowchart LR` | STABLE |
| D2 | Pillar map (overview) | `flowchart LR` | DERIVED |
| D3 | Per-pillar tree shards (×8) | `flowchart LR` | DERIVED |
| D4 | Coarse-to-fine chain, worked example | `flowchart LR` | DERIVED |
| D5 | Progressive disclosure + token cost | `flowchart TD` | DERIVED |
| D6 | `mir init` flow | `sequenceDiagram` | STABLE |
| D7 | Trust boundary / write policy | `flowchart TD` | STABLE |
| D8 | Placement decision tree | `flowchart TD` | STABLE |

Rejected: a single 46-node tree (past every readability ceiling), a cross-agent install
matrix (a 12-edge bipartite graph carries less than the existing table, and the target
paths are long strings that wreck node sizing), and a standalone validation-gate flow
(fold the interesting arrow into D6).

### `docs/gen_diagrams.py`

Stdlib-only, no network, no clock. Modes: `--list`, `--stdout <id>`, `--check` (exit 1
with a unified diff on drift), `--write` (the only mutating mode).

Imports `init/catalog.py` — `scan_skills`, `chain_of`, `load_planned`, `load_overlay`,
`resolve` — and reimplements none of it. Nothing hard-codes a slug except the D4
worked example, which is a named constant asserted to exist at generation time.

Marker format follows the ownership idiom `init/generate.py` already established:

```html
<!-- mir:gen:begin id=tree-frontend src=docs/gen_diagrams.py -->
<!-- mir:gen:end id=tree-frontend -->
```

**The single most important implementation constraint: no timestamp, no hash, no version
string inside a managed block.** `init/generate.py` stamps `generated_at`; the diagram
generator must not, or `--check` fails on every run for the wrong reason. Deterministic
ordering everywhere; byte-exact comparison.

Node IDs must be sanitized — slugs contain hyphens, and `mir-backend --> mir-backend-python`
is ambiguous to the flowchart parser. Emit `p_mir_backend["mir-backend"]` with the slug
only in the quoted label.

Every generated block ships three things from one data structure, so they cannot
disagree: the Mermaid block with `accTitle`/`accDescr`, a `<details>` text-version list,
and a link list. The text version is not optional — Mermaid conveys no node
relationships to assistive tech, and the README is also read on mirrors, in `less`, and
in agent context windows where Mermaid never renders. Node links inside diagrams do not
work on GitHub (`securityLevel: strict` plus CSP), which is why the link list exists.

Sharding is enforced by the generator, not the author: `SOFT_NODES = 16` warns,
`MAX_NODES = 24` refuses. The backend runtime grouping is the only hand-authored datum;
a new runtime that lands in no shard makes the generator **fail loudly** rather than
silently omitting it.

### `validate.py` integration

New `DIA` family, fitting the existing 3-letter/3-digit scheme:

| Code | Level | Check |
|---|---|---|
| `DIA001` | Error | A committed block does not match what the generator produces from the current tree. |
| `DIA002` | Error | Marker malformed, duplicated, or unknown id. |
| `DIA003` | Warning | Generated diagram exceeds `SOFT_NODES`. |
| `DIA004` | Warning | A generator id is embedded in no document — the diagram will never be seen. |

The sweep goes beside the existing `REF004`/`PLN001` block. Load the generator with
`importlib`, not `subprocess` — in-process, fast, stdlib-only — wrapped so a deleted
generator is loud.

**The enforcement chain comes free.** `install.sh` already runs `validate.py --quiet` and
refuses to install on any error. Making `DIA001` an error means a stale diagram blocks
installation, with zero new plumbing. That is the strongest argument for putting the
check in `validate.py` rather than a standalone script.

### Light/dark rules

Three rules, in priority order. These are where this kind of work usually ships broken:

- **A — never set `theme:`.** GitHub auto-detects the reader's theme and recolors only
  if you do not specify one. Setting it guarantees one mode is unreadable.
- **B — for unstyled nodes and all edges, style nothing.** Auto-theming handles them
  correctly in both modes.
- **C — when a node must carry meaning by color, set `fill`, `stroke` and `color`
  together, with a mid-tone fill.** Setting `fill` without `color` is the specific bug
  that yields light-on-light text. Hex only; Mermaid rejects color names.

Design for **Mermaid 10.x, not 11.x** — GitHub does not document its pinned version and
consistently trails upstream. Verify once with a block containing the single word `info`
before committing any diagram. No `-beta` types, no YAML frontmatter config, no
backticks in labels (v11 treats labels as Markdown, v10 does not).

Verification is a named task, not a footnote: both themes, plus the two documented
environment bugs (Dark Reader forces the light theme; GitHub's "sync with system" fails
to detect `prefers-color-scheme` reliably).

### README restructure

Measured today: 433 lines, 35,924 bytes, 34 headings, 134 table rows, 0 images. The
density problem is the table rows, not the headings.

Target ~18 KB. Move the ~110-row inventory to a new `docs/skill-tree.md` (−14 KB), add
8 diagrams (+6 KB), add a quickstart (a reader currently must reach line 292 to learn
how to install) and a `mir init` section (currently undocumented). Logo: downscale to
480px light/dark variants under 60 KB each, embedded with `<picture>` +
`prefers-color-scheme`.

No SVG diagrams, and no committed PNG renders of Mermaid diagrams — that reintroduces
the two-sources-of-truth problem the generator exists to kill.

---

## Workstream C — the six deferred skills

Takes the tree to 52 skills. Largest workstream by a wide margin.

### The contract, extracted from the code

| Constraint | Source | Value |
|---|---|---|
| Body line ceiling (`CTX001`) | `validate.py:49` | **380 lines after the closing `---`** |
| Description floor/ceiling (`RTR003`/`RTR004`) | `validate.py:50-51` | 200 / 2400 chars |
| Description hard cap | `init/test_init.py:161` | **1536 chars**, with `TRIGGER` *and* `SKIP` inside `desc[:1536]` |
| Observed sibling band | measured across 46 | 1049–1446, mean ≈ 1310 |
| Leaf `allowed-tools` | verified on 4 leaves | Read · Write · Edit · Bash · Glob · Grep |

Target 300–340 body lines for a tier, 220–280 for a module. `mir-frontend-react` at 375
is a warning away from tripping — do not use it as the size template; use `mir-cloud`
(315) and `mir-frontend-react-next` (206).

**Write `TRIGGER` and `SKIP` first, then fill coverage up to the remaining budget.** The
failure mode to prevent is a coverage clause that grows during authoring and pushes
`SKIP` past 1536. Impose a 1,450-char ceiling — 86 chars of headroom under the cap.

### Recommended order

1. **`mir-cloud-aws`** — clears the only live `REF002`, largest install base, and it is
   the template the other three copy, so the shared skeleton gets designed and reviewed
   once. Land defect **D1** in the same commit.
2. **`mir-frontend-angular`** — highest user-visible value in the workstream. It is the
   only stack where the kit currently admits it has nothing: `mir-frontend:92` literally
   says "run this pillar alone and say so in the design." An Angular user gets 1 of 3
   layers today; everyone else gets 2 or 3. It is also a tier, and tiers gate future
   modules.
3. **`mir-cloud-cloudflare`** — cheapest remaining; the pillar already carries the
   isolate/DO/R2/egress facts, and Gate 4 ranks Cloudflare #1 in three of seven workload
   classes and then cannot hand off.
4. **`mir-cloud-gcp`** — richest actionable IAM content (the service-account escalation
   graph and the Cloud Build path).
5. **`mir-cloud-azure`** — largest surface, most in-flux details, lowest actionable
   yield despite the highest raw CVE count.
6. **`mir-frontend-react-remix`** — last, and the primary slip candidate.

### What slips, and what does not

**Slip `mir-frontend-react-remix` first.** An RR7/8 user already gets 2 of 3 layers —
`mir-frontend-react:19` explicitly states its footguns apply to React Router 7 Framework
Mode. They lack only framework mechanics, against the Angular user's 1 of 3. RSC is also
still unstable in React Router v8, so writing the module now means either omitting the
surface everyone asks about or documenting an unstable API in a kit premised on verified
dated fact.

**Do not slip `mir-frontend-angular` under any circumstances.** It is the only item that
closes a stated, self-documented coverage hole rather than deepening existing coverage.

Ending at **50 skills with 2 honest planned entries** is strictly better than 52 with
two thin ones.

### The cloud modules are a set

`mir-cloud` Gate 5 instructs the model to load the provider module. Ship two of four and
that instruction resolves for two providers and **silently no-ops for the other two**.

This does not mean all four must ship together. It means: whenever the set is partial,
Gate 5 must be edited to name only the modules that exist, plus an explicit "not
written — run the pillar alone and record in the design that module-level mechanics were
unavailable" sentence for the rest, copying the pattern `mir-frontend:233` already uses.
That edit is non-optional and belongs in the same commit as each partial landing.

### The hidden cost: ~22 stale sentences across 11 files

`validate.py` catches a broken chain, a missing reference, a missing `SKIP`. It **cannot
catch a stale sentence**. `mir-frontend:233` says "Planned, not written — do not try to
load these" about skills that would then exist. An actively wrong instruction in a
loaded skill body is worse than a missing one, because the agent obeys it.

Known sites: `mir-cloud/SKILL.md` (3), `mir-frontend/SKILL.md` (4),
`mir-frontend/references/rendering-model-map.md` (5),
`mir-frontend/references/constraint-catalog.md` (1), the three sibling tier descriptions,
`mir-frontend-react-next/SKILL.md` (1), `mir-devsecops/SKILL.md` (1 description
amendment — measure it, it lands near 1,441 chars), plus `README.md`, `EXTENDING.md`,
`AGENTS.md`, and `init/overlay.json`.

`init/overlay.json` is the only hand-maintained init data — `catalog.py` derives the
picker from disk, so the six options appear automatically, but with ugly labels. Note
that the correct Remix detection signal is `@react-router/dev` (the Vite plugin), **not**
`react-router`, which is a transitive dependency of many React apps.

### `.mir-planned` mechanics

- `PLN001` fires the moment a directory named by a planned slug exists. Warnings do not
  fail `install.sh`, which is precisely why it will be forgotten. **Rule: the
  `.mir-planned` deletion goes in the same commit as the directory.**
- The dangerous direction is the opposite one. Removing a slug *before* its directory
  exists flips `REF002` → **`REF001`** and `CHN002` → **`CHN001`**, both errors, and
  `install.sh` then refuses to install for everyone. Never batch-clean the file.

### Research cost, stated honestly

The house style does not transfer to the cloud modules. `mir-cloud` cites exactly one
advisory, and that is a supply-chain CVE, not a cloud CVE — because cloud-provider
vulnerabilities are overwhelmingly mitigated server-side with no customer action.
Citing those produces a table an engineer can do nothing with.

What belongs in a cloud module instead: named escalation research, provider
post-incident reports, dated posture telemetry, IaC major-version breaks, and dated
service limits and prices — each with a retrieval date and a verify-before-quote list,
the way `mir-cloud`'s Provenance section already does it.

Estimated: ~32–58 new advisory identifiers (~22–32 genuinely actionable) and **~200
dated facts**. The recurring cost matters more than the one-time cost: those 200 dated
facts are the highest-decay content in the repository, and the four cloud modules
roughly **triple** the currency-pass surface for that pillar. Budget it as an ongoing
tax.

Total: ~5,000–6,500 lines across 6 `SKILL.md` files and 14 new `references/*.md`.

---

## Workstream D — cross-agent `mir init`

This is the release that earns `2.0.0`.

### What is already done, and what is not

`install.sh` has supported `--tool=claude|cursor|codex|antigravity|all` since the initial
commit, and `--scope=pillars` since `863af85`. The gap is depth:

| Surface | Claude Code | Codex | Antigravity |
|---|---|---|---|
| Skills installed | symlinked | manual (a printed NOTE) | symlinked |
| `AGENTS.md` | yes | yes | yes |
| Reviewer sub-agents | `~/.claude/agents` | no | no |
| **`mir init` output** | full | **nothing** | **nothing** |
| Enforced write policy | PreToolUse + probe | **nothing emitted** | **nothing emitted** |

### Correction to an earlier assumption

An early framing of this workstream assumed Codex and Antigravity might not be able to
enforce a write policy at all. Research says otherwise:

- **Codex** — `.codex/hooks.json`, the same event → matcher → handler shape as Claude,
  the same stdin field names, block via exit 2 + stderr. It intercepts `apply_patch`,
  Bash and MCP tools, which is *broader* than Claude's Bash-regex weakness. Plus an
  OS-enforced sandbox (`sandbox_mode`, `writable_roots`) that maps nearly 1:1 onto
  `manifest.policy.allowed_write_roots`.
- **Antigravity** — `PreToolUse` fires on **every** tool, file writes included; verified
  against the shipped binaries, not the docs (see
  [Q2](#q2--does-antigravitys-pretooluse-fire-on-file-write-tools--resolved-yes)).
  Blocking is **stdout JSON** (`{"decision":"deny"}`) with exit 0, not an exit code.
  Critically, its harness **fails open** on hook error, so mir's adapter must be
  fail-closed by construction — the opposite posture from the Claude adapter.
- **Cursor** — the genuine advisory-only case. No documented pre-tool hook.

### Design

`init/targets/` package with `base.py`, `claude.py`, `codex.py`, `antigravity.py`,
`cursor.py`.

`Capability` is deliberately not a boolean:

```python
@dataclass(frozen=True)
class Capability:
    level: str        # "enforced" | "advisory" | "unverified" | "none"
    mechanism: str
    source: str       # the doc URL the claim came from
    caveats: list[str]
```

Every target must supply all four keys — `skills`, `subagents`, `always_on_context`,
`write_policy_enforcement`. A `KeyError` on a missing key is intentional: adding a fifth
target forces the author to *state* its enforcement level rather than inherit silence.
This is the mechanical version of the repo's honesty rule.

The resolved target list is written into `.mir/manifest.json` under a new `targets`
block, so `probe.py` reads the declared set from the manifest rather than from flags — a
target cannot be quietly dropped from verification by re-running the probe with narrower
arguments.

### The guard becomes multi-protocol

`decide()`, `resolve()`, `_is_under()`, `find_repo_root()`, `load_manifest()` stay
exactly as they are; the policy engine is already target-neutral and correct. What is
added is a `--protocol` dispatch over event parsing and verdict emission.

**A missing or unknown `--protocol` must be a hard error (exit 3), never a default to
`claude`.** Defaulting is precisely how a mis-wired Codex hook runs the Claude adapter,
finds no recognizable fields, extracts zero write targets, and allows everything while
reporting clean.

Two more must-nots:

- Codex `apply_patch` whose body cannot be parsed must return `ask`, **never `ALLOW`**.
  An unparseable write that reads as allowed is exactly the laundering this repo exists
  to prevent.
- On Antigravity, `BLOCK` is exit 0 + stdout JSON. So the probe must assert on **parsed
  stdout** and treat "exit 0 with empty or unparseable stdout" as `GUARD-ERROR`, never
  `ALLOW`. Without that, a crashed adapter reads as a clean allow-path run. Note this
  interacts with the existing `probe.py:44-49` comment about a missing guard making
  Python exit 2, which collides with `BLOCK`.

### `.mir/COVERAGE.md` — the honesty artifact

Opens with a verdict block, not a table:

```
ENFORCED   (guard decides, host hook registered): claude
UNVERIFIED (hook file emitted, host invocation not proven): codex, antigravity
ADVISORY ONLY — the manifest is NOT enforced for: cursor
```

Then one row per target × capability: mechanism, the file emitted, level, what the probe
proved, **what it did not prove**, and the manual command to confirm.

### `probe.py` restructure

Three phases with distinct semantics, because the current probe conflates "the guard
decides correctly" with "the guard is wired," and has no notion of a target at all:

1. **Policy** — today's manifest-derived attacks, replayed per protocol.
2. **Wiring** — the registration file exists, parses, and contains a mir-tagged entry
   pointing at `.mir/guard.py` with the right `--protocol`. **A target declared in the
   manifest with no wiring file is a FAIL, not a skip.**
3. **Live** — not automatable; emitted as a manual checklist, counted in an `unverified`
   bucket. `--require-live` makes any unverified target exit nonzero for strict CI.

### Self-protection gap this opens

`schema.BASELINE_DENIED` must gain repo-local `.codex` and `.agents`, plus `~/.gemini`.
Without it, `--target codex` hands the agent a `.codex/hooks.json` it can rewrite to
unregister its own guard — the exact hole `.claude/settings.json` is already denied to
close.

Denying all of `.agents` also denies `.agents/skills/`, where project skills would be
linked. Recommendation: **deny `.agents` wholesale.** The generator runs outside the
agent's tool loop, so `mir` can still write there; the agent has no legitimate reason to
edit skill definitions mid-task.

### `agents_export.py`

The reviewer agents in `agents/*.md` stay the single source of truth; a new
`init/agents_export.py` parses their frontmatter and emits per-target files. It must not
be a checked-in parallel catalog of `agents/codex/*.toml` — that violates the
derive-from-frontmatter constraint the same way a parallel skill catalog would.

Conversion losses go in a module-level `LOSSY_FIELDS` constant and must surface in
`COVERAGE.md`. The known one: Codex sub-agent TOML has no `tools:` equivalent, so the
read-only restriction the reviewers currently declare is a real capability loss.

### `AGENTS.md` stays thin

One shared file across all targets — Codex, Antigravity and Cursor all read root
`AGENTS.md`. Only the write-policy paragraph changes (~3 lines). `generate.py:56-60`
currently states unconditionally that writes are "enforced by `.mir/guard.py` (a
PreToolUse hook registered in `.claude/settings.json`)" — which becomes a lie the moment
anyone runs `--target codex`. It must name the enforcing targets and, when any target is
advisory, say so in the same breath. All per-tool depth lives in `COVERAGE.md`.

Test the result against **12,000 chars** — Antigravity's per-rule-file cap is now the
tightest host limit, tighter than the 32 KiB Codex figure `EXTENDING.md` cites.

---

## Sequencing

**Phase 0 — today's tree, shipped as `1.0.1`**

Fix D1–D4, add CI, get green **before** any v2 content lands. CI that first runs on the
same PR that adds v2 content cannot tell you whether it caught a v2 bug or an existing
one.

**Phase 1 — `1.1.0`**

| Order | Work |
|---|---|
| 1 | `VERSION`, `_version.py`, `mir --version`, `install.sh --version`, `--max-warnings` + exit 3 |
| 2 | `CHANGELOG.md` — 1.0.0 backfilled, skeleton for the next release |
| 3 | `install.sh --prune` / `--prune-only` / `--dry-run` + install-smoke CI |
| 4 | Workstream B — diagrams, generator, `DIA` codes, README restructure |
| 5 | Workstream C — the six skills, in the order above, each with its cross-reference edits |
| 6 | `RELEASING.md`, `.github/workflows/release.yml` |

**Phase 2 — `2.0.0`**

Workstream D, with `MANIFEST_VERSION = 2` as the stated breaking change. If Phase 2 also
renames any slug, `install.sh --prune` and `mir doctor` stop being nice-to-haves and
become **release blockers** — you cannot rename slugs without shipping a way to clean up
the symlinks the old names left behind.

---

## Open questions

Two empirical unknowns block parts of Workstream D. Both are being verified against the
running install on this machine rather than against documentation, because doc research
on the first one already produced an answer the filesystem contradicted.

### Q1 — Antigravity's user-skill path — **RESOLVED: install.sh was wrong**

See defect [D6](#d6--installsh-linked-antigravity-skills-into-a-directory-nothing-reads).
Fixed 2026-08-25. The correct global path is `~/.gemini/config/skills`.

### Q2 — does Antigravity's `PreToolUse` fire on file-write tools? — **RESOLVED: yes**

**CONFIRMED-YES**, from binary evidence in two shipped builds (`~/.local/bin/agy`,
2026-08-20; the IDE's `language_server_macos_arm`, 2026-08-13).

`hooks.(*HookMixinWrapper).GetToolConverters` wraps **every** tool converter
unconditionally — no string comparison, no `run_command` reference, no skip branch
anywhere in the function. `hooks.(*PreToolHookConverter).ToolCallToCortexStep` calls
`applyPreToolHooks` on every complete (non-partial) tool call. The write converters are
ordinary peers of `run_command` in a single flat constructor table:

```
tools.NewMultiReplaceFileContentToolConverter
tools.NewRunCommandToolConverter
tools.NewSingleReplaceFileContentToolConverter
tools.NewWriteToFileToolConverter
```

Matching is on the model-facing tool-call name, compiled as `^(?:<matcher>)$`, with `""`
and `*` short-circuiting to match-all. `run_command` was only ever the documentation's
example, never the implementation's scope.

So Antigravity's `write_policy_enforcement` is **enforced**, not advisory. Denial is a
real gate: `hooks.PreToolHookDeniedError` propagates out of `ToolCallToCortexStep`, so
the call never becomes an executable step.

#### The finding that matters more than the answer

**Antigravity's hook harness fails OPEN.** Inside `applyPreToolHooks`, a failed hook is
logged as `pre-tool hook failed: %v` and control branches back into the loop — it logs
and continues. A malformed matcher logs `Invalid matcher regex %q: %v` and returns
false. **A crashed, timed-out, or malformed guard does not block the write.**

Three constraints this puts on Workstream D, without which the "enforced" claim is
overstated:

1. **The guard must be fail-closed by construction, because the host is fail-open.** No
   unhandled exceptions, no dependency on an interpreter that may be missing, a
   top-level catch that emits `{"decision":"deny"}`, and a comfortable margin under the
   30 s default timeout. `test_init.py` should assert the guard still emits deny-JSON
   when its own internals throw. Note this is the opposite posture from the Claude
   adapter, where `guard.py` deliberately fails open — the two hosts need different
   defaults, and that difference must be explicit in the target definition rather than
   inherited by accident.
2. **Use `"matcher": "*"`, not an enumerated list.** The matcher is anchored, and the
   write-capable surface is wider than the three obvious tools — the binary also ships
   `sed_file`, `notebook_edit`, a `delete_file` proto field,
   `CORTEX_STEP_TYPE_DELETE_DIRECTORY`/`_MOVE`, knowledge-directory write converters,
   and arbitrary MCP tools via `call_mcp_tool`. Enumerating three names is a hole. Match
   everything and decide inside the guard on the presence of `TargetFile` /
   `CommandLine`.
3. **Beware `overwrite`.** A `PreToolUse` hook can rewrite `TargetFile` before
   execution. Hooks from multiple sources merge and run sequentially, so another hook
   could move a write after mir's guard approved the original path.

Corrected config location: global hooks live at **`~/.gemini/config/hooks.json`**, not
`~/.gemini/antigravity-cli/hooks.json` — independent corroboration of the same
`~/.gemini/config/` migration that [D6](#d6--installsh-linked-antigravity-skills-into-a-directory-nothing-reads)
turned on. Workspace hooks are `<workspace>/.agents/hooks.json`. The `command` runs via
`sh -c` with cwd set to the directory containing `hooks.json`.

#### Still unresolved

- Whether a hook **timeout** takes the same fail-open path as an error. Assume it does.
- Precedence when multiple hooks match: whether a later `allow` can override an earlier
  `deny`, or whether the first `deny` short-circuits. Matters if the user has other
  hooks installed.
- Whether subagent tool calls route through the same wrapper. `CustomAgentMixin` and
  `DeclarativeAgentMixin` have their own `GetToolConverters`; they need the same wrap at
  their construction site to be covered. Worth a targeted follow-up.
- Version drift. The dispatch architecture looks stable and generic, but the shipped
  `hooks.md` is already out of sync with its own binary on the tool-name derivation
  rule, so treat any doc claim here as weaker than the binary.

A manual 3-phase runtime test procedure (fire / deny / fail-open) is available and takes
about five minutes in a throwaway directory. It was not run, because it requires driving
the IDE or CLI interactively.

### Lower-priority unknowns, carried from research

- Whether Codex requires per-hook trust approval before running a repo-local
  `.codex/hooks.json`. Same class of caveat as Claude Code's session-snapshot problem
  that `cli.py:119-121` already prints.
- The default of Codex `[features] hooks`. Sources conflict. Emit the enabling snippet
  and say so.
- Whether Antigravity permission rules are stored in any committable file. Assume UI-only
  until proven otherwise; do not emit one.
- Whether the Antigravity IDE and the `agy` CLI share a skills directory.
