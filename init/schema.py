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
same set.

MANIFEST_VERSION is 2 as of v2.0.0. The bump is the release's stated breaking change and it
buys one thing: a `targets` block, so a harness can record WHICH hosts it was generated for
and probe.py can read the declared set from the harness instead of from whoever last typed a
flag. A target cannot be dropped from verification by re-running the probe with narrower
arguments.

A v1 manifest still VALIDATES -- see SUPPORTED_VERSIONS. That is not a softening of the
breaking change; it is where the breaking change actually bites. The guard compares versions
and fails OPEN on a mismatch, so a v1 manifest beside a v2 guard enforces nothing, loudly.
Making validate_manifest reject v1 as well would turn "your harness is stale, regenerate it"
into "your harness is malformed", which sends the reader to the wrong repair.

No third-party dependencies. Standard library only, to match validate.py and install.sh.
"""

from __future__ import annotations

MANIFEST_VERSION = 2

# Versions this schema can still READ. A v1 manifest predates cross-agent init and declared
# Claude Code implicitly; probe.declared_targets reads "no targets block" as exactly that,
# so an old harness keeps verifying the target it always had rather than verifying nothing.
SUPPORTED_VERSIONS = (1, 2)

# Paths that are never a legitimate write target for a coding agent, added to every
# project policy unless the caller opts out. Repo-relative entries resolve against the
# repo root; ~ and absolute entries resolve as written. An entry holding a glob
# metacharacter is matched with fnmatch instead of as a prefix (see the module docstring).
BASELINE_DENIED = [
    ".git",                  # rewriting history / hooks is not a normal edit
    ".mir",                  # the policy, guard, and probe live here; must be self-protected
    # The hook registration; an agent must not unregister its own guard. A pattern, not the
    # literal path, because Claude Code also reads settings.local.json and that file can
    # carry PreToolUse hooks too -- denying only settings.json left the agent a second file
    # to disarm itself through. Deliberately NOT `.claude`, which would deny .claude/skills
    # and break `mir init --install`.
    ".claude/settings*.json",
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
    # Cross-agent init hands the agent two more files it could disarm itself through, so both
    # are denied REPO-LOCALLY -- which is what makes them rows the probe can label `rule`. A
    # `~/`-rooted entry sits outside every allowed root, so deny-by-default blocks it whether
    # or not the entry works; it is a real denial and it is NOT coverage of itself.
    ".codex",         # .codex/hooks.json registers the Codex guard; unregistering it is not an edit
    # Denied WHOLESALE, including .agents/skills. The generator runs outside the agent's tool
    # loop, so `mir init` can still write .agents/hooks.json here; the agent has no legitimate
    # reason to edit skill definitions or its own hook registration mid-task. Denying only
    # .agents/hooks.json would leave the skills beside it rewritable, which is the same hole
    # one directory over.
    ".agents",
    # Antigravity's home config. `deny-by-default` by the probe's own labelling, like every
    # other `~/`-rooted entry: a real denial that buys a row which cannot fail. Added because
    # it is real, not because it adds coverage.
    "~/.gemini",
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


def project_manifest(repo_name: str, allowed_write_roots=None, extra_denied=None,
                     targets=None) -> dict:
    """A baseline project policy: work inside the repo, never touch secrets or tool config.

    allowed_write_roots defaults to the repo root ("."), i.e. anywhere in the repo except
    the denied paths. A task policy later narrows this to the feature's actual directories.

    `targets` is the v2 block: {name: {protocol, wiring_files, write_policy_enforcement}}.
    Omitted rather than written empty when there is nothing to record, because "no targets
    block" already has a meaning -- probe.declared_targets reads it as the implicit Claude
    Code of every v1 harness -- and an empty dict would be a third state nothing understands.
    """
    policy = empty_policy()
    policy["allowed_write_roots"] = list(allowed_write_roots) if allowed_write_roots else ["."]
    policy["denied_paths"] = list(BASELINE_DENIED) + list(extra_denied or [])
    policy["invariants"] = [
        "no writes outside allowed_write_roots",
        "no writes to any denied_path, even inside an allowed root",
    ]
    manifest = {
        "mir_manifest_version": MANIFEST_VERSION,
        "kind": "project-policy",
        "repo": repo_name,
        "owned_by_mir": True,          # regeneration may overwrite; hand edits will be lost
        "generated_by": "mir-init",
        # generated_at is stamped by the CLI at write time; scripts cannot read the clock.
        "policy": policy,
    }
    if targets:
        manifest["targets"] = dict(targets)
    return manifest


def example_manifest() -> dict:
    """A concrete task policy, used by the probe's self-test and the docs."""
    m = project_manifest("example-app", allowed_write_roots=["src", "tests"])
    m["kind"] = "task-policy"
    m["policy"]["allowed_commands"] = ["npm", "node", "npx", "git", "eslint", "vitest"]
    m["policy"]["network_domains"] = ["registry.npmjs.org"]
    m["policy"]["required_checks"] = ["npm run lint", "npm test"]
    return m


def _validate_targets(raw) -> list:
    """Errors in the v2 `targets` block. An ABSENT block is not an error -- see
    project_manifest for why "no block" already means "the implicit Claude Code of v1".

    Shape-tolerant in the same two shapes probe.declared_targets accepts, and for the same
    reason: the probe is copied standalone into a project and outlives the generator that
    wrote the manifest beside it. A validator stricter than the reader would reject manifests
    the thing that reads them handles perfectly well.
    """
    if raw is None:
        return []
    errs: list = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(t, {}) if isinstance(t, str) else (None, t) for t in raw]
    else:
        return ["manifest 'targets' must be an object or a list of names, got %s"
                % type(raw).__name__]
    if not items:
        # An empty block is a third state: it reads as "no targets declared", which is a
        # harness that verifies nothing while looking configured. Omit the key instead.
        return ["manifest 'targets' is present but empty; omit the key to mean 'claude'"]
    for name, spec in items:
        if isinstance(spec, dict) and name is None:
            name = spec.get("name")
        if not isinstance(name, str) or not name:
            errs.append("a targets entry has no usable name: %r" % (spec,))
            continue
        if spec is None or isinstance(spec, dict):
            proto = (spec or {}).get("protocol")
            if proto is not None and (not isinstance(proto, str) or not proto):
                errs.append("targets.%s.protocol is %r, expected a non-empty string"
                            % (name, proto))
        else:
            errs.append("targets.%s is %s, expected an object" % (name, type(spec).__name__))
    return errs


def validate_manifest(obj) -> list[str]:
    """Return a list of human-readable errors. Empty list means the manifest is usable."""
    errs: list[str] = []
    if not isinstance(obj, dict):
        return ["manifest is not a JSON object"]

    v = obj.get("mir_manifest_version")
    if v not in SUPPORTED_VERSIONS:
        errs.append(f"mir_manifest_version is {v!r}, expected one of {SUPPORTED_VERSIONS}")

    errs += _validate_targets(obj.get("targets"))

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
