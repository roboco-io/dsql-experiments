"""E002 schema: the plan's eight tables; client-generated ids (DSQL has no sequences, E001)."""
from __future__ import annotations

TABLES = ("tenants", "customers", "products", "inventory", "orders", "order_items", "ledger",
          "operation_receipts")
RUN_ID_BASE = 10 ** 15          # rows created during measurement use ids at or above this

_TABLE_DDL = (
    "CREATE TABLE tenants (id bigint PRIMARY KEY, name text NOT NULL)",
    "CREATE TABLE customers (id bigint PRIMARY KEY, tenant_id bigint NOT NULL REFERENCES tenants(id), "
    "email text NOT NULL UNIQUE, name text NOT NULL, created_at timestamptz NOT NULL)",
    "CREATE TABLE products (id bigint PRIMARY KEY, tenant_id bigint NOT NULL REFERENCES tenants(id), "
    "sku text NOT NULL UNIQUE, name text NOT NULL, description text NOT NULL, "
    "price bigint NOT NULL CHECK (price > 0))",
    "CREATE TABLE inventory (product_id bigint PRIMARY KEY REFERENCES products(id), "
    "qty bigint NOT NULL CHECK (qty >= 0), base_qty bigint NOT NULL)",
    "CREATE TABLE orders (id bigint PRIMARY KEY, customer_id bigint NOT NULL REFERENCES customers(id), "
    "status text NOT NULL CHECK (status IN ('placed', 'cancelled')), total bigint NOT NULL, "
    "created_at timestamptz NOT NULL)",
    "CREATE TABLE order_items (order_id bigint NOT NULL REFERENCES orders(id), line_no int NOT NULL, "
    "product_id bigint NOT NULL REFERENCES products(id), qty int NOT NULL CHECK (qty > 0), "
    "price bigint NOT NULL, PRIMARY KEY (order_id, line_no))",
    "CREATE TABLE ledger (id bigint PRIMARY KEY, order_id bigint NOT NULL REFERENCES orders(id), "
    "kind text NOT NULL CHECK (kind IN ('charge', 'refund')), amount bigint NOT NULL, "
    "created_at timestamptz NOT NULL)",
    "CREATE TABLE operation_receipts (op_id text PRIMARY KEY, kind text NOT NULL, ref_id bigint NOT NULL, "
    "created_at timestamptz NOT NULL)",
)
_INDEX = "INDEX orders_customer_created ON orders (customer_id, created_at DESC)"


def ddl(kind: str) -> list[str]:
    """Tables then the customer-order index; DSQL builds secondary indexes with CREATE INDEX ASYNC."""
    if kind not in ("pg", "dsql"):
        raise ValueError(kind)
    index = f"CREATE {_INDEX.replace('INDEX', 'INDEX ASYNC', 1)}" if kind == "dsql" else f"CREATE {_INDEX}"
    return list(_TABLE_DDL) + [index]


def drop_sql() -> list[str]:
    return [f"DROP TABLE IF EXISTS {t}" for t in reversed(TABLES)]
