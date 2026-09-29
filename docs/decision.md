---
layout: page
title: "DSQL Adoption Decision Report"
question: "Can we run our workloads on Aurora DSQL, and if so, under what conditions?"
lang: "en"
permalink: /decision/
updated_at: "2026-09-29"
---

## Conclusion

**Aurora DSQL can be used for production OLTP. It is not the right choice for every service, however; it has the advantage for services that meet the conditions below.** This judgment is based on 12 experiments (2026-09-24–29) that ran the same order-processing workload in the Seoul Region on DSQL and on three existing services (RDS PostgreSQL Multi-AZ, Aurora Provisioned, and Aurora Serverless v2). Of the 12, E001, E002, and E004 were run at close to their planned scale, and the other nine were run at minimum scope (MVP).

DSQL was clearly ahead in four areas. First, its throughput scaled the furthest. With 256 connections it handled more than 24,000 transactions per second within the latency target, while every existing service exceeded the target under the same conditions (E002). Second, it costs almost nothing when there are no requests, and after 15 minutes idle it answered the first request within 0.3 seconds (E009, E010). Third, committed data was immediately visible from any connection, so no read routing was needed (E005). Fourth, a cluster was created in a little over 30 seconds, and there was no capacity, password, or vacuum to manage (E011).

The costs are just as clear. Latency per request was 3–6 times that of Aurora (write p95 about 30–40 ms), about half of the existing PostgreSQL SQL had to be rewritten (E001), and application code for conflict retries, transaction splitting, and connection rotation was mandatory (E004, E008, E003). When writes concentrated on the same rows, as with a popular product, a large share of requests still failed after retries (E004), and there was no point-in-time restore to a chosen moment (E007). When high load continued all day, the per-request pricing cost more than a fixed instance (E010).

## Results by criterion

| Criterion | DSQL | Existing services | Verdict | Evidence |
| --- | --- | --- | --- | --- |
| Correctness | 0 invariant violations with retries and business-ID receipts implemented (66 contention cells, connection failures) | Same | Equal | [E004](../experiments/e004/), [E006](../experiments/e006/) |
| Throughput (256 connections, within SLO) | 24,541 TPS or more | A2 11,559, R1 5,839, A1 3,375 TPS | DSQL ahead | [E002](../experiments/e002/) |
| Request latency | Write p95 about 28–41 ms, read about 5–10 ms | A2 write about 7–9 ms, read about 2–3 ms | Existing ahead | [E002](../experiments/e002/) |
| Surge (up to 2,000 TPS) | 0 failures without intervention | A2 0 failures without intervention | Equal | [E009](../experiments/e009/) |
| First request after 15 minutes idle | Connection in 0.1–0.3 seconds | A2 (paused at 0 ACU) failed after more than 15 seconds | DSQL ahead | [E009](../experiments/e009/) |
| Read immediately after write | Immediate, even from other connections | A2 reader after 22–33 ms | DSQL ahead | [E005](../experiments/e005/) |
| Connections | New connection p50 15 ms, 0 rejections with 1,000 concurrent connections. Throughput 1/70 with a new connection per request | Not compared | Pool required | [E003](../experiments/e003/) |
| Concentrated contention | Conflicts at commit time; 39% final failure after retries at 256 connections | Throughput dropped sharply from lock waits (small instance) | Design needed for both | [E004](../experiments/e004/) |
| Online DDL | Async index on 11 million rows in 12 minutes; write p99 30.7→79.8 ms during build, SLO maintained | Not compared | Possible | [E008](../experiments/e008/) |
| Transaction limits | Rejected above 3,000 rows or 300 seconds | No limit | Constraint | [E008](../experiments/e008/) |
| Large aggregation | Full aggregation of 11 million rows in 36 seconds, about 1,700 DPU. Little interference with OLTP | Not compared | Constraint | [E012](../experiments/e012/) |
| Recovery from mistakes | No point-in-time restore. Full backup (481 seconds) → new cluster (129 seconds) | A2 point-in-time restore 587 seconds | Existing ahead (RPO) | [E007](../experiments/e007/) |
| SQL migration | 16 of 35 needed changes | Almost no changes | Existing ahead | [E001](../experiments/e001/) |
| Infrastructure operations | Create 32 seconds, delete about 2 minutes, no capacity, passwords, or vacuum | A2 create about 11 minutes, delete about 15 minutes | DSQL ahead | [E011](../experiments/e011/) |
| Cost | About $0.31 per million requests, nearly 0 when idle, storage $0.40/GB-month | R1 about $1.22 per hour (fixed), storage $0.12–0.131/GB-month | Depends on load shape | [E010](../experiments/e010/) |

The verdict indicates which side was more favorable for the adoption decision within the scope of these measurements. "Equal" means we found no difference; it is not proof that there is no difference.

## Services that suit DSQL

- **Newly designed services:** If a service is built from the start to fit DSQL's SQL coverage, retries, and transaction limits, most of the migration burden seen in E001, E004, and E008 disappears.
- **Services with spiky load or long idle periods:** When average load was below about 1,100 TPS (for this workload), DSQL was cheaper than a fixed instance; for a pattern of 8 hours of work and 16 hours of idle, it was about $9 versus about $29 per day. The first request after an idle period is handled right away.
- **Services with unpredictable surges:** Without provisioning capacity in advance, latency barely increased up to 24,000 TPS.
- **Teams with few operations staff:** There is no need to manage instance sizes, failover standby replicas, password rotation, vacuum, or reader routing.
- **Screens that must read their own writes immediately:** There is no replication lag, so no routing code is needed for reads right after writes.

## Services that should avoid DSQL or adopt it with caution

- **Services that depend heavily on existing PostgreSQL features:** Code that uses PL/pgSQL or triggers, sequences or `serial`, temporary tables, partitions, READ COMMITTED, or `statement_timeout` must be rewritten.
- **Workloads where writes concentrate on the same rows:** When contention concentrates on a few rows, such as stock for a popular product or a global counter, a large share of requests still fails after retries. Schema redesign, such as splitting rows, has to come first.
- **Workloads where per-request latency matters:** If one request runs several queries one after another, DSQL's per-query latency (write about 30–40 ms) accumulates.
- **Services with high load all day:** When an average of several thousand TPS is sustained, DPU charges proportional to request count cost more than a fixed instance.
- **Services with heavy bulk batch or analytics work:** Batches must be split to fit the 3,000-row and 300-second limits, and large aggregations are billed in proportion to the work processed. Separating analytics into another store is the safer option.
- **Services that need recovery points to the second:** Without point-in-time restore, the latest recoverable point is determined by the backup interval.
- **Services with very large data:** The storage unit price is about 3.3 times that of Aurora.

## Required preparation before adoption

Concrete code examples and rules for coding agents are in the [Aurora DSQL usage guide](../guide/).

1. **Retries and idempotency:** Retry commit-time conflicts (`40001`), and for requests whose commit response was not received, check the result by business ID before retrying (E004, E006).
2. **Transaction splitting:** Split loads, deletes, and batches that exceed 3,000 rows or 300 seconds (E008).
3. **Connection management:** Use a connection pool, rotate connections within 1 hour, reuse IAM tokens, and prevent token signing from piling up when many first connections happen at once (E003).
4. **Schema deployment procedure:** Use an async index only after checking `indisvalid`, and create indexes on the referencing side of foreign keys yourself (E002, E008).
5. **Recovery design:** Set the AWS Backup interval according to the acceptable data-loss window, and prepare the procedure for switching to a new cluster (endpoint and IAM permission changes) (E007).
6. **Cost check:** Measure DPU per request with the real workload, and compare with a fixed instance using average load and idle ratio (E010).

## Limits of the evidence

- **Scale of runs:** Most measurements were taken once. Variance between repetitions was not measured, and nine experiments are MVPs that ran only part of their plan. The missing scope is listed under "Deviations from the plan" in each report.
- **Comparison conditions:** The throughput searches for DSQL and the control groups were run on different days with different runner specifications. DSQL used a public endpoint, and the control groups used a path inside the VPC. The control group in E004 was a small burstable instance.
- **DSQL ceiling:** The DSQL throughput of 24,541 TPS is a lower bound near load-generator saturation, not the service ceiling.
- **Workload and data:** One order-processing workload mix and about 5 GiB of data. Latency, DPU, and cost may differ for other query patterns or TB-scale data.
- **Availability:** DSQL internal failures, AZ failures, and Aurora and RDS failover times were not measured.
- **Cost:** Seoul Region On-Demand prices and linear extrapolation of measured values. Discounts and credits, per-load separation of Aurora I/O, and human working time are not included in the amounts.

## Experiment list and cost

| Experiment | Topic | Scope | Report |
| --- | --- | --- | --- |
| E001 | SQL compatibility | Planned scope | [View](../experiments/e001/) |
| E002 | OLTP throughput and latency | Up to search, main measurement excluded | [View](../experiments/e002/) |
| E003 | Connections and authentication | MVP, DSQL only | [View](../experiments/e003/) |
| E004 | Contention and correctness | Planned conditions, small control group | [View](../experiments/e004/) |
| E005 | Read immediately after write | MVP, D1 and A2 | [View](../experiments/e005/) |
| E006 | Connection failures and commit durability | MVP, D1 and A2 | [View](../experiments/e006/) |
| E007 | Backup and recovery from mistakes | MVP, D1 and A2 | [View](../experiments/e007/) |
| E008 | Online DDL and limits | MVP, DSQL only | [View](../experiments/e008/) |
| E009 | Surges and resuming after idle | MVP, D1 and A2 | [View](../experiments/e009/) |
| E010 | Cost and selection criteria | MVP, extrapolated from measurements | [View](../experiments/e010/) |
| E011 | Development and operations convenience | MVP, compiled from work logs | [View](../experiments/e011/) |
| E012 | Large queries and interference | MVP, DSQL only | [View](../experiments/e012/) |

The experiment cost for the whole study is about $74. E001 (about $0.08), E004 (about $2.07), and the first E002 run ($48.57) are billed amounts from Cost Explorer. The second E002 run together with E003, E008, and E012 (about $18.6), and E005, E006, E007, and E009 (about $5.1) are estimates calculated from CloudWatch usage and have not yet been reconciled with billed amounts. All experiment resources were deleted, and zero remaining resources was confirmed after each run.
