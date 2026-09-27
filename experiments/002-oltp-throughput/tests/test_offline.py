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
import infra as IN  # noqa: E402
import invariants as INV  # noqa: E402
import openloop as OL  # noqa: E402
import runner as R  # noqa: E402
import safety as S  # noqa: E402
import schema as SC  # noqa: E402
import slo  # noqa: E402
import workload as W  # noqa: E402

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


class Workload(unittest.TestCase):
    def test_mix_matches_plan(self):
        rng, sc = random.Random(1), G.scaled(0.001)
        n = 40000
        kinds = [W.make_op(rng, sc, "c", 0, i).kind for i in range(n)]
        for kind, share in W.MIX:
            self.assertAlmostEqual(kinds.count(kind) / n, share, delta=0.01, msg=kind)

    def test_op_ids_unique_and_run_ids_above_base(self):
        rng, sc = random.Random(2), G.scaled(0.001)
        ops = [W.make_op(rng, sc, "cell1", p, i) for p in range(3) for i in range(500)]
        self.assertEqual(len({o.op_id for o in ops}), len(ops))
        for o in ops:
            if o.kind in W.WRITE_KINDS:
                self.assertGreaterEqual(o.ref_id, SC.RUN_ID_BASE)

    def test_cancel_targets_come_from_the_pool(self):
        rng, sc = random.Random(3), G.scaled(0.001)
        pool = G.CANCEL_POOL(sc)
        for i in range(2000):
            o = W.make_op(rng, sc, "c", 0, i)
            if o.kind == "cancel":
                self.assertIn(o.key, pool)

    def test_ref_ids_do_not_collide_across_cells(self):
        self.assertNotEqual(W.ref_id("cell-a", 0, 1), W.ref_id("cell-b", 0, 1))

    def test_ref_id_fits_bigint(self):
        self.assertLess(W.ref_id("x" * 40, 255, 2 ** 22 - 1), 2 ** 63)


class OpenLoop(unittest.TestCase):
    def test_poisson_rate_and_bounds(self):
        s = OL.poisson_schedule(500.0, 60.0, random.Random(1))
        self.assertAlmostEqual(len(s) / 60.0, 500.0, delta=15)
        self.assertEqual(s, sorted(s))
        self.assertTrue(all(0 <= t < 60.0 for t in s))

    def test_poisson_gaps_look_exponential(self):
        s = OL.poisson_schedule(1000.0, 30.0, random.Random(2))
        gaps = [b - a for a, b in zip(s, s[1:])]
        mean = sum(gaps) / len(gaps)
        cv = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean
        self.assertAlmostEqual(cv, 1.0, delta=0.05)     # exponential: std == mean

    def test_zero_rate_schedules_nothing(self):
        self.assertEqual(OL.poisson_schedule(0.0, 10.0, random.Random(1)), [])

    def test_split_rate_sums(self):
        self.assertAlmostEqual(sum(OL.split_rate(1000.0, 16)), 1000.0)

    def test_skipped_counts_as_technical_failure_and_rejects_do_not(self):
        st = OL.Stats()
        for _ in range(997):
            st.record("product_read", W.Result("committed", 1), 5.0, 0.1, True)
        st.record("order_create", W.Result("rejected", 1), 5.0, 0.1, True)
        st.record_skipped("product_read", True)
        st.record("cancel", W.Result("failed", 3, reason="deadline"), 2000.0, 0.1, True)
        m = OL.metrics(st.to_dict(), 10.0)
        self.assertAlmostEqual(m["technical_failure_rate"], 2 / 1000)
        self.assertEqual(m["per_kind"]["product_read"]["skipped"], 1)

    def test_latencies_outside_measure_window_are_ignored(self):
        st = OL.Stats()
        st.record("product_read", W.Result("committed", 1), 999.0, 0.0, False)
        st.record("product_read", W.Result("committed", 1), 1.0, 0.0, True)
        self.assertLess(OL.metrics(st.to_dict(), 1.0)["per_kind"]["product_read"]["p99_ms"], 2)

    def test_ledger_counts_commits_outside_the_window_too(self):
        st = OL.Stats()
        st.record("order_create", W.Result("committed", 1), 1.0, 0.0, False)
        st.record("cancel", W.Result("failed", 1, ambiguous=True), 1.0, 0.0, True)
        self.assertEqual(st.ledger["order_create"]["committed"], 1)
        self.assertEqual(st.ledger["cancel"]["ambiguous"], 1)

    def test_stats_round_trip(self):
        st = OL.Stats()
        st.record("cancel", W.Result("committed", 2, ["40001"]), 12.0, 3.0, True)
        st.record_lag(4.0)
        again = OL.Stats.from_dict(json.loads(json.dumps(st.to_dict())))
        self.assertEqual(OL.metrics(again.to_dict(), 1.0), OL.metrics(st.to_dict(), 1.0))


def _res(p99=10.0, p95=5.0, fail=0.0, cpu=40.0, lag=1.0, viol=(), status="ok", rate=100.0, rep=1, tps=100.0):
    per = {k: {"p95_ms": p95, "p99_ms": p99, "success_tps": tps / 4} for k in slo.SLO}
    return {"status": status, "cell": {"rate": rate, "rep": rep, "concurrency": 64},
            "metrics": {"per_kind": per, "technical_failure_rate": fail, "success_tps": tps},
            "generator": {"cpu_pct": cpu, "lag_p99_ms": lag}, "invariants": {"violations": list(viol)}}


class Slo(unittest.TestCase):
    def test_pass_and_read_bound(self):
        self.assertEqual(slo.judge(_res())["verdict"], "pass")
        self.assertEqual(slo.judge(_res(p99=150.0))["verdict"], "fail")   # reads need p99 <= 100

    def test_write_bound_is_looser(self):
        r = _res()
        r["metrics"]["per_kind"]["order_create"]["p99_ms"] = 150.0
        self.assertEqual(slo.judge(r)["verdict"], "pass")

    def test_failure_rate_bound(self):
        self.assertEqual(slo.judge(_res(fail=0.002))["verdict"], "fail")

    def test_missing_kind_fails(self):
        r = _res()
        del r["metrics"]["per_kind"]["cancel"]
        self.assertEqual(slo.judge(r)["verdict"], "fail")

    def test_invalid_cell_is_not_a_slo_failure(self):
        self.assertEqual(slo.judge(_res(cpu=95.0, p99=500.0))["verdict"], "invalid")
        self.assertEqual(slo.judge(_res(lag=25.0))["verdict"], "invalid")
        self.assertEqual(slo.judge(_res(viol=["neg_stock"]))["verdict"], "invalid")
        self.assertEqual(slo.judge(_res(status="error"))["verdict"], "invalid")

    def test_qref_is_min_of_best_passing_tps(self):
        explore = {"D1": [_res(tps=900), _res(tps=2000)], "R1": [_res(tps=700), _res(tps=1500, p99=300)],
                   "A1": [_res(tps=800)], "A2": [_res(tps=1200)]}
        out = slo.qref(explore)
        self.assertEqual(out["qref"], 700)
        self.assertEqual(out["limiting"], "R1")

    def test_qref_none_when_a_config_never_passes(self):
        self.assertIsNone(slo.qref({"D1": [_res(p99=300)], "R1": [_res()]})["qref"])

    def test_next_rate_steps_then_bisects_once_then_stops(self):
        self.assertEqual(slo.next_rate([60, 100, 120], [], 100, False), 150)
        self.assertEqual(slo.next_rate([60, 100, 120, 150], [187.5], 100, False), 168.75)
        self.assertIsNone(slo.next_rate([60, 100, 120, 150, 168.75], [187.5], 100, False))
        self.assertIsNone(slo.next_rate([60, 100, 120, 150], [168.75, 187.5], 100, False))
        self.assertIsNone(slo.next_rate([60, 100, 120], [], 100, True))

    def test_select_q_requires_every_rep(self):
        rs = [_res(rate=100, rep=r) for r in (1, 2, 3)] + [_res(rate=120, rep=1), _res(rate=120, rep=2, p99=300)]
        out = slo.select_q(rs, 3)
        self.assertEqual((out["q"], out["status"]), (100, "confirmed"))

    def test_select_q_provisional_with_fewer_reps(self):
        self.assertEqual(slo.select_q([_res(rate=100, rep=1)], 3)["status"], "provisional")

    def test_select_q_lower_bound_when_no_rate_failed(self):
        rs = [_res(rate=r, rep=1) for r in (50, 100, 120)]
        self.assertEqual(slo.select_q(rs, 1, stopped_early=True)["status"], "lower_bound")

    def test_select_q_ignores_invalid_cells(self):
        rs = [_res(rate=100, rep=1), _res(rate=150, rep=1, cpu=99.0, p99=900)]
        self.assertEqual(slo.select_q(rs, 1)["q"], 100)


class Invariants(unittest.TestCase):
    ledger = {"order_create": {"committed": 10, "ambiguous": 0}, "cancel": {"committed": 4, "ambiguous": 0}}

    def _facts(self, **kw):
        base = {"neg_stock": 0, "stock_delta_mismatch": 0, "order_receipts": 10, "cancel_receipts": 4,
                "run_orders": 10, "orders_without_receipt": 0, "orders_without_items": 0,
                "charge_rows": 10, "refund_rows": 4, "charge_total_mismatch": 0, "refund_total_mismatch": 0,
                "cancelled_without_receipt": 0}
        base.update(kw)
        return base

    def test_clean_run_has_no_violations(self):
        self.assertEqual(INV.check(self._facts(), self.ledger)["violations"], [])

    def test_receipts_must_cover_client_commits(self):
        ledger = {"order_create": {"committed": 11, "ambiguous": 0}, "cancel": {"committed": 4, "ambiguous": 0}}
        self.assertIn("lost_commit:order_create", INV.check(self._facts(), ledger)["violations"])

    def test_ambiguous_commits_widen_the_upper_bound(self):
        ledger = {"order_create": {"committed": 9, "ambiguous": 1}, "cancel": {"committed": 4, "ambiguous": 0}}
        self.assertEqual(INV.check(self._facts(), ledger)["violations"], [])

    def test_extra_effects_are_violations(self):
        self.assertIn("unexpected_effect:cancel", INV.check(self._facts(cancel_receipts=5, refund_rows=5),
                                                            self.ledger)["violations"])

    def test_zero_required_facts(self):
        v = INV.check(self._facts(neg_stock=1, stock_delta_mismatch=2), self.ledger)["violations"]
        self.assertIn("neg_stock", v)
        self.assertIn("stock_delta_mismatch", v)

    def test_effect_counts_must_match_receipts(self):
        v = INV.check(self._facts(charge_rows=9), self.ledger)["violations"]
        self.assertIn("order_effect_count", v)


class Runner(unittest.TestCase):
    def test_start_delay_covers_staggered_connects(self):
        cell = OL.Cell("x", "D1", "open", 1000.0, 0, 1, 10, 60)
        self.assertGreaterEqual(R.start_delay_s(cell, 16), 256 / 16 * OL.STAGGER_S + 10)
        closed = OL.Cell("y", "D1", "closed", 0.0, 256, 1, 10, 60)
        self.assertGreaterEqual(R.start_delay_s(closed, 4), 256 / 4 * OL.STAGGER_S + 10)

    def test_load_method_by_kind(self):
        self.assertEqual(R.load_method({"kind": "dsql"}), "insert")
        self.assertEqual(R.load_method({"kind": "pg"}), "copy")
        self.assertEqual(R.load_method({"kind": "dsn"}), "copy")

    def test_pending_chunks_skip_finished(self):
        todo = R.pending_chunks([("orders", 0, 10), ("orders", 10, 20)], {"orders:0:10"})
        self.assertEqual(todo, [("orders", 10, 20)])

    def test_bundle_lists_every_runner_module(self):
        import remote
        self.assertEqual(remote.REMOTE_ROOT, "/opt/e002")
        for f in ("runner.py", "openloop.py", "workload.py", "datagen.py", "schema.py", "invariants.py",
                  "retry.py", "hist.py", "conn.py", "requirements.txt"):
            self.assertIn(f, remote.BUNDLE_FILES)
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), f)), f)


class Infra(unittest.TestCase):
    ch = {"engine": "aurora-postgresql", "version": "16.9", "class": "db.r6g.xlarge"}

    def test_r1_is_plan_sized(self):
        p = IN.r1_params("pfx", {"engine": "postgres", "version": "16.9", "class": "db.r6g.xlarge"}, {}, {})
        self.assertEqual((p["DBInstanceClass"], p["MultiAZ"], p["StorageType"]), ("db.r6g.xlarge", True, "gp3"))
        self.assertEqual((p["AllocatedStorage"], p["Iops"], p["StorageThroughput"]), (400, 12000, 500))

    def test_aurora_uses_standard_storage(self):
        self.assertEqual(IN.aurora_cluster_params("A1", "pfx", self.ch, {})["StorageType"], "aurora")
        self.assertNotIn("ServerlessV2ScalingConfiguration", IN.aurora_cluster_params("A1", "pfx", self.ch, {}))

    def test_a2_scaling_is_4_to_32(self):
        p = IN.aurora_cluster_params("A2", "pfx", self.ch, {})
        self.assertEqual(p["ServerlessV2ScalingConfiguration"], {"MinCapacity": 4, "MaxCapacity": 32})

    def test_reader_is_tier1_in_other_az(self):
        ch = dict(self.ch, **{"class": "db.serverless"})
        w = IN.aurora_instance_params("A2", "pfx", "writer", ch, {}, {}, "ap-northeast-2a")
        r = IN.aurora_instance_params("A2", "pfx", "reader", ch, {}, {}, "ap-northeast-2c")
        self.assertEqual((w["PromotionTier"], r["PromotionTier"]), (0, 1))
        self.assertNotEqual(w["AvailabilityZone"], r["AvailabilityZone"])
        self.assertNotEqual(w["DBInstanceIdentifier"], r["DBInstanceIdentifier"])
        self.assertEqual(w["DBInstanceClass"], "db.serverless")

    def test_plan_classes_runner_and_cidr(self):
        self.assertEqual(IN.DB_CLASS, {"R1": "db.r6g.xlarge", "A1": "db.r6g.xlarge", "A2": "db.serverless"})
        self.assertEqual(IN.RUNNER_TYPE, "c7g.4xlarge")
        self.assertEqual(IN.VPC_CIDR_PREFIX, "10.92")

    def test_db_rate_keys(self):
        self.assertEqual(IN.db_rate("R1"), cost.DB_RATE_USD_PER_H["R1"])
        self.assertEqual(IN.db_rate("A1"), cost.DB_RATE_USD_PER_H["A1_instance"])
        self.assertEqual(IN.db_rate("A2"), cost.DB_RATE_USD_PER_H["A2_instance_worst"])
