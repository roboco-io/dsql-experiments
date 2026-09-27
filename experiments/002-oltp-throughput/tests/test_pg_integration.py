"""Integration tests against PostgreSQL 16. Run:
docker run -d --rm --name e002-pg -e POSTGRES_PASSWORD=e002 -p 55433:5432 postgres:16
E002_PG_DSN=postgresql://postgres:e002@localhost:55433/postgres python3 -m unittest tests.test_pg_integration -v
"""
import asyncio
import os
import random
import time
import sys
import unittest

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conn as C  # noqa: E402
import datagen as G  # noqa: E402
import retry  # noqa: E402
import schema as SC  # noqa: E402
import workload as W  # noqa: E402

DSN = os.environ.get("E002_PG_DSN")
SMALL = G.scaled(0.001)


def fresh_load(conn, sc=SMALL):
    for s in SC.drop_sql() + SC.ddl("pg"):
        conn.execute(s)
    for t in SC.TABLES[:-1]:
        for _, a, b in G.chunks(t, sc, 2000):
            G.insert_chunk(conn, t, a, b, sc, "copy")


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class PostgresIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(DSN, autocommit=True)

    def tearDown(self):
        self.conn.close()

    def test_load_counts_match_generator(self):
        fresh_load(self.conn)
        for t in ("customers", "products", "orders", "ledger"):
            n = self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            self.assertEqual(n, G.count(t, SMALL), t)
        items = self.conn.execute("SELECT count(*) FROM order_items").fetchone()[0]
        self.assertEqual(items, sum(len(G.items_of(o, SMALL)) for o in range(G.count("orders", SMALL))))

    def test_insert_method_matches_copy(self):
        for s in SC.drop_sql() + SC.ddl("pg"):
            self.conn.execute(s)
        G.insert_chunk(self.conn, "tenants", 0, 100, SMALL, "insert")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0], 100)


def _run(op, target, first_attempt=1):
    async def go():
        h = W.ConnHolder(C.async_connect_factory(target))
        await h.open()
        try:
            return await W.run_op(h, op, retry.RetryPolicy(3), random.Random(0), time.monotonic(),
                                  first_attempt=first_attempt)
        finally:
            await h.close()
    return asyncio.run(go())


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class WorkloadIntegration(unittest.TestCase):
    target = {"kind": "dsn", "dsn": DSN or ""}

    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as c:
            fresh_load(c)

    def test_each_kind_commits(self):
        rng = random.Random(5)
        seen = {}
        for i in range(400):
            op = W.make_op(rng, SMALL, "it1", 0, i)
            seen.setdefault(op.kind, set()).add(_run(op, self.target).outcome)
        for kind in W.KINDS:
            self.assertIn("committed", seen[kind], kind)

    def test_cancel_retry_after_landed_commit_is_committed(self):
        rng = random.Random(6)
        op = next(o for o in (W.make_op(rng, SMALL, "it2", 0, i) for i in range(1000)) if o.kind == "cancel")
        self.assertEqual(_run(op, self.target).outcome, "committed")
        # same business ID again, as the retry after a lost COMMIT that actually landed
        self.assertEqual(_run(op, self.target, first_attempt=2).outcome, "committed")

    def test_order_create_retry_after_landed_commit_is_committed(self):
        rng = random.Random(7)
        op = next(o for o in (W.make_op(rng, SMALL, "it3", 0, i) for i in range(1000)) if o.kind == "order_create")
        self.assertEqual(_run(op, self.target).outcome, "committed")
        self.assertEqual(_run(op, self.target, first_attempt=2).outcome, "committed")
        with psycopg.connect(DSN, autocommit=True) as c:
            n = c.execute("SELECT count(*) FROM orders WHERE id = %s", (op.ref_id,)).fetchone()[0]
        self.assertEqual(n, 1)
