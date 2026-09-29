"""E002 per-cell reset and invariants.

Everything is scoped to rows the measurement created (id >= RUN_ID_BASE) or recorded in operation_receipts
(which holds only this cell's writes), so no statement scans the 5 GiB baseline. Statements that change rows
work in batches below the DSQL 3,000-row transaction limit.
"""
from __future__ import annotations

import zlib

from schema import RUN_ID_BASE

B = RUN_ID_BASE
QUERIES = {
    "order_receipts": "SELECT count(*) FROM operation_receipts WHERE kind = 'order_create'",
    "cancel_receipts": "SELECT count(*) FROM operation_receipts WHERE kind = 'cancel'",
    "run_orders": f"SELECT count(*) FROM orders WHERE id >= {B}",
    "orders_without_receipt": f"SELECT count(*) FROM orders o WHERE o.id >= {B} AND NOT EXISTS "
                              "(SELECT 1 FROM operation_receipts r WHERE r.kind = 'order_create' AND r.ref_id = o.id)",
    "orders_without_items": f"SELECT count(*) FROM orders o WHERE o.id >= {B} AND NOT EXISTS "
                            "(SELECT 1 FROM order_items i WHERE i.order_id = o.id)",
    "charge_rows": f"SELECT count(*) FROM ledger WHERE id >= {B} AND kind = 'charge'",
    "refund_rows": f"SELECT count(*) FROM ledger WHERE id >= {B} AND kind = 'refund'",
    "charge_total_mismatch": "SELECT count(*) FROM ledger l JOIN orders o ON o.id = l.order_id "
                             f"WHERE l.id >= {B} AND l.kind = 'charge' AND l.amount <> o.total",
    "refund_total_mismatch": "SELECT count(*) FROM ledger l JOIN orders o ON o.id = l.order_id "
                             f"WHERE l.id >= {B} AND l.kind = 'refund' "
                             "AND (l.amount <> o.total OR o.status <> 'cancelled')",
    "cancelled_without_receipt": "SELECT count(*) FROM operation_receipts r JOIN orders o ON o.id = r.ref_id "
                                 "WHERE r.kind = 'cancel' AND o.status <> 'cancelled'",
}
# Stock: for every product touched by a receipt, base_qty - qty must equal items ordered minus items returned.
STOCK_SQL = """
WITH moved AS (
  SELECT i.product_id, sum(CASE WHEN r.kind = 'order_create' THEN i.qty ELSE -i.qty END) AS delta
  FROM operation_receipts r JOIN order_items i ON i.order_id = r.ref_id GROUP BY i.product_id)
SELECT count(*) FILTER (WHERE inv.qty < 0), count(*) FILTER (WHERE inv.base_qty - inv.qty <> m.delta)
FROM moved m JOIN inventory inv ON inv.product_id = m.product_id
"""
_ZERO = ("neg_stock", "stock_delta_mismatch", "orders_without_receipt", "orders_without_items",
         "charge_total_mismatch", "refund_total_mismatch", "cancelled_without_receipt")


def collect(conn) -> dict:
    facts = {k: conn.execute(q).fetchone()[0] for k, q in QUERIES.items()}
    facts["neg_stock"], facts["stock_delta_mismatch"] = conn.execute(STOCK_SQL).fetchone()
    return facts


# Re-measure mode (2026-09-29): cells keep their rows (no reset) and each cell is checked on its own rows only.
# workload.ref_id gives a cell the id block [B + crc << 30, B + (crc + 1) << 30); its op_ids start with "<cell>:".
_R = "op_id LIKE %(op_like)s"             # a text range would depend on the collation (PG default ignores ':')
_CELL_QUERIES = {
    "order_receipts": f"SELECT count(*) FROM operation_receipts WHERE {_R} AND kind = 'order_create'",
    "cancel_receipts": f"SELECT count(*) FROM operation_receipts WHERE {_R} AND kind = 'cancel'",
    "run_orders": "SELECT count(*) FROM orders WHERE id >= %(lo)s AND id < %(hi)s",
    "orders_without_receipt": "SELECT count(*) FROM orders o WHERE o.id >= %(lo)s AND o.id < %(hi)s AND NOT EXISTS "
                              f"(SELECT 1 FROM operation_receipts r WHERE r.{_R} AND r.kind = 'order_create' "
                              "AND r.ref_id = o.id)",
    "orders_without_items": "SELECT count(*) FROM orders o WHERE o.id >= %(lo)s AND o.id < %(hi)s AND NOT EXISTS "
                            "(SELECT 1 FROM order_items i WHERE i.order_id = o.id)",
    "charge_rows": "SELECT count(*) FROM ledger WHERE id >= %(lo)s AND id < %(hi)s AND kind = 'charge'",
    "refund_rows": "SELECT count(*) FROM ledger WHERE id >= %(lo)s AND id < %(hi)s AND kind = 'refund'",
    "charge_total_mismatch": "SELECT count(*) FROM ledger l JOIN orders o ON o.id = l.order_id "
                             "WHERE l.id >= %(lo)s AND l.id < %(hi)s AND l.kind = 'charge' AND l.amount <> o.total",
    "refund_total_mismatch": "SELECT count(*) FROM ledger l JOIN orders o ON o.id = l.order_id "
                             "WHERE l.id >= %(lo)s AND l.id < %(hi)s AND l.kind = 'refund' "
                             "AND (l.amount <> o.total OR o.status <> 'cancelled')",
    "cancelled_without_receipt": "SELECT count(*) FROM operation_receipts r JOIN orders o ON o.id = r.ref_id "
                                 f"WHERE r.{_R} AND r.kind = 'cancel' AND o.status <> 'cancelled'",
}
_CELL_MOVED = ("SELECT i.product_id, sum(CASE WHEN r.kind = 'order_create' THEN i.qty ELSE -i.qty END) "
               f"FROM operation_receipts r JOIN order_items i ON i.order_id = r.ref_id WHERE r.{_R} "
               "GROUP BY i.product_id")


def cell_scope(cell_id: str) -> tuple[int, int, str, str]:
    """(id_lo, id_hi, op_lo, op_hi) of the rows one cell creates (see workload.ref_id and make_op)."""
    lo = B + (zlib.crc32(cell_id.encode()) << 30)
    return lo, lo + (1 << 30), f"{cell_id}:", f"{cell_id};"      # ';' sorts right after ':'


def inventory(conn) -> dict:
    return dict(conn.execute("SELECT product_id, qty FROM inventory").fetchall())


def collect_cell(conn, cell_id: str, inv_before: dict) -> dict:
    """Facts of one cell from its own rows; stock from the inventory change across the cell."""
    lo, hi, op_lo, op_hi = cell_scope(cell_id)
    esc = op_lo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    p = {"lo": lo, "hi": hi, "op_like": esc + "%"}
    facts = {k: conn.execute(q, p).fetchone()[0] for k, q in _CELL_QUERIES.items()}
    moved = {pid: d for pid, d in conn.execute(_CELL_MOVED, p).fetchall()}
    after = inventory(conn)
    facts["neg_stock"] = sum(1 for q in after.values() if q < 0)
    facts["stock_delta_mismatch"] = sum(1 for pid in set(after) | set(moved)
                                        if inv_before.get(pid, 0) - after.get(pid, 0) != moved.get(pid, 0))
    return facts


def check(facts: dict, ledger: dict) -> dict:
    """ledger: client-side committed/ambiguous counts per write kind. Receipts must lie in
    [committed, committed + ambiguous]; every effect must match its receipt count."""
    v = [k for k in _ZERO if facts.get(k)]
    for kind, rk in (("order_create", "order_receipts"), ("cancel", "cancel_receipts")):
        lo = ledger[kind]["committed"]
        hi = lo + ledger[kind]["ambiguous"]
        if facts[rk] < lo:
            v.append(f"lost_commit:{kind}")
        if facts[rk] > hi:
            v.append(f"unexpected_effect:{kind}")
    if facts["run_orders"] != facts["order_receipts"] or facts["charge_rows"] != facts["order_receipts"]:
        v.append("order_effect_count")
    if facts["refund_rows"] != facts["cancel_receipts"]:
        v.append("refund_effect_count")
    return {"violations": v, "facts": facts}


def _batched(conn, select_ids: str, apply: str, batch: int) -> int:
    """Select the ids once, then apply in batches: re-running the select per batch (a receipts join) made a
    D1 reset outlive the cell timeout (2026-09-28). Nothing else writes while a reset runs."""
    ids = [r[0] for r in conn.execute(select_ids).fetchall()]
    for i in range(0, len(ids), batch):
        with conn.transaction():
            conn.execute(apply, (ids[i:i + batch],))
    return len(ids)


def reset(conn, batch: int = 2000) -> dict:
    """Undo one cell. Stock and cancelled orders are restored through the receipts before anything is deleted."""
    per = max(1, batch // 4)          # up to 4 item rows per order id
    out = {}
    out["stock"] = _batched(conn, "SELECT DISTINCT v.product_id FROM operation_receipts r "
                            "JOIN order_items i ON i.order_id = r.ref_id JOIN inventory v ON v.product_id = i.product_id "
                            "WHERE v.qty <> v.base_qty",
                            "UPDATE inventory SET qty = base_qty WHERE product_id = ANY(%s)", batch)
    out["uncancel"] = _batched(conn, "SELECT r.ref_id FROM operation_receipts r JOIN orders o ON o.id = r.ref_id "
                               f"WHERE r.kind = 'cancel' AND r.ref_id < {B} AND o.status = 'cancelled'",
                               "UPDATE orders SET status = 'placed' WHERE id = ANY(%s)", batch)
    out["ledger"] = _batched(conn, f"SELECT id FROM ledger WHERE id >= {B}",
                             "DELETE FROM ledger WHERE id = ANY(%s)", batch)
    out["items"] = _batched(conn, f"SELECT DISTINCT order_id FROM order_items WHERE order_id >= {B}",
                            "DELETE FROM order_items WHERE order_id = ANY(%s)", per)
    out["orders"] = _batched(conn, f"SELECT id FROM orders WHERE id >= {B}",
                             "DELETE FROM orders WHERE id = ANY(%s)", batch)
    out["receipts"] = _batched(conn, "SELECT op_id FROM operation_receipts",
                               "DELETE FROM operation_receipts WHERE op_id = ANY(%s)", batch)
    return out
