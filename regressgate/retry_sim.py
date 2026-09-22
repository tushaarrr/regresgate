#!/usr/bin/env python3
"""Regenerate the retry-until-green table. The numbers it prints are the ONLY
source for the figures quoted in README.md, PLAN.md, gate.py and
verdict_cache.py.

This file exists because those figures were once written down from a
simulation that was not committed, and an independent re-derivation could not
reproduce them: the recorded per-run ship rate at -5pp was 0.268, and
regenerating it against the gate's own rule gives 0.205. Nobody could tell
which was right, because the code that produced 0.268 was gone. A number
quoted in four files needs a `python3 retry_sim.py` behind it.

WHAT IT MODELS. A developer whose change carries a true regression of `delta`
re-runs CI until the gate lets them through. Each run is an independent draw
from the same distribution the gate samples, so:

  * `churn` is the TOTAL two-way flip mass -- the fraction of cases that
    disagree with themselves between two runs of the SAME commit. That is
    exactly what quarantine.py measures from the A/A replays (discordant
    pairs / compared pairs), so the number here and the measured one are the
    same quantity. It is split evenly between the two directions.
  * a true drop of |delta| moves that much extra mass into `c` (broken).
  * every run is scored by the REAL gate: gate.significant(), i.e. one-sided
    exact McNemar p < 0.05 AND CI upper < -1pp. Not a re-implementation.

The unit is CASES, matching gate.collapse() (repeats of one case are not
independent) and verdict_cache.power_at (the power floor is in cases).

    python3 retry_sim.py                    # the table, at the measured churn
    python3 retry_sim.py --churn 0.02       # at some other churn
    python3 retry_sim.py --selfcheck
"""

import argparse
import json
import os
import random
import sys

import gate

SEED = 20260922
TRIALS = 40000
DELTAS_PP = (-2.0, -3.0, -5.0, -10.0)
RUNS = (1, 2, 3, 5)


def ship_rate(delta_pp, churn, n_cases, trials=TRIALS, seed=SEED):
    """P(a single run of the gate does NOT block a regression of delta_pp).

    Returns (ship, blocked_by_p, blocked_by_ci) so the two block conditions can
    be told apart -- which matters, because the materiality bar is the larger
    of the two leaks and PLAN.md once claimed it "essentially never binds".
    """
    d = abs(delta_pp) / 100.0
    p_b = churn / 2.0
    p_c = churn / 2.0 + d
    if p_c < 0 or p_b + p_c > 1:
        raise ValueError(f"infeasible: churn={churn} delta_pp={delta_pp}")
    rng = random.Random(seed)
    shipped = only_p = only_ci = 0
    for _ in range(trials):
        b = c = 0
        for _ in range(n_cases):
            u = rng.random()
            if u < p_b:
                b += 1
            elif u < p_b + p_c:
                c += 1
        _, _, _, sig, material = gate.significant({"b": b, "c": c, "n": n_cases})
        if sig and material:
            continue
        shipped += 1
        if not sig:
            only_p += 1
        elif not material:
            only_ci += 1
    return shipped / trials, only_p / trials, only_ci / trials


def table(churn, n_cases, trials=TRIALS, seed=SEED):
    rows = {}
    for i, d in enumerate(DELTAS_PP):
        s, by_p, by_ci = ship_rate(d, churn, n_cases, trials, seed + i)
        rows[d] = {"per_run": s, "not_significant": by_p, "not_material": by_ci,
                   "after": {k: 1 - (1 - s) ** k for k in RUNS}}
    return {"churn": churn, "n_cases": n_cases, "trials": trials, "seed": seed,
            "rule": "one-sided exact McNemar p < 0.05 AND CI upper < -1pp, via "
                    "gate.significant()", "unit": "cases", "rows": rows}


def render(t):
    L = [f"N = {t['n_cases']} cases, churn = {100 * t['churn']:.2f}% "
         f"(total two-way flip mass), {t['trials']:,} trials, seed {t['seed']}",
         f"rule: {t['rule']}", "",
         "P(a regression of this size SHIPS) after k re-runs:", "",
         "    true drop " + "".join(f"{k:>7} run{'s' if k > 1 else ' '}" for k in RUNS)
         + "     leaks by"]
    for d, r in t["rows"].items():
        cells = "".join(f"{r['after'][k]:>12.3f}" for k in RUNS)
        why = (f"   p {r['not_significant']:.3f} / CI {r['not_material']:.3f}")
        L.append(f"   {d:>6.0f}pp{cells}{why}")
    L += ["", "The two rightmost figures split the single-run leak: `p` is the "
              "regression the test could not call significant at all, `CI` is the "
              "one it called significant but not material (CI upper >= -1pp)."]
    return "\n".join(L)


def _selfcheck():
    # A regression so large the gate never misses it must not leak; one of zero
    # size must always ship. These bound the table from both ends.
    s, _, _ = ship_rate(-50.0, 0.02, 300, trials=400, seed=1)
    assert s < 0.01, f"a -50pp drop must not ship: {s}"
    s, _, _ = ship_rate(0.0, 0.02, 300, trials=400, seed=1)
    assert s > 0.99, f"a zero-size 'drop' must always ship: {s}"

    # More re-runs can only help the attacker, and a bigger drop is caught more
    # often. Both are monotone; a table that is not monotone is a broken model.
    t = table(0.02, 300, trials=2000, seed=7)
    for d, r in t["rows"].items():
        a = [r["after"][k] for k in RUNS]
        assert a == sorted(a), f"ship rate must not fall with more re-runs: {d} {a}"
    per_run = [t["rows"][d]["per_run"] for d in DELTAS_PP]
    assert per_run == sorted(per_run, reverse=True), \
        f"a bigger drop must be caught more often: {per_run}"

    # The arithmetic that turns one run into k.
    r = t["rows"][-5.0]
    assert abs(r["after"][5] - (1 - (1 - r["per_run"]) ** 5)) < 1e-12

    # THE POINT OF THE FILE: at the design effect size the materiality bar,
    # not the significance test, is the bigger leak. PLAN.md used to claim
    # condition (2) "essentially never binds".
    assert r["not_material"] > r["not_significant"], \
        f"expected the CI bar to be the larger leak at -5pp: {r}"

    # Determinism -- the whole point of committing this.
    assert ship_rate(-5.0, 0.02, 300, trials=500, seed=3) == \
           ship_rate(-5.0, 0.02, 300, trials=500, seed=3)
    print("retry_sim selfcheck OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--churn", type=float, default=None,
                    help="total two-way flip rate; default: read from quarantine.json")
    ap.add_argument("--n-cases", type=int, default=None,
                    help="default: the gating case count in quarantine.json")
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        return _selfcheck()

    churn, n = a.churn, a.n_cases
    qp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quarantine.json")
    if (churn is None or n is None) and os.path.exists(qp):
        with open(qp) as f:
            q = json.load(f)
        churn = q["churn"] if churn is None else churn
        n = q.get("n_gating_cases", q.get("n_cases")) if n is None else n
        print(f"# churn and N read from quarantine.json, measured "
              f"{q.get('measured_at')} over {q.get('aa_runs')} A/A replays")
    if churn is None or n is None:
        ap.error("no quarantine.json; pass --churn and --n-cases")

    t = table(churn, n, a.trials, a.seed)
    print(json.dumps(t, indent=2) if a.json else render(t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
