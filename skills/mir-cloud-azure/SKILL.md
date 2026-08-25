---
name: mir-cloud-azure
description: "Make It Right (Azure module). Microsoft Entra identity and Azure service mechanics, loaded after the provider is chosen. Covers: app registration vs. service principal vs. managed identity, and the ~24-hour token cache that delays every role change; federated identity credentials that fail silently on a subject mismatch; RBAC scope inheritance, additive-only evaluation, and no deny assignment you can write - Azure Policy is the floor; Key Vault access policies vs. RBAC and the 2026-02-01 default flip; Storage Accounts as the misconfiguration cluster; SAS as unrevocable, unauditable authorization; Functions Flex Consumption timeouts and always-ready billing; the October 2025 Front Door outage; Terraform azurerm 5.x breaks. Chains: mir-cloud then this. TRIGGER once mir-cloud Gate 5 has settled on Azure - Bicep/ARM/Terraform, an Entra app or managed identity, a federated credential, an RBAC or Policy assignment, Key Vault, a storage account or SAS, Functions or App Service wiring, a subscription layout, or a multi-region blast-radius review. SKIP for the other providers - mir-cloud-aws, mir-cloud-gcp and mir-cloud-cloudflare each get their own module. SKIP while the provider is undecided: comparison, elimination and the cost model are mir-cloud, and loading this early biases the choice. SKIP for app code - mir-backend, mir-frontend, mir-database. SKIP for provider-agnostic pipeline controls - mir-devsecops."
trigger: /mir-cloud-azure
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-cloud-azure · Make It Right (Azure)

Bottom tier of a two-tier chain: `mir-cloud` decides **where the workload belongs** (provider-neutral, Gates 0–7) → **this** carries Azure mechanics. Reach for it at **Gate 5** (service mapping and exit cost, after the decision is signed off), **Gate 6** (IaC), and **Gate 7** (security, reliability and cost review).

**This module contributes nothing before Gate 5, and that is deliberate.** The pillar's own rule is that naming a provider before the workload is characterized turns the rest of the conversation into justification. Loading Azure mechanics during Gates 0–4 biases the elimination and the ranking toward the provider you just read about. If the provider is still open, close this and go back to `mir-cloud`.

**Comparison facts stay in the pillar.** Egress tiers, the inter-AZ change of 21 May 2024, per-object caps, accelerator availability and the exit-cost table are load-bearing for `mir-cloud` Gate 3 eliminations and Gate 4 rankings. This module cites them; it does not restate them.

**Surface state, verified 25 August 2026.**

| Thing | State | Why it matters here |
|---|---|---|
| Terraform `azurerm` provider | **5.2.0** (20 Aug 2026); v5.0.0 landed late July 2026. No v6 line | v5 removed the whole `azurerm_app_service` family and flipped the resource-provider registration default — §12 |
| A shipped-broken upgrade path | v5.0.0's storage resource-ID rework needed state migrations that only landed in **5.0.1 and 5.1.0** | Do not cross 4.x → 5.0.0 with storage resources in state. Go to ≥ 5.1.0 |
| Key Vault control plane | API **2026-02-01** makes Azure RBAC the default for *new* vaults; every earlier control-plane version **retires 27 Feb 2027** | A template that omits `enableRbacAuthorization` now creates an RBAC vault — §6 |
| Managed-identity authorization | Tokens are cached by the platform **per resource URI for around 24 hours**, and cannot be force-refreshed | A role change can take hours to take effect. This is a design constraint, not a glitch — §2 |

## The Azure footguns AI walks into most

### 1. There are two directories and two authorization systems, and they are not the same one

Microsoft Entra ID and Azure RBAC are secured independently. Microsoft states it plainly: **Entra role assignments do not grant access to Azure resources, and Azure role assignments do not grant access to Entra ID.** AI treats "Global Administrator" and "Owner" as the same idea and they are not — until someone joins them on purpose.

| Object | Lives | Grants |
|---|---|---|
| **Application object** (app registration) | one, in the **home tenant** only | nothing by itself; it is the blueprint |
| **Service principal** | one **per tenant where the app is used** | Azure RBAC role assignments and Entra API permissions attach *here* |
| **Managed identity** | a service principal with no app object, created with the resource | same, but you cannot edit it directly |
| **Entra directory role** (Global Admin, Application Admin) | the tenant | Entra objects — users, apps, credentials. Not your resources |
| **Azure RBAC role** (Owner, Contributor) | a management group / subscription / RG / resource | resources. Not the directory |

The join is `elevateAccess`: a Global Administrator can toggle *Access management for Azure resources* and be **assigned User Access Administrator at root scope (`/`)**, which lets them assign any role in every subscription and management group in the tenant. It is self-service, per-user, and persists until someone toggles it back. Treat a standing elevated-access user as a finding: the portal now shows a banner counting them, and both Entra audit logs (`Azure RBAC (Elevated Access)`) and the Azure activity log (`Microsoft.Authorization/elevateAccess/action`) record every use. Alert on it.

Two rules that follow. **Anyone who can add a credential to an app registration is that application** — a client secret, a certificate, or a federated credential are all equivalent, and Application Administrator carries that power tenant-wide. And **deleting an app registration deletes its home-tenant service principal, but restoring the app registration does not restore the service principal**; every role assignment that pointed at it is now an orphan.

### 2. Managed identity is the right answer, and its token cache is the part nobody plans for

Prefer a managed identity over an app registration with a secret whenever the workload runs on Azure. Then plan for these:

- **The authorization change you make is not the authorization that is in force.** Group and role membership travels as claims in the access token, and the managed-identity back end **maintains a cache per resource URI for around 24 hours**. Microsoft is explicit that it can take several hours for a membership change to take effect and that **you cannot force a refresh**. So: put permissions *directly* on a user-assigned identity rather than adding and removing it from an Entra group, because the group path is the one that stalls.
- **System-assigned vs. user-assigned is a lifecycle decision, not a style one.** System-assigned dies with the resource and is the right choice when you want the permissions to die with it, or when the audit log must name the specific resource. User-assigned is the right choice when the role assignment must exist *before* the resource does — a common deployment failure is that the creator can make the resource but not the role assignment. Creating many system-assigned identities quickly can hit the Entra object-creation rate limit (HTTP 429), and a deleted one **still counts against your limit until it is purged after 30 days**.
- **Role assignments are not deleted with the identity.** They linger, showing as *Identity not found* in the portal, and they count against the per-subscription role-assignment limit. Sweep for `ObjectType -eq "Unknown"`.
- **The management API throttles hard, and a wide IaC run is what finds it.** Create-or-update on user-assigned identities is limited to **10 requests/second per tenant, 2 per subscription and 0.25 per resource**, returning HTTP 429 above that. A Terraform run that fans out across many identities needs the throttle in its retry story, not in its incident report.
- **Assigning an identity to a resource hands its permissions to anyone who can run code on that resource.** Microsoft's own worked example: a user with permission to execute code in a Logic App has everything that Logic App's identity has. So `Managed Identity Operator` — the right to attach an existing identity to a new resource — is Azure's `iam:PassRole`. Grep for it, and scope it to named identities.

**And the identity endpoint is the SSRF target.** On a VM the token comes from IMDS at `http://169.254.169.254/metadata/identity/oauth2/token`, gated only by a `Metadata: true` header that Microsoft describes as "a mitigation against server side request forgery (SSRF) attacks." That mitigation stops a plain URL fetcher; it does not stop an SSRF that controls request headers. Allow-list outbound hosts at the application layer and keep the identity's roles small, because Microsoft states the blast radius directly:

> *"The security boundary of managed identities for Azure resources is the resource where the identity is used. All code/scripts running on a virtual machine can request and retrieve tokens for any managed identities available on it."*

One operational corollary: **when more than one user-assigned identity is attached, `client_id` (or `object_id` / `msi_res_id`) becomes required on the token request.** Code that worked with one attached identity breaks the moment someone attaches a second. Pass the client ID explicitly from the start.

### 3. Federated credentials — the trust perimeter, and it fails silently

Workload identity federation removes long-lived secrets from CI, which is the right move. What it leaves behind is a `subject`/`issuer` pair that is now the only thing between an external workflow and your tenant.

```json
{ "name": "gh-prod",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:octo-org/octo-repo:environment:Production",
  "audiences": ["api://AzureADTokenExchange"] }
```

- **`issuer`, `subject` and `audiences` must match the incoming token case-sensitively, and `Wildcard characters aren't supported in any federated identity credential property value.`** There is no `repo:org/*` to get wrong — but there is also no pattern to cover a rename.
- **A wrong `subject` creates cleanly and then fails with no error.** Microsoft says it twice on the same page: the credential "is created successfully without error", and at exchange time "You won't get an error, the exchange fails without error." Read the real `sub` out of a workflow run; do not compose it from the docs.
- **Prefer the environment form.** `repo:<org>/<repo>:environment:<name>` puts GitHub's environment approvals in front of the credential itself, not just the deploy step. The branch form is `repo:<org>/<repo>:ref:refs/heads/<branch>` and trusts any workflow that can cause that ref to exist. **Sources disagree on the pull-request form**: Microsoft's CLI/PowerShell/REST examples write `repo:<org>/<repo>:pull-request`, while GitHub's own claim uses `pull_request`. Do not guess — read the token.
- **Ceilings and timing:** 20 federated credentials per app or user-assigned identity; 600 characters per field; propagation is not instant, so a token request minutes after creation can fail with `AADSTS70021: No matching federated identity record found for presented assertion` — retry, do not re-create. Creating several credentials on the *same* user-assigned identity concurrently returns 409; `azurerm` ≥ 3.40.0 serializes them, ARM templates need `"mode": "serial"` or `dependsOn`.
- Scope the *identity*, not just the trust. A federated credential that only `main` can use, attached to an Owner-level principal, is still an Owner-level principal.

### 4. RBAC is additive, inherits downward, and has no `Deny` you can write

`mir-cloud`'s Security section already flags `Owner` on a CI principal. Here is why it is worse on Azure than the equivalent elsewhere.

- **Scope is a parent-child chain — management group → subscription → resource group → resource — and a role assignment applies to everything below it.** There is no narrowing at a child scope. Assigning `Reader` on a resource group under a subscription-scoped `Contributor` changes nothing: **"Azure RBAC is an additive model, so your effective permissions are the sum of your role assignments."**
- **You cannot write a deny.** Microsoft: *"You can't directly create your own deny assignments. Deny assignments are created and managed by Azure."* The only customer-reachable path is a deployment stack's deny settings, and every deny assignment is `IsSystemProtected`. There is no SCP here. **If your guardrail design assumes you can deny at the management group, it does not exist — see §5.**
- **Anything that can create a role assignment can grant itself anything.** `Microsoft.Authorization/roleAssignments/write` is the escalation primitive; `Owner` and `User Access Administrator` both carry it. Microsoft's own guidance: at most **three subscription owners**, prefer a job-function role over a privileged administrator role, and if you must assign one, scope it to a resource group or resource rather than a subscription or management group. A CI principal with `Owner` at subscription scope is a principal that can quietly re-grant itself after you narrow it.
- **Control-plane roles are data-plane roles in disguise.** `Contributor` and `Storage Account Contributor` do not grant Entra-based data access — but they include `Microsoft.Storage/storageAccounts/listkeys/action`, and the account key reads every byte in the account. "They only have Contributor" is not a data-access answer.
- Assign to groups, not users; assign by role **ID**, not name; never `*` in a custom role's `Actions`, because a future operation added under that wildcard is granted retroactively.

### 5. Azure Policy is the guardrail, and its remediation identity is a new attack surface

Because §4 leaves no writable deny, **Azure Policy is where the floor goes.** Microsoft draws the line: Azure Policy "ensures that resource state is compliant to your business rules without concern for who made the change", while "Azure RBAC focuses on managing user actions at different scopes." Even a user who is allowed to act is blocked if the result would be non-compliant.

Four things AI gets wrong about it:

- **`audit` first, `deny` second.** Microsoft recommends starting on `audit`/`auditIfNotExists` precisely because a `deny` assignment can break autoscaling and pipelines you did not think about.
- **Compliance is not real-time.** Evaluation fires on create/update, on a new or changed assignment, and otherwise on a **standard cycle once every 24 hours**. A clean dashboard means "clean as of the last scan".
- **A `deny` cannot be overridden from below.** Azure Policy is an explicit-deny system: a permissive assignment on a child management group does not loosen a parent's deny. You must add the child to `notScopes` on the parent assignment. Ceilings that bite: 200 assignments per scope, 500 policy definitions per scope, 400 exclusions per assignment.
- **`deployIfNotExists` and `modify` run as a managed identity that you must grant real permissions to**, and `User Access Administrator` is required to do that granting. That identity is a standing, automation-triggered principal with write access across the assignment scope. Enumerate it in the Gate 5 identity model like any other.

Assignments at the management group scope only evaluate resources at subscription and resource-group level — the management group itself is not a resource.

### 6. Key Vault has two authorization models and the default just changed

- **Access policies are vault-wide.** A policy granting `get` on secrets grants it on *every* secret in that vault. RBAC can scope a role to the vault, to a resource group, to a management group — or to an **individual key, secret or certificate**. That is the reason the "one vault per application" rule existed, and the reason it is now optional.
- **The default flipped.** With API version **2026-02-01** and later, a new vault defaults to `enableRbacAuthorization = true`. Existing vaults are unchanged; a vault whose property is `null` (created by an older API version) keeps using access policies. **All control-plane API versions before 2026-02-01 retire on 27 February 2027.** If you want access policies on a new vault you must now set `enableRbacAuthorization = false` explicitly in the template.
- **Whoever holds `Microsoft.KeyVault/vaults/write` can change the model.** `Contributor` on the vault can flip an RBAC vault back to access policies, or rewrite the access policies — without holding a single data-plane role. Deny that with policy, or do not grant `Contributor` on vaults.
- **Two traps in migration.** Role assignments take minutes to propagate — build retries in. And **role assignments are not preserved when a vault is recovered from soft-delete**; you must recreate every one, or the recovered vault is a vault nobody can read.

### 7. Storage Accounts are where Azure misconfiguration actually lives — and they fail as a set

Intruder's **2026 Cloud Security Index** (published 11 Aug 2026; 3,000 organizations, the 12 months to July 2026) found that **all three of Azure's most common misconfigurations are on Storage Accounts**, at closely clustered rates: *Storage Account Key Rotation Not Enabled* 67%, *Storage Account Access Keys Enabled* 66%, *Storage Account Public Network Access Enabled* 61%. The report's own reading is the actionable part: the rates cluster because **"where storage accounts aren't hardened, several controls tend to be missing at once."**

So audit them as one set, not as three findings. Per account, in order:

| Control | Setting | Why it is on the list |
|---|---|---|
| Anonymous blob access | `allowBlobPublicAccess = false` | The account setting **overrides** every container's setting. Unset reads as `null`, and Microsoft's own audit policy treats anything not explicitly `false` as non-compliant |
| Shared Key | `allowSharedKeyAccess = false` | Kills service and account SAS (both are Shared-Key signed) and every account-key path. It is also a **prerequisite for applying Entra Conditional Access to the account** |
| Public network access | `publicNetworkAccess = Disabled` + private endpoint | The default is a public endpoint on a globally resolvable name |
| Key rotation | a rotation reminder / policy, or no keys at all | The 67% finding. The strongest version of this control is having no keys in play — see the row above |

Two edges that survive all four: **the `$web` container of a static website stays publicly accessible even when anonymous access is disallowed for the account**, and the account-level change takes up to 30 seconds to propagate.

Measure before you flip. Metrics Explorer on `Transactions` filtered by `Authentication` tells you how much traffic is `Anonymous`, `Account Key` or `SAS`; the resource logs name the callers. And know the failure signature you are about to create: once anonymous access is disallowed, a client on service version 2019-12-12 or later gets **401**, an older client gets **409** — not the 404 people expect — so "it started 409-ing" is the fingerprint of this change, not of a missing blob. Detection queries and the full control set: `references/service-mechanics.md`.

### 8. A SAS is unrevocable, unauditable, object-scoped authorization

Microsoft's wording is the whole problem: **"It's not possible to audit the generation of SAS tokens."** And: "Storage doesn't track the number of shared access signatures that have been generated for a storage account, and no API can provide this detail."

- **Three kinds, and only one is revocable by identity.** A **user delegation SAS** is signed with an Entra-derived key and dies when that key is revoked or expires — use it. A **service SAS** and an **account SAS** are signed with the account key: the only revocations are regenerating the key (which breaks every other consumer) or, for a service SAS, a **stored access policy** — of which there is a **limit of five per container**.
- Issue one per object per user, **after** the ownership check, over HTTPS, read-only where possible, with a near-term expiry. Set a **SAS expiration policy** on the account so an over-long validity interval warns and lands in the logs.
- Set the start time at least 15 minutes in the past, or omit it: up to 15 minutes of clock skew in either direction is normal.
- **You are billed for what the holder does.** Write access to a blob is an invitation to upload 200 GB; read access is an invitation to download it repeatedly.
- Metrics and logs **do not distinguish the three SAS types** — the `SAS` filter reports all of them — so "our SAS traffic is fine" is not a statement about which credential signed it.

### 9. The network defaults that are public, and one that is public to other people's tenants

- **Azure SQL's "Allow Azure services and resources to access this server" is not a scoping control.** It writes a firewall rule with start and end IP `0.0.0.0`, and Microsoft states the effect: the server "allows communications from all resources inside the Azure boundary, **regardless of whether they are part of your subscription**." Any Azure customer's VM is inside that boundary. It is unchecked at creation from the portal; leave it unchecked and use a private endpoint or explicit IP rules. Import/Export and Data Sync are the two features that push people to enable it — both have documented workarounds.
- IP firewall rule changes take up to 5 minutes to take effect, which is long enough to misread a test as a pass.
- The rest of the public-by-default list — NSG rules open to `0.0.0.0/0` on 22/3389, a public AKS API server, storage and Key Vault public endpoints — is in `mir-cloud`'s Security section. Do not re-litigate it here; check it.

### 10. Functions Flex Consumption — the timeout you set is not the timeout the caller sees

Read this before designing anything HTTP-triggered on Flex Consumption. Full limits: `references/service-mechanics.md`.

- **`functionTimeout` is unbounded on Flex Consumption (default 30 minutes) — and it does not raise the HTTP ceiling.** An HTTP-triggered function has roughly **230 seconds** to respond, because of the Azure Load Balancer idle timeout. Past that the caller gets a gateway error while your function keeps running and keeps billing. The fix is the pattern, not the setting: return 202 with a status URL and do the work in a queue-triggered or Durable function.
- **"Always ready" instances are rented instances.** They bill on a baseline of provisioned memory in GB-seconds whether or not traffic arrives, there are **no free grants** in always-ready billing, and they are exempt from the maximum-instance-count ceiling — so `max instances` does not cap your spend. If you set them above zero to fix cold start, re-run `mir-cloud` Gate 4; you are no longer on serverless economics. Zone redundancy forces a minimum of two per function group.
- **App initialization times out after 30 seconds and the timeout is not configurable.** A slow container or a heavy module graph surfaces as gRPC `System.TimeoutException`, not as a clear startup error.
- **The ceiling is a shared regional quota, not your `max instances`.** Every Flex Consumption app in a subscription and region shares **250 cores (512,000 MB)** by default; 2,048 MB is one core. A high maximum instance count means nothing if the region's bucket is empty.
- Structural limits that change the design, not the config: **no deployment slots**, **no in-place migration into or out of Flex Consumption**, Linux only, Blob triggers must be the Event Grid source, and the minimum billable execution is 1,000 ms.

### 11. Your multi-region plan has one global front door — 29 October 2025

The artifact to reach for when someone calls a design resilient and moves on. Microsoft's own post-incident review, tracking ID **YKYN-BWZ** — a provider PIR, not a vendor blog.

**What happened.** Customer configuration changes were processed across **incompatible control-plane build versions**, producing metadata that hit a latent defect in the Azure Front Door edge data plane. Edge servers crashed; because AFD's internal DNS service is hosted on those same edge sites, DNS resolution failed intermittently, which is what turned an AFD fault into a global platform fault. Impact ran **15:41 UTC 29 Oct to 00:05 UTC 30 Oct 2025** — a little over eight hours.

**Why the safety system did not catch it.** The configuration protection system validated at each stage and waited for health signals, but **the data-plane crash occurred asynchronously, around five minutes after deployment**, so the bad configuration passed the safeguards before the symptom existed. Once impact was visible the system did fire, halting all new and in-flight configuration propagation at 15:43 UTC.

**What it did to the response.** The Azure Portal itself sits behind AFD and had to be failed away from it before responders could work; **services with no fallback, such as Marketplace, stayed broken**. That is the lesson that transfers: the recovery path ran through the failed dependency.

**What to take into the design.** Microsoft's own repair items are the checklist — synchronous configuration processing, extra rollout stages with longer bake time, configuration isolation in separate worker processes (Jan 2026), and *critical first-party infrastructure migrated to active-active*. Do the same to yours: name every global surface your failover depends on, and mark each step data plane or control plane.

| Failover step | Data plane (survives) | Control plane (may not) |
|---|---|---|
| Shift traffic | a pre-existing Traffic Manager / DNS record with a low TTL, already published | editing an AFD or Front Door routing configuration mid-incident |
| Authenticate | an already-issued token | a new role assignment; an Entra or ARM control-plane write |
| Add capacity | instances already running; pre-provisioned always-ready | an ARM deployment, a scale rule, a quota increase |
| Read data | a geo-replicated replica already serving | promoting a replica, changing replication topology |
| Reach the console | CLI against a regional endpoint | the Azure Portal, if it is behind the thing that failed |

Any row that needs the right-hand column depends on the thing that just failed. Note also that this was **not** the same failure as the 9 October 2025 AFD incident; Microsoft says so explicitly, and secondary write-ups conflate them.

### 12. Terraform on Azure — the v5 breaks and the state file

- **`resource_provider_registrations` now defaults to `none`** (was `legacy` in v4), and `skip_provider_registration` is gone. **This is the opposite of a regression for restricted subscriptions** — v4's automatic registration of ~60 resource providers was what produced hard 403s for principals lacking `Microsoft.Resources/subscriptions/providers/register/action`. The v5 hazard runs the other way: a configuration that silently relied on Terraform registering providers now fails at *apply* with a misleading message. The provider's own error text names it: *"API version 2019-XX-XX was not found for Microsoft.Foo"*. Register providers explicitly (`resource_providers_to_register`) or set `legacy` deliberately.
- **The entire `azurerm_app_service` family was removed in v5.0**, having carried a deprecation banner through 4.x: `azurerm_app_service` → `azurerm_linux_web_app` / `azurerm_windows_web_app`; `azurerm_app_service_plan` → `azurerm_service_plan`; `azurerm_function_app` → `azurerm_linux_function_app` / `azurerm_windows_function_app`; the `_slot` variants likewise. The matching data sources went too. Any generated config using the old names is pre-v5 and will not plan.
- Other v5 flips worth a grep: `azurerm_storage_account.allow_nested_items_to_be_public` now defaults to `false`; `min_tls_version` no longer accepts `TLS1_0`/`TLS1_1`; the `queue_properties` and `static_website` blocks became standalone resources; **`enhanced_validation` moved into `features` and now defaults to disabled**, so bad locations and resource providers are caught at apply instead of plan.
- **State is a secret store, in the clear.** The provider documents it: `azurerm_key_vault_secret` — *"All arguments including the secret value will be stored in the raw state as plain-text"* — and the same note is on `azurerm_linux_virtual_machine`/`_windows_virtual_machine` for `admin_password`. `Sensitive: true` masks plan output only. Use the write-only `value_wo` argument for secrets, and treat the state container as production data. Note that the note is **not** on every secret-bearing resource; its absence is not evidence of safety.
- **The `azurerm` backend takes an infinite blob lease** (`LeaseDuration: -1`), not an expiring one. A crashed run leaves the state blob leased with no automatic recovery — you need `terraform force-unlock` or a manual lease break. Plan for that before it happens at 03:00.

```hcl
terraform {
  required_providers { azurerm = { source = "hashicorp/azurerm", version = "~> 5.2" } }
  backend "azurerm" {
    resource_group_name  = "tfstate-rg"
    storage_account_name = "tfstateprod"
    container_name       = "state"
    key                  = "platform.tfstate"
    use_azuread_auth     = true    # OIDC / workload identity, not an account key
  }
}
provider "azurerm" {
  features {}
  resource_provider_registrations = "none"
  resource_providers_to_register  = ["Microsoft.App", "Microsoft.Storage"]
}
```

## How this slots into the pipeline

- **Gate 5 (Architecture Review, after sign-off):** map each workload from the pillar's Gate 0 restatement to concrete Azure services; state the management-group and subscription layout; state the identity model; state the exit cost per adopted service — Cosmos DB multi-region writes, Fabric and the Event Grid / Logic Apps glue are the ones with no equivalent elsewhere. Read `references/entra-and-rbac.md`.

  The Azure-specific rows the design must carry before Gate 6 opens:

  ```
  TENANCY    management groups, subscriptions, what each isolates, who holds elevateAccess
  IDENTITY   every principal: managed identity or app registration, its scope, its credential
  POLICY     the deny/audit floor, and the identity every deployIfNotExists assignment runs as
  DATA       every store, its region, its replicas, backups, logs, and its network exposure
  SECRETS    which vault, which authorization model, and what holds vault Contributor
  FAILOVER   each step marked data plane or control plane, with the global surfaces named
  GUARDRAILS budget alert, max-instances / always-ready counts, tag policy, policy assignments
  EXIT       per adopted service: engineering months + data transfer to leave
  ```
- **Gate 6 (Implementation):** IaC only, provider pinned to ≥ 5.1, state in a private container with Entra auth. Managed identity over app registrations; no `Owner` on any automation principal; `allowBlobPublicAccess`, `allowSharedKeyAccess` and `publicNetworkAccess` set explicitly on every storage account in the same change that creates it; budget alert and instance cap alongside.
- **Gate 7 (Production-Readiness):** the security-reviewer works §§1–9 and the Security section against the `terraform plan` or `az deployment what-if` output, not the source — a module can widen what you wrote. The reliability-reviewer works §§10–11: the HTTP ceiling, the regional quota, and the control-plane dependencies of the failover path. The cost review diffs the first real bill against the pillar's Gate 5 model, with always-ready baseline on its own line.

## References

| File | What it holds | Read at |
|---|---|---|
| `references/entra-and-rbac.md` | Entra object model and the two-directory boundary; managed identity selection table and the token-cache constraint; a reviewed federated-credential set with the GitHub subject forms; RBAC scope/inheritance/evaluation and why there is no writable deny; the role-assignment escalation paths and the conditions that bound them; Azure Policy effects, evaluation timing and the remediation-identity risk; Key Vault access policies vs. RBAC; credential-exposure response | Gate 6, and Gate 7 security review |
| `references/service-mechanics.md` | The storage-account control set with detection queries; SAS types, revocation and stored access policies; Functions Flex Consumption limits, billing modes and quotas; Azure SQL and network defaults; the global-surface dependency checklist for a failover; the `azurerm` v5 migration checklist and state-backend hardening | Gate 5 design, Gate 6, Gate 7 reliability review |

## Security

Azure-specific security. Provider-agnostic items — federated credentials over static secrets as a principle, Actions pinning, secret scanning, residency enumeration — are in `mir-cloud` and `mir-devsecops`; do not restate them here.

**Evidence note.** This module deliberately carries **no CVE table**, and on Azure that decision is sharper than on any other provider. Azure has the highest raw count of published cloud-provider CVEs of the four, and the overwhelming majority are remediated server-side with no customer action — several ship explicitly saying no customer action is required and exist only for transparency. A list of them is a list an engineer can do nothing with. What is actionable on Azure is a *permission combination*, a *default*, a *dated limit*, or a *silent failure mode* — so that is what is cited: the provider's own post-incident report, dated third-party posture telemetry, dated API and provider version breaks, and Microsoft's own documented warnings.

The review list, in the order findings actually appear:

1. **`Owner` or `User Access Administrator` on an automation principal** — §4. Anything holding `roleAssignments/write` can re-grant itself after you narrow it. Check the scope too: at management group, it inherits to everything.
2. **A standing elevated-access user** — §1. `elevateAccess` is self-service and persists until toggled off. Alert on `Microsoft.Authorization/elevateAccess/action`.
3. **`Managed Identity Operator`, or an identity attached to a resource people can run code on** — §2. Azure's `PassRole`. Name which identities each principal may attach.
4. **A federated credential you have not verified against a real token** — §3. A wrong subject creates cleanly and fails silently; wildcards do not exist, so a rename breaks it rather than widening it.
5. **A storage account that is not explicitly `allowBlobPublicAccess=false` *and* `allowSharedKeyAccess=false` *and* network-restricted** — §7. Audit as a set; `null` is not `false`.
6. **A service or account SAS in the design** — §8. Unrevocable without a key regeneration, unauditable at generation, and a long or prefix-wide one is an IDOR you cannot recall. Use a user delegation SAS.
7. **Azure SQL "Allow Azure services"** — §9. That is every Azure tenant, not yours.
8. **A Key Vault whose authorization model nobody stated** — §6. Access policies are vault-wide; `Contributor` can change the model without any data role; role assignments are lost on soft-delete recovery.
9. **Secrets in Terraform state, in `custom_data`, or in app settings** — §12. Use `value_wo`, a Key Vault reference, and a state container with Entra auth and tight read access.
10. **No detective floor** — an Entra diagnostic setting shipping sign-in and audit logs off-box, activity logs on every subscription, Defender for Cloud on, and a policy assignment in `audit` before you ever try `deny`.

**If a credential is believed exposed:** for an app registration, delete the credential and check for *other* credentials someone added; for a storage account key, regenerate **both** keys and accept that every service and account SAS dies with them; for a managed identity, remember that already-issued tokens live out their lifetime and the platform cache can hold authorization for hours. Then read the Entra sign-in logs for the service principal and the resource's own diagnostic logs. Procedure: `references/entra-and-rbac.md`.

## You are wiring this wrong if…

- An automation principal holds `Owner`, or you cannot name which principals can create role assignments and at what scope.
- Your guardrail plan says "deny it at the management group with RBAC." You cannot; that is Azure Policy, and it needs an assignment.
- You tightened a role assignment and expected it to take effect immediately on a managed identity, or you manage its permissions through an Entra group.
- The federated credential's subject was written from the documentation rather than read out of a workflow run.
- A storage account was reviewed for public access but not for Shared Key, or `allowBlobPublicAccess` is unset rather than `false`.
- Your download links are service SAS URLs with a multi-day expiry and no stored access policy.
- An HTTP endpoint on Flex Consumption is designed around `functionTimeout` rather than around the 230-second response ceiling.
- Always-ready instances are above zero and the Gate 4 cost comparison was never re-run — or `max instances` is being treated as a spend cap.
- The design says multi-region but the failover runs through the portal, a new role assignment, or a Front Door configuration change.
- You reviewed the `.tf` or `.bicep` source and not the `terraform plan` / `what-if` output.
- Key Vault appears in the design with no statement of which authorization model it uses.

## Edit boundary

Four questions, in order, before adding anything here:

1. **True on AWS, GCP and Cloudflare too** (egress discipline, exit cost, residency enumeration, gate structure)? → **up** to `mir-cloud`.
2. **Is this fact used by a `mir-cloud` Gate 3 elimination or a Gate 4 ranking row** (the Flex Consumption HTTP ceiling as a comparison number, egress tiers, per-object caps, accelerator availability)? → **it stays in `mir-cloud`.** Cite it from here; never repeat it. A number maintained in two files becomes wrong in one of them.
3. **Identical on every provider's pipeline** (Actions pinning, SBOM, secret scanning, plan-vs-apply review)? → **across** to `mir-devsecops`. Application behaviour — handler logic, transactions, idempotency implementation — is `mir-backend`.
4. **True only because the provider is Azure** (the Entra/RBAC split, managed-identity token caching, federated-credential matching, deny assignments, Key Vault authorization models, storage account settings, SAS semantics, Flex Consumption, the `azurerm` provider)? → **here.**

A different provider → its own `mir-cloud-<provider>` module. Never widen this one.

## Provenance

Retrieved **25 August 2026**. Sources are Microsoft's own documentation and post-incident review, the Terraform `azurerm` provider changelog, upgrade guide and resource docs read at source, and one named dated third-party telemetry artifact (Intruder, *2026 Cloud Security Index*, published 11 Aug 2026; 3,000 organizations; 12 months to July 2026). Verify before quoting:

- Entra apps and service principals — `learn.microsoft.com/entra/identity-platform/app-objects-and-service-principals`
- Managed identity selection and the token cache — `learn.microsoft.com/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations`
- Federated identity credentials — `learn.microsoft.com/entra/workload-id/workload-identity-federation-considerations` and `.../workload-identity-federation-create-trust`
- RBAC evaluation, deny assignments, elevate access — `learn.microsoft.com/azure/role-based-access-control/overview`, `.../deny-assignments`, `.../elevate-access-global-admin`, `.../best-practices`
- Azure Policy — `learn.microsoft.com/azure/governance/policy/overview`
- Key Vault authorization and the 2026-02-01 change — `learn.microsoft.com/azure/key-vault/general/access-control-default` and `.../rbac-migration`
- Storage — `learn.microsoft.com/azure/storage/blobs/anonymous-read-access-prevent`, `.../common/shared-key-authorization-prevent`, `.../common/storage-sas-overview`
- Azure SQL network access — `learn.microsoft.com/azure/azure-sql/database/network-access-controls-overview`
- Functions Flex Consumption — `learn.microsoft.com/azure/azure-functions/flex-consumption-plan` and `.../functions-scale`
- The October 2025 event — `azure.status.microsoft/status/history/?trackingId=YKYN-BWZ` (primary source; prefer it over any secondary account, several of which conflate it with the separate 9 October AFD incident)
- Terraform — `registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/5.0-upgrade-guide` and `developer.hashicorp.com/terraform/language/backend/azurerm`

**Quote nothing from this file you have not just confirmed at the source.** Quotas, defaults, provider versions and the posture percentages here are a 25 Aug 2026 snapshot and are the highest-decay content in this module. Where a claim could not be confirmed at a primary source it is not in this file; where two primary sources disagree — the pull-request subject form in §3, and the v5.0.0 release date carried as "late July 2026" because Microsoft's changelog and the GitHub tag differ by a day — the disagreement is stated rather than resolved.
</content>
</invoke>
