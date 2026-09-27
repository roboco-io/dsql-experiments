"""Integration tests against PostgreSQL 16. Run:
docker run -d --rm --name e002-pg -e POSTGRES_PASSWORD=e002 -p 55433:5432 postgres:16
E002_PG_DSN=postgresql://postgres:e002@localhost:55433/postgres python3 -m unittest tests.test_pg_integration -v
"""
import os
import sys
import unittest

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import datagen as G  # noqa: E402
import schema as SC  # noqa: E402

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
