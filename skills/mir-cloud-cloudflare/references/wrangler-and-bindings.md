# Wrangler, bindings, R2 operations, and the Terraform split

Read at **Gate 6** (before writing configuration) and **Gate 7** (security review and plan review).

> Retrieved **25 August 2026**. Wrangler ships every few days and the Terraform provider every two to three weeks; **check the release notes before acting on a version claim here.** Prices below are cross-checked against `mir-cloud/references/cost-model.md`, which owns the comparison figures — this file owns the *mechanics* that decide which figure applies.

---

## 1. Version state

| Component | State on 25 Aug 2026 |
|---|---|
| Wrangler | **4.125.0** (20 Aug 2026). v4.0.0 shipped 13 Mar 2025. v3 is the `legacy` npm dist-tag (3.114.17) |
| Terraform Cloudflare provider | **5.24.0** (24 Aug 2026). v5.0.0 shipped 29 Jan 2025 as a ground-up rewrite. The v4 line still receives patches (4.52.8, 24 Jun 2026) |
| Migration tooling | `tf-migrate` is the supported path. The in-provider `cmd/migrate` was **deprecated in 5.19.0** (24 Apr 2026); Grit-based migration is deprecated |
| State upgraders | Automatic from **5.19**, applied on `terraform plan`/`apply` |
| Config format | Cloudflare recommends **`wrangler.jsonc`** for new projects and states some newer features are JSON-config-only |

Pin both. Wrangler in `devDependencies` with a committed package lock; the provider with `~> 5.24` and a committed `.terraform.lock.hcl`. `npx wrangler` unpinned means the tool that deploys production changed between Friday and Monday and nobody chose it.

---

## 2. `wrangler.jsonc` — the fields a review actually checks

```jsonc
{
  "name": "checkout-api",
  "main": "src/index.ts",
  "compatibility_date": "2026-08-04",        // reviewed, owned, bumped in its own commit
  "compatibility_flags": [],                 // opt in early, or disable a default — both directions
  "workers_dev": false,                      // a *.workers.dev URL is a second, unprotected front door
  "observability": { "enabled": true },      // you cannot review a p99 you never collected

  "limits": {
    "cpu_ms": 50000,                         // from a measured p99, not from the 300000 maximum
    "subrequests": 200
  },

  "vars": { "LOG_LEVEL": "info" },           // plaintext configuration ONLY. Never a credential

  "r2_buckets": [
    { "binding": "ASSETS", "bucket_name": "prod-assets", "jurisdiction": "eu" }
  ],
  "kv_namespaces":   [{ "binding": "CONFIG", "id": "..." }],
  "d1_databases":    [{ "binding": "DB", "database_name": "prod", "database_id": "..." }],
  "durable_objects": { "bindings": [{ "name": "ROOM", "class_name": "Room" }] },
  "migrations":      [{ "tag": "v1", "new_sqlite_classes": ["Room"] }]
}
```

Review points, in the order they bite:

- **`compatibility_date` present and deliberate.** Absent on an API upload means `2021-11-02`. See `workers-platform-mechanics.md` §2.
- **`workers_dev`.** Leaving the `*.workers.dev` route enabled gives the Worker a public hostname outside your zone — no WAF rule bound to your domain, no Access policy, no custom-domain routing. Same class of mistake as leaving `r2.dev` on. Set it to `false` for anything with a custom route.
- **`observability`.** The p99 CPU number that `limits.cpu_ms` should be set from does not exist unless this is on.
- **Durable Object `migrations` are declarative and ordered.** Each tag applies once; the tag is the identity. Renaming a class without a migration entry orphans its storage. `new_sqlite_classes` is the only path for a new namespace — see §5 of the platform-mechanics reference.
- **`jurisdiction` on an R2 binding, and `.jurisdiction()` on a Durable Object namespace**, are the only enforced residency controls. Both are fixed at creation.
- **Environments** (`env.staging.*`) inherit some keys and override others. A binding declared only at the top level and *not* under an environment is not silently inherited for every key — diff the deployed configuration per environment rather than assuming.

---

## 3. Secrets, `vars`, and the leak that actually happens

Cloudflare's own wording:

- *"Secrets are a type of binding that allow you to attach encrypted text values to your Worker."*
- *"Secrets are environment variables. The difference is secret values are not visible within Wrangler or Cloudflare dashboard after you define them."*
- On `vars`: *"Text strings and JSON values are not encrypted and are useful for storing application configuration."*
- *"Do not use `vars` to store sensitive information in your Worker's Wrangler configuration file. Use secrets instead."*

There is **no documented read-back path** for a secret once set — `wrangler secret list` returns names only.

| Command | Behaviour |
|---|---|
| `wrangler secret put KEY` | Accepts piped stdin (`echo "v" \| wrangler secret put KEY`). **Creates a new version and deploys it immediately** |
| `wrangler secret bulk [FILE]` | JSON `{"key":"value"}` or a `.env` file. **Up to 100 secrets per request.** A `null` value deletes the key — deletion is unsupported from `.env` |
| `wrangler secret delete KEY` | Same immediate-deploy semantics |
| `wrangler secret list` | Names only. `--format json` (default) or `pretty` |
| `wrangler versions secret ...` | Creates the version **without** deploying — the right command when secret rotation must be staged |
| `wrangler deploy --secrets-file .env.production` | Uploads secrets alongside code. Secrets apply **additively**; omitted secrets are preserved |

**Local development.** Secrets for `wrangler dev` go in `.dev.vars` or `.env`. The docs say to add `.dev.vars*` and `.env*` to `.gitignore`. **Check the repo's ignore file in the review** — this is the leak that occurs in practice, not a clever attack on the platform.

**Secrets Store is open beta**, account-scoped rather than per-Worker, and unavailable on the Cloudflare China Network. It is the right destination for a credential shared across Workers; it is not something to write into a design as GA.

**Rotation is a deploy.** A Worker secret changed without a new version is not in effect. Every `wrangler secret` subcommand deploys — which is convenient and is also why rotating a secret during an incident ships whatever code is currently in the working tree if you are not careful. Use `wrangler versions secret` when you need to separate the two.

---

## 4. R2 operations — where the bill goes when egress is free

**Class A** (mutate state or list; the expensive class): `ListBuckets`, `PutBucket`, `ListObjects`, `PutObject`, `CopyObject`, `CompleteMultipartUpload`, `CreateMultipartUpload`, `LifecycleStorageTierTransition`, `ListMultipartUploads`, `UploadPart`, `UploadPartCopy`, `ListParts`, `PutBucketEncryption`, `PutBucketCors`, `PutBucketLifecycleConfiguration`.

**Class B** (read existing state): `HeadBucket`, `HeadObject`, `GetObject`, `UsageSummary`, `GetBucketEncryption`, `GetBucketLocation`, `GetBucketCors`, `GetBucketLifecycleConfiguration`.

Consequences to carry into the Gate 5 cost model:

- **A multipart upload is Class A three ways.** `CreateMultipartUpload` + one per `UploadPart` + `CompleteMultipartUpload`. A 100-part upload is **102** Class A operations. `ListParts` adds more.
- **`LifecycleStorageTierTransition` is Class A.** A lifecycle rule that moves many small objects to Infrequent Access pays a Class A operation per object to do it.
- **Infrequent Access is not the cheap tier for active data.** Storage is lower, but Class A is **2×** Standard and Class B **2.5×**, there is a per-GB retrieval fee, and there is a **30-day minimum duration** — deleting or transitioning earlier still bills 30 days. The free-tier allowances are **Standard class only**.
- Model the workload as **objects and parts written per month**, not gigabytes. The pillar's cost model has the rates; this is the operation count they multiply.

Limits that end designs:

| Limit | Value | Where documented |
|---|---|---|
| Max object size | ~5 TiB (page notes 4.995 TiB) | R2 limits |
| Max single-part upload | 5 GiB | R2 limits |
| Max parts per multipart upload | 10,000 | R2 limits **and** multipart-objects |
| Min / max part size | 5 MiB (except the last) / 5 GiB; **all parts except the last must be the same size** | **multipart-objects page only** — the limits page omits these entirely |
| Concurrent writes to the same key | 1 per second | R2 limits |
| Object key length / metadata | 1,024 bytes / 8,192 bytes | R2 limits |
| Custom domains per bucket | 100 | R2 limits |

An uploader written against the limits page alone will produce parts R2 rejects. Cite the multipart-objects page for part sizing.

---

## 5. Public access checklist

Cloudflare's wording on the development subdomain:

- *"Public access through `r2.dev` subdomains is rate-limited and should only be used for development purposes."*
- *"Avoid creating a CNAME record pointing to the `r2.dev` subdomain. This is an unsupported access path, and we cannot guarantee consistent reliability or performance."*
- *"To enable access management, cache, and bot management features, you must set up a custom domain when enabling public access to your bucket."*
- *"Disable public access to your `r2.dev` subdomain when using products like WAF or Cloudflare Access. If you do not disable public access, your bucket will remain publicly available through your `r2.dev` subdomain."*

No numeric rate limit is published. **Do not quote one.**

- [ ] Buckets are private by default — confirm nothing enabled public access "temporarily."
- [ ] Any production public bucket is served through a **custom domain** on a zone you control, so cache, WAF, bot management and Access apply.
- [ ] `r2.dev` is **disabled** on that bucket. Then fetch the `r2.dev` URL and confirm it fails — the setting and the observed behaviour are two different facts.
- [ ] No CNAME anywhere points at an `r2.dev` hostname.
- [ ] A Worker or signed URL in front of the bucket issues **one authorization per object per user, after the ownership check**. The pillar's presigned-URL rule applies unchanged: it carries the signer's authority and cannot be revoked individually.
- [ ] `workers_dev` is `false` on any Worker with a custom route — same failure shape, different product.

---

## 6. Terraform, and who owns what

### Migrating v4 → v5

v5.0.0 regenerated the provider from Cloudflare's API schema, so attribute and resource shapes changed across the board. The current path:

1. `tf-migrate migrate --dry-run --source-version v4 --target-version v5` to preview the HCL transformation, then run it without `--dry-run`. It handles **configuration only**.
2. **State migration is automatic** from provider 5.19 via built-in state upgraders, applied on the next `plan`/`apply`. You do not need Grit and you do not need `cmd/migrate`.
3. Expect benign first-plan noise — many read-only attributes render as `(known after apply)` because v5 defines them differently. **Re-plan after the first apply and require it to be empty.** Anything remaining is a real difference, not provider churn.
4. `tf-migrate` covers the common resources; coverage is expanding, and some complex resources are deliberately manual. Check the v5 stabilization tracker for the resources you actually use.

Run the migration as its **own change with an empty diff as the goal**, exactly as the AWS module says of provider v6. A plan that shows a **replacement** on a bucket, a KV namespace, or anything holding a Durable Object's storage is a data event — read it before applying, do not scroll past it.

### The credential

- Use a **scoped API token**, never the Global API Key. Scope to the account, the specific zone, and only the permission groups the plan needs. A token that can edit DNS for every zone is the blast radius of your CI runner.
- **Terraform state holds token values and binding contents in plaintext.** Encrypt the backend, enable versioning, and restrict read access as tightly as production data. The pillar's rule stands: state is a secret store.
- Whatever CI mints or holds the token gets the pillar's OIDC-over-static-keys treatment and `mir-devsecops`'s pipeline controls. Neither is repeated here.

### The ownership split

Two tools can write the same object, and the one that runs last wins silently.

| Surface | Owner | Why |
|---|---|---|
| Worker code, bindings, secrets, routes, versions | **Wrangler** | It is the deploy tool; the bindings are part of the artefact |
| Zone settings, DNS, WAF and rate-limiting rules, Access policies | **Terraform** | Long-lived infrastructure, reviewed as a plan |
| R2 buckets, KV namespaces, D1 databases, queues | **Terraform** creates; Wrangler binds by id | Creation is an infrastructure event with residency and naming consequences; binding is a deploy concern |
| Durable Object namespaces and migrations | **Wrangler** (`migrations` in config) | The migration tags are part of the code's history, not the infrastructure's |

Write the split into the repository, not just the design. Without it, a `wrangler deploy` that omits a binding Terraform created will remove it from the Worker, and the next `terraform plan` will report no drift — because from Terraform's view nothing changed.

---

## 7. Measuring before you set a limit

You cannot set `limits.cpu_ms` honestly without a number, and the number does not exist by default.

| Signal | Where it comes from | What it tells you |
|---|---|---|
| `startup_time_ms` | printed by `wrangler deploy` / `wrangler versions upload` | Distance from the 1-second upload-time ceiling. Rises with bundle size and module-scope work |
| CPU profile | emitted automatically by Wrangler when a deploy fails error 10021 | Import into Chrome DevTools or open in VS Code to find the expensive global-scope call |
| Invocation outcome | Workers Logs / Logpush — `exceededMemory`, `exceededCpu`, `ok` | Whether you are hitting 128 MB or the CPU limit, and how often |
| CPU time per request | Workers observability, p50/p99 | The input to `limits.cpu_ms`. Cloudflare's stated baseline: average Worker ~2.2 ms; auth, SSR or large-payload parsing typically 10–20 ms |
| `wrangler tail` | live log stream | Correlating a specific request with an outcome during an incident |

Set `cpu_ms` a margin above the observed p99, review it when the workload changes, and record the number in the Gate 5 design so the next reviewer knows what it was derived from. Cloudflare notes the runtime has *"some built-in flexibility"* for infrequent overruns — a Worker that consistently exceeds the limit is terminated, one that spikes occasionally may not be. That flexibility is undocumented in magnitude; do not design against it.
