"""MVP probes for E003 (connections), E008 (operations under load) and E012 (large reads), run on a runner
against an already loaded E002 target (2026-09-29 decision: finish the remaining experiments as MVPs).

Each probe returns a JSON-able dict. OLTP cells go through runner.cmd_cell in no-reset mode, so the same
metrics, SLO inputs and per-cell invariants as E002 apply. Nothing here prints endpoints or tokens.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import asdict

import conn as C
import openloop as OL
import runner as R


def pct(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    s = sorted(samples)

    def q(p):
        return round(s[min(len(s) - 1, int(p * len(s)))], 2)
    return {"n": len(s), "p50": q(0.50), "p95": q(0.95), "p99": q(0.99), "max": round(s[-1], 2)}


def _err(exc) -> str:
    return f"{type(exc).__name__}:{getattr(exc, 'sqlstate', None) or ''}"


def _count(errors: list[str]) -> dict:
    out: dict[str, int] = {}
    for e in errors:
        out[e] = out.get(e, 0) + 1
    return out


# ---------------------------------------------------------------- E003: connections
def _timed_connects(target, n):
    connect, lat, errors = C.sync_connect_factory(target), [], []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            c = connect()
            c.execute("SELECT 1").fetchone()
            lat.append((time.monotonic() - t0) * 1000)
            c.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))
    return {"connect_ms": pct(lat), "errors": _count(errors)}


async def _storm(target, n):
    """n connection attempts at once (10x a 100-connection pool's normal churn, compressed into one burst)."""
    connect = C.async_connect_factory(target)
    lat, errors, conns = [], [], []

    async def one():
        t0 = time.monotonic()
        try:
            c = await connect()
            await (await c.execute("SELECT 1")).fetchone()
            lat.append((time.monotonic() - t0) * 1000)
            conns.append(c)
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))
    t0 = time.monotonic()
    await asyncio.gather(*(one() for _ in range(n)))
    wall = time.monotonic() - t0
    for c in conns:
        await c.close()
    return {"attempts": n, "ok": len(lat), "wall_s": round(wall, 2), "connect_ms": pct(lat),
            "errors": _count(errors)}


async def _read_loop(target, workers, seconds, per_request):
    """product reads at fixed concurrency: a persistent connection per worker, or a new one per request."""
    connect = C.async_connect_factory(target)
    lat, errors, stop = [], [], time.monotonic() + seconds

    async def worker(i):
        c = None if per_request else await connect()
        k = i
        while time.monotonic() < stop:
            k = (k * 7919 + 1) % 200_000
            t0 = time.monotonic()
            try:
                cc = await connect() if per_request else c
                await (await cc.execute("SELECT name, price FROM products WHERE id = %s", (k,))).fetchone()
                if per_request:
                    await cc.close()
                lat.append((time.monotonic() - t0) * 1000)
            except Exception as exc:  # noqa: BLE001
                errors.append(_err(exc))
                if not per_request:
                    c = await connect()
        if c is not None:
            await c.close()
    await asyncio.gather(*(worker(i) for i in range(workers)))
    return {"workers": workers, "seconds": seconds, "per_request": per_request,
            "tps": round(len(lat) / seconds, 1), "latency_ms": pct(lat), "errors": _count(errors)}


def e003(target, storm=(500, 1000), read_s=30, **_) -> dict:
    out = {"kind": target["kind"]}
    if target["kind"] == "dsql":
        lat = []
        for _ in range(50):
            t0 = time.monotonic()
            C._sign(target)
            lat.append((time.monotonic() - t0) * 1000)
        out["token_sign_ms"] = pct(lat)
    out["sequential_connect"] = _timed_connects(target, 100)
    out["read_persistent"] = asyncio.run(_read_loop(target, 16, read_s, False))
    out["read_new_connection_per_request"] = asyncio.run(_read_loop(target, 16, read_s, True))
    for n in storm:
        out[f"storm_{n}"] = asyncio.run(_storm(target, n))
    out["after_storm_connect"] = _timed_connects(target, 20)
    return out


# ---------------------------------------------------------------- shared: an OLTP cell with side work
def _cell_with(target, cell, out_dir, side=None, side_delay_s=0.0) -> dict:
    """Run one no-reset OLTP cell; `side` (a function returning a dict) starts side_delay_s after the start."""
    box: dict = {}

    def run_side():
        time.sleep(side_delay_s)
        t0 = time.time()
        try:
            box["side"] = side()
        except Exception as exc:  # noqa: BLE001
            box["side"] = {"error": _err(exc)}
        box["side_window"] = [t0, time.time()]
    th = threading.Thread(target=run_side, daemon=True) if side else None
    if th:
        th.start()
    res = R.cmd_cell(target, json.dumps(asdict(cell)), f"{out_dir}/{cell.cell_id}.json", no_reset=True)
    if th:
        th.join(timeout=900)
    return {"cell": {k: res.get(k) for k in ("status", "error", "metrics", "invariants", "window", "generator")},
            **box}


def _index_sql(target, name, table, cols):
    return (f"CREATE INDEX ASYNC {name} ON {table} ({cols})" if target["kind"] == "dsql"
            else f"CREATE INDEX CONCURRENTLY {name} ON {table} ({cols})")


def _wait_valid(c, name, limit_s=1800):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        row = c.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
                        "WHERE x.relname = %s", (name,)).fetchone()
        if row and row[0]:
            return round(time.monotonic() - t0, 1)
        time.sleep(5)
    return None


def _step(c, sql):
    t0 = time.monotonic()
    try:
        c.execute(sql)
        return {"sql": sql.split(" ON ")[0][:80], "ok": True, "s": round(time.monotonic() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"sql": sql.split(" ON ")[0][:80], "ok": False, "error": _err(exc),
                "message": str(exc).splitlines()[0][:160], "s": round(time.monotonic() - t0, 2)}


def _ddl_under_load(target):
    c = C.sync_connect_factory(target)()
    steps = [_step(c, _index_sql(target, "orders_status_mvp", "orders", "status"))]
    steps[-1]["valid_after_s"] = _wait_valid(c, "orders_status_mvp") if steps[-1]["ok"] else None
    steps.append(_step(c, "ALTER TABLE customers ADD COLUMN mvp_note text"))
    steps.append(_step(c, "ALTER TABLE customers DROP COLUMN mvp_note"))
    steps.append(_step(c, "DROP INDEX orders_status_mvp"))
    c.close()
    return {"steps": steps}


def _limits(target, long_txn_s=310):
    """Transaction row and age limits (DSQL documents 3,000 rows and 5 minutes)."""
    c = C.sync_connect_factory(target)()
    out = {}
    try:
        with c.transaction():
            c.execute("UPDATE products SET description = description WHERE id < 5000")
        out["update_5000_rows"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        out["update_5000_rows"] = {"ok": False, "error": _err(exc), "message": str(exc).splitlines()[0][:160]}
    t0 = time.monotonic()
    try:
        with c.transaction():
            c.execute("SELECT count(*) FROM tenants").fetchone()
            time.sleep(long_txn_s)
            c.execute("SELECT count(*) FROM tenants").fetchone()
        out["long_transaction"] = {"ok": True, "hold_s": long_txn_s, "s": round(time.monotonic() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        out["long_transaction"] = {"ok": False, "hold_s": long_txn_s, "error": _err(exc), "message": str(exc).splitlines()[0][:160],
                                   "s": round(time.monotonic() - t0, 1)}
    c.close()
    return out


def e008(target, out_dir, fraction=1.0, rate=1000.0, warmup_s=60, measure_s=300, long_txn_s=310, **_) -> dict:
    cfg = target.get("config", "X")
    base = OL.Cell("E008-base", cfg, "open", rate, 0, 1, warmup_s, measure_s, fraction)
    ddl = OL.Cell("E008-ddl", cfg, "open", rate, 0, 1, warmup_s, measure_s, fraction)
    return {"baseline": _cell_with(target, base, out_dir),
            "with_ddl": _cell_with(target, ddl, out_dir, lambda: _ddl_under_load(target), side_delay_s=warmup_s),
            "limits": _limits(target, long_txn_s)}


# ---------------------------------------------------------------- E012: large reads
QUERIES = {
    "customer_recent_orders": ("SELECT o.id, o.created_at, i.product_id, i.qty FROM orders o "
                               "JOIN order_items i ON i.order_id = o.id WHERE o.customer_id = %s "
                               "ORDER BY o.created_at DESC LIMIT 20"),
    "revenue_by_day_10pct": ("SELECT date_trunc('day', created_at) d, count(*), sum(total) FROM orders "
                             "WHERE id < 1100000 GROUP BY 1 ORDER BY 1"),
    "product_rank_1pct": ("SELECT product_id, s, rank() OVER (ORDER BY s DESC) FROM (SELECT product_id, "
                          "sum(qty) s FROM order_items WHERE order_id < 110000 GROUP BY product_id) t "
                          "ORDER BY s DESC LIMIT 10"),
    "status_counts_full": "SELECT status, count(*), sum(total) FROM orders GROUP BY status ORDER BY status",
}


def _run_query(c, name, params=None):
    t0, w0 = time.monotonic(), time.time()
    try:
        rows = c.execute(QUERIES[name], params).fetchall()
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1), "rows": len(rows),
                "hash": hash(json.dumps(rows, default=str)) & 0xFFFFFFFF, "window": [w0, time.time()]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _err(exc), "message": str(exc).splitlines()[0][:160],
                "ms": round((time.monotonic() - t0) * 1000, 1), "window": [w0, time.time()]}


def _repeat(target, name, seconds):
    c = C.sync_connect_factory(target)()
    runs, stop = [], time.monotonic() + seconds
    while time.monotonic() < stop:
        runs.append(_run_query(c, name))
    c.close()
    ok = [r["ms"] for r in runs if r["ok"]]
    return {"query": name, "runs": len(runs), "ok": len(ok), "latency_ms": pct(ok),
            "errors": _count([r["error"] for r in runs if not r["ok"]])}


def e012(target, out_dir, fraction=1.0, rate=1000.0, warmup_s=60, measure_s=300, quiet_s=90, **_) -> dict:
    c = C.sync_connect_factory(target)()
    out = {"single": {}}
    lat = [_run_query(c, "customer_recent_orders", ((k * 104729) % 1_000_000,)) for k in range(1, 31)]
    out["single"]["customer_recent_orders"] = {"latency_ms": pct([r["ms"] for r in lat if r["ok"]]),
                                               "errors": _count([r["error"] for r in lat if not r["ok"]])}
    for name in ("revenue_by_day_10pct", "product_rank_1pct", "status_counts_full"):
        time.sleep(quiet_s)                   # a quiet minute on each side so CloudWatch DPU can be attributed
        out["single"][name] = [_run_query(c, name) for _ in range(2)]
    c.close()
    time.sleep(quiet_s)
    cell = OL.Cell("E012-oltp-with-agg", target.get("config", "X"), "open", rate, 0, 1, warmup_s, measure_s,
                   fraction)
    out["interference"] = _cell_with(target, cell, out_dir, lambda: _repeat(target, "revenue_by_day_10pct", measure_s),
                                     side_delay_s=warmup_s)
    return out


# ---------------------------------------------------------------- E005: read-after-write visibility
def reader_target(target) -> dict:
    """Aurora's reader endpoint differs from the cluster endpoint only by '.cluster-ro-'."""
    t = dict(target)
    if t.get("kind") != "dsql" and ".cluster-" in t.get("host", ""):
        t["host"] = t["host"].replace(".cluster-", ".cluster-ro-", 1)
    return t


def _visibility(write_c, read_c, table, n, poll_ms=10, limit_s=5.0):
    delays, stale, censored = [], 0, 0
    for i in range(n):
        write_c.execute(f"INSERT INTO {table} (id, v) VALUES (%s, %s)", (i, i))     # autocommit: acked = committed
        t0 = time.monotonic()
        first = True
        while True:
            if read_c.execute(f"SELECT 1 FROM {table} WHERE id = %s", (i,)).fetchone():
                delays.append((time.monotonic() - t0) * 1000)
                break
            if first:
                stale += 1
            first = False
            if time.monotonic() - t0 > limit_s:
                censored += 1
                break
            time.sleep(poll_ms / 1000)
    return {"trials": n, "stale_first_read": stale, "censored_over_5s": censored, "visible_after_ms": pct(delays)}


def e005(target, trials=200, **_) -> dict:
    table = "mvp_visibility"
    w = C.sync_connect_factory(target)()
    w.execute(f"DROP TABLE IF EXISTS {table}")
    w.execute(f"CREATE TABLE {table} (id bigint PRIMARY KEY, v int)")
    out = {"kind": target["kind"]}
    try:
        same = C.sync_connect_factory(target)()
        out["other_connection_same_endpoint"] = _visibility(w, same, table, trials)
        same.close()
        rt = reader_target(target)
        if rt.get("host") != target.get("host"):
            w.execute(f"DELETE FROM {table}")
            rd = C.sync_connect_factory(rt)()
            out["reader_endpoint"] = _visibility(w, rd, table, trials)
            out["reader_is_replica"] = rd.execute("SELECT pg_is_in_recovery()").fetchone()[0]
            rd.close()
    finally:
        w.execute(f"DROP TABLE IF EXISTS {table}")
        w.close()
    return out


# ---------------------------------------------------------------- E006: client-side connection loss
def _db_ips(target) -> list[str]:
    import socket
    if target.get("kind") == "dsn":
        return []
    return sorted({a[4][0] for a in socket.getaddrinfo(target["host"], 5432, proto=socket.IPPROTO_TCP)})


def _blackhole(ips, add):
    import subprocess
    for ip in ips:
        subprocess.run(["ip", "route", "add" if add else "del", "blackhole", f"{ip}/32"], check=add)


def _first_success_after(target, limit_s=300):
    connect, t0 = C.sync_connect_factory(target), time.monotonic()
    attempts = 0
    while time.monotonic() - t0 < limit_s:
        attempts += 1
        try:
            c = connect()
            c.execute("SELECT 1").fetchone()
            c.close()
            return {"s": round(time.monotonic() - t0, 2), "attempts": attempts}
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return {"s": None, "attempts": attempts}


def e006(target, out_dir, fraction=1.0, rate=300.0, warmup_s=30, measure_s=150, block_at_s=40, block_s=30,
         **_) -> dict:
    """Drop the runner's packets to the DB for block_s during an OLTP cell (a client/network fault, not a
    database failover), then reconcile commits through the receipts (per-cell invariants)."""
    ips = _db_ips(target)

    def side():
        _blackhole(ips, True)
        t_block = time.time()
        time.sleep(block_s)
        _blackhole(ips, False)
        return {"blocked_ips": len(ips), "block_s": block_s, "block_started": t_block,
                "reconnect": _first_success_after(target)}
    cell = OL.Cell("E006-block", target.get("config", "X"), "open", rate, 0, 1, warmup_s, measure_s, fraction)
    try:
        return _cell_with(target, cell, out_dir, side if ips else None, side_delay_s=warmup_s + block_at_s)
    finally:
        try:
            _blackhole(ips, False)
        except Exception:  # noqa: BLE001 - already removed
            pass


# ---------------------------------------------------------------- E009: spike and first request after idle
def e009_spike(target, out_dir, fraction=1.0, base=1000.0, phases=((0.2, 120), (2.0, 60), (1.0, 120), (0.05, 60)),
               **_) -> dict:
    out = []
    for i, (f, sec) in enumerate(phases):
        cell = OL.Cell(f"E009-p{i}-{int(base * f)}", target.get("config", "X"), "open", base * f, 0, 1, 0, sec,
                       fraction)
        out.append({"rate": base * f, "seconds": sec, **_cell_with(target, cell, out_dir)})
    return {"phases": out}


def e009_idle(target, cycles=2, idle_s=900, **_) -> dict:
    """No connection at all for idle_s, then time the first connection, the first read and the next 20 reads."""
    out = []
    for _ in range(cycles):
        time.sleep(idle_s)
        t0 = time.monotonic()
        rec = {"idle_s": idle_s}
        try:
            c = C.sync_connect_factory(target)()
            rec["connect_ms"] = round((time.monotonic() - t0) * 1000, 1)
            t1 = time.monotonic()
            c.execute("SELECT name FROM products WHERE id = 1").fetchone()
            rec["first_read_ms"] = round((time.monotonic() - t1) * 1000, 1)
            lat = []
            for k in range(20):
                t2 = time.monotonic()
                c.execute("SELECT name FROM products WHERE id = %s", (k + 2,)).fetchone()
                lat.append((time.monotonic() - t2) * 1000)
            rec["next_reads_ms"] = pct(lat)
            c.close()
        except Exception as exc:  # noqa: BLE001
            rec["error"] = _err(exc)
            rec["failed_after_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out.append(rec)
    return {"cycles": out}


# ---------------------------------------------------------------- E007: markers, a mistake, and a restored copy
MARK = "mvp_marker"


def e007_mark(target, n=30, **_) -> dict:
    """One marker per second; returns the last good marker's commit time (server clock)."""
    c = C.sync_connect_factory(target)()
    c.execute(f"DROP TABLE IF EXISTS {MARK}")
    c.execute(f"CREATE TABLE {MARK} (id int PRIMARY KEY, v int NOT NULL, at timestamptz NOT NULL)")
    for i in range(n):
        c.execute(f"INSERT INTO {MARK} (id, v, at) VALUES (%s, %s, now())", (i, i))
        time.sleep(1)
    good = c.execute(f"SELECT max(at), count(*), sum(v) FROM {MARK}").fetchone()
    c.close()
    return {"markers": good[1], "sum_v": good[2], "last_good_at": good[0].isoformat()}


def e007_mistake(target, **_) -> dict:
    """The 'operator mistake': every marker overwritten, then one marker that must not survive a restore."""
    c = C.sync_connect_factory(target)()
    at = c.execute("SELECT now()").fetchone()[0]
    c.execute(f"UPDATE {MARK} SET v = -1")
    c.execute(f"INSERT INTO {MARK} (id, v, at) VALUES (100000, 100000, now())")
    c.close()
    return {"mistake_at": at.isoformat()}


def e007_verify(target, host=None, secret_arn=None, **_) -> dict:
    """Connect to the restored copy and check the markers: all good, none overwritten, no post-mistake row."""
    t = dict(target)
    if host:
        t["host"] = host
    if secret_arn:
        t["secret_arn"] = secret_arn
    t0 = time.monotonic()
    c = C.sync_connect_factory(t)()
    connect_ms = round((time.monotonic() - t0) * 1000, 1)
    row = c.execute(f"SELECT count(*), count(*) FILTER (WHERE v = -1), count(*) FILTER (WHERE id >= 100000), "
                    f"sum(v) FILTER (WHERE v >= 0) FROM {MARK}").fetchone()
    orders = c.execute("SELECT count(*) FROM orders").fetchone()[0]
    c.close()
    return {"connect_ms": connect_ms, "markers": row[0], "overwritten": row[1], "after_mistake": row[2],
            "sum_v": row[3], "orders": orders}


PROBES = {"e003": e003, "e005": e005, "e006": e006, "e008": e008, "e009-spike": e009_spike,
          "e009-idle": e009_idle, "e012": e012, "e007-mark": e007_mark, "e007-mistake": e007_mistake,
          "e007-verify": e007_verify}
