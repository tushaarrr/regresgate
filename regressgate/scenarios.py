#!/usr/bin/env python3
"""Build synthetic pairing JSONs and run gate.py over each. Self-checking."""

import json
import os
import sys

import gate

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "scenarios")

KEY = {"dataset_sha": "d41d8c", "judge_snapshot": "claude-judge-2026-07-01",
       "promptfoo_version": "0.123.1", "suite": "support-qa"}


def pairs(n, b, c, concordant_fail=0, quarantine_case=None, quarantine_pairs=0,
          quarantine_broken=0):
    """n total pairs; c broken, b fixed, `concordant_fail` failing in both."""
    out, i = [], 0

    def add(case, pid, bp, hp):
        out.append({"pair_id": pid, "case_id": case, "baseline_pass": bp, "head_pass": hp})

    if quarantine_case:
        for r in range(quarantine_pairs):
            add(quarantine_case, f"{quarantine_case}#{r}", True, r >= quarantine_broken)
        c -= quarantine_broken
        n -= quarantine_pairs
    for _ in range(c):
        add(f"case-{i:04d}", f"case-{i:04d}#0", True, False); i += 1
    for _ in range(b):
        add(f"case-{i:04d}", f"case-{i:04d}#0", False, True); i += 1
    for _ in range(concordant_fail):
        add(f"case-{i:04d}", f"case-{i:04d}#0", False, False); i += 1
    while len(out) < n + (quarantine_pairs if quarantine_case else 0):
        add(f"case-{i:04d}", f"case-{i:04d}#0", True, True); i += 1
    return out


def doc(ps, quarantined=(), baseline_key=None, head_key=None, baseline=True,
        harness_error=None, expected=None, head_errors=0, power_floor_ok=True):
    ncase = len({p["case_id"] for p in ps})
    return {
        "harness_error": harness_error,
        "n_cases_expected": ncase if expected is None else expected,
        "quarantined": list(quarantined),
        "power_floor_ok": power_floor_ok,
        "baseline": ({"eval_id": "eval-base-991", "contract_key": baseline_key or KEY,
                      "errors": 0} if baseline else None),
        "head": {"eval_id": "eval-head-992", "contract_key": head_key or KEY,
                 "errors": head_errors},
        "pairs": ps,
    }


SCENARIOS = {
    "A_clean_pass": (doc(pairs(300, b=2, c=1, concordant_fail=14)), "PASS"),
    "B_real_regression": (doc(pairs(300, b=4, c=17, concordant_fail=10)), "BLOCK"),
    "C_significant_but_tiny": (doc(pairs(1000, b=0, c=6, concordant_fail=40)), "COMMENT"),
    "D_explained_by_quarantine": (
        doc(pairs(300, b=1, c=14, concordant_fail=10,
                  quarantine_case="flaky-json-mode", quarantine_pairs=16,
                  quarantine_broken=13),
            quarantined=["flaky-json-mode"]), "COMMENT"),
    "E_no_compatible_baseline": (
        doc(pairs(300, b=1, c=12, concordant_fail=10),
            baseline_key=dict(KEY, judge_snapshot="claude-judge-2026-04-15")), "REFUSE"),
    "F_harness_error": (doc(pairs(300, b=0, c=120), harness_error=None, head_errors=120),
                        "HARNESS_ERROR"),
    "G_case_count_mismatch": (doc(pairs(300, b=1, c=2), expected=312), "HARD_FAIL"),
    # Guardrail B: the same regression as B, on a suite that cannot see -5pp.
    "H_below_power_floor": (
        doc(pairs(300, b=4, c=17, concordant_fail=10), power_floor_ok=False), "COMMENT"),
    # The two sidedness regression tests. Under a direction-BLIND one-sided p
    # (tail from min(b,c)) these were PASS and COMMENT: a real regression shipped
    # and an improvement was flagged.
    "I_small_suite_drop": (doc(pairs(60, b=0, c=5, concordant_fail=3)), "BLOCK"),
    "J_pure_improvement": (doc(pairs(300, b=17, c=4, concordant_fail=10)), "PASS"),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    bad = 0
    for name, (d, want) in SCENARIOS.items():
        path = os.path.join(OUT, f"{name}.json")
        with open(path, "w") as f:
            json.dump(d, f, indent=1)
        got, detail = gate.decide(d)
        body = gate.render(got, detail)
        print("=" * 72)
        print(f"# {name}   ->  {got} (exit {gate.EXIT[got]})   expected {want}"
              f"{'' if got == want else '   *** MISMATCH ***'}")
        t = detail.get("tally")
        if t:
            print(f"#   b(fixed)={t['b']} c(broken)={t['c']} n={t['n']} "
                  f"p={detail['p']:.6g} ci=({detail['ci'][0]:.4f}, {detail['ci'][1]:.4f})pp")
        print("-" * 72)
        print(body)
        bad += got != want
    print("=" * 72)
    print("MISMATCHES:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
