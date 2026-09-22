#!/usr/bin/env python3
"""Phase 6: triage -- turn the gate's list of broken case ids into something a
human can read at 9am. READ-ONLY, and it only ever APPENDS to the comment
gate.py already rendered.

It decides nothing: no verdict, no cache write, no quarantine write, no exit
code the gate reads. The store is opened `mode=ro` so that stays true by
construction rather than by good intentions.

Two things it does that a list of ids cannot:

  grouping    a judge that degrades breaks many cases with near-identical
              rationale text -- that is the shape of the measured composed
              failure (churn 2% -> 20%, power@-5pp 0.878 -> 0.192). Eleven ids
              read as eleven bugs; the same eleven under one rationale read as
              one. The signal is the failing assertion type plus a
              digit-stripped rationale prefix. No clustering library, no model
              call, nothing that can itself be wrong in an interesting way.
  flaky split a case the store has already seen flip on its own is a different
              conversation from one that never has, and it is exactly the case
              a reader is tempted to quarantine on the spot. Measured:
              quarantining on any flip drains a 400-case suite to 145 in a year
              and power@-5pp 0.866 -> 0.405. So the flag is printed and nothing
              is excluded -- the reading aid must not become the exclusion path.

Works in CASES, not paired samples: with --repeat 3 one broken case is three
broken pairs, and nobody wants to read the same rationale three times.

    python3 triage.py --pairing p.json --db runs.db --run-id <id> --comment comment.md
    python3 triage.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

import gate

EXCERPT = 180          # one blockquote line, not a transcript
PREFIX = 60            # how much normalised rationale has to match to be "the same thing"
NO_REASON = "_no rationale recorded in the run store_"


def open_ro(path):
    """Read-only handle. Hard constraint 1 says triage never writes; sqlite can
    enforce that for free, so let it."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _norm(text):
    """Digits and punctuation out, so 'expected 30 days, saw 14' and 'expected
    30 days, saw 7' land in one group -- same bug, different numbers in it."""
    return " ".join(re.sub(r"[^a-z]+", " ", (text or "").lower()).split())[:PREFIX]


def _excerpt(text):
    t = " ".join((text or "").split())
    return t[:EXCERPT] + ("..." if len(t) > EXCERPT else "")


def failures(db, run_id):
    """{case_id: (assertion type, rationale)} for the head run's failed cases.

    The per-assertion reason beats gradingResult.reason: on a multi-assert case
    the latter is a summary of the whole set, and the reader wants the leaf that
    actually said no.
    """
    out = {}
    for r in db.execute(
            "SELECT c.case_id, c.judge_reason, a.type AS atype, a.reason AS areason"
            " FROM case_results c LEFT JOIN assertion_results a"
            "   ON a.case_result_id = c.id AND a.pass = 0"
            " WHERE c.run_id = ? AND c.status = 'FAILED'"
            " ORDER BY c.case_id, a.leaf_idx", (run_id,)):
        # First failing leaf wins, but a later repeat that DID record a reason
        # beats an earlier one that recorded nothing.
        if out.get(r["case_id"], (None, None))[1]:
            continue
        out[r["case_id"]] = (r["atype"], r["areason"] or r["judge_reason"])
    return out


def flips(db, run_id):
    """{case_id: (runs it failed in, runs it passed in, runs it appeared in)} for
    cases that disagreed with themselves BEFORE this run.

    Scoped to runs that are genuine replays of the head run -- same commit, same
    golden set, same judge, same assertion config, same model, and a run that
    produced results at all. Without that scope this is not flakiness but "the
    case ever changed state", and the two permanent, monotonic sources of that
    are a bug that was fixed and a recorded judge/model/golden-set change. Both
    would accumulate forever off nightly history, and each one costs a real
    failure its weight (section 7.6: any snapshot change invalidates the
    comparison; store.BASELINE_SQL and quarantine.analyse refuse it too).

    Counted in runs, not rows: with --repeat 3 a stable failure is three failed
    rows and that is not evidence of instability. Two repeats of one run
    disagreeing IS, and this catches that too.
    """
    return {r["case_id"]: (r["fail_runs"], r["pass_runs"], r["runs"]) for r in db.execute(
        "SELECT c.case_id,"
        " count(DISTINCT CASE WHEN c.status='FAILED' THEN c.run_id END) AS fail_runs,"
        " count(DISTINCT CASE WHEN c.status='PASSED' THEN c.run_id END) AS pass_runs,"
        " count(DISTINCT c.run_id) AS runs"
        " FROM case_results c JOIN runs r ON r.run_id = c.run_id"
        " JOIN runs h ON h.run_id = ?"
        " WHERE c.run_id <> h.run_id AND c.status IN ('PASSED','FAILED')"
        "   AND r.git_sha = h.git_sha"
        "   AND r.golden_set_hash = h.golden_set_hash"
        "   AND r.judge_snapshot = h.judge_snapshot"
        "   AND r.assertion_config_hash = h.assertion_config_hash"
        "   AND r.model_under_test_snapshot = h.model_under_test_snapshot"
        "   AND r.outcome IN ('OK','TESTS_FAILED')"
        " GROUP BY c.case_id HAVING fail_runs > 0 AND pass_runs > 0", (run_id,))}


def group(broken, fails, flaky):
    """-> ([((type, normalised prefix), [(case_id, rationale), ...]), ...], flaky rows).

    Biggest group first: it is the one sentence that explains the most red.
    """
    groups, flaky_rows = {}, []
    for cid in broken:
        atype, reason = fails.get(cid, (None, None))
        if cid in flaky:
            flaky_rows.append((cid, flaky[cid], atype or "unknown", reason))
        else:
            groups.setdefault((atype or "unknown", _norm(reason)), []).append((cid, reason))
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])), flaky_rows


def render(run_id, groups, flaky_rows):
    n_stable = sum(len(v) for _, v in groups)
    L = ["", "---", "", f"### Triage: {n_stable + len(flaky_rows)} newly failing case(s)", ""]

    if groups:
        L += [f"{len(groups)} distinct failure(s) across {n_stable} case(s) with no history "
              "of flipping:", ""]
    for (atype, _), members in groups:
        _, reason = members[0]
        L += [f"**{len(members)} case(s) -- `{atype}`**",
              "> " + (_excerpt(reason) if reason else NO_REASON),
              # gate._ids so the ids here render exactly as the ids above them.
              gate._ids(c for c, _ in members), ""]

    if flaky_rows:
        L += [f"**{len(flaky_rows)} case(s) that were already unstable** -- these have flipped "
              "on their own before, so a failure here is weaker evidence. It is not a reason to "
              "quarantine them: quarantining on any flip drains a 400-case suite to 145 in a "
              "year and takes power@-5pp from 0.866 to 0.405.", ""]
        for cid, (fail_runs, pass_runs, runs), atype, reason in flaky_rows:
            # both halves of the disagreement: "failed 1 of 1" reads as "always
            # red", which is the opposite of what this section is saying.
            L.append(f"- `{cid}` failed in {fail_runs} and passed in {pass_runs} of {runs} "
                     f"earlier run(s) -- `{atype}`: "
                     + (_excerpt(reason) if reason else NO_REASON))
        L.append("")

    L += [f"_Triage read the rationales stored for run `{run_id}`. It is a reading aid: it did "
          "not compute, change or re-check the verdict above._"]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairing", required=True, help="the pairing JSON gate.py decided on")
    ap.add_argument("--db", required=True, help="run store (opened read-only)")
    ap.add_argument("--run-id", required=True, help="the HEAD run in the store")
    ap.add_argument("--comment", required=True, help="gate.py's comment; appended to in place")
    a = ap.parse_args(argv)

    # A reading aid must never be able to fail a build, and 0 is the only exit
    # code it is allowed to have: 20/30/40/50 belong to the gate.
    try:
        # Only a BLOCK or a COMMENT has broken cases to explain. Under a REFUSE, a
        # HARD_FAIL or a HARNESS_ERROR the gate computed no verdict at all, and
        # under a PASS it said "merge away" -- a list of newly failing cases under
        # any of those contradicts the comment instead of annotating it. This reads
        # what the gate SAID, including a replayed cached verdict; it never
        # recomputes the decision. Read inside the try: gate._HEAD is private and
        # deliberately depended on, and reshaping it must not crash a reading aid.
        with open(a.comment) as f:
            said = f.readline().strip()
        if said not in (gate._HEAD["BLOCK"], gate._HEAD["COMMENT"]):
            print(f"triage: gate said {said!r}; nothing to annotate")
            return 0
        with open(a.pairing) as f:
            broken = gate.tally(json.load(f)["pairs"])["broken_ids"]
        if not broken:
            print("triage: nothing broke; comment left untouched")
            return 0
        db = open_ro(a.db)
        # An unknown run id is not an empty run: failures() would return nothing
        # and flips() would stop excluding the head, so every broken case would be
        # declared already-unstable on the head's own rows. Say nothing instead.
        if not db.execute("SELECT 1 FROM runs WHERE run_id = ?", (a.run_id,)).fetchone():
            raise ValueError(f"run {a.run_id!r} is not in the store")
        groups, flaky_rows = group(broken, failures(db, a.run_id), flips(db, a.run_id))
        with open(a.comment, "a") as f:          # append: the bytes above are the gate's
            f.write(render(a.run_id, groups, flaky_rows))
    except Exception as e:                       # noqa: BLE001 -- see below
        # Deliberately everything: constraint 1 is that no triage failure can reach
        # the exit code, and an enumerated tuple is a list of the failures someone
        # already thought of (a TypeError out of a malformed pairing was not).
        print(f"::warning::triage skipped: {e!r}", file=sys.stderr)
        return 0
    print(f"triage: {len(broken)} broken case(s) -> {len(groups)} group(s), "
          f"{len(flaky_rows)} already-unstable, appended to {a.comment}")
    return 0


# --------------------------------------------------------------------------- #
def _selfcheck():
    import os
    import tempfile

    import store

    d = tempfile.mkdtemp()
    dbp, cp, pp = (os.path.join(d, x) for x in ("t.db", "comment.md", "p.json"))
    db = store.open_db(dbp)

    def run(rid, at, **off):
        """An earlier run. Every keyword is one of the six fields flips() scopes
        on, so a fixture run can differ from head in exactly ONE of them."""
        scope = dict(git_sha="s", judge_snapshot="j", golden_set_hash="g",
                     assertion_config_hash="a", model_under_test_snapshot="m",
                     outcome="TESTS_FAILED")
        scope.update(off)
        store.add_run(db, run_id=rid, branch="main", started_at=store.ago(at), trigger="pr",
                      exit_code=0, n_cases_expected=100, n_cases_seen=100,
                      successes=84, failures=16, errors=0, pass_rate=84.0, **scope)

    def case(rid, cid, status, atype=None, areason=None, judge=None, rep=None, leaves=None):
        """leaves = [(type, pass, reason), ...] in leaf order; atype/areason is
        the one-leaf shorthand for it."""
        cur = db.execute("INSERT INTO case_results (run_id, case_id, prompt_idx, repeat_index,"
                         " status, judge_reason) VALUES (?,?,0,?,?,?)",
                         (rid, cid, rep, status, judge))
        if leaves is None:
            leaves = [(atype, 0 if status == "FAILED" else 1, areason)] if atype else []
        for i, (t, p, why) in enumerate(leaves):
            db.execute("INSERT INTO assertion_results (case_result_id, leaf_idx, type, pass,"
                       " reason) VALUES (?,?,?,?,?)", (cur.lastrowid, i, t, p, why))

    # one real shape of a bad morning. Every id says what it is here to show:
    # 9 cases broken by one judge complaint (the last of them differing only in
    # capitals), 2 by a different assertion, 2 `twin`s whose rationales agree
    # past the grouping threshold and 2 `cousin`s that agree for eleven
    # characters, 1 with nothing recorded, 1 multi-assert, 1 whose reason landed
    # on a later repeat, 1 whose reason is only on the judge, 1 always red, and
    # 4 that really have disagreed with themselves.
    rubric = [f"rubric-{i}" for i in range(8)] + ["rubric-lowercase"]
    contains = [f"contains-{i}" for i in range(2)]
    threshold = ["twin-early", "twin-late", "cousin-a", "cousin-b"]
    stable = rubric + contains + threshold + ["silent", "multi", "always-red",
                                              "repeat", "judge-only"]
    flaky_ids = ["flip", "stale", "wobble", "lean"]
    broken = stable + flaky_ids

    run("head", 1)
    for i, cid in enumerate(rubric[:8]):
        case("head", cid, "FAILED", "llm-rubric",
             f"Response omits the refund window (expected 30 days, saw {i + 2}).")
    # the same complaint with different capitals -- judges vary them. One bug,
    # so one group; and being last in it, its text is the wrong one to print.
    case("head", "rubric-lowercase", "FAILED", "llm-rubric",
         "response omits the refund window (expected 30 days, saw 10).")
    for cid in contains:
        case("head", cid, "FAILED", "contains", "Expected output to contain \"ticket id\"")

    # PREFIX has to be wrong in both directions to be worth a threshold: the
    # twins agree for more than PREFIX normalised characters and are one bug;
    # the cousins agree for eleven and are two.
    twin = "The judge says the assistant hedged instead of answering, and then cited "
    case("head", "twin-early", "FAILED", "llm-rubric", twin + "a 30-day window.")
    case("head", "twin-late", "FAILED", "llm-rubric", twin + "no window at all.")
    case("head", "cousin-a", "FAILED", "llm-rubric",
         "Expected a ticket id somewhere in the reply.")
    case("head", "cousin-b", "FAILED", "llm-rubric",
         "Expected a refund window somewhere in the reply.")

    case("head", "silent", "FAILED")                      # no assertion row, no judge_reason
    case("head", "always-red", "FAILED", "contains", "Never passed here, not once.")
    # the failing leaf recorded nothing and the judge did: that is where
    # javascript/python/equals leaves put their explanation.
    case("head", "judge-only", "FAILED", "llm-rubric", None,
         judge="Judge: the refund window is wrong.")
    # --repeat 3 head rows: repeat 0 recorded no reason, repeat 1 did.
    case("head", "repeat", "FAILED", "llm-rubric", None, rep=0)
    case("head", "repeat", "FAILED", "llm-rubric",
         "The second repeat recorded what the first did not.", rep=1)
    # red in the head run but red at baseline too, so the pairing does not call
    # it a regression. Triage explains the PAIRING, not everything the store saw.
    case("head", "pre-existing", "FAILED", "contains", "Was already red before this PR.")
    for cid in ("flip", "wobble"):
        case("head", cid, "FAILED", "llm-rubric", "Tone is dismissive.")
    case("head", "lean", "FAILED", "llm-rubric", "Answer drifts off topic.")
    case("head", "stale", "FAILED")                       # rationale only in an EARLIER run
    # the leaf that said no beats a passing sibling, a later failing leaf, and the
    # whole-set judge summary -- all three are wrong answers the reader would read
    # as the cause.
    case("head", "multi", "FAILED", judge="Overall: 1 of 3 assertions failed.",
         leaves=[("contains", 1, "Assertion passed"),
                 ("llm-rubric", 0, "The reply invents a 60-day refund window."),
                 ("regex", 0, "Formatting is off.")])

    # history: three repeats per case per run, so a row count and a run count
    # cannot be confused. Stable cases passed; `always-red` never did; `flip` and
    # `stale` disagree across runs; `lean` is red in 2 of its 3; `wobble`
    # disagrees with itself inside prev1.
    for rid, at in (("prev1", 48), ("prev2", 24)):
        run(rid, at)
        for cid in stable + ["flip", "stale", "lean"]:
            for rep in range(3):
                st = "PASSED"
                if cid in ("always-red", "lean") or (cid == "flip" and rid == "prev2"):
                    st = "FAILED"
                elif cid == "stale" and rid == "prev1":
                    st = "FAILED"
                case(rid, cid, st, "llm-rubric",
                     "stale rationale from an earlier run" if cid == "stale" else "ok", rep=rep)
    for rep, st in enumerate(("PASSED", "FAILED", "PASSED")):
        case("prev1", "wobble", st, "llm-rubric", "ok", rep=rep)
    # a third replay, so the two headline numbers are not interchangeable:
    # `lean` came back green here and is 2-of-3 red, not 1-of-1. `flip` only
    # ERRORed here -- an infra error measured nothing and must not pad `runs`.
    run("prev3", 12)
    case("prev3", "lean", "PASSED", "llm-rubric", "ok")
    case("prev3", "flip", "ERROR")

    # `rubric-0` was red in each of these and is green now: a bug that was fixed,
    # a newer judge, an edited golden set, a re-pinned model, a changed assertion
    # config, a run that errored. None is a replay of head, so none is flakiness.
    # One run per scoped field, differing in exactly THAT field, so no single
    # conjunct of the scope can be dropped without a fixed bug reading as a flip.
    off_contract = {"other-sha": {"git_sha": "s2"},
                    "other-golden": {"golden_set_hash": "g2"},
                    "other-judge": {"judge_snapshot": "j2"},
                    "other-assertions": {"assertion_config_hash": "a2"},
                    "other-model": {"model_under_test_snapshot": "m2"},
                    "errored": {"outcome": "HARNESS_ERROR"}}
    for i, (rid, field) in enumerate(off_contract.items()):
        run(rid, 72 + i, **field)
        case(rid, "rubric-0", "FAILED", "llm-rubric", "a bug that was fixed since")
    db.commit()

    # --repeat 3: one broken CASE is three broken PAIRS and the reader wants the
    # case. `pre-existing` is red on both sides, which is not a regression.
    suite = broken + ["pre-existing"] + [f"ok-{i}" for i in range(99 - len(broken))]
    doc = {"harness_error": None, "n_cases_expected": 100, "quarantined": [],
           "power_floor_ok": True,
           "baseline": {"eval_id": "b", "git_sha": "bbb", "contract_key": {"k": 1}, "errors": 0},
           "head": {"eval_id": "h", "git_sha": "hhh", "contract_key": {"k": 1}, "errors": 0},
           "pairs": [{"pair_id": f"{c}#0#{rep}", "case_id": c,
                      "baseline_pass": c != "pre-existing",
                      "head_pass": c not in broken and c != "pre-existing"}
                     for c in suite for rep in range(3)]}
    with open(pp, "w") as f:
        json.dump(doc, f)

    # append under the comment the gate really rendered for this pairing
    decision, detail = gate.decide(doc)
    assert decision == "BLOCK", decision
    original = (gate.render(decision, detail) + "\n").encode()
    with open(cp, "wb") as f:
        f.write(original)

    # the other inputs the runs below need, made now so that the only file that
    # may appear in `d` from here on is one triage wrote.
    refused, badp = os.path.join(d, "refuse.md"), os.path.join(d, "bad.json")
    noted = os.path.join(d, "comment-verdict.md")
    body = gate.render("REFUSE", {"why": "no baseline run recorded for this contract"})
    with open(refused, "w") as f:
        f.write(body + "\n")
    heads_up = gate._HEAD["COMMENT"] + "\n\nSomething moved, not enough to block.\n"
    with open(noted, "w") as f:
        f.write(heads_up)
    with open(badp, "w") as f:
        json.dump(["not", "a", "dict"], f)      # structurally wrong: pairs is not there
    files_before = set(os.listdir(d))

    assert main(["--pairing", pp, "--db", dbp, "--run-id", "head", "--comment", cp]) == 0
    with open(cp, "rb") as f:
        after = f.read()

    # 2. it APPENDS -- the gate's bytes are untouched, to the byte
    assert after[:len(original)] == original, "triage rewrote the gate's comment"
    section = after[len(original):].decode()
    # counted in CASES: 24 broken cases, not the 72 pairs they arrived as, and
    # not `pre-existing`, which the store calls red and the pairing does not.
    assert f"### Triage: {len(broken)} newly failing case(s)" in section, section
    assert "`pre-existing`" not in section, section

    fails, flaky = failures(open_ro(dbp), "head"), flips(open_ro(dbp), "head")
    # nothing measured off this run's contract may reach the flaky set. Checked
    # here by name, before the group sizes below shift under it (3 pins the rest).
    assert "rubric-0" not in flaky, f"a bug that was fixed read as a flip: {flaky}"
    groups, flaky_rows = group(broken, fails, flaky)
    # grouped, not 24 separate lines: the 9 rubric complaints (same bug, other
    # digits, other capitals) are one group, the 2 `contains` are one, the twins
    # are one, and the rest each stand alone -- cousins included.
    assert [len(v) for _, v in groups] == [9, 2, 2] + [1] * 7, [(k, len(v)) for k, v in groups]
    assert "**9 case(s) -- `llm-rubric`**" in section, section
    assert section.count("case(s) -- `") == 10, section
    # the one sentence that explains the most red is printed, in full, and it is
    # the FIRST member's: an empty blockquote or an arbitrary member's sentence
    # is the whole product silently gone.
    assert "> Response omits the refund window (expected 30 days, saw 2)." in section, section
    assert "saw 10" not in section, section

    # 2b. the rationale is the leaf that said no, in THIS run: not a passing
    # sibling leaf, not a later failing leaf, not the whole-set judge summary,
    # and not a reason recorded for the same case in some other run. Where the
    # leaf said nothing, the judge's sentence and a later repeat's both count.
    assert fails["multi"] == ("llm-rubric", "The reply invents a 60-day refund window."), fails
    assert fails["judge-only"] == ("llm-rubric",
                                   "Judge: the refund window is wrong."), fails["judge-only"]
    assert fails["repeat"] == ("llm-rubric",
                               "The second repeat recorded what the first did not."
                               ), fails["repeat"]
    assert fails["stale"] == (None, None), fails
    for wrong in ("Assertion passed", "Formatting is off", "1 of 3 assertions",
                  "stale rationale"):
        assert wrong not in section, wrong

    # 3. flaky reported SEPARATELY from stable, counted in RUNS and not in rows
    # (prev1/prev2 hold three repeats of every case), over runs that SCORED the
    # case (`flip` only ERRORed in prev3), and only over runs that are replays of
    # this one -- `rubric-0` was red under a different sha, judge, golden set,
    # assertion config or model, or in a run that errored, and every one of those
    # is a fixed bug or a changed contract, not a flip. `always-red` never
    # passed: always red is not unstable either.
    assert flaky == {"flip": (1, 1, 2), "stale": (1, 1, 2), "wobble": (1, 1, 1),
                     "lean": (2, 1, 3)}, flaky
    assert sorted(c for c, *_ in flaky_rows) == sorted(flaky_ids), flaky_rows
    assert not set(flaky_ids) & {c for _, v in groups for c, _ in v}
    assert ("`flip` failed in 1 and passed in 1 of 2 earlier run(s) -- `llm-rubric`: "
            "Tone is dismissive." in section), section
    # one earlier run whose repeats disagree: "failed 1 of 1" would read as always red
    assert "`wobble` failed in 1 and passed in 1 of 1 earlier run(s)" in section, section
    # and one that is asymmetric, so the two numbers cannot be swapped unseen:
    # "failed in 1 and passed in 2" would read as a case that is nearly always
    # green, the opposite of this one.
    assert "`lean` failed in 2 and passed in 1 of 3 earlier run(s)" in section, section
    assert "already unstable" in section
    stable_half = section.split("already unstable")[0]
    assert "`rubric-0`" in stable_half and "`always-red`" in stable_half, section

    # 4. a missing rationale is labelled, never invented -- in both sections
    assert NO_REASON in section, section
    assert "`silent`" in section
    assert "unknown" in section        # no assertion row either -> named as unknown
    assert "`None`" not in section, section

    # 1. read-only with respect to everything
    try:
        open_ro(dbp).execute("DELETE FROM case_results")
        raise AssertionError("the store must be opened read-only")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e), e

    # a broken wiring degrades to a warning, never to a nonzero exit the gate owns,
    # and never to a half-written section. Same pairing, so it really reaches the db.
    # A run id that is not in the store is not an empty run: unguarded it invents
    # a whole section saying every broken case is already unstable. A pairing that
    # is not shaped like a pairing raises a TypeError, which is still not allowed
    # to become an exit code the gate owns.
    for args in (["--run-id", "typo", "--pairing", pp], ["--run-id", "head", "--pairing", badp]):
        assert main(args + ["--db", dbp, "--comment", cp]) == 0, args
        with open(cp, "rb") as f:
            assert f.read() == after, f"triage must append nothing after {args}"

    # Both missing-file paths, because they raise DIFFERENT exceptions and the
    # `except Exception` in main() is what keeps either from becoming an exit
    # code the gate owns. A missing --db raises sqlite3.OperationalError; a
    # missing --comment raises FileNotFoundError. Assert only the OSError one
    # and narrowing the handler to a tuple of the obvious classes still passes.
    for missing in ("--db", "--comment"):
        argv = {"--pairing": pp, "--db": dbp, "--run-id": "head", "--comment": cp}
        argv[missing] = os.path.join(d, "gone.db")
        assert main([x for kv in argv.items() for x in kv]) == 0, missing
        with open(cp, "rb") as f:
            assert f.read() == after, f"a failed triage must not touch the comment ({missing})"

    # it annotates a verdict, it does not argue with one. A COMMENT has broken
    # cases to explain and is the verdict where the list is ALL the reader gets,
    # so it is annotated exactly like a BLOCK. Under a REFUSE the gate computed
    # no verdict, so broken cases there would contradict it.
    assert main(["--pairing", pp, "--db", dbp, "--run-id", "head", "--comment", noted]) == 0
    with open(noted) as f:
        annotated = f.read()
    assert annotated.startswith(heads_up), annotated
    assert "### Triage:" in annotated, "triage must annotate a COMMENT"

    main(["--pairing", pp, "--db", dbp, "--run-id", "head", "--comment", refused])
    with open(refused) as f:
        assert f.read() == body + "\n", "triage must not annotate a REFUSE"

    # nothing broke -> nothing appended, so a PASS comment cannot grow a triage section
    green = dict(doc, pairs=[{"pair_id": "a#0#0", "case_id": "a",
                              "baseline_pass": True, "head_pass": True}])
    with open(pp, "w") as f:
        json.dump(green, f)
    main(["--pairing", pp, "--db", dbp, "--run-id", "head", "--comment", cp])
    with open(cp, "rb") as f:
        assert f.read() == after, "a clean pairing must leave the comment alone"

    # 1b. NO CONTROL PATH: the comment is the only thing triage may write. A
    # quarantine suggestion, a watchlist JSONL, a verdict annotation -- whatever
    # shape the next well-meaning edit takes, it shows up here as a new file.
    assert set(os.listdir(d)) == files_before, set(os.listdir(d)) ^ files_before

    print(section.rstrip())
    print("\ntriage selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    sys.exit(main())
