"""Offline tests: no AWS or database access. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conn as C  # noqa: E402
import cost  # noqa: E402
import datagen as G  # noqa: E402
import e002 as E  # noqa: E402
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
    def test_config_may_be_provisioned_again_after_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.data["config_status"][S.BATCH] = "ready"
            m.add_resource("A2", "db_cluster", "c", state="created")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "A2")
            m.set_state("A2", "db_cluster", "c", "deleted")
            S.check_can_provision(m.data, "A2")      # pilot deletes A2, the main run provisions it again

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


class Orchestrator(unittest.TestCase):
    def test_parallel_phase_isolates_runner_lost(self):
        def fn(cfg):
            if cfg == "A2":
                raise E.RunnerLost("spot reclaimed")
            return cfg.lower()
        out = E.parallel(["D1", "R1", "A1", "A2"], fn)
        self.assertEqual([out[c]["value"] for c in ("D1", "R1", "A1")], ["d1", "r1", "a1"])
        self.assertTrue(out["A2"]["runner_lost"])
        self.assertFalse(out["A2"]["ok"])

    def test_full_load_refused_when_extrapolation_exceeds_guard(self):
        usd = E.load_estimate(slice_dpu=50_000, fraction=0.02, usd_per_million=10.0)
        self.assertAlmostEqual(usd, 25.0)
        data = {"resources": [], "measured_usd": {"D1_dpu": 25.0}}
        self.assertFalse(cost.Guard(lambda: data, cap=45.0).reserve("D1", usd)["ok"])

    def test_main_cells_shuffle_rates_per_rep_but_cover_all(self):
        a = E.main_cells("D1", [50, 100, 120], 1, 180, 600, "s")
        b = E.main_cells("D1", [50, 100, 120], 2, 180, 600, "s")
        self.assertEqual(sorted(c.rate for c in a), [50, 100, 120])
        self.assertTrue(all(c.mode == "open" for c in a))
        self.assertEqual({c.cell_id for c in a} & {c.cell_id for c in b}, set())

    def test_explore_cells_are_closed_loop_16_64_256(self):
        cells = E.explore_cells("R1", 1, 120, 300)
        self.assertEqual([c.concurrency for c in cells], [16, 64, 256])
        self.assertTrue(all(c.mode == "closed" for c in cells))

    def test_boundary_loop_stops_on_guard(self):
        calls = []

        def run_rate(rate):
            calls.append(rate)
            return _res(rate=rate, rep=1)
        out = E.boundary_loop("D1", 100.0, run_rate, stop_fn=lambda: len(calls) >= 2)
        self.assertEqual(calls, [150.0, 187.5])
        self.assertTrue(out["stopped_early"])

    def test_boundary_loop_stops_when_run_rate_returns_none(self):
        out = E.boundary_loop("D1", 100.0, lambda rate: None, stop_fn=lambda: False)
        self.assertTrue(out["stopped_early"])
        self.assertEqual(out["results"], [])

    def test_boundary_loop_finds_fail_then_midpoint(self):
        def run_rate(rate):
            return _res(rate=rate, rep=1, p99=10.0 if rate <= 160 else 500.0)
        out = E.boundary_loop("R1", 100.0, run_rate, stop_fn=lambda: False)
        self.assertEqual([r["cell"]["rate"] for r in out["results"]], [150.0, 187.5, 168.75])
        self.assertFalse(out["stopped_early"])

    def test_pilot_plan_sums_parts(self):
        pilot = {"d1_dpu_per_attempt": 0.02, "a2_acu_by_rate": {"100.0": 6.0}, "fixed_usd_per_h": 3.0}
        out = E.pilot_plan(pilot, reps=1, warmup_s=0, measure_s=3600, rates=[100.0])
        self.assertAlmostEqual(out["d1_usd"], 100 * 3600 * 0.02 / 1e6 * 10)
        self.assertAlmostEqual(out["a2_usd"], 6.0 * 2 * cost.ACU_USD_PER_H)
        self.assertGreater(out["total_usd"], out["d1_usd"] + out["a2_usd"])

    def test_pilot_plan_scales_acu_above_measured_rates(self):
        pilot = {"d1_dpu_per_attempt": 0.0, "a2_acu_by_rate": {"100.0": 4.0, "400.0": 8.0}, "fixed_usd_per_h": 0.0}
        low = E.pilot_plan(pilot, 1, 0, 3600, [400.0])["a2_usd"]
        high = E.pilot_plan(pilot, 1, 0, 3600, [800.0])["a2_usd"]
        self.assertAlmostEqual(high, 2 * low)

    def test_cell_estimate_uses_config_rates_and_dpu(self):
        m = {"resources": [{"config": "D1", "state": "created", "extra": {"rate_usd_per_h": 0.2}},
                           {"config": "R1", "state": "created", "extra": {"rate_usd_per_h": 1.5}}]}
        cell = OL.Cell("D1-m-100.0-r01", "D1", "open", 100.0, 0, 1, 0, 3600)
        est = E.cell_estimate_usd(m, cell, {"d1_dpu_per_attempt": 0.01}, seen_attempt_tps=0.0)
        overhead_h = (3600 + E.CELL_OVERHEAD_S) / 3600
        want = 0.2 * overhead_h + 100 * 1.1 * (3600 + E.CELL_OVERHEAD_S) * 0.01 / 1e6 * 10
        self.assertAlmostEqual(est, want)

    def test_q_ratios_mark_missing_denominators(self):
        rows = E.q_ratios({"D1": {"q": 300, "status": "lower_bound"}, "R1": {"q": 100, "status": "confirmed"},
                           "A1": {"q": None, "status": "none"}})
        self.assertEqual(rows["R1"]["throughput_ratio"], 3.0)
        self.assertEqual(rows["R1"]["note"], "D1 lower_bound")
        self.assertIsNone(rows["A1"]["throughput_ratio"])


class Conn(unittest.TestCase):
    def test_dsql_factory_signs_a_fresh_token_per_connection(self):
        # DSQL tokens expire and connections last at most one hour: every new connection needs a new token
        target = {"kind": "dsql", "host": "h", "dbname": "postgres", "region": "r", "sslrootcert": "ca"}
        tokens = iter(["t1", "t2"])
        with mock.patch.object(C, "_credentials", side_effect=lambda t: ("admin", next(tokens))), \
                mock.patch.object(C.psycopg, "connect", side_effect=lambda **kw: kw["password"]):
            connect = C.sync_connect_factory(target)
            self.assertEqual([connect(), connect()], ["t1", "t2"])

    def test_pg_factory_fetches_the_secret_once(self):
        target = {"kind": "pg", "host": "h", "dbname": "d", "region": "r", "sslrootcert": "ca", "secret_arn": "s"}
        with mock.patch.object(C, "_credentials", return_value=("u", "p")) as cred, \
                mock.patch.object(C.psycopg, "connect", side_effect=lambda **kw: kw["password"]):
            connect = C.sync_connect_factory(target)
            connect()
            connect()
            self.assertEqual(cred.call_count, 1)


class ReviewFixes(unittest.TestCase):
    def test_recreated_resource_restarts_its_clock(self):
        # C2: pilot deletes A2, the main run re-creates the same ids; cost must count from the re-creation
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource("A2", "db_instance", "w1", state="created", rate_usd_per_h=10.0)
            r = m.find("A2", "db_instance", "w1")
            r["recorded_at"] = S.iso(S.utcnow() - timedelta(hours=3))
            r["extra"].update(measured_usd=1.0, measured_until=S.iso(S.utcnow() - timedelta(hours=2)))
            m.set_state("A2", "db_instance", "w1", "deleted")
            r["deleted_at"] = S.iso(S.utcnow() - timedelta(hours=2))
            m.add_resource("A2", "db_instance", "w1", state="requested", rate_usd_per_h=10.0)
            m.set_state("A2", "db_instance", "w1", "created")
            r = m.find("A2", "db_instance", "w1")
            later = S.utcnow() + timedelta(hours=1)
            self.assertNotIn("deleted_at", r)
            self.assertNotIn("measured_usd", r["extra"])
            self.assertAlmostEqual(cost.resource_usd(r, later), 1.0 + 10.0, delta=0.1)   # earlier life + 1 h

    def test_recreated_resource_keeps_the_earlier_lifetime_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource("A2", "db_instance", "w1", state="created", rate_usd_per_h=10.0)
            r = m.find("A2", "db_instance", "w1")
            r["recorded_at"] = S.iso(S.utcnow() - timedelta(hours=3))
            m.set_state("A2", "db_instance", "w1", "deleted")
            r["deleted_at"] = S.iso(S.utcnow() - timedelta(hours=2))      # lived 1 h: USD 10
            m.add_resource("A2", "db_instance", "w1", state="requested", rate_usd_per_h=10.0)
            self.assertAlmostEqual(cost.spent_usd(m.data), 10.0, delta=0.1)

    def test_runner_alive_raises_runner_lost_for_a_deleted_or_vanished_runner(self):
        # I8: EC2 forgets terminated instances after about an hour (InvalidInstanceID.NotFound)
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource("D1", "ec2_instance", "i-1", state="created")
            m.data["runners"] = {"D1": "i-1"}

            class NotFound(Exception):
                response = {"Error": {"Code": "InvalidInstanceID.NotFound"}}

            class EC2:
                def describe_instances(self, **kw):
                    raise NotFound()

            class Sess:
                def client(self, name):
                    return EC2()
            with self.assertRaises(E.RunnerLost):
                E.runner_alive(Sess(), m, "D1")
            m.set_state("D1", "ec2_instance", "i-1", "deleted")
            with self.assertRaises(E.RunnerLost):
                E.runner_alive(Sess(), m, "D1")

    def test_load_config_resumes_without_dropping_the_schema(self):
        # C1: a rerun after a failed load must not DROP tables while the runner still has chunk progress
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            calls = []

            def fake_runner_json(sess, m_, cfg, command, extra, timeout_s, what):
                calls.append(command)
                if command == "load" and calls.count("load") == 1:
                    raise RuntimeError("SSM failed mid-load")
                return {"rows": {}, "seconds": 1, "method": "copy", "fraction": 1.0, "index_wait_s": 0}
            with mock.patch.object(E, "runner_json", fake_runner_json):
                with self.assertRaises(RuntimeError):
                    E.load_config(None, m, "R1", 1.0)
                E.load_config(None, m, "R1", 1.0)
            self.assertEqual(calls, ["schema", "load", "load"])


import psycopg  # noqa: E402


class _PgErr(psycopg.Error):
    def __init__(self, sqlstate):
        self._st = sqlstate
        super().__init__(sqlstate)

    @property
    def sqlstate(self):
        return self._st


class _Err(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class ReviewFixesLoadAndOps(unittest.TestCase):
    def test_chunk_that_landed_before_a_dropped_commit_counts_as_done(self):
        # I1: connection drops during COMMIT, the retry hits 23505 -> the chunk is already in
        seq = [_Err(None), _Err("23505")]

        def insert():
            if seq:
                raise seq.pop(0)
        self.assertEqual(R.insert_with_retry(insert, reconnect=lambda: None, sleep=lambda s: None), "landed")

    def test_first_attempt_unique_violation_means_the_chunk_is_already_loaded(self):
        # chunks are single transactions over disjoint keys: 23505 can only come from the same chunk's earlier
        # commit (a resumed load whose worker died before recording progress)
        def insert():
            raise _Err("23505")
        self.assertEqual(R.insert_with_retry(insert, reconnect=lambda: None, sleep=lambda s: None), "landed")

    def test_progress_is_read_from_every_worker_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "load.json.progress")
            json.dump(["orders:0:10"], open(base, "w"))
            open(base + ".w0", "w").write("orders:10:20\n")
            open(base + ".w3", "w").write("orders:20:30\norders:30:4")      # torn last line is ignored
            self.assertEqual(R.read_progress(base), {"orders:0:10", "orders:10:20", "orders:20:30"})

    def test_worker_reconnects_before_the_dsql_hour(self):
        self.assertTrue(R.needs_refresh(opened_at=0.0, now=R.CONN_MAX_AGE_S + 1))
        self.assertFalse(R.needs_refresh(opened_at=0.0, now=10.0))

    def _run(self, txn, receipt, connect=None):
        import asyncio

        class H(W.ConnHolder):
            pass
        h = H(connect or (lambda: None))
        h.conn = object()
        op = W.Op("order_create", "c:0:1", 1, SC.RUN_ID_BASE + 1, ((0, 1, 1),))
        with mock.patch.object(W, "_txn", txn), mock.patch.object(W, "_receipt_exists", receipt):
            return asyncio.run(W.run_op(h, op, W.retry.RetryPolicy(3, deadline_s=0.2), random.Random(0),
                                        time.monotonic()))

    def test_deadline_during_commit_without_receipt_is_ambiguous(self):
        # I3: the commit may still land after the lookup; no retry follows, so keep it ambiguous
        import asyncio

        async def txn(h, op):
            h.in_commit = True
            await asyncio.sleep(1)

        async def receipt(h, op):
            return False
        res = self._run(txn, receipt)
        self.assertEqual(res.outcome, "failed")
        self.assertTrue(res.ambiguous)

    def test_failed_reconnect_is_a_failed_attempt_not_an_exception(self):
        # I4: a database refusing connections must show up as failed/connection, not crash the cell
        async def txn(h, op):
            raise _PgErr("40001")

        async def receipt(h, op):
            return False

        async def refuse():
            raise OSError("connection refused")
        with mock.patch.object(W.ConnHolder, "rollback_quiet", W.ConnHolder.rollback_quiet):
            res = self._run(txn, receipt, connect=refuse)
        self.assertEqual(res.outcome, "failed")

    def test_dsql_token_is_reused_within_its_ttl(self):
        # I5: signing per connection blocks the event loop; cache the token for a few minutes
        C._TOKEN_CACHE.clear()
        target = {"kind": "dsql", "host": "h", "region": "r"}
        signer = mock.Mock(return_value="tok")
        with mock.patch.object(C, "_sign", signer):
            C._dsql_token(target, now=0.0)
            C._dsql_token(target, now=60.0)
            C._dsql_token(target, now=C.TOKEN_TTL_S + 1)
        self.assertEqual(signer.call_count, 2)


class ReviewFixesCostAndMeasurement(unittest.TestCase):
    def test_pending_tail_counts_until_cloudwatch_covers_it(self):
        # I6: DPU of a D1 cell's last minutes is not in CloudWatch yet when the reservation ends
        data = {"resources": [], "measured_usd": {}, "pending_usd": {}}
        cost.add_pending(data, "D1:cell1", 0.5, until="2026-09-28T10:10:00Z")
        self.assertAlmostEqual(cost.spent_usd(data), 0.5)
        cost.settle_pending(data, covered_until="2026-09-28T10:05:00Z")
        self.assertAlmostEqual(cost.spent_usd(data), 0.5)
        cost.settle_pending(data, covered_until="2026-09-28T10:11:00Z")
        self.assertAlmostEqual(cost.spent_usd(data), 0.0)

    def test_pilot_cells_book_dpu_with_the_e004_prior(self):
        data = {"resources": []}
        cell = OL.Cell("D1-p-1600", "D1", "open", 1600.0, 0, 0, 60, 120)
        self.assertGreater(E.cell_estimate_usd(data, cell, None, 0.0), 0.0)

    def test_loads_of_controls_are_booked(self):
        data = {"resources": [{"config": "R1", "state": "created", "extra": {"rate_usd_per_h": 1.5}}]}
        self.assertAlmostEqual(E.load_booking_usd(data, "R1"), 1.5 * E.LOAD_HOURS_EST)

    def test_forgetting_a_deleted_config_clears_its_schema_step_too(self):
        # after the pilot deletes A2, a re-created A2 is empty: its schema must be created again
        import threading

        class M:
            data = {"steps": {"A2": ["schema:1.0", "provision", "load:1.0", "ready"], "D1": ["ready"]}}
            lock = threading.RLock()

            def save(self):
                pass

        m = M()
        E._forget_config(m, "A2")
        self.assertEqual(m.data["steps"]["A2"], [])
        self.assertEqual(m.data["steps"]["D1"], ["ready"])

    def test_reset_batches_select_their_ids_once(self):
        # re-running the receipts join per batch made a D1 reset outlive the cell timeout (2026-09-28)
        import contextlib
        import invariants as INV

        class Conn:
            def __init__(self):
                self.selects, self.applied = 0, []

            def execute(self, sql, params=None):
                if sql.startswith("SELECT"):
                    self.selects += 1
                    rows = [(i,) for i in range(5)]
                    return mock.Mock(fetchall=lambda: rows)
                self.applied.append(list(params[0]))
                return mock.Mock()

            def transaction(self):
                return contextlib.nullcontext()

        c = Conn()
        self.assertEqual(INV._batched(c, "SELECT id FROM t", "DELETE FROM t WHERE id = ANY(%s)", 2), 5)
        self.assertEqual(c.selects, 1)
        self.assertEqual(c.applied, [[0, 1], [2, 3], [4]])

    def test_cell_scope_matches_the_ids_the_workload_gives_the_cell(self):
        import invariants as INV
        import workload as W
        lo, hi, op_lo, op_hi = INV.cell_scope("D1-r-2400.0-r01")
        rid = W.ref_id("D1-r-2400.0-r01", 15, 123456)
        self.assertTrue(lo <= rid < hi)
        self.assertFalse(lo <= W.ref_id("D1-r-3600.0-r01", 0, 0) < hi)
        self.assertTrue(op_lo <= "D1-r-2400.0-r01:15:123456" < op_hi)
        self.assertFalse(op_lo <= "D1-r-2400.0-r010:0:0" < op_hi)

    def test_terminating_a_spot_runner_also_cancels_its_request(self):
        # a one-time request stayed 'active' after its instance was terminated, so verify counted it (2026-09-28)
        ec2 = mock.Mock()
        sess = mock.Mock(client=lambda name: ec2)
        r = {"type": "ec2_instance", "id": "i-1", "extra": {"spot_request": "sir-1"}}
        IN._delete(sess, r, {})
        ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-1"])
        ec2.cancel_spot_instance_requests.assert_called_once_with(SpotInstanceRequestIds=["sir-1"])

    def test_runner_launch_falls_back_to_the_next_spot_type(self):
        self.assertEqual(IN.runner_types("c6g.4xlarge,m7g.4xlarge"), ["c6g.4xlarge", "m7g.4xlarge"])
        self.assertEqual(IN.runner_types("c7g.4xlarge"), ["c7g.4xlarge"])

    def test_dsql_ramp_rates_grow_by_half_from_the_pilot_rate(self):
        self.assertEqual(E.ramp_rates(1600, 1.5, 13000), [2400, 3600, 5400, 8100, 12150])

    def test_dsql_ramp_summary_takes_the_highest_pass_below_the_first_fail(self):
        steps = [(2400, "pass"), (3600, "pass"), (5400, "fail"), (4500, "pass")]
        self.assertEqual(E.ramp_summary(steps), {"q": 4500, "status": "confirmed", "first_fail": 5400})
        self.assertEqual(E.ramp_summary([(2400, "pass"), (3600, "pass")]),
                         {"q": 3600, "status": "lower_bound", "first_fail": None})
        self.assertEqual(E.ramp_summary([(2400, "fail")]), {"q": None, "status": "none", "first_fail": 2400})
        # an invalid cell (e.g. generator saturated) stops the ramp without proving a failure
        self.assertEqual(E.ramp_summary([(2400, "pass"), (3600, "invalid")]),
                         {"q": 2400, "status": "lower_bound", "first_fail": None})

    def test_hard_cap_is_the_e002_cap_raised_on_2026_09_28(self):
        self.assertEqual(cost.HARD_CAP_USD, 60.0)

    def test_a_reload_is_booked_from_the_measured_load_time(self):
        # A2 was re-created after the pilot; its earlier full load took 552 s
        data = {"resources": [{"config": "A2", "state": "created", "extra": {"rate_usd_per_h": 12.8}}],
                "loads": {"A2:1.0": {"seconds": 552.1}}}
        self.assertAlmostEqual(E.load_booking_usd(data, "A2"), 12.8 * E.LOAD_REPEAT_FACTOR * 552.1 / 3600)

    def test_aligned_window_stays_inside_the_measure_window(self):
        start, end = E.aligned_window("2026-09-28T10:00:20Z", "2026-09-28T10:02:20Z")
        self.assertEqual((S.iso(start), S.iso(end)), ("2026-09-28T10:01:00Z", "2026-09-28T10:02:00Z"))

    def test_dpu_per_attempt_uses_attempts_of_the_same_window(self):
        # 60 s aligned window out of a 120 s measure window: half of the measured attempts
        self.assertAlmostEqual(E.dpu_per_attempt(dpu=600.0, attempt_tps=100.0, window_s=60.0), 0.1)

    def test_unknown_generator_cpu_is_invalid(self):
        self.assertEqual(slo.judge(_res(cpu=None))["verdict"], "invalid")

    def test_cloudwatch_queries_per_config(self):
        conns = {"R1": {"instance_id": "r1"}, "A2": {"cluster_id": "c", "writer": "w", "reader": "r"},
                 "D1": {"cluster_id": "d"}}
        names = {q["metric"] for q in E.cloudwatch_queries("A2", conns["A2"])}
        self.assertTrue({"CPUUtilization", "DatabaseConnections", "ServerlessDatabaseCapacity"} <= names)
        self.assertIn("ReadIOPS", {q["metric"] for q in E.cloudwatch_queries("R1", conns["R1"])})
        self.assertEqual({q["namespace"] for q in E.cloudwatch_queries("D1", conns["D1"])}, {"AWS/AuroraDSQL"})

    def test_saturation_cause_prefers_generator_then_db_cpu(self):
        r = _res(cpu=95.0)
        self.assertEqual(E.saturation_cause(r), "generator")
        r = _res()
        r["cloudwatch"] = {"CPUUtilization": {"max": 97.0}}
        self.assertEqual(E.saturation_cause(r), "db_cpu")
        r = _res()
        r["metrics"]["per_kind"]["product_read"]["queue_p99_ms"] = 800.0
        self.assertEqual(E.saturation_cause(r), "connection_pool")

    def test_lag_is_recorded_only_inside_the_measure_window(self):
        import asyncio

        class FakeHolder:
            def __init__(self, connect):
                pass

            async def open(self):
                pass

            async def close(self):
                pass

        async def fake_run_op(h, op, policy, rng, t):
            return W.Result("committed", 1)
        cell = OL.Cell("lag", "LOCAL", "open", 200.0, 0, 1, 1, 1, 0.001)
        st = OL.Stats()
        with mock.patch.object(OL.W, "ConnHolder", FakeHolder), mock.patch.object(OL.W, "run_op", fake_run_op), \
                mock.patch.object(OL, "STAGGER_S", 0.0):
            asyncio.run(OL._open_proc(cell, 0, 200.0, 2, None, time.monotonic(), G.scaled(0.001), st))
        sched = OL.poisson_schedule(200.0, 2, random.Random("sched:lag:0"))
        self.assertEqual(st.lag.count, sum(1 for t in sched if t >= 1))

    def test_batch_down_cleans_every_config_and_refreshes_first(self):
        calls = []
        m = mock.Mock()
        m.data = {"resources": [], "config_status": {}}
        m.prefix = PFX
        with mock.patch.object(E, "session", return_value=(None, {})), \
                mock.patch.object(E, "load_manifest", return_value=m), \
                mock.patch.object(E.IN, "cleanup", side_effect=lambda sess, m_, cfg: calls.append(cfg)), \
                mock.patch.object(E.IN, "verify", return_value={"verified_at": "x", "remaining_count": 0,
                                                                "tag_index_arns_for_review": []}), \
                mock.patch.object(E.S, "write_private"):
            E.main(["batch-down", "--account-id", ACCT, "--prefix", PFX])
        self.assertEqual(sorted(calls), sorted(S.CONFIGS))

    def test_phases_refresh_measured_costs_before_booking(self):
        order = []
        m = mock.Mock()
        m.data = {"resources": []}
        m.prefix = PFX
        with mock.patch.object(E, "refresh_measured", side_effect=lambda *a: order.append("refresh")), \
                mock.patch.object(E, "_require_ready"), mock.patch.object(E, "_pilot", return_value=None), \
                mock.patch.object(E, "run_cell_on", side_effect=lambda *a, **k: order.append("cell") or _res()), \
                mock.patch.object(E.S, "write_private"):
            E.do_explore(None, m, mock.Mock(budget_cap=45.0, explore_warmup_s=1, explore_measure_s=1))
        self.assertEqual(order[0], "refresh")


class CloudWatchPerCell(unittest.TestCase):
    def test_fetch_summarizes_avg_and_max_and_expands_dsql_metrics(self):
        class CW:
            def list_metrics(self, Namespace):
                return {"Metrics": [{"MetricName": "TotalDPU", "Dimensions": [{"Name": "ClusterId", "Value": "d"}]},
                                    {"MetricName": "TotalDPU", "Dimensions": [{"Name": "ClusterId", "Value": "x"}]}]}

            def get_metric_statistics(self, **kw):
                return {"Datapoints": [{"Average": 10.0, "Maximum": 20.0, "Sum": 30.0},
                                       {"Average": 30.0, "Maximum": 50.0, "Sum": 60.0}]}
        now = S.utcnow()
        out = E.fetch_cloudwatch(CW(), [{"namespace": "AWS/RDS", "metric": "CPUUtilization",
                                          "dims": {"DBInstanceIdentifier": "w"}}], now, now)
        self.assertEqual(out["CPUUtilization"], {"avg": 20.0, "max": 50.0, "sum": 90.0, "points": 2})
        out = E.fetch_cloudwatch(CW(), [{"namespace": "AWS/AuroraDSQL", "metric": "*", "dims": "d"}], now, now)
        self.assertEqual(list(out), ["TotalDPU"])

    def test_summarize_row_carries_saturation_cause_for_failed_cells(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(E, "ART", tmp):
            d = os.path.join(tmp, PFX, "results", "R1")
            os.makedirs(d)
            r = _res(p99=500.0)
            r["cell"].update(cell_id="R1-m-100.0-r01", mode="open", rate=100.0, rep=1)
            r["cloudwatch"] = {"CPUUtilization": {"max": 99.0}}
            json.dump(r, open(os.path.join(d, "R1-m-100.0-r01.json"), "w"))
            E.summarize(PFX)
            row = json.load(open(os.path.join(tmp, PFX, "summary.json")))["cells"][0]
        self.assertEqual((row["verdict"], row["saturation"]), ("fail", "db_cpu"))


class DsqlTransientErrors(unittest.TestCase):
    def test_server_unavailable_is_retried(self):
        # observed in the E002 pilot load and in E004: psycopg InternalError_ (XX000) "server unavailable"
        err = _Err("XX000")
        err.args = ("server unavailable",)
        seq = [err]

        def insert():
            if seq:
                raise seq.pop(0)
        self.assertEqual(R.insert_with_retry(insert, reconnect=lambda: None, sleep=lambda s: None), "inserted")

    def test_other_internal_errors_are_not_retried(self):
        err = _Err("XX000")
        err.args = ("something else",)

        def insert():
            raise err
        with self.assertRaises(_Err):
            R.insert_with_retry(insert, reconnect=lambda: None, sleep=lambda s: None)


class SchemaForeignKeyIndexes(unittest.TestCase):
    def test_every_foreign_key_column_used_by_deletes_is_indexed(self):
        # E002 pilot: deleting orders made the ledger FK check scan 11M rows; DSQL aborted it at its 300 s
        # transaction limit and PostgreSQL ran one DELETE for 24 minutes
        for kind in ("pg", "dsql"):
            ddl = " ".join(SC.ddl(kind))
            self.assertIn("ON ledger (order_id)", ddl, kind)

    def test_index_names_listed_for_the_async_wait(self):
        self.assertEqual(set(SC.INDEX_NAMES), {"orders_customer_created", "ledger_order"})


class IndexWait(unittest.TestCase):
    def test_wait_until_indexes_are_valid_not_just_visible(self):
        # DSQL lists an ASYNC index in pg_indexes at once while its build job is still processing
        counts = iter([0, 1, 2])
        seen = []

        class Cur:
            def __init__(self, n):
                self.n = n

            def fetchone(self):
                return (self.n,)

        class Conn:
            def execute(self, sql, params=None):
                seen.append(sql)
                return Cur(next(counts))
        with mock.patch.object(R.time, "sleep"):
            R.wait_indexes(Conn(), time.monotonic())
        self.assertEqual(len(seen), 3)
        self.assertIn("indisvalid", seen[0])
