# Azure service mechanics — storage controls, SAS, Functions limits, network defaults, and the `azurerm` provider

Read at **Gate 5** when mapping workloads to services, at **Gate 6** when writing IaC, and at **Gate 7** for the reliability review.

> Retrieved **25 August 2026** from Microsoft documentation, the Terraform `azurerm` changelog and upgrade guide, and one dated third-party telemetry artifact (Intruder, *2026 Cloud Security Index*, 11 Aug 2026). Quotas and defaults move; several below are soft. **Re-check the service's own page before a number enters a design.**
>
> Cross-provider comparison numbers — egress tiers, per-object caps, duration ceilings used as Gate 3 eliminators, accelerator availability — live in `mir-cloud/references/provider-decision-tables.md` and `cost-model.md`. They are not repeated here.

---

## 1. The storage account control set

Intruder's 2026 index found Azure's three most common misconfigurations are all Storage Account controls, at 67% / 66% / 61% of accounts — clustered because, in the report's words, *"where storage accounts aren't hardened, several controls tend to be missing at once."* Treat the four rows below as one audit, per account.

| Property | Set to | Notes |
|---|---|---|
| `allowBlobPublicAccess` | `false` | The account setting **overrides every container's setting**. Unset reads as `null`, and Microsoft's own audit policy is written as `not(field equals "false")` — so `null` is non-compliant, not "fine" |
| `allowSharedKeyAccess` | `false` | Not set by default and returns no value until you set it; the account permits Shared Key when it is `null` **or** `true` |
| `publicNetworkAccess` | `Disabled` + private endpoint | The default is a public endpoint on a globally resolvable name |
| key rotation | a rotation policy / reminder, or no keys in play | The 67% finding. Disabling Shared Key is the stronger form of this control |

### Disallowing anonymous access

- Container levels are `Private` (default), `Blob` (blobs readable anonymously, no enumeration) and `Container` (blobs plus enumeration). The account setting wins over all of them.
- **`$web` is the exception.** *"Disallowing anonymous access for a storage account does not affect any static websites hosted in that storage account. The `$web` container is always publicly accessible."*
- Propagation takes **up to 30 seconds**.
- Response codes after the change, which is how you will recognise the outage you just caused: service version 2019-12-12 or later (bearer challenge supported) → **401**; older client with anonymous disallowed on the account → **409**; older client with anonymous allowed but the container private → **404**.
- `Set Container ACL` does **not** support Entra authorization — that operation still needs the account key or a SAS, which is its own argument for turning containers private and leaving them that way.

### Disallowing Shared Key

- Blocks service SAS and account SAS entirely (both are Shared-Key signed) across every storage service. **A user delegation SAS still works**, because it is Entra-authorized.
- **It is a prerequisite for Entra Conditional Access on the account.** *"To protect an Azure Storage account with Microsoft Entra Conditional Access policies, you must disallow Shared Key authorization for the storage account."*
- Known breakage: **Azure Cloud Shell** persists files in an Azure file share and those become inaccessible; the portal uses Shared Key for Azure Files by default. Move Files workloads to their own account, or grant the Files RBAC roles first.
- The Azure Policy is **Storage accounts should prevent shared key access**. Run it on `Audit`, fix, then switch the effect to `Deny`. Policy effect changes take up to 30 minutes.

### Detection before you flip anything

```kusto
// which accounts are exposed — Azure Resource Graph
resources
| where type =~ 'Microsoft.Storage/storageAccounts'
| extend pub  = parse_json(properties).allowBlobPublicAccess,
         key  = parse_json(properties).allowSharedKeyAccess,
         net  = parse_json(properties).publicNetworkAccess
| project subscriptionId, resourceGroup, name, pub, key, net
```

```kusto
// who is still using the credentials you are about to remove — Log Analytics
StorageBlobLogs
| where TimeGenerated > ago(7d) and AuthenticationType in ("AccountKey", "SAS", "Anonymous")
| summarize count() by AuthenticationType, CallerIpAddress, UserAgentHeader, AccountName
| top 20 by count_ desc
```

Metrics Explorer: `Transactions`, aggregation `Sum`, filter `Authentication` in {`Account Key`, `SAS`, `Anonymous`}. Note that **metrics and logs do not distinguish the three SAS types** — the `SAS` value covers all of them.

---

## 2. SAS — types, revocation, and what cannot be audited

| Type | Signed with | Authorized as | Revoked by | Stored access policy? |
|---|---|---|---|---|
| **User delegation SAS** (Blob, Queue, Table, Files) | a user delegation key derived from Entra credentials | Microsoft Entra ID | revoking the delegation key, or removing the signer's role | no — must be ad hoc |
| **Service SAS** (one service) | the account key | Shared Key | regenerating the account key, or the stored access policy | yes |
| **Account SAS** (several services, plus service-level ops) | the account key | Shared Key | regenerating the account key | no — must be ad hoc |

The two sentences that decide the design:

> *"It's not possible to audit the generation of SAS tokens. Any user that has privileges to generate a SAS token, either by using the account key, or via an Azure role assignment, can do so without the knowledge of the owner of the storage account."*

> *"Storage doesn't track the number of shared access signatures that have been generated for a storage account, and no API can provide this detail."*

Rules that follow:

- **Prefer a user delegation SAS.** Generating one needs `Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey` — a role assignment you can see, unlike possession of an account key.
- **One SAS per object per user, issued after the ownership check.** Never per prefix, never reused.
- **Stored access policies are the only revocation for a service SAS short of key regeneration — and there is a limit of five per container.** That ceiling is why "give every tenant its own policy" does not work.
- Set a **SAS expiration policy** on the account: a longer-than-recommended validity interval then warns at generation and lands in the logs.
- **Clock skew is up to 15 minutes in either direction.** Set the start time at least 15 minutes in the past, or omit it entirely.
- **You are billed for the holder's usage.** Write access invites a 200 GB upload; read access invites repeated download. Short expiry plus least privilege is a cost control as well as a security one.
- Track issuance yourself with the `sip` (signed IP), `st` (start) and `se` (expiry) fields — *"There is no direct way to identify which clients have accessed a resource."*
- A download already in progress when the URL expires continues; a resumed download after expiry fails.

---

## 3. Functions on the Flex Consumption plan

| Limit | Value | Consequence |
|---|---|---|
| Instance memory | 512 MB (0.25 core), 2,048 MB (1 core), 4,096 MB (2 cores); +272 MB platform buffer, unbilled | 2,048 MB is the documented default choice. HTTP trigger concurrency defaults follow instance size |
| `functionTimeout` | default 30 min, maximum **unlimited** | Does **not** apply to the HTTP response — next row |
| HTTP response ceiling | ~**230 seconds**, from the Azure Load Balancer idle timeout | Applies on every plan. Past it the caller gets a gateway error while the function keeps running and billing. Use 202 + polling, or Durable Functions |
| Max scale-out | 1 minimum, **1,000** maximum on-demand instances | Applies **per independently-scaling function group**, not per app |
| Always-ready instances | default 0; minimum 2 per group when zone redundancy is on | Billed on provisioned memory (GB-s) whether or not traffic arrives; **no free grants**; **exempt from the max-instance ceiling**, so `max instances` is not a spend cap |
| Regional subscription quota | **250 cores (512,000 MB)** per region per subscription, shared by all Flex Consumption apps | Apps at zero, or scaling in, do not count; **always-ready instances do count**. Raisable on capacity review |
| App initialization | **times out after 30 seconds and is not configurable** | Surfaces as gRPC `System.TimeoutException`, not a clear startup error |
| Scale-in grace period | 60 minutes on scale-in; 10 minutes on platform updates | Long-running work survives a scale-in, not a redeploy |
| Minimum billable execution | 1,000 ms, then rounded up to the nearest 100 ms | Sub-second functions do not bill sub-second |

Structural constraints that change the architecture rather than the config:

- **No deployment slots.** Zero-downtime is rolling updates (public preview at time of writing), not slot swaps.
- **No in-place migration** into or out of Flex Consumption — you create a new app and redeploy.
- **One app per plan.** Linux only; the C# in-process model is unsupported.
- **Blob triggers must use the Event Grid source**, and non-C# apps need extension bundle `[4.0.0, 5.0.0)` or later.
- Virtual network integration requires the **`Microsoft.App`** resource provider registered in the subscription, with subnet delegation `Microsoft.App/environments`. This is exactly the case that the `azurerm` v5 registration default (§5) stops doing for you.
- Durable Functions storage providers are limited to Azure Storage and the Durable Task Scheduler. `WEBSITE_TIME_ZONE` / `TZ` are unsupported. NFS file shares are unsupported; SMB mounts authenticate with an **account key**, which conflicts with §1's `allowSharedKeyAccess = false` — decide which one wins before Gate 6.
- The scale-out *rate* is platform-managed and decelerates as instance count grows. Do not design against a per-minute figure; pre-provision always-ready capacity for a known burst instead.

---

## 4. Network defaults

- **Azure SQL — "Allow Azure services and resources to access this server."** Writes a server-level firewall rule with start and end IP `0.0.0.0`. Microsoft: the server *"allows communications from all resources inside the Azure boundary, **regardless of whether they are part of your subscription**."* Unchecked by default when created from the portal. Leave it unchecked; use a private endpoint, a virtual-network rule, or explicit IP rules.
  - The two features that push people to enable it are **Import/Export Service** and **Data Sync**. Both have documented alternatives: run SqlPackage from a VM in your VNet, or add explicit rules for the `Sql.<Region>` service tag of the region hosting the hub database.
  - **IP firewall rule changes take up to 5 minutes to take effect.** A test run inside that window is not a result.
  - Server-level rules apply to every database; database-level rules exist but are T-SQL only (`sp_set_database_firewall_rule`).
- Service tags (`Sql`, `SqlManagement`, and the rest) are usable in NSGs, Azure Firewall and UDRs, and are segmented by region. They describe Azure's addresses, not your trust boundary.
- The rest of the public-by-default list — NSG rules open to `0.0.0.0/0` on 22/3389, a public AKS API server, a storage or Key Vault public endpoint — lives in `mir-cloud`'s Security section. Check it; do not restate it.

---

## 5. Terraform `azurerm` — v5 migration and the state backend

Current line at time of writing: **5.2.0 (20 Aug 2026)**. v5.0.0 landed late July 2026 — the changelog heads the section 27 July and the GitHub tag reads 28 July; both are primary and they disagree by a day. There is no v6 line. The last 4.x release is 4.81.0 (14 Jul 2026).

**Do not cross 4.x → 5.0.0 with storage resources in state.** v5.0 reworked storage resource IDs (`resource_manager_id` → `id`; `storage_account_name` → `storage_account_id` on containers, queues, shares and tables) and shipped it broken: state migrations fixing the 4.x → 5.x upgrade path for `azurerm_storage_queue`, `azurerm_storage_table_entity`, `azurerm_storage_container` and `azurerm_storage_share` landed across **5.0.1 and 5.1.0**. Go to ≥ 5.1.0.

### Migration checklist

| Change | What breaks | Do |
|---|---|---|
| `resource_provider_registrations` defaults to **`none`** (was `legacy`); `skip_provider_registration` removed | Configs that relied on Terraform registering ~60 providers now fail at **apply**, with a misleading message — the provider's own text: *"API version 2019-XX-XX was not found for Microsoft.Foo"* | List what you need in `resource_providers_to_register`, or set `legacy` deliberately. Note this default **fixes** restricted subscriptions: v4's auto-registration is what produced hard 403s for principals lacking `Microsoft.Resources/subscriptions/providers/register/action` |
| `azurerm_app_service`, `_app_service_plan`, `_function_app`, `_app_service_slot`, `_function_app_slot`, `_app_service_active_slot`, `_app_service_hybrid_connection`, `_app_service_source_control_token` **removed**, plus the matching data sources | Any config or generated snippet using them will not plan | `azurerm_linux_web_app` / `azurerm_windows_web_app`; `azurerm_service_plan`; `azurerm_linux_function_app` / `azurerm_windows_function_app`; and the `_slot` equivalents |
| `enhanced_validation` moved into `features` and now **defaults to disabled** | Bad locations and resource providers are caught at apply, not plan | Re-enable it, or opt into `features.enhanced_validation.preflight_enabled` (supported on a short resource list; unsupported resources are silently skipped) |
| `azurerm_storage_account.allow_nested_items_to_be_public` now defaults to `false`; `min_tls_version` rejects `TLS1_0`/`TLS1_1`; `queue_properties` and `static_website` blocks removed | Plan diffs on accounts you did not intend to change | Set the values explicitly; move to the standalone queue-properties and static-website resources |
| Retired-service removal wave (PostgreSQL single server, HPC Cache, Orbital, Redis Enterprise, Static Site) and renames — `azurerm_redis_enterprise_database` → `azurerm_managed_redis_database`, `azurerm_ai_services` → `azurerm_cognitive_account` | Plan failures on modules you did not write | Read the 5.0 upgrade guide's removal list against your module tree before upgrading |
| Case-sensitive resource-ID validation widened (API Management, Data Factory, EventGrid, CDN FrontDoor) | IDs that used to pass now fail validation | Fix the casing; do not lowercase blindly |

There is no documented minimum Terraform core version for `azurerm` v5 — the guide and README say only "use the latest", and the provider protocol did not change. Treat any specific floor you see quoted as unverified.

### State

- **Secrets are in state in plaintext.** The provider documents it on `azurerm_key_vault_secret` (*"All arguments including the secret value will be stored in the raw state as plain-text"*) and on `azurerm_linux_virtual_machine` / `_windows_virtual_machine` for the administrator login and password. `Sensitive: true` on `admin_password` and `custom_data` redacts CLI and plan output only — the value is still written to state.
  - Use the write-only **`value_wo`** argument (with `value_wo_version` as the update trigger) on `azurerm_key_vault_secret`; write-only arguments are not persisted to state.
  - **The plaintext note is not on every secret-bearing resource.** Its absence is not evidence of safety — assume any resource handling secret material writes it to state.
- **Locking is an infinite blob lease.** The `azurerm` backend ships in Terraform core, not the provider. `Lock` proposes a UUID lease ID and calls `AcquireLease` with `LeaseDuration: -1` — no expiry. A crashed or killed run leaves the state blob leased with no automatic recovery; you need `terraform force-unlock` or a manual lease break in Azure. Decide who is allowed to do that before you need it.
- **Authenticate the backend with Entra, not a key.** The backend docs say of access keys, SAS tokens and client secrets: *"Terraform retains this method for backwards compatibility only, do not use it for any new workloads"*, recommending OIDC / workload identity federation. Set `use_azuread_auth = true`. Lock the state container down at least as tightly as production data, and remember §1 — `listkeys` on that account is a read of your state.

---

## 6. Global-surface dependency checklist for a failover

Walk the runbook and mark every step. Anything in the right-hand column is a dependency on the thing that just failed — the 29 October 2025 Front Door incident is the worked example in `SKILL.md`.

| Step | Data plane | Control plane / global surface |
|---|---|---|
| Shift traffic | a pre-published DNS or Traffic Manager record with a low TTL | editing an Azure Front Door routing configuration mid-incident |
| Authenticate | an already-issued token | an Entra write, a new role assignment, a policy change |
| Add capacity | already-running instances; always-ready instances provisioned in advance | an ARM deployment, a scale rule, a quota increase |
| Read data | a geo-replicated replica already serving reads | promoting a replica; changing replication topology |
| Deploy a fix | an artifact already present in the standby region | a build or registry pull in the failed region |
| Operate | Azure CLI against a regional endpoint | the Azure Portal, if it sits behind the failed surface |
| Page someone | a second channel | a monitoring stack hosted in the failed region |

Rules that follow: **pre-create everything the standby needs** so recovery is a data-plane action; keep artifacts and images replicated; and rehearse the failover with the control plane assumed unavailable, because that is the condition under which you will need it.
</content>
