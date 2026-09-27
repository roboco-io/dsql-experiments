#!/usr/bin/env python3
"""E002 operator CLI: parallel phases over D1/R1/A1/A2, each config with its own Spot runner.

Order (see README "재현 절차"):
  init → discover → batch-up --configs D1,A2 → pilot (decision point) →
  batch-up --configs D1,R1,A1,A2 → load --configs R1,A1,A2 → explore → measure → summarize → batch-down → verify
Every AWS command requires --account-id (checked against STS). A phase runs its configs in parallel; one
config's failure (including a lost Spot runner, exit 3) never stops the others. Any failure or interruption:
run `batch-down` then `verify`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import timedelta

import cost
import infra as IN
import openloop as OL
import remote
import safety as S
import slo
from infra import log

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, "artifacts")
EXPLORE_CONCURRENCY = (16, 64, 256)
PILOT_FRACTION = 0.02
PILOT_RATES = (100.0, 400.0, 1600.0)
PILOT_WARMUP_S, PILOT_MEASURE_S = 60, 120
CELL_OVERHEAD_S = 120          # connections + invariants + reset + SSM round trips (estimate, re-measured)
SAFETY = 1.25                  # headroom on pilot-based estimates
LOAD_WORKERS = 32
CW_LAG_S = 240                 # wait before reading CloudWatch for a window that just ended


class BudgetStop(RuntimeError):
    pass


class RunnerLost(RuntimeError):
    pass


# ---------------------------------------------------------------- pure ----

def parallel(configs, fn) -> dict:
    out = {}
    if not configs:
        return out
    with ThreadPoolExecutor(len(configs)) as ex:
        futs = {cfg: ex.submit(fn, cfg) for cfg in configs}
        for cfg, f in futs.items():
            try:
                out[cfg] = {"ok": True, "value": f.result()}
            except Exception as exc:  # noqa: BLE001 - reported per config; others keep their results
                out[cfg] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400],
                            "runner_lost": isinstance(exc, RunnerLost), "budget": isinstance(exc, BudgetStop)}
    return out


def load_estimate(slice_dpu, fraction, usd_per_million) -> float:
    return cost.dpu_cost_usd(slice_dpu / fraction, usd_per_million)


def explore_cells(cfg, reps, warmup_s, measure_s):
    return [OL.Cell(f"{cfg}-x-c{c}-r{r:02d}", cfg, "closed", 0.0, c, r, warmup_s, measure_s)
            for r in range(1, reps + 1) for c in EXPLORE_CONCURRENCY]


def main_cells(cfg, rates, rep, warmup_s, measure_s, seed):
    order = list(rates)
    random.Random(f"{seed}:{rep}").shuffle(order)
    return [OL.Cell(f"{cfg}-m-{rate:.1f}-r{rep:02d}", cfg, "open", rate, 0, rep, warmup_s, measure_s)
            for rate in order]


def boundary_loop(cfg, qref, run_rate, stop_fn, max_steps=8) -> dict:
    """Entered after the main rates passed. run_rate returns a cell result, or None when it could not run
    (budget stop): the search then ends and the config's Q is reported as a lower bound."""
    passed, failed, results, stopped = list(slo.main_rates(qref)), [], [], False
    for _ in range(max_steps):
        if stop_fn():
            stopped = True
            break
        rate = slo.next_rate(passed, failed, qref, False)
        if rate is None:
            break
        r = run_rate(rate)
        if r is None:
            stopped = True
            break
        results.append(r)
        v = slo.judge(r)["verdict"]
        if v == "pass":
            passed.append(rate)
        elif v == "fail":
            failed.append(rate)
        else:
            stopped = True           # invalid (e.g. generator saturated): stop, report a lower bound
            break
    return {"results": results, "stopped_early": stopped or not failed}


def _acu_at(acu_by_rate: dict, rate: float) -> float:
    """Writer ACU at `rate`: nearest measured pilot rate, scaled linearly above the highest one."""
    pts = sorted((float(k), v) for k, v in acu_by_rate.items())
    k, v = min(pts, key=lambda p: abs(p[0] - rate))
    return v * max(1.0, rate / k)


def pilot_plan(pilot, reps, warmup_s, measure_s, rates) -> dict:
    secs = (warmup_s + measure_s) * reps
    d1 = sum(cost.dpu_cost_usd(rate * secs * pilot["d1_dpu_per_attempt"], cost.DSQL_USD_PER_MILLION_DPU)
             for rate in rates)
    # writer + tier-1 reader, the reader follows the writer's capacity
    a2 = sum(_acu_at(pilot["a2_acu_by_rate"], rate) * 2 * cost.ACU_USD_PER_H * secs / 3600 for rate in rates)
    hours = (secs + CELL_OVERHEAD_S * reps) * len(rates) / 3600
    fixed = pilot["fixed_usd_per_h"] * hours
    return {"d1_usd": d1, "a2_usd": a2, "fixed_usd": fixed, "hours": hours, "total_usd": (d1 + a2 + fixed) * SAFETY}


def config_rate(data, cfg) -> float:
    return sum(r["extra"].get("rate_usd_per_h", 0.0) for r in data["resources"]
               if r["config"] == cfg and r["state"] != "deleted")


def cell_estimate_usd(data, cell, pilot, seen_attempt_tps) -> float:
    """Guard booking for one cell: the config's resource rates for the cell's duration, plus D1 DPU from the
    pilot's DPU per attempt (open loop: 1.1 x rate; closed loop: 2 x the highest attempt TPS seen so far)."""
    dur = cell.warmup_s + cell.measure_s + CELL_OVERHEAD_S
    usd = config_rate(data, cell.config) * dur / 3600
    if cell.config == "D1" and pilot:
        tps = cell.rate * 1.1 if cell.mode == "open" else max(seen_attempt_tps * 2, PILOT_RATES[-1])
        usd += cost.dpu_cost_usd(tps * dur * pilot["d1_dpu_per_attempt"], cost.DSQL_USD_PER_MILLION_DPU)
    return usd


def q_ratios(qs: dict) -> dict:
    """Throughput ratio Q_D1 / Q_B per control; None with a reason when a side is missing."""
    d1 = qs.get("D1") or {}
    out = {}
    for cfg, q in qs.items():
        if cfg == "D1":
            continue
        if not d1.get("q") or not q.get("q"):
            out[cfg] = {"throughput_ratio": None, "note": f"D1 {d1.get('status')} / {cfg} {q.get('status')}"}
            continue
        notes = [f"{n} {x['status']}" for n, x in (("D1", d1), (cfg, q)) if x["status"] != "confirmed"]
        out[cfg] = {"throughput_ratio": round(d1["q"] / q["q"], 3), "note": ", ".join(notes) or None}
    return out


# --------------------------------------------------------------- AWS side --

def run_dir(prefix):
    return os.path.join(ART, S.validate_run_prefix(prefix))


def results_dir(prefix, cfg):
    d = os.path.join(run_dir(prefix), "results", cfg)
    os.makedirs(d, exist_ok=True)
    return d


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def session(args):
    import boto3
    S.validate_account_id(args.account_id)
    sess = boto3.Session(profile_name=args.profile, region_name=args.region)
    ident = sess.client("sts").get_caller_identity()
    S.assert_identity(args.account_id, ident)
    log(f"STS identity verified for confirmed account (region {args.region})")
    return sess, ident


def load_manifest(args):
    m = S.Manifest.load(os.path.join(run_dir(args.prefix), "manifest.json"))
    m.assert_scope(args.account_id, args.region)
    return m


def _steps(m, cfg):
    with m.lock:
        return m.data.setdefault("steps", {}).setdefault(cfg, [])


def _mark(m, cfg, step):
    with m.lock:
        if step not in _steps(m, cfg):
            _steps(m, cfg).append(step)
        m.save()


def _unmark(m, cfg, *steps):
    with m.lock:
        m.data.setdefault("steps", {})[cfg] = [s for s in _steps(m, cfg) if s not in steps]
        m.save()


def runner_alive(sess, m, cfg) -> str:
    iid = (m.data.get("runners") or {}).get(cfg)
    if not iid:
        raise RunnerLost(f"{cfg} has no runner; run `batch-up --configs {cfg}`")
    res = sess.client("ec2").describe_instances(InstanceIds=[iid])["Reservations"]
    inst = res[0]["Instances"][0] if res else None
    state = inst["State"]["Name"] if inst else "missing"
    if state != "running":
        m.event(cfg, "runner_lost", state=state, reason=(inst or {}).get("StateReason", {}).get("Message"))
        raise RunnerLost(f"{cfg} runner is {state}; run `replace-runner --config {cfg}` (or clean up)")
    return iid


def with_runner_check(fn, alive):
    """Run fn; if it fails, ask alive() first so a lost Spot runner surfaces as RunnerLost, not a plain error."""
    try:
        return fn()
    except RunnerLost:
        raise
    except Exception:
        alive()
        raise


def retry_once(fn, on_retry):
    try:
        return fn()
    except (RunnerLost, S.SafetyError, BudgetStop):
        raise
    except Exception as exc:  # noqa: BLE001 - one retry for transient runner/SSM failures
        on_retry(exc)
        return fn()


def _ssm_ok(sess, m, cfg, cmd, timeout_s, what):
    iid = runner_alive(sess, m, cfg)
    alive = lambda: runner_alive(sess, m, cfg)  # noqa: E731
    status, out, err = with_runner_check(
        lambda: remote.ssm_run(sess.client("ssm"), iid, [cmd], timeout_s=timeout_s, check=alive), alive)
    if status != "Success":
        alive()
        raise RuntimeError(f"{cfg} {what}: SSM {status}: {err[-300:]}")
    return iid, out


def _fetch(sess, m, cfg, iid, path):
    return with_runner_check(lambda: remote.fetch_file(sess.client("ssm"), iid, path),
                             lambda: runner_alive(sess, m, cfg))


def runner_json(sess, m, cfg, command, extra, timeout_s, what):
    out = f"{remote.REMOTE_ROOT}/results/{what}.json"
    iid, _ = _ssm_ok(sess, m, cfg, remote.runner_cmd(command, cfg, f"{extra} --out {out}"), timeout_s, what)
    return json.loads(_fetch(sess, m, cfg, iid, out))


def refresh_measured(sess, m):
    """Replace estimates with CloudWatch-measured costs: D1 DPU, A2 ACU (writer and reader), A1/A2 I/O."""
    until = S.utcnow() - timedelta(minutes=3)
    for r in [r for r in m.data["resources"] if r["state"] != "deleted"]:
        start = S.parse_iso(r["recorded_at"])
        if until <= start:
            continue
        try:
            if r["type"] == "dsql_cluster":
                usd = cost.dpu_cost_usd(IN.dsql_dpu(sess, r["id"], start, until), cost.DSQL_USD_PER_MILLION_DPU)
                with m.lock:
                    m.data["measured_usd"]["D1_dpu"] = max(usd, m.data["measured_usd"].get("D1_dpu", 0.0))
            elif r["config"] == "A2" and r["type"] == "db_instance":
                acu_h = IN.acu_hours(sess, r["id"], start, until)
                with m.lock:
                    r["extra"].update(measured_usd=acu_h * cost.ACU_USD_PER_H, measured_until=S.iso(until))
            elif r["type"] == "db_cluster":
                io = IN.aurora_io(sess, r["id"], start, until)
                with m.lock:
                    m.data["measured_usd"][f"{r['config']}_io"] = io / 1e6 * cost.AURORA_IO_USD_PER_MILLION
        except Exception as exc:  # noqa: BLE001 - keep the previous estimate; the guard stays conservative
            log(f"CloudWatch refresh skipped for {r['config']} {r['type']}: {type(exc).__name__}")
    m.save()


def run_cell_on(sess, m, guard, cfg, cell, pilot, seen_attempt_tps=0.0):
    """Book the cell with the shared guard, run it on the config's runner, save the result locally."""
    path = os.path.join(results_dir(m.prefix, cfg), f"{cell.cell_id}.json")
    done = _load_json(path)
    if done and done.get("status") == "ok":
        return done
    if m.expired():
        raise BudgetStop("absolute lifetime exceeded")
    g = guard.reserve(cfg, cell_estimate_usd(m.data, cell, pilot, seen_attempt_tps))
    m.event(cfg, "guard", cell=cell.cell_id, **g)
    if not g["ok"]:
        raise BudgetStop(f"guard stopped before {cell.cell_id}: projected {g['projected_usd']} > {g['cap_usd']}")
    try:
        timeout = int(cell.warmup_s + cell.measure_s + 1200)
        res = retry_once(lambda: runner_json(sess, m, cfg, "cell", f"--cell-json {shlex.quote(json.dumps(asdict(cell)))}",
                                             timeout, cell.cell_id),
                         lambda exc: log(f"{cell.cell_id}: retrying once after {type(exc).__name__}"))
    finally:
        guard.release(cfg)
    S.write_private(path, res)
    refresh_measured(sess, m)
    met = res.get("metrics") or {}
    log(f"{cell.cell_id}: {res['status']} {slo.judge(res)['verdict']} tps={met.get('success_tps')} "
        f"fail={met.get('technical_failure_rate')} spent={cost.spent_usd(m.data):.2f}")
    return res


def _pilot(m):
    return _load_json(os.path.join(run_dir(m.prefix), "pilot.json"))


def _print_outcomes(phase, out):
    lost = False
    for cfg, o in out.items():
        log(f"{phase} {cfg}: {'ok' if o['ok'] else o['error']}")
        lost = lost or (not o["ok"] and o.get("runner_lost"))
    if any(not o["ok"] for o in out.values()):
        log(f"{phase}: failures above. Fix and rerun the phase (finished work is skipped), or run batch-down then verify")
    return 3 if lost else (1 if any(not o["ok"] for o in out.values()) else 0)


# ------------------------------------------------------------- phases ------

def ensure_config(sess, m, cfg, args):
    """Provision the DB (unless live), launch and bootstrap a runner (unless alive), push the target, probe."""
    disc = m.data["discovery"]
    if "provision" not in _steps(m, cfg):
        IN.provision_db(sess, m, cfg, disc)
        _mark(m, cfg, "provision")
    iid = (m.data.get("runners") or {}).get(cfg)
    try:
        runner_alive(sess, m, cfg)
    except RunnerLost:
        if iid and m.find(cfg, "ec2_instance", iid) and m.find(cfg, "ec2_instance", iid)["state"] != "deleted":
            IN.terminate_runner(sess, m, cfg, iid)
        iid = IN.launch_runner(sess, m, disc, args.allow_on_demand, cfg, args.runner_type)
        IN.bootstrap_runner(sess, m, cfg, iid, HERE)
    IN.push_target(sess, m, cfg)
    probe = runner_json(sess, m, cfg, "probe", "", 300, f"probe-{cfg}")
    m.event(cfg, "probe", **probe)
    _mark(m, cfg, "ready")
    return probe


def do_batch_up(sess, m, args, cfgs):
    if m.data["config_status"].get(S.BATCH) != "ready":
        try:
            IN.create_batch(sess, m, m.data["discovery"])
        except Exception:
            log("batch-up failed: deleting BATCH resources")
            IN.cleanup(sess, m, S.BATCH)
            raise
    return _print_outcomes("batch-up", parallel(cfgs, lambda cfg: ensure_config(sess, m, cfg, args)))


def load_config(sess, m, cfg, fraction, guard=None, usd=0.0):
    """schema + load on the config's runner; the step name records the fraction."""
    step = f"load:{fraction}"
    if step in _steps(m, cfg):
        return m.data.get("loads", {}).get(f"{cfg}:{fraction}")
    if guard is not None:
        g = guard.reserve(cfg, usd)
        m.event(cfg, "guard", what=step, **g)
        if not g["ok"]:
            raise BudgetStop(f"guard refused {cfg} {step}: projected {g['projected_usd']} > {g['cap_usd']}")
    try:
        start = S.utcnow()
        schema = runner_json(sess, m, cfg, "schema", "", 3600, f"schema-{cfg}")
        loaded = runner_json(sess, m, cfg, "load", f"--fraction {fraction} --workers {LOAD_WORKERS}",
                             6 * 3600, f"load-{cfg}-{fraction}")
        info = {**loaded, "index_wait_s": schema.get("index_wait_s"), "window": [S.iso(start), S.iso(S.utcnow())]}
    finally:
        if guard is not None:
            guard.release(cfg)
    with m.lock:
        m.data.setdefault("loads", {})[f"{cfg}:{fraction}"] = info
    _mark(m, cfg, step)
    m.event(cfg, "load_done", **{k: info[k] for k in ("seconds", "method", "fraction", "window")})
    return info


def _dsql_cluster(m):
    return m.data["connections"]["D1"]["cluster_id"]


def _dpu_between(sess, m, start_iso, end_iso):
    wait = CW_LAG_S - (S.utcnow() - S.parse_iso(end_iso)).total_seconds()
    if wait > 0:
        time.sleep(wait)
    return IN.dsql_dpu(sess, _dsql_cluster(m), S.parse_iso(start_iso) - timedelta(minutes=1),
                       S.parse_iso(end_iso) + timedelta(minutes=2))


def full_load_d1(sess, m, guard, pilot):
    usd = load_estimate(pilot["load_slice_dpu"], PILOT_FRACTION, cost.DSQL_USD_PER_MILLION_DPU) * SAFETY
    return load_config(sess, m, "D1", 1.0, guard, usd)


def do_pilot(sess, m, args):
    for cfg in ("D1", "A2"):
        if "ready" not in _steps(m, cfg):
            raise S.SafetyError(f"run `batch-up --configs D1,A2` first ({cfg} not ready)")
    guard = cost.Guard(lambda: m.data, cap=args.budget_cap)
    pilot = _pilot(m) or {}
    if "load_slice_dpu" not in pilot:
        sl = load_config(sess, m, "D1", PILOT_FRACTION)
        pilot["load_slice_dpu"] = _dpu_between(sess, m, *sl["window"])
        pilot["load_slice"] = {k: sl[k] for k in ("rows", "seconds", "fraction")}
        S.write_private(os.path.join(run_dir(m.prefix), "pilot.json"), pilot)
    est = load_estimate(pilot["load_slice_dpu"], PILOT_FRACTION, cost.DSQL_USD_PER_MILLION_DPU)
    log(f"D1 full-load estimate from the {PILOT_FRACTION:.0%} slice: {pilot['load_slice_dpu']:.0f} DPU -> "
        f"about USD {est:.2f}")
    out = parallel(["D1", "A2"], lambda cfg: (full_load_d1(sess, m, guard, pilot) if cfg == "D1"
                                              else load_config(sess, m, cfg, 1.0)))
    if _print_outcomes("pilot-load", out):
        return 1
    refresh_measured(sess, m)

    def cells(cfg):
        got = []
        for rate in PILOT_RATES:
            cell = OL.Cell(f"{cfg}-p-{rate:.0f}", cfg, "open", rate, 0, 0, PILOT_WARMUP_S, PILOT_MEASURE_S)
            got.append(run_cell_on(sess, m, guard, cfg, cell, None))
        return got
    out = parallel(["D1", "A2"], cells)
    if _print_outcomes("pilot-cells", out):
        return 1
    per_attempt, acu = [], {}
    for res in out["D1"]["value"]:
        att = (res.get("metrics") or {}).get("attempt_tps", 0) * PILOT_MEASURE_S
        if res["status"] == "ok" and att:
            dpu = _dpu_between(sess, m, *res["window"])
            per_attempt.append(dpu / att)
    writer = m.data["connections"]["A2"]["writer"]
    for res in out["A2"]["value"]:
        if res["status"] == "ok":
            w0, w1 = res["window"]
            time.sleep(max(0, CW_LAG_S - (S.utcnow() - S.parse_iso(w1)).total_seconds()))
            acu_h = IN.acu_hours(sess, writer, S.parse_iso(w0), S.parse_iso(w1))
            acu[str(res["cell"]["rate"])] = acu_h * 3600 / PILOT_MEASURE_S
    if not per_attempt or not acu:
        raise RuntimeError("pilot produced no usable D1 DPU or A2 ACU measurement")
    fixed = cost.DB_RATE_USD_PER_H["R1"] + 2 * cost.DB_RATE_USD_PER_H["A1_instance"] + sum(
        r["extra"].get("rate_usd_per_h", 0.0) for r in m.data["resources"]
        if r["type"] == "ec2_instance" and r["state"] != "deleted") * 2
    pilot.update(d1_dpu_per_attempt=max(per_attempt), a2_acu_by_rate=acu, fixed_usd_per_h=fixed,
                 d1_full_load=m.data.get("loads", {}).get("D1:1.0"), measured_at=S.iso(S.utcnow()))
    S.write_private(os.path.join(run_dir(m.prefix), "pilot.json"), pilot)
    refresh_measured(sess, m)
    rates = [PILOT_RATES[-1] * f for f in (0.5, 1.0, 1.2)]       # placeholder Qref until explore measures it
    report = {"spent_usd_now": round(cost.spent_usd(m.data), 3), "d1_dpu_per_attempt": pilot["d1_dpu_per_attempt"],
              "a2_writer_acu_by_rate": acu,
              "plans_at_qref_1600": {f"{reps}x{w}+{s}": pilot_plan(pilot, reps, w, s, rates)
                                     for reps, w, s in ((3, 180, 600), (1, 180, 600), (3, 120, 300))}}
    print(json.dumps(report, indent=2))
    log("pilot done: A2 is deleted and D1's runner terminated while the decision is pending (D1 keeps its data)")
    IN.cleanup(sess, m, "A2")
    for step in ("provision", "ready", "load:1.0"):
        _unmark(m, "A2", step)
    IN.terminate_runner(sess, m, "D1", m.data["runners"]["D1"])
    _unmark(m, "D1", "ready")
    return 0


def do_load(sess, m, args, cfgs):
    guard = cost.Guard(lambda: m.data, cap=args.budget_cap)
    pilot = _pilot(m)

    def one(cfg):
        if cfg == "D1" and args.fraction >= 1.0:
            if not pilot or "load_slice_dpu" not in pilot:
                raise S.SafetyError("run `pilot` before a full D1 load")
            return full_load_d1(sess, m, guard, pilot)
        return load_config(sess, m, cfg, args.fraction)
    return _print_outcomes("load", parallel(cfgs, one))


def _require_ready(m, cfgs):
    for cfg in cfgs:
        missing = [s for s in ("ready", "load:1.0") if s not in _steps(m, cfg)]
        if missing:
            raise S.SafetyError(f"{cfg} is missing {missing}; run batch-up/load first")


def do_explore(sess, m, args):
    cfgs = list(S.CONFIGS)
    _require_ready(m, cfgs)
    guard = cost.Guard(lambda: m.data, cap=args.budget_cap)
    pilot = _pilot(m)

    def one(cfg):
        got, seen = [], 0.0
        for cell in explore_cells(cfg, 1, args.explore_warmup_s, args.explore_measure_s):
            r = run_cell_on(sess, m, guard, cfg, cell, pilot, seen)
            seen = max(seen, (r.get("metrics") or {}).get("attempt_tps") or 0.0)
            got.append(r)
        return got
    out = parallel(cfgs, one)
    code = _print_outcomes("explore", out)
    q = slo.qref({cfg: o["value"] for cfg, o in out.items() if o["ok"]}) if not code else {"qref": None}
    S.write_private(os.path.join(run_dir(m.prefix), "qref.json"), q)
    print(json.dumps(q, indent=2))
    if q.get("qref") is None:
        log("no Qref: a config never met the SLO or a config failed. Decide before `measure`.")
        return code or 1
    return 0


def do_measure(sess, m, args):
    cfgs = list(S.CONFIGS)
    _require_ready(m, cfgs)
    q = _load_json(os.path.join(run_dir(m.prefix), "qref.json"))
    if not q or not q.get("qref"):
        raise S.SafetyError("run `explore` first (qref.json has no Qref)")
    qref = q["qref"]
    guard = cost.Guard(lambda: m.data, cap=args.budget_cap)
    pilot = _pilot(m)

    def open_cell(cfg, rate, rep, kind):
        cell = OL.Cell(f"{cfg}-{kind}-{rate:.1f}-r{rep:02d}", cfg, "open", rate, 0, rep, args.warmup_s,
                       args.measure_s)
        return run_cell_on(sess, m, guard, cfg, cell, pilot)

    def one(cfg):
        results, stopped, boundary_pass = [], False, []
        for rep in range(1, args.reps + 1):
            try:
                for cell in main_cells(cfg, slo.main_rates(qref), rep, args.warmup_s, args.measure_s, m.prefix):
                    results.append(run_cell_on(sess, m, guard, cfg, cell, pilot))
                if rep == 1:
                    top = [r for r in results if r["cell"]["rate"] == max(slo.main_rates(qref))]
                    if top and slo.judge(top[0])["verdict"] == "pass":
                        def run_rate(rate):
                            try:
                                return open_cell(cfg, rate, 1, "b")
                            except BudgetStop as exc:
                                log(f"{cfg} boundary search stopped: {exc}")
                                return None
                        b = boundary_loop(cfg, qref, run_rate, lambda: False)
                        results += b["results"]
                        stopped = b["stopped_early"]
                        boundary_pass = sorted(r["cell"]["rate"] for r in b["results"]
                                               if slo.judge(r)["verdict"] == "pass")
                elif boundary_pass:
                    results.append(open_cell(cfg, boundary_pass[-1], rep, "b"))
            except BudgetStop as exc:
                log(f"{cfg}: {exc}")
                stopped = True
                break
        sel = slo.select_q(results, args.reps, stopped_early=stopped)
        return {"q": sel, "cells": len(results)}
    out = parallel(cfgs, one)
    code = _print_outcomes("measure", out)
    qs = {cfg: o["value"]["q"] for cfg, o in out.items() if o["ok"]}
    report = {"qref": qref, "reps": args.reps, "warmup_s": args.warmup_s, "measure_s": args.measure_s, "q": qs,
              "ratios": q_ratios(qs) if "D1" in qs else None}
    S.write_private(os.path.join(run_dir(m.prefix), "q.json"), report)
    print(json.dumps(report, indent=2))
    return code


def summarize(prefix) -> int:
    """Per-cell table (verdict, success TPS, per-kind p99, failure rate) and D1/control p99 ratios per rate."""
    base = run_dir(prefix)
    rows = []
    for cfg in S.CONFIGS:
        d = os.path.join(base, "results", cfg)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            r = _load_json(os.path.join(d, f))
            m = r.get("metrics") or {}
            rows.append({"config": cfg, "cell": r["cell"]["cell_id"], "mode": r["cell"]["mode"],
                         "rate": r["cell"]["rate"], "concurrency": r["cell"]["concurrency"], "rep": r["cell"]["rep"],
                         "verdict": slo.judge(r)["verdict"], "reasons": slo.judge(r)["reasons"],
                         "success_tps": m.get("success_tps"), "failure_rate": m.get("technical_failure_rate"),
                         "p99_ms": {k: v.get("p99_ms") for k, v in (m.get("per_kind") or {}).items()},
                         "p95_ms": {k: v.get("p95_ms") for k, v in (m.get("per_kind") or {}).items()},
                         "generator": r.get("generator"), "rtt_ms": r.get("rtt_ms"),
                         "violations": (r.get("invariants") or {}).get("violations")})
    ratios = []
    main = [r for r in rows if r["mode"] == "open" and "-m-" in r["cell"] and r["verdict"] != "invalid"]
    for r in [x for x in main if x["config"] == "D1"]:
        for c in [x for x in main if x["config"] != "D1" and x["rate"] == r["rate"] and x["rep"] == r["rep"]]:
            ratios.append({"control": c["config"], "rate": r["rate"], "rep": r["rep"],
                           "p99_ratio": {k: (round(r["p99_ms"][k] / c["p99_ms"][k], 3)
                                             if r["p99_ms"].get(k) and c["p99_ms"].get(k) else None)
                                         for k in slo.SLO}})
    out = {"cells": rows, "p99_ratios": ratios, "qref": _load_json(os.path.join(base, "qref.json")),
           "q": _load_json(os.path.join(base, "q.json")), "pilot": _load_json(os.path.join(base, "pilot.json"))}
    S.write_private(os.path.join(base, "summary.json"), out)
    for r in rows:
        print(f"{r['cell']:<28} {r['verdict']:<8} tps={r['success_tps']} fail={r['failure_rate']} "
              f"p99={r['p99_ms']}")
    return 0


# ---------------------------------------------------------------- CLI ------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["init", "discover", "batch-up", "load", "pilot", "explore", "measure",
                                        "summarize", "batch-down", "verify", "replace-runner", "status"])
    p.add_argument("--account-id")
    p.add_argument("--profile", default=S.PROFILE)
    p.add_argument("--region", default=S.REGION)
    p.add_argument("--prefix")
    p.add_argument("--configs", default=",".join(S.CONFIGS))
    p.add_argument("--config", choices=S.CONFIGS, help="replace-runner: the config whose runner to replace")
    p.add_argument("--allow-on-demand", action="store_true")
    p.add_argument("--runner-type", default=IN.RUNNER_TYPE)
    p.add_argument("--max-lifetime-minutes", type=int, default=900)
    p.add_argument("--fraction", type=float, default=1.0)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--warmup-s", type=int, default=180)
    p.add_argument("--measure-s", type=int, default=600)
    p.add_argument("--explore-warmup-s", type=int, default=120)
    p.add_argument("--explore-measure-s", type=int, default=300)
    p.add_argument("--budget-cap", type=float, default=cost.BUDGET_CAP_USD)
    args = p.parse_args(argv)
    if args.region != S.REGION:
        raise S.SafetyError(f"E002 is fixed to {S.REGION}")
    if args.budget_cap > cost.HARD_CAP_USD:
        raise S.SafetyError(f"--budget-cap cannot exceed the USD {cost.HARD_CAP_USD} hard cap")
    cfgs = [c for c in args.configs.split(",") if c]
    if any(c not in S.CONFIGS for c in cfgs):
        p.error(f"--configs must be a subset of {','.join(S.CONFIGS)}")
    if args.command == "summarize":
        return summarize(args.prefix)
    if not args.account_id:
        p.error("--account-id is required")
    sess, ident = session(args)
    if args.command == "init":
        prefix = S.new_run_prefix()
        S.Manifest.create(os.path.join(run_dir(prefix), "manifest.json"), args.account_id, args.region, prefix,
                          args.max_lifetime_minutes, ident["Arn"])
        print(prefix)
        return 0
    m = load_manifest(args)
    if args.command == "discover":
        disc = IN.discover(sess)
        with m.lock:
            m.data["discovery"] = disc
            m.save()
        print(json.dumps({k: disc[k] for k in ("common_16", "zones", "runner_az", "spot_usd_per_h")}, indent=2))
        return 0
    if args.command == "status":
        refresh_measured(sess, m)
        print(json.dumps({"spent_usd": round(cost.spent_usd(m.data), 3), "steps": m.data.get("steps"),
                          "minutes_left": round(m.minutes_left())}, indent=2))
        return 0
    if args.command in ("batch-up", "load", "pilot", "explore", "measure") and "discovery" not in m.data:
        raise S.SafetyError("run discover first")
    if args.command == "batch-up":
        return do_batch_up(sess, m, args, cfgs)
    if args.command == "load":
        return do_load(sess, m, args, cfgs)
    if args.command == "pilot":
        return do_pilot(sess, m, args)
    if args.command == "explore":
        return do_explore(sess, m, args)
    if args.command == "measure":
        return do_measure(sess, m, args)
    if args.command == "replace-runner":
        if not args.config:
            p.error("--config is required")
        _unmark(m, args.config, "ready")
        ensure_config(sess, m, args.config, args)
        return 0
    if args.command == "batch-down":
        live = [c for c in cfgs if any(r["config"] == c and r["state"] != "deleted" for r in m.data["resources"])]
        out = parallel(live, lambda cfg: IN.cleanup(sess, m, cfg))
        code = _print_outcomes("batch-down", out)
        if not S.active_configs(m.data) and any(r["config"] == S.BATCH and r["state"] != "deleted"
                                                for r in m.data["resources"]):
            IN.cleanup(sess, m, S.BATCH)
        if code:
            return code
    rep = IN.verify(sess, m)
    S.write_private(os.path.join(run_dir(m.prefix), f"verify-{rep['verified_at'].replace(':', '')}.json"), rep)
    log(f"remaining_count={rep['remaining_count']} (tag index ARNs for review: {len(rep['tag_index_arns_for_review'])})")
    return 0 if rep["remaining_count"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
