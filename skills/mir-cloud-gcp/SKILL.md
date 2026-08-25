---
name: mir-cloud-gcp
description: "Make It Right (GCP module). Google Cloud identity, org-policy and service mechanics, loaded after the provider is chosen. Covers: the service-account escalation graph — iam.serviceAccounts.actAs plus any deploy permission, setIamPolicy as full control; the Compute Engine default service account and roles/editor; Cloud Build's default service account as an escalation and supply-chain path; Organization Policy and IAM deny policies as the only real guardrail; Workload Identity Federation instead of service-account keys; Cloud Run billing modes and the min-instances trap; BigQuery on-demand bytes versus editions slot-hours; Pub/Sub and Eventarc delivery semantics. Chains: mir-cloud then this. TRIGGER once mir-cloud Gate 5 has settled on Google Cloud — Terraform or gcloud, an IAM policy, a service account or workload-identity pool, an organization policy constraint, Cloud Run/GKE/Cloud Build wiring, a BigQuery cost question, or a project and folder layout. SKIP for the other providers — mir-cloud-aws, mir-cloud-azure and mir-cloud-cloudflare each get their own module. SKIP while the provider is undecided: comparison, elimination and the cost model are mir-cloud, and loading this before Gate 5 biases the choice. SKIP for app code — handlers are mir-backend, UI is mir-frontend, schema is mir-database. SKIP for pipeline controls identical on every provider (action pinning, SBOM, secret scanning, plan-vs-apply review) — mir-devsecops."
trigger: /mir-cloud-gcp
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-cloud-gcp · Make It Right (Google Cloud)

Bottom tier of a two-tier chain: `mir-cloud` decides **where the workload belongs** (provider-neutral, Gates 0–7) → **this** carries Google Cloud mechanics. Reach for it at **Gate 5** (service mapping and exit cost, after the decision is signed off), **Gate 6** (IaC), and **Gate 7** (security, reliability and cost review).

**This module contributes nothing before Gate 5, and that is deliberate.** The pillar's rule is that naming a provider before the workload is characterized turns the rest of the conversation into justification. Loading Google Cloud mechanics during Gates 0–4 biases the elimination and the ranking toward the provider you just read about. If the provider is still open, close this and go back to `mir-cloud`.

**Comparison facts stay in the pillar.** Cloud Run's duration ceilings, the Premium/Standard egress tiers and the 200 GiB Standard allowance, accelerator availability, per-object caps and the exit-cost table are load-bearing for `mir-cloud` Gate 3 eliminations and Gate 4 rankings. This module cites them; it does not restate them.

**Surface state, verified 25 August 2026.**

| Thing | State | Why it matters here |
|---|---|---|
| Terraform `google` provider | **7.45.0** (18 Aug 2026). Majors have landed on **26 August two years running** — v6.0.0 26 Aug 2024, v7.0.0 26 Aug 2025 | A floating `~> 7.0` is safe today and may not be next week. Pin, and keep the lock file |
| Secure-by-default IAM | Three `iam.*` org-policy constraints are **enforced by default only for organizations created on or after 3 May 2024** | The default is a function of your *organization's* birthday, not the project's. An older org has none of them |
| Cloud Build default identity | Projects created since the **May–June 2024** rollout build as the **Compute Engine default service account**; older projects keep the **legacy** Cloud Build account | Two identities, two role sets, decided by project age. Audit which one your builds actually run as |
| Deployment Manager | End of support **1 Apr 2026**; new users blocked **30 Jun 2026**; turndown after **30 Jun 2027**. Successor: Infrastructure Manager | The classic `deploymentmanager` escalation path is closing, and any IaC still on it now needs a dated migration plan |

## The Google Cloud footguns AI walks into most

### 1. The service account is the security boundary, and `actAs` is the key to it

On AWS the escalation primitive is `iam:PassRole`. On Google Cloud it is **`iam.serviceAccounts.actAs`**, and the shape is the same: attach a more-privileged identity to something you are allowed to create, then run code as it. Google states the mechanic plainly — the permission lets a principal "attach a service account to a resource," after which code on that resource obtains the service account's credentials automatically.

`actAs` is inert alone. It escalates when paired with a create-a-compute-thing permission:

| `iam.serviceAccounts.actAs` plus… | Runs your code as | Note |
|---|---|---|
| `run.services.create` / `run.services.update` | the Cloud Run runtime service account | `roles/run.developer` grants the create permissions and **not** `actAs` — the deployer needs Service Account User on the runtime identity separately. `roles/run.admin` bundles both |
| `compute.instances.create` | the instance's service account | credentials read from the metadata server; add `compute.instances.setMetadata` and an existing VM is enough |
| `cloudfunctions.functions.create` / `.update` | the function's service account | the same shape one layer up |
| `cloudbuild.builds.create` | the build service account | §4 — this is the one with published research behind it |
| `cloudscheduler.jobs.create` | the job's service account | frequently overlooked; a scheduler is "not compute" until it is |
| `deploymentmanager.deployments.create` | the Deployment Manager service agent | historically the sharpest path, because the agent ran as `roles/editor` and needed no `actAs`. Closing — see the surface table |

Rhino Security Labs' *Privilege Escalation in Google Cloud Platform* series (Spencer Gietzen) catalogues **15 IAM-side methods** and names most of the permissions above. Its framing is the one to carry into review: these are not vulnerabilities in Google Cloud, they are vulnerabilities in how the environment was configured. Nothing here is patched server-side. **Grep every IAM binding in the change for `actAs`, `serviceAccountUser`, and `serviceAccountTokenCreator`** — and then look at the live estate, because Terraform only sees what Terraform created:

```bash
# every binding of the three escalation roles, anywhere under the org
gcloud asset search-all-iam-policies --scope=organizations/ORG_ID \
  --query='policy:(roles/iam.serviceAccountUser OR roles/iam.serviceAccountTokenCreator OR roles/iam.securityAdmin)' \
  --format='table(resource, policy.bindings.role, policy.bindings.members)'

# which of those are project- or folder-scoped rather than bound to one identity
#   -> anything whose `resource` is a project or folder is §2's problem
```

### 2. Two grants that quietly hand over every identity in a project

- **`iam.serviceAccounts.setIamPolicy` is full control of that identity.** Google's own wording: roles carrying it "give a user full control over a service account: The user can grant themselves permission to impersonate the service account." `roles/iam.securityAdmin` and `roles/iam.serviceAccountAdmin` both carry it. A principal with Security Admin on a project is, in practice, every service account in that project.
- **Token Creator granted at the project is Token Creator on everything in it.** Google again: "if you grant a user the Service Account Token Creator role in a Google Cloud project, the user can impersonate any service account in the Google Cloud project." Grant these roles **on the service account resource**, never on the project or folder. This is the single most common over-grant in generated Terraform, because `google_project_iam_member` is the easier resource to write than `google_service_account_iam_member`.

```hcl
# WRONG — project-scoped, so it covers every service account that exists or ever will
resource "google_project_iam_member" "ci" {
  project = var.project_id
  role    = "roles/iam.serviceAccountTokenCreator"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

# RIGHT — scoped to the one identity the caller may become
resource "google_service_account_iam_member" "ci_may_impersonate_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.ci.email}"
}
```

Know the difference between the two roles: `roles/iam.serviceAccountUser` grants `actAs` (attach the identity to a resource) and does **not** permit `--impersonate-service-account`. `roles/iam.serviceAccountTokenCreator` grants `getAccessToken`, `getOpenIdToken`, `signBlob`, `signJwt` and `implicitDelegation` — direct credential minting, no resource required. Full pair-by-pair table: `references/iam-and-org-policy.md`.

### 3. Default service accounts, and the date that decides your defaults

The Compute Engine default service account (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`) has historically been granted **`roles/editor` on the project** at creation. Editor on the project, attached to every VM, GKE node pool and Cloud Run revision that did not specify otherwise, is an escalation waiting for one SSRF.

Google fixed the default, but **only forward**: for organizations created **on or after 3 May 2024**, `constraints/iam.automaticIamGrantsForDefaultServiceAccounts` is enforced by default, along with `constraints/iam.disableServiceAccountKeyCreation` and `constraints/iam.disableServiceAccountKeyUpload`. An organization created before that date has **none of the three** unless somebody turned them on, and turning them on does not retract grants already made.

So the review question is not "is this a modern project" but **"when was the organization created, and what does `gcloud resource-manager org-policies describe` actually return?"** Then: set the three constraints explicitly regardless of org age, add `constraints/iam.managed.preventPrivilegedBasicRolesForDefaultServiceAccounts` so Editor/Owner cannot be re-added by hand, and give every workload a purpose-built service account rather than the default. Cross-project attachment is a separate constraint (`iam.disableCrossProjectServiceAccountUsage`) and is **not** enforced by default at any org age.

### 4. Cloud Build is a supply chain and a privilege boundary at the same time

Cloud Build runs as a service account with permission to write your images and, usually, to deploy them. That makes `cloudbuild.builds.create` a deploy permission wearing a CI label.

- **Orca Security's "Bad.Build" (2023)** is the named artifact. The Cloud Build default service account held `logging.privateLogEntries.list`, which exposed the project's full audit-log picture; combined with `cloudbuild.builds.create` — carried by several ordinary developer-flavoured roles — it let a build impersonate the build account, tamper with images in Artifact Registry, and poison anything that later pulled them. Google's fix **revoked `logging.privateLogEntries.list`** and left the Artifact-Registry write path intact, which Orca called a partial fix. Treat it as unfixed: it is a permission design, not a bug.
- **The identity moved.** Since the May–June 2024 rollout, new projects build as the Compute Engine default service account; older projects keep the legacy `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` with `roles/cloudbuild.builds.builder`. The service *agent* (`service-PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com`) is a third identity and is not the one your build steps run as. Read the build's `serviceAccount` field; do not infer it.
- **The path to close.** `cloudbuild.builds.create` → build runs as a privileged SA → writes to Artifact Registry → a deploy pulls `:latest`. Break it in three places: a **user-specified build service account** with only the repositories it needs, an Artifact Registry repository whose writer is that account and nobody else, and **Binary Authorization** requiring an attestation the build produced, with deploys pinned by digest rather than tag.

### 5. Organization Policy is the only guardrail an owner cannot argue with

IAM is additive: a project Owner can grant themselves anything IAM can grant. Three mechanisms sit outside that, and they are the reason a Google Cloud design can be bounded at all.

| Mechanism | Bounds | The catch |
|---|---|---|
| **Organization Policy** | what *can exist or be configured* — key creation, public IPs, allowed domains, allowed regions | Set it with `roles/orgpolicy.policyAdmin` **on the organization**. A descendant can override an inherited policy — via `enforce: false` or `inheritFromParent` — but only if someone holds that role on the descendant. Keep it out of project-level bindings |
| **IAM deny policies** | what a principal *may do*, ahead of every allow | "IAM always checks relevant deny policies before checking relevant allow policies." Conditions here recognize **only resource-tag functions** — not `request.time`, not IP. Limits: 500 deny policies and 500 rules per resource; changes are eventually consistent |
| **Principal access boundaries** | which *resources* a principal is eligible to touch at all | Eligibility only — a PAB never grants. Bound to an **enforcement version** (default 4) that fixes which permissions it can block; `latest` is documented as unsafe because principals can lose access unexpectedly, and a new version can take up to 4 weeks to become default. 1,000 policies per org, 500 resources per policy, fail-closed |

Add **VPC Service Controls** when the risk is exfiltration rather than escalation: a service perimeter blocks cross-perimeter reads of Cloud Storage and BigQuery that IAM would happily allow. Google is explicit that it does not support every service, and that it "is not designed to enforce comprehensive controls on metadata movement" — so it is a data boundary, not a residency proof. Roll every one of these out in **dry-run first**: violations are audit-logged and not denied, which is the only safe way to learn what your estate actually does. Baseline constraint set: `references/iam-and-org-policy.md`.

### 6. Service-account keys are the credential you were trying not to have

A downloaded `.json` key is a permanent, non-expiring, non-repudiable credential. Google's own guidance is that key authentication "introduces a non-repudiation threat" because there is no reliable way to tell who used the key, and recommends blocking creation with the org policy rather than managing them. Datadog's *State of Cloud Security* (data collected September 2025) reports **55% of Google Cloud service accounts had an access key older than one year** — between AWS IAM users at 59% and Entra ID applications at 40%.

Replace them, in this order of preference:

1. **Attached identity** — a VM, Cloud Run revision, or GKE workload with its own service account. No credential exists to leak.
2. **Workload Identity Federation for GKE** — the pool is `PROJECT_ID.svc.id.goog` and a Kubernetes ServiceAccount becomes an IAM principal directly (`principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/PROJECT_ID.svc.id.goog/subject/ns/NAMESPACE/sa/SERVICEACCOUNT`). With it on, "Pods can no longer access the Compute Engine metadata server" — which is the point. **`hostNetwork: true` bypasses the interception entirely**; audit for it, because it silently restores the node's identity to the pod.
3. **Workload Identity Federation** for anything outside Google Cloud (GitHub Actions, another cloud, on-prem). Prefer **direct resource access** over federating-then-impersonating: one fewer identity to over-grant.
4. **Keys, with an expiry**, only when nothing above fits — `constraints/iam.serviceAccountKeyExpiryHours` caps new keys at values from `1h` to `2160h`.

**The federation footgun is the attribute condition, not the mapping.** `google.subject` is required and `attribute.NAME` maps up to 50 custom claims, but a pool with no attribute condition trusts *every* identity the issuer will mint — for GitHub that is every repository on github.com. The condition, not the mapping, is the perimeter. Worked pool and condition: `references/iam-and-org-policy.md`.

### 7. The default network ships with SSH and RDP open to the internet

This one surprises people who came from AWS, where the default security group is closed to the world. A new Google Cloud project's **default VPC** is auto-mode — a subnet in *every* region — and arrives with four pre-populated ingress rules at priority 65534:

| Rule | Source | Allows |
|---|---|---|
| `default-allow-internal` | `10.128.0.0/9` | all TCP and UDP ports, plus ICMP, between anything in the default network |
| `default-allow-ssh` | **`0.0.0.0/0`** | **TCP 22** |
| `default-allow-rdp` | **`0.0.0.0/0`** | **TCP 3389** |
| `default-allow-icmp` | `0.0.0.0/0` | ICMP |

Any VM that gets an external IP is therefore reachable on 22 and 3389 from the internet the moment it boots, and `default-allow-internal` means one compromised instance sees every other one. The implied rules underneath are sane — deny ingress, allow egress — but these four sit above them.

The fix is estate-level, not per-project: enforce `constraints/compute.skipDefaultNetworkCreation` so no project gets the default network at all, then build custom-mode VPCs with the subnets you actually want. Pair it with `constraints/compute.vmExternalIpAccess` (deny by default, allow-list the exceptions), `constraints/compute.requireOsLogin` so SSH keys are IAM-controlled rather than metadata-controlled, `constraints/compute.disableSerialPortAccess`, and IAP TCP forwarding instead of public SSH. On GKE, a private cluster with an authorized-networks list on the control plane, Workload Identity Federation on, and Shielded Nodes enabled — not the defaults.

### 8. Cloud Run — the billing mode is a design decision, not a flag

The pillar owns the duration ceilings and uses them to eliminate. What belongs here is what the ceilings do not tell you.

- **Two billing modes, and one flag switches you between them.** Request-based is the default. `--no-cpu-throttling` selects **instance-based**, and `--cpu-throttling` reverts. Instance-based additionally requires at least **512 MiB** of memory. Anything that does work between requests — a background flush, a warm cache refresh, a queue drain — needs instance-based to run at all, and then bills for the whole instance lifetime.
- **GPU forces it.** Google's wording: "There are no per request fees. You must use instance-based billing to use the GPU feature." L4 needs a minimum of 4 vCPU and 16 GiB; RTX PRO 6000 Blackwell needs 20 vCPU and 80 GiB. **Zonal redundancy is on by default and costs more**; turning it off is best-effort failover, which is a reliability decision to make at Gate 5, not a cost tweak at Gate 6. `max-instances` must sit under the project's regional GPU quota or deploys fail late.
- **`min-instances > 0` is where "serverless" quietly stops.** In request-based mode idle instances bill at a reduced rate; in instance-based mode at the full rate, forever. Either way the pillar's rule applies: re-run the Gate 4 comparison, because a rented instance may be cheaper and simpler.
- **`--allow-unauthenticated` grants `roles/run.invoker` to `allUsers`.** That is a public endpoint on the internet, not "internal because it has a long URL". Block the whole class with `constraints/iam.allowedPolicyMemberDomains` (domain restricted sharing), which refuses `allUsers` and `allAuthenticatedUsers` bindings estate-wide, and put ingress behind a load balancer with Cloud Armor when it should be public.

### 9. BigQuery — the bill surprise is the pricing model, not the query

More Google Cloud budget overruns come from BigQuery than from compute, and the cause is almost always that nobody established which compute model the project is on.

- **On-demand bills bytes processed.** First 1 TiB per month is free; after that it is per TiB scanned (list price is around **$6.25/TiB** in the US — see Provenance, this figure is the one I could not confirm on Google's own rendered page). Columnar, so cost is set by the columns you select and the partitions you touch, not by rows returned. `SELECT *` on a wide table is the whole table. On-demand also caps you at roughly **2,000 concurrent slots per project**, so it is a *performance* ceiling as well as a price.
- **Editions bill slot-hours.** Standard, Enterprise and Enterprise Plus, per second with a **one-minute minimum by default**; opting into **fluid scaling** at the reservation level gives per-second with no minimum. Commitments are **regional** and cannot be moved. The two models can be mixed per project, which is how an estate ends up with both and an unexplained bill.
- **The controls that actually bound it**, and belong in the Gate 6 change rather than a follow-up: `maximum_bytes_billed` on every query path, project- and user-level custom query quotas, partition and cluster keys chosen for the actual predicates, `require_partition_filter` on large tables, and materialized views instead of a scheduled query that re-scans.

### 10. Delivery semantics — Pub/Sub and Eventarc do not do what the diagram says

| Hop | Ordering | Duplicates | What the handler owes you |
|---|---|---|---|
| Pub/Sub pull/push (default) | none | yes | an idempotency key; a dead-letter topic |
| Pub/Sub with ordering keys | per ordering key | yes | a key that actually partitions the work |
| Pub/Sub exactly-once subscription | per key, if enabled | suppressed within the ack deadline | still write the idempotent handler |
| Eventarc → target | **none** — "There is no in-order, first-in-first-out delivery guarantee" | yes | idempotency; a Pub/Sub dead-letter topic on the Standard path |
| Cloud Run push subscription | none | yes | ack inside the deadline or you get redelivered, not extended |

Numbers that change the code: ack deadline defaults to **10 s** and maxes at **600 s**; message retention defaults to **7 days** and maxes at **31**; a subscription with no activity expires after **31 days** by default; a dead-letter topic accepts **5 to 100** maximum delivery attempts. Eventarc retries with exponential backoff for **24 hours** and caps events at **1 MB** (Advanced) or **512 KB** (Standard).

Even exactly-once is narrower than it sounds — Google warns that "Pub/Sub might redeliver a message even after an acknowledgment request for the message returns successfully." Write the idempotent handler anyway; the mechanism belongs in `mir-backend`, the requirement belongs in the Gate 5 design. Eventarc's exit cost is **high** and under-estimated: the events, filters and CloudEvent contracts have no equivalent elsewhere, and they are the glue nobody prices. Quotas and wiring: `references/service-mechanics.md`.

### 11. Location is not residency — `gcp.resourceLocations` says less than it looks

The pillar makes you enumerate every data sink. Here is what Google Cloud's own location controls do and do not cover, because the constraint name reads like a residency guarantee and is not one.

- **`constraints/gcp.resourceLocations`** restricts where new resources may be *created*, using value groups such as `in:eu-locations`, `in:europe-west1-locations` or country groups like `in:de-locations`. Google's own caveats: it "controls only where resources are created," it applies to a subset of products, and it "will not be enforced on sub-resource creation for certain services, such as Cloud Storage" — so the bucket lands in the right place and something inside it may not.
- **`global` counts as a multi-region**, spread across datacenters worldwide, and Google states you cannot predict or control which ones. A resource whose location is `global` is outside your residency story, not inside it.
- **The `US` and `EU` multi-regions are multiple countries.** For a GDPR question that is usually fine; for a national-residency requirement it is not. Dual-region (two named regions) is the option that lets you name both.
- **The sinks the constraint never sees:** the `_Default` and `_Required` Cloud Logging buckets and any aggregated org-level sink, Cloud Monitoring metrics, Error Reporting, Cloud Trace, Support case attachments, and anything a third-party APM agent ships out. Set the log bucket locations explicitly and route with an org-level sink you chose, rather than inheriting `_Default`.
- Where the regime demands it, **Assured Workloads** is the product that enforces personnel and location controls as a folder-level construct; `gcp.resourceLocations` alone is not that, and saying it is in a design document is the failure mode this section exists to prevent.

### 12. One control plane, replicated globally — 12 June 2025

Reach for this when someone calls a Google Cloud design "multi-region" and moves on. It is the provider's own post-incident report, not a vendor blog.

**What happened.** An automated quota policy update containing unintended blank fields was written into Service Control's regional Spanner tables. The blank fields hit a code path with an unhandled null pointer, and the binary crash-looped. Because that metadata is designed for instant global consistency, it **replicated worldwide within seconds** — so a single bad policy row took Service Control down in every region at once. Over 50 products returned 503s, including Cloud Storage, BigQuery, Compute Engine, GKE, Cloud SQL and IAM. **12 June 2025, 10:49–13:49 PDT.**

**What to take into the design.** (i) Global-consistency metadata is a *global* blast radius; a multi-region layout does not dilute it. (ii) Ask, for each failover step, whether it needs an API call that goes through Service Control — because during this event most did. (iii) Static stability: pre-create the standby's service accounts, DNS and capacity so recovery is a data-plane action. (iv) Google's own commitments name the general fixes — feature-flag critical binaries off by default, propagate global data incrementally with validation time, and fail open rather than crash on malformed input. Check your own reconcilers against that list.

Walk the failover step by step and mark each action, the way the pillar's Gate 5 asks:

| Failover step | Data plane (survives) | Control plane (may not) |
|---|---|---|
| Shift traffic | a pre-existing global load balancer with both backends already attached | creating a backend service, editing Cloud DNS, changing a URL map |
| Get credentials | a token already minted; an attached identity still in its lease | `generateAccessToken`, a new IAM binding, an org-policy change |
| Add capacity | a MIG already running; a Cloud Run revision already warm | `instances.insert`, a new revision, a quota increase |
| Read data | a Spanner multi-region instance; an existing read replica | promoting a replica, changing a replication topology |
| Deploy a fix | an image already in the standby region's Artifact Registry | a Cloud Build run, an API enablement, a cross-region pull |

If a row in your runbook needs the middle column and lands in the right, the plan depends on the thing that just failed.

**And the deletion case.** In May 2024 a blank parameter in an internal tool gave UniSuper's GCVE Private Cloud a one-year subscription term instead of a perpetual one; at term end it was deleted — **across both geographies**, because the subscription, not the region, was the unit of deletion. Recovery came from backups held with a third party. The lesson is not "Google deletes things"; it is that **replication within one provider is not a backup**, and that Cloud Storage soft delete (on by default, 7 days, configurable 7–90) is a window, not an archive.

### 13. Terraform on Google Cloud — the yearly major and the state file

- **Majors land annually, in late August.** v6.0.0 on 26 Aug 2024, v7.0.0 on 26 Aug 2025, latest 7.45.0 on 18 Aug 2026. Pin `version = "~> 7.45"` and commit `.terraform.lock.hcl`; a `>= 7.0` constraint will cross a major on schedule.
- **v7 breaks that surface as plan failures:** `google_tpu_node` removed (use `google_tpu_v2_vm`), `google_notebooks_location` and the `google_beyondcorp_application` family removed; `enable_flow_logs` removed from `google_compute_subnetwork`; `event_type` is now required inside `event_trigger` on `google_cloudfunctions2_function`; `password_wo_version` is required when `password_wo` is set on `google_sql_user`.
- **State is a secret store.** Service-account keys, generated passwords and SQL user credentials sit in it in plaintext. Put the backend bucket in its own project, enable **uniform bucket-level access** and **public access prevention**, turn on versioning, encrypt with a CMEK, and restrict `storage.objects.get` on the state prefix as tightly as production data.
- **`google` versus `google-beta`.** Beta-only fields require the beta provider on that resource; mixing them across a module is how a resource silently loses an attribute on the next apply. Declare both explicitly or neither.
- **Terraform is not the whole surface.** `gcloud` changes and console clicks are invisible to a plan, and Google's own reconciliation (default service accounts, service agents, API enablement) creates bindings you did not write. Audit with `gcloud asset search-all-iam-policies`, not with `terraform plan`.

## How this slots into the pipeline

- **Gate 5 (Architecture Review, after sign-off):** map each workload from the pillar's Gate 0 restatement to concrete Google Cloud services; state the organization/folder/project layout; state the identity model; state the exit cost per adopted service — BigQuery, Spanner, Firestore and the Eventarc glue are the ones with no equivalent elsewhere. Read `references/service-mechanics.md`.

  The GCP-specific rows the design must carry before Gate 6 opens:

  ```
  HIERARCHY  org, folders, projects — what each isolates, and the org's creation date
  IDENTITY   every service account, what attaches it, who holds actAs/TokenCreator on it
  GUARDRAIL  the org-policy constraint set, deny policies, any VPC-SC perimeter, dry-run first
  DATA       every store, its location, its replicas, its backups, its logs and audit config
  DELIVERY   per hop: ordering, duplicates, DLQ, ack deadline, idempotency mechanism
  BILLING    Cloud Run mode per service; BigQuery model per project; min-instances stated
  EXIT       per adopted service: engineering months + data transfer to leave
  ```
- **Gate 6 (Implementation):** IaC only, provider pinned, state bucket hardened. No default service accounts on workloads; no project-scoped `serviceAccountUser`/`serviceAccountTokenCreator`; org-policy constraints applied in dry-run then enforced; budget alert, `max-instances` and `maximum_bytes_billed` in the same change as the resource. Read `references/iam-and-org-policy.md` before writing any binding.
- **Gate 7 (Production-Readiness):** the security-reviewer works §§1–7, §11 and the Security section against `terraform plan` **plus** `gcloud asset search-all-iam-policies`, because a plan cannot see bindings Terraform did not create. The reliability-reviewer works §§10 and 12: idempotency, dead-letter topics, ack deadlines, and which failover steps depend on a global control plane. The cost review diffs the first real bill against the pillar's Gate 5 model, BigQuery on its own line.

## References

| File | What it holds | Read at |
|---|---|---|
| `references/iam-and-org-policy.md` | Full escalation-pair table with the bounding grant for each; `actAs` vs Token Creator vs `setIamPolicy`; resource-scoped binding patterns in Terraform; the baseline org-policy constraint set with dry-run rollout order; IAM deny policy and principal-access-boundary syntax and limits; a reviewed Workload Identity Federation pool and attribute condition; credential-exposure response | Gate 6, and Gate 7 security review |
| `references/service-mechanics.md` | Cloud Run configuration and quota table with the billing-mode consequences; Pub/Sub and Eventarc delivery semantics, ack deadlines and DLQ wiring; BigQuery cost-control mechanics and the slot/on-demand decision; Cloud Storage defaults, soft delete and residency sinks; GKE identity and network defaults; the control-plane dependency checklist for a failover | Gate 5 design, Gate 7 reliability review |

## Security

Google Cloud-specific security. Provider-agnostic items — federation over static credentials as a principle, action pinning, secret scanning, residency enumeration — are in `mir-cloud` and `mir-devsecops`; do not restate them here.

**Evidence note.** This module deliberately carries **no CVE table.** Cloud-provider vulnerabilities are overwhelmingly remediated server-side with no customer action, so a list of them is something an engineer can do nothing about. What is actionable on Google Cloud is a *permission combination*, an *organization-policy default*, or a *dated limit* — so that is what is cited: named escalation research, the provider's own post-incident reports, dated posture telemetry, and dated provider and product changes.

The review list, in the order findings actually appear:

1. **`iam.serviceAccounts.actAs` or `roles/iam.serviceAccountUser` granted at project or folder scope** — §1. The single highest-value grep in any Google Cloud change. Bind on the service account resource.
2. **`roles/iam.serviceAccountTokenCreator` or `roles/iam.securityAdmin` on a project** — §2. Both are "every identity in this project" written in a way that does not look like it.
3. **A workload running as a default service account** — §3. Check the organization's creation date before assuming `roles/editor` is absent, and check for grants made before the constraint was enforced.
4. **`cloudbuild.builds.create` treated as a CI permission** — §4. It is a deploy permission. Name the build service account, scope the Artifact Registry writer, and deploy by digest under Binary Authorization.
5. **An org-policy constraint set at project level, or `roles/orgpolicy.policyAdmin` in a project binding** — §5. That is the guardrail granting permission to remove itself.
6. **A user-managed service-account key** — §6. `constraints/iam.disableServiceAccountKeyCreation` plus deletion of the keys federation already made redundant. Deactivating is not deleting.
7. **A workload-identity pool with no attribute condition**, or `hostNetwork: true` on a GKE pod that bypasses the intercepting metadata server — §6.
8. **The default VPC still present, or `default-allow-ssh` / `default-allow-rdp` untouched** — §7. Open to `0.0.0.0/0` on 22 and 3389 the day the project is created.
9. **`--allow-unauthenticated` / `allUsers` on a Cloud Run service, an `allUsers` bucket binding, a Cloud SQL public IP, or a public GKE control-plane endpoint** — §8. `constraints/iam.allowedPolicyMemberDomains` blocks the first two as a class.
10. **No `maximum_bytes_billed` and no custom query quota** — §9. On-demand BigQuery has no natural ceiling; one bad `SELECT *` is a four-figure line.
11. **`gcp.resourceLocations` presented as a residency guarantee** — §11. It bounds creation, not storage, and never sees your log buckets, metrics or traces.
12. **Data Access audit logs left off.** Admin Activity, System Event and Policy Denied are always on and free; "Except for BigQuery, Data Access audit logs are disabled by default because they can generate large volumes of data." Enable them for your data services deliberately, and price the ingestion — the reason they are off is volume, not oversight.

**If a service-account key or token is believed exposed:** disable the key and add an IAM deny policy on the principal *before* deleting anything, then read Admin Activity logs for what it did — new keys, new bindings, `setIamPolicy` calls, resources in regions you do not use — then delete the key. Deleting first destroys the timeline. Procedure: `references/iam-and-org-policy.md`.

## You are wiring this wrong if…

- A binding grants `roles/iam.serviceAccountUser` or `roles/iam.serviceAccountTokenCreator` on a project, and you cannot name which identities the caller can therefore become.
- A VM, Cloud Run revision or node pool runs as a default service account, or you cannot say whether your organization predates 3 May 2024.
- Your builds run as an account you did not choose, or a deploy pulls a mutable tag from Artifact Registry with no attestation.
- The design's only guardrail is IAM, so a project Owner can undo it — no organization policy, no deny policy, no perimeter.
- A service-account JSON key exists in CI, in a Kubernetes Secret, or anywhere at all, and Workload Identity Federation would have fit.
- A Cloud Run service does work between requests and is on request-based billing, or has `min-instances > 0` and is still described as serverless in the cost model.
- Nobody can say which BigQuery compute model each project is on, and no query path sets `maximum_bytes_billed`.
- The project still has its default VPC, or a VM has an external IP and nobody removed `default-allow-ssh`.
- "EU multi-region" is written in the residency section as if it named a country, or the log buckets were never located deliberately.
- The multi-region plan does not name a single failover step that would have worked on 12 June 2025.
- Backups live only inside the same provider, or Cloud Storage soft delete is being counted as the retention policy.

## Edit boundary

Four questions, in order, before adding anything here:

1. **True on AWS, Azure and Cloudflare too** (egress discipline, exit cost, residency enumeration, gate structure)? → **up** to `mir-cloud`.
2. **Is this fact used by a `mir-cloud` Gate 3 elimination or a Gate 4 ranking row** (Cloud Run's duration ceilings, Premium/Standard egress tiers, per-object caps, accelerator availability)? → **it stays in `mir-cloud`.** Cite it from here; never repeat it. A number maintained in two files becomes wrong in one of them.
3. **Identical on every provider's pipeline** (action pinning, SBOM, secret scanning, plan-vs-apply review)? → **across** to `mir-devsecops`. Application behaviour — handler logic, transactions, idempotency implementation — is `mir-backend`.
4. **True only because the provider is Google Cloud** (`actAs` and the escalation graph, default service accounts, Organization Policy, Cloud Build identity, Cloud Run billing modes, BigQuery pricing models, the `google` Terraform provider)? → **here.**

A different provider → its own `mir-cloud-<provider>` module. Never widen this one.

## Provenance

Retrieved **25 August 2026**. Sources are Google Cloud's own documentation and incident reports, the Terraform `google` provider release history and v7 upgrade guide, and three named third-party artifacts: Orca Security's *Bad.Build* (2023), Rhino Security Labs' *Privilege Escalation in Google Cloud Platform, Part 1: IAM* (Spencer Gietzen), and Datadog's *State of Cloud Security* (data collected September 2025). Verify before quoting:

- Service-account permissions and best practice — `cloud.google.com/iam/docs/service-account-permissions` and `.../best-practices-service-accounts`
- The `iam.*` org-policy constraints and the 3 May 2024 default — `cloud.google.com/resource-manager/docs/organization-policy/restricting-service-accounts`
- Deny policies and principal access boundaries — `cloud.google.com/iam/docs/deny-overview` and `.../principal-access-boundary-policies`
- Workload Identity Federation — `cloud.google.com/iam/docs/workload-identity-federation` and `.../kubernetes-engine/docs/concepts/workload-identity`
- Cloud Build identity change — `cloud.google.com/build/docs/cloud-build-service-account-updates`
- Cloud Run billing modes and GPU — `cloud.google.com/run/docs/configuring/billing-settings` and `.../configuring/services/gpu`
- Pub/Sub and Eventarc — `cloud.google.com/pubsub/docs/subscription-properties` and `cloud.google.com/eventarc/docs/overview`
- Audit logs — `cloud.google.com/logging/docs/audit`
- The 12 June 2025 event — `status.cloud.google.com/incidents/ow5i3PPK96RduMcb1SsW` (primary source; prefer it over any secondary account)
- Terraform — `registry.terraform.io/providers/hashicorp/google/latest/docs/guides/version_7_upgrade`

**Two figures I could not confirm at the source and you should re-check first:** the BigQuery on-demand rate (~$6.25/TiB, US) and the Cloud Run per-vCPU-second rates — both pricing pages render their tables client-side and did not resolve for automated retrieval, so those numbers come from third-party aggregation dated April 2026, not from Google. Everything else on this page was read from the primary source listed above.

**Quote nothing from this file you have not just confirmed at the source.** Quotas, defaults, org-policy behaviour and provider versions here are a 25 Aug 2026 snapshot and are the highest-decay content in this module.
