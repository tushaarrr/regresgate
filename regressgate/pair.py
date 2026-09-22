#!/usr/bin/env python3
"""Phase 3: turn two promptfoo exports into the PAIRING SCHEMA gate.py consumes.

This is the only place that decides *what is comparable to what*. Everything
downstream (McNemar, the CI, the quarantine rule) is arithmetic on the pairs
this file emits, so every rule here is load-bearing.

Pair key is (case_id, prompt_idx, repeat_slot).

  - case_id is an explicit var. testIdx is a ROW index; grouping on `testIdx % N`
    breaks silently on per-test `options.repeat` and on string-array vars that
    expand into extra combinations.
  - repeat_slot is the provider-echoed __repeatIndex when the provider echoes it
    for every row in the group, and otherwise the ordinal position of the row
    within its group in file order. Repeats of one case are i.i.d. draws, so
    which head repeat meets which baseline repeat carries no information --
    even with an echoed index, slot 0 vs slot 0 is an arbitrary pairing. What is
    NOT safe is *inferring* the repeat from the row index, so we never do.
  - ERROR and UNSCORED rows never become pairs. An UNSCORED row is not evidence
    of a regression, and an errored row is not evidence of anything.

Suite identity travels in contract_key, which gate.py diffs: a changed golden
set, a changed assertion, a swapped judge or a bumped promptfoo pin all become
REFUSE rather than a delta measured across two different experiments.

    python3 pair.py --baseline base.json --head head.json \
        --manifest cases.manifest.json --quarantine quarantine.json --out pairing.json
    python3 pair.py --head head.json --write-manifest cases.manifest.json
    python3 pair.py --selfcheck
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import parse

HERE = os.path.dirname(os.path.abspath(__file__))

# Assertion types whose verdict comes from a model. If the suite contains one of
# these and no judge is pinned, llm-rubric picks its grader from whichever API
# keys happen to be in the environment -- so the same suite measures a different
# thing on two machines. That is a harness error, not a quality signal.
MODEL_GRADED = {
    "llm-rubric", "model-graded-closedqa", "model-graded-factuality", "factuality",
    "answer-relevance", "context-faithfulness", "context-recall", "context-relevance",
    "select-best", "g-eval", "pi", "similar", "classifier", "moderation",
}

# The union of the quarantine set and the known-flaky watchlist, as a fraction of
# the suite. Measured: uncapped, "quarantine on any flip" drains a 400-case suite
# to 145 in a year -- power@-5pp 0.866 -> 0.405 -- while churn *improves* and the
# false-positive rate goes to zero. Nothing else goes red.
QUARANTINE_CAP = 0.15

# The power floor is a QUARTERLY measurement. A stale one is not a measurement:
# the two failures it exists to catch -- a shrinking suite and a degrading judge
# -- both develop over months while every other number improves. Past this age
# the gate treats power as unmeasured and stops blocking.
POWER_MAX_AGE_DAYS = 100


class Integrity(Exception):
    """The inputs cannot honestly be paired. Becomes harness_error, never a verdict."""


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj) -> str:
    return hashlib.sha256(canon(obj).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# contract key -- what must match before a delta means anything
# --------------------------------------------------------------------------- #
def tests_from_rows(raw_rows) -> list[dict]:
    """The tests that ACTUALLY RAN, taken from each row's `testCase`.

    MEASURED, and it silently broke the contract key: the PUBLISHED promptfoo
    0.123.1 leaves `tests: [file://cases/a.yaml, ...]` in the export as RAW
    STRINGS, while the git checkout resolves them into dicts. Hashing
    config.tests therefore hashed an empty list -- every suite got the same
    dataset_sha, `n_cases_expected` came out 0, and two completely different
    golden sets compared as one contract. No error, no warning.

    row.testCase is the resolved test (vars, assert, options), so hash what
    ran rather than what was declared. That is the better question anyway:
    a case that was filtered out did not participate in the measurement.
    """
    out = {}
    for r in raw_rows:
        tc = r.get("testCase")
        if not isinstance(tc, dict):
            continue
        cid = (tc.get("vars") or {}).get("case_id")
        # One entry per case: --repeat N gives N rows carrying the same test.
        out.setdefault(cid, {"vars": tc.get("vars"), "assert": tc.get("assert"),
                             "options": tc.get("options")})
    return list(out.values())


def _tests(cfg, tests=None) -> list[dict]:
    # `tests` empty means the rows taught us nothing (a zero-row export), not
    # that the suite is empty -- fall back to the declared config and let the
    # caller's own emptiness check decide.
    if tests:
        return tests
    t = cfg.get("tests")
    if not isinstance(t, list):
        raise Integrity(f"config.tests is {type(t).__name__}, not a resolved list")
    return [x for x in t if isinstance(x, dict)]


def _case_id(test: dict):
    return (test.get("vars") or {}).get("case_id")


def dataset_sha(cfg, tests=None) -> str:
    """Hash of the golden set: every case_id and the vars that drive it."""
    items = []
    for t in _tests(cfg, tests):
        v = dict(t.get("vars") or {})
        v.pop("case_id", None)
        items.append([_case_id(t), v])
    return sha(sorted(items, key=canon))


def assertions_sha(cfg, tests=None) -> str:
    """Hash of what counts as passing. Kept apart from the dataset hash so a
    reviewer can see *which* half of the contract moved."""
    items = [[_case_id(t), t.get("assert")] for t in _tests(cfg, tests)]
    return sha([sorted(items, key=canon), cfg.get("defaultTest")])


def _assert_types(node) -> list[str]:
    if isinstance(node, dict):
        out = [node["type"]] if isinstance(node.get("type"), str) else []
        return out + _assert_types(node.get("assert"))
    if isinstance(node, list):
        return [t for n in node for t in _assert_types(n)]
    return []


def judge_snapshot(cfg, tests=None) -> str | None:
    """The pinned grader, or None. A recorded judge nobody enforced is an
    intention; this reads the enforcement -- defaultTest.options.provider, or
    the per-test options promptfoo resolved onto every row."""
    prov = ((cfg.get("defaultTest") or {}).get("options") or {}).get("provider")
    if not prov:
        seen = {canon((t.get("options") or {}).get("provider"))
                for t in (tests or []) if (t.get("options") or {}).get("provider")}
        if len(seen) == 1:
            prov = json.loads(seen.pop())
        elif len(seen) > 1:
            raise Integrity(f"tests disagree about the judge: {sorted(seen)}; "
                            "one suite must have one grader or the pass rate "
                            "is a mix of two measurements")
    if isinstance(prov, dict):
        prov = prov.get("id")
    return prov or os.environ.get("REGRESSGATE_JUDGE") or None


def pinned_version() -> str:
    with open(os.path.join(HERE, "promptfoo.version")) as f:
        return f.read().strip()


def contract_key(cfg, tests=None) -> dict:
    resolved = _tests(cfg, tests)
    if not resolved:
        # Never hash an empty set into a key that is supposed to identify a
        # golden set: every suite would collide and the gate would happily
        # compare two different experiments.
        raise Integrity("no resolved tests in the export; refusing to build a "
                        "contract key that would be identical for every suite")
    types = set(_assert_types([t.get("assert") for t in resolved]))
    types |= set(_assert_types((cfg.get("defaultTest") or {}).get("assert")))
    judge = judge_snapshot(cfg, resolved)
    if types & MODEL_GRADED and not judge:
        raise Integrity(
            f"suite uses model-graded assertions {sorted(types & MODEL_GRADED)} but no judge "
            "is pinned; llm-rubric would pick its grader from the ambient API keys")
    return {
        "suite": cfg.get("description") or "",
        "dataset_sha": dataset_sha(cfg, resolved),
        "assertions_sha": assertions_sha(cfg, resolved),
        "judge_snapshot": judge or "none:no-model-graded-assertions",
        "promptfoo_version": pinned_version(),
    }


# --------------------------------------------------------------------------- #
# loading + pairing
# --------------------------------------------------------------------------- #
def load(path: str) -> dict:
    with open(path) as f:
        doc = json.load(f)
    res = doc.get("results") or {}
    raw = res.get("results") or []
    rows = [parse.normalize_row(r) for r in raw]
    stats = res.get("stats") or {}
    return {
        "eval_id": doc.get("evalId"),
        "config": doc.get("config") or {},
        "tests": tests_from_rows(raw),
        "rows": rows,
        # stats.errors is the contract; the row count is the fallback if a future
        # version drops the key. They must agree, and disagreeing is itself a bug.
        "errors": stats.get("errors", sum(r.state == parse.ERROR for r in rows)),
        "stats": stats,
    }


def slots(rows) -> dict:
    """{(case_id, prompt_idx, repeat_slot): Row} over scorable rows only."""
    groups: dict = {}
    for r in rows:
        if r.state in (parse.ERROR, parse.UNSCORED):
            continue
        if r.case_id is None:
            raise Integrity("a scorable row has no `case_id` var; pairing needs an "
                            "explicit case id, and testIdx is not one")
        groups.setdefault((r.case_id, r.prompt_idx), []).append(r)

    out = {}
    for (cid, pidx), rs in groups.items():
        if all(r.repeat_index is not None for r in rs):
            keys = [r.repeat_index for r in rs]
            if len(set(keys)) != len(keys):
                raise Integrity(
                    f"case {cid!r} prompt {pidx} echoed duplicate repeatIndex "
                    f"{sorted(keys)}; the provider echo is wrong and the repeats "
                    "cannot be told apart")
        else:
            keys = list(range(len(rs)))  # file order; repeats are exchangeable
        for k, r in zip(keys, rs):
            out[(cid, pidx, k)] = r
    return out


def make_pairs(base_rows, head_rows):
    b, h = slots(base_rows), slots(head_rows)
    common = sorted(set(b) & set(h), key=lambda k: (k[0], k[1] or 0, k[2]))
    pairs = [{"pair_id": f"{cid}#{pidx}#{slot}", "case_id": cid,
              "baseline_pass": b[(cid, pidx, slot)].state == parse.PASSED,
              "head_pass": h[(cid, pidx, slot)].state == parse.PASSED}
             for cid, pidx, slot in common]
    return pairs, {"baseline_only": len(set(b) - set(h)),
                   "head_only": len(set(h) - set(b)),
                   "paired": len(common)}


def unscored(rows) -> int:
    return sum(r.state == parse.UNSCORED for r in rows)


# --------------------------------------------------------------------------- #
# manifest + quarantine
# --------------------------------------------------------------------------- #
def build_manifest(head: dict) -> dict:
    cfg, tests = head["config"], head.get("tests")
    ids = sorted({_case_id(t) for t in _tests(cfg, tests)} - {None})
    if not ids:
        raise Integrity("no case_ids in the export; a manifest of zero cases "
                        "would make the gate's count check vacuous")
    return {"suite": cfg.get("description") or "", "n_cases_expected": len(ids),
            "dataset_sha": dataset_sha(cfg, tests), "case_ids": ids}


def read_quarantine(path, n_cases, contract_key=None):
    """-> (excluded case ids, power_floor_ok). The cap covers the UNION of the
    quarantine set and the flaky watchlist -- capping them separately is how the
    watchlist alone reaches 30 cases in a year and costs 11 points of power
    at -5pp.

    power_floor_ok rides along because quarantine.json is where the last
    measurement of it lives. None means nobody has measured it, which is NOT the
    same as passing and must not be silently treated as passing -- including
    when there is no quarantine file at all.
    """
    if not path or not os.path.exists(path):
        return [], None
    with open(path) as f:
        q = json.load(f)
    # A measurement belongs to the suite it was taken on. Exclusions and the
    # power floor from an older golden set, judge or pin say nothing about this
    # one, so a mismatched contract is "unmeasured" -- never carried over.
    qk = q.get("contract_key")
    if contract_key is not None and qk is not None and qk != contract_key:
        print("::warning::quarantine.json was measured on a different contract "
              f"{json.dumps(qk, sort_keys=True)}; ignoring it. Re-run the A/A "
              "replays through quarantine.py on this suite.", file=sys.stderr)
        return [], None
    ids = sorted({*(q.get("quarantined") or []), *(q.get("watchlist") or [])})
    power_ok = q.get("power_ok")
    if q.get("churn_ok") is False:
        # The churn ceiling STOPS the gate rather than informing it. quarantine.py
        # already exits 1 on this, but the json it wrote is still what the PR
        # path reads, so the stop has to be enforced here too.
        print("::warning::A/A churn is over the ceiling in quarantine.json; the "
              "gate runs comment-only until the suite is stabilised", file=sys.stderr)
        power_ok = False
    if power_ok is not None and _stale(q.get("measured_at")):
        print(f"::warning::power floor last measured {q.get('measured_at')}, over "
              f"{POWER_MAX_AGE_DAYS} days ago; treating it as unmeasured", file=sys.stderr)
        power_ok = None
    cap = math.floor(QUARANTINE_CAP * n_cases) if n_cases else 0
    if n_cases and len(ids) > cap:
        raise SystemExit(
            f"::error::quarantine+watchlist holds {len(ids)} of {n_cases} cases "
            f"({len(ids)/n_cases:.1%}), over the {QUARANTINE_CAP:.0%} cap ({cap}). "
            "Excluding more is how a suite goes blind while every other metric "
            "improves. Fix or delete cases instead of excluding them.")
    return ids, power_ok


def _stale(measured_at):
    if not measured_at:
        return True
    try:
        t = datetime.strptime(measured_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - t > timedelta(days=POWER_MAX_AGE_DAYS)


def git_sha(explicit=None):
    if explicit:
        return explicit
    for env in ("REGRESSGATE_HEAD_SHA", "GITHUB_SHA"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# --------------------------------------------------------------------------- #
def build(head_path, baseline_path, manifest_path=None, quarantine_path=None,
          head_sha=None, baseline_sha=None):
    doc = {"harness_error": None, "n_cases_expected": None, "quarantined": [],
           "power_floor_ok": None, "baseline": None, "head": None, "pairs": []}
    try:
        head = load(head_path)
        doc["head"] = {"eval_id": head["eval_id"], "git_sha": git_sha(head_sha),
                       "contract_key": contract_key(head["config"], head.get("tests")),
                       "errors": head["errors"], "unscored": unscored(head["rows"])}

        if manifest_path and os.path.exists(manifest_path):
            with open(manifest_path) as f:
                doc["n_cases_expected"] = json.load(f)["n_cases_expected"]
        else:
            doc["n_cases_expected"] = len(build_manifest(head)["case_ids"])
        doc["quarantined"], doc["power_floor_ok"] = read_quarantine(
            quarantine_path, doc["n_cases_expected"], doc["head"]["contract_key"])

        # A missing baseline is a REFUSE, not an error: gate.py says so in the PR
        # comment rather than guessing at a comparison.
        if not baseline_path or not os.path.exists(baseline_path):
            return doc
        base = load(baseline_path)
        doc["baseline"] = {"eval_id": base["eval_id"], "git_sha": baseline_sha,
                           "contract_key": contract_key(base["config"], base.get("tests")),
                           "errors": base["errors"], "unscored": unscored(base["rows"])}
        doc["pairs"], doc["coverage"] = make_pairs(base["rows"], head["rows"])
    except Integrity as e:
        doc["harness_error"] = str(e)
    except (OSError, json.JSONDecodeError, KeyError) as e:
        doc["harness_error"] = f"unreadable eval output: {e!r}"
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--head")
    ap.add_argument("--baseline")
    ap.add_argument("--manifest")
    ap.add_argument("--quarantine")
    ap.add_argument("--head-sha")
    ap.add_argument("--baseline-sha")
    ap.add_argument("--out")
    ap.add_argument("--write-manifest", metavar="PATH")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)

    if a.selfcheck:
        return _selfcheck()
    if not a.head:
        ap.error("--head is required")

    if a.write_manifest:
        m = build_manifest(load(a.head))
        with open(a.write_manifest, "w") as f:
            json.dump(m, f, indent=2)
            f.write("\n")
        print(f"wrote {a.write_manifest}: {m['n_cases_expected']} cases, "
              f"dataset_sha={m['dataset_sha']}")
        return 0

    doc = build(a.head, a.baseline, a.manifest, a.quarantine, a.head_sha, a.baseline_sha)
    out = json.dumps(doc, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(out + "\n")
    cov = doc.get("coverage") or {}
    print(f"pairs={len(doc['pairs'])} cases_expected={doc['n_cases_expected']} "
          f"quarantined={len(doc['quarantined'])} baseline="
          f"{'yes' if doc['baseline'] else 'MISSING'} coverage={cov} "
          f"harness_error={doc['harness_error']}")
    return 0


# --------------------------------------------------------------------------- #
def _row(case, pidx=0, rep=None, state=parse.PASSED):
    return parse.Row(case_id=case, prompt_idx=pidx, repeat_index=rep, state=state,
                     success=state == parse.PASSED, score=None, model_id="m",
                     latency_ms=None, cost=None, error=None)


def published_export(cases, desc="s"):
    """An export in the shape the PUBLISHED promptfoo 0.123.1 writes: config.tests
    is the raw `file://` string and only row.testCase says what ran. Every
    module that builds a contract key from an export must self-check against
    THIS shape -- two call sites were missed by the first fix and died on the
    first real run. cases: [(case_id, question, expected_substring), ...]."""
    return {"evalId": "e", "config": {"description": desc,
                                      "tests": ["file://cases/a.yaml"]},
            "results": {"stats": {"successes": len(cases), "failures": 0, "errors": 0},
                        "results": [
                {"testCase": {"vars": {"case_id": c, "q": q},
                              "assert": [{"type": "icontains", "value": a}],
                              "options": {"provider": "openai:gpt-4o-2024-11-20"}},
                 "vars": {"case_id": c}, "promptIdx": 0, "success": True,
                 "gradingResult": {"componentResults": [
                     {"assertion": {"type": "icontains"}, "pass": True}]}}
                for c, q, a in cases]}}


def _selfcheck():
    P, F, E, U = parse.PASSED, parse.FAILED, parse.ERROR, parse.UNSCORED

    # echoed repeat indices pair slot-for-slot
    base = [_row("a", 0, 0, P), _row("a", 0, 1, P), _row("b", 0, 0, P)]
    head = [_row("a", 0, 0, P), _row("a", 0, 1, F), _row("b", 0, 0, P)]
    pairs, cov = make_pairs(base, head)
    assert cov == {"baseline_only": 0, "head_only": 0, "paired": 3}, cov
    assert sum(p["baseline_pass"] and not p["head_pass"] for p in pairs) == 1

    # no echo at all -> ordinal slots, still pairs
    base = [_row("a", 0, None, P), _row("a", 0, None, P)]
    head = [_row("a", 0, None, P), _row("a", 0, None, F)]
    pairs, cov = make_pairs(base, head)
    assert cov["paired"] == 2 and sum(not p["head_pass"] for p in pairs) == 1

    # uneven repeats: pair what overlaps, report the rest, never invent a pair
    pairs, cov = make_pairs([_row("a", 0, i, P) for i in range(5)],
                            [_row("a", 0, i, P) for i in range(3)])
    assert (cov["paired"], cov["baseline_only"], cov["head_only"]) == (3, 2, 0), cov

    # ERROR and UNSCORED rows are never pairs
    pairs, _ = make_pairs([_row("a", 0, 0, P), _row("b", 0, 0, P)],
                          [_row("a", 0, 0, E), _row("b", 0, 0, U)])
    assert pairs == [], pairs

    # a duplicated echoed index is a harness error, not a silently dropped row
    try:
        slots([_row("a", 0, 0, P), _row("a", 0, 0, P)])
        raise AssertionError("duplicate repeatIndex must raise")
    except Integrity as e:
        assert "duplicate repeatIndex" in str(e)

    # a scorable row with no case_id is a harness error
    try:
        slots([_row(None, 0, 0, P)])
        raise AssertionError("missing case_id must raise")
    except Integrity as e:
        assert "case_id" in str(e)

    # an unpinned judge over model-graded assertions refuses to produce a key
    cfg = {"description": "s", "tests": [{"vars": {"case_id": "a"},
                                          "assert": [{"type": "llm-rubric", "value": "x"}]}]}
    try:
        contract_key(cfg)
        raise AssertionError("unpinned judge must raise")
    except Integrity as e:
        assert "no judge is pinned" in str(e)
    pinned = dict(cfg, defaultTest={"options": {"provider": "openai:gpt-4o-2024-11-20"}})
    assert contract_key(pinned)["judge_snapshot"] == "openai:gpt-4o-2024-11-20"

    # the two halves of the contract move independently
    c1 = {"description": "s", "tests": [{"vars": {"case_id": "a", "q": "1"},
                                         "assert": [{"type": "contains", "value": "x"}]}]}
    c2 = json.loads(json.dumps(c1)); c2["tests"][0]["vars"]["q"] = "2"
    c3 = json.loads(json.dumps(c1)); c3["tests"][0]["assert"][0]["value"] = "y"
    k1, k2, k3 = contract_key(c1), contract_key(c2), contract_key(c3)
    assert k1["dataset_sha"] != k2["dataset_sha"] and k1["assertions_sha"] == k2["assertions_sha"]
    assert k1["dataset_sha"] == k3["dataset_sha"] and k1["assertions_sha"] != k3["assertions_sha"]
    # case order in the config must not change the hash
    c4 = {"description": "s", "tests": list(reversed([
        {"vars": {"case_id": "a"}}, {"vars": {"case_id": "b"}}]))}
    c5 = {"description": "s", "tests": [{"vars": {"case_id": "b"}}, {"vars": {"case_id": "a"}}]}
    assert dataset_sha(c4) == dataset_sha(c5)

    # THE REGRESSION TEST FOR THE SILENT CONTRACT-KEY COLLAPSE.
    # The published promptfoo leaves `tests: [file://...]` as raw strings in
    # the export. Hashing config.tests then hashed an EMPTY list, so every
    # suite produced the same dataset_sha and n_cases_expected came out 0 --
    # a contract key that cannot tell two golden sets apart, failing silently.
    # The fix reads row.testCase instead; these assertions pin it.
    _export = published_export
    import tempfile as _tf
    def _write(cases, desc="s"):
        pth = os.path.join(_tf.mkdtemp(), "e.json")
        with open(pth, "w") as fh:
            json.dump(_export(cases, desc), fh)
        return pth

    ex1 = load(_write([("a", "q1", "x"), ("b", "q2", "y")]))
    assert len(ex1["tests"]) == 2, ex1["tests"]
    k1 = contract_key(ex1["config"], ex1["tests"])
    assert k1["judge_snapshot"] == "openai:gpt-4o-2024-11-20", k1
    # a DIFFERENT golden set must not collide with the first
    ex2 = load(_write([("a", "q1", "x"), ("c", "q3", "z")]))
    k2 = contract_key(ex2["config"], ex2["tests"])
    assert k1["dataset_sha"] != k2["dataset_sha"], "two golden sets collided"
    # the manifest must count the real cases, never zero
    m = build_manifest(ex1)
    assert m["n_cases_expected"] == 2 and m["case_ids"] == ["a", "b"], m
    # --repeat must not multiply the case count
    rep = _export([("a", "q1", "x"), ("a", "q1", "x"), ("b", "q2", "y")])
    pth = os.path.join(_tf.mkdtemp(), "r.json")
    with open(pth, "w") as fh:
        json.dump(rep, fh)
    assert build_manifest(load(pth))["n_cases_expected"] == 2
    # an export with no resolvable tests must RAISE, never hash an empty set
    empty = {"evalId": "e", "config": {"description": "s", "tests": ["file://x.yaml"]},
             "results": {"stats": {"errors": 0}, "results": []}}
    pth = os.path.join(_tf.mkdtemp(), "empty.json")
    with open(pth, "w") as fh:
        json.dump(empty, fh)
    exE = load(pth)
    try:
        contract_key(exE["config"], exE["tests"])
        raise AssertionError("an empty test set must not produce a contract key")
    except Integrity as e:
        assert "identical for every suite" in str(e)

    # no quarantine file at all is "unmeasured", not "fine"
    assert read_quarantine(None, 300) == ([], None)
    assert read_quarantine("/nonexistent/q.json", 300) == ([], None)

    # a stale power measurement is not a passing one
    assert _stale(None) and _stale("garbage")
    assert not _stale(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert _stale((datetime.now(timezone.utc) - timedelta(days=POWER_MAX_AGE_DAYS + 1))
                  .strftime("%Y-%m-%dT%H:%M:%SZ"))
    import tempfile
    qp = os.path.join(tempfile.mkdtemp(), "q.json")
    with open(qp, "w") as f:
        json.dump({"quarantined": ["a"], "watchlist": ["b"], "power_ok": True,
                   "measured_at": "2020-01-01T00:00:00Z"}, f)
    ids, ok = read_quarantine(qp, 100)
    assert ids == ["a", "b"] and ok is None, (ids, ok)

    # a measurement from a DIFFERENT contract is ignored outright: not its
    # exclusions, not its power verdict
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(qp, "w") as f:
        json.dump({"quarantined": ["a"], "power_ok": True, "churn_ok": True,
                   "measured_at": now, "contract_key": {"dataset_sha": "old"}}, f)
    assert read_quarantine(qp, 100, {"dataset_sha": "new"}) == ([], None)
    assert read_quarantine(qp, 100, {"dataset_sha": "old"}) == (["a"], True)
    assert read_quarantine(qp, 100) == (["a"], True)   # no key to compare: trust it

    # the churn ceiling stops the gate even when power alone would pass
    with open(qp, "w") as f:
        json.dump({"power_ok": True, "churn_ok": False, "measured_at": now}, f)
    assert read_quarantine(qp, 100) == ([], False)

    print("pair selfcheck OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
