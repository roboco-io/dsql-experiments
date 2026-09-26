"""Offline tests: no AWS or database access. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import random
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import safety as S  # noqa: E402

ACCT = "123456789012"
PFX = "e004-20260925t010203z-ab12"


def _manifest(tmp, lifetime=600):
    return S.Manifest.create(os.path.join(tmp, "m.json"), ACCT, S.REGION, PFX, lifetime, "arn:aws:iam::x:user/op")


class Safeguards(unittest.TestCase):
    def test_prefix_and_identity(self):
        self.assertTrue(S.new_run_prefix().startswith("e004-"))
        S.validate_run_prefix(PFX)
        for bad in ("e001-20260925t010203z-ab12", "e004-bad"):
            with self.assertRaises(S.SafetyError):
                S.validate_run_prefix(bad)
        S.assert_identity(ACCT, {"Account": ACCT})
        with self.assertRaises(S.SafetyError):
            S.assert_identity(ACCT, {"Account": "999999999999"})

    def test_manifest_private_and_lifetime_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            self.assertEqual(stat.S_IMODE(os.stat(m.path).st_mode), 0o600)
            self.assertEqual(m.data["experiment"], "E004")
            with self.assertRaises(S.SafetyError):
                _manifest(tmp)  # already exists
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.SafetyError):
                _manifest(tmp, lifetime=S.MAX_LIFETIME_MIN + 1)

    def test_scope_types_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            with self.assertRaises(S.SafetyError):
                m.add_resource("D1", "vpc", "vpc-1")
            m.add_resource(S.BATCH, "vpc", "vpc-1", state="created")

    def test_owned_requires_exact_tags(self):
        tags = S.resource_tags(PFX, "R1", "2026-09-25T10:00:00Z")
        self.assertTrue(S.owned(tags, PFX, "R1"))
        self.assertFalse(S.owned(tags, PFX, "A1"))
        self.assertFalse(S.owned({**tags, S.TAG_MANAGED: "other"}, PFX))


class Ordering(unittest.TestCase):
    def test_batch_first_then_one_config_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "D1")  # batch not ready
            S.check_can_provision(m.data, S.BATCH)
            m.add_resource(S.BATCH, "vpc", "vpc-1", state="created")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, S.BATCH)  # batch exists
            m.data["config_status"][S.BATCH] = "ready"
            S.check_can_provision(m.data, "D1")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "R1")  # D1 not cleaned
            S.check_can_provision(m.data, "R1", allow_order_override=True)
            m.add_resource("D1", "dsql_cluster", "c1", state="created")
            with self.assertRaises(S.SafetyError):
                S.check_can_provision(m.data, "R1", allow_order_override=True)  # D1 active

    def test_cleanup_order_and_batch_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            for t, i in (("vpc", "v"), ("client_security_group", "c"), ("db_security_group", "d"),
                         ("iam_role", "r"), ("instance_profile", "p"), ("ec2_instance", "i"),
                         ("internet_gateway", "g"), ("subnet", "s"), ("db_subnet_group", "n")):
                m.add_resource(S.BATCH, t, i, state="created")
            m.add_resource("R1", "db_instance", "db", state="created")
            with self.assertRaises(S.SafetyError):
                S.cleanup_plan(m.data, S.BATCH)  # R1 still active
            m.set_state("R1", "db_instance", "db", "deleted")
            order = [r["type"] for r in S.cleanup_plan(m.data, S.BATCH)]
            self.assertEqual(order, ["ec2_instance", "instance_profile", "iam_role", "db_subnet_group",
                                     "db_security_group", "client_security_group", "subnet",
                                     "internet_gateway", "vpc"])
            m.add_resource("A1", "rds_secret", "arn:s", state="created")
            m.add_resource("A1", "db_instance", "w1", state="created")
            m.add_resource("A1", "db_cluster", "c", state="created")
            self.assertEqual([r["type"] for r in S.cleanup_plan(m.data, "A1")],
                             ["db_instance", "db_cluster", "rds_secret"])


import cost as C  # noqa: E402


class Cost(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc)

    def _data(self):
        return {"dsql_dpu_usd": 0.5, "resources": [
            {"config": "R1", "type": "db_instance", "id": "db", "state": "deleted",
             "recorded_at": "2026-09-25T10:00:00Z", "deleted_at": "2026-09-25T12:00:00Z",
             "extra": {"rate_usd_per_h": 0.203}},
            {"config": "BATCH", "type": "ec2_instance", "id": "i", "state": "created",
             "recorded_at": "2026-09-25T10:00:00Z", "extra": {"rate_usd_per_h": 0.1}},
            {"config": "BATCH", "type": "vpc", "id": "v", "state": "created",
             "recorded_at": "2026-09-25T10:00:00Z", "extra": {}},
        ]}

    def test_spent_includes_deleted_active_and_dpu(self):
        self.assertAlmostEqual(C.spent_usd(self._data(), self.NOW), 0.406 + 0.3 + 0.5, places=6)
        self.assertAlmostEqual(C.active_rate(self._data()), 0.1)

    def test_guard_stops_before_cap(self):
        g = C.guard(self._data(), 90, now=self.NOW)
        self.assertAlmostEqual(g["next_cell_usd"], 0.1 * 90 / 3600)
        self.assertAlmostEqual(g["reserve_usd"], 0.05)
        self.assertTrue(g["ok"])
        self.assertFalse(C.guard(self._data(), 90, cap=1.25, now=self.NOW)["ok"])

    def test_guard_counts_dpu_estimate(self):
        g = C.guard(self._data(), 90, est_cell_dpu=2_000_000, dpu_usd_per_million=1.0, now=self.NOW)
        self.assertAlmostEqual(g["next_cell_usd"], 0.1 * 90 / 3600 + 2.0)
        self.assertFalse(C.guard(self._data(), 90, est_cell_dpu=4_000_000, dpu_usd_per_million=1.0,
                                 cap=4.5, now=self.NOW)["ok"])

    def test_caps_follow_the_per_experiment_budget(self):
        self.assertEqual((C.HARD_CAP_USD, C.BUDGET_CAP_USD), (50.0, 45.0))   # user-set 2026-09-26

    def test_measured_cost_replaces_worst_case_until_measured_point(self):
        r = {"recorded_at": "2026-09-25T10:00:00Z", "state": "created",
             "extra": {"rate_usd_per_h": 1.04, "measured_usd": 0.5, "measured_until": "2026-09-25T12:00:00Z"}}
        self.assertAlmostEqual(C.resource_usd(r, self.NOW), 0.5 + 1.04)


import retry as R  # noqa: E402


class Retry(unittest.TestCase):
    def test_policies(self):
        self.assertEqual(R.POLICIES["none"].max_attempts, 1)
        self.assertEqual(R.POLICIES["retry3"].max_attempts, 3)
        self.assertEqual(R.POLICIES["retry3"].deadline_s, 2.0)

    def test_next_delay(self):
        rng = random.Random(1)
        p = R.POLICIES["retry3"]
        self.assertIsNone(p.next_delay(1, "23505", 0.0, rng))           # not retryable
        self.assertIsNone(R.POLICIES["none"].next_delay(1, "40001", 0.0, rng))
        d1 = p.next_delay(1, "40001", 0.0, rng)
        self.assertTrue(0 <= d1 <= 0.01)
        d2 = p.next_delay(2, "40P01", 0.0, rng)
        self.assertTrue(0 <= d2 <= 0.02)
        self.assertIsNone(p.next_delay(3, "40001", 0.0, rng))           # attempts exhausted
        fixed = type("FixedRng", (), {"uniform": lambda self, a, b: 0.005})()
        self.assertEqual(p.next_delay(1, "40001", 1.99, fixed), 0.005)
        self.assertIsNone(p.next_delay(1, "40001", 1.996, fixed))       # sleep would cross the 2 s deadline

    def test_classify_and_ambiguity(self):
        self.assertEqual(R.classify("40001"), "serialization")
        self.assertEqual(R.classify("40P01"), "deadlock")
        self.assertEqual(R.classify("23505"), "unique_violation")
        self.assertEqual(R.classify(None), "connection")
        self.assertEqual(R.classify("08006"), "connection")
        self.assertEqual(R.classify("timeout"), "timeout")
        self.assertEqual(R.classify("42P01"), "other")
        self.assertTrue(R.is_ambiguous(None, True))
        self.assertTrue(R.is_ambiguous("08006", True))
        self.assertFalse(R.is_ambiguous(None, False))
        self.assertFalse(R.is_ambiguous("40001", True))


import hist as H  # noqa: E402


class Histogram(unittest.TestCase):
    def test_percentiles_within_one_percent(self):
        h = H.LogHistogram()
        for ms in range(1, 1001):
            h.record(float(ms))
        self.assertEqual(h.count, 1000)
        self.assertAlmostEqual(h.percentile(50), 500, delta=500 * 0.011)
        self.assertAlmostEqual(h.percentile(99), 990, delta=990 * 0.011)
        self.assertEqual(h.percentile(100), 1000)
        self.assertEqual((h.min, h.max), (1.0, 1000.0))

    def test_merge_roundtrip_empty(self):
        a, b = H.LogHistogram(), H.LogHistogram()
        self.assertIsNone(a.percentile(50))
        a.record(5)
        b.record(50)
        a.merge(b)
        c = H.LogHistogram.from_dict(a.to_dict())
        self.assertEqual((c.count, c.min, c.max), (2, 5, 50))
        self.assertEqual(c.percentile(100), 50)
        with self.assertRaises(ValueError):
            a.merge(H.LogHistogram(precision=0.05))


import re  # noqa: E402
import workload as W  # noqa: E402


class Workload(unittest.TestCase):
    def test_batches_respect_row_quota_and_cover_all_rows(self):
        rows = {"e004_products": 0, "e004_inventory": 0, "e004_accounts": 0}
        for sql, params in W.seed_batches():
            table = re.search(r"INSERT INTO (\w+)", sql).group(1)
            ncols = len(re.search(r"\(([^)]*)\) VALUES", sql).group(1).split(","))
            n = len(params) // ncols
            self.assertLessEqual(n, W.MAX_ROWS_PER_TXN)
            rows[table] += n
        self.assertEqual(rows, {"e004_products": W.N_PRODUCTS, "e004_inventory": W.N_PRODUCTS,
                                "e004_accounts": W.N_ACCOUNTS})
        covered = 0
        for sql, (value, lo, hi) in W.restore_batches():
            self.assertLessEqual(hi - lo + 1, W.MAX_ROWS_PER_TXN)
            covered += hi - lo + 1
        self.assertEqual(covered, W.N_PRODUCTS + W.N_ACCOUNTS)
        for _table, _sel, _tpl, limit, rows_per_key in W.DELETE_PLANS:
            self.assertLessEqual(limit * rows_per_key, W.MAX_ROWS_PER_TXN)

    def test_hot_distribution(self):
        rng = random.Random(7)
        hot_n = int(W.N_PRODUCTS * W.HOT_FRACTION)
        draws = [W.choose_key(rng, W.N_PRODUCTS, "hot") for _ in range(20000)]
        share = sum(k <= hot_n for k in draws) / len(draws)
        self.assertAlmostEqual(share, 0.8, delta=0.02)
        uni = [W.choose_key(rng, W.N_PRODUCTS, "uniform") for _ in range(20000)]
        self.assertAlmostEqual(sum(k <= hot_n for k in uni) / len(uni), 0.01, delta=0.005)
        self.assertTrue(all(1 <= k <= W.N_PRODUCTS for k in draws + uni))
        with self.assertRaises(ValueError):
            W.choose_key(rng, 10, "zipf")

    def test_ops_are_well_formed_and_unique(self):
        rng = random.Random(3)
        ops = [W.make_op(rng, "hot", "cellX", w, s) for w in range(4) for s in range(500)]
        kinds = [o.kind for o in ops]
        self.assertAlmostEqual(kinds.count("order") / len(ops), 0.7, delta=0.03)
        self.assertEqual(len({o.ref_id for o in ops}), len(ops))
        self.assertEqual(len({o.op_id for o in ops}), len(ops))
        for o in ops:
            if o.kind == "order":
                pids = [p for p, _ in o.items]
                self.assertEqual(pids, sorted(set(pids)))
                self.assertTrue(all(1 <= q <= 3 for _, q in o.items))
            else:
                a, b, amt = o.transfer
                self.assertNotEqual(a, b)
                self.assertTrue(1 <= amt <= 100)
        with self.assertRaises(ValueError):
            W.ref_id(-1, 0)

    def test_begin_sql(self):
        self.assertEqual(W.begin_sql("REPEATABLE READ"), "BEGIN ISOLATION LEVEL REPEATABLE READ")
        with self.assertRaises(ValueError):
            W.begin_sql("repeatable read; DROP TABLE x")


import invariants as I  # noqa: E402


def _clean_facts(orders=10, transfers=5):
    f = {k: 0 for k in I.QUERIES}
    f.update(balance_total=W.N_ACCOUNTS * W.INITIAL_BALANCE, orders=orders, transfers=transfers)
    return f


class Invariants(unittest.TestCase):
    LED = {"order": {"committed": 10, "ambiguous": 0}, "transfer": {"committed": 5, "ambiguous": 0}}

    def test_clean(self):
        self.assertEqual(I.check(_clean_facts(), self.LED, "REPEATABLE READ"), ([], []))

    def test_each_violation_detected(self):
        for key in ("neg_stock", "stock_mismatch", "account_mismatch", "orders_without_receipt",
                    "transfers_without_receipt", "receipts_without_effect", "orders_without_items",
                    "items_without_order", "bad_totals"):
            f = _clean_facts()
            f[key] = 1
            v, _ = I.check(f, self.LED, "REPEATABLE READ")
            self.assertTrue(any(key in x for x in v), key)
        f = _clean_facts()
        f["balance_total"] -= 1
        self.assertTrue(I.check(f, self.LED, "REPEATABLE READ")[0])

    def test_lost_and_unexpected_commits(self):
        self.assertTrue(any("lost commit" in x for x in I.check(_clean_facts(orders=9), self.LED,
                                                                 "REPEATABLE READ")[0]))
        self.assertTrue(any("unexpected" in x for x in I.check(_clean_facts(orders=11), self.LED,
                                                                "REPEATABLE READ")[0]))
        led = {"order": {"committed": 10, "ambiguous": 1}, "transfer": {"committed": 5, "ambiguous": 0}}
        self.assertEqual(I.check(_clean_facts(orders=11), led, "REPEATABLE READ"), ([], []))

    def test_negative_balance_is_observation_only_under_rc(self):
        f = _clean_facts()
        f["neg_balance"] = 2
        self.assertEqual(I.check(f, self.LED, "READ COMMITTED")[0], [])
        self.assertTrue(I.check(f, self.LED, "READ COMMITTED")[1])
        self.assertTrue(I.check(f, self.LED, "REPEATABLE READ")[0])


import scenarios as SC  # noqa: E402


class Scenarios(unittest.TestCase):
    def test_expected_table_complete(self):
        for s in SC.SCENARIOS:
            for lvl in W.LEVELS:
                self.assertIn((s, lvl), SC.EXPECTED)

    def test_outcome_and_judge(self):
        self.assertEqual(SC.outcome_of(True, ["40001"], True), "anomaly")
        self.assertEqual(SC.outcome_of(False, ["40001"], True), "prevented_error")
        self.assertEqual(SC.outcome_of(False, [], True), "prevented_wait")
        self.assertEqual(SC.outcome_of(False, [], False), "no_anomaly")
        self.assertEqual(SC.judge("lost_update", "READ COMMITTED", "anomaly"), "as_expected")
        self.assertEqual(SC.judge("lost_update", "REPEATABLE READ", "anomaly"), "violation")
        self.assertEqual(SC.judge("write_skew", "REPEATABLE READ", "prevented_error"), "differs_no_violation")
        self.assertEqual(SC.judge("deadlock", "READ COMMITTED", "not_applicable"), "not_applicable")
        self.assertEqual(SC.judge("deadlock", "READ COMMITTED", "inconclusive"), "inconclusive")


import asyncio  # noqa: E402
import psycopg  # noqa: E402
import load as L  # noqa: E402


class _Cur:
    def __init__(self, row=None, rowcount=1):
        self.row, self.rowcount = row, rowcount

    async def fetchone(self):
        return self.row


class _FakeDB:
    """commit_errors: exceptions (or None for success) raised by successive COMMITs.
    landed: an erroring COMMIT still persisted (ambiguous commit that actually happened)."""

    def __init__(self, commit_errors=(), landed=False, stock_ok=True, reconnect_ok=True):
        self.commit_errors, self.landed = list(commit_errors), landed
        self.stock_ok, self.reconnect_ok = stock_ok, reconnect_ok
        self.receipt, self.connects = False, 0

    async def connect(self):
        self.connects += 1
        if self.connects > 1 and not self.reconnect_ok:
            raise psycopg.OperationalError("down")
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, params=None):
        if sql == "COMMIT":
            exc = self.db.commit_errors.pop(0) if self.db.commit_errors else None
            if exc is not None:
                if self.db.landed:
                    self.db.receipt = True
                raise exc
            self.db.receipt = True
        if sql == W.SEL_RECEIPT:
            return _Cur(("x",) if self.db.receipt else None)
        if sql == W.DEC_STOCK:
            return _Cur(None, 1 if self.db.stock_ok else 0)
        if sql == W.SEL_BALANCE:
            return _Cur((W.INITIAL_BALANCE,))
        return _Cur()

    async def close(self):
        pass


def _run(db, policy="retry3", kind="order"):
    op = (W.Op("order", "c:0:0", 0, "c", items=((1, 1),)) if kind == "order"
          else W.Op("transfer", "c:0:0", 0, "c", transfer=(1, 2, 5)))

    async def go():
        h = L.ConnHolder(db.connect)
        await h.open()
        return await L.run_op(h, op, "REPEATABLE READ", R.POLICIES[policy], random.Random(0))
    return asyncio.run(go())


class LoadPlan(unittest.TestCase):
    def test_matrix_sizes_and_ids(self):
        d1, r1 = L.plan_cells("D1"), L.plan_cells("R1")
        self.assertEqual(len(d1), 36)
        self.assertEqual(len(r1), 54)
        self.assertEqual(len({c.cell_id for c in r1}), 54)
        self.assertTrue(all(c.isolation == "REPEATABLE READ" for c in d1))
        rc = [c for c in r1 if c.isolation == "READ COMMITTED"]
        self.assertEqual(len(rc), 18)
        self.assertTrue(all(c.retry == "retry3" for c in rc))
        self.assertEqual([c.cell_id for c in L.plan_cells("R1")], [c.cell_id for c in r1])  # deterministic
        order = lambda rep: [c.cell_id.rsplit("-r", 1)[0] for c in r1 if c.rep == rep]  # noqa: E731
        self.assertEqual(sorted(order(1)), sorted(order(2)))   # same conditions every repetition ...
        self.assertNotEqual(order(1), order(2))                # ... in a different order
        self.assertEqual(L.Cell("R1", "READ COMMITTED", "hot", 256, "retry3", 2).cell_id, "R1-RC-hot-c256-retry3-r2")

    def test_connection_gate(self):
        self.assertIsNone(L.connection_gate(None, 256))
        self.assertIsNone(L.connection_gate(400, 256))
        self.assertIn("max_connections", L.connection_gate(189, 256))


class RunOp(unittest.TestCase):
    def test_conflict_then_commit(self):
        r = _run(_FakeDB([psycopg.errors.SerializationFailure("x")]))
        self.assertEqual((r.outcome, r.attempts, r.errors), ("committed", 2, ["40001"]))

    def test_no_retry_fails(self):
        r = _run(_FakeDB([psycopg.errors.SerializationFailure("x")]), policy="none")
        self.assertEqual((r.outcome, r.attempts, r.reason), ("failed", 1, "serialization"))

    def test_ambiguous_commit_that_landed(self):
        db = _FakeDB([psycopg.OperationalError("lost")], landed=True)
        r = _run(db)
        self.assertEqual((r.outcome, r.resolved, r.ambiguous, r.attempts), ("committed", True, False, 1))
        self.assertEqual(db.connects, 2)

    def test_ambiguous_commit_that_did_not_land_is_retried(self):
        r = _run(_FakeDB([psycopg.OperationalError("lost")], landed=False))
        self.assertEqual((r.outcome, r.attempts), ("committed", 2))

    def test_ambiguous_unresolved(self):
        r = _run(_FakeDB([psycopg.OperationalError("lost")], reconnect_ok=False))
        self.assertEqual((r.outcome, r.ambiguous), ("failed", True))

    def test_business_reject(self):
        self.assertEqual(_run(_FakeDB(stock_ok=False)).outcome, "rejected_stock")
        self.assertEqual(_run(_FakeDB(), kind="transfer").outcome, "committed")


class StatsAndMetrics(unittest.TestCase):
    def test_measure_window_and_ledger(self):
        s = L.Stats()
        s.record("order", L.Result("committed", 1), 5.0, in_measure=False)
        s.record("order", L.Result("committed", 2, ["40001"]), 10.0, in_measure=True)
        s.record("transfer", L.Result("failed", 3, ["40P01", "40P01", "40001"], reason="deadlock"), 30.0, True)
        s.record("order", L.Result("failed", 1, ["conn"], ambiguous=True, reason="unresolved"), 2.0, True)
        t = L.Stats.from_dict(s.to_dict())
        t.merge(L.Stats())
        self.assertEqual(t.ledger["order"], {"committed": 2, "ambiguous": 1})
        self.assertEqual((t.ops_total, t.attempts_total), (4, 7))
        m = L.metrics(t.to_dict(), measure_s=10)
        self.assertEqual((m["ops"], m["committed"]), (3, 1))
        self.assertAlmostEqual(m["success_tps"], 0.1)
        self.assertAlmostEqual(m["final_failure_rate"], 2 / 3)
        self.assertAlmostEqual(m["raw_conflict_rate"], 4 / 6)
        self.assertEqual(m["max_ms"], 30.0)
        self.assertIn("transfer", m["per_kind"])


import conn as CN  # noqa: E402
import runner as RN  # noqa: E402


class ConnAndRunner(unittest.TestCase):
    def test_kwargs(self):
        dsql = {"kind": "dsql", "host": "h.dsql", "dbname": "postgres", "user": "admin", "region": S.REGION,
                "sslrootcert": "/etc/pki/tls/certs/ca-bundle.crt"}
        kw = CN.conn_kwargs(dsql, "admin", "tok")
        self.assertEqual((kw["sslmode"], kw["autocommit"], kw["port"]), ("verify-full", True, 5432))
        self.assertEqual(kw["sslrootcert"], "/etc/pki/tls/certs/ca-bundle.crt")
        self.assertEqual(CN.conn_kwargs({"kind": "dsn", "dsn": "postgresql://x"}, None, None),
                         {"conninfo": "postgresql://x", "autocommit": True})
        with self.assertRaises(ValueError):
            CN.conn_kwargs({"kind": "mysql"}, None, None)

    def test_redact(self):
        t = {"kind": "pg", "host": "db.internal", "secret_arn": "arn:aws:secretsmanager:x:1:secret:rds!abc"}
        self.assertEqual(CN.redact("fail db.internal arn:aws:secretsmanager:x:1:secret:rds!abc", t),
                         "fail <redacted> <redacted>")

    def test_cpu_busy(self):
        self.assertIsNone(RN.cpu_busy_pct(None, (1, 2)))
        self.assertAlmostEqual(RN.cpu_busy_pct((100, 1000), (150, 2000)), 95.0)


import io  # noqa: E402
import tarfile  # noqa: E402
import remote as RM  # noqa: E402


class Remote(unittest.TestCase):
    def test_split_assemble(self):
        s = "abc" * 10001
        parts = RM.split(s, 7000)
        self.assertTrue(all(len(p) <= 7000 for p in parts))
        self.assertEqual(RM.assemble(parts, len(s)), s)
        with self.assertRaises(RuntimeError):
            RM.assemble(parts[:-1], len(s))

    def test_bundle_contents(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        names = tarfile.open(fileobj=io.BytesIO(RM.make_bundle(root)), mode="r:gz").getnames()
        self.assertEqual(sorted(names), sorted(RM.BUNDLE_FILES))
        self.assertFalse(any("artifacts" in n or "test" in n for n in names))

    def test_runner_cmd_quotes_json(self):
        cmd = RM.runner_cmd("cell", "R1", "--cell-json " + RM.shlex.quote('{"a": "b c"}') + " --out /x.json")
        self.assertIn("/opt/e004/venv/bin/python runner.py cell --target /opt/e004/target-R1.json", cmd)
        self.assertIn("'{\"a\": \"b c\"}'", cmd)


import infra as IN  # noqa: E402


class InfraPure(unittest.TestCase):
    def test_policies(self):
        self.assertEqual(IN.trust_policy()["Statement"][0]["Principal"], {"Service": "ec2.amazonaws.com"})
        self.assertIsNone(IN.runner_policy([], []))
        doc = IN.runner_policy(["arn:dsql:b", "arn:dsql:a"], ["arn:secret:x"])
        self.assertEqual(doc["Statement"][0], {"Effect": "Allow", "Action": ["dsql:DbConnectAdmin"],
                                               "Resource": ["arn:dsql:a", "arn:dsql:b"]})
        self.assertEqual(doc["Statement"][1]["Action"], ["secretsmanager:GetSecretValue"])
        self.assertNotIn('"*"', __import__("json").dumps(doc))

    def test_launch_params(self):
        tags = S.resource_tags(PFX, S.BATCH, "2026-09-25T20:00:00Z")
        spot = IN.launch_params("ami-1", "c7g.2xlarge", "subnet-1", "sg-1", "prof", tags, spot=True)
        self.assertEqual(spot["InstanceMarketOptions"]["SpotOptions"],
                         {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"})
        self.assertNotIn("KeyName", spot)
        self.assertEqual(spot["MetadataOptions"]["HttpTokens"], "required")
        self.assertTrue(spot["NetworkInterfaces"][0]["AssociatePublicIpAddress"])
        rtypes = {t["ResourceType"] for t in spot["TagSpecifications"]}
        self.assertIn("spot-instances-request", rtypes)
        od = IN.launch_params("ami-1", "c7g.2xlarge", "subnet-1", "sg-1", "prof", tags, spot=False)
        self.assertNotIn("InstanceMarketOptions", od)
        self.assertNotIn("spot-instances-request", {t["ResourceType"] for t in od["TagSpecifications"]})

    def test_choose_zones(self):
        z = IN.choose_zones([{"a", "b", "c"}, {"a", "b", "c", "d"}], {"a": 0.12, "b": 0.07, "c": 0.09, "d": 0.01})
        self.assertEqual((z["zones"], z["runner_az"], z["spot_usd_per_h"]), (["b", "c"], "b", 0.07))
        with self.assertRaises(S.SafetyError):
            IN.choose_zones([{"a"}, {"a", "b"}], {"a": 0.1, "b": 0.1})

    def test_secret_owned(self):
        ok = {"OwningService": "rds", "Tags": [{"Key": "aws:rds:primaryDBClusterArn",
                                               "Value": f"arn:aws:rds:x:1:cluster:{PFX}-a1"}]}
        self.assertTrue(IN.secret_owned(ok, PFX))
        self.assertFalse(IN.secret_owned({**ok, "OwningService": "other"}, PFX))
        self.assertFalse(IN.secret_owned({"OwningService": "rds", "Tags": []}, PFX))


import e004 as E  # noqa: E402


def _res(cfg, iso, dist, c, retry_, rep, tps, p99, conflict=0.1, status="ok", violations=()):
    return {"cell": {"config": cfg, "isolation": iso, "dist": dist, "concurrency": c, "retry": retry_,
                     "rep": rep, "warmup_s": 30, "measure_s": 60},
            "status": status, "monitor": {"lock_waiters_max": 3},
            "metrics": {"success_tps": tps, "p50_ms": p99 / 4, "p95_ms": p99 / 2, "p99_ms": p99,
                        "raw_conflict_rate": conflict, "final_failure_rate": 0.0},
            "invariants": {"violations": list(violations)}}


class Orchestration(unittest.TestCase):
    def test_drive_skips_done_and_stops_on_guard(self):
        cells = L.plan_cells("D1")[:4]
        ran = []
        E.drive(cells, {cells[0].cell_id}, lambda c: {"ok": True}, lambda c: ran.append(c.cell_id))
        self.assertEqual(ran, [c.cell_id for c in cells[1:]])
        ran.clear()
        with self.assertRaises(E.BudgetStop):
            E.drive(cells, set(), lambda c: {"ok": c is not cells[2]}, lambda c: ran.append(c.cell_id))
        self.assertEqual(ran, [cells[0].cell_id, cells[1].cell_id])   # nothing runs at or after the stop

    def test_pilot_estimate_uses_max_rate_per_concurrency(self):
        pilot = [{"cell": {"concurrency": 16, "warmup_s": 5, "measure_s": 20}, "stats": {"attempts_total": 250}},
                 {"cell": {"concurrency": 16, "warmup_s": 5, "measure_s": 20}, "stats": {"attempts_total": 500}},
                 {"cell": {"concurrency": 64, "warmup_s": 5, "measure_s": 20}, "stats": {"attempts_total": 1000}}]
        planned = [L.Cell("D1", "REPEATABLE READ", "hot", 16, "none", 1),
                   L.Cell("D1", "REPEATABLE READ", "hot", 64, "none", 1)]
        est = E.pilot_estimate(pilot, dpu_total=17500, planned_cells=planned, usd_per_million=10.0)
        self.assertAlmostEqual(est["dpu_per_attempt"], 10.0)
        self.assertEqual(est["attempt_rate_by_concurrency"], {"16": 20.0, "64": 40.0})
        self.assertAlmostEqual(est["est_attempts"], (20 + 40) * 90 * E.PILOT_SAFETY)
        self.assertAlmostEqual(est["est_usd"], est["est_dpu"] / 1e6 * 10.0)
        self.assertAlmostEqual(E.pilot_cell_dpu(est, planned[1]), 40 * 90 * E.PILOT_SAFETY * 10.0)

    def test_aggregate_and_ratios(self):
        rows = E.aggregate([
            _res("D1", "REPEATABLE READ", "hot", 64, "retry3", r, tps, 100) for r, tps in ((1, 90), (2, 100), (3, 110))
        ] + [_res("R1", "REPEATABLE READ", "hot", 64, "retry3", r, 200, 50) for r in (1, 2, 3)]
          + [_res("R1", "READ COMMITTED", "hot", 64, "retry3", 1, 1, 1, status="not_applicable")])
        d1 = next(r for r in rows if r["config"] == "D1")
        self.assertEqual(d1["success_tps"], {"median": 100, "min": 90, "max": 110, "n": 3})
        self.assertTrue(d1["spread_over_10pct"])
        na = next(r for r in rows if r["isolation"] == "READ COMMITTED")
        self.assertEqual((na["ok_reps"], na["success_tps"]), (0, None))
        rat = E.ratios(rows)
        self.assertEqual(len(rat), 1)
        self.assertAlmostEqual(rat[0]["tps_ratio_d1_over_control"], 0.5)
        self.assertAlmostEqual(rat[0]["p99_ratio_d1_over_control"], 2.0)

class ReviewFixes(unittest.TestCase):
    def test_ssm_run_aborts_when_runner_check_fails(self):
        class Lost(Exception):
            pass

        class FakeSSM:
            class exceptions:
                InvocationDoesNotExist = KeyError

            def send_command(self, **kw):
                return {"Command": {"CommandId": "c1"}}

            def get_command_invocation(self, **kw):
                return {"Status": "InProgress"}

        def check():
            raise Lost()
        t0 = time.monotonic()
        with self.assertRaises(Lost):
            RM.ssm_run(FakeSSM(), "i-1", ["true"], timeout_s=900, check=check, poll_s=0.01, check_every_s=0)
        self.assertLess(time.monotonic() - t0, 5)

    def test_failures_become_runner_lost_when_runner_is_gone(self):
        def boom():
            raise TimeoutError("ssm")

        def dead():
            raise E.RunnerLost("gone")
        with self.assertRaises(E.RunnerLost):
            E.with_runner_check(boom, dead)
        with self.assertRaises(TimeoutError):
            E.with_runner_check(boom, lambda: "i-1")
        self.assertEqual(E.with_runner_check(lambda: 5, dead), 5)

    def test_cycle_refuses_cleaned_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.data["config_status"]["R1"] = "cleaned"
            with self.assertRaises(S.SafetyError):
                E.assert_config_runnable(m, "R1")
            m.data["config_status"]["R1"] = "cleanup_failed"
            with self.assertRaises(S.SafetyError):
                E.assert_config_runnable(m, "R1")
            m.data["config_status"]["A1"] = "provisioned"
            E.assert_config_runnable(m, "A1")

    def test_dpu_price_falls_back_to_pilot_and_is_required_for_d1(self):
        args = type("A", (), {"dsql_usd_per_million_dpu": None})()
        self.assertEqual(E.dpu_price(args, "D1", {"usd_per_million_dpu": 7.5}), 7.5)
        self.assertEqual(E.dpu_price(args, "R1", None), 0.0)
        with self.assertRaises(S.SafetyError):
            E.dpu_price(args, "D1", None)
        args.dsql_usd_per_million_dpu = 8.0
        self.assertEqual(E.dpu_price(args, "D1", {"usd_per_million_dpu": 7.5}), 8.0)

    def test_rds_secret_state(self):
        self.assertEqual(IN.secret_state(None), "gone")
        self.assertEqual(IN.secret_state({"DeletedDate": "x", "OwningService": "rds"}), "pending")
        self.assertEqual(IN.secret_state({"OwningService": "rds"}), "live")

    def test_cell_records_connect_failure_instead_of_crashing(self):
        def bad_factory(target):
            raise psycopg.OperationalError("throttled")
        orig = RN.C.sync_connect_factory
        RN.C.sync_connect_factory = bad_factory
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "r.json")
                cell = L.Cell("R1", "REPEATABLE READ", "hot", 16, "none", 1)
                res = RN.cmd_cell({"kind": "pg", "host": "h"}, json.dumps(L.asdict(cell)), out)
                self.assertEqual(res["status"], "error")
                self.assertTrue(os.path.exists(out))
        finally:
            RN.C.sync_connect_factory = orig

    def test_retry_once(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return "ok"
        self.assertEqual(E.retry_once(flaky, lambda exc: None), "ok")

        def lost():
            raise E.RunnerLost("x")
        with self.assertRaises(E.RunnerLost):
            E.retry_once(lost, lambda exc: None)

    def test_lifetime_check_uses_config_estimate(self):
        need = E.config_minutes("A2", 3, 30, 60)
        self.assertGreater(need, 150)
        self.assertIsNone(E.lifetime_problem(need + 31, "A2", 3, 30, 60))
        self.assertIn("lifetime", E.lifetime_problem(need, "A2", 3, 30, 60))
        self.assertGreaterEqual(S.MAX_LIFETIME_MIN, 900)

    def test_retired_runner_stops_accruing(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource(S.BATCH, "ec2_instance", "i-old", state="created", rate_usd_per_h=0.1)
            E.retire_runner(m, "i-old")
            self.assertEqual(C.active_rate(m.data), 0.0)
            self.assertEqual(m.find(S.BATCH, "ec2_instance", "i-old")["state"], "deleted")

    def test_late_landing_commit_is_not_a_failure(self):
        class LateDB(_FakeDB):
            def __init__(self):
                super().__init__([psycopg.OperationalError("lost")], landed=False)
                self.attempt_receipt_inserts = 0

        db = LateDB()
        orig = _FakeConn.execute

        async def execute(self, sql, params=None):
            if sql == W.INS_RECEIPT:
                self.db.attempt_receipt_inserts += 1
                if self.db.attempt_receipt_inserts == 2:   # the first COMMIT landed late, after the lookup
                    raise psycopg.errors.UniqueViolation("dup")
            return await orig(self, sql, params)
        _FakeConn.execute = execute
        try:
            r = _run(db)
        finally:
            _FakeConn.execute = orig
        self.assertEqual((r.outcome, r.resolved), ("committed", True))

    def test_dsql_ignores_show_max_connections(self):
        self.assertIsNone(RN.gate_max_connections({"kind": "dsql"}, 100))
        self.assertEqual(RN.gate_max_connections({"kind": "pg"}, 100), 100)

class PilotFixes(unittest.TestCase):
    def test_non_preventing_error_is_inconclusive(self):
        tx = lambda err: type("T", (), {"error": err, "waited_ms": 0.0, "committed": False})()  # noqa: E731
        r = SC._result("lost_update", "REPEATABLE READ", False, [tx("42P01"), tx("42P01")], None)
        self.assertEqual((r["outcome"], r["judgement"]), ("inconclusive", "inconclusive"))
        r = SC._result("lost_update", "REPEATABLE READ", False, [tx(None), tx("40001")], 9)
        self.assertEqual((r["outcome"], r["judgement"]), ("prevented_error", "as_expected"))

    def test_tables_exist_before_transactions_begin(self):
        self.assertEqual(set(SC.SETUP), set(SC.SCENARIOS))
        import inspect
        for name, fn in SC._FLOWS.items():
            self.assertNotIn("CREATE TABLE", inspect.getsource(fn), name)

    def test_spot_price_for_az(self):
        hist = [{"AvailabilityZone": "b", "SpotPrice": "0.2"}, {"AvailabilityZone": "a", "SpotPrice": "0.1"},
                {"AvailabilityZone": "b", "SpotPrice": "0.3"}]
        self.assertEqual(IN.spot_price_for_az(hist, "b"), 0.2)   # newest first
        with self.assertRaises(S.SafetyError):
            IN.spot_price_for_az(hist, "c")

class RunnerSwapFixes(unittest.TestCase):
    def test_spot_price_query_matches_the_ec2_api_model(self):
        import botocore.session
        members = botocore.session.get_session().get_service_model("ec2").operation_model(
            "DescribeSpotPriceHistory").input_shape.members
        q = IN.spot_price_query("c7g.4xlarge", "ap-northeast-2d")
        self.assertTrue(set(q) <= set(members), set(q) - set(members))
        self.assertEqual(q["AvailabilityZone"], "ap-northeast-2d")

    def test_runner_is_recorded_before_any_post_launch_call(self):
        import inspect
        src = inspect.getsource(IN.launch_runner)
        self.assertLess(src.index('m.add_resource(S.BATCH, "ec2_instance"'), src.index("describe_spot_price_history"))

class ManifestUpdates(unittest.TestCase):
    def test_updating_extra_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = _manifest(tmp)
            m.add_resource(S.BATCH, "ec2_instance", "i-1", state="created", rate_usd_per_h=0.1)
            m.add_resource(S.BATCH, "ec2_instance", "i-1", rate_usd_per_h=0.2)
            r = m.find(S.BATCH, "ec2_instance", "i-1")
            self.assertEqual((r["state"], r["extra"]["rate_usd_per_h"]), ("created", 0.2))
            m.add_resource(S.BATCH, "vpc", "v-1")
            self.assertEqual(m.find(S.BATCH, "vpc", "v-1")["state"], "requested")

class SummaryEdgeCases(unittest.TestCase):
    def test_zero_control_tps_gives_no_ratio_instead_of_crashing(self):
        rows = E.aggregate([_res("D1", "REPEATABLE READ", "hot", 256, "none", 1, 100, 50),
                            _res("A2", "REPEATABLE READ", "hot", 256, "none", 1, 0, 2000)])
        rat = E.ratios(rows)
        self.assertEqual(len(rat), 1)
        self.assertIsNone(rat[0]["tps_ratio_d1_over_control"])
        self.assertAlmostEqual(rat[0]["p99_ratio_d1_over_control"], 0.025)


if __name__ == "__main__":
    unittest.main()
