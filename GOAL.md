# GOAL — Make It Right v2.0.0

This is the definition of DONE. Every line under "Acceptance criteria" is a box ticked by
running a command and reading its output, not by someone's judgement. Where a criterion
genuinely cannot be automated, it says so, names the procedure, and names who runs it —
because a criterion that reads as checkable and is not is the same defect this repository
exists to catch, moved into its own checklist.

The reasoning lives in `docs/v2-plan.md`. This file does not repeat it; it references the
section and states the check.

Baseline measured at `2061bbf`, before any v2 work:

```
46 skills  (7 pillars, 17 tiers, 22 modules)  · 6 reviewers · 6 planned
0 errors, 7 warnings                          (./validate.py, exit 0)
348 passed, 0 failed                          (/usr/bin/python3 — 3.9.6)
348 passed, 0 failed                          (python3 — 3.14.6)
```

---

## A. The goal

**v2.0.0 makes `mir init` emit a real, verified write-policy harness for Claude Code,
Codex and Antigravity — with Cursor documented as conditional rather than supported —
carrying `schema.MANIFEST_VERSION` 1 → 2 as its stated breaking change, and ships the
visual documentation, the deferred skills, and the README that describe what the tree
actually does.**

**v2.0.0 is NOT** a rename or removal of any existing skill slug, a generative fallback
for uncovered stacks, a Windows port, or a packaging release. Every one of those is a
second migration surface on top of the manifest bump, and one breaking change per major
is the most a user can be asked to absorb at once.

---

## B. Acceptance criteria

`$P` is a throwaway project directory; `$M` is the make-it-right checkout.

### B1 — Cross-agent `mir init`

Design: `docs/v2-plan.md`, "Workstream D".

- [ ] **B1.1** `init/targets/` contains `base.py`, `claude.py`, `codex.py`,
      `antigravity.py`, `cursor.py`, and every target supplies all four `Capability` keys
      (`skills`, `subagents`, `always_on_context`, `write_policy_enforcement`), each with a
      `level` in `{enforced, advisory, unverified, none}` and a non-empty `mechanism` and
      `source`. **The `KeyError` is the test** — a fifth target added later must *state*
      its enforcement level rather than inherit silence.

- [ ] **B1.2** `--target codex` writes `.codex/hooks.json`; `--target antigravity` writes
      `.agents/hooks.json`; `--target claude` writes `.claude/settings.json`.
      `--target cursor` writes **no** enforcement file and names the
      *Include third-party Plugins, Skills, and other configs* toggle in its output.
      Emitting a Cursor-specific file nothing reads would be defect D6 repeated.

- [ ] **B1.3** The manifest carries a `targets` block and the probe reads the declared set
      from the manifest, not from flags. A declared target whose wiring file is deleted
      makes the probe **exit 1**, not skip.

- [ ] **B1.4** A missing or unknown `--protocol` exits 3 and enforces nothing silently.
      The cached-v1-command case is a named test: a running Claude session holds the v1
      command with no `--protocol`, and only exit 2 blocks, so exit 3 there is fail-open
      until restart. `CHANGELOG.md`'s 2.0.0 `### Upgrading` names `--protocol`.

- [ ] **B1.5** Verdict emission matches each host's own channel. Antigravity BLOCK is
      **exit 0 with parseable deny-JSON on stdout**; the probe asserts on parsed stdout and
      treats "exit 0 with empty or unparseable stdout" as `GUARD-ERROR`, never `ALLOW`.
      The Antigravity adapter is fail-closed by construction because the host is fail-open.
      An unparseable Codex `apply_patch` returns `deny` or exit 2 — never `ask`, which
      Codex treats as unsupported and continues past.

- [ ] **B1.6** `schema.BASELINE_DENIED` gains repo-local `.codex` and `.agents`, plus
      `~/.gemini`. The two repo-local entries produce rows the probe labels `rule`, not
      `deny-by-default` — a `~/`-rooted entry cannot isolate its rule, so it must not be
      counted as coverage. `.agents` is denied wholesale and `mir init` can still write
      `.agents/hooks.json`, because the generator runs outside the agent's tool loop.

- [ ] **B1.7** `.mir/COVERAGE.md` exists and opens with a verdict block, not a table.
      Every target × capability row carries a non-empty **"what the probe did NOT prove"**
      cell for every `unverified` target. `agents_export.py` derives from `agents/*.md`
      frontmatter, its `LOSSY_FIELDS` surfaces in `COVERAGE.md` (Codex sub-agent TOML has
      no `tools:` equivalent, so the reviewers' read-only restriction is a real loss), and
      there is no checked-in `agents/codex/` parallel catalog.

- [ ] **B1.8** The generated `AGENTS.md` stops claiming Claude Code unconditionally.
      `generate.py`'s "enforced by `.mir/guard.py` (a PreToolUse hook registered in
      `.claude/settings.json`)" names the enforcing targets instead, and says so in the
      same breath when any target is advisory. It stays under **12,000 chars** —
      Antigravity's per-rule-file cap, tighter than the Codex figure `EXTENDING.md` cites.

- [ ] **B1.9** `schema.MANIFEST_VERSION == 2`, generated manifests carry
      `"mir_manifest_version": 2`, and `CHANGELOG.md`'s 2.0.0 `### Breaking` leads with the
      manifest bump — not "lots of new skills".

- [ ] **B1.10 — NOT automatable.** Four claims no command in this repository can check.
      Listed so a green CI run is not read as covering them.

| Claim | Procedure | Who |
|---|---|---|
| Antigravity actually invokes `.agents/hooks.json` and honours the deny | The 3-phase fire / deny / fail-open test in `docs/v2-plan.md` Q2, ~5 min in a throwaway dir | maintainer |
| Codex requires (or not) per-hook trust approval for a repo-local `.codex/hooks.json` | Run `codex` in a repo with a generated harness, attempt a denied write, record whether a trust prompt appeared | maintainer |
| Cursor enforces only with third-party configs enabled | Toggle off → denied write succeeds; toggle on → exit-2 block. Record both | maintainer |
| Antigravity subagent tool calls route through the same hook wrapper | Binary inspection follow-up per Q2 "Still unresolved" | maintainer |

Until each is run the target's `write_policy_enforcement.level` stays `unverified` and
`COVERAGE.md` says so. `probe.py --require-live` exits nonzero while any target is
unverified, so a team needing the stronger claim can gate on it.

### B2 — Mermaid visual documentation

Design: `docs/v2-plan.md`, "Workstream B".

- [ ] `docs/gen_diagrams.py` exists, is stdlib-only, supports `--list` / `--stdout` /
      `--check` / `--write`, imports `init/catalog.py` and reimplements none of it.
- [ ] **No timestamp, hash, or version string inside a managed block.** Two consecutive
      `--check` runs are byte-identical. `init/generate.py` stamps `generated_at`; a
      generator copying that habit fails every run for the wrong reason and the whole
      mechanism becomes noise.
- [ ] `validate.py` carries `DIA001`–`DIA004`, `DIA001` is an **error**, so a stale
      diagram blocks `install.sh` through the gate that already exists.
- [ ] Sharding is enforced by the generator: `SOFT_NODES = 16` warns, `MAX_NODES = 24`
      refuses. A new runtime in no shard fails loudly rather than being omitted.
- [ ] Every managed block ships three things from one data structure: the `mermaid` block
      with `accTitle`/`accDescr`, a `<details>` text version, and a link list. Mermaid
      conveys no node relationships to assistive tech, and the README is also read in
      `less`, on mirrors, and in agent context windows where Mermaid never renders.
- [ ] No block sets `theme:`; no styled node sets `fill` without `color`. Node IDs are
      sanitised — a raw slug makes `mir-backend --> mir-backend-python` ambiguous to the
      flowchart parser.
- [ ] **NOT automatable:** that the diagrams render correctly on GitHub in both themes.
      GitHub does not document its pinned Mermaid version and trails upstream, so design
      for 10.x. Procedure: open the PR preview in light and dark, then repeat with Dark
      Reader disabled and with "sync with system" off. Run by whoever opens the PR; record
      the result in the PR body.

### B3 — The deferred skills

| Skill | Status | Why |
|---|---|---|
| `mir-cloud-aws` | **MUST** | Clears the only live `REF002`; the skeleton the other three copy, designed and reviewed once |
| `mir-frontend-angular` | **MUST** | The only item closing a *stated, self-documented* hole. An Angular user gets 1 of 3 layers; everyone else gets 2 or 3 |
| `mir-cloud-cloudflare` | SHOULD | Cheapest remaining; the pillar already carries the isolate/DO/R2/egress facts |
| `mir-cloud-gcp` | SHOULD | Richest actionable IAM content |
| `mir-cloud-azure` | MAY SLIP | Largest surface, most in-flux, lowest actionable yield |
| `mir-frontend-react-remix` | MAY SLIP FIRST | RR7/8 users already get 2 of 3 layers; RSC is unstable in v8, so writing it now documents an unstable API in a kit premised on dated verified fact |

Ending at 50 skills with two honest planned entries is strictly better than 52 with two
thin ones.

- [ ] **B3.1** Defect D1 lands before any cloud module: all four slugs spelled in full at
      every body site in `skills/mir-cloud/SKILL.md`, so `mir-cloud` emits **four**
      `REF002` rather than one. **The CI ratchet is raised in the same commit** — see
      Trap 1.
- [ ] **B3.2** No new skill lands with an error or an already-over-budget `CTX001`. Every
      new description is ≤ 1,450 chars with `TRIGGER` and `SKIP` inside `desc[:1536]`.
- [ ] **B3.3** Whenever the cloud set is partial, Gate 5 names only the modules that exist
      plus the explicit "not written — run the pillar alone and record that module-level
      mechanics were unavailable" sentence. Ships in the same commit as each landing.
- [ ] **B3.4** The stale-sentence sweep passes: no body says "planned", "not written", or
      "do not try to load" about a slug that now has a directory. `validate.py` cannot
      catch this class, and an actively wrong instruction in a loaded body is worse than a
      missing one because the agent obeys it.
- [ ] **B3.5** `.mir-planned` deletions ship in the same commit as the directory; zero
      `PLN001`, and zero `REF001`/`CHN001`. Removing a slug *before* its directory exists
      promotes warnings to errors and `install.sh` then refuses to install for everyone.
      `init/overlay.json` gains labels, and the Remix detection signal is
      `@react-router/dev` — **not** `react-router`, a transitive dep of many React apps.

### B4 — The README

- [ ] Every claim cross-agent support falsifies is gone. Seven known sites; the grep gate
      is conditioned on `init/targets/` existing.
- [ ] "Tool support at a glance" reads **conditional** for Cursor, not yes or no. A
      harness that is present and inert is harder to document honestly than one that is
      absent, which is why it gets a sentence rather than a cell.
- [ ] The README carries the **generated** diagram blocks, not hand-drawn copies, and
      `gen_diagrams.py --check` exits 0. No committed SVG or PNG renders — either
      reintroduces the two-sources-of-truth problem the generator exists to kill.
- [ ] Density is down. Measured today: **767 lines, 79,180 bytes, 188 table rows**. Target
      **under 40 KB and under 90 table rows**, with the inventory in `docs/skill-tree.md`.
      The plan's ~18 KB target was set against a 35,924-byte file and is no longer
      reachable without deleting content that earns its place; 40 KB is the revised
      number, stated here rather than silently missed.
- [ ] `assets/logo.png` is either used or removed. It is **5,571,013 bytes** at 2272×1884
      and referenced zero times. A 5.5 MB blob in the clone path of a tool that advertises
      token discipline is a claim the repository does not support about itself.
- [ ] README and `EXTENDING.md` counts agree with `validate.py`, and both say which
      classification scheme they mean.

### B5 — The two open findings

Both are about the **generated** probe — the thing a user runs — not the repo's own suite,
which already covers the guard directly. A frozen `.mir/probe.py` that never asks a
question is how a guard branch rots silently in every installed project while the
maintainer's CI stays green.

- [ ] **B5.1** `build_attacks` emits at least one Bash attack per verb family
      (`redirect`, `tee`, `dd`, `cp`), labelled. Row count stays **linear** in
      `denied_paths`, not combinatorial. A mutation test per verb deletes that branch from
      the **generated** guard and asserts the probe exits 1.
- [ ] **B5.2** The probe fires a **derived** sibling control per literal denied entry
      (`.gitignore` against `.git`), expected ALLOW, counted as a positive control — so an
      over-block yields exit 3 rather than a silently greener run. Glob and `~`-rooted
      entries are excluded deliberately: `~/.sshx` is outside every allowed root, so
      deny-by-default blocks it regardless and asserting ALLOW would fail working code.
      A mutation test replaces `_is_under` with a bare `startswith(root)` and asserts
      exit **3** — not 0, not 1.

---

## C. Phase ordering

```
P0  D1 + ratchet ──┐
                   ├──> P2  six skills ──┐
P1  diagrams ──────┘                     ├──> P4  README + release
P3  cross-agent init ────────────────────┘
```

| Phase | Depends on | Parallel with |
|---|---|---|
| **P0** D1 + ratchet | nothing | P1, P3 |
| **P1** generator, 8 diagrams, DIA codes | nothing | P0, P3 |
| **P2** six skills (aws → angular → cloudflare → gcp → azure → remix) | P0, P1 | P3 |
| **P3** `init/targets/`, multi-protocol guard, 3-phase probe, `COVERAGE.md`, `MANIFEST_VERSION = 2` | nothing | P0, P1, P2 — **except the README** |
| **P4** README, `docs/skill-tree.md`, CHANGELOG 2.0.0, tag | P1, P2, P3 | — |

**P1 and P3 run concurrently** — disjoint code (`docs/` + `validate.py` vs `init/`). Their
one contention point is the README, resolved by **ownership, not scheduling**: P3 owns
"Tool support at a glance" and "Project harness (`mir init`)"; P1 owns everything else plus
every `mir:gen:` block. The markers make that boundary mechanical instead of social.

**Does the diagram generator depend on the six skills?** No, and it must not — the derived
diagrams read the tree at generation time and work at 46 or 52. The dependency runs the
*other* way: once `DIA001` is an error, every commit adding a skill directory must carry
the regenerated blocks. That is why B lands before C.

**Does cross-agent parity need to land before the README?** Tighter than that — the README
is *currently correct* and becomes *false* the moment `init/targets/codex.py` exists. The
edit is **coupled to P3's commit**, not sequenced after it.

### Sequencing traps

**Trap 1 — D1 turns CI red on its own.** `mir-cloud` emits one `REF002` today because
`SKILL_REF` misses the bare suffixes. Spelling all four in full makes it emit four:
warnings 7 → 10, against `BUDGET = 7` hardcoded at `.github/workflows/ci.yml:65`. The
pressure is to revert the fix or delete the `.mir-planned` lines — and deleting them
promotes `REF002` to `REF001`, an error, so `install.sh` refuses to install for everyone.
**Raise the ratchet in the D1 commit, with the breakdown in the comment beside it.**

**Trap 2 — a green wiring row over a dead hook.** `wiring_report` builds every row from
`entry.get("matcher")` and never reads the command string. After `--protocol` becomes
required, a repo carrying the v1 command has a correct matcher and a guard that exits 3 on
every call — **status `wired`, enforcement zero**. The wiring phase must compare the
registered *command* including `--protocol`, and that must land **in or before** the commit
that makes `--protocol` required.

**Trap 3 — making the Antigravity adapter exit 2 "so the probe passes."** BLOCK there is
exit 0 + stdout JSON. If the probe is not taught to read stdout first, every Antigravity
BLOCK reads as ALLOW, every denied row becomes a LEAK, and the tempting fix turns the probe
green while the real host allows every write. **Teach the probe protocol-aware verdict
parsing first, then add the target.**

**Trap 4 — `~/.gemini` buys a row that cannot fail.** By `probe.py`'s own labelling a
`~`-rooted entry is `deny-by-default`. Add it — it is a real denial — but assert
`proves == "rule"` only for repo-local `.codex` and `.agents`.

**Trap 5 — `guard.py` claims a check `probe.py` does not contain.** `guard.py:76` says
"a version mismatch fails closed — the probe owns that." `probe.py` has **zero** references
to `mir_manifest_version`. Today the mismatch is caught by accident: the guard allows
everything, so every denied row reads as a leak. At `MANIFEST_VERSION = 2` that accident
becomes the primary migration path for every existing user. Either make the claim true or
reword the docstring — do not ship a doc claim the code does not support, which is the
category this release closes.

**Trap 6 — a stale sentence survives every check in the repository.** ~22 across 11 files.
B3.4's script is the only mechanical check for this class; run it in CI, not by hand.

---

## D. The regression bar

The pass count never goes down and the fail count is always zero, on **both** interpreters,
at every commit. `/usr/bin/python3` is not optional: it is 3.9.6 on stock macOS,
`generate.py` registers the hook as bare `python3`, and every macOS user without a pyenv
runs the guard on it. `errors == 0` is hard; `warnings <= BUDGET` is a ratchet that moves
only in the commit that justifies it, with the breakdown written beside the number.

A commit that lowers the pass count must name, in its message, which test was deleted and
why. Deleting a test to make a change land is the failure this bar prevents, and the count
alone cannot tell that apart from a merge that consolidated two assertions — which is why
the burden is on the message, not the number.

**The mutation rule.** Every fix carries a test that reverts the fix and asserts the suite
goes red. Not a test that the fixed behaviour works — a test that the *broken* behaviour is
caught. Two properties, both learned here:

1. **Mutate the generated copy, not the source.** The defect class is a frozen
   `.mir/guard.py` rotting inside a user's project while the maintainer's tree is fine.
2. **Assert the exit code, not just "nonzero."** `1` (leak), `2` (could not run) and `3`
   (inconclusive) are three different findings. A mutation that flips a leak into a crash
   and scores as "still red" has stopped testing what it names.

---

## E. Non-goals

1. **The generative fallback for uncovered stacks.** A generated skill has no dated facts,
   no verified advisories and no reviewer; shipping one under the same `mir-*` namespace as
   46 researched skills would make the namespace mean nothing.
2. **A Windows installer or Windows CI.** `install.sh` is bash, `generate.py` registers bare
   `python3`, and both symlink paths need Developer Mode or elevation. A half-working
   Windows path is worse than an honest absence.
3. **PyPI or any build artifact.** `install.sh` symlinks rather than copies, so "the
   installed version" is a property of the working tree at `git pull` time.
4. **Renaming or removing any skill slug.** Each is independently MAJOR-worthy and v2
   already has its breaking change.
5. **Adding a required frontmatter key.** It fails every third-party skill, after which
   `install.sh` refuses to install the whole tree.
6. **Enforcing `network_domains` and `allowed_commands`.** Recorded, not enforced; that
   line stays honest in v2.
7. **Fixing the 4 `CTX001` warnings.** Three to eight lines over on four unrelated skills.
   A forced refactor for a rounding error is a worse trade than a ratchet that notices
   growth.
8. **Signed tags.** `v1.0.0` is annotated but unsigned and its tagger identity does not
   match the commit author. A key-management change does not belong in the release path.

**Additions v2 specifically needs**, planned for 1.1.0 and never shipped:

- **`mir doctor`** — `MANIFEST_VERSION` 1 → 2 makes every existing `.mir/` stale and there
  is no command that tells a user so. Shipping the breaking change without the detector
  leaves "re-run `mir init` after upgrading" as exactly the convention-with-no-check
  `validate.py` exists to abolish.
- **`generated_by_version` in the manifest** — `generated_at` says *when* a guard was
  frozen, not *whether* it contains a fix. `mir doctor` is the consumer that earns it.
- **`validate.py --max-warnings` returning exit 3** — the ratchet currently lives as a
  literal inside a workflow heredoc. Exit 3, not 1, so a budget overrun never makes
  `install.sh` refuse installs on user machines.

---

## F. Risks

| # | Risk | Concrete failure | Caught by |
|---|---|---|---|
| R1 | A path written where nothing reads it | `.codex/hooks.json` is emitted, Codex's hooks default off or need trust approval, and the harness sits inert while `COVERAGE.md` says `enforced` | B1.3, B1.7, B1.10 |
| R2 | A probe that passes while never attacking what it names | The new `.codex`/`.agents` entries get rows that block trivially | B1.6, B1.5, the existing generalised assertion |
| R3 | A guard branch rots and no installed probe notices | A frozen guard loses `tee`/`dd`/`cp`; the in-tree tests stay green | B5.1 |
| R4 | A doc claim the code does not support | Seven live sites say Claude-Code-only and become false the instant `targets/codex.py` exists | B4, B1.8, Trap 5 |
| R5 | A canonicalisation that turns the suite red while looking like a security win | `/var` vs `/private/var` again, under the protocol dispatch | D, run on both interpreters |
| R6 | The prefix over-match nobody tests | A "simplification" to bare `startswith` denies `.gitignore`; the probe reports *fewer* leaks and reads as an improvement | B5.2 |
| R7 | A green wiring row over a dead hook | The v1 command reports `wired` while the guard exits 3 every call | Trap 2, gated by B1.3 + B1.4 ordering |
| R8 | CI red for the right reason gets fixed the wrong way | D1 takes warnings 7 → 10; reverting the fix or deleting `.mir-planned` lines both make it worse | B3.1, B3.5, Trap 1 |
| R9 | A partial cloud set leaves Gate 5 pointing at nothing | Two land, two slip, the pillar keeps naming all four | B3.3 |
| R10 | A thin skill shipped to hit a count | Two rushed cloud modules at 220 lines of generic advice triple the currency-pass surface forever | B3's MUST/SHOULD/MAY-SLIP table |
