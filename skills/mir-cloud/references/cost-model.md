# Cost model

Read at **Gate 5**. A provider recommendation without a cost model is an opinion.

> **Every rate in this file was retrieved 13 Aug 2026 and is list price in a US/EU region.** Cloud prices, free tiers, and tier boundaries change — sometimes with no announcement in the region you care about. **Re-verify before you quote.** Where you cannot verify, write the pricing *dimension* ("billed per GB scanned") rather than inventing a number. A confidently wrong figure is worse than "verify this".

---

## Build order

1. **List the billed dimensions** for the chosen architecture (tables below). Not the ones you remember — the ones on the provider's pricing page.
2. **Put egress on its own line.** Always. Even when it is small, showing zero is information.
3. **Add the line items nobody estimates** — NAT processing, cross-AZ, public IPv4, storage operations, log ingestion.
4. **Apply commitments** — existing ones first, then any you would sign.
5. **State the exit cost** separately from the run cost.
6. **Record the retrieval date next to every rate**, and state the growth assumption (if traffic triples, which line triples?).

---

## Billed dimensions by architecture

| Architecture | Dimensions you must have a number for |
|---|---|
| **Serverless functions** | Invocations · GB-seconds (or vCPU-s + GiB-s) · egress · provisioned/always-ready idle capacity if used · log ingestion + retention |
| **Serverless containers** | vCPU-seconds · GiB-seconds · **which billing mode** — request-based bills active time plus a reduced idle rate for min-instances; instance-based (Cloud Run `--no-cpu-throttling`, and mandatory for GPU) bills the full rate for the whole instance lifetime, which is VM economics under a serverless name · requests · egress · image registry storage |
| **Cloudflare Workers** | Requests · CPU-milliseconds · Durable Object requests + duration + stored rows · KV/D1/R2 operations · **no bandwidth line for Workers/R2**, but Containers bill egress |
| **VMs / Kubernetes** | Instance-hours (× count, × environments) · block storage GB-month + provisioned IOPS · load balancer hours + LCU/processed-GB · egress · NAT gateway hours + per-GB processing · public IPv4 hours · control-plane fee (EKS/GKE/AKS) · snapshots |
| **Object storage** | GB-month by storage class · Class A (write/list) operations · Class B (read) operations · egress · retrieval fees on infrequent/archive tiers · early-deletion minimums · replication transfer |
| **Managed database** | Instance-hours or serverless capacity units · storage GB-month · IOPS · backup storage beyond the free allowance · cross-AZ/multi-AZ premium · egress from read replicas |
| **Data warehouse** | Bytes scanned **or** compute-hours (know which model you are on) · storage GB-month · streaming inserts · materialized-view refresh |
| **GPU** | GPU-second or GPU-hour · the CPU/memory floor the GPU SKU forces · idle time when min-instances > 0 · storage for weights · egress of outputs |

---

## Egress — the tables

### Free allowances

| Provider | Allowance | Scope |
|---|---|---|
| AWS | 100 GB/month | Aggregated across all services and regions. Perpetual, not 12-month. Excludes China and GovCloud. Does not roll over |
| Azure | 100 GB/month | All regions |
| GCP Premium tier | ~1 GiB/month | Premium is the default and is what Cloud Run uses |
| GCP Standard tier | 200 GiB/month **per account, aggregated across all regions** | Not per region — Google calculates it across all of them. Standard routes over the public internet, not Google's backbone. **Does not apply to Cloud Run**, and Always Free limits do not apply to Standard tier |
| Cloudflare R2 | Egress free at any volume, all storage classes. Free storage/operations tier: 10 GB-month + 1M Class A + 10M Class B | The free storage and operation allowances are **Standard class only** — Infrequent Access has no free tier and adds a $0.01/GB retrieval fee plus a 30-day minimum duration |
| Cloudflare Containers | 1 TB/month (NA/EU), 500 GB (elsewhere) | Beyond that, egress is billed per GB |

### Structure after the allowance

| Provider | Shape | Approximate list, US/EU (13 Aug 2026 — verify) |
|---|---|---|
| AWS | Tiered per GB, cheaper as monthly volume rises. Tiers are cumulative across the month and aggregated across services | ~$0.09/GB first 10 TB → ~$0.085 next 40 TB → ~$0.07 next 100 TB → ~$0.05 above 150 TB. Asia-Pacific regions start higher. **CloudFront is not on this rate card** — it has its own plans, allowances and rates, so never price CDN bytes off this row |
| Azure | Same tier shape, marginally cheaper in the first two bands | ~$0.087/GB first 10 TB → ~$0.083 → ~$0.07 → ~$0.05. Zone 2 (Asia-Pacific) and Zone 3 (South America, Africa, Middle East) are materially more |
| GCP Premium | Tiered per GB, highest of the three at low volume | ~$0.12/GB first TB, falling with volume |
| GCP Standard | Tiered per GiB, cheapest hyperscaler band | ~$0.085/GiB to 10 TiB, falling with volume |
| Cloudflare R2 | **$0 at all volumes** | Cost moves to operations: Class A ~$4.50/M, Class B ~$0.36/M, storage ~$0.015/GB-month Standard |
| Cloudflare Containers | Per GB after the allowance | ~$0.025/GB NA/EU; ~$0.04–0.05/GB elsewhere |

### How egress flips the answer

- **Under ~1 TB/month:** roughly $85–$120 at list. Whether that is a rounding error depends on the rest of the bill, so express it as a percentage of projected monthly spend rather than reaching for the word. It should not drive the choice unless it changes the ranking.
- **1–50 TB/month:** egress is a top-three line. Model R2 (or any zero-egress store) in front of the compute you already have — a split is often the right answer.
- **Over ~50 TB/month:** egress likely dominates. Zero-egress storage becomes a requirement, and the compute provider becomes the secondary question.
- **Write-heavy, many small objects:** the saving flips. R2's Class A operation charge means the cost moves from bandwidth to operations. Count objects written per month, not just GB.

### EU Data Act, Article 29

From **12 Jan 2027**, providers may not charge EU customers a switching charge, including egress for the switch. From 11 Jan 2024 to that date, switching fees are capped at the provider's direct costs. All three hyperscalers already waive egress for customers leaving.

**This is exit-cost relief, not a bandwidth discount.** Ordinary serving egress stays billed. Intra-provider region-to-region transfer sits outside the clear scope and is generally still billed. The relief attaches to a covered *switching process*, not to any export an EU customer happens to run, and Art. 31 excludes some custom-built and non-production services — check that the move qualifies before booking $0. It is a Regulation, so it binds directly: a contract clause cannot preserve a switching charge the Article prohibits. Standard service fees and early-termination penalties are separate and survive.

---

## The line items nobody estimates

| Item | Where it bites |
|---|---|
| **NAT Gateway** (AWS) | Two charges: hourly per gateway — HA means one per AZ, so a 3-AZ design pays three hourly rates with zero traffic — and per GB on bytes that **actually traverse the gateway**, including traffic to AWS services. Regularly rivals the egress bill. Read the route table before estimating: traffic via S3/DynamoDB gateway endpoints never touches the NAT gateway. IPv6-only egress via an egress-only internet gateway avoids both charges |
| **Cross-AZ transfer** | AWS charges per GB each way. GCP charges per GiB each way between zones in a region. Azure stopped charging inter-AZ within a region on 21 May 2024. A chatty three-AZ service pays for its own internal traffic |
| **Public IPv4 addresses** (AWS) | Billed hourly per address, attached or idle. Large fleets and forgotten Elastic IPs add up |
| **Load balancer capacity units** | ALB/NLB LCU, Front Door routing, Cloud Load Balancing forwarding rules — priced on top of hours, driven by connections and processed bytes |
| **Storage operations** | Class A/Class B (R2), PUT/GET/LIST (S3, GCS, Blob). High-frequency small-object workloads pay here |
| **Retrieval and early-deletion fees** | Infrequent-access and archive tiers charge per GB retrieved and impose a minimum storage duration (R2 Infrequent Access: 30 days). Lifecycle policies that move data too eagerly cost more than they save |
| **Log and telemetry ingestion** | Charged at ingestion *and* at retention, and shipping logs cross-region is also egress. Often the largest surprise line in a serverless bill |
| **Control plane fees** | EKS/GKE/AKS per-cluster hourly charges, multiplied by every environment |
| **Snapshots and backups** | GB-month, retained indefinitely by default in most setups |
| **Cross-region replication** | Transfer charge plus a second copy of storage — and a residency question (see Gate 5) |
| **Support plan** | A percentage of spend on the hyperscalers. Real money at scale, never in an estimate |

---

## Commitment instruments

| Provider | Instrument | Shape | Note |
|---|---|---|---|
| **AWS** | Compute Savings Plans | Commit an hourly $ rate; applies across EC2, Fargate, Lambda; flexible across family/region/OS | All-upfront and partial-upfront options give deeper discounts |
| **AWS** | Reserved Instances | Commit to a specific family/region/term | Deepest discount, least flexible |
| **Azure** | Savings plan for compute | Commit an hourly $ rate across VMs, Container Instances, App Service Premium v3/Isolated v2, AKS, Functions Premium | Stack with Azure Hybrid Benefit if you hold Windows/SQL Software Assurance |
| **Azure** | Reserved VM Instances | Specific SKU/region/term | Stacks under the savings plan |
| **GCP** | Resource-based CUD | Specific machine capacity in a region | |
| **GCP** | Spend-based / flexible CUD | Commit hourly spend, movable across eligible compute | **No-upfront only** — no all-upfront option. CUD sharing is on by default for billing accounts created on or after 16 Jun 2026 |
| **All three** | Enterprise commitment (EDP / MACC / Google commit) | Total $ over a term, drawn down by consumption | Qualifying third-party Marketplace purchases draw down the commitment — route eligible software through Marketplace |

**Application order when instruments stack:** reservations consume matching usage first, then spend-based plans apply to the remainder starting with the highest savings rate, then anything left bills on demand.

**The comparison error to avoid:** if an unmet enterprise commitment exists, you pay the shortfall whether or not you use the provider. The correct comparison for new spend is *list price elsewhere* vs. *effectively-already-paid-for capacity here*, not list vs. list.

---

## Exit cost

```
exit cost = engineering months to replace each adopted service
          + one-time data transfer out (→ $0 from 12 Jan 2027 only where the move
            is a covered EU switching process; check the Art. 31 exclusions)
          + parallel-run cost during cutover
          + retraining / tooling replacement
```

Engineering months dominate. Use the exit-cost table in `provider-decision-tables.md` to classify each adopted service Low/Medium/High, then estimate only the High ones in detail. If every adopted service is Low, say so — that is a genuinely reversible decision and worth stating.

---

## Template

```
COST MODEL — <workload>            rates retrieved <date>, region <region>, list price
────────────────────────────────────────────────────────────────────
Compute            <dimension × quantity>                 $<x>/mo
Storage            <GB-month × class>                     $<x>/mo
Storage operations <Class A / Class B counts>             $<x>/mo
EGRESS             <GB/mo, after free allowance>          $<x>/mo   ← own line, always
NAT / cross-AZ     <GB processed>                         $<x>/mo
Load balancer      <hours + capacity units>               $<x>/mo
Logs + telemetry   <GB ingested, retention>               $<x>/mo
Control plane      <clusters × hours>                     $<x>/mo
Support plan       <% of spend>                           $<x>/mo
────────────────────────────────────────────────────────────────────
Subtotal (list)                                           $<x>/mo
Commitments applied  <instrument, coverage>              -$<x>/mo
TOTAL                                                     $<x>/mo

Growth: at 3x traffic, <which line triples> → $<x>/mo
Idle cost: $<x>/mo  (zero only if nothing is min-instance / always-ready / provisioned)
Exit cost: <n> engineering months + <GB> transfer out
Unverified: <list every rate you could not confirm today>
```

---

## Re-verify here

Check these before quoting anything above. If a page is unreachable, say the rate is unverified rather than reciting this file.

| What | Where |
|---|---|
| AWS data transfer + EC2 rates | `aws.amazon.com/ec2/pricing/on-demand/` |
| AWS Lambda quotas | `docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html` |
| Azure bandwidth | `azure.microsoft.com/pricing/details/bandwidth/` |
| Azure Functions hosting limits | `learn.microsoft.com/azure/azure-functions/functions-scale` |
| GCP network service tiers | `cloud.google.com/network-tiers/pricing` |
| Cloud Run pricing + timeouts | `cloud.google.com/run/pricing` · `cloud.google.com/run/docs/configuring/request-timeout` |
| Cloudflare R2 pricing | `developers.cloudflare.com/r2/pricing/` |
| Cloudflare Workers limits + pricing | `developers.cloudflare.com/workers/platform/limits/` · `/pricing/` |
| Cloudflare Containers pricing | `developers.cloudflare.com/containers/pricing/` |

---

## Common estimation errors

1. **Egress omitted entirely.** The most common and the most expensive.
2. **Free tiers treated as planning headroom.** AWS's 100 GB is gone in the first hour of the month on any real service.
3. **The GCP Standard-tier allowance applied to Cloud Run.** Cloud Run is pinned to Premium with ~1 GB free.
4. **p50 duration used for a serverless ceiling check.** The p99 is what fails.
5. **Non-production environments forgotten.** Dev, staging, and preview environments are often half the bill.
6. **Idle cost recorded as zero after enabling min-instances, provisioned concurrency, or always-ready instances.**
7. **List-price comparison against a provider where an enterprise commitment is already unmet.**
8. **Log ingestion and retention left out of a serverless estimate.** It is frequently larger than the compute line.
9. **One-region estimate presented for a multi-region design.** Replication is transfer plus a second copy of storage.
10. **Prices quoted without a retrieval date.** Six-month-old figures read as current and get put in a business case.
