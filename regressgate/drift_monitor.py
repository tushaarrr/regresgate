#!/usr/bin/env python3
"""Phase 5: the nightly drift monitor -- the half of the product no commit causes.

promptfoo has no scheduler and no alerting sink, so both live here. Every night
the same suite runs against an UNCHANGED commit and three independent things are
checked, because each of them is invisible to the other two:

  EWMA        a slow quality slide with no code change. lam=0.2, L=3.0, sigma
              estimated by MOVING RANGE (MR/d2), never by trailing SD -- a slow
              drift inflates SD and so widens the very limits meant to catch it.
              Never by the binomial sqrt(p(1-p)/n) either: on a FIXED golden set
              the nights are not independent draws, and the binomial number is
              3.02x too wide (1.7321pp vs a measured 0.5753pp), which would make
              the limits three times too loose.
  model drift the provider-echoed model id changed between consecutive nights.
              Fires on IDENTITY, not on score, so a silent swap is caught at a
              perfectly flat pass rate. This is the observation the project
              exists to make.
  dead-man    no nightly in 30h. The absence of a run is invisible to every exit
              code there is, so the store has to assert it.

Measured operating characteristics (not the draft's): ARL0 is 555 nights with
sigma known and 452 with sigma estimated -- ~1.24-1.53 years between false
alarms, not 2.4. Detection of a 3pp break takes ~1.4 nights, not ~5. False
alarms are 1.6-1.9x more frequent than claimed and detection is faster; report
both numbers, not the flattering one.

    python3 drift_monitor.py record --export today.json --db regressgate.db
    python3 drift_monitor.py check  --db regressgate.db
    python3 drift_monitor.py check  --db regressgate.db --inject -5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pair
import runner
import store
from stats.drift import ewma_chart, sigma_mr

LAM, L, WARMUP, WINDOW = 0.2, 3.0, 20, 30

# Outcomes whose pass rate is a real measurement. A harness error is a hole in
# the series, not a low point in it -- averaging it in would drag the centre line
# down and then STOP alarming once the outage became the new normal.
MEASURED = ("OK", "TESTS_FAILED")


def record(db, export_path, git_sha, branch, trigger, repeat, exit_code, run_id=None):
    ex = pair.load(export_path)
    key = pair.contract_key(ex["config"], ex.get("tests"))   # published export: config.tests is raw file:// strings
    cases = {r.case_id for r in ex["rows"] if r.case_id is not None}
    stats = ex["stats"]
    s, f, e = (stats.get("successes", 0), stats.get("failures", 0), stats.get("errors", 0))
    n_rows = s + f + e
    # expected ROWS = cases x repeats. The store's n_cases_* columns are in CASES.
    # Mixing the two units in one column is how changing --repeat silently
    # rewrites the meaning of the history behind it.
    outcome, detail = runner.classify(stats, len(cases) * (repeat or 1), exit_code)
    run_id = run_id or f"{trigger}-{ex['eval_id'] or os.path.basename(export_path)}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    store.add_run(
        db, run_id=run_id, git_sha=git_sha, branch=branch, started_at=now, ended_at=now,
        trigger=trigger, promptfoo_version=key["promptfoo_version"],
        model_under_test_snapshot=pair.canon(ex["config"].get("providers")),
        judge_snapshot=key["judge_snapshot"], golden_set_hash=key["dataset_sha"],
        assertion_config_hash=key["assertions_sha"],
        config_dir=os.environ.get("PROMPTFOO_CONFIG_DIR", ""), exit_code=exit_code,
        outcome=outcome, n_cases_expected=len(cases), n_cases_seen=len(cases),
        successes=s, failures=f, errors=e,
        pass_rate=(100.0 * s / n_rows) if n_rows else None)
    store.ingest(db, run_id, export_path)
    db.commit()
    return {"run_id": run_id, "outcome": outcome, "detail": detail,
            "pass_rate": (100.0 * s / n_rows) if n_rows else None, "cases": len(cases)}


def series(db):
    rows = db.execute(
        "SELECT run_id, started_at, pass_rate FROM runs WHERE trigger='nightly'"
        f" AND outcome IN ({','.join('?' * len(MEASURED))}) AND pass_rate IS NOT NULL"
        " ORDER BY started_at", MEASURED).fetchall()
    return [dict(r) for r in rows]


def check(db, inject_pp=None, lam=LAM, L_=L, warmup=WARMUP, window=WINDOW):
    hist = series(db)
    vals = [r["pass_rate"] for r in hist]
    labels = [r["run_id"] for r in hist]

    injected_from = None
    if inject_pp is not None:
        # A synthetic continuation of the REAL series: same noise, shifted mean.
        # This is how you find out whether tonight's suite could see a break at
        # all, before you need it to.
        injected_from = len(vals)
        step = sigma_mr(vals[-window:]) if len(vals) > 1 else 0.0
        import random
        rng = random.Random(20260921)
        base = sum(vals[-window:]) / len(vals[-window:]) + inject_pp if vals else inject_pp
        for i in range(10):
            vals.append(base + rng.gauss(0, step))
            labels.append(f"injected+{i + 1}")

    points = list(ewma_chart(vals, lam=lam, L=L_, warmup=warmup, window=window))
    alarms = [p for p in points if p["low_alarm"]]
    out = {
        "nights": len(hist), "charted": len(points),
        "need_nights": max(0, warmup + 1 - len(vals)),
        "series_tail": [round(v, 3) for v in vals[-5:]],
        "sigma_mr_pp": round(sigma_mr(vals[-window:]), 4) if len(vals) > 1 else None,
        "alarms": [{"point": labels[p["i"]], "x": round(p["x"], 3), "z": round(p["z"], 3),
                    "lcl": round(p["lcl"], 3)} for p in alarms],
        "model_drift": store.detect_model_drift(db),
        "deadman": store.deadman(db),
    }
    if injected_from is not None:
        fired = [p["i"] for p in alarms if p["i"] >= injected_from]
        out["injection"] = {"delta_pp": inject_pp, "from_night": injected_from,
                            "detected_after_nights": (fired[0] - injected_from + 1)
                            if fired else None}
    db.commit()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="ingest tonight's export into the run store")
    r.add_argument("--export", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--git-sha", default=None)
    r.add_argument("--branch", default="main")
    r.add_argument("--trigger", default="nightly", choices=("nightly", "pr", "manual"))
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--exit-code", type=int, default=0)
    r.add_argument("--run-id")

    c = sub.add_parser("check", help="EWMA + model drift + dead-man's switch")
    c.add_argument("--db", required=True)
    c.add_argument("--inject", type=float, metavar="PP",
                   help="append a simulated break of PP and report detection lag")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    db = store.open_db(a.db)
    if a.cmd == "record":
        res = record(db, a.export, pair.git_sha(a.git_sha), a.branch, a.trigger,
                     a.repeat, a.exit_code, a.run_id)
        print(f"recorded {res['run_id']}: outcome={res['outcome']} "
              f"cases={res['cases']} pass_rate={res['pass_rate']} ({res['detail']})")
        return 0 if res["outcome"] in MEASURED else 1

    out = check(db, a.inject)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"nights recorded   : {out['nights']}"
              + (f"  (need {out['need_nights']} more before the chart starts)"
                 if out["need_nights"] else ""))
        print(f"sigma (MR/d2)     : {out['sigma_mr_pp']} pp")
        print(f"charted points    : {out['charted']}")
        print(f"dead-man's switch : {out['deadman']['verdict']} "
              f"(last nightly {out['deadman']['last_nightly']})")
        print(f"model drift       : {len(out['model_drift'])} change(s)")
        for d in out["model_drift"]:
            print(f"  {d['prev_models']} -> {d['models']}  pass rate "
                  f"{d['prev_pass_rate']} -> {d['pass_rate']}  "
                  f"quality_moved={d['quality_moved']}"
                  + ("   <- a silent swap at a flat pass rate"
                     if not d["quality_moved"] else ""))
        print(f"EWMA low alarms   : {len(out['alarms'])}")
        for al in out["alarms"]:
            print(f"  {al['point']}: x={al['x']}pp z={al['z']}pp lcl={al['lcl']}pp")
        if "injection" in out:
            inj = out["injection"]
            print(f"injection {inj['delta_pp']}pp  : detected after "
                  f"{inj['detected_after_nights']} night(s)"
                  if inj["detected_after_nights"] else
                  f"injection {inj['delta_pp']}pp  : NOT DETECTED in 10 nights")

    bad = (out["alarms"] or out["model_drift"]
           or out["deadman"]["verdict"] != "OK")
    return 1 if bad else 0


def _selfcheck():
    """40 flat nights, then a 5pp break: the chart must see it within 3 nights."""
    import tempfile
    db = store.open_db(os.path.join(tempfile.mkdtemp(), "d.db"))
    import random
    rng = random.Random(7)
    for i in range(40):
        rid = f"n{i:03d}"
        store.add_run(db, run_id=rid, git_sha="s", branch="main",
                      started_at=store.ago(24 * (50 - i)), trigger="nightly",
                      model_under_test_snapshot="m", judge_snapshot="j",
                      golden_set_hash="g", assertion_config_hash="a", exit_code=0,
                      outcome="TESTS_FAILED", n_cases_expected=300, n_cases_seen=300,
                      successes=270, failures=30, errors=0,
                      pass_rate=90.0 + rng.gauss(0, 0.58))
        store.add_cases(db, rid, "served-2026-01", 270, 30)
    db.commit()

    clean = check(db)
    assert clean["nights"] == 40, clean["nights"]
    assert not clean["alarms"], f"an in-control series must not alarm: {clean['alarms']}"
    assert 0.3 < clean["sigma_mr_pp"] < 0.9, clean["sigma_mr_pp"]

    inj = check(db, inject_pp=-5.0)["injection"]
    assert inj["detected_after_nights"] is not None, "a 5pp break must be detected"
    assert inj["detected_after_nights"] <= 3, inj
    print(f"  in-control 40 nights: no alarm, sigma={clean['sigma_mr_pp']}pp "
          f"(binomial would say {100 * (0.9 * 0.1 / 300) ** 0.5:.4f}pp)")
    print(f"  -5pp injection detected after {inj['detected_after_nights']} night(s)")

    # model drift at a PERFECTLY FLAT pass rate
    for i, mid in ((40, "served-2026-01"), (41, "served-2026-03")):
        rid = f"n{i:03d}"
        store.add_run(db, run_id=rid, git_sha="s", branch="main",
                      started_at=store.ago(24 * (50 - i)), trigger="nightly",
                      model_under_test_snapshot="m", judge_snapshot="j",
                      golden_set_hash="g", assertion_config_hash="a", exit_code=0,
                      outcome="OK", n_cases_expected=300, n_cases_seen=300,
                      successes=300, failures=0, errors=0, pass_rate=100.0)
        store.add_cases(db, rid, mid, 300, 0)
    db.commit()
    md = check(db)["model_drift"]
    assert len(md) == 1 and md[0]["quality_moved"] is False, md
    print(f"  model drift fired at a flat pass rate: {md[0]['prev_models']} -> "
          f"{md[0]['models']}, quality_moved={md[0]['quality_moved']}")

    # a harness-errored night is a HOLE, never a low point
    store.add_run(db, run_id="bad", git_sha="s", branch="main", started_at=store.ago(1),
                  trigger="nightly", model_under_test_snapshot="m", judge_snapshot="j",
                  golden_set_hash="g", assertion_config_hash="a", exit_code=100,
                  outcome="HARNESS_ERROR", n_cases_expected=300, n_cases_seen=300,
                  successes=0, failures=0, errors=300, pass_rate=0.0)
    db.commit()
    assert not check(db)["alarms"], "an outage must not be charted as a quality drop"
    print("  harness-errored night excluded from the series, not charted as 0%")
    # the PUBLISHED export shape through record() itself (config.tests are raw
    # file:// strings) -- the second call site the first contract-key fix missed
    pub = os.path.join(tempfile.mkdtemp(), "night.json")
    with open(pub, "w") as f:
        json.dump(pair.published_export([("a", "q1", "x")]), f)
    db2 = store.open_db(os.path.join(tempfile.mkdtemp(), "d2.db"))
    record(db2, pub, "sha", "main", "nightly", 1, 0, run_id="pub-1")
    db2.commit()
    assert check(db2)["nights"] == 1, "a published-shape export must record as a measured night"
    print("drift_monitor selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    sys.exit(main())
