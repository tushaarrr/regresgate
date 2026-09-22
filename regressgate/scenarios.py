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


def pairs(n, b, c, concordant_fail=0, quarantine_cases=(), repeat=1):
    """n cases; c broken, b fixed, `concordant_fail` failing in both.

    `quarantine_cases` are broken cases given known-flaky ids, counted inside c.
    They must be distinct CASES: the gate collapses repeats of one case into one
    observation, so a single flaky case repeated 16 times is one broken case,
    not sixteen. This scenario file used to build it the second way, which is
    the pseudo-replication the collapse exists to stop.

    `repeat` emits each case `repeat` times with distinct repeat slots. The
    verdict must not move with it -- that is what R_repeat_invariance asserts.
    """
    out, i = [], 0

    def add(case, bp, hp):
        for r in range(repeat):
            out.append({"pair_id": f"{case}#0#{r}", "case_id": case,
                        "baseline_pass": bp, "head_pass": hp})

    for q in quarantine_cases:
        add(q, True, False)
    c -= len(quarantine_cases)
    n -= len(quarantine_cases)
    for _ in range(c):
        add(f"case-{i:04d}", True, False); i += 1
    for _ in range(b):
        add(f"case-{i:04d}", False, True); i += 1
    for _ in range(concordant_fail):
        add(f"case-{i:04d}", False, False); i += 1
    while len(out) < (n + len(quarantine_cases)) * repeat:
        add(f"case-{i:04d}", True, True); i += 1
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
    # 14 cases broke; 13 of them are known-flaky and quarantined. Dropping those
    # leaves 1 break against 1 fix, which is nothing. DISTINCT cases, because
    # one flaky case cannot carry a 14-case drop however many times it repeats.
    "D_explained_by_quarantine": (
        doc(pairs(300, b=1, c=14, concordant_fail=10,
                  quarantine_cases=['flaky-json-mode', 'flaky-retry', 'flaky-tool-order', 'flaky-stop-seq', 'flaky-unicode', 'flaky-latency', 'flaky-empty', 'flaky-order', 'flaky-dupe', 'flaky-trunc', 'flaky-enc', 'flaky-lang', 'flaky-tz']),
            quarantined=['flaky-json-mode', 'flaky-retry', 'flaky-tool-order', 'flaky-stop-seq', 'flaky-unicode', 'flaky-latency', 'flaky-empty', 'flaky-order', 'flaky-dupe', 'flaky-trunc', 'flaky-enc', 'flaky-lang', 'flaky-tz']), "COMMENT"),
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
    # --repeat is a COST knob. It must not move a verdict. Before repeats were
    # collapsed, this same regression read COMMENT at --repeat 1 and BLOCK at
    # --repeat 3, and one broken case repeated ten times reached p = 0.00098.
    print("=" * 72)
    print("# repeat invariance: the same regression at --repeat 1 / 3 / 5")
    for c in (4, 5, 6, 7, 10, 17):
        seen = []
        for rep in (1, 3, 5):
            got, det = gate.decide(doc(pairs(300, b=0, c=c, repeat=rep)))
            seen.append((got, round(det["p"], 12), round(det["ci"][1], 9), det["tally"]["n"]))
        print(f"#   c={c:2d}  {seen[0][0]:7s} p={seen[0][1]:<10.4g} n={seen[0][3]}"
              f"   {'SAME at 3 and 5' if seen[0] == seen[1] == seen[2] else '*** MOVED ***'}")
        if seen[0] != seen[1] or seen[1] != seen[2]:
            print(f"#     {seen}")
            bad += 1
    # MAJORITY, not "passed at least once". A case that passes 1 of 3 repeats on
    # the head and 2 of 3 on the baseline HAS broken; `any()` would call it
    # green and hide a real regression behind flakiness.
    mixed = []
    for i in range(20):
        for r, (bp, hp) in enumerate([(True, True), (True, False), (False, False)]):
            mixed.append({"pair_id": f"mix-{i}#0#{r}", "case_id": f"mix-{i}",
                          "baseline_pass": bp, "head_pass": hp})
    for i in range(280):
        mixed.append({"pair_id": f"ok-{i}#0#0", "case_id": f"ok-{i}",
                      "baseline_pass": True, "head_pass": True})
    t = gate.tally(mixed)
    print(f"#   20 cases at baseline 2/3 -> head 1/3: b={t['b']} c={t['c']} n={t['n']}"
          f"{'' if (t['b'], t['c'], t['n']) == (0, 20, 300) else '   *** majority rule broken ***'}")
    bad += (t["b"], t["c"], t["n"]) != (0, 20, 300)

    # STRICT majority: with an even repeat count, half the repeats passing is
    # not passing. baseline 3/4 -> head 2/4 is a break; `>=` would call it green.
    tie = [{"pair_id": f"t{i}#0#{r}", "case_id": f"t{i}",
            "baseline_pass": r < 3, "head_pass": r < 2}
           for i in range(20) for r in range(4)]
    tie += [{"pair_id": f"u{i}#0#0", "case_id": f"u{i}",
             "baseline_pass": True, "head_pass": True} for i in range(280)]
    t = gate.tally(tie)
    print(f"#   20 cases at baseline 3/4 -> head 2/4 (head ties): b={t['b']} c={t['c']}"
          f"{'' if (t['b'], t['c']) == (0, 20) else '   *** a tie must not count as a pass ***'}")
    bad += (t["b"], t["c"]) != (0, 20)

    # the same rule on the baseline side: a baseline that only managed 2 of 4
    # was not passing, so head 1/4 is not a NEW break
    tie2 = [{"pair_id": f"v{i}#0#{r}", "case_id": f"v{i}",
             "baseline_pass": r < 2, "head_pass": r < 1}
            for i in range(20) for r in range(4)]
    tie2 += [{"pair_id": f"w{i}#0#0", "case_id": f"w{i}",
              "baseline_pass": True, "head_pass": True} for i in range(280)]
    t = gate.tally(tie2)
    print(f"#   20 cases at baseline 2/4 (a tie) -> head 1/4: b={t['b']} c={t['c']}"
          f"{'' if (t['b'], t['c']) == (0, 0) else '   *** a tied baseline must not count as a pass ***'}")
    bad += (t["b"], t["c"]) != (0, 0)

    # the collapse unit is (case_id, prompt_idx), never case_id alone: two
    # prompts of one case are two observations, and merging them halves n
    two_prompts = [{"pair_id": f"c{i}#{pi}#0", "case_id": f"c{i}",
                    "baseline_pass": True, "head_pass": pi == 0}
                   for i in range(10) for pi in (0, 1)]
    t = gate.tally(two_prompts)
    print(f"#   10 cases x 2 prompts, one prompt broken: n={t['n']} c={t['c']}"
          f"{'' if (t['n'], t['c']) == (20, 10) else '   *** prompt_idx collapsed away ***'}")
    bad += (t["n"], t["c"]) != (20, 10)

    # one case cannot decide a merge, however many times it is repeated
    got, det = gate.decide(doc(pairs(30, b=0, c=1, repeat=10)))
    print(f"#   1 broken case x10 repeats on a 30-case suite -> {got} "
          f"(p={det['p']:.4g}, n={det['tally']['n']})"
          f"{'' if got == 'PASS' else '   *** one case must not block ***'}")
    bad += got != "PASS"

    print("=" * 72)
    print("MISMATCHES:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
