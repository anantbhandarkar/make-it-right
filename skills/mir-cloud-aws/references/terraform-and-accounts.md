# Terraform on AWS, and the account layout underneath it

Read at **Gate 6** (before writing IaC) and **Gate 7** (plan review).

> Retrieved **25 August 2026**. Provider versions and deprecation timelines move fast; **check the release notes and the upgrade guide before acting on a version claim here.**

---

## 1. Version state

| Component | State on 25 Aug 2026 |
|---|---|
| Terraform AWS provider | **6.61.0** (19 Aug 2026). No v7 line. v6.0.0 shipped 18 Jun 2025 |
| A withdrawn release | **6.57.0** was removed from GitHub and the Registry for significant bugs; 6.57.1 supersedes it |
| v5 | critical security fixes only since v6.0.0 |
| S3 backend locking | `use_lockfile` since Terraform 1.10; `dynamodb_table` deprecated in 1.11, still warning-only through 1.13, removal announced but undated |

Pin the provider (`~> 6.61`) *and* commit `.terraform.lock.hcl`. A withdrawn release is exactly why a floating constraint plus no lock file is a reproducibility bug: the version that built production may no longer be fetchable.

---

## 2. Provider v6 migration checklist

- [ ] **`region` is now an argument on most resources.** Multi-Region no longer needs aliased providers. Audit for the reverse hazard: a module that accepts and forwards `region` is no longer pinned by the provider block above it, and a resource whose `region` changes will be **destroyed and recreated**, not moved. Diff the plan for replacements before applying.
- [ ] **`aws_instance.user_data` is no longer hashed** — it is stored in state in clear text and printed in plan output. AWS's guidance: do not put passwords or sensitive information in `user_data`. `user_data_base64` is encoding, not encryption. Bootstrap secrets from SSM/Secrets Manager at first boot instead.
- [ ] `aws_eip.vpc` removed → `domain = "vpc"`.
- [ ] `aws_api_gateway_deployment` lost `stage_name`, `stage_description`, `canary_settings`, `invoke_url`, `execution_arn` → manage `aws_api_gateway_stage` explicitly.
- [ ] `aws_batch_job_queue.compute_environments` → `compute_environment_order`.
- [ ] `aws_redshift_cluster` defaults flipped: `encrypted = true`, `publicly_accessible = false`. Read the plan — this can look like a change you did not make.
- [ ] `aws_ami` data source now requires `owners`, or specific filters when `most_recent = true`. An unowned `most_recent` AMI lookup was always a supply-chain hole; now it fails.
- [ ] EC2: `cpu_core_count` / `cpu_threads_per_core` removed → `cpu_options` block.
- [ ] Strict booleans: `"1"` / `"0"` strings are rejected.
- [ ] Removed services: all 17 OpsWorks Stacks resources, `aws_simpledb_domain`, 2 Worklink resources. Deprecated: Elastic Transcoder, CloudWatch Evidently, Kinesis Analytics (v1), MediaStore.
- [ ] ElastiCache engine values must be lowercase.
- [ ] Provider `endpoints.opsworks` / `.simpledb` / `.sdb` / `.worklink` removed.

Run the upgrade as its own change with an empty diff as the goal: `terraform plan` on the new provider against unmodified configuration should show nothing. Anything it does show is a v6 behaviour change you have not accounted for.

---

## 3. State is a secret store

Every attribute of every resource is written to state in plaintext — RDS master passwords, generated private keys, `user_data`, and any `sensitive` output (marking a value `sensitive` hides it from CLI output, not from state).

```hcl
terraform {
  required_version = ">= 1.11.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 6.61" } }

  backend "s3" {
    bucket       = "tfstate-prod"
    key          = "platform/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    kms_key_id   = "arn:aws:kms:eu-west-1:111122223333:key/xxxxxxxx"
    use_lockfile = true
  }
}
```

Bucket requirements: versioning **on** (state corruption is recoverable only from a prior version), Block Public Access on, a CMK rather than the AWS-managed key so the key policy is a second authorization layer, access logging on, and a lifecycle rule that expires old versions on a schedule you chose rather than never.

Access: **fewer people should be able to read the state bucket than can read production**, because state is a superset. Separate state per environment and per blast radius — one monolithic state file means one lock, one apply queue, and one accident.

Locking permissions: `s3:GetObject`, `s3:PutObject` **and `s3:DeleteObject`** on `<key>.tflock`. A policy written before native locking usually grants only the first two, so the lock is acquired and never released.

Migration from DynamoDB locking: set both `dynamodb_table` and `use_lockfile = true` while runners upgrade, then remove the DynamoDB argument and the table. Do not leave it half-done — two mechanisms in one team is one mechanism.

---

## 4. Account and Organizations layout

An AWS account is the only hard isolation boundary AWS offers. Tags, VPCs and IAM paths are not.

| Account | Holds | Never holds |
|---|---|---|
| Management | Organizations, SCPs/RCPs, billing | any workload, any CI role, any human day-to-day access |
| Security tooling | GuardDuty/Config/Access Analyzer delegated admin, detection pipelines | production data |
| Log archive | the organization CloudTrail bucket, write-once | anything that can delete from it |
| Shared network | Transit Gateway, shared VPCs, egress | applications |
| Workload (per env, per blast radius) | one environment of one system | another environment |

- **The management account runs nothing.** SCPs do not apply to it, so a compromise there is total. Use delegated administrators for every security service.
- Split on blast radius and compliance, not org chart. Prod and non-prod never share an account. A regulated data set gets its own.
- Account **creation** is fast; account **closure** is slow and manual. Decide the layout at Gate 5.
- Every account, including unused ones, gets the baseline below — attackers create resources in the Regions and accounts nobody watches.

### Baseline controls

| Control | Mechanism |
|---|---|
| Audit | one **organization** CloudTrail, log-file validation on, to the log-archive account |
| Threat detection | GuardDuty in **every enabled Region**, delegated admin in the security account |
| Config drift | AWS Config recorder + conformance pack, org-wide |
| External access | IAM Access Analyzer at the organization zone of trust, findings triaged |
| Root usage | SCP denying root actions; root MFA and no root access keys |
| Region fencing | SCP denying every Region you do not use (mind global services) |
| Data perimeter | RCP requiring `aws:PrincipalOrgID` on S3/KMS/STS-class resources |
| IMDS | declarative policy `httpTokensEnforced` per Region, plus account-level `HttpTokens=required` and `HttpPutResponseHopLimit=2` |
| Spend | a budget per account with an alarm that pages someone, plus per-service concurrency and `max-instances` caps |
| Tagging | a tag policy for owner / environment / cost-centre, applied at creation |

Note the difference: an **SCP** filters what principals **in** your accounts may do; an **RCP** filters what may be done **to resources in** your accounts, including by principals outside them. You need both, and RCP service coverage is partial — verify your services are supported before relying on it.

---

## 5. Reading a `terraform plan` for privilege changes

Review the **plan**, not the `.tf` files. A module can widen what its caller wrote, a variable default can be permissive, and a policy document data source resolves at plan time.

Grep the plan output for:

- `iam:PassRole` — and whether `Resource` is a wildcard, and whether `iam:PassedToService` is present.
- `"Resource": "*"` on any `iam:*`, `kms:*`, `sts:*`, `organizations:*`, `s3:*` statement.
- `AdministratorAccess`, `PowerUserAccess`, `IAMFullAccess` attachments.
- `aws_iam_role.assume_role_policy` diffs — a trust policy change is a change to *who you are*, and belongs in a separate, separately reviewed change.
- New `aws_iam_openid_connect_provider` / SAML provider objects.
- `publicly_accessible = true`, `0.0.0.0/0`, `"Principal": "*"`, `map_public_ip_on_launch`.
- Block Public Access flags flipping to `false`; `BucketOwnerEnforced` being relaxed.
- `http_tokens = "optional"` or `http_put_response_hop_limit = 1` in a launch template used by containers.
- KMS key policy edits — a key policy is the only thing standing between a key and every principal in the account.
- Anything marked `# forces replacement` on a stateful resource. On a database or a bucket, that is data loss wearing an ordinary diff.

CI shape: `plan` on the pull request with a **read-only** role, `apply` after approval with a separate write role. The plan role must not be able to apply, or the review is decoration. Post the plan to the PR so the diff a human approves is the diff that runs.

---

## 6. Guardrails that ship in the same change as the resource

Not "after the first bill":

- budget alarm with a real destination;
- reserved concurrency / `max-instances` / autoscaling maximum on anything that can scale;
- `owner`, `environment`, `cost-centre` tags via `default_tags` on the provider (note: `default_tags` does not reach every resource type — check the ones that matter);
- log retention set explicitly on every CloudWatch log group. The default is *never expire*, and forgotten log groups are a large, silent, permanent line item.
