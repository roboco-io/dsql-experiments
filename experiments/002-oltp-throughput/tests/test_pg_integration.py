"""Integration tests against PostgreSQL 16. Run:
docker run -d --rm --name e002-pg -e POSTGRES_PASSWORD=e002 -p 55433:5432 postgres:16 -c max_connections=600
E002_PG_DSN=postgresql://postgres:e002@localhost:55433/postgres python3 -m unittest tests.test_pg_integration -v
"""
import asyncio
import os
import random
import time
import sys
import json
import tempfile
import unittest
from unittest import mock
from dataclasses import asdict

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conn as C  # noqa: E402
import datagen as G  # noqa: E402
import invariants as INV  # noqa: E402
import openloop as OL  # noqa: E402
import retry  # noqa: E402
import runner as R  # noqa: E402
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


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class EngineIntegration(unittest.TestCase):
    target = {"kind": "dsn", "dsn": DSN or ""}

    @classmethod
    def setUpClass(cls):
        with psycopg.connect(DSN, autocommit=True) as c:
            fresh_load(c)

    def test_open_loop_hits_rate(self):
        cell = OL.Cell("it-open", "LOCAL", "open", 200.0, 0, 1, 2, 5, 0.001)
        st = OL.run_cell(self.target, cell, time.time() + 8, SMALL, processes=2)
        m = OL.metrics(st.to_dict(), cell.measure_s)
        self.assertAlmostEqual(m["attempt_tps"], 200.0, delta=30)
        self.assertLess(m["technical_failure_rate"], 0.01)

    def test_closed_loop_runs(self):
        cell = OL.Cell("it-closed", "LOCAL", "closed", 0.0, 8, 1, 1, 3, 0.001)
        st = OL.run_cell(self.target, cell, time.time() + 3, SMALL, processes=2)
        self.assertGreater(OL.metrics(st.to_dict(), cell.measure_s)["success_tps"], 10)


def _baseline(c):
    return (c.execute("SELECT sum(qty) FROM inventory").fetchone()[0],
            c.execute("SELECT count(*) FROM orders WHERE status = 'cancelled'").fetchone()[0],
            c.execute("SELECT count(*) FROM orders").fetchone()[0],
            c.execute("SELECT count(*) FROM ledger").fetchone()[0])


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class ResetIntegration(unittest.TestCase):
    target = {"kind": "dsn", "dsn": DSN or ""}

    def test_cell_then_invariants_then_reset_restores_baseline(self):
        with psycopg.connect(DSN, autocommit=True) as c:
            fresh_load(c)
            before = _baseline(c)
        cell = OL.Cell("it-inv", "LOCAL", "open", 150.0, 0, 1, 1, 4, 0.001)
        st = OL.run_cell(self.target, cell, time.time() + 8, SMALL, processes=2)
        with psycopg.connect(DSN, autocommit=True) as c:
            self.assertGreater(st.ledger["cancel"]["committed"], 0)
            out = INV.check(INV.collect(c), st.ledger)
            self.assertEqual(out["violations"], [], out["facts"])
            INV.reset(c, batch=50)
            self.assertEqual(_baseline(c), before)
            self.assertEqual(c.execute("SELECT count(*) FROM operation_receipts").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM inventory WHERE qty <> base_qty").fetchone()[0], 0)


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class RunnerIntegration(unittest.TestCase):
    def test_schema_load_cell_via_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tgt = os.path.join(tmp, "t.json")
            json.dump({"kind": "dsn", "dsn": DSN}, open(tgt, "w"))
            self.assertEqual(R.main(["schema", "--target", tgt, "--out", f"{tmp}/s.json"]), 0)
            self.assertEqual(R.main(["load", "--target", tgt, "--out", f"{tmp}/l.json", "--fraction", "0.001",
                                     "--workers", "2"]), 0)
            loaded = json.load(open(f"{tmp}/l.json"))
            self.assertEqual(loaded["rows"]["orders"], G.count("orders", SMALL))
            # rerun resumes: every chunk is already done, nothing is inserted twice
            self.assertEqual(R.main(["load", "--target", tgt, "--out", f"{tmp}/l.json", "--fraction", "0.001",
                                     "--workers", "2"]), 0)
            with psycopg.connect(DSN, autocommit=True) as c:
                self.assertEqual(c.execute("SELECT count(*) FROM orders").fetchone()[0], G.count("orders", SMALL))
            cell = OL.Cell("it-cli", "LOCAL", "open", 100.0, 0, 1, 1, 3, 0.001)
            R.main(["cell", "--target", tgt, "--out", f"{tmp}/c.json", "--cell-json", json.dumps(asdict(cell))])
            out = json.load(open(f"{tmp}/c.json"))
            self.assertEqual(out["status"], "ok", out.get("error"))
            self.assertEqual(out["invariants"]["violations"], [])
            self.assertIsNotNone(out["generator"]["lag_p99_ms"])
            self.assertEqual(set(loaded["analyze"].values()), {"ok"})


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class Rehearsal(unittest.TestCase):
    def test_rehearsal_produces_qref_and_q(self):
        import rehearse
        out = rehearse.run(DSN, fraction=0.02, warmup_s=2, measure_s=5, slo_factor=20.0)
        self.assertIsNotNone(out["qref"]["qref"], out["verdicts"])
        self.assertIn(out["q"]["status"], ("confirmed", "provisional", "lower_bound"))


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class ReviewFixIntegration(unittest.TestCase):
    def _target(self, tmp):
        tgt = os.path.join(tmp, "t.json")
        json.dump({"kind": "dsn", "dsn": DSN}, open(tgt, "w"))
        return tgt

    def test_schema_checks_reset_and_invariant_sql_and_clears_load_progress(self):
        # I9: run the reset/invariant SQL on empty tables before any data is loaded; C1: stale progress removed
        with tempfile.TemporaryDirectory() as tmp:
            tgt = self._target(tmp)
            stale = f"{tmp}/results/load-x-1.0.json.progress"
            os.makedirs(os.path.dirname(stale))
            json.dump(["orders:0:10"], open(stale, "w"))
            self.assertEqual(R.main(["schema", "--target", tgt, "--out", f"{tmp}/results/s.json"]), 0)
            out = json.load(open(f"{tmp}/results/s.json"))
            self.assertEqual(out["sql_check"], "ok")
            self.assertFalse(os.path.exists(stale))

    def test_cell_resets_leftovers_from_an_earlier_failed_cell(self):
        # I2: a cell that died before its reset must not poison the next cell's invariants
        with tempfile.TemporaryDirectory() as tmp:
            tgt = self._target(tmp)
            R.main(["schema", "--target", tgt, "--out", f"{tmp}/s.json"])
            R.main(["load", "--target", tgt, "--out", f"{tmp}/l.json", "--fraction", "0.001", "--workers", "2"])
            with psycopg.connect(DSN, autocommit=True) as c:      # leftovers: a receipt and a changed stock
                c.execute("INSERT INTO operation_receipts (op_id, kind, ref_id, created_at) "
                          "VALUES ('dead:0:0', 'cancel', 0, now())")
                c.execute("UPDATE orders SET status = 'cancelled' WHERE id = 0")
            cell = OL.Cell("it-after-dead", "LOCAL", "open", 50.0, 0, 1, 1, 3, 0.001)
            R.main(["cell", "--target", tgt, "--out", f"{tmp}/c.json", "--cell-json", json.dumps(asdict(cell))])
            out = json.load(open(f"{tmp}/c.json"))
            self.assertEqual(out["status"], "ok", out.get("error"))
            self.assertEqual(out["invariants"]["violations"], [])
            self.assertGreaterEqual(out["pre_reset"]["receipts"], 1)


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class NoResetIntegration(unittest.TestCase):
    """Re-measure mode (2026-09-29): cells keep their rows; invariants look only at the cell's own rows."""

    def test_cells_accumulate_and_each_is_checked_on_its_own_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tgt = os.path.join(tmp, "t.json")
            json.dump({"kind": "dsn", "dsn": DSN}, open(tgt, "w"))
            R.main(["schema", "--target", tgt, "--out", f"{tmp}/s.json"])
            self.assertEqual(json.load(open(f"{tmp}/s.json"))["sql_check"], "ok")
            R.main(["load", "--target", tgt, "--out", f"{tmp}/l.json", "--fraction", "0.001", "--workers", "2"])
            outs = []
            for name in ("it-acc-1", "it-acc-2"):
                cell = OL.Cell(name, "LOCAL", "open", 120.0, 0, 1, 1, 3, 0.001)
                R.main(["cell", "--target", tgt, "--out", f"{tmp}/{name}.json", "--no-reset",
                        "--cell-json", json.dumps(asdict(cell))])
                outs.append(json.load(open(f"{tmp}/{name}.json")))
            for out in outs:
                self.assertEqual(out["status"], "ok", out.get("error"))
                self.assertEqual(out["reset_mode"], "none")
                self.assertNotIn("reset", out)
                self.assertEqual(out["invariants"]["violations"], [], out["invariants"]["facts"])
            with psycopg.connect(DSN, autocommit=True) as c:
                kept = c.execute("SELECT count(*) FROM operation_receipts").fetchone()[0]
            own = [o["invariants"]["facts"]["order_receipts"] + o["invariants"]["facts"]["cancel_receipts"]
                   for o in outs]
            self.assertGreater(min(own), 0)
            self.assertEqual(kept, sum(own))          # both cells' rows are kept, each counted once

    def test_a_bad_row_of_another_cell_does_not_fail_this_cell(self):
        with psycopg.connect(DSN, autocommit=True) as c:
            fresh_load(c)
            c.execute("INSERT INTO operation_receipts (op_id, kind, ref_id, created_at) "
                      "VALUES ('other:0:0', 'cancel', 0, now())")     # other cell's receipt, order 0 still placed
        inv0 = None
        with psycopg.connect(DSN, autocommit=True) as c:
            inv0 = INV.inventory(c)
        cell = OL.Cell("it-scope", "LOCAL", "open", 100.0, 0, 1, 1, 3, 0.001)
        st = OL.run_cell({"kind": "dsn", "dsn": DSN}, cell, time.time() + 6, SMALL, processes=2)
        with psycopg.connect(DSN, autocommit=True) as c:
            out = INV.check(INV.collect_cell(c, cell.cell_id, inv0), st.ledger)
        self.assertEqual(out["violations"], [], out["facts"])


@unittest.skipUnless(DSN, "set E002_PG_DSN to run")
class MvpProbeIntegration(unittest.TestCase):
    """E003/E008/E012 MVP probes (2026-09-29) run end to end through `runner.py mvp` with short waits."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.tgt = os.path.join(cls.tmp, "t.json")
        json.dump({"kind": "dsn", "dsn": DSN, "config": "LOCAL"}, open(cls.tgt, "w"))
        R.main(["schema", "--target", cls.tgt, "--out", f"{cls.tmp}/s.json"])
        R.main(["load", "--target", cls.tgt, "--out", f"{cls.tmp}/l.json", "--fraction", "0.001", "--workers", "2"])

    def _probe(self, name, **kw):
        out = f"{self.tmp}/{name}.json"
        R.main(["mvp", "--probe", name, "--target", self.tgt, "--out", out, "--fraction", "0.001",
                "--probe-json", json.dumps(kw)])
        return json.load(open(out))

    def test_e003_connections(self):
        out = self._probe("e003", storm=[50], read_s=2)
        self.assertEqual(out["sequential_connect"]["connect_ms"]["n"], 100)
        self.assertEqual(out["storm_50"]["ok"], 50)
        self.assertGreater(out["read_persistent"]["tps"], out["read_new_connection_per_request"]["tps"])

    def test_e008_ddl_under_load_and_limits(self):
        out = self._probe("e008", rate=50.0, warmup_s=1, measure_s=4, long_txn_s=1)
        self.assertEqual(out["with_ddl"]["cell"]["status"], "ok", out["with_ddl"]["cell"].get("error"))
        self.assertEqual(out["with_ddl"]["cell"]["invariants"]["violations"], [])
        self.assertTrue(all(s["ok"] for s in out["with_ddl"]["side"]["steps"]), out["with_ddl"]["side"])
        self.assertIsNotNone(out["with_ddl"]["side"]["steps"][0]["valid_after_s"])
        self.assertTrue(out["limits"]["update_5000_rows"]["ok"])      # PostgreSQL has no 3,000-row limit
        self.assertTrue(out["limits"]["long_transaction"]["ok"])

    def test_e012_queries_and_interference(self):
        out = self._probe("e012", rate=50.0, warmup_s=1, measure_s=4, quiet_s=0)
        for name in ("revenue_by_day_10pct", "product_rank_1pct", "status_counts_full"):
            self.assertTrue(all(r["ok"] for r in out["single"][name]), out["single"][name])
            self.assertEqual(out["single"][name][0]["hash"], out["single"][name][1]["hash"])
        self.assertEqual(out["interference"]["cell"]["status"], "ok", out["interference"]["cell"].get("error"))
        self.assertGreater(out["interference"]["side"]["runs"], 0)

    def test_e005_visibility_on_another_connection(self):
        out = self._probe("e005", trials=20)
        v = out["other_connection_same_endpoint"]
        self.assertEqual(v["trials"], 20)
        self.assertEqual(v["stale_first_read"], 0)       # a single PostgreSQL primary: visible at once
        self.assertNotIn("reader_endpoint", out)         # a plain DSN has no reader endpoint

    def test_e009_spike_phases_and_idle(self):
        out = self._probe("e009-spike", base=50.0, phases=[[0.5, 2], [2.0, 2]])
        self.assertEqual([p["cell"]["status"] for p in out["phases"]], ["ok", "ok"])
        idle = self._probe("e009-idle", cycles=1, idle_s=0)
        self.assertIn("first_read_ms", idle["cycles"][0])

    def test_e007_markers_mistake_and_verify(self):
        good = self._probe("e007-mark", n=3)
        self.assertEqual(good["markers"], 3)
        self._probe("e007-mistake")
        seen = self._probe("e007-verify")          # no restore locally: the source shows the mistake
        self.assertEqual((seen["markers"], seen["overwritten"], seen["after_mistake"]), (4, 3, 1))
