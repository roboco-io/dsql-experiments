"""Open-loop (fixed Poisson arrival rate) and closed-loop (fixed concurrency) engines for one E002 cell.

Open loop: each process owns rate/P arrivals and POOL_SIZE/P connections. A producer enqueues requests at their
scheduled times; workers take them in order. Latency is scheduled time -> outcome, so queueing is included and
coordinated omission is avoided. A request not started within SKIP_AFTER_S of its scheduled time is skipped
(a technical failure). Closed loop (exploration only): each worker issues the next request when the last ends.
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from multiprocessing import get_context

import datagen as G
import hist
import retry
import workload as W

POOL_SIZE = 256
SKIP_AFTER_S = 2.0
STAGGER_S = 0.02
POLICY = retry.RetryPolicy(3)


@dataclass
class Cell:
    cell_id: str
    config: str
    mode: str                 # "open" | "closed"
    rate: float               # arrivals/s (open)
    concurrency: int          # workers (closed)
    rep: int
    warmup_s: int
    measure_s: int
    scale_fraction: float = 1.0


def poisson_schedule(rate: float, duration_s: float, rng) -> list[float]:
    out, t = [], 0.0
    if rate <= 0:
        return out
    while True:
        t += rng.expovariate(rate)
        if t >= duration_s:
            return out
        out.append(t)


def split_rate(rate: float, processes: int) -> list[float]:
    return [rate / processes] * processes


def _kind():
    return {"committed": 0, "rejected": 0, "failed": 0, "skipped": 0, "attempts": 0, "errors": {},
            "latency": hist.LogHistogram(), "queue": hist.LogHistogram()}


class Stats:
    def __init__(self):
        self.kinds = {k: _kind() for k in W.KINDS}
        self.lag = hist.LogHistogram()
        self.ledger = {"order_create": {"committed": 0, "ambiguous": 0}, "cancel": {"committed": 0, "ambiguous": 0}}

    def record(self, kind, res, latency_ms, queue_ms, in_measure):
        """The ledger counts every write of the cell (invariants compare against the whole DB); metrics only
        count requests scheduled inside the measure window."""
        if kind in self.ledger:
            if res.outcome == "committed":
                self.ledger[kind]["committed"] += 1
            if res.ambiguous:
                self.ledger[kind]["ambiguous"] += 1
        if not in_measure:
            return
        k = self.kinds[kind]
        k[res.outcome] += 1
        k["attempts"] += res.attempts
        for e in res.errors:
            c = retry.classify(e)
            k["errors"][c] = k["errors"].get(c, 0) + 1
        k["latency"].record(latency_ms)
        k["queue"].record(queue_ms)

    def record_skipped(self, kind, in_measure):
        if in_measure:
            self.kinds[kind]["skipped"] += 1

    def record_lag(self, ms):
        self.lag.record(ms)

    def merge(self, other):
        for n, o in other.kinds.items():
            k = self.kinds[n]
            for f in ("committed", "rejected", "failed", "skipped", "attempts"):
                k[f] += o[f]
            for e, c in o["errors"].items():
                k["errors"][e] = k["errors"].get(e, 0) + c
            k["latency"].merge(o["latency"])
            k["queue"].merge(o["queue"])
        self.lag.merge(other.lag)
        for kind, led in other.ledger.items():
            for f, n in led.items():
                self.ledger[kind][f] += n

    def to_dict(self):
        return {"kinds": {n: {**{f: v for f, v in k.items() if f not in ("latency", "queue")},
                              "latency": k["latency"].to_dict(), "queue": k["queue"].to_dict()}
                          for n, k in self.kinds.items()},
                "lag": self.lag.to_dict(), "ledger": self.ledger}

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for n, k in d["kinds"].items():
            s.kinds[n] = {**k, "errors": dict(k["errors"]), "latency": hist.LogHistogram.from_dict(k["latency"]),
                          "queue": hist.LogHistogram.from_dict(k["queue"])}
        s.lag = hist.LogHistogram.from_dict(d["lag"])
        s.ledger = {kind: dict(v) for kind, v in d["ledger"].items()}
        return s


def _p99(h):
    return h.percentile(99) if h.count else None


def metrics(stats: dict, measure_s: float) -> dict:
    per, tot_ops, committed, attempts, technical = {}, 0, 0, 0, 0
    for n, k in stats["kinds"].items():
        lat = hist.LogHistogram.from_dict(k["latency"])
        ops = k["committed"] + k["rejected"] + k["failed"] + k["skipped"]
        per[n] = {"ops": ops, "committed": k["committed"], "rejected": k["rejected"], "failed": k["failed"],
                  "skipped": k["skipped"], "errors": k["errors"], "success_tps": round(k["committed"] / measure_s, 3),
                  "p50_ms": lat.percentile(50), "p95_ms": lat.percentile(95), "p99_ms": lat.percentile(99),
                  "min_ms": lat.min, "max_ms": lat.max,
                  "queue_p99_ms": _p99(hist.LogHistogram.from_dict(k["queue"]))}
        tot_ops += ops
        committed += k["committed"]
        attempts += k["attempts"]
        technical += k["failed"] + k["skipped"]
    return {"per_kind": per, "ops": tot_ops, "success_tps": round(committed / measure_s, 3),
            "attempt_tps": round(attempts / measure_s, 3),
            "technical_failure_rate": technical / tot_ops if tot_ops else None,
            "lag_p99_ms": _p99(hist.LogHistogram.from_dict(stats["lag"]))}


async def _sleep_until_mono(t):
    d = t - time.monotonic()
    if d > 0:
        await asyncio.sleep(d)


async def _open_proc(cell, proc, rate, conns, connect, t0_mono, sc, stats):
    rng = random.Random(f"{cell.cell_id}:{proc}")
    sched = poisson_schedule(rate, cell.warmup_s + cell.measure_s, random.Random(f"sched:{cell.cell_id}:{proc}"))
    q: asyncio.Queue = asyncio.Queue()
    holders = []
    for _ in range(conns):
        h = W.ConnHolder(connect)
        await h.open()
        holders.append(h)
        await asyncio.sleep(STAGGER_S)

    async def producer():
        for seq, off in enumerate(sched):
            t = t0_mono + off
            await _sleep_until_mono(t)
            if off >= cell.warmup_s:                  # startup jitter must not invalidate the cell
                stats.record_lag((time.monotonic() - t) * 1000)
            q.put_nowait((seq, t, off >= cell.warmup_s))
        for _ in holders:
            q.put_nowait(None)

    async def worker(i, h):
        wrng = random.Random(f"{cell.cell_id}:{proc}:w{i}")
        while True:
            item = await q.get()
            if item is None:
                return
            seq, t_sched, in_measure = item
            op = W.make_op(rng, sc, cell.cell_id, proc, seq)
            waited = time.monotonic() - t_sched
            if waited >= SKIP_AFTER_S:
                stats.record_skipped(op.kind, in_measure)
                continue
            res = await W.run_op(h, op, POLICY, wrng, t_sched)
            stats.record(op.kind, res, (time.monotonic() - t_sched) * 1000, waited * 1000, in_measure)

    await asyncio.gather(producer(), *(worker(i, h) for i, h in enumerate(holders)))
    for h in holders:
        await h.close()


async def _closed_proc(cell, wids, connect, t0_mono, sc, stats):
    t_measure, t_end = t0_mono + cell.warmup_s, t0_mono + cell.warmup_s + cell.measure_s

    async def worker(wid):
        rng = random.Random(f"{cell.cell_id}:{wid}")
        h = W.ConnHolder(connect)
        await h.open()
        await _sleep_until_mono(t0_mono)
        seq = 0
        while time.monotonic() < t_end:
            op = W.make_op(rng, sc, cell.cell_id, wid, seq)
            seq += 1
            t = time.monotonic()
            res = await W.run_op(h, op, POLICY, rng, t)
            stats.record(op.kind, res, (time.monotonic() - t) * 1000, 0.0, t_measure <= t < t_end)
        await h.close()
    tasks = []
    for wid in wids:
        tasks.append(asyncio.create_task(worker(wid)))
        await asyncio.sleep(STAGGER_S)
    await asyncio.gather(*tasks)


def _proc_entry(args):
    target, cell_d, proc, share, t_start_wall = args
    import conn as C
    cell = Cell(**cell_d)
    sc = G.S_SCALE if cell.scale_fraction >= 1.0 else G.scaled(cell.scale_fraction)
    connect = C.async_connect_factory(target)
    stats = Stats()
    t0_mono = time.monotonic() + (t_start_wall - time.time())
    if cell.mode == "open":
        rate, conns = share
        asyncio.run(_open_proc(cell, proc, rate, conns, connect, t0_mono, sc, stats))
    else:
        asyncio.run(_closed_proc(cell, share, connect, t0_mono, sc, stats))
    return stats.to_dict()


def run_cell(target, cell, t_start, sc=None, processes=None) -> Stats:
    """t_start (wall clock) must leave room for staggered connection opens: see runner.start_delay_s."""
    width = POOL_SIZE if cell.mode == "open" else cell.concurrency
    processes = max(1, min(processes or os.cpu_count() or 1, width))
    if cell.mode == "open":
        conns = [POOL_SIZE // processes + (1 if i < POOL_SIZE % processes else 0) for i in range(processes)]
        shares = list(zip(split_rate(cell.rate, processes), conns))
    else:
        shares = [list(range(i, cell.concurrency, processes)) for i in range(processes)]
    args = [(target, asdict(cell), i, shares[i], t_start) for i in range(processes)]
    with ProcessPoolExecutor(processes, mp_context=get_context("spawn")) as ex:
        parts = list(ex.map(_proc_entry, args))
    total = Stats()
    for p in parts:
        total.merge(Stats.from_dict(p))
    return total
