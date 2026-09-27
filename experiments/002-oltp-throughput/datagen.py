"""Deterministic S-dataset rows. Every value is a function of (table, id), so any process can build any chunk.

Baseline stock is a separate large counter (baseline orders do not decrement it); invariants and reset only
look at the change during a run (base_qty - qty).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
INITIAL_STOCK = 10_000_000            # never exhausted within the experiment
CANCEL_POOL_FRACTION = 0.25           # first quarter of baseline orders may be cancelled


@dataclass(frozen=True)
class Scale:
    customers: int
    products: int
    orders_per_customer: int
    tenants: int = 100


S_SCALE = Scale(customers=1_000_000, products=200_000, orders_per_customer=11)  # about 5.1 GiB on PostgreSQL 16 (calibrate.py)


def scaled(fraction: float) -> Scale:
    return replace(S_SCALE, customers=max(100, int(S_SCALE.customers * fraction)),
                   products=max(100, int(S_SCALE.products * fraction)))


def _h(*parts) -> int:
    return int.from_bytes(hashlib.blake2b(repr(parts).encode(), digest_size=8).digest(), "big")


def price(pid: int) -> int:
    return 500 + _h("price", pid) % 99_500


def count(table: str, sc: Scale) -> int:
    orders = sc.customers * sc.orders_per_customer
    return {"tenants": sc.tenants, "customers": sc.customers, "products": sc.products,
            "inventory": sc.products, "orders": orders, "order_items": orders, "ledger": orders,
            "operation_receipts": 0}[table]


def items_of(order_id: int, sc: Scale) -> list[tuple[int, int, int]]:
    """(line_no, product_id, qty) for a baseline order: 1-4 lines, qty 1-3."""
    n = 1 + _h("n", order_id) % 4
    return [(ln, _h("p", order_id, ln) % sc.products, 1 + _h("q", order_id, ln) % 3) for ln in range(n)]


def _created(i: int) -> datetime:
    return EPOCH + timedelta(seconds=i * 7 % (240 * 86400))


def CANCEL_POOL(sc: Scale) -> range:  # noqa: N802 - constant-like accessor
    return range(0, int(count("orders", sc) * CANCEL_POOL_FRACTION))


def rows(table: str, start: int, stop: int, sc: Scale) -> list[tuple]:
    out = []
    for i in range(start, stop):
        if table == "tenants":
            out.append((i, f"tenant-{i}"))
        elif table == "customers":
            out.append((i, i % sc.tenants, f"c{i}@example.test", f"customer {i}", _created(i)))
        elif table == "products":
            out.append((i, i % sc.tenants, f"SKU-{i:08d}", f"product {i}",
                        f"description {_h('d', i):x} " * 6, price(i)))
        elif table == "inventory":
            out.append((i, INITIAL_STOCK, INITIAL_STOCK))
        elif table == "orders":
            total = sum(price(p) * q for _, p, q in items_of(i, sc))
            out.append((i, i % sc.customers, "placed", total, _created(i)))
        elif table == "order_items":
            out.extend((i, ln, p, q, price(p)) for ln, p, q in items_of(i, sc))
        elif table == "ledger":
            total = sum(price(p) * q for _, p, q in items_of(i, sc))
            out.append((i, i, "charge", total, _created(i)))
        else:
            raise ValueError(f"no generator for {table}")
    return out


def chunks(table: str, sc: Scale, size: int) -> list[tuple[str, int, int]]:
    """Id ranges of at most `size` rows (order_items: size/4 order ids, since an order has up to 4 lines)."""
    if size > 2500:
        raise ValueError("chunk size above 2500 risks the DSQL 3,000-row transaction limit")
    per = max(1, size // 4) if table == "order_items" else size
    n = count(table, sc)
    return [(table, a, min(a + per, n)) for a in range(0, n, per)]


COLUMNS = {
    "tenants": "id, name", "customers": "id, tenant_id, email, name, created_at",
    "products": "id, tenant_id, sku, name, description, price", "inventory": "product_id, qty, base_qty",
    "orders": "id, customer_id, status, total, created_at",
    "order_items": "order_id, line_no, product_id, qty, price",
    "ledger": "id, order_id, kind, amount, created_at",
}


def insert_chunk(conn, table: str, start: int, stop: int, sc: Scale, method: str) -> int:
    """Insert one chunk in its own transaction. method: 'copy' (PostgreSQL) or 'insert' (DSQL)."""
    data = rows(table, start, stop, sc)
    if not data:
        return 0
    cols = COLUMNS[table]
    with conn.transaction():
        if method == "copy":
            with conn.cursor().copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
                for r in data:
                    cp.write_row(r)
        else:
            ph = "(" + ", ".join(["%s"] * len(data[0])) + ")"
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES " + ", ".join([ph] * len(data)),
                         [v for r in data for v in r])
    return len(data)
