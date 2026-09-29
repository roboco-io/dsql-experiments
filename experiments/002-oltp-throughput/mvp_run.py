"""Operator for the MVP probes (2026-09-29 decision: finish the remaining experiments as MVPs, total USD 50).

  run-a  --prefix P            E003, E008, E012 on the loaded DSQL-only run P (after `dsql-ramp`)
  run-b  --prefix P            E005, E006, E009 on a small-data run P with D1 and A2 (fraction 0.02)

Probes run on each config's runner through `runner.py mvp` (mvp_probes.py); results are saved under
artifacts/<prefix>/mvp/. Cleanup stays with `e002.py batch-down` + `verify`.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

import cost
import e002 as E
import infra as IN
import remote
import safety as S
from infra import log

HERE = os.path.dirname(os.path.abspath(__file__))


def _args(prefix, account_id, profile):
    return argparse.Namespace(account_id=account_id, profile=profile, region=S.REGION, prefix=prefix)


def _guard_ok(sess, m, cap, next_usd):
    E.refresh_measured(sess, m)
    spent = cost.spent_usd(m.data)
    ok = spent + next_usd + cost.active_rate(m.data) * cost.CLEANUP_HOURS <= cap
    log(f"guard: spent {spent:.2f} + next {next_usd:.2f} (cap {cap}) -> {'ok' if ok else 'STOP'}")
    return ok


def probe(sess, m, cfg, name, fraction, timeout_s, **kw):
    extra = f"--probe {name} --fraction {fraction} --probe-json {shlex.quote(json.dumps(kw))}"
    res = E.runner_json(sess, m, cfg, "mvp", extra, timeout_s, f"mvp-{name}-{cfg}")
    path = os.path.join(E.run_dir(m.prefix), "mvp", f"{name}-{cfg}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    S.write_private(path, res)
    log(f"{name} {cfg}: saved")
    return res


def push_code(sess, m, cfgs):
    for cfg in cfgs:
        remote.push_bundle(sess.client("ssm"), E.runner_alive(sess, m, cfg), HERE)


def run_a(sess, m, cap):
    """DSQL-only: connection behaviour, operations under load, large reads (S data, fraction 1.0)."""
    push_code(sess, m, ["D1"])
    plan = [("e003", 1.0, 1800, {}), ("e008", 3.0, 3600, {}), ("e012", 3.0, 5400, {})]
    for name, usd, timeout, kw in plan:
        if not _guard_ok(sess, m, cap, usd):
            return 1
        probe(sess, m, "D1", name, 1.0, timeout, **kw)
    return 0


def _a2_autopause(sess, m, on: bool):
    cid = m.data["connections"]["A2"]["cluster_id"]
    cfg = ({"MinCapacity": 0.0, "MaxCapacity": float(IN.A2_ACU[1]), "SecondsUntilAutoPause": 300} if on
           else {"MinCapacity": float(IN.A2_ACU[0]), "MaxCapacity": float(IN.A2_ACU[1])})
    sess.client("rds").modify_db_cluster(DBClusterIdentifier=cid, ServerlessV2ScalingConfiguration=cfg,
                                         ApplyImmediately=True)
    m.event("A2", "deviation", note=f"E009 idle branch: auto-pause {'on' if on else 'off'} {cfg}")
    log(f"A2 scaling set to {cfg}")


def run_b(sess, m, cap, fraction=0.02):
    """D1 and A2 on small data: visibility, connection loss, spike and first request after idle."""
    cfgs = ["D1", "A2"]
    push_code(sess, m, cfgs)
    for name, usd, timeout, kw in (("e005", 1.0, 1800, {}), ("e006", 1.5, 1800, {}),
                                   ("e009-spike", 2.5, 2400, {})):
        if not _guard_ok(sess, m, cap, usd):
            return 1
        out = E.parallel(cfgs, lambda cfg, n=name, t=timeout, k=kw: probe(sess, m, cfg, n, fraction, t, **k))
        if E._print_outcomes(name, out):
            return 1
    if not _guard_ok(sess, m, cap, 1.5):
        return 1
    _a2_autopause(sess, m, True)
    try:
        out = E.parallel(cfgs, lambda cfg: probe(sess, m, cfg, "e009-idle", fraction, 3600))
        E._print_outcomes("e009-idle", out)
    finally:
        _a2_autopause(sess, m, False)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=("run-a", "run-b"))
    p.add_argument("--account-id", required=True)
    p.add_argument("--profile", default=S.PROFILE)
    p.add_argument("--prefix", required=True)
    p.add_argument("--cap", type=float, required=True, help="guard for this run (USD, estimate-based)")
    a = p.parse_args(argv)
    args = _args(a.prefix, a.account_id, a.profile)
    sess, _ = E.session(args)
    m = E.load_manifest(args)
    return run_a(sess, m, a.cap) if a.phase == "run-a" else run_b(sess, m, a.cap)


if __name__ == "__main__":
    sys.exit(main())
