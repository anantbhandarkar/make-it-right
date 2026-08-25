---
name: mir-init
description: "Make It Right (project init). Prepares a repository's Make It Right harness before feature work starts: detects the stack from lockfiles, confirms it through a deterministic picker (never guesses), resolves the matching mir-* skills, and generates the per-project artifacts — a thin AGENTS.md that names the applicable pillars, a CLAUDE.md that imports it, a .mir/manifest.json write policy, and a .claude/settings.json PreToolUse hook that enforces it — then runs a manifest-derived probe to prove the hook actually blocks the denied paths. It is a thin wrapper over the CLI at init/cli.py; it installs no software and generates no code. TRIGGER when a user is setting up a new or existing repo for AI coding agents, asks to 'prepare/scaffold/bootstrap the harness', to generate AGENTS.md/CLAUDE.md/hooks, or to choose and lock a stack. SKIP for writing feature code (that is the backend/frontend/mobile pillars and their gated pipeline), for one-off task constraints (the constraint-interrogator handles those at Gate 1), and for anything that installs packages or changes the machine (mir init only emits declarative files)."
trigger: /mir-init
argument-hint: "<repo path, default .>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - AskUserQuestion
---

# /mir-init · Make It Right (project init)

Prepare a repo's harness once, before feature work. This skill drives the CLI; it does not
reimplement it. The CLI lives at `init/cli.py` in the make-it-right repo (or run `bin/mir`).

## What it does, in order

1. **Detect.** Read `package.json`, `go.mod`, `Cargo.toml`, `requirements.txt`, `pyproject.toml`,
   `Gemfile`, `composer.json`, `mix.exs`, `*.xcodeproj`. Detection **proposes** with "detected X
   because Y" and a confidence; it never silently decides. Two frameworks in one pillar is a
   conflict you must resolve, not a guess.
2. **Confirm through the picker.** The options are derived from the installed skill tree, so they
   are always in sync with what exists. Present them with `AskUserQuestion`; pre-fill the detected
   answers. A stack with no skill is an explicit "Something else" choice — recorded as a gap, never
   dropped. Do not let the user skip a genuinely ambiguous or high-stakes choice.
3. **Resolve** the answers to the coarse→fine skill set (pillar → tier → module), with
   `mir-devsecops` always included (security by default).
4. **Generate** the artifacts (see below). `--dry-run` prints the plan and writes nothing.
5. **Verify.** Run `.mir/probe.py` against the generated guard. The probe derives its attacks from
   the manifest and includes positive controls, so a missing or broken guard fails loudly instead
   of reading as "everything blocked."

## Run it

```bash
python3 init/cli.py init .            # or: bin/mir init .
python3 init/cli.py init . --dry-run  # plan only
python3 init/cli.py init . --answers answers.json --noninteractive
python3 init/cli.py detect .          # just show what the repo looks like
```

## What it writes

| File | What it is |
|---|---|
| `AGENTS.md` | Thin baseline — names the applicable pillars and the one hard rule. Never skill content (EXTENDING.md Rule 2). Content below the marker is preserved on re-run. |
| `CLAUDE.md` | `@AGENTS.md` import plus room for repo notes. |
| `.mir/manifest.json` | The write policy: allowed roots, denied paths, and the recorded stack. |
| `.mir/guard.py` | The PreToolUse hook that enforces the manifest. Under `.mir/` so the agent cannot rewrite it. |
| `.mir/probe.py` | The manifest-derived verifier, so anyone can re-check with `python3 .mir/probe.py --repo .`. |
| `.claude/settings.json` | Registers the hook. **Merged**, not overwritten — existing hooks are kept. |

## Hard rules

- **Installs nothing.** If the repo needs software, emit a declarative plan (devcontainer / setup
  steps) for a human to run in isolation. An agent that installs software is the exact thing the
  containment harness exists to stop.
- **Generated skills for uncovered stacks are a later, quarantined slice** — not this skill. Today,
  an uncovered stack routes to its pillar, whose gates still apply; version-specific facts are
  verified by live search at design time, not frozen into generated prose.
- **Re-running is idempotent.** Files above the ownership marker regenerate; your edits below it stay.

## Security

`mir init` writes files that shape what a coding agent is *allowed to do*, so its own output is a
trust boundary. The concrete risks and how this skill handles them:

- **The policy must protect itself.** `.mir/` and `.claude/settings.json` are in the baseline
  `denied_paths`, so the guard blocks an agent from rewriting the manifest or the guard to widen
  its own permissions. The probe asserts this (`.mir/manifest.json` → BLOCK). Note the scope
  precisely: the denied entry is the settings *file*, not the whole `.claude/` directory — the
  directory has to stay writable because `mir init --install` links project skills into
  `.claude/skills/`. So `.claude/settings.local.json`, which the host also reads, is **not**
  covered. Do not read this bullet as full self-protection of the hook configuration.
- **Deny-by-default outside the allowed roots**, and denied paths win even inside an allowed root,
  so `src/.env` is blocked though `src` is writable. SSH/cloud/kube credentials, `.git`, and the
  tools' own config (`~/.claude`, `~/.codex`) are denied by default.
- **Secrets are denied by pattern, not by prefix.** A `denied_paths` entry is a path prefix unless
  it holds a glob metacharacter, and the baseline ships `**/.env*` because a prefix cannot say "at
  any depth" — `.env` as a prefix denies the repo-root `.env` and leaves `src/.env` writable, which
  is exactly what v1.0.0 shipped while this section claimed otherwise. The pattern covers `.env`,
  `.env.<anything>`, `.envrc` (direnv exports live credentials from it), and `.env.example` (a
  template filled in place is the commonest way a real key lands in a repo) at every depth. A name
  without the leading dot is untouched, so `environment.ts` and `env.ts` stay writable. The probe
  fires each pattern at several depths and suffixes, so a prefix-only regression fails the gate
  instead of reporting green.
- **No secret leakage into generated context.** The thin AGENTS.md records the *stack*, never file
  contents, tokens, or environment values. Do not add secrets to AGENTS.md or the manifest.
- **Honest coverage.** The guard fully covers Write/Edit/MultiEdit/NotebookEdit; Bash write
  coverage is partial (explicit redirects only) and MCP/apply_patch writes are not covered. The
  probe prints its blind spots. Never read a clean probe as proof the untested paths are safe.
- **Session timing.** Claude Code snapshots hooks at session start, so a freshly generated
  `.claude/settings.json` does not protect the session that generated it. Restart and approve the
  new settings before relying on the guard.
- **Merged settings, reviewed.** `.claude/settings.json` is merged so mir does not silently drop an
  existing hook, and project hooks require the user's trust/approval in the host before they run.
