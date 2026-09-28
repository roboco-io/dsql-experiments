"""Local rehearsal of the E002 pipeline against PostgreSQL (no AWS): schema → load → explore → Qref → main cells
→ Q, all through the same runner code the EC2 runners use. Run: python3 rehearse.py --dsn DSN"""
import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict

import e002 as E
import runner as R
import slo


def _cell(tgt, tmp, cell):
    out = os.path.join(tmp, f"{cell.cell_id}.json")
    R.main(["cell", "--target", tgt, "--out", out, "--cell-json", json.dumps(asdict(cell))])
    with open(out) as fh:
        return json.load(fh)


def run(dsn, fraction=0.001, warmup_s=2, measure_s=5, slo_factor=1.0):
    """slo_factor loosens every SLO bound and the schedule-lag limit for a laptop where the generator and the database share the CPU;
    the rehearsal checks the pipeline, not the numbers."""
    saved = (dict(slo.SLO), slo.MAX_TECH_FAILURE, slo.GEN_LAG_P99_MAX_MS)
    slo.REQUIRE_GENERATOR_CPU = False          # no /proc/stat on macOS
    slo.SLO.update({k: (a * slo_factor, b * slo_factor) for k, (a, b) in saved[0].items()})
    slo.MAX_TECH_FAILURE = saved[1] * slo_factor
    slo.GEN_LAG_P99_MAX_MS = saved[2] * slo_factor
    try:
        return _run(dsn, fraction, warmup_s, measure_s)
    finally:
        slo.SLO.clear()
        slo.SLO.update(saved[0])
        slo.MAX_TECH_FAILURE, slo.GEN_LAG_P99_MAX_MS = saved[1], saved[2]
        slo.REQUIRE_GENERATOR_CPU = True


def _run(dsn, fraction, warmup_s, measure_s):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "t.json")
        with open(tgt, "w") as fh:
            json.dump({"kind": "dsn", "dsn": dsn}, fh)
        R.main(["schema", "--target", tgt, "--out", f"{tmp}/s.json"])
        R.main(["load", "--target", tgt, "--out", f"{tmp}/l.json", "--fraction", str(fraction), "--workers", "4"])
        explore = []
        for c in E.explore_cells("LOCAL", 1, warmup_s, measure_s):
            c.scale_fraction = fraction
            explore.append(_cell(tgt, tmp, c))
        q = slo.qref({"LOCAL": explore})
        results = []
        if q["qref"]:
            for c in E.main_cells("LOCAL", slo.main_rates(q["qref"]), 1, warmup_s, measure_s, "rehearse"):
                c.scale_fraction = fraction
                results.append(_cell(tgt, tmp, c))
        return {"qref": q, "q": slo.select_q(results, 1),
                "verdicts": [(r["cell"]["cell_id"], slo.judge(r)) for r in explore + results]}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dsn", required=True)
    p.add_argument("--fraction", type=float, default=0.02)
    p.add_argument("--slo-factor", type=float, default=1.0)
    a = p.parse_args()
    print(json.dumps(run(a.dsn, a.fraction, slo_factor=a.slo_factor), indent=2, default=str))
    sys.exit(0)
