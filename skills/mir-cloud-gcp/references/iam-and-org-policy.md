# IAM and organization policy on Google Cloud — the depth `SKILL.md` cites

Read at **Gate 6** before writing any binding or constraint, and at **Gate 7** during the security review.

> Retrieved **25 August 2026** against Google Cloud documentation. Behaviour here is stable, but constraint coverage, managed-constraint names and enforcement versions change. **Confirm at the source before quoting a rule into a design document.**

---

## 1. The four identity permissions, and what each one really grants

| Permission / role | What it lets the holder do | What it does **not** |
|---|---|---|
| `iam.serviceAccounts.actAs` (`roles/iam.serviceAccountUser`) | attach the service account to a resource they create — VM, Cloud Run revision, function, job — so that resource's code runs as it | mint a token directly; `--impersonate-service-account` does not work with this alone |
| `roles/iam.serviceAccountTokenCreator` | mint credentials directly: `getAccessToken`, `getOpenIdToken`, `signBlob`, `signJwt`, `implicitDelegation` | need any resource at all — this is impersonation without deploying anything |
| `iam.serviceAccounts.setIamPolicy` (`roles/iam.securityAdmin`, `roles/iam.serviceAccountAdmin`) | rewrite the account's own policy — i.e. grant themselves either of the above | announce itself; it reads as an admin role, not as an impersonation role |
| `iam.serviceAccountKeys.create` | download a permanent credential for the account | expire, rotate, or be attributable to a human afterwards |

Google's own wording is the sentence to quote in a review: roles carrying `setIamPolicy` "give a user full control over a service account: The user can grant themselves permission to impersonate the service account." And on scope: "if you grant a user the Service Account Token Creator role in a Google Cloud project, the user can impersonate any service account in the Google Cloud project."

**`signBlob` and `signJwt` are impersonation.** They look like signing utilities and are not — either one lets the holder mint a self-signed JWT that exchanges for the service account's access token. Treat them exactly as you treat `getAccessToken`.

---

## 2. The escalation pairs, and the grant that bounds each

Escalation happens when a principal can (a) act as a more-privileged identity and (b) start something that runs as it. Named research: Rhino Security Labs' *Privilege Escalation in Google Cloud Platform, Part 1: IAM* (Spencer Gietzen) catalogues 15 IAM-side methods; Orca Security's *Bad.Build* (2023) covers the Cloud Build path in §4 of `SKILL.md`.

| Pair | Runs code as | Bound it with |
|---|---|---|
| `actAs` + `run.services.create` / `.update` | the Cloud Run runtime service account | `roles/run.developer` on the service, plus Service Account User **on the one runtime identity** — never `roles/run.admin` on a project for CI |
| `actAs` + `compute.instances.create` | the instance service account | a deploy identity that cannot create instances; or a dedicated node/instance SA with no project roles |
| `actAs` + `compute.instances.setMetadata` | the existing instance's service account | `constraints/compute.requireOsLogin`; deny `setMetadata` on production instances |
| `actAs` + `cloudfunctions.functions.create` / `.update` | the function service account | same pattern as Cloud Run; the runtime SA is a separate grant |
| `actAs` + `cloudscheduler.jobs.create` | the job service account | scheduler admin is not a developer role; scope it |
| `cloudbuild.builds.create` | the build service account | a user-specified build SA scoped to one repository; see `SKILL.md` §4 |
| `deploymentmanager.deployments.create` | the DM service agent, historically `roles/editor`, with no `actAs` needed | the service is in turndown — end of support 1 Apr 2026, shutdown after 30 Jun 2027. Migrate to Infrastructure Manager and delete the API enablement |

Non-`actAs` primitives to check in the same review: `iam.roles.update` (rewrite a custom role you already hold), `iam.serviceAccountKeys.create` on another identity, `iam.serviceAccounts.implicitDelegation`, and `orgpolicy.policy.set` anywhere it does not belong.

**Chained impersonation is the one nobody diagrams.** Google names it: a service account in one project with permission to impersonate a service account in another creates "a chain of impersonations across projects." Map the chain, not the single hop — `gcloud asset search-all-iam-policies` at organization scope is the only view that shows it.

---

## 3. Binding patterns in Terraform

```hcl
# Resource-scoped impersonation. The `service_account_id` argument is what makes
# this a binding ON the identity rather than across the whole project.
resource "google_service_account_iam_member" "ci_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.gh.name}/attribute.repository/my-org/my-repo"
}

# Cloud Run: two grants, deliberately separate.
resource "google_cloud_run_v2_service_iam_member" "deployer" {
  name   = google_cloud_run_v2_service.api.name
  role   = "roles/run.developer"          # create/update the service
  member = "serviceAccount:${google_service_account.deployer.email}"
}
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.api_runtime.name
  role               = "roles/iam.serviceAccountUser"   # actAs, on this identity only
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
```

Three rules that follow:

- **Prefer `_iam_member` over `_iam_binding`, and never `_iam_policy`.** `_binding` is authoritative for that role on that resource and silently removes bindings made elsewhere; `_iam_policy` is authoritative for the *whole* resource and has locked people out of their own projects.
- **Never grant a basic role.** `roles/owner`, `roles/editor`, `roles/viewer` are the wrong granularity everywhere, and Editor in particular carries most of the escalation permissions in §2.
- **The runtime identity gets no project-level roles.** Bind it on the resources it reads and writes.

---

## 4. The baseline organization-policy set

Apply in **dry-run first** — violations are audit-logged, not denied — then enforce. Set at the organization node with `roles/orgpolicy.policyAdmin`, which must not appear in any project-level binding.

| Constraint | Effect | Default |
|---|---|---|
| `iam.automaticIamGrantsForDefaultServiceAccounts` | stops the Compute Engine default SA getting `roles/editor` | enforced by default **only** for orgs created on/after 3 May 2024 |
| `iam.disableServiceAccountKeyCreation` | no new downloadable keys, and no Cloud Storage HMAC keys | same date rule |
| `iam.disableServiceAccountKeyUpload` | no uploaded external public keys | same date rule |
| `iam.managed.preventPrivilegedBasicRolesForDefaultServiceAccounts` | Editor/Owner cannot be re-added to a default SA by hand | not enforced by default |
| `iam.serviceAccountKeyExpiryHours` | caps new key lifetime; values `1h`, `8h`, `24h`, `168h`, `336h`, `720h`, `1440h`, `2160h`. Cannot merge with a parent policy | unset |
| `iam.disableCrossProjectServiceAccountUsage` | an SA cannot be attached to resources in another project | **not** enforced at any org age |
| `iam.allowedPolicyMemberDomains` | domain-restricted sharing; refuses `allUsers` and `allAuthenticatedUsers` | unset |
| `compute.skipDefaultNetworkCreation` | no default VPC, so no `default-allow-ssh` from `0.0.0.0/0` | unset |
| `compute.vmExternalIpAccess` | deny external IPs, allow-list exceptions | unset |
| `compute.requireOsLogin` · `compute.disableSerialPortAccess` | SSH under IAM; no serial console back door | unset |
| `storage.uniformBucketLevelAccess` · `storage.publicAccessPrevention` | kills object ACLs and public buckets as a class | unset |
| `sql.restrictPublicIp` · `sql.restrictAuthorizedNetworks` | no public Cloud SQL endpoint | unset |
| `gcp.resourceLocations` | where resources may be **created** — see the caveats in `SKILL.md` §11 | unset |

Inheritance: descendants inherit by default and may override with `enforce: false` or by replacing the list — but only with `orgpolicy.policyAdmin` on the descendant. `inheritFromParent: true` merges a list policy with the parent's rather than replacing it.

---

## 5. Deny policies and principal access boundaries

**IAM deny policies** are the explicit floor an allow cannot climb over: "IAM always checks relevant deny policies before checking relevant allow policies."

- Attach at organization, folder or project. **500 deny policies and 500 rules per resource.**
- Denial conditions recognize **only resource-tag functions** — no `request.time`, no IP, no principal attributes. If your control needs those, it is not a deny policy.
- `exceptionPrincipals` is the escape hatch for break-glass; keep the list short and alarm on its use.
- Changes are eventually consistent. Do not write a runbook that assumes instant effect.

The rules worth writing on day one: deny `iam.serviceAccountKeys.create` to everyone except a named break-glass group; deny `orgpolicy.policy.set` to everyone outside the platform group; deny `resourcemanager.projects.delete` and `logging.sinks.delete` broadly.

**Principal access boundaries** answer a different question — which resources a principal is *eligible* to touch at all, regardless of roles. They never grant. Limits: 1,000 policies per organization, 500 resources referenced per policy, fail-closed if they cannot be evaluated. Each is pinned to an **enforcement version** (default 4) that fixes which permissions it can block; `latest` is documented as risky because principals can lose access unexpectedly, and a new version can take up to 4 weeks to become the default. Pin the version explicitly.

**VPC Service Controls** sits beside both and addresses exfiltration rather than escalation: a service perimeter blocks cross-perimeter access to services such as Cloud Storage and BigQuery that IAM would allow. It does not cover every service, and Google states it "is not designed to enforce comprehensive controls on metadata movement." Dry-run first, always — an enforced perimeter breaks builds, log sinks and support tooling in ways no plan predicts.

---

## 6. Workload Identity Federation — a reviewed pool

```hcl
resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.gh.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # WITHOUT this the pool trusts every repository on github.com.
  attribute_condition = <<-CEL
    assertion.repository_owner_id == "123456" &&
    assertion.repository == "my-org/my-repo" &&
    assertion.ref == "refs/heads/main"
  CEL
}
```

Checklist:

- [ ] An `attribute_condition` exists. A pool without one is an open door, not a federation.
- [ ] The condition pins an **immutable** claim (`repository_owner_id`, `repository_id`) as well as the human-readable name — names can be released and re-registered.
- [ ] The grant is bound with `principalSet://…/attribute.repository/my-org/my-repo`, not `principal://…` on a wildcard and not `principalSet://…/*`.
- [ ] Prefer **direct resource access** over impersonating a service account; one fewer identity to over-grant. Use impersonation only where a product requires a service-account principal.
- [ ] `google.subject` is mapped. Up to 50 `attribute.NAME` mappings exist; map only what a condition or binding uses.
- [ ] On GKE: Workload Identity Federation for GKE enabled, and **no pod runs with `hostNetwork: true`** unless someone has justified it in writing — that flag bypasses the intercepting metadata server and hands the pod the node's identity.

---

## 7. Credential exposure — the response order

1. **Contain first, delete second.** Disable the key (`gcloud iam service-accounts keys disable`) and attach an IAM **deny policy** to the principal covering the permissions it holds. Deleting the key first destroys the timeline you are about to need.
2. **Cut live sessions.** A minted access token lives up to an hour and cannot be revoked individually — removing the bindings is what ends its usefulness. Remove them, then verify with a test call rather than assuming.
3. **Scope the blast radius** in Admin Activity logs (always on, free, retained 400 days): `SetIamPolicy` calls, new service accounts and keys, `google.iam.credentials.v1.IAMCredentials.GenerateAccessToken`, resource creation in regions you do not use, org-policy changes, log-sink changes.
4. **Look for persistence:** a new key on any service account, a new workload-identity pool or provider, an altered `attribute_condition`, a new custom role, a Cloud Run service with `allUsers` invoker, an Artifact Registry repository with an external writer, a scheduled Cloud Build trigger.
5. **Then delete the key**, and only then.

**Prevention that pays for itself:** no user-managed keys anywhere federation fits; the keys federation made redundant deleted rather than disabled; Recommender's role recommendations reviewed on a schedule; Policy Analyzer run for "who can impersonate this account" before each release. Datadog's *State of Cloud Security* (data collected September 2025) reports **55% of Google Cloud service accounts holding an access key older than one year** — those are almost always keys nobody can name an owner for.
