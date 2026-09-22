#!/usr/bin/env python3
"""regressgate gate: decide whether a promptfoo eval regression blocks a PR.

BLOCK only if ALL THREE hold:
  1. one-sided exact McNemar p < 0.05 (head worse than baseline)
  2. upper bound of the 95% CI on delta < -1pp
  3. the delta is NOT explained away by known-flaky / quarantined cases

Only (1) -> COMMENT, never block.

Input is a pairing JSON produced upstream by the extractor (see PAIRING SCHEMA
below). gate.py never reads promptfoo exports directly and never reimplements
the statistics -- it imports them from regressgate.stats.paired.

PAIRING SCHEMA
{
  "harness_error": null | "<message>",          # upstream infra failure
  "n_cases_expected": 300,                       # from the committed manifest
  "quarantined": ["case-id", ...],
  "power_floor_ok": true | false | null,         # from quarantine.json; null = unmeasured
  "baseline": {"eval_id": "...", "git_sha": "...", "contract_key": {...},
               "errors": 0} | null,
  "head":     {"eval_id": "...", "git_sha": "...", "contract_key": {...},
               "errors": 0},                 # git_sha keys the verdict cache
  "pairs": [
    {"pair_id": "alpha#0", "case_id": "alpha",
     "baseline_pass": true, "head_pass": false}, ...
  ]
}

A pair is one (case_id, prompt_idx, repeat) sample present in BOTH runs.
One case_id can own many pairs (that is what --repeat produces), which is why
quarantine is applied by case_id, not by pair.

EXIT CODES (deliberately disjoint from promptfoo's 0/1/100/130)
  0  PASS or COMMENT   -- merge allowed
  20 BLOCK             -- statistically real regression
  30 REFUSE            -- no compatible baseline; no verdict was computed
  40 HARD_FAIL         -- case-count mismatch; the suite itself is wrong
  50 HARNESS_ERROR     -- infrastructure alarm, NOT a quality verdict
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import verdict_cache

# Direction-aware one-sided p + pp-scaled CI. paired.mcnemar_exact defaults to
# TWO-sided and its one-sided flag keys off min(b,c), so it is direction-BLIND:
# it returns the same p for a 17-broken regression and a 17-fixed improvement.
from stats.adapter import delta_ci, mcnemar_one_sided_worse as mcnemar_exact

P_MAX = 0.05
CI_UPPER_MAX_PP = -1.0

EXIT = {"PASS": 0, "COMMENT": 0, "BLOCK": 20, "REFUSE": 30, "HARD_FAIL": 40, "HARNESS_ERROR": 50}

# Only quality verdicts are cached. REFUSE, HARD_FAIL and HARNESS_ERROR describe
# a fixable state of the world -- a missing baseline, a wrong case count, a 429
# storm -- and re-running after the fix MUST re-measure. Caching them would pin
# an infrastructure hiccup to a commit forever.
CACHEABLE = {"PASS", "COMMENT", "BLOCK"}


def cache_keys(doc):
    """(candidate_sha, baseline_sha, judge_snapshot), or None if any is missing.

    The verdict is a pure function of these three, so a re-run is a lookup, not
    a new roll of the dice. Measured: without this, re-running CI five times
    ships a -5pp regression 60% of the time and a -3pp one 96% of the time after
    three (retry_sim.py, at this suite's measured churn). The developer samples exactly the distribution the gate samples, so no
    amount of threshold calibration touches it -- best-of-k IS the attack.
    """
    head, base = doc.get("head") or {}, doc.get("baseline") or {}
    k = (head.get("git_sha"), base.get("git_sha"),
         (head.get("contract_key") or {}).get("judge_snapshot"))
    return k if all(k) else None


def _cached_note(hit):
    return (
        f"\n\n---\n_Cached verdict, first computed {hit['first_seen']} "
        f"(this is attempt {hit['retries']})._ The gate is a statistical test, so "
        "re-running it re-rolls it; the verdict for a given (commit, baseline, judge) "
        "is therefore computed once and replayed. Push a fix -- a new commit is a "
        "genuinely new measurement. If you believe the measurement itself was wrong, "
        "re-measure by POOLING repetitions, never by taking the best of several runs."
    )


def collapse(pairs):
    """Repeats of one case are NOT independent observations. Collapse them.

    promptfoo --repeat N gives every case N samples per run, and they are
    correlated: a deterministically broken case breaks all N times. Feeding
    each sample to McNemar as its own observation multiplies the evidence by N
    for a regression of unchanged size -- measured, with 300 cases and 6 of
    them broken: COMMENT at --repeat 1, BLOCK at --repeat 3, and at --repeat 10
    a SINGLE broken case reaches p = 0.00098 on its own. The repeat count is a
    cost knob; it must not move the verdict.

    quarantine.py already measures power in CASES ("pairs of one case are not
    independent draws"), so the test has to be in cases too, or the floor is
    guarding a different experiment from the one that runs.

    The unit is (case_id, prompt_idx) -- one case under one prompt -- which is
    the pair_id minus its repeat slot. A side passes if a STRICT majority of
    its repeats passed; the same rule is applied to baseline and head, so a
    flaky case is not pushed toward either verdict. That is what --repeat is
    for: averaging out flakiness within a run, not inflating n.
    """
    units = {}
    for p in pairs:
        # pair_id is "{case_id}#{prompt_idx}#{repeat_slot}"
        key = p["pair_id"].rsplit("#", 1)[0]
        u = units.setdefault(key, {"case_id": p["case_id"], "b": [], "h": []})
        u["b"].append(bool(p["baseline_pass"]))
        u["h"].append(bool(p["head_pass"]))
    return [{"pair_id": k, "case_id": u["case_id"],
             "baseline_pass": sum(u["b"]) * 2 > len(u["b"]),
             "head_pass": sum(u["h"]) * 2 > len(u["h"]),
             "repeats": len(u["b"])}
            for k, u in sorted(units.items())]


def tally(pairs):
    pairs = collapse(pairs)
    b = c = 0
    base_pass = head_pass = 0
    broken_ids = []
    for p in pairs:
        bp, hp = bool(p["baseline_pass"]), bool(p["head_pass"])
        base_pass += bp
        head_pass += hp
        if bp and not hp:
            c += 1
            broken_ids.append(p["case_id"])
        elif hp and not bp:
            b += 1
    n = len(pairs)
    return {
        "b": b,
        "c": c,
        "n": n,
        "broken_ids": sorted(set(broken_ids)),
        "rate_before": 100.0 * base_pass / n if n else 0.0,
        "rate_after": 100.0 * head_pass / n if n else 0.0,
        "repeats": max((p.get("repeats", 1) for p in pairs), default=1),
    }


def significant(t):
    """(1) and (2) together, on whatever pair set is handed in."""
    p = mcnemar_exact(t["b"], t["c"])
    lo, hi = delta_ci(t["b"], t["c"], t["n"])
    return p, lo, hi, (p < P_MAX), (hi < CI_UPPER_MAX_PP)


def decide(doc):
    """-> (decision, detail dict). Order matters: infra beats stats, always."""
    if doc.get("harness_error"):
        return "HARNESS_ERROR", {"why": doc["harness_error"]}

    head = doc.get("head") or {}
    # Verified: promptfoo folds provider errors into the pass rate and exits 100
    # exactly like assertion failures. A run with errored rows cannot produce a
    # quality verdict, only an infrastructure alarm.
    if head.get("errors", 0):
        return "HARNESS_ERROR", {"why": f"head run had {head['errors']} errored row(s); "
                                        "pass rate is not a quality signal"}

    base = doc.get("baseline")
    if not base:
        return "REFUSE", {"why": "no baseline run recorded for this contract"}
    if base.get("errors", 0):
        return "HARNESS_ERROR", {"why": f"baseline run had {base['errors']} errored row(s)"}
    bk, hk = base.get("contract_key", {}), head.get("contract_key", {})
    drift = sorted(k for k in set(bk) | set(hk) if bk.get(k) != hk.get(k))
    if drift:
        return "REFUSE", {"why": "baseline is not comparable; contract drifted",
                          "drift": {k: [bk.get(k), hk.get(k)] for k in drift}}

    pairs = doc.get("pairs", [])
    expected = doc.get("n_cases_expected")
    seen = len({p["case_id"] for p in pairs})
    if expected is not None and seen != expected:
        return "HARD_FAIL", {"why": f"saw {seen} cases, manifest expects {expected}",
                             "seen": seen, "expected": expected}

    t = tally(pairs)
    if t["n"] == 0:
        return "HARD_FAIL", {"why": "zero paired cases", "seen": 0, "expected": expected}

    p, lo, hi, sig, material = significant(t)
    d = {"tally": t, "p": p, "ci": (lo, hi), "delta_pp": 100.0 * (t["b"] - t["c"]) / t["n"],
         "significant": sig, "material": material, "quarantined_excused": []}

    if not sig:
        return "PASS", d
    if not material:
        return "COMMENT", d

    # (3) does it survive dropping the quarantined cases?
    q = set(doc.get("quarantined", []))
    if q:
        kept = [p_ for p_ in pairs if p_["case_id"] not in q]
        d["quarantined_excused"] = sorted(q & set(t["broken_ids"]))
        if not kept:
            return "COMMENT", d
        tq = tally(kept)
        pq, loq, hiq, sigq, matq = significant(tq)
        d["ex_quarantine"] = {"tally": tq, "p": pq, "ci": (loq, hiq),
                              "delta_pp": 100.0 * (tq["b"] - tq["c"]) / tq["n"]}
        if not (sigq and matq):
            return "COMMENT", d

    # Guardrail B. A suite whose power@-5pp is under the floor is not a gate, it
    # is a coin that occasionally lands on the truth -- so it advises and does
    # not block. This is the only check that goes red in either measured composed
    # failure: under naive quarantine and under a degrading judge, churn, FPR,
    # the null-fire rate and required-N all move the REASSURING way while the
    # suite goes blind. null (never measured) is not a pass.
    if doc.get("power_floor_ok") is not True:
        d["degraded"] = ("unmeasured" if doc.get("power_floor_ok") is None
                         else "below_floor")
        return "COMMENT", d

    return "BLOCK", d


# --- PR comment renderer -------------------------------------------------

_HEAD = {
    "BLOCK": "### Blocked: quality regression",
    "COMMENT": "### Heads up: change detected, not blocking",
    "PASS": "### No regression detected",
    "REFUSE": "### No verdict: cannot compare",
    "HARD_FAIL": "### Hard fail: suite integrity",
    "HARNESS_ERROR": "### Infrastructure alarm (not a quality verdict)",
}


def _ids(xs, limit=10):
    xs = list(xs)
    shown = ", ".join(f"`{x}`" for x in xs[:limit])
    return shown + (f" (+{len(xs) - limit} more)" if len(xs) > limit else "")


def render(decision, d):
    L = [_HEAD[decision], ""]

    if decision == "HARNESS_ERROR":
        L += [f"The eval did not produce trustworthy results: {d['why']}.",
              "",
              "This is an **infrastructure** signal. No claim is made about the quality of "
              "this change -- do not read it as a pass or a fail. Fix the run and re-trigger.",
              "",
              "_Reminder: promptfoo counts provider errors as failures in `passRate`, so a "
              "green or red exit code from the CLI cannot separate infra from quality. "
              "regressgate reads `stats.errors` instead._"]
        return "\n".join(L)

    if decision == "REFUSE":
        L += [f"regressgate **refuses to compare**: {d['why']}.", ""]
        if d.get("drift"):
            L.append("What changed between the baseline and this run:")
            L += [f"- `{k}`: `{a}` -> `{b}`" for k, (a, b) in d["drift"].items()]
            L += ["", "A regression number measured across this change would be meaningless, "
                      "so none was computed. Re-baseline on the new contract (run the suite on "
                      "`main` with these settings and promote it), then re-run this check."]
        return "\n".join(L)

    if decision == "HARD_FAIL":
        L += [f"**{d['why']}.**", "",
              "The gate deliberately does not fall back to comparing whatever showed up. "
              "A missing case is a silently green build -- promptfoo exits 0 on an empty "
              "suite. Fix the test manifest or the filter, then re-run."]
        return "\n".join(L)

    t = d["tally"]
    lo, hi = d["ci"]
    delta, p = d["delta_pp"], d["p"]
    sign = "" if delta < 0 else "+"
    line = (f"Pass rate moved **{sign}{delta:.2f}pp** "
            f"(95% CI {lo:.2f}pp to {hi:.2f}pp), from **{t['rate_before']:.1f}%** to "
            f"**{t['rate_after']:.1f}%** over {t['n']} paired cases"
            + (f" ({t['repeats']} repeats each, collapsed by majority -- "
               "repeats of one case are not independent observations)"
               if t.get("repeats", 1) > 1 else "") + ". "
            f"This change **broke {t['c']}** case(s) that passed on the baseline and "
            f"**fixed {t['b']}**. One-sided exact McNemar p = {p:.4g}.")
    L += [line, ""]

    if t["broken_ids"]:
        L += [f"Newly failing cases: {_ids(t['broken_ids'])}", ""]

    if decision == "BLOCK":
        L += ["All three block conditions hold: the shift is significant (p < 0.05), the "
              f"CI rules out a drop smaller than 1pp (upper bound {hi:.2f}pp < -1.00pp), "
              "and it is not accounted for by quarantined cases.",
              "", "**Action:** fix the cases above, or -- if this is an intended behaviour "
              "change -- update their expectations and re-baseline in the same PR."]
    elif decision == "COMMENT":
        if d.get("degraded"):
            why = ("has never been measured" if d["degraded"] == "unmeasured"
                   else "is below the 0.60 floor or its A/A churn is over the 6% ceiling")
            L += [f"**Not blocking:** this shift clears both statistical bars, but the "
                  f"suite's power@-5pp {why}, so the gate is running comment-only. "
                  "A suite that cannot reliably see the effect size it was built for "
                  "does not get to block a merge on the occasion it does.",
                  "", "**Action:** treat the cases above as a real finding and look at "
                  "them, then restore the suite (re-run the A/A replays with "
                  "`quarantine.py`; if churn is fine, the suite is too small)."]
        elif d.get("ex_quarantine"):
            q = d["ex_quarantine"]
            qlo, qhi = q["ci"]
            L += [f"**Not blocking:** the drop is carried by quarantined case(s) "
                  f"{_ids(d['quarantined_excused'])}. Excluding them, the delta is "
                  f"{q['delta_pp']:+.2f}pp (95% CI {qlo:.2f}pp to {qhi:.2f}pp, "
                  f"p = {q['p']:.4g}) -- no longer a real regression.",
                  "", "**Action:** none required for this PR. The quarantine itself is the "
                  "debt; fix or delete those cases."]
        else:
            L += [f"**Not blocking:** the shift is statistically detectable (p = {p:.4g}) but "
                  f"too small to matter -- the CI upper bound is {hi:.2f}pp, inside the "
                  "1pp materiality floor.",
                  "", "**Action:** none required. Worth a look if it repeats across PRs."]
    else:
        L += [f"No significant paired difference (p = {p:.4g} >= 0.05). Merge away."]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pairing", help="pairing JSON from the extractor")
    ap.add_argument("--comment-out", help="write the rendered PR comment here")
    ap.add_argument("--cache-db", help="verdict cache; a re-run of the same "
                                       "(commit, baseline, judge) replays its verdict")
    a = ap.parse_args(argv)

    with open(a.pairing) as f:
        doc = json.load(f)

    db = verdict_cache.connect(a.cache_db) if a.cache_db else None
    keys = cache_keys(doc) if db else None
    hit = verdict_cache.lookup(db, *keys) if keys else None

    if hit:
        decision, detail = hit["decision"], hit["evidence"]
        body = render(decision, detail) + _cached_note(hit)
        print(f"CACHED verdict (attempt {hit['retries']})")
    else:
        decision, detail = decide(doc)
        body = render(decision, detail)
        if keys and decision in CACHEABLE:
            verdict_cache.store(db, *keys, decision, detail,
                                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if a.comment_out:
        with open(a.comment_out, "w") as f:
            f.write(body + "\n")
    print(f"DECISION={decision} EXIT={EXIT[decision]}")
    print(body)
    return EXIT[decision]


if __name__ == "__main__":
    sys.exit(main())
