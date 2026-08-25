---
name: mir-cloud-aws
description: "Make It Right (AWS module). AWS identity and service mechanics, loaded after the provider is chosen. Covers: IAM evaluation order and where a permissions boundary does not bind; iam:PassRole escalation; Condition-operator traps (StringEquals, ForAllValues:); GitHub OIDC sub specificity and the immutable numeric-ID sub format; IMDSv2 and HttpPutResponseHopLimit; S3 Block Public Access defaulting only for post-April-2023 buckets; presigned URLs as unrevocable bearer tokens; Lambda and SQS event-source limits; the October 2025 us-east-1 DynamoDB DNS failure as the one-control-plane artifact; Terraform AWS provider v6 breaks and state locking. Chains: mir-cloud then this. TRIGGER once mir-cloud Gate 5 has settled on AWS — Terraform/CDK/SAM, an IAM identity or trust policy, a GitHub Actions OIDC role, an S3 bucket or presigned URL, Lambda/ECS/SQS/DynamoDB wiring, an account and Organizations layout, or a multi-Region blast-radius review. SKIP for the other providers — mir-cloud-gcp, mir-cloud-azure and mir-cloud-cloudflare each get their own module. SKIP while the provider is undecided: comparison, elimination and the cost model are mir-cloud, and loading this before Gate 5 biases the choice. SKIP for app code — handlers and transactions are mir-backend, UI is mir-frontend, schema is mir-database. SKIP for pipeline controls identical on every provider (action pinning, SBOM, secret scanning, plan-vs-apply review) — mir-devsecops."
trigger: /mir-cloud-aws
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-cloud-aws · Make It Right (AWS)

Bottom tier of a two-tier chain: `mir-cloud` decides **where the workload belongs** (provider-neutral, Gates 0–7) → **this** carries AWS mechanics. Reach for it at **Gate 5** (service mapping and exit cost, after the decision is signed off), **Gate 6** (IaC), and **Gate 7** (security, reliability and cost review).

**This module contributes nothing before Gate 5, and that is deliberate.** The pillar's own rule is that naming a provider before the workload is characterized turns the rest of the conversation into justification. Loading AWS mechanics during Gates 0–4 biases the elimination and the ranking toward the provider you just read about. If the provider is still open, close this and go back to `mir-cloud`.

**Comparison facts stay in the pillar.** Lambda's 900 s ceiling, NAT Gateway per-AZ pricing, egress tiers, per-object caps and the exit-cost table are load-bearing for `mir-cloud` Gate 3 eliminations and Gate 4 rankings. This module cites them; it does not restate them.

**Surface state, verified 25 August 2026.**

| Thing | State | Why it matters here |
|---|---|---|
| Terraform AWS provider | **6.61.0** (19 Aug 2026). No v7 line exists | v6 (18 Jun 2025) carries the breaks in §12. v5 gets critical security fixes only |
| A pulled release | **6.57.0 was withdrawn** from GitHub and the Registry for significant bugs; 6.57.1 supersedes it | A floating `~> 6.0` can resolve to a version that no longer exists upstream. Pin, and keep the lock file |
| Terraform S3 backend locking | `use_lockfile` (native, S3 conditional writes) since Terraform 1.10; `dynamodb_table` deprecated in 1.11 and still warning-only through 1.13 | Two lock mechanisms in one team is one lock mechanism |
| New-account quotas | New AWS accounts ship with **reduced** Lambda concurrency and memory quotas, raised automatically by usage | A load test in a fresh account measures the quota, not the architecture |

## The AWS footguns AI walks into most

### 1. IAM is an evaluation order, not a list of permissions

An explicit `Deny` in *any* policy type wins, always. Past that, the combination rules differ per type and AI treats them as one bucket:

| Combination | Result |
|---|---|
| Identity policy + resource policy, **same account** | **Union** — an allow in either is enough |
| Identity policy + resource policy, **cross-account** | **Both** must allow. The trusting account's resource policy alone is not enough |
| Identity policy + permissions boundary | **Intersection** |
| Identity policy + SCP + RCP | **Intersection** — all three must allow |
| Identity policy + session policy + boundary | **Intersection** of all three |

The hole AI never accounts for: **an implicit deny in a permissions boundary does not limit a resource-based policy.** AWS states it directly — a principal whose boundary omits Secrets Manager entirely can still read a secret whose resource policy names them, because the boundary denies only implicitly. Same-account resource policies naming an **IAM user ARN** or an **IAM role *session* ARN** are not limited by an implicit deny in the identity policy, boundary, or session policy. A policy naming the **role ARN** *is* limited by the boundary. That one-word difference between `:role/X` and `:assumed-role/X/session` decides whether your boundary holds.

So: a permissions boundary is a ceiling on what identity policies can grant, **not** a ceiling on what the account can be granted. To bound the account, you need an explicit `Deny` — in an SCP, an RCP, or the boundary itself. Write the deny.

Pick the type on purpose, not by habit:

| Policy type | Attached to | Use it to | It cannot |
|---|---|---|---|
| **Identity policy** | user, group, role | grant a principal its day-to-day permissions | constrain anything it is not attached to |
| **Resource policy** | the resource (bucket, queue, key, secret, role trust) | allow cross-account access, and say *who* may assume a role | be bounded by an implicit deny in a boundary (see above) |
| **Permissions boundary** | user or role | cap what a *delegated* admin can grant when they create principals | stop a resource policy, or apply to anyone you forget to attach it to |
| **SCP** | OU or account | put a floor under an entire account — deny regions, deny root, deny disabling CloudTrail | grant anything; it only filters |
| **RCP** | OU or account | constrain access **to resources in** those accounts, including by external principals | grant anything; coverage is per-service, so check yours is supported |
| **Session policy** | passed at `AssumeRole` | shrink one session below the role's permissions | expand them |

Two rules that follow: an SCP is the only place a deny survives an account admin, and every cross-account allow needs the pair — resource policy in the trusting account, identity policy in the calling one.

Also: `NotPrincipal` with `Deny` in a resource policy denies *every* principal that has a boundary attached, regardless of what you listed. Use `ArnNotEquals` on `aws:PrincipalArn` instead.

### 2. `iam:PassRole` is the escalation primitive, and `Resource: "*"` hands it over

`iam:PassRole` lets a principal attach an existing role to a service it is creating. Paired with almost any create-a-compute-thing permission, it is a full escalation:

```
iam:PassRole + ec2:RunInstances                              → launch with an admin role, read its credentials
iam:PassRole + lambda:CreateFunction + lambda:InvokeFunction → run attacker code as the passed role
iam:PassRole + lambda:CreateFunction + lambda:CreateEventSourceMapping → same, when InvokeFunction is denied
```

Datadog's **pathfinding.cloud** (17 Dec 2025) documents 65 AWS privilege-escalation paths and names `ec2-001` — `iam:PassRole` + `ec2:RunInstances` — as the most commonly exploited; **42% of the 65 paths (27) had no detection coverage in the open-source tooling they evaluated.** Rhino Security Labs' original series established the class. Neither is a CVE, and neither is patched server-side: these are permission combinations *you* grant.

```json
// WRONG — "the CI role needs to pass roles" becomes "the CI role is every role"
{ "Effect": "Allow", "Action": "iam:PassRole", "Resource": "*" }

// RIGHT — name the exact roles, and bind the service that may receive them
{ "Effect": "Allow", "Action": "iam:PassRole",
  "Resource": "arn:aws:iam::111122223333:role/app-task-role",
  "Condition": { "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" } } }
```

Grep every policy in the change for `iam:PassRole`. If `Resource` is `"*"` or a prefix wildcard, that is a Gate 7 blocker, not a nit. `iam:PassedToService` without a `Resource` restriction is only half the control.

### 3. Condition operators that read as controls and are not

- **`StringEquals` does not expand wildcards.** `"StringEquals": {"...:sub": "repo:org/repo:*"}` never matches anything — the `*` is a literal. It fails closed, so you notice. The dangerous direction is the fix: swapping to `StringLike` widens the match to whatever the wildcard covers.
- **`ForAllValues:` returns `true` when the key is absent from the request.** AWS explicitly warns against pairing `ForAllValues` with an `Allow` effect: it turns overly permissive the moment a context key is unexpectedly absent. Always pair it with `"Null": {"<key>": "false"}`. In an `Allow`, a `ForAllValues:` without that `Null` check is an unconditional allow wearing a condition's clothes.
- **Set operators on single-valued keys are a defect.** `aws:PrincipalTag/x`, `aws:ResourceTag/x` and `aws:SourceVpce` are single-valued, and AWS documents that a set operator on one produces an overly permissive policy. Set operators belong only on multivalued keys such as `aws:TagKeys`.
- **`ForAnyValue:` in a `Deny` does not fire when the key is absent** — the deny you thought was a floor has a hole in it. Add the `Null` check on that side too.
- Multiple condition operators in one block are `AND`; multiple values for one key are `OR`; a negated operator (`StringNotEquals`, `ArnNotLike`) with multiple values is `NOR`.

### 4. GitHub OIDC — the trust policy is the entire perimeter

OIDC federation removes long-lived keys from CI, which is the right move. It replaces them with a trust policy, and that policy is now the only thing standing between any GitHub workflow and your account.

```json
"Condition": {
  "StringEquals": {
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:my-org/my-repo:ref:refs/heads/main"
  }
}
```

- **`sub` is mandatory and must be narrow.** `repo:my-org/*` trusts every repository in the org, including forks and anything any member creates. `repo:my-org/my-repo:*` trusts every branch, tag, pull-request merge ref and environment in that repo — including a branch an outside contributor can cause to exist.
- **`aud` alone is not a control.** Every GitHub-issued token carries the same audience.
- **The immutable `sub` format changes the string.** Repositories created on or after **15 July 2026** (and repos that opted in) emit a `sub` containing immutable numeric owner and repository IDs: `repo:octo-org@123456/octo-repo@456789:ref:refs/heads/octo-branch`. Not available on GitHub Enterprise Server. A `StringLike` pattern written for the old format silently stops matching, and a policy tightened around the old shape both breaks and stops protecting. Read the actual `sub` out of a workflow run before writing the condition.
- Scope the *role*, not just the trust: a deploy role reachable from `main` should still be least-privilege. Trust policy and permission policy are two layers, not one.

```yaml
# The workflow side. id-token: write is what mints the OIDC token — grant it at the
# job level, never at the top of the file, or every job in the run can assume the role.
permissions:
  contents: read
  id-token: write
```

Two trust conditions worth adding beyond `sub`: `token.actions.githubusercontent.com:environment` when the role belongs to a protected environment (approvals then gate the credential, not just the deploy step), and a `Deny` elsewhere for any principal outside `aws:PrincipalOrgID`.

### 5. STS sessions — lifetime, chaining, and the confused deputy

A role is not a credential; a *session* is. AI writes the trust policy and stops thinking about the session, which is where several sharp edges live.

- **`MaxSessionDuration` is 1–12 hours**, and `DurationSeconds` on `AssumeRole` may request anything from 15 minutes up to it. Default is 1 hour. Set the role's maximum to the shortest duration the job actually needs — a 12-hour deploy role is a 12-hour stolen credential.
- **Role chaining caps the session at one hour, always.** Using one role's credentials to assume another applies the cap regardless of `MaxSessionDuration`, and a longer `DurationSeconds` is a failure, not a truncation. Long jobs that chain must re-assume, so build refresh in rather than discovering it at minute 61.
- **EC2 instance-profile credentials are not subject to `MaxSessionDuration`** and are refreshed for you. Shortening the role's max session does nothing for an instance profile — that is §7's problem, not this one.
- **Rotation is not revocation.** Deleting a key does not kill sessions already minted from it. Revoke with an explicit deny on `aws:TokenIssueTime` older than now (the console's *Revoke sessions* writes exactly this inline policy), then read CloudTrail.
- **Third-party trust needs `sts:ExternalId`.** A vendor role whose trust policy names only the vendor's account is a confused deputy: the vendor's *other* customers can ask it to act on your account. Require a secret external ID with `StringEquals`, and never accept one the vendor lets you choose to be blank.
- **Bound the org, not just the account.** `aws:PrincipalOrgID` in a resource policy prevents a copied bucket or queue policy from quietly trusting principals outside your organization.

### 6. Accounts are the blast-radius boundary — nothing smaller is

An AWS account is the only hard isolation AWS gives you. Tags are not, VPCs are not, IAM paths are not. AI produces one-account designs because examples are one-account.

- Split on **blast radius and compliance**, not on team names: prod and non-prod never share an account; a regulated data set gets its own; the security tooling, log archive and shared network accounts are separate from every workload.
- The **management account runs nothing.** No workloads, no CI role, no exceptions — SCPs do not apply to it, so any compromise there is total. Use delegated administrators for GuardDuty, Config, IAM Access Analyzer and the rest.
- **Baseline every account, including the ones nobody uses yet:** an organization CloudTrail with log-file validation, GuardDuty in every enabled Region, an SCP denying root usage, CloudTrail tampering and unused Regions, and an RCP requiring `aws:PrincipalOrgID` on your data stores.
- Account creation is easy; account *deletion* is slow and manual. Decide the layout at Gate 5, not after the first environment exists. Layout and the SCP/RCP baseline: `references/terraform-and-accounts.md`.

### 7. IMDSv2 and the hop limit — SSRF's shortest path to your role

Any server-side URL fetcher that an attacker can point at `169.254.169.254` reads the instance role's credentials, unless IMDSv2 is *required*.

- `HttpTokens=required` per instance; set the **account-level default per Region**, and enforce with `HttpTokensEnforced=enabled` (an account with enforcement on rejects a launch that specifies `HttpTokens=optional`). Precedence at launch is **launch parameter → account default → AMI (`imds-support: v2.0`)**, and an account-level `HttpPutResponseHopLimit=1` will override the AMI's intended `2`.
- **`HttpPutResponseHopLimit` defaults to 1**, which breaks containers: the extra network hop from the container to the host consumes it. Set `2` for ECS/EKS/Docker on EC2. AWS documents the container case explicitly. Do not "fix" a container that cannot reach IMDS by reverting to `optional`.
- **Posture reality:** Datadog's *State of Cloud Security* (10 Nov 2025) found roughly **one in two EC2 instances enforces IMDSv2** — up from 32% a year earlier — while **82% had used only IMDSv2 in the preceding two weeks.** For most fleets, enforcement is a config change with no functional impact, and the reason it is off is that nobody turned it on.
- Prefer no instance credentials at all where possible: IRSA / EKS Pod Identity, task roles, or `containerCredentialsFullUri` rather than the instance profile.

### 8. S3 defaults changed in April 2023 — for new buckets only

Since **April 2023** (announced 27 Apr 2023), newly created buckets in every Region get all four Block Public Access settings on and ACLs disabled (`BucketOwnerEnforced`). **Existing buckets were not changed.** An account older than that has buckets on the old defaults and nobody has audited them.

- BPA is **four independent flags** — `BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`, `RestrictPublicBuckets`. Three of four on is not on. Set it at the **account** level as well as the bucket level, so a new bucket cannot opt out.
- A bucket name is globally unique and **freed on delete**. Anyone can recreate it and serve content to every client still pointed there. Retire names, do not recycle them.
- Public-read is not the only exposure: a bucket policy with `"Principal": "*"` and a weak `Condition`, a broad `s3:*` grant to another account, and an over-permissive access point all count.

### 9. A presigned URL is unrevocable, object-level authorization

It carries the **signer's** authority, and AWS is explicit that presigned URLs *"are bearer tokens that grant access to those who possess them."* There is no per-caller check when it is used.

- Generate one per object per user **after** the ownership check. Never per prefix, never reused.
- **Lifetime is `min(configured expiry, credential lifetime)`.** An IAM user's SigV4 URL can live **7 days**; a role session's URL dies with the session (default 1 hour); an EC2 instance-profile URL dies with the rotating credential (~6 hours). Teams get surprised in both directions.
- There is no revoke. The nearest control is `s3:signatureAge` in the bucket policy — deny requests whose signature is older than N milliseconds — plus `aws:SourceIp` / `aws:SourceVpce` network-path conditions.

```json
{ "Sid": "PresignedUrlsExpireInTenMinutes", "Effect": "Deny",
  "Principal": "*", "Action": "s3:*", "Resource": "arn:aws:s3:::my-bucket/*",
  "Condition": { "NumericGreaterThan": { "s3:signatureAge": "600000" } } }
```

That converts "unrevocable forever" into "unrevocable for ten minutes," which is the difference between a leaked link in a support ticket and an incident. Signing with a short-lived role session gets you most of the way for free; the bucket policy is what covers the IAM-user path you forgot about.

### 10. Event-driven wiring — the delivery contract is not what the diagram says

Full quota and failure-mode tables: `references/service-mechanics.md`.

- **SQS → Lambda.** Set the queue's visibility timeout to **at least 6×** the function timeout (plus `MaximumBatchingWindowInSeconds` if batching); Lambda now *validates* that the function timeout is ≤ the visibility timeout and rejects the event source mapping otherwise. Without partial batch responses (`ReportBatchItemFailures`), **one bad message redelivers the entire batch** — so every handler must be idempotent, and a non-idempotent handler with a batch size of 10 reprocesses 9 good messages per failure. Set `maxReceiveCount` ≥ 5 on a redrive policy with a real DLQ.
- **Delivery is at-least-once** on SQS standard, EventBridge, and SNS. Idempotency is a design requirement, not a hardening step — the mechanism (a key, a conditional write, a dedupe table) belongs in the Gate 5 design and in `mir-backend`.
- **Payload ceilings bite silently.** Lambda: 6 MB request *and* response for synchronous invokes, 1 MB asynchronous, 200 MB for a streamed response. A batch is capped by the same 6 MB, and SQS/Lambda metadata counts toward it, so you get fewer records than your configured batch size.
- **`/tmp` is 512 MB unless you raise it** (to 10,240 MB); memory 128–10,240 MB, with one vCPU at 1,769 MB; 5 layers; 4 KB total for environment variables. Control-plane APIs are throttled at **15 rps across all of them combined** — a deploy loop that describes every function will throttle itself.
- **VPC-attaching a function consumes ENIs** from a per-VPC quota (default 500, shared with EFS). It is a capacity decision, not a checkbox.

State the delivery contract per hop in the Gate 5 design, because the code you write differs:

| Hop | Ordering | Duplicates | What the handler owes you |
|---|---|---|---|
| SQS standard → Lambda | none | yes | idempotency key; partial batch response; DLQ |
| SQS FIFO → Lambda | per message group | suppressed within the dedup window only | a message group id that actually partitions the work |
| SNS → SQS/Lambda | none | yes | idempotency, plus a DLQ on the *subscription*, not only the queue |
| EventBridge → target | none | yes | idempotency; a dead-letter queue configured on the target |
| Kinesis/DynamoDB Streams → Lambda | per shard | yes | a poison-pill policy (`BisectBatchOnFunctionError`, `MaximumRetryAttempts`) or one bad record blocks the shard |

The last row is the one AI never handles: with an ordered stream source, a record that always throws stalls its shard until the record expires.

### 11. Your multi-Region plan has one control plane — us-east-1, October 2025

This is the artifact to reach for when someone calls a design "multi-Region" and moves on. It is a provider post-incident report, which is the primary source, not a vendor blog.

**What happened.** A latent race in DynamoDB's internal DNS automation: two DNS Enactors ran concurrently, and the slower one applied a **much older plan** over the newer one; the cleanup process then deleted that plan, **removing every IP address for the regional DynamoDB endpoint**. No Enactor could apply a subsequent update, so recovery required manual operator intervention. Failure began **19 Oct 2025, 23:48 PDT**; DynamoDB recovered **20 Oct, 02:25–02:40**; full resolution **20 Oct, 13:50 PDT** — roughly **14 hours**.

**Why it did not stop at DynamoDB.** EC2's DWFM lost its droplet leases while DynamoDB was unreachable, and on recovery entered *congestive collapse* — new instance launches failed for another eleven hours. NLB then failed health checks on newly launched targets and triggered AZ failovers of its own. Lambda, ECS/EKS/Fargate, STS, IAM updates, Redshift and Connect all rode the same dependency chain.

**Why other Regions felt it.** us-east-1 hosts control planes for global surfaces — IAM propagation, the global STS endpoint, CloudFront and Route 53 control planes. A workload running healthily in eu-west-1 could still not mint credentials, change IAM, or update DNS. DynamoDB **global tables kept serving reads and writes in other Regions** — replication merely lagged — which is exactly the point: the *data plane* held and the *control plane* did not.

**What to take into the design.** (i) Name every control plane your failover *depends on*, and check which Region it lives in; a failover that requires an IAM change, a Route 53 change, or an STS call in the failed Region is not a failover. (ii) Static stability: pre-provision capacity and pre-create the standby's IAM/DNS/target groups so recovery is a data-plane action. (iii) Test the failover without the control plane available. (iv) The fix reached all Regions by 28 Oct 2025; the *class* — a stale plan overwriting a newer one, then being garbage-collected — is not AWS-specific. Check your own reconcilers for it.

Walk the failover step by step and mark each action:

| Failover step | Data plane (survives) | Control plane (may not) |
|---|---|---|
| Shift traffic | Route 53 ARC routing control, or a pre-weighted record | creating or editing a Route 53 record |
| Get credentials | an already-issued session; a regional STS endpoint | the global STS endpoint; an IAM change |
| Add capacity | instances already running / warm pool | `RunInstances`, `UpdateService`, a scaling policy |
| Read data | a global-table replica, a cross-Region read replica | promoting a replica, changing a table's replication |

If any row in your plan needs the middle column and lands in the right, the plan has a dependency on the thing that just failed. Note also the secondary reporting on this incident disagrees on the affected-service count (78 versus 140+); use the AWS message, not a blog, when you quote it.

### 12. Terraform on AWS — the v6 breaks and the state file

- **`region` is now an argument on most resources** (provider v6, 18 Jun 2025). Multi-Region no longer needs aliased providers. The trap is the reverse: a resource whose `region` differs from the provider's default now moves silently, and a module that passes `region` through is no longer Region-pinned by its provider block.
- **`aws_instance.user_data` is stored in clear text** — v6 stopped hashing it. Anything you put there is readable in state and in `terraform plan` output. AWS's guidance is blunt: do not put passwords or sensitive information in `user_data`. Use `user_data_base64` for binary payloads; that is encoding, not secrecy.
- Other v6 breaks that surface as plan failures: all 17 OpsWorks resources removed; `aws_eip.vpc` replaced by `domain = "vpc"`; `aws_api_gateway_deployment` lost `stage_name`/`stage_description`/`canary_settings`; `aws_batch_job_queue.compute_environments` → `compute_environment_order`; `aws_redshift_cluster` defaults flipped (`encrypted = true`, `publicly_accessible = false`); the `aws_ami` data source now requires `owners` or specific filters with `most_recent = true`; string booleans like `"1"` are rejected.
- **State is a secret store.** Every attribute of every resource, in plaintext: RDS passwords, generated keys, `user_data`. Encrypt the backend bucket with a CMK, block public access, enable versioning, and restrict `s3:GetObject` on the state prefix as tightly as you restrict production data.
- **One lock mechanism.** `use_lockfile = true` (Terraform ≥ 1.10, S3 conditional writes) replaces `dynamodb_table`; the DynamoDB argument is deprecated from 1.11 and still warning-only through 1.13. During migration you may set both — but finish the migration, because half the team locking one way is not locking.

```hcl
terraform {
  required_version = ">= 1.11.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.61" } }
  backend "s3" {
    bucket       = "tfstate-prod"
    key          = "platform/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    kms_key_id   = "arn:aws:kms:eu-west-1:111122223333:key/..."   # CMK, not the AWS-managed key
    use_lockfile = true                                           # not dynamodb_table
  }
}
```

The lock object needs `s3:GetObject`, `s3:PutObject` **and `s3:DeleteObject`** on `<key>.tflock`. A state-access policy written before native locking usually grants the first two only, so the lock is taken and never released.
- Pin the provider by version and modules by commit or version. See `references/terraform-and-accounts.md` for the account/Organizations layout, SCP and RCP baseline, and the plan-vs-apply review.

## How this slots into the pipeline

- **Gate 5 (Architecture Review, after sign-off):** map each workload from the pillar's Gate 0 restatement to concrete AWS services; state the account and Organizations layout; state the identity model (which roles exist, what each trusts, where `iam:PassRole` appears); state the exit cost per adopted service — DynamoDB, Aurora and the EventBridge/Step Functions glue are the ones with no equivalent elsewhere. Read `references/terraform-and-accounts.md`.

  The AWS-specific rows the design must carry before Gate 6 opens:

  ```
  ACCOUNTS   which accounts exist, what each isolates, which is the management account
  IDENTITY   every role, its trust policy, its ceiling, and where PassRole appears
  DATA       every store, its Region, its replicas, its backups, its logs
  DELIVERY   per hop: ordering, duplicates, DLQ, idempotency mechanism
  FAILOVER   each step marked data plane or control plane, with the Region of each
  GUARDRAILS budget alarm, concurrency/max-instances cap, tag policy, SCP/RCP baseline
  EXIT       per adopted service: engineering months + data transfer to leave
  ```
- **Gate 6 (Implementation):** IaC only, provider pinned, state encrypted and locked one way. Every role least-privilege with no `iam:PassRole: "*"`; IMDSv2 required with the right hop limit; BPA on at account and bucket; budget alert and concurrency cap in the same change as the resource. Read `references/iam-and-identity.md` when writing any policy.
- **Gate 7 (Production-Readiness):** the security-reviewer works §§1–9 and the Security section against the `terraform plan` output — not the `.tf` source, because a module can widen what you wrote. The reliability-reviewer works §§10–11: idempotency, DLQs, visibility timeouts, and the control-plane dependencies of the failover path. The cost review diffs the first real bill against the pillar's Gate 5 model.

## References

| File | What it holds | Read at |
|---|---|---|
| `references/iam-and-identity.md` | Full evaluation-order matrix with the resource-policy exceptions; a reviewed OIDC trust policy for both `sub` formats; `iam:PassRole` escalation pairs and the conditions that bound them; permissions boundary vs SCP vs RCP selection; condition-operator table with the `Null` pairings; session-policy and cross-account patterns; credential-exposure response | Gate 6, and Gate 7 security review |
| `references/service-mechanics.md` | Lambda quota table; SQS/SNS/EventBridge delivery semantics and the idempotency contract; event-source-mapping failure modes and DLQ wiring; S3 consistency, storage classes and bucket-policy patterns; DynamoDB item and partition limits; VPC endpoint vs NAT routing; the control-plane dependency checklist for a failover | Gate 5 design, Gate 7 reliability review |
| `references/terraform-and-accounts.md` | Provider v6 migration checklist; state backend hardening and `use_lockfile` migration; account and Organizations layout; SCP/RCP baseline including the `httpTokensEnforced` declarative policy; tagging and budget guardrails; how to read a `terraform plan` for privilege changes | Gate 6, Gate 7 |

## Security

AWS-specific security. Provider-agnostic items — OIDC over static keys as a principle, Actions pinning, secret scanning, residency enumeration — are in `mir-cloud` and `mir-devsecops`; do not restate them here.

**Evidence note.** This module deliberately carries **no CVE table.** Cloud-provider vulnerabilities are overwhelmingly remediated server-side with no customer action, so a list of them is something an engineer can do nothing about. What is actionable on AWS is a *permission combination*, a *default*, or a *dated limit* — so that is what is cited: named escalation research, the provider's own post-incident report, dated posture telemetry, and dated IaC breaks.

The review list, in the order findings actually appear:

1. **`iam:PassRole` with `Resource: "*"`, or without `iam:PassedToService`** — §2. The single highest-value grep in any AWS change.
2. **A trust policy without a specific `sub`** — §4. `repo:org/*`, a bare `aud` condition, or a `StringLike` pattern written before the 15 Jul 2026 immutable-`sub` change.
3. **A permissions boundary assumed to bound resource-policy grants** — §1. It does not. Write the explicit deny in an SCP or RCP.
4. **`HttpTokens=optional`, or hop limit 1 on a container host** — §7. Check the account-level Regional default too, not just the launch template.
5. **A bucket created before April 2023** — §8. And BPA set at bucket level only, so the next bucket is unprotected.
6. **A presigned URL with a long TTL, a prefix scope, or no `s3:signatureAge` deny** — §9.
7. **Secrets in `user_data`, in Lambda environment variables, or in state** — §12. Lambda env vars are readable by anyone holding `lambda:GetFunctionConfiguration`; use Secrets Manager or SSM `SecureString` and read at init.
8. **Long-lived IAM user access keys.** Datadog's Nov 2025 study reports the share of keys older than three years rising across every cloud. An access key that OIDC made redundant and nobody deleted is still a credential. Delete, do not deactivate-and-forget.
9. **Public data-plane endpoints** — RDS `publicly_accessible`, a public EKS API endpoint, a security group open to `0.0.0.0/0` on 22/3389. Terraform v6 flipped the Redshift defaults; nothing flipped the others.
10. **No detective floor** — CloudTrail (organization trail, log-file validation on) and GuardDuty in **every** Region, including the ones you do not use, because that is where an attacker creates resources.

**If a key or token is believed exposed:** rotate, then revoke sessions issued before the rotation (`aws:TokenIssueTime` deny, or the role's `RevokeOlderSessions`), then read CloudTrail for what the credential did. Rotation alone leaves live sessions running. Procedure: `references/iam-and-identity.md`.

## You are wiring this wrong if…

- A role's policy says `iam:PassRole` on `"*"`, or you cannot name which roles a CI principal can pass and to which service.
- Your OIDC trust policy would still match if the repository were renamed, forked, or a new branch created — or you have not looked at a real `sub` value from a workflow run.
- You call the design multi-Region but cannot name which Region each control plane it depends on lives in.
- The queue's visibility timeout is the default and the handler is not idempotent.
- The state bucket is not encrypted with a customer-managed key, or more people can read it than can read production.
- You reviewed the `.tf` files and not the `terraform plan`.
- Prod and non-prod share an account, or the management account runs a workload or holds a CI role.
- A vendor's cross-account role has no `sts:ExternalId`, or a bucket/queue policy has no `aws:PrincipalOrgID` bound.
- A stream-sourced Lambda has no poison-pill policy, so one unparseable record can stall its shard.

## Edit boundary

Four questions, in order, before adding anything here:

1. **True on GCP, Azure and Cloudflare too** (egress discipline, exit cost, residency enumeration, gate structure)? → **up** to `mir-cloud`.
2. **Is this fact used by a `mir-cloud` Gate 3 elimination or a Gate 4 ranking row** (Lambda's 900 s ceiling, NAT Gateway per-AZ cost, per-object caps, accelerator availability, egress tiers)? → **it stays in `mir-cloud`.** Cite it from here; never repeat it. A number maintained in two files becomes wrong in one of them.
3. **Identical on every provider's pipeline** (Actions pinning, SBOM, secret scanning, plan-vs-apply review)? → **across** to `mir-devsecops`. Application behaviour — handler logic, transactions, idempotency implementation — is `mir-backend`.
4. **True only because the provider is AWS** (IAM evaluation order, `iam:PassRole`, IMDS, S3 defaults, presigned URLs, event-source-mapping semantics, the AWS Terraform provider)? → **here.**

A different provider → its own `mir-cloud-<provider>` module. Never widen this one.

## Provenance

Retrieved **25 August 2026**. Sources are AWS's own documentation and post-incident report, GitHub's OIDC documentation, the Terraform AWS provider release history and v6 upgrade guide, and two named third-party research artifacts (Datadog Security Labs *pathfinding.cloud*, 17 Dec 2025; Datadog *State of Cloud Security*, 10 Nov 2025). Verify before quoting:

- IAM policy evaluation and boundaries — `docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html` and `.../access_policies_boundaries.html`
- Condition set operators — `.../reference_policies_condition-single-vs-multi-valued-context-keys.html`
- GitHub OIDC and the immutable `sub` — `docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws`
- IMDS options — `docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html`
- S3 defaults and presigned URLs — `aws.amazon.com/blogs/aws/heads-up-amazon-s3-security-changes-are-coming-in-april-of-2023/` and `docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html`
- Lambda quotas and the SQS event source mapping — `docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html` and `.../services-sqs-configure.html`
- The October 2025 event — `aws.amazon.com/message/101925/` (primary source; prefer it over any secondary account, including for the service count, which secondary sources report inconsistently)
- Terraform — `registry.terraform.io/providers/hashicorp/aws/latest/docs/guides/version-6-upgrade` and `developer.hashicorp.com/terraform/language/backend/s3`

**Quote nothing from this file you have not just confirmed at the source.** Quotas, defaults and provider versions here are a 25 Aug 2026 snapshot and are the highest-decay content in this module.
