---
layout: page
title: "Aurora DSQL Usage Guide"
question: "Rules that people and coding agents must follow when building and operating services on DSQL"
lang: "en"
permalink: /guide/
updated_at: "2026-09-29"
---

## Conclusion

This guide turns the problems we actually hit, and how we solved them, in experiments E001–E012 (run in the Seoul Region on 2026-09-24–29) into rules. People should read the explanation in each section; coding agents should read "Rules for coding agents" directly below first. Each rule links to the experiment report it is based on. DSQL's supported feature set keeps changing, so if an error message differs from this guide, the [official AWS documentation](https://docs.aws.amazon.com/aurora-dsql/latest/userguide/) takes precedence. For whether to use DSQL at all, see the [adoption decision](../decision/).

## Rules for coding agents

This section lists the rules a coding agent must follow when writing or changing code for DSQL. **MUST** marks rules whose violation actually caused errors or data problems; **SHOULD** marks rules tied to performance or cost problems.

**Connections**

1. **MUST:** Authenticate with an IAM token instead of a password. User `admin`, database `postgres`, port 5432, `sslmode=verify-full`. Sign the token with `boto3.client("dsql").generate_db_connect_admin_auth_token(Hostname=..., Region=..., ExpiresIn=900)`. ([E003](../experiments/e003/))
2. **MUST:** Cache and reuse the token per process (for up to 10 minutes), and take a lock so that multiple threads do not sign at the same time when the cache is empty. Concurrent signing caused the instance credential lookup to fail (`NoCredentialsError`). ([E002](../experiments/e002/))
3. **MUST:** Use a connection pool and set the connection lifetime shorter than 1 hour (for example, 50 minutes). DSQL closes connections older than 1 hour. ([E002](../experiments/e002/))
4. **SHOULD:** Do not open a new connection per request. Throughput fell to about 1/70 of that with persistent connections. ([E003](../experiments/e003/))

**Transactions**

5. **MUST:** Retry every write transaction on SQLSTATE `40001` (commit-time conflict). Validated with exponential backoff plus jitter, at most 3 attempts, and a total deadline of 2 seconds. ([E004](../experiments/e004/))
6. **MUST:** For writes, insert a receipt row that records the business ID (idempotency key) in the same transaction. If the commit response is not received (connection lost), retry only after querying by business ID whether the commit happened. ([E004](../experiments/e004/), [E006](../experiments/e006/))
7. **MUST:** Do not change more than 3,000 rows or run longer than 300 seconds in one transaction. Exceeding either is rejected with `54000` (`transaction row limit exceeded`, `transaction age limit of 300s exceeded`). Split bulk work into batches of fewer than 3,000 rows. ([E008](../experiments/e008/))
8. **MUST:** Do not specify an isolation level. DSQL supports only REPEATABLE READ, and statements such as `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` are rejected. ([E001](../experiments/e001/))
9. **MUST:** Do not assume that `SELECT ... FOR UPDATE` makes other transactions wait. In DSQL nothing waits; one side fails with `40001` at commit time. ([E001](../experiments/e001/), [E004](../experiments/e004/))
10. **MUST:** Do not mix DDL and DML in the same transaction. A table created after a transaction started was not visible in that transaction (`42P01`). ([E004](../experiments/e004/))

**Schema**

11. **MUST:** Do not use `serial`. Specify `CACHE 65536` explicitly for sequences and identity columns. The default CACHE is rejected. ([E001](../experiments/e001/))
12. **MUST:** Create indexes with `CREATE INDEX ASYNC`, and rely on an index only after waiting until `pg_index.indisvalid` is true. Appearing in `pg_indexes` does not mean the index is usable. It took 12 minutes for 11 million rows. ([E002](../experiments/e002/), [E008](../experiments/e008/))
13. **MUST:** Create an index yourself on the referencing column of a foreign key (for example, `ledger.order_id`). Without it, deleting a parent row scanned the entire child table and hit the 300-second limit. ([E002](../experiments/e002/))
14. **MUST:** Do not use PL/pgSQL functions or triggers, temporary tables, or partitions. Move the logic into the application or into SQL functions. ([E001](../experiments/e001/))
15. **SHOULD:** Avoid designs where writes concentrate on a few rows (a global counter, a single stock row for a popular product). At 256 connections, 39% still failed after retries. Split the rows or batch the writes. ([E004](../experiments/e004/))

**Queries and operations**

16. **MUST:** Do not rely on `statement_timeout` or server-side cancellation. Neither worked. Use client-side deadlines and limits on work size instead. ([E001](../experiments/e001/))
17. **SHOULD:** For aggregations that scan millions of rows or more, split the range or send them to an analytics store. An aggregation over 11 million rows took 36 seconds and about 1,700 DPU. ([E012](../experiments/e012/))
18. **SHOULD:** Do not use the `SHOW max_connections` value (20) as the connection limit. 1,000 concurrent connections all succeeded. ([E003](../experiments/e003/), [E004](../experiments/e004/))
19. **MUST:** Do not assume point-in-time recovery (PITR) exists. Recovery is possible only from an AWS Backup full backup into a new cluster. ([E007](../experiments/e007/))
20. **SHOULD:** Treat `XX000 server unavailable` during data loading as a transient error and retry. ([E002](../experiments/e002/))

## Connections

DSQL has no passwords; for every connection you put a token signed with IAM permissions in place of the password. Signing takes about 0.2 ms with no network call, so the overhead is small, but the first signing took more than 100 ms because it creates the AWS client, and when multiple threads signed for the first time concurrently, the instance credential lookup failed. Below is the approach used in this experiment's tooling.

```python
import threading, time, boto3, psycopg

SYSTEM_CA = "/etc/pki/tls/certs/ca-bundle.crt"      # System CA bundle on Amazon Linux 2023 (our runner)

_LOCK, _CACHE, _TTL = threading.Lock(), {}, 600      # Reuse for less than the token lifetime (15 min)

def dsql_token(host, region):
    now = time.monotonic()
    with _LOCK:                                        # Sign only once even on concurrent first connects
        hit = _CACHE.get(host)
        if hit and now - hit[1] < _TTL:
            return hit[0]
        client = boto3.client("dsql", region_name=region)
        token = client.generate_db_connect_admin_auth_token(Hostname=host, Region=region, ExpiresIn=900)
        _CACHE[host] = (token, now)
        return token

def connect(host, region):
    return psycopg.connect(host=host, port=5432, dbname="postgres", user="admin",
                           password=dsql_token(host, region), sslmode="verify-full",
                           sslrootcert=SYSTEM_CA, connect_timeout=15)
```

- The runner (application) IAM role needs `dsql:DbConnectAdmin` (admin) or `dsql:DbConnect` (user role) permission on the cluster.
- One new connection, including TLS and authentication, took p50 15.5 ms and p99 120 ms. Open the pool's minimum connections in advance.
- Rotate connections within 1 hour of their lifetime. Our tooling reconnected every 50 minutes.

## Transactions and retries

Instead of making transactions wait on locks, DSQL checks for conflicts at commit time (optimistic concurrency control). Of the transactions that change the same row concurrently, one fails at commit with `40001`, so without a retry that request ends in failure. Thanks to this approach, write skew, which PostgreSQL's REPEATABLE READ allows, was also blocked with `40001` in DSQL (E004).

```python
import random, time, psycopg

# Example: assume reconnect() returns a new connection via connect() above.
def run_with_retry(conn, work, op_id, max_attempts=3, deadline_s=2.0, base_s=0.01):
    """work(conn) writes the business data and the receipt (op_id) in one transaction."""
    t0 = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        try:
            with conn.transaction():
                if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                    return "duplicate"                 # Already applied: do not redo
                work(conn)
            return "committed"
        except psycopg.errors.SerializationFailure:     # 40001: commit-time conflict
            delay = random.uniform(0, base_s * 2 ** (attempt - 1))
            if attempt == max_attempts or time.monotonic() - t0 + delay >= deadline_s:
                raise
            time.sleep(delay)
        except psycopg.OperationalError:                # Connection lost during commit: outcome unknown
            conn = reconnect()                          # Retry only after checking the receipt on a new connection
            if conn.execute("SELECT 1 FROM operation_receipts WHERE op_id = %s", (op_id,)).fetchone():
                return "committed"
```

- Define the receipt table with `op_id text PRIMARY KEY`, and insert into it in the same transaction as the business write. With this approach there were 0 lost or duplicated commits in E004's 66 contention cells and in E006's 30-second connection block.
- For bulk changes, query the target IDs once, then split them into chunks of fewer than 3,000 rows and commit each. Re-querying the targets for every batch becomes very slow (E002).

```python
ids = [r[0] for r in conn.execute("SELECT id FROM orders WHERE created_at < %s", (cutoff,)).fetchall()]
for i in range(0, len(ids), 2000):
    with conn.transaction():
        conn.execute("DELETE FROM orders WHERE id = ANY(%s)", (ids[i:i + 2000],))
```

## Schema and indexes

- **ID generation:** Instead of `serial`, declare an identity column or sequence with `CACHE 65536`, or generate IDs such as UUIDs in the application.
- **Index creation:** `CREATE INDEX ASYNC idx ON t (col)` returns immediately, and the build proceeds in the background. Deployment scripts must wait for completion as follows.

```sql
SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'idx';
```

- **Impact during build:** During an index build on 11 million rows (12 minutes), write p99 under a load of 1,000 requests per second rose from 30.7 ms to 79.8 ms, but there were no failures (E008).
- **Foreign keys:** Foreign key constraints work, but no index is created automatically on the referencing column. Create indexes yourself on columns used to delete parent rows or to join.
- **Column changes:** `ALTER TABLE ... ADD COLUMN`/`DROP COLUMN` finished within 0.1 seconds even under load.
- **Unsupported features that require design changes:** PL/pgSQL functions, triggers, temporary tables, partitions. Alternatives for GIN and expression indexes were not verified (E001).

## Queries and performance

- **Latency:** The latency of a single query is longer than on Aurora (write p95 about 28–41 ms, read about 5–10 ms, Seoul, E002). Do not run several queries in sequence within one request; reduce them with joins or a single transaction.
- **Scaling:** Increasing connections and concurrency increased throughput almost proportionally (6,680 TPS at 64 connections, 24,541 TPS at 256, with almost the same latency). If you need throughput, increase concurrency.
- **Freshness:** Committed data was immediately visible from any connection (E005). No routing that sends reads-after-writes to a specific node is needed.
- **Large aggregations:** They slow down in proportion to the number of rows processed and are billed in DPU. Queries that exceed the 300-second limit are rejected, and there is no server-side cancellation, so split the range when running them (E012).

## Operations and recovery

- **Create and delete:** Creating a cluster took 32 seconds and deleting it about 2 minutes (E011). There are no capacity, instance size, or vacuum settings.
- **Backup:** Backups are possible only through AWS Backup. You must first create a backup vault and a service role (`AWSBackupServiceRolePolicyForBackup`, `AWSBackupServiceRolePolicyForRestores`), and DSQL must be enabled in the account's AWS Backup settings. Every backup is a full backup; about 100 MB took 481 seconds (E007).
- **Restore:** `start-restore-job` always creates a new cluster. Deletion protection is on by default, so set `isDeletionProtectionEnabled` in the metadata `regionalConfig` if needed. The new cluster has a different endpoint, so the application configuration and IAM permissions must change. The restore took 129 seconds.
- **No point-in-time restore:** The latest recoverable point is the time of the last backup. Set the backup interval according to the acceptable data-loss window.
- **Automation caveat:** Looking up a backup vault that does not exist returned `AccessDeniedException`. Treat it as meaning the vault does not exist.
- **Observability:** Cost and usage were checked with `TotalDPU` in CloudWatch `AWS/AuroraDSQL`. It is a per-minute sum.

## Cost estimation

- **Pricing structure (Seoul, as published 2026-09-11):** $10 per million DPU, $0.40 per GB-month of storage, with 100,000 DPU and 1 GB-month free each month.
- **DPU per request:** For this order workload, one request used 0.029–0.032 DPU (about $0.31 per million requests). This varies greatly with query shape, so measure it with your real workload.
- **Quick calculation:** Cost per hour ≈ average TPS × 3,600 × DPU per request × $10 / 1,000,000. For this workload, the break-even point against RDS Multi-AZ (`db.r6g.xlarge`, about $1.22 per hour) was an average of about 1,100 TPS (E010).
- **Loading:** Loading about 5 GiB took about 520,000 DPU (about $5.2) (E002).

## Pre-adoption checklist

- [ ] Every write path has `40001` retries and a business-ID receipt.
- [ ] Requests whose commit response was not received are retried only after checking the receipt.
- [ ] No operation exceeds 3,000 rows or 300 seconds (including batches, migrations, and cleanup jobs).
- [ ] No code depends on specifying an isolation level, on `SELECT FOR UPDATE` waiting, or on `statement_timeout`.
- [ ] `serial`, PL/pgSQL, triggers, temporary tables, and partitions are not used.
- [ ] Indexes are created with `CREATE INDEX ASYNC` and then checked with `indisvalid`, and indexes on the foreign key side have been created.
- [ ] There is a connection pool, connection lifetime is shorter than 1 hour, tokens are cached, and concurrent signing is prevented.
- [ ] No place concentrates writes on a few rows, or a distributed design is in place.
- [ ] The backup interval and the recovery procedure of switching to a new cluster are documented.
- [ ] DPU per request has been measured with the real workload and compared with the cost of a fixed instance.
