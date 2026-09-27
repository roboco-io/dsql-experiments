"""Offline tests: no AWS or database access. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import random
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cost  # noqa: E402
import datagen as G  # noqa: E402
import safety as S  # noqa: E402
import schema as SC  # noqa: E402

ACCT = "123456789012"
PFX = "e002-20260928t010203z-ab12"


def _manifest(tmp, lifetime=600):
    return S.Manifest.create(os.path.join(tmp, "m.json"), ACCT, S.REGION, PFX, lifetime, "arn:aws:iam::x:user/op")


class SafetyScopes(unittest.TestCase):
    def test_prefix_is_e002(self):
        self.assertTrue(S.new_run_prefix().startswith("e002-"))
        with self.assertRaises(S.SafetyError):
            S.validate_run_prefix("e004-20260928t010203z-ab12")

    def test_each_config_owns_a_runner_and_batch_does_not(self):
        for cfg in S.CONFIGS:
            self.assertIn("ec2_instance", S.SCOPE_TYPES[cfg])
        self.assertNotIn("ec2_instance", S.SCOPE_TYPES[S.BATCH])

    def test_configs_may_be_alive_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.data["config_status"][S.BATCH] = "ready"
            m.add_resource("D1", "dsql_cluster", "c1", state="created")
            S.check_can_provision(m.data, "A2")      # no "one config at a time" error

    def test_config_requires_ready_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "R1")

    def test_batch_cleanup_waits_for_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource("A1", "ec2_instance", "i-1", state="created")
            with self.assertRaises(S.SafetyError):
                S.cleanup_plan(m.data, S.BATCH)

    def test_manifest_add_resource_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)

            def add(cfg):
                for i in range(50):
                    m.add_resource(cfg, "ec2_instance", f"i-{cfg}-{i}", state="created")
            ts = [threading.Thread(target=add, args=(c,)) for c in S.CONFIGS]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(S.Manifest.load(m.path).data["resources"]), 200)


class Cost(unittest.TestCase):
    prices = {"gb_month": 0.1, "iops_month": 0.02, "mibps_month": 0.04}

    def test_gp3_small_volume_pays_above_3000_iops_and_125_mibps(self):
        want = (100 * 0.1 + 3000 * 0.02 + 75 * 0.04) * 2 / 730
        self.assertAlmostEqual(cost.gp3_usd_per_h(100, 6000, 200, self.prices, 2), want)

    def test_gp3_400gib_includes_12000_iops_and_500_mibps(self):
        # RDS PostgreSQL gp3 at 400 GiB or more: 12,000 IOPS and 500 MiB/s are the included baseline
        self.assertAlmostEqual(cost.gp3_usd_per_h(400, 12000, 500, self.prices, 2), 400 * 0.1 * 2 / 730)

    def test_spent_includes_measured_costs(self):
        data = {"resources": [], "measured_usd": {"D1_dpu": 1.5, "A1_io": 0.25}}
        self.assertAlmostEqual(cost.spent_usd(data), 1.75)

    def test_guard_counts_inflight_reservations(self):
        data = {"resources": [], "measured_usd": {"D1_dpu": 40.0}}
        g = cost.Guard(lambda: data, cap=45.0)
        self.assertTrue(g.reserve("D1", 3.0)["ok"])
        second = g.reserve("A2", 3.0)            # 40 + 3 in flight + 3 > 45
        self.assertFalse(second["ok"])
        g.release("D1")
        self.assertTrue(g.reserve("A2", 3.0)["ok"])

    def test_guard_adds_cleanup_reserve_from_active_rates(self):
        data = {"resources": [{"state": "created", "extra": {"rate_usd_per_h": 10.0},
                               "recorded_at": S.iso(S.utcnow())}], "measured_usd": {}}
        out = cost.Guard(lambda: data, cap=45.0).reserve("R1", 0.0)
        self.assertAlmostEqual(out["reserve_usd"], 10.0 * cost.CLEANUP_HOURS)

    def test_new_manifest_has_measured_usd(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_manifest(tmp).data["measured_usd"], {})


class Data(unittest.TestCase):
    def test_rows_are_deterministic_per_id(self):
        sc = G.scaled(0.001)
        self.assertEqual(G.rows("orders", 10, 20, sc), G.rows("orders", 10, 20, sc))
        self.assertEqual(G.rows("orders", 10, 20, sc)[5], G.rows("orders", 15, 16, sc)[0])

    def test_chunks_cover_every_id_once(self):
        sc = G.scaled(0.001)
        for t in SC.TABLES:
            ids = [i for _, a, b in G.chunks(t, sc, 700) for i in range(a, b)]
            self.assertEqual(ids, list(range(G.count(t, sc))), t)

    def test_chunk_size_limit(self):
        with self.assertRaises(ValueError):
            G.chunks("orders", G.scaled(0.001), 2600)

    def test_items_have_one_to_four_lines_of_existing_products(self):
        sc = G.scaled(0.001)
        for oid in range(200):
            items = G.items_of(oid, sc)
            self.assertTrue(1 <= len(items) <= 4)
            self.assertTrue(all(0 <= p < sc.products and 1 <= q <= 3 for _, p, q in items))

    def test_order_items_rows_match_items_of(self):
        sc = G.scaled(0.001)
        rows = G.rows("order_items", 0, 3, sc)      # ids here are order ids; each yields its lines
        want = [(oid, ln, p, q, G.price(p)) for oid in range(3) for ln, p, q in G.items_of(oid, sc)]
        self.assertEqual(rows, want)

    def test_dsql_ddl_uses_async_index(self):
        self.assertTrue(any("CREATE INDEX ASYNC" in s for s in SC.ddl("dsql")))
        self.assertFalse(any("ASYNC" in s for s in SC.ddl("pg")))

    def test_cancel_pool_is_baseline_orders(self):
        sc = G.scaled(0.001)
        pool = G.CANCEL_POOL(sc)
        self.assertLess(pool.stop, SC.RUN_ID_BASE)
        self.assertLessEqual(pool.stop, G.count("orders", sc))
