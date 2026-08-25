"""The mir task-policy manifest: the contract mir init generates and the probe verifies.

Both plan reviews (Sol High, Fable) landed on the same point: stack selection is not
enough to produce a task policy, and a verify loop that does not derive its probes from
this manifest proves nothing. So this file is deliberately the first thing built. It
defines what a policy IS; init/guard.py enforces it; init/probe.py attacks it.

Two kinds of manifest, kept separate on purpose:

  project-policy  What is true for the whole repo regardless of the task: never write the
                  git internals, never write a secret file, never write outside the repo.
                  mir init generates this once.

  task-policy     What is true for one feature: the write roots that feature may touch, the
                  commands it may run. Narrower than the project policy. Produced per task,
                  later, by the gated pipeline -- NOT derivable from stack choice at init.

The guard enforces the merged effect: a write is allowed only if it is under an
allowed_write_root AND not under any denied_path. denied_paths win over allowed roots, so
`src/.env` is blocked even though `src` is allowed.

A denied_paths entry is a path PREFIX unless it contains a glob metacharacter (`*`, `?`,
`[`), in which case the guard matches it with fnmatch. Both forms live in the one list
because a prefix cannot express "at any depth" and a secrets file has no fixed depth --
`.env` as a prefix denies exactly the repo-root `.env` and leaves `src/.env` writable, which
is how a false security claim shipped in v1.0.0. Literal entries keep their old meaning
exactly, so a manifest written before globs existed still validates and still denies the
same set; MANIFEST_VERSION therefore stays 1.

No third-party dependencies. Standard library only, to match validate.py and install.sh.
"""

from __future__ import annotations

MANIFEST_VERSION = 1

# Paths that are never a legitimate write target for a coding agent, added to every
# project policy unless the caller opts out. Repo-relative entries resolve against the
# repo root; ~ and absolute entries resolve as written. An entry holding a glob
# metacharacter is matched with fnmatch instead of as a prefix (see the module docstring).
BASELINE_DENIED = [
    ".git",                  # rewriting history / hooks is not a normal edit
    ".mir",                  # the policy, guard, and probe live here; must be self-protected
    ".claude/settings.json", # the hook registration; an agent must not unregister the guard
    # Every dotfile whose name starts with `.env`, at any depth: `.env`, `.env.local`,
    # `.env.production`, `src/.env`, `a/b/.env.development`. A secrets file has no fixed
    # location, so the literal prefixes this replaces only ever covered the repo root.
    # Deliberately included, and both are calls rather than accidents:
    #   `.envrc`      -- direnv exports real credentials from it, so it is a secrets file.
    #   `.env.example`-- a template filled in place is the commonest way a live secret
    #                    lands in a repo; a blocked write here is a human's one-line fix,
    #                    a leaked key is not.
    # Deliberately NOT matched: no leading dot means no match, so `environment.ts`,
    # `env.ts`, and `src/env.d.ts` stay writable.
    "**/.env*",
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.kube",
    "~/.npmrc",
    "~/.gitconfig",
    "~/.claude",      # do not let a project agent rewrite the tool's own config
    "~/.codex",
]

KINDS = ("project-policy", "task-policy")

# Every key the guard and probe read. Anything else is advisory metadata.
_POLICY_KEYS = (
    "allowed_write_roots",
    "denied_paths",
    "allowed_commands",
    "network_domains",
    "invariants",
    "required_checks",
)


def empty_policy() -> dict:
    return {
        "allowed_write_roots": [],
        "denied_paths": [],
        "allowed_commands": [],
        "network_domains": [],
        "invariants": [],
        "required_checks": [],
    }


def project_manifest(repo_name: str, allowed_write_roots=None, extra_denied=None) -> dict:
    """A baseline project policy: work inside the repo, never touch secrets or tool config.

    allowed_write_roots defaults to the repo root ("."), i.e. anywhere in the repo except
    the denied paths. A task policy later narrows this to the feature's actual directories.
    """
    policy = empty_policy()
    policy["allowed_write_roots"] = list(allowed_write_roots) if allowed_write_roots else ["."]
    policy["denied_paths"] = list(BASELINE_DENIED) + list(extra_denied or [])
    policy["invariants"] = [
        "no writes outside allowed_write_roots",
        "no writes to any denied_path, even inside an allowed root",
    ]
    return {
        "mir_manifest_version": MANIFEST_VERSION,
        "kind": "project-policy",
        "repo": repo_name,
        "owned_by_mir": True,          # regeneration may overwrite; hand edits will be lost
        "generated_by": "mir-init",
        # generated_at is stamped by the CLI at write time; scripts cannot read the clock.
        "policy": policy,
    }


def example_manifest() -> dict:
    """A concrete task policy, used by the probe's self-test and the docs."""
    m = project_manifest("example-app", allowed_write_roots=["src", "tests"])
    m["kind"] = "task-policy"
    m["policy"]["allowed_commands"] = ["npm", "node", "npx", "git", "eslint", "vitest"]
    m["policy"]["network_domains"] = ["registry.npmjs.org"]
    m["policy"]["required_checks"] = ["npm run lint", "npm test"]
    return m


def validate_manifest(obj) -> list[str]:
    """Return a list of human-readable errors. Empty list means the manifest is usable."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["manifest is not a JSON object"]

    v = obj.get("mir_manifest_version")
    if v != MANIFEST_VERSION:
        errs.append(f"mir_manifest_version is {v!r}, expected {MANIFEST_VERSION}")

    kind = obj.get("kind")
    if kind not in KINDS:
        errs.append(f"kind is {kind!r}, expected one of {KINDS}")

    policy = obj.get("policy")
    if not isinstance(policy, dict):
        return errs + ["manifest has no 'policy' object"]

    for key in _POLICY_KEYS:
        if key not in policy:
            errs.append(f"policy is missing '{key}'")
        elif not isinstance(policy[key], list):
            errs.append(f"policy.{key} must be a list, got {type(policy[key]).__name__}")
        elif not all(isinstance(x, str) for x in policy[key]):
            errs.append(f"policy.{key} must contain only strings")

    roots = policy.get("allowed_write_roots")
    if isinstance(roots, list) and len(roots) == 0:
        # An empty allow-list under a deny-by-default guard blocks every write, which is
        # never what a generated policy means. Say so instead of shipping a repo the
        # agent cannot write to at all.
        errs.append("policy.allowed_write_roots is empty; the guard would block every write")

    return errs


if __name__ == "__main__":
    import json
    import sys

    obj = example_manifest()
    errs = validate_manifest(obj)
    if errs:
        print("example_manifest FAILS its own schema:", file=sys.stderr)
        for e in errs:
            print("  -", e, file=sys.stderr)
        sys.exit(1)
    json.dump(obj, sys.stdout, indent=2)
    print()
