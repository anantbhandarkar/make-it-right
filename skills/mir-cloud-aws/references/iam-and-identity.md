# IAM and identity on AWS — the depth `SKILL.md` cites

Read at **Gate 6** before writing any policy, and at **Gate 7** during the security review.

> Retrieved **25 August 2026** against AWS documentation. Behaviour here is stable, but condition-key coverage and service support change. **Confirm at the source before quoting a rule into a design document.**

---

## 1. Evaluation, in the order AWS actually runs it

1. **Authenticate** the principal (some S3 requests are anonymous — that is a real path).
2. **Build the request context**: principal, action, resource, and every condition key the service populates. A key the service does not populate is *absent*, which is the root of most condition bugs.
3. **Evaluate all applicable policies.** An explicit `Deny` anywhere ends it.

Order of types once no explicit deny fires:

| Step | Type | Effect on the outcome |
|---|---|---|
| 1 | **Organizations SCP** | must allow, or the request dies before the account's own policies matter |
| 2 | **Resource control policy (RCP)** | must allow, for resources in scoped accounts. Per-service coverage — check yours |
| 3 | **Resource-based policy** | same account: an allow here alone is sufficient. Cross-account: required *and* not sufficient |
| 4 | **Identity-based policy** | same account: an allow here alone is sufficient. Cross-account: also required |
| 5 | **Permissions boundary** | intersects the identity policy only |
| 6 | **Session policy** | intersects whatever remains |

### The exceptions worth memorising

- **An implicit deny in a permissions boundary does not limit a resource-based policy.** AWS's own worked example: a principal whose boundary allows nothing in Secrets Manager can still `GetSecretValue` on a secret whose resource policy names them, because the boundary's denial is implicit. Only an *explicit* deny stops it.
- **Resource policies naming an IAM user ARN**, an **IAM role *session* ARN** (`:assumed-role/Role/session-name`), or a **federated user ARN** grant permissions *directly to the session* and are not limited by an implicit deny in the identity policy, boundary, or session policy.
- **A resource policy naming the role ARN** (`:role/Role`) *is* limited by an implicit deny in a boundary or session policy. Same resource, one different ARN form, opposite outcome.
- **`NotPrincipal` + `Deny` denies every principal that has a boundary attached**, whatever you listed. Use `"Condition": {"ArnNotEquals": {"aws:PrincipalArn": [...]}}` instead.

### Choosing the type

| You want to… | Use |
|---|---|
| give a principal permissions | identity policy |
| let another account in | resource policy (+ their identity policy) |
| stop an account admin from removing a control | SCP (or RCP for resource-side) |
| let a delegated admin create principals without exceeding a ceiling | permissions boundary, plus `iam:PermissionsBoundary` conditions on their `iam:Create*` calls |
| narrow one session for one job | session policy at `AssumeRole` |

A boundary is a *delegation* tool. If your goal is "nobody in this account can do X," write an SCP with an explicit `Deny`.

---

## 2. Condition operators — the table to check a policy against

| Form | Fires when key is absent? | Safe in an `Allow`? | Note |
|---|---|---|---|
| `StringEquals` | no (no match) | yes | does **not** expand `*` — a wildcard is a literal character |
| `StringLike` | no | with care | the only string operator that expands `*` and `?` |
| `StringEqualsIfExists` | **yes (true)** | only with a separate `Null` check | the `IfExists` suffix means "true if the key is missing" |
| `ForAllValues:` | **yes (true)** | **no, not without `Null`** | AWS documents this combination as a source of overly permissive policies |
| `ForAnyValue:` | no (false) | yes | but in a `Deny` this means the deny does **not** fire when the key is absent |
| `Null` | n/a | — | `{"Null": {"key": "false"}}` = "the key must be present" |
| negated (`StringNotEquals`, `ArnNotLike`) | see `IfExists` variants | with care | multiple values combine as `NOR` |

Combination rules: multiple operators in one `Condition` block are `AND`; multiple keys under one operator are `AND`; multiple values for one key are `OR`.

**Single-valued vs multivalued.** Set operators belong only on multivalued keys (`aws:TagKeys`, `aws:PrincipalServiceNamesList`). AWS documents that set operators must not be used with single-valued keys — `aws:PrincipalTag/x`, `aws:ResourceTag/x` and `aws:SourceVpce` are single-valued, and a set operator on them produces an overly permissive policy that reads as tight.

The pattern to write:

```json
"Condition": {
  "ForAllValues:StringEquals": { "aws:TagKeys": ["environment", "cost-center"] },
  "Null": { "aws:TagKeys": "false" }
}
```

---

## 3. `iam:PassRole` — the escalation pairs and how to bound them

Escalation happens when a principal can (a) pass a more-privileged role and (b) start something that runs as it. Named research: Rhino Security Labs' *AWS IAM Privilege Escalation — Methods and Mitigation* series established the class; Datadog Security Labs' **pathfinding.cloud** (17 Dec 2025) catalogues 65 paths, names `iam:PassRole` + `ec2:RunInstances` (`ec2-001`) as the most commonly exploited, and reports **27 of the 65 (42%) with no detection coverage in the open-source tools evaluated**.

| Pair | Runs attacker code as | Notes |
|---|---|---|
| `iam:PassRole` + `ec2:RunInstances` | the instance profile role | credentials read straight from IMDS — see the IMDS section of `SKILL.md` |
| `iam:PassRole` + `lambda:CreateFunction` + `lambda:InvokeFunction` | the function role | the classic |
| `iam:PassRole` + `lambda:CreateFunction` + `lambda:CreateEventSourceMapping` | the function role | used when `InvokeFunction` is denied |
| `iam:PassRole` + `ecs:RegisterTaskDefinition` + `ecs:RunTask` | the task role | frequently overlooked because the CI role "only deploys" |
| `iam:PassRole` + `glue:CreateDevEndpoint` / data-service equivalents | the service role | analytics accounts collect these |

Non-PassRole primitives to check for in the same review: `iam:CreatePolicyVersion` + `iam:SetDefaultPolicyVersion` (rewrite your own policy), `iam:AttachUserPolicy` / `AttachRolePolicy` (attach `AdministratorAccess`), `iam:CreateAccessKey` on another user, `iam:UpdateLoginProfile` / `CreateLoginProfile` (set a console password on a privileged user), `iam:UpdateAssumeRolePolicy` (add yourself to a role's trust), `lambda:UpdateFunctionCode` on a privileged function, and `iam:PutUserPolicy` / `PutRolePolicy`.

Bounding pattern:

```json
{
  "Effect": "Allow",
  "Action": "iam:PassRole",
  "Resource": [
    "arn:aws:iam::111122223333:role/app/*"
  ],
  "Condition": {
    "StringEquals": { "iam:PassedToService": "ecs-tasks.amazonaws.com" }
  }
}
```

Both halves matter. `Resource` alone lets the role be passed to any service; `iam:PassedToService` alone lets *any* role be passed to that service. And if the roles under `role/app/*` can themselves be edited by the same principal, the path reopens — deny `iam:*` on the roles a deploy principal may pass.

---

## 4. GitHub Actions OIDC — a reviewed trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:my-org/my-repo:environment:production"
      }
    }
  }]
}
```

Checklist:

- [ ] `sub` present, and **not** `repo:org/*` or `repo:org/repo:*`.
- [ ] `aud` present (`sts.amazonaws.com`) — necessary, never sufficient.
- [ ] The `sub` format matches what the repository actually emits. Repositories created **on or after 15 July 2026**, and repositories that opted in, emit immutable numeric IDs: `repo:octo-org@123456/octo-repo@456789:ref:refs/heads/octo-branch`. Not available on GitHub Enterprise Server. Read a real token claim from a workflow run before writing the condition; do not infer it.
- [ ] If the pattern needs a wildcard, it is `StringLike`, and the wildcard is at the end of a still-specific prefix. `StringEquals` never expands `*` — the policy simply never matches.
- [ ] Only one OIDC provider object per issuer per account, and its thumbprint/`ClientIDList` reviewed.
- [ ] Workflow side: `permissions: id-token: write` granted at the **job**, not the workflow.
- [ ] The role's *permission* policy is least-privilege independently of the trust policy.
- [ ] `MaxSessionDuration` set to the length of the job, not 12 hours.

Environment-scoped `sub` values (`:environment:production`) are worth the extra config: GitHub's environment approvals then gate the credential itself.

---

## 5. Sessions, chaining and third-party trust

- `MaxSessionDuration` on a role: **1 to 12 hours**; `DurationSeconds` at `AssumeRole` may request 15 minutes up to that. Default session is 1 hour.
- **Role chaining caps at one hour**, regardless of `MaxSessionDuration`, and a longer request fails. This does not apply to the first assumption from user credentials, nor to EC2 instance profiles.
- **EC2 instance-profile credentials ignore `MaxSessionDuration`** and are auto-refreshed. Shortening the role's max session does not shorten them.
- **Third-party roles need `sts:ExternalId`.** Without it, the vendor is a confused deputy: any of the vendor's other customers can ask the vendor's software to act against your account. Require a value the *vendor* generated and that is unique to you.

```json
"Condition": {
  "StringEquals": { "sts:ExternalId": "a-value-the-vendor-issued-for-you" }
}
```

- **Bound the organization** on resource policies: `"Condition": {"StringEquals": {"aws:PrincipalOrgID": "o-abc123"}}`. A queue or bucket policy copied between accounts is otherwise trusting whatever the original trusted.
- Prefer regional STS endpoints (`sts.<region>.amazonaws.com`) over the global one; the global endpoint is a cross-Region dependency, which is the lesson of the October 2025 event.

---

## 6. Credential exposure — the response order

1. **Contain.** Attach a deny-all policy or `AWSCompromisedKeyQuarantineV3`-class quarantine policy to the principal. Deactivate the key. For a role, deny `sts:AssumeRole` on it.
2. **Revoke live sessions.** Rotation does not kill sessions already issued. Add an inline deny on `aws:TokenIssueTime` earlier than now — the console's *Revoke sessions* button writes exactly this. Verify the policy landed.
3. **Scope the blast radius.** CloudTrail for every call the principal made: `iam:*` (new users, keys, login profiles, trust-policy edits), `ec2:RunInstances`, `lambda:CreateFunction`, `s3:GetObject` volume, `organizations:*`, and any Region you do not normally use.
4. **Look for persistence** the attacker left: a new IAM user or access key, an altered role trust policy, a new OIDC/SAML provider, a Lambda with a public function URL, an EC2 image or snapshot shared to another account, an S3 bucket policy granting an external principal.
5. **Then rotate**, and only then. Rotating first destroys the timeline you needed in step 3.

**Prevention that pays for itself:** no IAM users with long-lived keys where OIDC or a role will do; the keys OIDC made redundant deleted, not deactivated; IAM Access Analyzer external-access findings reviewed on a schedule; unused permissions reviewed against Access Analyzer's last-accessed data. Datadog's *State of Cloud Security* (10 Nov 2025) reports the share of access keys older than three years rising year over year across every cloud — those are almost always keys nobody could name an owner for.
