# Google Cloud service mechanics — limits, delivery semantics, and failure modes

Read at **Gate 5** when mapping workloads to services, and at **Gate 7** for the reliability review.

> Retrieved **25 August 2026** from Google Cloud documentation. Quotas move and many are per-project and adjustable. **Re-check the service's own quota page before a number enters a design, and check what *your* project actually has** — GPU and Cloud Run instance quotas in particular are granted per project per region and a first deploy can fail on them.
>
> Cross-provider comparison numbers (Cloud Run's duration ceilings against Lambda and Workers, Premium/Standard egress tiers and the 200 GiB allowance, per-object caps, accelerator availability) live in `mir-cloud/references/provider-decision-tables.md` and `cost-model.md`. They are not repeated here.
>
> **Two prices are not first-party.** The BigQuery on-demand rate (~$6.25/TiB, US) and Cloud Run's per-vCPU-second rates come from third-party aggregation dated April 2026, because both pricing tables render client-side and did not resolve for automated retrieval. Re-verify at `cloud.google.com/bigquery/pricing` and `cloud.google.com/run/pricing` before either number reaches a cost model.

---

## 1. Cloud Run — the configuration decisions that change the bill or the behaviour

| Setting | What it does | The consequence AI misses |
|---|---|---|
| **Billing mode** | request-based (default) vs instance-based (`--no-cpu-throttling`; `--cpu-throttling` reverts) | Request-based throttles CPU to near-zero between requests, so background work silently does not run. Instance-based runs it and bills the whole lifetime |
| **Instance-based minimum memory** | at least **512 MiB** required | A small service cannot switch modes without also resizing |
| **GPU** | "You must use instance-based billing to use the GPU feature" | Scale-to-zero still applies, but every second an instance exists is billed at the full rate |
| **GPU minimums** | L4: 4 vCPU / 16 GiB. RTX PRO 6000 Blackwell: 20 vCPU / 80 GiB | The floor, not the recommendation — 8 vCPU / 32 GiB is the suggested L4 shape |
| **Zonal redundancy** | **on by default** for GPU, and costs more; off is best-effort failover | A reliability decision made at Gate 5, not a cost tweak at Gate 6 |
| **`max-instances`** | ceiling on scale-out | For GPU it must sit under the project's regional GPU quota or the deploy fails. It is also your only hard spend cap — a budget alert is detection, not prevention |
| **`min-instances`** | keeps N warm | Request-based: reduced idle rate. Instance-based: full rate, always. Either way, re-run the pillar's Gate 4 comparison |
| **Concurrency** | requests per instance, default 80 | Sized for I/O-bound handlers. A CPU-bound or memory-heavy container needs it far lower, and the symptom of getting it wrong is tail latency, not errors |
| **Startup CPU boost** | extra CPU during container start | The cheapest cold-start fix that does not change billing mode. Try it before reaching for `min-instances` |
| **Runtime service account** | defaults to the Compute Engine default SA | Always set it explicitly. See `SKILL.md` §3 |
| **`--allow-unauthenticated`** | grants `roles/run.invoker` to `allUsers` | A public internet endpoint. Block the class with `constraints/iam.allowedPolicyMemberDomains` |

Duration ceilings for services and jobs are the pillar's Gate 3 rows — read them there, do not re-derive them here.

---

## 2. Pub/Sub and Eventarc — write this into the Gate 5 design

| Source | Ordering | Duplicates | Must have |
|---|---|---|---|
| Pub/Sub pull or push, default | none | yes | idempotency key; a dead-letter topic; an ack deadline the handler can actually meet |
| Pub/Sub with ordering keys | per ordering key | yes | a key that partitions the work; note that ordering reduces throughput per key to one in flight |
| Pub/Sub exactly-once subscription | per key if enabled | suppressed within the ack deadline | still an idempotent handler — see the caveat below |
| Eventarc → target | **none** | yes | idempotency; a Pub/Sub dead-letter topic on the Standard path |
| Cloud Storage → Pub/Sub notification | none | yes | idempotency; the notification is at-least-once and can be delayed |

Numbers that change the code:

| Property | Value |
|---|---|
| Ack deadline | default **10 s**, minimum 10 s, maximum **600 s** |
| Message retention | default **7 days**, maximum **31 days** |
| Subscription expiry with no activity | default **31 days**, minimum 1 day |
| Dead-letter topic max delivery attempts | **5 to 100** |
| Eventarc retry | exponential backoff for **24 hours** |
| Eventarc event size | **1 MB** (Advanced), **512 KB** (Standard) |

**Exactly-once is narrower than it sounds.** Google states that Pub/Sub "might redeliver a message even after an acknowledgment request for the message returns successfully." The client libraries also extend the ack deadline dynamically, so a handler tuned against the configured deadline can still see redelivery; `minDurationPerAckExtension` / `maxDurationPerAckExtension` are the knobs. Write the idempotent handler. The mechanism — a natural key, a conditional write, a dedupe table with a TTL — belongs in `mir-backend`; what belongs in the design is the requirement and which hop imposes it.

**Push to Cloud Run** is the trap in the middle: the push subscription's ack deadline caps the effective request duration, so a long handler is redelivered rather than extended. The pillar's execution-limits table carries the number.

**Exit cost is high and never estimated.** Eventarc triggers, CloudEvent contracts, Pub/Sub filters and the IAM glue that binds them have no equivalent on another provider. Ledger it at Gate 5 alongside BigQuery and Spanner.

---

## 3. BigQuery — cost mechanics

- **On-demand** bills bytes *processed*, not returned. Columnar, so cost is set by the columns selected and the partitions touched. First 1 TiB/month free. Roughly **2,000 concurrent slots per project**, which makes on-demand a performance ceiling as well as a price.
- **Editions** (Standard, Enterprise, Enterprise Plus) bill slot-hours, per second with a **one-minute minimum by default**; opting into **fluid scaling** at the reservation level gives per-second with no minimum. Commitments are **regional** and non-transferable. On-demand and editions can be mixed per project — which is how an estate acquires both and an unexplained invoice.
- **Controls that belong in the same change as the table**, not in a follow-up ticket:
  - `maximum_bytes_billed` on every query path, including scheduled queries and BI tools.
  - Custom query quotas at project and user level.
  - Partitioning on the column the predicates actually use, plus `require_partition_filter = true` on anything large.
  - Clustering for high-cardinality filters; materialized views instead of a scheduled query that re-scans the source.
  - Storage: long-term storage pricing applies automatically to partitions untouched for 90 days — do not "optimise" by rewriting tables, which resets it.
- Streaming inserts, BI Engine, BigQuery ML and Omni are separately billed dimensions. A cost model with only "queries" and "storage" is incomplete.

---

## 4. Storage, and where data actually rests

- **Uniform bucket-level access** disables object ACLs and is the only sane default; **public access prevention** blocks `allUsers` / `allAuthenticatedUsers` at the bucket. Enforce both estate-wide with the matching org-policy constraints rather than per bucket.
- **Soft delete is on by default**, 7 days, configurable 7–90 or 0 to disable. It is a window for accidents, **not a backup** — soft-deleted objects keep accruing storage charges, and a bucket of short-lived temporary objects can double its bill because of it.
- **Location types are not interchangeable.** A region is one place. **Dual-region** names two. **Multi-region (`US`, `EU`, `ASIA`) is several countries**, and `global` is unpredictable by design. For a national-residency requirement, only region or dual-region answers the question.
- **The sinks a residency review forgets:** the `_Required` and `_Default` Cloud Logging buckets, aggregated org-level sinks, Cloud Monitoring metrics, Error Reporting, Cloud Trace, Artifact Registry replicas, BigQuery datasets created by a log sink, third-party APM, and support-case attachments. Enumerate all of them, per `mir-cloud` Gate 5.

---

## 5. Audit logging — what you get, what you must turn on, what it costs

| Log type | Default | Cost |
|---|---|---|
| Admin Activity | always on, cannot be disabled | free |
| System Event | always on | free |
| Policy Denied | always on | **billed** for storage |
| Data Access | **off, except for BigQuery** — "disabled by default because they can generate large volumes of data" | billed on ingestion |

Retention: the `_Required` bucket holds audit logs for **400 days and is not configurable**. `_Default` is **30 days**, configurable only at project level; user-defined buckets default to 30 days and accept 1–3,650.

So the detective floor is: enable Data Access logs deliberately on the services that hold data (and price the ingestion), route audit logs to a bucket in a **separate project** with its own IAM, and alarm on `SetIamPolicy`, key creation, org-policy changes and log-sink deletion. An attacker's first move against this estate is the log sink.

---

## 6. Compute, GKE and network defaults worth changing

- The **default VPC** is auto-mode with a subnet in every region and four ingress rules — including `default-allow-ssh` and `default-allow-rdp` from `0.0.0.0/0`. Block it with `constraints/compute.skipDefaultNetworkCreation` and build custom-mode VPCs.
- **Private Google Access** on a subnet lets private instances reach Google APIs without an external IP; **Private Service Connect** is the endpoint model for reaching managed services and third parties privately. Which one you use decides the route and the egress line.
- **GKE:** private cluster with authorized networks on the control plane; Workload Identity Federation for GKE on; Shielded Nodes on; node auto-upgrade on a release channel. Autopilot removes several of these decisions and takes the node-level ones away from you — that is a tradeoff to state, not a default to assume.
- **Cloud SQL:** private IP only (`constraints/sql.restrictPublicIp`), IAM database authentication, and automated backups with point-in-time recovery. "Allow all" authorized networks is the equivalent of an open security group.
- **Quotas are per project per region and mostly soft.** A design that assumes a large first deploy will get the capacity is a design with an untested assumption; request the increase during Gate 6, not during the incident.

---

## 7. Control-plane dependency checklist for a failover

Walk the runbook and mark every step. Anything in the right-hand column is a dependency on the thing that just failed — and 12 June 2025 showed that on Google Cloud a control-plane dependency can be **global**, not regional, because Service Control metadata is replicated worldwide within seconds.

| Step | Data plane | Control plane |
|---|---|---|
| Shift traffic | a global load balancer with both backends already attached and healthy | creating a backend service; editing Cloud DNS; changing a URL map |
| Authenticate | a token already minted; an attached identity still in its lease | `GenerateAccessToken`; a new IAM binding; an org-policy change |
| Add capacity | a MIG already running; a Cloud Run revision already warm | `instances.insert`; deploying a new revision; a quota increase |
| Read/write data | a Spanner multi-region instance; an existing read replica | promoting a replica; changing replication topology |
| Deploy a fix | an image already present in the standby region's Artifact Registry | a Cloud Build run; enabling an API; a cross-region image pull |
| Page someone | a second channel outside Google Cloud | a monitoring stack that itself depends on the failed control plane |

Rules that follow: **static stability** — pre-create the standby's service accounts, DNS records, backend services and capacity so recovery is a data-plane action; replicate images to the standby region's registry; keep at least one backup **outside** the provider, because the May 2024 GCVE deletion removed a customer's estate in both geographies at once; and rehearse the failover with the control plane assumed unavailable, because that is the condition under which you will need it.
