"""E002 basic OLTP: four business operations, SQL, mix, op generation and receipt-based retry.

Retry logic follows E004 `load.run_op` (ambiguous commits resolved through the business ID), except that the
2 s deadline counts from the request's scheduled time, so time spent queueing uses up the same budget.
"""
from __future__ import annotations

import asyncio
import time
import zlib
from dataclasses import dataclass, field

import psycopg

import datagen as G
import retry
from schema import RUN_ID_BASE

MIX = (("product_read", 0.40), ("order_history", 0.30), ("order_create", 0.20), ("cancel", 0.10))
KINDS = tuple(k for k, _ in MIX)
WRITE_KINDS = {"order_create", "cancel"}
BEGIN = "BEGIN ISOLATION LEVEL REPEATABLE READ"
HISTORY_PAGE = 20

SEL_PRODUCT = ("SELECT p.id, p.name, p.price, i.qty FROM products p JOIN inventory i ON i.product_id = p.id "
               "WHERE p.id = %s")
SEL_HISTORY = ("SELECT o.id, o.status, o.total, o.created_at, count(*) AS lines, sum(it.qty) AS units "
               "FROM orders o JOIN order_items it ON it.order_id = o.id WHERE o.customer_id = %s "
               "GROUP BY o.id, o.status, o.total, o.created_at ORDER BY o.created_at DESC LIMIT %s")
SEL_RECEIPT = "SELECT ref_id FROM operation_receipts WHERE op_id = %s"
DEC_STOCK = "UPDATE inventory SET qty = qty - %s WHERE product_id = %s AND qty >= %s"
INC_STOCK = "UPDATE inventory SET qty = qty + %s WHERE product_id = %s"
INS_ORDER = "INSERT INTO orders (id, customer_id, status, total, created_at) VALUES (%s, %s, 'placed', %s, now())"
INS_ITEM = "INSERT INTO order_items (order_id, line_no, product_id, qty, price) VALUES (%s, %s, %s, %s, %s)"
INS_LEDGER = "INSERT INTO ledger (id, order_id, kind, amount, created_at) VALUES (%s, %s, %s, %s, now())"
INS_RECEIPT = "INSERT INTO operation_receipts (op_id, kind, ref_id, created_at) VALUES (%s, %s, %s, now())"
CANCEL = "UPDATE orders SET status = 'cancelled' WHERE id = %s AND status = 'placed'"
SEL_ORDER = "SELECT status, total FROM orders WHERE id = %s"
SEL_ITEMS = "SELECT product_id, qty FROM order_items WHERE order_id = %s"


@dataclass(frozen=True)
class Op:
    kind: str
    op_id: str
    key: int            # product id, customer id, or order id to cancel
    ref_id: int         # new order id (order_create) or new ledger id (cancel); 0 for reads
    items: tuple = ()   # ((line_no, product_id, qty), ...) for order_create


def ref_id(cell_id: str, proc: int, seq: int) -> int:
    """Unique per (cell, proc, seq) and at or above RUN_ID_BASE, so reset can find every run-created row."""
    return RUN_ID_BASE + (zlib.crc32(cell_id.encode()) << 30) + (proc << 22) + seq


def make_op(rng, sc: G.Scale, cell_id: str, proc: int, seq: int) -> Op:
    r, acc, kind = rng.random(), 0.0, KINDS[-1]
    for k, share in MIX:
        acc += share
        if r < acc:
            kind = k
            break
    op_id = f"{cell_id}:{proc}:{seq}"
    if kind == "product_read":
        return Op(kind, op_id, rng.randrange(sc.products), 0)
    if kind == "order_history":
        return Op(kind, op_id, rng.randrange(sc.customers), 0)
    if kind == "order_create":
        n = rng.randint(1, 3)
        items = tuple((ln, rng.randrange(sc.products), rng.randint(1, 2)) for ln in range(n))
        return Op(kind, op_id, rng.randrange(sc.customers), ref_id(cell_id, proc, seq), items)
    pool = G.CANCEL_POOL(sc)
    return Op(kind, op_id, rng.randrange(pool.start, pool.stop), ref_id(cell_id, proc, seq))


@dataclass
class Result:
    outcome: str                 # committed | rejected | failed
    attempts: int
    errors: list = field(default_factory=list)
    ambiguous: bool = False      # commit may have happened and could not be resolved
    resolved: bool = False       # an ambiguous commit was confirmed through the receipt
    reason: str | None = None


class ConnHolder:
    def __init__(self, connect):
        self.connect, self.conn, self.in_commit, self.reconnects = connect, None, False, 0

    async def open(self):
        self.conn = await self.connect()

    async def close(self):
        try:
            await self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    async def reopen(self):
        if self.conn is not None:
            await self.close()
        self.reconnects += 1
        self.conn = await self.connect()

    async def rollback_quiet(self):
        try:
            await self.conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - connection unusable: replace it
            try:
                await self.reopen()
            except Exception:  # noqa: BLE001 - DB refuses connections: the next attempt reports it
                self.conn = None


async def _commit(h):
    h.in_commit = True
    await h.conn.execute("COMMIT")
    h.in_commit = False


async def _read(h, sql, params):
    c = h.conn
    await c.execute(BEGIN)
    await (await c.execute(sql, params)).fetchall()
    await c.execute("COMMIT")
    return "committed"


async def _has_receipt(c, op) -> bool:
    return (await (await c.execute(SEL_RECEIPT, (op.op_id,))).fetchone()) is not None


async def order_create(h, op):
    c = h.conn
    await c.execute(BEGIN)
    if await _has_receipt(c, op):
        await c.execute("ROLLBACK")
        return "duplicate"
    for _, pid, qty in op.items:
        if (await c.execute(DEC_STOCK, (qty, pid, qty))).rowcount != 1:
            await c.execute("ROLLBACK")
            return "rejected"
    total = sum(G.price(pid) * qty for _, pid, qty in op.items)
    await c.execute(INS_ORDER, (op.ref_id, op.key, total))
    for ln, pid, qty in op.items:
        await c.execute(INS_ITEM, (op.ref_id, ln, pid, qty, G.price(pid)))
    await c.execute(INS_LEDGER, (op.ref_id, op.ref_id, "charge", total))
    await c.execute(INS_RECEIPT, (op.op_id, "order_create", op.ref_id))
    await _commit(h)
    return "committed"


async def cancel(h, op):
    c = h.conn
    await c.execute(BEGIN)
    if await _has_receipt(c, op):
        await c.execute("ROLLBACK")
        return "duplicate"
    row = await (await c.execute(SEL_ORDER, (op.key,))).fetchone()
    if row is None or row[0] != "placed" or (await c.execute(CANCEL, (op.key,))).rowcount != 1:
        await c.execute("ROLLBACK")
        return "rejected"
    for pid, qty in await (await c.execute(SEL_ITEMS, (op.key,))).fetchall():
        await c.execute(INC_STOCK, (qty, pid))
    await c.execute(INS_LEDGER, (op.ref_id, op.key, "refund", row[1]))
    await c.execute(INS_RECEIPT, (op.op_id, "cancel", op.key))
    await _commit(h)
    return "committed"


async def _txn(h, op):
    if op.kind == "product_read":
        return await _read(h, SEL_PRODUCT, (op.key,))
    if op.kind == "order_history":
        return await _read(h, SEL_HISTORY, (op.key, HISTORY_PAGE))
    return await (order_create if op.kind == "order_create" else cancel)(h, op)


async def _receipt_exists(h, op):
    """After an ambiguous commit: reconnect and look up the business ID. None = could not tell."""
    try:
        await h.reopen()
        return (await (await h.conn.execute(SEL_RECEIPT, (op.op_id,))).fetchone()) is not None
    except Exception:  # noqa: BLE001
        return None


async def run_op(h, op, policy, rng, t_sched: float, first_attempt: int = 1) -> Result:
    """Deadline counts from the scheduled time t_sched (monotonic). first_attempt > 1 marks a retry after a
    possibly-landed commit (used by tests to exercise the late-landing path)."""
    errors, attempt = [], first_attempt - 1
    after_ambiguous = first_attempt > 1
    while True:
        attempt += 1
        remaining = policy.deadline_s - (time.monotonic() - t_sched)
        if remaining <= 0:
            return Result("failed", attempt - 1, errors, reason="deadline")
        if h.conn is None:                               # an earlier reconnect failed
            try:
                await h.reopen()
            except Exception:  # noqa: BLE001 - overload shows up as a failed request, never a crashed cell
                errors.append("conn")
                return Result("failed", attempt, errors, reason="connection")
        try:
            out = await asyncio.wait_for(_txn(h, op), remaining)
        except asyncio.TimeoutError:
            errors.append("timeout")
            was_commit, h.in_commit = h.in_commit, False
            if op.kind in WRITE_KINDS:
                state = await _receipt_exists(h, op)       # also replaces the interrupted connection
            else:
                state = False
                try:
                    await h.reopen()
                except Exception:  # noqa: BLE001
                    pass
            if state:
                return Result("committed", attempt, errors, resolved=True)
            # no retry follows: a commit that was in flight stays ambiguous unless its receipt was seen
            return Result("failed", attempt, errors, ambiguous=was_commit and not state, reason="deadline")
        except psycopg.Error as exc:
            st = exc.sqlstate
            was_commit, h.in_commit = h.in_commit, False
            errors.append(st or "conn")
            if st is None or st.startswith("08"):
                if not (op.kind in WRITE_KINDS and retry.is_ambiguous(st, was_commit)):
                    try:
                        await h.reopen()
                    except Exception:  # noqa: BLE001
                        pass
                    return Result("failed", attempt, errors, reason="connection")
                state = await _receipt_exists(h, op)
                if state:
                    return Result("committed", attempt, errors, resolved=True)
                if state is None:
                    return Result("failed", attempt, errors, ambiguous=True, reason="unresolved")
                after_ambiguous, retry_as = True, "40001"   # not visible yet: retry like a conflict
            elif st == "23505" and after_ambiguous:       # the earlier COMMIT landed late and owns the receipt
                await h.rollback_quiet()
                return Result("committed", attempt, errors, resolved=True)
            else:
                await h.rollback_quiet()
                retry_as = st
            delay = policy.next_delay(attempt, retry_as, time.monotonic() - t_sched, rng)
            if delay is None:
                return Result("failed", attempt, errors, reason=retry.classify(st))
            await asyncio.sleep(delay)
            continue
        if out == "duplicate":            # op ids are unique: only our own earlier attempt owns the receipt
            out = "committed" if after_ambiguous else "failed"
        return Result(out, attempt, errors, reason="duplicate" if out == "failed" else None)
