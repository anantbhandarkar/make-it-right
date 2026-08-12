# Provider decision tables

Read at **Gate 3** (elimination) and **Gate 4** (ranking). The point of this file is determinism: the same nine inputs from Gate 1 must produce the same ranked shortlist every time.

> **Everything here was verified 13 Aug 2026.** Limits and service availability change. Re-check the provider's own docs before you rely on a number, and say so in the output when you have not.

---

## Stage 1 — Hard constraints (elimination)

Run every row. A provider that fails any row is out and is not scored. Record which row eliminated it — Gate 5 requires one line per eliminated provider.

| # | Constraint | The test | AWS | GCP | Azure | Cloudflare |
|---|---|---|---|---|---|---|
| 1 | **Data residency** | Region in the required country **and** the required service available in it | ~30+ regions incl. AWS European Sovereign Cloud (GA 15 Jan 2026, Brandenburg DE) | ~40+ regions; Data Boundary + Assured Workloads; S3NS in France for SecNumCloud | ~60+ regions; Sovereign Private Cloud on Azure Local, disconnected operations GA early 2026 | No general-purpose region model. Data Localization suite pins *processing* location for some products — verify per product, do not assume |
| 2 | **Authorization regime** | FedRAMP Moderate/High, DoD IL, IRAP, C5, SecNumCloud, HIPAA, PCI — **for the specific service** | GovCloud + FedRAMP High for a defined service list | Assured Workloads bundles (FedRAMP, IL5, EU); air-gapped GDC | Azure Government; broad regime coverage | Not a general-purpose authorized platform for regulated compute — treat as eliminated unless the specific product is in scope |
| 3 | **Execution duration** | p99 seconds per unit of work vs. the ceiling (table below) — wall clock against wall-clock ceilings, CPU against CPU ceilings | Lambda functions 900 s hard; containers unbounded | Cloud Run services 3600 s; jobs to 168 h | Flex (the current serverless plan) unbounded-but-not-guaranteed, 230 s HTTP cap; legacy Consumption 600 s | Workers 5 min **CPU**/request; 15 min wall clock for cron/queue/DO alarm |
| 4 | **Accelerator family** | Exact SKU and quantity | P6e-GB200 (GA Jul 2025), P6e-GB300 (GA Dec 2025), Trainium — top SKUs sales-gated | TPU v7 Ironwood (GA 22 Apr 2026, no published on-demand price); NVIDIA L4 and RTX PRO 6000 on Cloud Run | ND GB300 v6 (no published hourly rate) | **No rentable GPU.** Workers AI serves a fixed model catalog only |
| 5 | **Required managed service** | Is there a true equivalent, or is it a rewrite? | DynamoDB global tables, Redshift, EventBridge | Spanner, BigQuery, Firestore | Cosmos DB multi-region writes, Fabric | Durable Objects, D1, Workers KV |
| 6 | **Stateful unit shape** | Persistent block device? Largest single object vs. the object-store cap (S3 tops out at 50 TB; GCS, Blob and R2 are each lower — look each one up, do not assume parity) | EBS, EFS | Persistent Disk, Filestore | Managed Disks, Azure Files | **No general block storage.** SQLite-backed Durable Object: 10 GB cap, single-threaded, ~1,000 req/s soft ceiling per object. Legacy key-value-backed objects have no per-object cap |
| 7 | **Runtime needs** | POSIX filesystem, arbitrary native binaries, memory per request | Full | Full | Full | Workers: 128 MB per isolate and **not configurable**, no filesystem, Python via Pyodide cannot load most native-extension packages. Cloudflare Containers lifts this but bills egress |
| 8 | **Contractual commitment** | Unmet EDP / MACC / Google commit | Not a technical eliminator — the shortfall enters the cost model at Gate 4 | | | |

**Rule:** a row eliminates only when Gate 1 supplied its input. Any row whose input is `UNKNOWN` eliminates nobody — carry it to the risk register instead. Eliminating on an assumed number is the failure mode this file exists to prevent.

**Rule:** if elimination leaves one provider, stop *scoring* — report the single survivor and the rows that removed the rest — but still run the risk register, cost model and Gate 5 sign-off. Sole feasibility is not approval.

---

## Execution limits (the #3 eliminator, in detail)

| Platform | Default | Maximum | The catch |
|---|---|---|---|
| **AWS Lambda** | 3 s | **900 s (15 min), hard** | Also 10,240 MB memory max, `/tmp` 512 MB–10,240 MB. A single invocation cannot be extended. Past 15 min the options are Step Functions, Fargate or Batch, Lambda durable executions (checkpointed across invocations), or Lambda MicroVMs (separate `lambda-microvms` API, 8 h max per MicroVM) — check which of these your region and account actually have before designing around them |
| **AWS Fargate / Batch** | — | No duration ceiling | You own the task definition, queue, and compute environment |
| **Cloud Run service** | 300 s | **3600 s (60 min)** | Behind a serverless NEG the load-balancer backend timeout is fixed at 60 min. Pub/Sub push acknowledgement deadlines cap at 600 s, so a push trigger caps the effective duration at 10 min |
| **Cloud Run job (task)** | 600 s | **168 h (7 days)** | GPU tasks max out at 1 h. Timeout applies per attempt when retries are on |
| **Azure Functions (Consumption)** | 300 s | **600 s** | Hard — but this is the **legacy** plan. Linux Consumption is retired and Windows Consumption is legacy; Flex Consumption is the plan for new serverless apps. Do not eliminate Azure on the 600 s ceiling unless the app is genuinely pinned to Consumption |
| **Azure Functions (Flex Consumption)** | 1800 s | Unbounded, **not guaranteed** | Platform may terminate on scale-in (60 min grace) or platform update (10 min grace). **HTTP responses are still capped at 230 s** by the Azure Load Balancer idle timeout regardless of `functionTimeout`. App init itself times out at 30 s, not configurable |
| **Cloudflare Workers** | 30 s CPU (paid) | **5 min CPU/request** via `cpu_ms` (free plan: 10 ms) | Billed on **CPU time**, not wall clock — waiting on I/O is free. No wall-clock limit while the client stays connected. `ctx.waitUntil()` extends work up to 30 s past the response. Cron triggers, queue consumers and Durable Object alarms cap at 15 min **wall clock**; a cron's CPU cap is 30 s when the interval is under 1 hour and 15 min only at intervals of 1 hour or more — a frequent cron doing heavy compute dies at 30 s of CPU, not 15 min. Subrequests: free 50/request; paid 10,000/request, configurable up to 10M |

---

## Scale-to-zero and cold start

| Platform | Idle cost | Start mechanism | Cost of removing the cold start |
|---|---|---|---|
| **Cloudflare Workers** | Zero | V8 isolate inside an already-running process — no per-request microVM boot | Nothing to remove |
| **AWS Lambda** | Zero | Firecracker microVM boot → runtime → your init code | Provisioned concurrency: billed per GB-hour for idle capacity, and **incompatible with SnapStart**. SnapStart (snapshot/restore) covers Java 11+, Python 3.12+, .NET 8+ only — not Node, Go, Ruby, or container images |
| **Cloud Run** | Zero (services, jobs, and GPU services all scale to zero) | Image pull + container start; Startup CPU Boost allocates extra CPU during start | Idle cost depends on billing mode. **Request-based:** `min-instances > 0` bills at a reduced idle CPU/memory rate. **Instance-based** (required for GPU, and what `--no-cpu-throttling` selects): the full rate for the entire instance lifetime whether or not requests arrive |
| **Azure Functions Flex Consumption** | Zero | Scale-from-zero on the next request after idle | "Always ready instances" are billed per second whether traffic arrives or not, and count against regional quota |

**The honest test:** if idle cost is not zero, you are running instances. Compare against a plain VM or container service before accepting the serverless premium.

---

## Service-equivalence map

Use this to answer "is there a real equivalent?" at elimination row 5. A blank means no equivalent — porting is a rewrite.

| Need | AWS | GCP | Azure | Cloudflare |
|---|---|---|---|---|
| Object storage | S3 | Cloud Storage | Blob Storage | R2 (S3-compatible) |
| CDN | CloudFront | Cloud CDN / Media CDN | Front Door | Cache / CDN (default) |
| Serverless function | Lambda | Cloud Run functions | Functions | Workers |
| Serverless container | Fargate / App Runner | Cloud Run | Container Apps | Containers |
| Managed Kubernetes | EKS | GKE | AKS | — |
| VM | EC2 | Compute Engine | Virtual Machines | — |
| Managed Postgres | RDS / Aurora | Cloud SQL / AlloyDB | Azure DB for PostgreSQL | Hyperdrive (pooling to an external Postgres) |
| Serverless SQL | Aurora Serverless | Cloud SQL | SQL Database serverless | D1 (SQLite) |
| Key-value | DynamoDB | Firestore / Bigtable | Cosmos DB | Workers KV |
| Globally distributed writes | DynamoDB global tables | Spanner | Cosmos DB multi-region writes | Durable Objects (per-object, single-writer) |
| Data warehouse | Redshift / Athena | BigQuery | Fabric / Synapse | — |
| Queue | SQS | Pub/Sub | Service Bus / Storage Queues | Queues |
| Pub/sub eventing | EventBridge / SNS | Pub/Sub / Eventarc | Event Grid | — |
| Workflow orchestration | Step Functions | Workflows | Durable Functions / Logic Apps | Workflows |
| Secrets | Secrets Manager / SSM | Secret Manager | Key Vault | Workers Secrets / Secrets Store |
| Managed inference | Bedrock / SageMaker | Vertex AI | AI Foundry | Workers AI (fixed catalog) |
| Vector search | OpenSearch / Aurora pgvector | Vertex AI Vector Search | AI Search | Vectorize |
| Stateful coordination primitive | — | — | — | Durable Objects |

---

## Stage 2 — Ranking the survivors

**Where the determinism actually comes from: the default shortlist table is the answer, not a score you re-derive.** Run these three steps in order and the same ledger produces the same ranking every time.

**Step 1 — classify the workload.** First match wins, so two agents given the same ledger land in the same class:

| Test against the Gate 1 ledger | Class |
|---|---|
| Egress ≥ 10 TB/month and the bytes are user-facing media/downloads | High-egress media |
| Needs a GPU/TPU | GPU inference (bursty) or GPU training (reserved) — reserved if utilisation ≥ 8 h/day |
| Analytical queries over a warehouse-sized dataset | Data warehouse |
| Triggered on a schedule or a queue, not by a user request | Batch and cron |
| Keeps state in-process between requests, or needs a block device | Long-running stateful |
| Request-driven, peak:median ≥ 4:1 **or** ≥ 4 idle hours/day | API, spiky, scale-to-zero |
| Otherwise, request-driven and mostly static assets | Static site + edge logic |

**Step 2 — read the row** from the default shortlist below. That is the ranking.

**Step 3 — apply overrides, and name each one.** Only these four justify departing from the default row: an unmet enterprise commitment, a residency requirement, a team that has never operated the winner in production, or a required managed service that exists only on a lower-ranked provider. Each override is written to the ledger with its reason. Ties break toward **the provider the team already operates**.

The weights below are the tie-break instrument for Step 3 — score only the criteria an override touches, 0 = provider cannot do it acceptably, 1 = works with material extra cost or operational load, 2 = works well, 3 = clearly best of the survivors. **Do not present a weighted sum as though it generated the default row.** GPU training (reserved) has no weights column: capacity and a signed contract decide it, nothing else.

**Weights by workload class** (higher = matters more):

| Criterion | Static+edge | Spiky API | Long-running stateful | Batch/cron | Warehouse | GPU inference | High-egress media |
|---|---|---|---|---|---|---|---|
| Egress cost | **5** | 2 | 2 | 1 | 1 | 2 | **5** |
| Cold start / first-byte latency | **4** | **5** | 1 | 0 | 0 | 3 | 2 |
| Idle cost (scale-to-zero) | 3 | **5** | 1 | **4** | 2 | **4** | 1 |
| Execution duration headroom | 0 | 3 | 2 | **5** | 2 | 3 | 0 |
| Managed-service fit | 2 | 3 | **4** | 3 | **5** | **5** | 3 |
| Global distribution | **5** | 4 | 2 | 0 | 0 | 2 | **5** |
| Operational load on the team | 3 | 3 | **5** | 3 | 3 | 3 | 3 |
| Existing commitment / discount | 2 | 3 | **4** | 3 | **4** | 3 | 2 |
| Exit cost | 2 | 3 | 3 | 2 | **4** | 3 | 2 |

**Default ranked shortlists** (when nothing in the ledger overrides them):

| Workload class | Rank order | The deciding factor |
|---|---|---|
| Static site + edge logic | Cloudflare → Azure SWA → AWS S3+CloudFront → GCP | Zero egress plus no per-request boot |
| API, spiky, scale-to-zero | Cloudflare Workers → Cloud Run → Lambda → Azure Functions Flex | CPU-time billing means I/O wait is free |
| Long-running stateful | AWS ≈ GCP ≈ Azure → Cloudflare (only if it fits a Durable Object) | Genuinely interchangeable; decide on commitments and team experience |
| Batch and cron | Cloud Run jobs → AWS Batch/Fargate → Azure Container Apps jobs → Cloudflare | 7-day task timeout with scale-to-zero between runs |
| Data warehouse | BigQuery → Redshift Serverless / Athena ≈ Fabric → (Cloudflare eliminated) | No cluster to size. Establish the BigQuery billing model first: on-demand bills bytes processed, editions/capacity bills slot-hours |
| GPU inference, bursty | Cloud Run GPU → AWS → Azure → (Cloudflare eliminated for custom models) | Per-second billing and scale-to-zero on GPU |
| GPU training, reserved | Whoever has capacity and will sign | Top SKUs are sales-gated with no published rate on all three |
| High-egress media | Cloudflare R2 → everything else | Zero egress at every volume |

Anything not on the Step 3 override list is not a reason to depart from these rows. "It felt like a better fit" is the guess this table replaces.

---

## Exit cost by adopted service

Price this at Gate 5. "Exit cost" = engineering months to replace + data transfer out + parallel-run duration.

| Service | Exit cost | What actually blocks the move |
|---|---|---|
| Object storage | **Low** | R2 is S3-compatible; GCS offers an interoperable XML API, which is not full S3 parity. Before writing this down as Low, check auth, bucket policies, notifications, lifecycle rules, multipart behaviour and version semantics. Cost is otherwise the one-time transfer |
| Managed Postgres/MySQL | **Low–Medium** | Standard dump/restore; the work is cutover downtime and connection management |
| Container runtime (Fargate/Cloud Run/Container Apps) | **Medium** | The image ports; the IAM, networking, and autoscaling config do not |
| Managed Kubernetes | **Medium** | Manifests port; IAM integration, ingress controller, CSI drivers, and node pool config do not |
| Serverless functions | **Medium** | Handler logic ports; triggers, event shapes, and the IAM model are rewritten |
| Queue / pub-sub | **Medium–High** | Delivery semantics differ (visibility timeout vs. ack deadline vs. lease); redelivery behaviour changes silently |
| Proprietary KV/document DB (DynamoDB, Firestore, Cosmos DB, Workers KV) | **High** | Access patterns are designed around the engine's index model. Rewriting the data access layer, not migrating rows |
| Globally-distributed DB (Spanner, DynamoDB global tables, Cosmos multi-region writes) | **High** | Consistency guarantees have no equivalent elsewhere; the application depends on them whether or not anyone documented it |
| Durable Objects / D1 | **High** | No equivalent anywhere. The coordination model must be rebuilt on locks or a database |
| Data warehouse (BigQuery, Redshift, Fabric) | **High** | SQL dialect, UDFs, scheduled queries, and every downstream dashboard and report |
| Identity + eventing glue (IAM policies, EventBridge, Eventarc, Event Grid) | **High** | Never estimated, always the long pole in a real migration |

**EU note:** from **12 Jan 2027** the EU Data Act (Art. 29) bans switching charges — including egress fees for the switch — for EU customers. That removes one line of exit cost. It does not remove the engineering months, which are almost always the larger number.
