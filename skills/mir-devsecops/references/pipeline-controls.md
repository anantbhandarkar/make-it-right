# Pipeline controls — where each one is enforced, and whether it blocks

Read at **Gate 0** (to pick the enforcement point) and **Gate 6** (to write the config that makes it block).

**The rule this file exists to enforce: a control that only warns is not a control.** Every row below states where the control runs and what it does when it fires. If your implementation reports instead of blocking, you have telemetry, not a control — say so in the Gate 5 table rather than pretending otherwise.

## Enforcement points, and what each one can and cannot do

| Point | Runs on | Can it block? | Bypassable by |
|---|---|---|---|
| **pre-commit** | Developer machine | Only locally | `git commit --no-verify`, or not installing the hook. **Never the only place a control lives.** |
| **CI** | Every push/PR, on the runner | Yes, if the job fails **and** branch protection requires that check | Admins with bypass; workflows that use `continue-on-error: true`; forked-PR runs that skip the job |
| **registry** | On push/pull of the artifact | Yes, if the registry rejects the push or admission verifies on pull | Direct pulls that skip admission; a second registry nobody governs |
| **deploy** | At apply/rollout time | Yes, if the deploy step exits non-zero or admission denies | Manual `kubectl apply`, console changes, break-glass paths |
| **runtime** | In production, continuously | Detects; blocks only where a policy engine is inline | Anything that happens faster than the detection loop |

Pick the **latest point where the control still prevents harm**, then add earlier points for fast feedback. Pre-commit is feedback; CI and deploy are gates.

## Supply chain

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| Committed lockfile with hashes | CI | BLOCK | `npm ci`, `pnpm install --frozen-lockfile`, `yarn install --immutable`, `uv sync --frozen`, `pip install --require-hashes` | A malicious version already pinned in the lockfile |
| Install scripts disabled | CI + `.npmrc` | **WARN by default on npm 12 — BLOCK only with `strict-allow-scripts=true`** | npm 12 blocks an unreviewed dependency's install scripts but lets the install succeed with a warning; `strict-allow-scripts=true` in `.npmrc` turns it into a hard error. Approve deliberately: `npm install-scripts approve <pkg>` writes version-pinned entries (and `false` denials) to `allowScripts` in `package.json`. npm ≤11: `ignore-scripts=true`, `npm ci --ignore-scripts`, then `npm rebuild <pkg> --ignore-scripts=false` for esbuild/sharp/node-gyp/Cypress/Playwright. pnpm 11: `allowBuilds` map | Malicious code that runs at import time rather than install time. `--ignore-scripts` and `--dangerously-allow-all-scripts` both override `strict-allow-scripts` |
| Release-age cooldown | CI + package-manager config | **WARN on defaults — BLOCK only when strictness is set explicitly** | pnpm's *built-in* 1440-minute default is documented as non-strict: when no mature version satisfies the range it installs the too-new one anyway. Set `minimumReleaseAge` yourself (this flips `minimumReleaseAgeStrict` on) or set `minimumReleaseAgeStrict: true`, plus `minimumReleaseAgeIgnoreMissingTime: false`. Yarn `npmMinimalAgeGate` (added 4.10, on by default at `1d` since 4.15) takes a duration — `7d`, not a minute count. npm `min-release-age=7` (days; off by default). Bun 1.3 `minimumReleaseAge` (seconds), uv `exclude-newer`, pip `--uploaded-prior-to` | Long-dwell account takeover; a compromise you install on day 8. Every cooldown needs registry upload-time metadata — pnpm skips the check outright for packages whose metadata omits `time` unless you set `minimumReleaseAgeIgnoreMissingTime: false`, and pip/uv need PEP 700, which many private indexes do not publish |
| New-dependency name verification | pre-commit + code review | WARN (human gate) | Require the PR to state the registry URL, publish date, repo, and download history for each new direct dependency. AI-suggested names must be checked against the registry before install — hallucinated names recur across runs and across models | A legitimate-looking package that is malicious |
| Dependency vulnerability scan | CI | BLOCK on Critical/High | `npm audit --audit-level=high`, `pip-audit`, `trivy fs --exit-code 1 --severity CRITICAL,HIGH`, Dependabot/Renovate with grouped auto-merge for patches only. `uv audit` exists but is preview-gated — bare `uv audit` errors out; it needs `preview-features = ["audit-command"]` (or `--preview-features audit-command`), so do not make it the only Python gate | Malware (audit databases track CVEs, not backdoors) |
| Malware/behaviour scan | CI + registry | BLOCK | A scanner that flags install hooks, obfuscation, and network calls in package tarballs (Socket, StepSecurity, registry-side equivalents) | Novel obfuscation |
| SBOM generation | CI | WARN (an artifact, not a gate) | `syft . -o cyclonedx-json` / `spdx-json`, attached to the release. Formats: CycloneDX 1.7, SPDX 3.0.1. CISA's 2026 Minimum Elements adds component hashes, licenses, generation tool name, generation context | Anything, by itself — an SBOM answers "what shipped" after the fact |
| Artifact signing + provenance | CI | BLOCK (only paired with verification) | `actions/attest@v4` (`id-token: write`, `attestations: write`, `artifact-metadata: write`) or `cosign sign --yes` | Whether the code is safe. Malicious npm packages have shipped with valid provenance |
| Attestation verification | deploy + registry admission | BLOCK | `gh attestation verify oci://…@sha256:… -o ORG` or `cosign verify --certificate-identity-regexp … --certificate-oidc-issuer …`, in the deploy step, exiting non-zero. Bind to digest, never a tag | A signed-but-malicious artifact from a compromised build |
| Trusted publishing (no stored token) | registry | BLOCK | npm OIDC (CLI ≥11.5.1, Node ≥22.14.0, `id-token: write`; post-20-May-2026 configs must select allowed actions); PyPI via `pypa/gh-action-pypi-publish` ≥1.11.0 | A compromised workflow that is itself allowed to publish |

## CI/CD

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| Actions pinned to full commit SHA | CI + org policy | BLOCK | Org **allowed actions** policy with SHA pinning required (available since Aug 2025). In-repo: `zizmor` with the unpinned-uses rule failing the job | A malicious commit that was already at that SHA; a compromised action whose SHA you then bump |
| Workflow static analysis | CI | BLOCK | Two invocations, not one: `zizmor --format sarif . > zizmor.sarif` for the code-scanning upload, and a separate plain `zizmor .` as the required check. **`--format=sarif` suppresses zizmor's 11–14 finding exit codes by design**, so the SARIF run always exits 0 and gates nothing. Complementary: OpenSSF Scorecard, CodeQL Actions queries | Logic bugs; anything outside workflow YAML |
| No untrusted checkout in privileged workflows | CI | BLOCK | `actions/checkout` v7+ (GA 18 Jun 2026; backported 20 Jul 2026 to v6/v5/v4/v3, not v1) fails by default on fork `repository`/`refs/pull/N/head` or `merge`. Ban `allow-unsafe-pr-checkout` in review. This covers the action only — a privileged job can still reach fork code via raw `git fetch`, `gh repo clone`, or an `issue_comment` ref, so ban those separately. Since 8 Dec 2025 the `pull_request_target` workflow file and checkout commit come from the default branch | A privileged job that downloads a fork-produced artifact and runs it |
| Script-injection prevention | CI | BLOCK | Bind context values to `env:` and reference `"$VAR"`. `zizmor` template-injection rule set to fail. Risky contexts end in `body`, `title`, `head_ref`, `ref`, `label`, `message`, `name`, `email`, `default_branch`, `page_name` | Injection into a third-party action's own inputs |
| Least-privilege `GITHUB_TOKEN` | CI + org setting | BLOCK | Org/repo default token permissions = read-only (default for new enterprises/orgs/personal repos; **existing repos unchanged**), plus `permissions:` at workflow level. Unlisted scopes become `none` once the block exists | A job that legitimately needs write and is then compromised |
| Secrets unavailable to forks | CI (platform behaviour) | BLOCK | Use plain `pull_request` for fork CI — read-only token, no secrets. Do not add a `workflow_run` job that hands secrets to fork-produced content | Secrets in a job triggered by a repo collaborator |
| Environment approval on publish/deploy | CI | BLOCK | GitHub Environment with required reviewers + branch/tag restriction, referenced by the publish job. Scope publish secrets to that environment only | An approver who approves without reading the diff |
| Immutable releases and tags | registry/VCS | BLOCK | Repo/org setting for immutable releases (GA Oct 2025); protected tags ruleset | Assets published elsewhere |
| Runner egress restriction | CI runtime | BLOCK after an audit period | `step-security/harden-runner` with `egress-policy: block`, explicit `allowed-endpoints`, `disable-sudo: true`. Self-hosted: network policy on the runner pod/VM | Exfiltration to an allowlisted host (e.g. your own registry) |
| Self-hosted runner isolation | CI runtime | BLOCK | Ephemeral runners, one job per VM, never on public repos with fork PRs enabled | A compromised job within its own run |

## Secrets

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| No standing cloud credential | CI + cloud IAM | BLOCK | OIDC federation (`aws-actions/configure-aws-credentials` v6 — v5 is a major behind — pinned by SHA, with `id-token: write`; equivalents for GCP/Azure). Cap the session at both ends: `role-duration-seconds: 3600` and the role's `MaxSessionDuration`; the STS credentials stay valid to that expiry even after the job ends. Trust policy uses `StringEquals`/`StringLike` on `sub` pinned to repo **and** ref/environment. **Never `ForAllValues:` in an Allow statement** — it is true when the claim is absent. Opt into immutable `sub` claims (numeric org/repo IDs after `@`; default for repos created on/after 15 Jul 2026) | A workflow in the trusted repo that is itself compromised |
| Push protection | VCS | BLOCK | Enable at repository/org level — it is **off** by default there (on by default only for pushes to public repos and new personal-account public repos). Configure delegated bypass, otherwise any write-access user bypasses with a reason | Historical commits; token types not in the detector set |
| History scanning | CI + scheduled | BLOCK on new findings | `gitleaks git . --redact --exit-code 1` (`detect` is deprecated since v8.19.0 — still works, hidden from `--help`), `trufflehog git file://. --only-verified --fail` | Secrets already exfiltrated. **Rotate first, then rewrite history** |
| Client-bundle secret check | CI | BLOCK | Grep the build output for known secret shapes; ban real credentials behind `NEXT_PUBLIC_*`, `VITE_*`, `REACT_APP_*` — those are compiled into the JavaScript users download | Secrets fetched at runtime by the client from an unauthenticated endpoint |
| Rotation | runtime | BLOCK if expiry is enforced | Short max lifetimes in the secrets manager; automatic rotation where supported; expiry dates on tokens (npm granular tokens cap at 90 days; classic tokens were revoked 9 Dec 2025) | Copies made before rotation |
| Log redaction | CI | WARN — never rely on it | Masking fails on structured data (JSON/XML/YAML) and on transformed values; a base64 of a secret is not masked unless separately registered. Ban `set -x` in steps handling secrets and secrets passed as CLI arguments | Anything. Treat redaction as a safety net, not a control |

## IaC

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| Encrypted, versioned, locked state backend | deploy | BLOCK | OpenTofu `encryption` block with a KMS key provider and `enforced = true`. On state that is currently plaintext, migrate first — add `fallback`, run once to rewrite encrypted, verify, then drop the fallback and enforce; enforcing against unmigrated state makes it unreadable. Terraform (1.15.x) has no client-side state encryption — use backend encryption plus tight IAM, versioning, and locking | Anyone with legitimate read access to state — state contains plaintext secrets regardless of `sensitive = true` |
| Plan-artifact review | CI + deploy | BLOCK | `terraform plan -out=tfplan`, apply **that file**, gate policy on `terraform show -json tfplan`. Never re-plan at apply time after human approval. Treat `tfplan` and its JSON as secret material — they carry resource values in cleartext regardless of `sensitive = true`, so never post them to a PR comment or upload them as a world-readable artifact | A plan that is correct and still wrong |
| Policy as code | CI | BLOCK (hard-mandatory) | OPA/Conftest or Checkov over the plan JSON with a non-zero exit; `trivy config --exit-code 1 --severity CRITICAL,HIGH .` — **bare `trivy config` exits 0 with findings**. Sentinel is HCP-only and does not run against OpenTofu. Advisory/soft-mandatory modes are reports, not gates | Resources created outside IaC |
| Native validation | CI (fast feedback) | BLOCK — except `check` | `validation` on variables and `precondition`/`postcondition` fail the run: cheapest place to catch a bad value. A **`check` block does not gate** — a failed assertion is a warning and the operation continues, so never treat one as enforcement | Cross-resource policy |
| Default-open resource rules | CI | BLOCK | Policy rules for `0.0.0.0/0` ingress (22, 3389, 5432, 6379), public ACLs, missing `aws_s3_bucket_public_access_block`, unencrypted storage, disabled logging | A resource made public in the console later |
| Drift detection | runtime (scheduled) | WARN → ticket with an owner | Scheduled `terraform plan -detailed-exitcode` in CI — **plain `plan` exits 0 whether or not there are changes**; with the flag, 0 = no changes, 2 = changes, 1 = error, so branch on 2 to raise the alert. Note: client-side encrypted state hides attributes from platforms that compute drift by parsing state | Drift in resources not under IaC |

## Containers

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| Base image pinned by digest | CI | BLOCK | `FROM image:tag@sha256:…`; lint for bare tags. Pair with a **scheduled rebuild** — a digest pin also freezes unpatched CVEs | Vulnerabilities in the pinned digest until you rebuild |
| Minimal/distroless base | CI | WARN (design choice) | Prefer distroless or hardened bases (Docker Hardened Images are Apache-2.0 with SBOM and provenance). Watch glibc↔musl differences when switching | Vulnerabilities in your own dependencies |
| Non-root user | CI + deploy | BLOCK | `USER` before `ENTRYPOINT` in the Dockerfile **and** `runAsNonRoot: true` at admission — the manifest is the enforceable one | A process that escalates after start |
| Read-only root filesystem | deploy | BLOCK | `readOnlyRootFilesystem: true` (`--read-only` in Docker) with explicit `emptyDir`/`tmpfs` mounts for writable paths | Writes to mounted volumes |
| Dropped capabilities + no privilege escalation | deploy | BLOCK | `capabilities: drop: ["ALL"]` (add back only what is needed), `allowPrivilegeEscalation: false`, `--security-opt no-new-privileges`, default seccomp, AppArmor/SELinux enforcing | Kernel and runtime vulnerabilities |
| No build secrets in layers | CI | BLOCK | BuildKit `--mount=type=secret`; scan the built image for secrets. A secret in an early layer stays in the image even if a later layer deletes it | Secrets injected at runtime and then logged |
| Image scanning | CI + registry | BLOCK on Critical/High | `trivy image --exit-code 1 --severity CRITICAL,HIGH` in CI plus registry-side scanning for images already pushed. Document every ignored base-image CVE with a re-review date | Zero-days; malicious-but-unknown content |
| Registry trust at admission | deploy | BLOCK | Sigstore policy-controller / Kyverno verifying signature and digest, or `cosign verify` in the deploy step. Allowlist registries | An image signed by a compromised build |
| Container runtime patching | runtime | BLOCK (patch SLA) | Track runc/containerd advisories. Current floor is **runc ≥ 1.3.6** (1.4.3 / 1.5.0-rc.3 on those branches; current release 1.5.1): CVE-2026-41579 (Jun 2026) supersedes the Nov 2025 cluster (CVE-2025-31133, CVE-2025-52565, CVE-2025-52881, fixed in 1.2.8 / 1.3.3 / 1.4.0-rc.3), and the 1.2 branch received no fix for it. Image hardening does not mitigate any of them | Anything before you patch |

## Runtime

| Control | Where | BLOCK/WARN | Make it block | Does not cover |
|---|---|---|---|---|
| Least-privilege IAM | deploy + runtime | BLOCK | Resource-level ARNs, no wildcard actions, permission boundaries as a cap, tightly scoped `iam:PassRole`, `AdministratorAccess` only on a break-glass role that alerts on use. Generate policies from observed calls (Access Analyzer over CloudTrail) | Over-permissive third-party integrations |
| Egress restriction | runtime | BLOCK | Default-deny egress with an allowlist for production workloads; block `169.254.169.254` from workloads that fetch user-supplied URLs; require IMDSv2 with hop limit 1 | Exfiltration to an allowlisted destination |
| Audit logging retained and queryable | runtime | BLOCK (config), WARN (detection) | Enable across all regions/accounts, route to storage the workload's role cannot write to, set retention deliberately (PCI DSS: 12 months, 3 immediately queryable; SOC 2 in practice: 12 months), verify integrity (`aws cloudtrail validate-logs`) | Anything outside the retention window |
| Alerting on identity change | runtime | WARN → page | Alert on `CreateAccessKey`, `AttachRolePolicy`, `PutRolePolicy`, trust-policy edits, and new OIDC providers | Actions that look legitimate |
| Rollback path | deploy | BLOCK (must exist) | Previous known-good **digest** recorded per environment; a rollback command that is tested, not theoretical | Data migrations that are not reversible |
| Credential revocation runbook | runtime | BLOCK (must exist) | Written list of every credential the pipeline can reach — OIDC role trust, registry tokens, environment secrets, signing identities — with the command to revoke each. Rehearse once | Credentials nobody documented |
