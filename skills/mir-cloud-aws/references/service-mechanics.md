# AWS service mechanics — limits, delivery semantics, and failure modes

Read at **Gate 5** when mapping workloads to services, and at **Gate 7** for the reliability review.

> Retrieved **25 August 2026** from AWS documentation. Quotas move and several below are soft. **Re-check the service's own quota page before a number enters a design, and check Service Quotas for what *your* account actually has** — new accounts ship with reduced Lambda concurrency and memory quotas that AWS raises automatically with usage, so an early load test measures the quota, not the architecture.
>
> Cross-provider comparison numbers (Lambda's 900 s ceiling versus Cloud Run and Workers, egress tiers, per-object caps, accelerator availability) live in `mir-cloud/references/provider-decision-tables.md` and `cost-model.md`. They are not repeated here.

---

## 1. Lambda quotas that change a design

| Limit | Value | Consequence |
|---|---|---|
| Memory | 128 MB – 10,240 MB, 1 MB steps | CPU is proportional; **1,769 MB ≈ 1 vCPU**. A CPU-bound function is often cheaper at higher memory |
| Timeout | 900 s hard | See the pillar's Gate 3 row before designing around it |
| Invocation payload | **6 MB** request and response (sync), **1 MB** async, 200 MB streamed response | The 6 MB cap also bounds a batch; SQS/Lambda metadata counts toward it |
| Streamed-response bandwidth | uncapped for the first 6 MB, then 2 MB/s | Large streamed downloads are slower than they look |
| `/tmp` | 512 MB default, configurable to 10,240 MB | The default is why "it worked locally" fails on a big file |
| Environment variables | 4 KB total | Not a config store. Use SSM/Secrets Manager and read at init |
| Layers | 5 per function | |
| Deployment package | 50 MB zipped upload (S3 for larger), 250 MB unzipped incl. layers; 10 GB container image | The image path exists precisely because of the 250 MB wall |
| Concurrency | 1,000 default, per Region, soft | Scaling: 1,000 new execution environments per function per 10 s |
| Control-plane APIs | **15 rps across all of them combined** (excluding invoke, `GetFunction`, `GetPolicy`) | A deploy script that describes every function throttles itself |
| ENIs per VPC | 500 default, shared with EFS | VPC-attaching functions is a capacity decision |
| File descriptors / threads | 1,024 | |

**Environment variables are not secret storage.** Anyone with `lambda:GetFunctionConfiguration` reads them. Store the secret in Secrets Manager or SSM `SecureString`, fetch at init, cache in the execution environment.

**Concurrency arithmetic is the outage.** API Gateway's default is 10,000 rps; Lambda's is 1,000 concurrent. Downstream, an RDS instance has a connection cap far below either. Reserved concurrency on the function protects the database from the API; without it, the first traffic spike exhausts connections and every function fails at once. Set reserved concurrency and use RDS Proxy for connection reuse.

---

## 2. Delivery semantics — write this into the Gate 5 design

| Source | Ordering | Duplicates | Retry owner | Must have |
|---|---|---|---|---|
| SQS standard | none | yes | SQS visibility timeout | idempotency key, DLQ via `maxReceiveCount`, partial batch responses |
| SQS FIFO | per message group | dedup window only | SQS | a message-group id that partitions the work; batch size max 10 |
| SNS → HTTP/SQS/Lambda | none | yes | SNS delivery policy | a DLQ on the **subscription**, not only on the queue |
| EventBridge rule → target | none | yes | EventBridge retry policy | a DLQ configured on the target; note the event-size limit |
| Kinesis / DynamoDB Streams | per shard | yes | the iterator | `BisectBatchOnFunctionError`, `MaximumRetryAttempts`, `MaximumRecordAgeInSeconds`, an on-failure destination |
| Async Lambda invoke | none | yes | Lambda's internal queue (up to 6 h) | an on-failure destination; the caller sees only a 202 |
| S3 event notification | none | at-least-once | S3 | idempotency; note events can be delayed, and a filter is prefix/suffix only |

**At-least-once is the default everywhere that matters.** Idempotency is a Gate 5 design decision, not Gate 6 hardening. The mechanism — a natural idempotency key, a conditional write, a dedupe table with a TTL — belongs in `mir-backend`; what belongs here is the requirement and which hop imposes it.

### SQS → Lambda, specifically

- Queue visibility timeout ≥ **6×** the function timeout, plus `MaximumBatchingWindowInSeconds` if batching. Lambda now validates that the function timeout is **≤** the visibility timeout and rejects the event source mapping otherwise.
- Without `ReportBatchItemFailures`, **any** error returns the whole batch to the queue. With batch size 10, one poison message reprocesses nine good ones per attempt. Return the failed message ids.
- `maxReceiveCount` ≥ 5 on the redrive policy, and a DLQ that someone actually alarms on. A DLQ with no alarm is a data-loss folder.
- Scaling: five concurrent batches initially, then up to 300 additional concurrent invocations per minute, to a ceiling of 1,250 (or the configured maximum concurrency / provisioned pollers).
- Batch size up to 10,000 for standard queues, 10 for FIFO; a batch size over 10 requires a batching window ≥ 1 s. The real cap is the 6 MB payload.
- Encrypted queue → the execution role needs `kms:Decrypt`.

### The poison-pill on an ordered source

A record that always throws blocks its Kinesis or DynamoDB Streams shard until the record ages out — hours of stalled processing from one bad payload. `BisectBatchOnFunctionError` narrows it, `MaximumRetryAttempts` and `MaximumRecordAgeInSeconds` bound it, and an on-failure destination captures it. AI-generated stream consumers set none of these.

---

## 3. S3

- **Strongly consistent read-after-write** for PUTs, overwrites and deletes since December 2020, for all requests. Code that sleeps to "let S3 catch up" is cargo cult. Note that *bucket configuration* changes (policies, ACLs, replication) remain eventually consistent.
- Object size: 5 GB maximum for a single `PutObject`; multipart to 5 TB with up to 10,000 parts. **Abort incomplete multipart uploads with a lifecycle rule** — otherwise you pay storage for parts that were never assembled and cannot see them in a normal listing.
- Naming: bucket names are globally unique and **released when the bucket is deleted**. Anyone can recreate a retired name and serve to every client still pointing at it. Retire names permanently.
- Block Public Access is four independent flags: `BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`, `RestrictPublicBuckets`. Set them at the **account** level as well as the bucket, so a new bucket cannot opt out. Since April 2023 new buckets get all four plus `BucketOwnerEnforced` (ACLs disabled) by default — **existing buckets were not changed**.
- Prefer **`aws:SourceVpce` / VPC gateway endpoint** conditions over IP allow-lists. Traffic through an S3 gateway endpoint also avoids NAT Gateway processing charges (the pillar's cost model covers the arithmetic).
- Encryption: SSE-S3 is on by default. Use SSE-KMS with a customer-managed key where you need a key policy as a second authorization layer, and enable **S3 Bucket Keys** — without them a KMS request per object turns into a throttling and cost problem on high-request workloads.
- Lifecycle rules and storage-class transitions have minimum-duration and per-object charges; a bucket of many small objects can cost more in transitions than it saves.

### Presigned URL rules (mechanics for the `SKILL.md` section)

- Lifetime is `min(requested expiry, credential lifetime)`. IAM user + SigV4: up to **7 days**. Role session: dies with the session (1 h default, `MaxSessionDuration` at most). EC2 instance profile: with the rotating credential, ~6 h. Console-generated: 1 minute to 12 hours.
- They are bearer tokens; they carry the signer's authority and cannot be revoked individually.
- Controls: `s3:signatureAge` deny in the bucket policy, `aws:SourceIp` / `aws:SourceVpc` / `aws:SourceVpce` network-path conditions, one URL per object per user issued after the ownership check.
- A download in progress when the URL expires continues; a resumed download after expiry fails.

---

## 4. DynamoDB

Numbers here are long-standing but **verify before quoting**:

- Item size limit **400 KB** including attribute names. Large payloads go to S3 with a pointer in the item.
- Per-partition throughput ceiling — roughly 3,000 read and 1,000 write units — so a hot partition key throttles while the table looks under-provisioned. Design the partition key for spread, not for readability.
- `Query` and `Scan` return at most 1 MB per page; a `Scan` on a large table is a full-table cost. Paginate, and treat `Scan` in production code as a design smell.
- `TransactWriteItems` is limited (100 items / 4 MB at time of writing) and costs double the write units. It is not a general transaction manager.
- **Global tables give you a multi-Region data plane, not a multi-Region control plane** — that distinction is the October 2025 lesson in `SKILL.md`. Replication is last-writer-wins on a timestamp; concurrent writes in two Regions silently lose one.
- On-demand versus provisioned is a cost *and* a behaviour decision: on-demand absorbs spikes but has its own scaling ramp; provisioned with auto-scaling reacts in minutes, not seconds.
- Streams: 24-hour retention, per-shard ordering, at-least-once to consumers.

Exit cost is high — there is no DynamoDB elsewhere. Ledger it at Gate 5 (`mir-cloud` carries the exit-cost table).

---

## 5. Networking, briefly

- **VPC gateway endpoints** (S3, DynamoDB) are free and keep that traffic off the NAT Gateway; **interface endpoints** (PrivateLink) bill per hour per AZ plus per GB. Which one exists per service decides the route and the bill — trace the route table before estimating.
- A Lambda in a VPC has **no internet access** without a NAT Gateway or an interface endpoint. This surprises people whose function calls a third-party API.
- Security groups are stateful and allow-only; NACLs are stateless and need both directions. A "mysterious" one-way failure is usually a NACL.
- Cross-AZ traffic is billed each way. A chatty three-AZ service pays for its own internal chatter (the pillar's cost model has the rates).

---

## 6. Control-plane dependency checklist for a failover

Walk the runbook and mark every step. Anything in the right-hand column is a dependency on the thing that just failed:

| Step | Data plane | Control plane |
|---|---|---|
| Shift traffic | Route 53 ARC routing control; a pre-existing weighted/failover record | creating or editing a Route 53 record set |
| Authenticate | an already-issued session; a **regional** STS endpoint | the global STS endpoint; any IAM policy or role change |
| Scale up | already-running instances; a warm pool; pre-provisioned capacity | `RunInstances`, `UpdateService`, ASG scaling, quota increases |
| Read/write data | a global-table replica; a cross-Region read replica already streaming | promoting a replica; changing replication topology |
| Deploy a fix | an artifact already in the standby Region | a build in the failed Region; an ECR pull from it |
| Page someone | a second channel | a monitoring stack hosted in the failed Region |

Rules that follow: **static stability** — pre-create everything the standby needs so recovery is a data-plane action; keep artifacts and images replicated to the standby Region; and rehearse the failover with the control plane assumed unavailable, because that is the condition under which you will need it.
