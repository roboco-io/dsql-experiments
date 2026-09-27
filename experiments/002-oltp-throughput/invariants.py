"""E002 per-cell reset and invariants.

Everything is scoped to rows the measurement created (id >= RUN_ID_BASE) or recorded in operation_receipts
(which holds only this cell's writes), so no statement scans the 5 GiB baseline. Statements that change rows
work in batches below the DSQL 3,000-row transaction limit.
"""
from __future__ import annotations

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
    n = 0
    while True:
        ids = [r[0] for r in conn.execute(f"{select_ids} LIMIT {batch}").fetchall()]
        if not ids:
            return n
        with conn.transaction():
            conn.execute(apply, (ids,))
        n += len(ids)


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
