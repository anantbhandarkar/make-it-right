# Microsoft Entra and Azure RBAC — the authorization model, its escalation paths, and its guardrails

Read at **Gate 6** when writing any identity, role assignment or policy, and at **Gate 7** for the security review.

> Retrieved **25 August 2026** from Microsoft documentation. Limits and defaults move. **Re-check the page before a number enters a design**, and note that one dated default — Key Vault's — changes underneath existing templates rather than only new ones (§6).
>
> Cross-provider comparison facts (egress, per-object caps, accelerator availability, the exit-cost table) live in `mir-cloud/references/provider-decision-tables.md` and `cost-model.md`. They are not repeated here.

---

## 1. Two directories, one tenant

Microsoft's own statement is the boundary: *"Microsoft Entra ID and Azure resources are secured independently from one another. That is, Microsoft Entra role assignments do not grant access to Azure resources, and Azure role assignments do not grant access to Microsoft Entra ID."*

| Object | Scope of existence | What it is for | What it is not |
|---|---|---|---|
| Application object (app registration) | exactly one, in the **home tenant** | the blueprint: redirect URIs, exposed scopes, requested permissions, credentials | not a security principal; nothing is granted to it |
| Service principal, type **Application** | one per tenant where the app is used | the local instance. Azure role assignments and consented Graph permissions land here | not portable; a multitenant app has one per consenting tenant |
| Service principal, type **Managed identity** | created and deleted with the Azure resource (system-assigned) or standalone (user-assigned) | credential-free identity for Azure workloads | has **no** application object; cannot be edited directly |
| Service principal, type **Legacy** | pre-app-registration or created through legacy tooling | historical | no app registration behind it; tenant-local only |
| Entra directory role | tenant | users, groups, applications, credentials | does not touch your resources |
| Azure RBAC role | management group / subscription / RG / resource | resources | does not touch the directory |

Consequences worth writing into a design:

- **A credential on an app registration is the application.** Client secret, certificate and federated credential are equivalent in effect. Whoever can add one — the app owner, Application Administrator, Cloud Application Administrator, Global Administrator, Hybrid Identity Administrator — can become the app. Audit app owners as if they were the app.
- **Deleting the app registration deletes the home-tenant service principal.** Restoring the app registration through the portal **does not** restore the service principal. Every role assignment that named it becomes an orphan. For temporary suspension, deactivate the application instead of deleting it.
- For a multitenant app, the consenting tenant's admin grants permissions to *their* service principal. Your app object does not carry their grants and you cannot read them.

### The elevate-access join

A Global Administrator can set *Microsoft Entra ID → Properties → Access management for Azure resources* to **Yes** and be assigned **User Access Administrator at root scope (`/`)** — permission to assign roles in every subscription and management group in the tenant. It is per-user, self-service, and persists until toggled back. Also reachable as `POST /providers/Microsoft.Authorization/elevateAccess?api-version=2016-07-01`.

Controls:

- The portal shows a banner counting users with elevated access, on the Entra Properties page and on any subscription's Access control (IAM) page.
- Log both sides. Entra audit logs carry a `Azure RBAC (Elevated Access)` service filter (in preview at time of writing); the Azure activity log records `Microsoft.Authorization/elevateAccess/action` under Directory Activity. Ship them off-box and alert.
- With PIM, **deactivating the Global Administrator role does not flip the toggle back.** Set it to No first.

---

## 2. Managed identity — selection, and the constraint nobody plans for

| Scenario | Choose | Because |
|---|---|---|
| Role assignment must exist before the resource does | **user-assigned** | a system-assigned identity does not exist yet, and the deployer may not hold `roleAssignments/write` |
| Many identical resources needing the same access | **user-assigned** | one identity, one set of role assignments, instead of N |
| Rapid create/destroy (ephemeral compute) | **user-assigned** | many system-assigned identities in a short window hits the Entra object-creation rate limit (HTTP 429), and a deleted one counts against the limit **until purged after 30 days** |
| The audit log must name the specific resource | **system-assigned** | one identity, one resource |
| Permissions must die with the resource | **system-assigned** | lifecycle is coupled by construction |

Then the constraints:

- **Token cache.** Group and role membership are claims in the token; the managed-identity back end *"maintain[s] a cache per resource URI for around 24 hours"*, and *"it isn't possible to force a managed identity's token to be refreshed before its expiry."* A membership change can take hours. Microsoft's own workaround is architectural: group resources under a **user-assigned identity with permissions applied directly to the identity**, rather than adding and removing the identity from an Entra group. Control who can attach it with `Managed Identity Contributor` and `Managed Identity Operator`.
- **Role assignments survive the identity.** Deleting an identity leaves its assignments behind as *Identity not found*, counting against the per-subscription limit. Sweep: `Get-AzRoleAssignment | Where-Object {$_.ObjectType -eq "Unknown"} | Remove-AzRoleAssignment`.
- **Attachment is delegation.** Anyone who can run code on a resource has every permission of every identity attached to it: *"All code/scripts running on a virtual machine can request and retrieve tokens for any managed identities available on it."* Treat `Managed Identity Operator` as `iam:PassRole` and scope it to named identities.
- **Token endpoint.** `GET http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=<app-id-uri>` with header `Metadata: true`. Microsoft calls that header *"a mitigation against server side request forgery (SSRF) attacks"* — it defeats a naive fetcher, not a header-controlling one. **With more than one user-assigned identity attached, `client_id`, `object_id` or `msi_res_id` becomes required**; code written against a single identity breaks when a second is attached.
- **Throttling on the management API:** create-or-update 10 rps/tenant, 2 rps/subscription, 0.25 rps/resource; get 30/10/0.5; delete 10/2/0.25. Above these, HTTP 429.

---

## 3. Federated identity credentials — a reviewed set

```json
[
  { "name": "gh-prod",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:octo-org/octo-repo:environment:Production",
    "audiences": ["api://AzureADTokenExchange"] },

  { "name": "aks-workload",
    "issuer": "https://<cluster-oidc-issuer>/",
    "subject": "system:serviceaccount:<namespace>:<serviceaccount>",
    "audiences": ["api://AzureADTokenExchange"] }
]
```

Rules, all from the considerations page:

- `issuer`, `subject` and `audiences` are matched **case-sensitively** against the incoming token's `iss`, `sub` and `aud`. **"Wildcard characters aren't supported in any federated identity credential property value."**
- Exactly one audience, 600 characters per field, and the `issuer`+`subject` pair must be unique on the identity (a duplicate returns 400).
- **A wrong subject creates without error and fails at exchange without error.** Read the actual `sub` from a real workflow run. Never compose it from a template.
- **Maximum 20** federated credentials per application or user-assigned managed identity. They do not consume the tenant's service-principal quota.
- Only **RS256**-signed issuers are supported. Microsoft Entra-issued tokens cannot be used as the external assertion.
- Only the **first 100 signing keys** are read from the issuer's OIDC endpoint; an IdP publishing more can fail intermittently.
- **Propagation is not instant.** A token request minutes after creation can return `AADSTS70021: No matching federated identity record found for presented assertion`. Retry; do not recreate the credential.
- **Concurrent creation on one user-assigned identity returns 409.** `azurerm` ≥ 3.40.0 serializes them; ARM templates need `"mode": "serial"` or `dependsOn`. Credentials on *different* identities can be created in parallel.
- Creation is currently unsupported on user-assigned identities in **Malaysia South**; use an identity in a supported region.

**GitHub subject forms.** Environment: `repo:<org>/<repo>:environment:<name>`. Branch or tag: `repo:<org>/<repo>:ref:refs/heads/<branch>` or `...:ref:refs/tags/<tag>`. Prefer the environment form — GitHub's environment approvals then gate the credential itself. **Sources disagree on the pull-request form:** Microsoft's CLI, PowerShell and REST examples write `repo:<org>/<repo>:pull-request`, GitHub's own claim uses `pull_request`. Read the token rather than picking one.

You can block federated credentials outright where they are not wanted, with a policy on `Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials`:

```json
{ "policyRule": {
    "if":   { "field": "type", "equals": "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials" },
    "then": { "effect": "deny" } } }
```

---

## 4. RBAC evaluation, scope and the missing deny

The four scopes are a parent-child chain: **management group → subscription → resource group → resource.** An assignment applies to its scope and everything beneath it.

Evaluation order, per Microsoft:

1. Acquire a token for Azure Resource Manager. **The token carries group memberships, including transitive ones** — which is why membership changes need a new token (§2).
2. ARM gathers all role assignments **and deny assignments** at and above the resource.
3. **If a deny assignment applies, access is blocked.** Otherwise continue.
4. Effective permissions = `Actions − NotActions` (and `DataActions − NotDataActions`).
5. Any ABAC conditions on the assignment are evaluated last.

**Multiple assignments are additive:** *"Azure RBAC is an additive model, so your effective permissions are the sum of your role assignments."* `Reader` on a resource group under subscription-scoped `Contributor` changes nothing. There is no narrowing at a child scope.

**And you cannot write a deny.** *"You can't directly create your own deny assignments. Deny assignments are created and managed by Azure."* Every deny assignment is `IsSystemProtected`. The one customer-reachable path is a **deployment stack's deny settings**, which produces an Azure-owned deny assignment scoped to the stack's managed resources (typically `*/delete`, with `DoNotApplyToChildScopes: True` and an `ExcludePrincipals` list). That is resource protection, not a policy floor. **The floor is Azure Policy — §5.**

RBAC data is replicated globally and deletions are global. Assignment changes take minutes, not seconds, to propagate — build retries into anything that assigns a role and then immediately uses it.

### Escalation paths to grep for

| Grant | Combined with | Result |
|---|---|---|
| `Microsoft.Authorization/roleAssignments/write` (`Owner`, `User Access Administrator`) | anything | can grant itself any role at that scope and below, including re-granting after you narrow it |
| Global Administrator | the elevate-access toggle | User Access Administrator at root scope over every subscription |
| `Managed Identity Operator` | permission to create a compute resource | attach a privileged identity to a resource you control and read its tokens |
| `Microsoft.Storage/storageAccounts/listkeys/action` (`Contributor`, `Storage Account Contributor`) | — | full data access to the account without any data-plane role |
| `Microsoft.KeyVault/vaults/write` (`Contributor`) | — | change the vault's authorization model or rewrite its access policies |
| Application Administrator / app ownership | — | add a credential to any app and become it |

Microsoft's own limits: at most **three subscription owners**; prefer a job-function role over a privileged administrator role; if a privileged role is unavoidable, scope it to a resource group or resource; consider a **condition constraining which roles a delegate may assign**; assign to groups, not users; assign by **role ID**, not name; never `*` in a custom role's `Actions`.

---

## 5. Azure Policy — the guardrail RBAC cannot be

Microsoft draws the line: Azure Policy *"ensures that resource state is compliant to your business rules without concern for who made the change or who has permission to make a change"*, while *"Azure RBAC focuses on managing user actions at different scopes."* Both are needed; only one of them can deny a shape.

| Thing | Value | Consequence |
|---|---|---|
| Evaluation triggers | create/update; new assignment; changed assignment; **standard cycle every 24 hours** | a green dashboard is a statement about the last scan |
| Effects | `audit`, `auditIfNotExists`, `deny`, `denyAction`, `append`, `modify`, `deployIfNotExists`, `disabled` | start on `audit`; `deny` breaks pipelines you did not enumerate |
| Override direction | explicit-deny system — a permissive child assignment does **not** loosen a parent deny | exclude the child via `notScopes` on the parent, then assign the looser definition below |
| Coverage | assignable at management group, but **only subscription- and resource-group-level resources are evaluated** | the management group itself is not a resource |
| Visibility | all policy objects are readable by every role holder at that scope and below | do not encode secrets or sensitive scope names in definitions |
| Ceilings | 500 definitions and 200 initiative definitions per scope; **200 assignments per scope**; 1,000 exemptions; 400 `notScopes` per assignment; 1,000 policies per initiative | a sprawling per-team assignment pattern hits 200 sooner than it looks |
| Remediation identity | `deployIfNotExists` and `modify` run as a managed identity; **`User Access Administrator` is required to grant it its permissions** | a standing, automation-triggered principal with write access across the assignment scope — enumerate it |

Policy can also gate on *who* is asking, via `requestContext().identity` — for example blocking interactive deletes of critical resources, or blocking create/update from a user without MFA. That is the closest thing to an identity-aware deny that Azure gives a customer.

---

## 6. Key Vault — two authorization models, one dated flip

| | Access policies (legacy) | Azure RBAC |
|---|---|---|
| Assignable at | the vault, and only the vault | management group, subscription, RG, the vault, **or an individual key/secret/certificate** |
| Granularity | vault-wide per permission — `get` on secrets means every secret | per role, per scope, and per object if you want it |
| Ceiling | policies per vault | the per-subscription role-assignment limit |
| Tooling | vault-specific | PIM, conditions, Access Reviews, the same tooling as everything else |

**The dated change.** From API version **2026-02-01**, a new vault defaults to `enableRbacAuthorization = true`. Existing vaults are untouched, and a vault whose property is `null` (created by an older API version) keeps using access policies. **All control-plane API versions before 2026-02-01 retire on 27 February 2027**; data-plane APIs are unaffected. If you want access policies on a new vault, set `enableRbacAuthorization = false` explicitly in the ARM/Bicep/Terraform template — silence now means RBAC.

Migration traps:

- Flipping the model needs `Microsoft.KeyVault/vaults/write`; the portal additionally requires `Microsoft.Authorization/roleAssignments/write` **specifically so you cannot lock yourself out**.
- **Role assignments are not preserved when a vault is recovered after soft-delete.** Recreate every one, or the recovered vault is readable by nobody.
- Role assignments take several minutes to propagate — retry in the application.
- Two access-policy templates have **no built-in role equivalent** and need a custom role: *Azure Data Lake Storage or Azure Storage*, and *Azure Backup*.
- Enable the `AuditEvent` diagnostic category to a Log Analytics workspace before you migrate, not after.

---

## 7. If a credential is believed exposed

The response differs per credential type, and getting the type wrong wastes the window.

| Credential | Revoke by | What survives anyway |
|---|---|---|
| App registration secret / certificate | delete it — **then list the app's other credentials**, because adding one is how persistence is established | issued access tokens live out their lifetime |
| Federated identity credential | delete the credential | any token already exchanged |
| Storage account key | regenerate **both** keys | nothing signed by them: every service and account SAS dies with the key |
| User delegation SAS | revoke the user delegation key, or remove the signer's role | the SAS dies with the key |
| Service / account SAS | only the account-key regeneration above, or a stored access policy if one was used | otherwise it is valid until its expiry, and you cannot enumerate the outstanding ones |
| Managed identity | remove the role assignments | the platform token cache can hold authorization for hours (§2) — removal is not immediate |

Then investigate: Entra **sign-in logs** filtered to the service principal, Entra **audit logs** for credential additions and consent grants, the **activity log** for `roleAssignments/write` and `elevateAccess`, and the resource's own diagnostic logs (Key Vault `AuditEvent`, `StorageBlobLogs`). If a diagnostic setting was not configured before the incident, the data does not exist retroactively — which is why the detective floor is a Gate 6 item, not a Gate 7 one.
</content>
