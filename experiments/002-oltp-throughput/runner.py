#!/usr/bin/env python3
"""E002 runner CLI (runs on the load-generator EC2, or locally against a DSN target).

Commands write one JSON result to --out: probe, schema, load (resumable), cell (run + invariants + reset).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from multiprocessing import get_context

import conn as C
import datagen as G
import invariants as INV
import openloop as OL
import schema as SC

LOCK_SQL = "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
SATURATION_PCT = 85.0


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, sort_keys=True, default=str)
    os.replace(tmp, path)


def _cpu():
    """(idle+iowait, total) jiffies from /proc/stat, or None off Linux."""
    try:
        with open("/proc/stat") as fh:
            vals = [int(x) for x in fh.readline().split()[1:]]
        return vals[3] + vals[4], sum(vals)
    except OSError:
        return None


def cpu_busy_pct(c0, c1):
    if not c0 or not c1 or c1[1] == c0[1]:
        return None
    return 100.0 * (1 - (c1[0] - c0[0]) / (c1[1] - c0[1]))


class Monitor(threading.Thread):
    """Samples generator CPU and, for PostgreSQL targets, lock waiters once per second in the measure window."""

    def __init__(self, connect, t_measure, t_end):
        super().__init__(daemon=True)
        self.connect, self.t_measure, self.t_end = connect, t_measure, t_end
        self.samples, self.cpu_pct, self.error = [], None, None

    def run(self):
        try:
            time.sleep(max(0.0, self.t_measure - time.time()))
            c0 = _cpu()
            conn = self.connect() if self.connect else None
            while time.time() < self.t_end:
                if conn:
                    self.samples.append(int(conn.execute(LOCK_SQL).fetchone()[0]))
                time.sleep(1)
            self.cpu_pct = cpu_busy_pct(c0, _cpu())
            if conn:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"[:200]

    def result(self):
        s = self.samples
        return {"generator_cpu_pct": self.cpu_pct, "lock_waiters_max": max(s) if s else None,
                "lock_waiters_mean": sum(s) / len(s) if s else None, "lock_samples": len(s), "error": self.error}


def _max_connections(conn):
    try:
        return int(conn.execute("SHOW max_connections").fetchone()[0])
    except Exception:  # noqa: BLE001 - DSQL may not expose it; its connection quota is checked separately
        return None


def _rtt_ms(conn, n=20):
    samples = []
    for _ in range(n):
        t0 = time.monotonic()
        conn.execute("SELECT 1").fetchone()
        samples.append((time.monotonic() - t0) * 1000)
    return sorted(samples)[n // 2]


def cmd_probe(target):
    c = C.sync_connect_factory(target)()
    out = {"max_connections": _max_connections(c)}
    for key, sql in (("server_version", "SHOW server_version"),
                     ("default_isolation", "SHOW default_transaction_isolation")):
        try:
            out[key] = c.execute(sql).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            out[key] = f"unavailable:{getattr(exc, 'sqlstate', None)}"
    out["rtt_ms"] = _rtt_ms(c)
    c.close()
    return out


LOAD_CHUNK = 2000
INDEX_WAIT_S = 1800
INDEX_NAME = "orders_customer_created"


def start_delay_s(cell, processes) -> float:
    width = OL.POOL_SIZE if cell.mode == "open" else cell.concurrency
    return width / max(1, min(processes, width)) * OL.STAGGER_S + 10


def load_method(target) -> str:
    return "insert" if target["kind"] == "dsql" else "copy"


def pending_chunks(chunks, done: set) -> list:
    return [c for c in chunks if f"{c[0]}:{c[1]}:{c[2]}" not in done]


def cmd_schema(target):
    """Drop and create the schema. DSQL builds the index asynchronously; wait until pg_indexes shows it
    (the check E001 used for CREATE INDEX ASYNC)."""
    c = C.sync_connect_factory(target)()
    kind = "dsql" if target["kind"] == "dsql" else "pg"
    for s in SC.drop_sql() + SC.ddl(kind):
        c.execute(s)
    t0 = time.monotonic()
    while not c.execute("SELECT count(*) FROM pg_indexes WHERE indexname = %s", (INDEX_NAME,)).fetchone()[0]:
        if time.monotonic() - t0 > INDEX_WAIT_S:
            raise RuntimeError("index not visible after CREATE INDEX ASYNC")
        time.sleep(5)
    c.close()
    return {"kind": kind, "tables": list(SC.TABLES), "index_wait_s": round(time.monotonic() - t0, 1)}


def _retryable(exc) -> bool:
    code = getattr(exc, "sqlstate", None)
    return code is None or code == "40001" or str(code).startswith("08")


def _load_worker(args):
    target, method, fraction, chunk_list = args
    sc = G.S_SCALE if fraction >= 1.0 else G.scaled(fraction)
    connect = C.sync_connect_factory(target)
    c, done = connect(), []
    for table, a, b in chunk_list:
        for attempt in range(5):
            try:
                G.insert_chunk(c, table, a, b, sc, method)
                break
            except Exception as exc:  # noqa: BLE001 - retry conflicts/connection loss, re-raise the rest
                if attempt == 4 or not _retryable(exc):
                    raise
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5 * (attempt + 1))
                c = connect()
        done.append(f"{table}:{a}:{b}")
    c.close()
    return done


def cmd_load(target, fraction, tables, workers, progress_path):
    """Parents before children, one table at a time. Finished chunks are recorded so a rerun resumes.
    A chunk that committed but was not recorded (crash between the two) fails with 23505 on rerun: the
    operator then restarts from `schema`."""
    sc = G.S_SCALE if fraction >= 1.0 else G.scaled(fraction)
    done = set(json.load(open(progress_path))) if os.path.exists(progress_path) else set()
    method, t0, rows = load_method(target), time.monotonic(), {}
    for table in tables:
        todo = pending_chunks(G.chunks(table, sc, LOAD_CHUNK), done)
        groups = [g for g in (todo[i::workers] for i in range(workers)) if g]
        if groups:
            with ProcessPoolExecutor(len(groups), mp_context=get_context("spawn")) as ex:
                for part in ex.map(_load_worker, [(target, method, fraction, g) for g in groups]):
                    done.update(part)
                    _write(progress_path, sorted(done))
        rows[table] = G.count(table, sc)
    return {"rows": rows, "seconds": round(time.monotonic() - t0, 1), "method": method, "fraction": fraction,
            "analyze": analyze(target)}


def analyze(target) -> dict:
    """Same preparation on every service: fresh planner statistics after the bulk load (errors recorded, e.g.
    if a service rejects ANALYZE)."""
    c, out = C.sync_connect_factory(target)(), {}
    for t in SC.TABLES:
        try:
            c.execute(f"ANALYZE {t}")
            out[t] = "ok"
        except Exception as exc:  # noqa: BLE001
            out[t] = f"error:{getattr(exc, 'sqlstate', None) or type(exc).__name__}"
    c.close()
    return out


def cmd_cell(target, cell_json, out):
    cell = OL.Cell(**json.loads(cell_json))
    sc = G.S_SCALE if cell.scale_fraction >= 1.0 else G.scaled(cell.scale_fraction)
    result = {"cell": asdict(cell), "status": "error", "started": _now()}
    try:
        admin = C.sync_connect_factory(target)()
        result["rtt_ms"] = _rtt_ms(admin)
        processes = os.cpu_count() or 1
        t_start = time.time() + start_delay_s(cell, processes)
        t_measure = t_start + cell.warmup_s
        # PostgreSQL targets: sample lock waiters too (DSQL has no lock waits to sample: N/A)
        mon_connect = None if target["kind"] == "dsql" else C.sync_connect_factory(target)
        mon = Monitor(mon_connect, t_measure, t_measure + cell.measure_s)
        mon.start()
        stats = OL.run_cell(target, cell, t_start, sc, processes)
        mon.join()
        m = OL.metrics(stats.to_dict(), cell.measure_s)
        mr = mon.result()
        result.update(metrics=m, stats=stats.to_dict(), monitor=mr,
                      window=[datetime.fromtimestamp(t_measure, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              _now()],
                      generator={"cpu_pct": mr["generator_cpu_pct"], "lag_p99_ms": m["lag_p99_ms"]})
        result["invariants"] = INV.check(INV.collect(admin), stats.ledger)
        result["reset"] = INV.reset(admin)
        admin.close()
        result["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the orchestrator decides; never leak the endpoint
        result["error"] = C.redact(f"{type(exc).__name__}: {exc}", target)[:500]
    result["finished"] = _now()
    _write(out, result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("probe", "schema", "load", "cell"))
    p.add_argument("--target", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--cell-json")
    p.add_argument("--fraction", type=float, default=1.0)
    p.add_argument("--tables", default=",".join(SC.TABLES[:-1]))
    p.add_argument("--workers", type=int, default=32)
    a = p.parse_args(argv)
    with open(a.target) as fh:
        target = json.load(fh)
    if a.command == "probe":
        res = cmd_probe(target)
    elif a.command == "schema":
        res = cmd_schema(target)
    elif a.command == "load":
        res = cmd_load(target, a.fraction, a.tables.split(","), a.workers, a.out + ".progress")
    else:
        res = cmd_cell(target, a.cell_json, a.out)
    if a.command != "cell":
        _write(a.out, res)
    print(json.dumps({"command": a.command, "status": res.get("status", "ok")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
