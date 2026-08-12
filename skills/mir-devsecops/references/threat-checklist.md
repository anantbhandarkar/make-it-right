# Gate 7 — pipeline security review pass

Run this over the changed files. Every item is a question with a way to answer it from the repo, not an opinion. Report findings as `Severity | File:line | What breaks | Fix`. Fix Critical/High before finishing; list what you deferred and why.

Fast first sweep, if the tools are installed:

```bash
zizmor .github/workflows/                       # workflow static analysis (do NOT add --format sarif here: it suppresses the finding exit codes)
gitleaks git . --redact --no-banner             # secrets in history (`detect` is deprecated since v8.19.0)
checkov -d . --compact                          # IaC misconfiguration
trivy config .                                  # IaC + Dockerfile misconfiguration
trivy fs --severity CRITICAL,HIGH .             # dependency vulnerabilities
trivy image --severity CRITICAL,HIGH <image>    # image vulnerabilities
gh attestation verify oci://<image>@sha256:<d> -o <ORG>
```

A clean run of these is a starting point, not a pass — and none of these invocations blocks anything. `trivy` exits 0 with findings unless you add `--exit-code 1`. Work the checklist below.

## 1. Supply chain

- [ ] Lockfile committed and current. CI installs with `npm ci` / `pnpm install --frozen-lockfile` / `yarn install --immutable` / `uv sync --frozen`, **not** `npm install`. Grep: `grep -rn "npm install" .github/`
- [ ] Lockfile carries per-artifact hashes (npm `integrity`, `uv.lock` sha256, pip `--require-hashes`). Direct-version pins alone do not pin the transitive tree.
- [ ] Install scripts are off, or the exceptions are an explicit allowlist. npm 12: `strict-allow-scripts=true` is set (without it an unreviewed script is only a warning) and `allowScripts` in `package.json` is version-pinned. npm ≤11: `ignore-scripts=true` + targeted `npm rebuild`. pnpm: `allowBuilds`. Every 2025–2026 npm worm executed through a lifecycle hook.
- [ ] Release-age cooldown configured **and strict**. pnpm/Yarn ship a cooldown on, but pnpm's built-in default is non-strict and silently installs a too-new version — `minimumReleaseAgeStrict: true` and `minimumReleaseAgeIgnoreMissingTime: false` must be set explicitly. npm's `min-release-age` is off by default.
- [ ] Every **new** direct dependency in this diff: does it exist on the registry with a plausible publish history, a real source repo, and a name that is not one character or one word from a popular package? AI-suggested imports need this check specifically — hallucinated names recur across runs, and attackers register them.
- [ ] No dependency added from a URL, a git ref, or a tarball path without a hash.
- [ ] Private packages cannot be shadowed by a public one of the same name (scoped names, registry scoping, no fallback to the public index).
- [ ] SBOM generated at build and attached to the release.
- [ ] Artifacts are signed/attested **and the deploy step verifies them by digest**. An attestation nobody verifies is not a control. Provenance proves origin, not safety.
- [ ] Publishing uses OIDC trusted publishing, or the token is scoped to one package with a short expiry.

## 2. CI/CD workflows

- [ ] Every `uses:` for a third-party action is a full 40-character SHA with the version in a trailing comment. Grep: `grep -rnE "uses: [^.].*@(v[0-9]|main|master)" .github/workflows/`
- [ ] No `pull_request_target`, `workflow_run`, or `issue_comment` workflow checks out fork-controlled code. Grep for `allow-unsafe-pr-checkout` — its presence is a finding. `actions/checkout` is v7+ where the safe defaults exist.
- [ ] No `${{ github.event.* }}` / `${{ github.head_ref }}` interpolation inside a `run:` block. Values are bound to `env:` and referenced as `"$VAR"`, quoted. Check contexts ending in `body`, `title`, `head_ref`, `ref`, `label`, `message`, `name`, `email`, `default_branch`, `page_name`.
- [ ] Workflow-level `permissions:` present and minimal (`contents: read`), widened per job only where needed. No `permissions: write-all`. Repo default token permission verified, not assumed — the read-only default applied to new orgs/repos, not existing ones.
- [ ] No secret is reachable from a workflow that a fork can trigger.
- [ ] A privileged job does not download and execute an artifact produced by an unprivileged, fork-triggered job.
- [ ] Publish/deploy jobs are gated by an environment with required reviewers, and publish secrets are scoped to that environment.
- [ ] No `continue-on-error: true` on a security check, and no security job excluded from required status checks. Every scanner exits non-zero on Critical/High.
- [ ] No secret echoed, no `set -x` in a step handling secrets, no secret passed as a CLI argument.
- [ ] Self-hosted runners are ephemeral and are not exposed to fork PRs.
- [ ] Runner egress is restricted (or an audit baseline is running with a date to switch to block).
- [ ] Release tags and assets are immutable; the release path cannot be rewritten after publish.

## 3. Secrets

- [ ] No credential in the diff. Check `.env` files, test fixtures, IaC variable files, Helm `values.yaml`, Dockerfiles, and CI YAML.
- [ ] Cloud access uses OIDC. If a long-lived key exists, it has an owner, an expiry, and a rotation record.
- [ ] OIDC trust policy pins `aud` (`token.actions.githubusercontent.com:aud` = `sts.amazonaws.com`) **and** `sub` to repository plus ref/environment, uses `StringEquals`/`StringLike`, and contains no `ForAllValues:` in an Allow statement. Wildcards in `sub` (`repo:org/*`) are a finding. Note the two `sub` shapes: `repo:ORG/REPO:ref:refs/heads/BRANCH` and `repo:ORG/REPO:environment:ENV` — the environment form carries no branch, so the environment's own deployment-branch rules become the trust boundary.
- [ ] Push protection enabled at repository/org level (not just the user-level default), with bypass restricted.
- [ ] History scanned, not just the working tree.
- [ ] Anything ever exposed has been **rotated**, not just deleted from a commit.
- [ ] No real credential behind `NEXT_PUBLIC_*`, `VITE_*`, `REACT_APP_*`, or otherwise referenced from client code — those values ship in the bundle. If one was there, it is public.
- [ ] Secrets are not baked into image layers (BuildKit `--mount=type=secret` used instead).
- [ ] The rotation list includes everything a compromised job could read, not only the credential that leaked.

## 4. IaC

- [ ] State backend is encrypted, versioned, locked, and access-restricted. Read access to state is treated as access to every secret in it — `sensitive = true` only redacts CLI output.
- [ ] No secret value in `.tf`, `.tfvars`, or a committed plan file.
- [ ] `terraform plan -out=tfplan` produces the artifact that is applied; policy runs against `terraform show -json tfplan`; no re-plan after approval.
- [ ] Policy-as-code runs in hard-mandatory mode (non-zero exit), not advisory.
- [ ] No `0.0.0.0/0` on ingress, especially 22, 3389, 5432, 6379, 27017, 9200.
- [ ] Buckets: public access block present, ACLs not public, encryption on, access logging on, versioning on where data matters.
- [ ] Databases and queues: not publicly reachable, encrypted at rest and in transit, backups configured.
- [ ] No wildcard IAM in the IaC (`Action: "*"`, `Resource: "*"`); `iam:PassRole` scoped.
- [ ] Logging and audit trails enabled by the IaC, and their deletion protected.
- [ ] Drift detection scheduled, and drift has an owner rather than a dashboard.

## 5. Containers

- [ ] Base image pinned by digest, and a scheduled rebuild exists so the pin does not freeze unpatched CVEs.
- [ ] Base image comes from a registry you trust and is minimal for what the process needs.
- [ ] `USER` set to a non-root UID before `ENTRYPOINT`, **and** the deployment manifest sets `runAsNonRoot: true`.
- [ ] `readOnlyRootFilesystem: true` with explicit writable mounts.
- [ ] `capabilities: drop: ["ALL"]`, additions justified individually; `allowPrivilegeEscalation: false`; no `privileged: true`; no host network, PID, or IPC namespace; no docker-socket mount.
- [ ] Seccomp default profile on; AppArmor or SELinux enforcing.
- [ ] Multi-stage build — no compilers, package managers, or build credentials in the final image.
- [ ] Image scan gates the build on Critical/High; ignored CVEs are listed with a reason and a re-review date.
- [ ] Deployment references an image **digest**, not `:latest` or a moving tag, and admission verifies the signature.
- [ ] Container runtime version is patched against known escapes: **runc ≥1.3.6** (or 1.4.3 / 1.5.0-rc.3) for CVE-2026-41579, which supersedes the Nov 2025 floor of 1.2.8 / 1.3.3 / 1.4.0-rc.3. The 1.2 branch is not patched for it, so 1.2.8 is a finding.

## 6. Runtime and the incident path

- [ ] Workload IAM is resource-scoped, with no wildcard actions and a permission boundary where the role can be modified by others.
- [ ] Egress is default-deny with an allowlist for production workloads.
- [ ] IMDSv2 required, hop limit 1, and metadata blocked from any workload that fetches user-supplied URLs.
- [ ] Audit logging enabled across accounts and regions, delivered to storage the workload cannot write to, with a stated retention period and verified integrity.
- [ ] Alerts exist for identity changes (`CreateAccessKey`, `AttachRolePolicy`, trust-policy edits, new OIDC providers) and route to a human.
- [ ] Rollback: the previous known-good digest is recorded per environment and the rollback command has been run at least once.
- [ ] Revocation runbook lists every credential the pipeline can reach, with the command to revoke each, and names who executes it.
- [ ] Someone is on call, and the alert has been tested end to end.

## Severity guidance

| Severity | Examples |
|---|---|
| **Critical** | Fork-controlled code executing with secrets or write access; a live credential in the repo or a client bundle; wildcard `sub` in an OIDC trust policy; publishing without any approval gate |
| **High** | Unpinned third-party action; script injection in a `run:` block; `write-all` token; unencrypted state with secrets; public bucket or `0.0.0.0/0` on a database port; scanner present but not blocking; container running as root with a writable root filesystem |
| **Medium** | Missing cooldown or SBOM; attestations generated but unverified; `:latest` in a deployment; audit logging retained under the required window; missing drift detection |
| **Low** | Missing version comment on a SHA-pinned action; scan exclusions without a re-review date; documentation gaps in the runbook |
