#!/usr/bin/env python3
"""Phase 2: derive the exclusion list and the two guardrails, from A/A replays.

Run the suite K times against the SAME commit at production temperature. Nothing
about the system changed between those runs, so every disagreement is noise, and
that noise is the thing the gate has to see past.

Three numbers come out, and two of them are gates, not diagnostics:

  churn        the two-way flip mass. HARD CEILING 6%. This is the only guardrail
               that catches a degrading judge at the source, and it fires before
               power halves. It is a ceiling that stops the gate, not a target
               that informs it.
  quarantine   cases that did not agree with themselves across all K runs.
               CAPPED at 15% of the suite, over the UNION with the flaky
               watchlist -- capped separately, the watchlist alone reaches 30
               cases in a year and costs 11 points of power at -5pp.
  power@-5pp   FLOOR 0.60. The only metric that goes red in either measured
               composed failure. Under naive quarantine it falls 0.866 -> 0.405
               while churn "improves" 1.92% -> 0.43%; under a degrading judge it
               falls 0.878 -> 0.192 while the reported null-fire rate stays
               inside its budget every quarter. Everything else lies.

    python3 quarantine.py --runs aa1.json aa2.json aa3.json aa4.json aa5.json \
        --out quarantine.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import datetime, timezone

import pair
import parse
from verdict_cache import DESIGN_EFFECT_PP, POWER_FLOOR, power_at

CHURN_CEILING = 0.06


def case_verdicts(rows):
    """{case_id: [pass?, ...]} over scorable rows only."""
    out = {}
    for r in rows:
        if r.state in (parse.ERROR, parse.UNSCORED):
            continue
        out.setdefault(r.case_id, []).append(r.state == parse.PASSED)
    return out


def analyse(paths):
    runs = [pair.load(p) for p in paths]
    # r["tests"] is what actually ran, read from the rows. The PUBLISHED
    # promptfoo leaves config.tests as raw "file://" strings, and contract_key
    # refuses to hash that -- so without this argument every real A/A run dies.
    keys = [pair.contract_key(r["config"], r.get("tests")) for r in runs]
    if any(k != keys[0] for k in keys):
        raise SystemExit("::error::the A/A runs do not share one contract; "
                         "they are not replays of the same experiment")

    # churn: sample-level discordance over every unordered pair of replays. Using
    # every pair rather than consecutive ones costs nothing and halves the noise.
    disc = tot = 0
    for a, b in itertools.combinations(runs, 2):
        ps, _ = pair.make_pairs(a["rows"], b["rows"])
        disc += sum(p["baseline_pass"] != p["head_pass"] for p in ps)
        tot += len(ps)
    churn = disc / tot if tot else 0.0

    # quarantine: a case that did not agree with itself, anywhere, K times over.
    per_run = [case_verdicts(r["rows"]) for r in runs]
    cases = sorted({c for pr in per_run for c in pr})
    flips = {}
    for c in cases:
        seen = [v for pr in per_run for v in pr.get(c, [])]
        flips[c] = {"samples": len(seen), "passes": sum(seen),
                    "unanimous": len(set(seen)) <= 1}
    unstable = sorted(c for c in cases if not flips[c]["unanimous"])
    return {"runs": len(runs), "contract_key": keys[0], "n_cases": len(cases),
            "churn": churn, "discordant": disc, "compared": tot,
            "flips": flips, "unstable": unstable}


def report(a, watchlist=(), floor=POWER_FLOOR, ceiling=CHURN_CEILING):
    excluded = sorted({*a["unstable"], *watchlist})
    gating = a["n_cases"] - len(excluded)
    cap = int(pair.QUARANTINE_CAP * a["n_cases"])
    # Power is computed in CASES, not in paired samples. The gate tallies samples,
    # so with --repeat it sees a larger n -- but repeats of one case are not
    # independent draws, and counting them as if they were would overstate power.
    # The case count is the conservative unit, so the floor is enforced on it.
    power = power_at(-DESIGN_EFFECT_PP, a["churn"], gating) if gating > 0 else 0.0
    return {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contract_key": a["contract_key"], "aa_runs": a["runs"],
        "quarantined": a["unstable"], "watchlist": sorted(watchlist),
        "n_cases": a["n_cases"], "n_gating_cases": gating,
        "churn": round(a["churn"], 5), "churn_ceiling": ceiling,
        "churn_ok": a["churn"] <= ceiling,
        f"power_at_{int(DESIGN_EFFECT_PP)}pp": round(power, 3),
        "power_floor": floor, "power_ok": power >= floor,
        "exclusion_cap": cap, "exclusion_ok": len(excluded) <= cap,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="+", required=True, help="K A/A exports of one commit")
    ap.add_argument("--watchlist", nargs="*", default=[],
                    help="known-flaky case ids from gate condition (3)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    if len(a.runs) < 2:
        ap.error("need at least 2 A/A replays; the plan says 5")

    r = report(analyse(a.runs), a.watchlist)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(r, f, indent=2)
            f.write("\n")

    pk = f"power_at_{int(DESIGN_EFFECT_PP)}pp"
    print(f"A/A replays        : {r['aa_runs']}")
    print(f"cases              : {r['n_cases']} ({r['n_gating_cases']} gating after exclusions)")
    print(f"churn              : {r['churn']:.2%}  ceiling {r['churn_ceiling']:.0%}  "
          f"{'OK' if r['churn_ok'] else 'OVER CEILING -> stop the gate'}")
    print(f"quarantined        : {len(r['quarantined'])} + {len(r['watchlist'])} watchlist, "
          f"cap {r['exclusion_cap']}  {'OK' if r['exclusion_ok'] else 'OVER CAP'}")
    print(f"power@-{int(DESIGN_EFFECT_PP)}pp         : {r[pk]:.3f}  floor {r['power_floor']:.2f}  "
          f"{'OK' if r['power_ok'] else 'BELOW FLOOR -> comment-only'}")
    if r["quarantined"]:
        print("  unstable: " + ", ".join(r["quarantined"][:12])
              + (" ..." if len(r["quarantined"]) > 12 else ""))
    # Phase 2 is done when churn is under the ceiling and the exclusions are under
    # the cap. A power miss degrades the gate; it does not invalidate the suite.
    return 0 if (r["churn_ok"] and r["exclusion_ok"]) else 1


def _selfcheck():
    def run(spec):
        """spec: {case: [pass?, ...]} -> a fake loaded run."""
        rows = [pair._row(c, 0, i, parse.PASSED if v else parse.FAILED)
                for c, vs in spec.items() for i, v in enumerate(vs)]
        return {"rows": rows, "config": None}

    # analyse() loads from disk, so exercise its two halves directly.
    stable = {"a": [True], "b": [True], "c": [False]}
    runs = [run(stable) for _ in range(5)]
    disc = tot = 0
    for x, y in itertools.combinations(runs, 2):
        ps, _ = pair.make_pairs(x["rows"], y["rows"])
        disc += sum(p["baseline_pass"] != p["head_pass"] for p in ps)
        tot += len(ps)
    assert (disc, tot) == (0, 30), (disc, tot)   # 10 run-pairs x 3 cases

    # one case that cannot make up its mind is the only one quarantined
    pr = [case_verdicts(run({"a": [True], "b": [True, False]})["rows"]),
          case_verdicts(run({"a": [True], "b": [True, True]})["rows"])]
    cases = sorted({c for p in pr for c in p})
    unstable = [c for c in cases
                if len({v for p in pr for v in p.get(c, [])}) > 1]
    assert unstable == ["b"], unstable

    # the two guardrails must actually bite
    r = report({"runs": 5, "contract_key": {}, "n_cases": 300, "churn": 0.02,
                "discordant": 1, "compared": 100, "flips": {}, "unstable": []})
    assert r["churn_ok"] and r["power_ok"], r
    r = report({"runs": 5, "contract_key": {}, "n_cases": 300, "churn": 0.20,
                "discordant": 1, "compared": 100, "flips": {}, "unstable": []})
    assert not r["churn_ok"], "20% churn must breach the 6% ceiling"
    assert not r["power_ok"], "a degraded judge must trip the power floor"
    r = report({"runs": 5, "contract_key": {}, "n_cases": 100, "churn": 0.01,
                "discordant": 1, "compared": 100, "flips": {},
                "unstable": [f"c{i}" for i in range(20)]})
    assert not r["exclusion_ok"], "20 of 100 cases excluded must breach the 15% cap"
    # THE PUBLISHED EXPORT SHAPE, through analyse() itself. config.tests are raw
    # file:// strings; only row.testCase says what ran. This call site was
    # missed by the first contract-key fix and died on the first real A/A run.
    import tempfile
    d = tempfile.mkdtemp()
    paths = []
    for i in range(2):
        p = os.path.join(d, f"aa{i}.json")
        with open(p, "w") as f:
            json.dump(pair.published_export([("a", "q1", "x"), ("b", "q2", "y")]), f)
        paths.append(p)
    a = analyse(paths)
    assert a["n_cases"] == 2 and a["churn"] == 0.0, a
    assert a["contract_key"]["dataset_sha"], a["contract_key"]
    print("quarantine selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    sys.exit(main())
