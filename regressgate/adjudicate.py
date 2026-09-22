#!/usr/bin/env python3
"""Phase 7a: re-measure a gate verdict by POOLING K replicates. Logged, never controlled.

When the gate BLOCKs, "was that real?" is a legitimate question and re-measuring
is a legitimate answer -- but only if the re-measurement POOLS evidence. Taking
the most favourable of several runs is exactly the retry attack the verdict
cache exists to stop (measured: a -5pp regression ships 79% of the time after
five re-runs), just run deliberately instead of by accident. So this module runs
a FIXED K, sums every replicate's discordant pairs into ONE (b, c, n), and
applies ONE test to that pooled tally. The K replicates must be K DISTINCT runs
(distinct head eval_ids): replaying one measurement K times multiplies its
evidence without adding any, which is best-of-k with the sign flipped.

Fixed-K replication is unbiased for SAMPLING error -- 99.3% accuracy at 2.0
reps, ~$1.20 per blocked PR, no humans in the loop. Two limits are load-bearing:

  NO SEQUENTIAL STOPPING. K is pre-registered and run to the end. Stopping once
  the answer looks clear inflates the false-"real" rate and reintroduces
  selection on effect size -- the same defect as best-of-k, wearing a lab coat.
  PLAN.md s5 cuts it explicitly.

  THE CEILING IS d_measured, NOT d_true. Replication removes sampling error and
  nothing else. At a 2pp construct gap between what the judge scores and what is
  actually true, even a perfect adjudicator is 67% accurate. Construct validity
  is the binding constraint and no amount of replication learns it. Every record
  written here carries that caveat.

NO CONTROL PATH. This module writes one jsonl line and returns 0. It holds no
import of the cache, the store or the phase-2 guardrails, it never calls
gate.decide, it opens nothing for writing except the append-only log, and its
exit code is a constant so no CI step can branch on the answer. The self-check
asserts all four, because "we promised not to" is not a structure. The plan's
words: log adjudications as monitors, do not wire them to a parameter.

Statistics are gate.significant() -- the same directional exact McNemar
(stats.adapter.mcnemar_one_sided_worse), the same delta_ci, and the same two
bars (p < 0.05, CI upper < -1pp) the gate used. Importing the adapter directly
would let the adjudicator and the gate drift onto different thresholds and then
disagree for a reason that is not evidence. Conditions (3) quarantine and the
power floor are NOT re-litigated here; the gate already applied them.

    python3 adjudicate.py --pairings a.json b.json ... --gate-decision BLOCK --log adj.jsonl
    python3 adjudicate.py --pairings a.json --replicates reps/ --gate-decision BLOCK
    python3 adjudicate.py --selfcheck
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

import gate

# Pre-registered replicate count, from PLAN.md s5. --k exists because K must be
# fixed BEFORE the replicates are run, not because 20 is negotiable after.
K = 20

CAVEAT = ("This converges on d_measured, not d_true. Replication removes sampling error "
          "only; at a 2pp construct gap between the judge and the truth even a perfect "
          "adjudicator is 67% accurate. Construct validity is the binding constraint and "
          "no amount of replication learns it. Monitor only -- nothing reads this log.")


def pool(tallies):
    """SUM the replicates. The single most important line in this module.

    Not max, not min, not the one with the friendliest p. Adjudicating on the
    most favourable replicate IS the attack, and it is indistinguishable from
    honest re-measurement from the outside.
    """
    return {f: sum(t[f] for t in tallies) for f in ("b", "c", "n")}


def _ident(doc):
    """What must match before two replicates are the SAME comparison."""
    h, b = doc["head"], doc["baseline"]
    return (h.get("contract_key"), h.get("git_sha"), b.get("contract_key"), b.get("git_sha"))


def load_pairings(paths):
    docs = []
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        # An errored or unpaired replicate is not weak evidence, it is no
        # evidence; pooling it in would dilute the tally with absence. errors is
        # the field gate.decide reads -- promptfoo folds provider errors into the
        # pass rate, so a 429 storm arrives as c broken pairs and pools into a
        # manufactured regression unless it is refused here.
        errs = (d.get("head") or {}).get("errors") or (d.get("baseline") or {}).get("errors")
        if d.get("harness_error") or errs:
            raise SystemExit(f"::error::{p}: harness_error {d.get('harness_error')!r}, "
                             f"{errs or 0} errored row(s) -- the gate calls this "
                             "HARNESS_ERROR and refuses to score it. An errored replicate "
                             "is not evidence and must not be pooled")
        if not d.get("baseline") or not d.get("pairs"):
            raise SystemExit(f"::error::{p}: no baseline or no pairs; nothing to pool")
        d["_path"] = p
        docs.append(d)
    return docs


def adjudicate(docs, gate_decision, k=K):
    if len(docs) != k:
        raise SystemExit(
            f"::error::adjudication needs exactly K={k} replicates, got {len(docs)}. "
            "K is pre-registered. Stopping early because the answer already looks clear, "
            "or adding replicates until it does, is selection on effect size: it inflates "
            "the false-'real' rate exactly the way best-of-k does. Run the missing "
            "replicates, or pre-register a different K before looking at them.")

    ref = _ident(docs[0])
    for d in docs[1:]:
        if _ident(d) != ref:
            raise SystemExit(
                f"::error::{d['_path']} is a different comparison than {docs[0]['_path']} "
                "(contract key or commit differs). Pooling across it would average two "
                "experiments and call the result one measurement.")

    # _ident checks they are the same COMPARISON; this checks they are different
    # RUNS. Pooling one measurement K times multiplies its evidence without adding
    # any -- amplification, the mirror image of best-of-k, and just as invisible.
    ids = [(d.get("head") or {}).get("eval_id") for d in docs]
    if None in ids or len(set(ids)) != k:
        raise SystemExit(
            f"::error::{k} replicates carry {len(set(ids))} distinct head eval_ids "
            f"{sorted(map(str, set(ids)))}. A replicate counted twice multiplies one "
            "measurement's evidence without adding a measurement. K fresh runs means K "
            "fresh eval_ids (and a missing eval_id cannot be shown to be a fresh run).")

    reps = []
    for d in docs:
        t = gate.tally(d["pairs"])
        reps.append({"source": d["_path"], "eval_id": d["head"].get("eval_id"),
                     "b": t["b"], "c": t["c"], "n": t["n"]})

    # A replicate that measured nothing is not evidence (load_pairings says the
    # same for a doc read off disk); counting it would let the record claim K
    # runs of evidence it does not have, and an all-empty pool has no rate.
    empty = [r["source"] for r in reps if r["n"] == 0]
    if empty:
        raise SystemExit(f"::error::{empty} pooled 0 pairs. A replicate with no pairs "
                         "still counts toward K, so the record would claim K runs of "
                         "evidence while fewer than K runs measured anything.")

    pooled = pool(reps)
    p, lo, hi, sig, material = gate.significant(pooled)
    real = sig and material
    # COMMENT is the gate declining to rule -- on quarantine (condition 3) or on the
    # power floor -- as often as it is the gate saying "not real". This module
    # re-tests only the two statistical bars, so it cannot tell those apart and must
    # not log a disagreement it did not measure. null, not false.
    agrees = None if gate_decision == "COMMENT" else (real == (gate_decision == "BLOCK"))
    return {
        "adjudicated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "k": k,
        "gate_decision": gate_decision,
        "pooled": {**pooled, "p": p, "ci_lo": lo, "ci_hi": hi, "significant": sig,
                   "material": material,
                   "delta_pp": 100.0 * (pooled["b"] - pooled["c"]) / pooled["n"]},
        "pooled_verdict": "REAL" if real else "NOT_REAL",
        "agrees_with_gate": agrees,
        "replicates": reps,
        "candidate_sha": docs[0]["head"].get("git_sha"),
        "contract_key": docs[0]["head"].get("contract_key"),
        "caveat": CAVEAT,
    }


def append(path, rec):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def summary(rec):
    p_ = rec["pooled"]
    agree = {True: "AGREES with", False: "DISAGREES with",
             None: "NOT COMPARABLE with"}[rec["agrees_with_gate"]]
    lines = [
        f"pooled {rec['k']} replicates: b={p_['b']} c={p_['c']} n={p_['n']} "
        f"(summed, not best-of-{rec['k']})",
        f"pooled delta      : {p_['delta_pp']:+.2f}pp "
        f"(95% CI {p_['ci_lo']:.2f}pp to {p_['ci_hi']:.2f}pp), one-sided p = {p_['p']:.4g}",
        f"pooled verdict    : {rec['pooled_verdict']}  -- {agree} the gate's "
        f"{rec['gate_decision']}",
        "per replicate b/c/n: " + "  ".join(
            f"{os.path.basename(r['source'])} {r['b']}/{r['c']}/{r['n']}"
            for r in rec["replicates"]),
        f"ceiling           : {rec['caveat']}",
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairings", nargs="+", default=[], metavar="JSON",
                    help="pairing JSONs from pair.py, one per independent re-run pair")
    ap.add_argument("--replicates", metavar="DIR", help="also pool every *.json in DIR")
    ap.add_argument("--gate-decision", choices=sorted(gate.CACHEABLE),
                    help="what the gate decided for this commit")
    ap.add_argument("--log", default=".store/adjudications.jsonl")
    ap.add_argument("--k", type=int, default=K,
                    help=f"pre-registered replicate count (default {K}); choose it BEFORE "
                         "running the replicates, never after seeing them")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)

    if a.selfcheck:
        return _selfcheck()
    paths = list(a.pairings)
    if a.replicates:
        paths += sorted(glob.glob(os.path.join(a.replicates, "*.json")))
    if not paths or not a.gate_decision:
        ap.error("--pairings/--replicates and --gate-decision are required")

    rec = adjudicate(load_pairings(paths), a.gate_decision, a.k)
    append(a.log, rec)
    print(summary(rec))
    print(f"appended to {a.log}")
    # The exit code is NOT a verdict. Returning a constant is the structural half
    # of "logged, never controlled": there is no status for a workflow step to
    # branch on, so this module cannot grow a control path by accident.
    return 0


# --------------------------------------------------------------------------- #
def _doc(b, c, n, path, ck=None):
    """A minimal pairing doc with exactly b fixed, c broken, rest passing."""
    pairs = [{"pair_id": f"x{i}", "case_id": f"x{i}",
              "baseline_pass": i >= b, "head_pass": not (b <= i < b + c)}
             for i in range(n)]
    ck = ck or {"suite": "s", "dataset_sha": "d"}
    return {"harness_error": None, "pairs": pairs, "_path": path,
            "head": {"git_sha": "cand", "contract_key": ck, "errors": 0,
                     "eval_id": f"E-{path}"},
            "baseline": {"git_sha": "base", "contract_key": ck, "errors": 0,
                         "eval_id": "E-base"}}


def _selfcheck():
    import tempfile

    k = 5
    # Four replicates that saw a real -10pp regression, plus one that came back
    # wildly favourable. Best-of-k would adjudicate on that last one.
    docs = [_doc(0, 30, 300, f"r{i}.json") for i in range(4)] + [_doc(25, 0, 300, "best.json")]

    # 1. pooling is the SUM of the discordant counts, not the best replicate.
    reps = [dict(zip(("b", "c", "n"), (t["b"], t["c"], t["n"])))
            for t in (gate.tally(d["pairs"]) for d in docs)]
    pooled = pool(reps)
    assert pooled == {"b": 25, "c": 120, "n": 1500}, pooled
    assert pooled["c"] == sum(r["c"] for r in reps) > max(r["c"] for r in reps), pooled

    # 2. one wildly favourable replicate does NOT flip the pooled verdict.
    rec = adjudicate(docs, "BLOCK", k=k)
    assert rec["pooled_verdict"] == "REAL", rec["pooled"]
    assert rec["agrees_with_gate"], rec
    alone = gate.significant(pool([reps[-1]]))
    assert not (alone[3] and alone[4]), "the favourable replicate alone must read NOT real"
    print(f"  pooled {rec['pooled']['delta_pp']:+.2f}pp -> REAL, while best-of-{k} "
          f"(p={alone[0]:.3g}) would have said NOT_REAL")

    # 2b. BOTH bars are load-bearing. Verdict a pooled tally that clears one and
    # not the other, in each direction, or `real = sig and material` can lose
    # either half silently -- which is the adjudicator/gate threshold drift that
    # importing gate.significant exists to prevent, reintroduced below the import.
    sig_only = adjudicate([_doc(0, 1, 1000, f"s{i}.json") for i in range(6)], "BLOCK", k=6)
    assert sig_only["pooled"]["significant"] and not sig_only["pooled"]["material"], sig_only
    assert sig_only["pooled_verdict"] == "NOT_REAL", sig_only["pooled"]
    mat_only = adjudicate([_doc(0, 2, 15, f"m{i}.json") for i in range(2)], "BLOCK", k=2)
    assert mat_only["pooled"]["material"] and not mat_only["pooled"]["significant"], mat_only
    assert mat_only["pooled_verdict"] == "NOT_REAL", mat_only["pooled"]

    # 2c. K replicates must be K distinct RUNS. One run replayed K times is
    # amplification: b=1/c=6/n=300 is a PASS alone and p=7e-19 pooled twenty times.
    fresh = [_doc(0, 30, 300, f"r{i}.json") for i in range(k - 1)]
    anonymous = _doc(0, 30, 300, "r9.json")
    anonymous["head"].pop("eval_id")
    for why, dupes in (
            ("one run pooled K times", [_doc(0, 30, 300, "same.json")] * k),
            ("one replicate counted twice", fresh + [_doc(0, 30, 300, "r0.json")]),
            # None is its own distinct value, so len(set(ids)) is still K: a
            # replicate that cannot be shown to be a fresh run must be refused
            # even when every other eval_id differs.
            ("a replicate with no eval_id", fresh + [anonymous])):
        try:
            adjudicate(dupes, "BLOCK", k=k)
            raise AssertionError(f"{why} must fail loudly")
        except SystemExit as e:
            assert "distinct head eval_ids" in str(e), (why, e)

    # 2d. a replicate that measured nothing is refused AT LOAD, on every field
    # gate.decide refuses on and on both sides of the pairing. A 429 storm folds
    # in as broken pairs and manufactures a regression; a doc with no pairs
    # dilutes K with absence while the record still claims K runs of evidence.
    def _written(spoil):
        d = {q: v for q, v in _doc(0, 100, 300, "storm.json").items() if q != "_path"}
        spoil(d)
        p = os.path.join(tempfile.mkdtemp(), "storm.json")
        with open(p, "a") as f:
            json.dump(d, f)
        return p

    for why, spoil, msg in (
            ("head errored", lambda d: d["head"].update(errors=100), "errored row(s)"),
            ("baseline errored", lambda d: d["baseline"].update(errors=100), "errored row(s)"),
            ("harness crashed", lambda d: d.update(harness_error="ECONNRESET"), "ECONNRESET"),
            ("no baseline", lambda d: d.pop("baseline"), "nothing to pool"),
            ("no pairs", lambda d: d.update(pairs=[]), "nothing to pool")):
        try:
            load_pairings([_written(spoil)])
            raise AssertionError(f"a replicate with {why} must not be pooled")
        except SystemExit as e:
            assert msg in str(e), (why, e)

    # 3. K is honoured exactly -- loudly, in BOTH directions.
    for docs_ in (docs[:-1], docs + [_doc(0, 30, 300, "extra.json")]):
        try:
            adjudicate(docs_, "BLOCK", k=k)
            raise AssertionError(f"{len(docs_)} replicates at K={k} must fail loudly")
        except SystemExit as e:
            assert f"exactly K={k}" in str(e), e

    # ...and the default K really is the pre-registered 20. Every case here
    # passes an explicit k, so nothing else walks the module's own constant.
    default_k = adjudicate([_doc(0, 1, 10, f"k{i}.json") for i in range(K)], "BLOCK")["k"]
    assert default_k == 20, f"K is pre-registered at 20, module default is {default_k}"

    # a replicate of a different comparison is not a replicate -- on EITHER half
    # of the key, the contract it ran or the commits it compared.
    drifted = {"contract key": _doc(0, 30, 300, "other.json", ck={"suite": "other"}),
               "head commit": _doc(0, 30, 300, "othersha.json"),
               "baseline commit": _doc(0, 30, 300, "otherbase.json")}
    drifted["head commit"]["head"]["git_sha"] = "someone-elses-commit"
    drifted["baseline commit"]["baseline"]["git_sha"] = "someone-elses-baseline"
    for why, odd in drifted.items():
        try:
            adjudicate(docs[:-1] + [odd], "BLOCK", k=k)
            raise AssertionError(f"a drifted {why} must not be pooled")
        except SystemExit as e:
            assert "different comparison" in str(e), (why, e)

    # K counts runs that MEASURED something: a zero-pair doc reaching adjudicate()
    # would fill a slot in the pre-registered K while contributing no evidence.
    try:
        adjudicate(docs[:-1] + [_doc(0, 0, 0, "empty.json")], "BLOCK", k=k)
        raise AssertionError("a replicate with 0 pairs must not count toward K")
    except SystemExit as e:
        assert "pooled 0 pairs" in str(e), e

    # 4. the written line round-trips and carries the caveat.
    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "adj.jsonl")
    append(log, rec)
    with open(log) as f:
        back = json.loads(f.read().splitlines()[-1])
    assert back["pooled"]["c"] == 120 and back["k"] == k, back
    # delta_pp is the headline number in the log and the printout; b=25 c=120 n=1500
    # is -6.33pp. Unasserted, a sign flip reports a regression as an improvement.
    assert abs(back["pooled"]["delta_pp"] - -6.3333) < 0.01, back["pooled"]["delta_pp"]
    # the RENDERED number too: pinning only the JSON leaves the printout -- the
    # thing a human reads next to the verdict -- free to disagree with the record.
    assert "-6.33pp" in summary(rec), summary(rec)
    # the interval runs low to high, and ci_hi is the bound materiality is
    # defined against (CI upper < -1pp), so swapping them misreports the bar.
    assert back["pooled"]["ci_lo"] < back["pooled"]["ci_hi"] < -1.0, back["pooled"]
    # provenance: which commit was adjudicated, under which contract, from which
    # runs. An audit row that names the wrong commit cannot be traced to a run.
    assert back["candidate_sha"] == "cand", back["candidate_sha"]
    assert back["contract_key"] == {"suite": "s", "dataset_sha": "d"}, back["contract_key"]
    assert ([r["eval_id"] for r in back["replicates"]]
            == [f"E-{d['_path']}" for d in docs]), back["replicates"]
    assert "d_measured, not d_true" in back["caveat"], back["caveat"]

    # 5. end to end through the CLI: disagreement is recorded, exit stays 0.
    paths = []
    for i, d in enumerate(docs):
        p = os.path.join(tmp, f"p{i}.json")
        with open(p, "a") as f:
            json.dump({q: v for q, v in d.items() if q != "_path"}, f)
        paths.append(p)
    rc = main(["--pairings", *paths, "--gate-decision", "PASS", "--log", log, "--k", str(k)])
    with open(log) as f:
        last = json.loads(f.read().splitlines()[-1])
    assert rc == 0, "the exit code must never carry the verdict"
    assert last["agrees_with_gate"] is False, last
    assert last["pooled_verdict"] == "REAL", last
    # ...but COMMENT is the gate declining to rule on a guardrail this module does
    # not evaluate, so it is logged as not comparable, never as a disagreement.
    commented = adjudicate(docs, "COMMENT", k=k)
    assert commented["agrees_with_gate"] is None, commented
    # all three renderings, on the record that carries each: the printed
    # conclusion is what a reviewer acts on and must not contradict the log.
    for wording, printed in (("-- DISAGREES with the gate's PASS", summary(last)),
                             ("-- AGREES with the gate's BLOCK", summary(rec)),
                             ("-- NOT COMPARABLE with the gate's COMMENT", summary(commented))):
        assert wording in printed, (wording, printed)

    # 5b. --pairings and --replicates pool TOGETHER: the invocation in this
    # module's own docstring names one file AND a directory, and dropping either
    # source changes what was pooled without changing what the record claims.
    reps_dir = os.path.join(tmp, "reps")
    os.makedirs(reps_dir)
    for i, d in enumerate(docs[1:], 1):
        with open(os.path.join(reps_dir, f"rep{i}.json"), "a") as f:
            json.dump({q: v for q, v in d.items() if q != "_path"}, f)
    try:
        main(["--pairings", paths[0], "--replicates", reps_dir,
              "--gate-decision", "BLOCK", "--log", log, "--k", str(k)])
    except SystemExit as e:
        raise AssertionError(f"--pairings and --replicates must pool together, "
                             f"not either-or -- one source was dropped: {e}")
    with open(log) as f:
        both = json.loads(f.read().splitlines()[-1])
    assert len(both["replicates"]) == k, both["replicates"]
    assert paths[0] in [r["source"] for r in both["replicates"]], both["replicates"]

    # 6. no control path, as structure rather than as a promise.
    import ast
    with open(__file__) as f:
        src = f.read()
    tree = ast.parse(src)
    imported = {n.name.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import)
                for n in x.names}
    imported |= {x.module.split(".")[0] for x in ast.walk(tree)
                 if isinstance(x, ast.ImportFrom) and x.module}
    forbidden = imported & {"verdict_cache", "store", "quarantine", "drift_monitor"}
    assert not forbidden, f"adjudicator must not import a control surface: {forbidden}"
    # Matched on the parsed tree, not on the source text, so these two checks do
    # not trip over their own spelling.
    called = {x.attr for x in ast.walk(tree) if isinstance(x, ast.Attribute)}
    # open() is not the only way to write, and mode= is not only positional. Both
    # holes let a function that truncates and rewrites quarantine.json -- the file
    # feeding gate condition (3) and guardrail B -- pass this check.
    banned = called & {"decide", "replace", "rename", "write_text", "write_bytes",
                       "unlink", "remove", "rmtree", "truncate", "connect"}
    assert not banned, f"no gate decision and no write but the append-only log: {banned}"
    modes = [x.args[1] if len(x.args) > 1
             else next((kw.value for kw in x.keywords if kw.arg == "mode"), ast.Constant("r"))
             for x in ast.walk(tree)
             if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "open"]
    # Fails closed: a mode that cannot be resolved statically is not an allowed mode.
    assert all(isinstance(m, ast.Constant) and m.value in ("r", "a") for m in modes), \
        f"only append and read are allowed: {[ast.dump(m) for m in modes]}"
    print("  no cache/store/quarantine import, no gate decision, append-only, exit always 0")

    print("adjudicate selfcheck OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
