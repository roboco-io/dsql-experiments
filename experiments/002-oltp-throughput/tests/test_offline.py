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
import safety as S  # noqa: E402

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
