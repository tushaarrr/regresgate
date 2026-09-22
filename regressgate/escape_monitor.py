#!/usr/bin/env python3
"""Phase 7b: the escape-rate monitor -- the only check that is not the golden set.

Everything else in this repo measures the golden set against itself. PLAN.md
section 9: "no amount of replication learns whether the golden set measures what
production cares about. At a 2pp construct gap, even a perfect adjudicator is 67%
accurate. Treat the escape-rate monitor as the only external check." An ESCAPE is
a real production incident whose causing commit the gate let through.

Three things this module refuses to do, each because of a measured number.

  NO CONTROL PATH. PLAN.md section 5: "Log adjudications and the escape rate as
  monitors. Do not wire them to a parameter." An escape-driven threshold is the
  override-derived label with a nicer name, and that one was measured: clicking
  it drives -1.00pp -> -4.09pp, where power@-5pp is 0.049. Enforced structurally,
  not by discipline -- the cache is opened mode=ro (a write raises), the only
  other file handle is append-mode, and nothing here imports quarantine.py.

  NO RATE ON A TINY SAMPLE. Labelled events arrive at 0.0159 per PR, one per 63
  PRs, ~6 usable per year. So the denominator is small for YEARS, and a small
  denominator makes a percentage a lie with a decimal point: 1 escape in 3
  incidents is "33%" and a 95% interval of 6.1% to 79.2%. Below 10 classified
  incidents this prints counts and says the sample is too small -- with the
  interval for the sample IN HAND, computed at print time, never a baked-in
  constant. At exactly 10 the interval is 45pp wide at 2 escapes and 52.7pp at
  5, so the rate is never printed without it.

  NO SILENT DENOMINATOR. escaped/total_incidents and escaped/total_merges are
  different quantities and the flattering one is easy to print by accident. The
  denominator here is escaped+caught -- incidents the gate actually ruled on --
  and it is named in the report record and in the printed text. UNKNOWN is its
  own bucket: folding it into CAUGHT flatters the gate, folding it into ESCAPED
  slanders it, and in a young project UNKNOWN is the biggest bucket.

    python3 escape_monitor.py record --sha a1b2c3d --date 2026-09-21 \
        --severity sev2 --description "..." --missing-cases c17 c42 --log escapes.jsonl
    python3 escape_monitor.py report --log escapes.jsonl --cache-db verdicts.db
    python3 escape_monitor.py --selfcheck
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Below this many RULED-ON incidents, print counts and refuse the percentage.
# 10 is not a comfort threshold: 2 escapes in 10 is 5.7% to 51.0%. It is the
# point below which the interval stops fitting on the line (1 in 3 is 73pp wide).
MIN_RULED_ON = 10

SEVERITIES = ("sev0", "sev1", "sev2", "sev3")

# What the rate is over. Carried in the report record verbatim so a number that
# gets pasted into a slide arrives with its denominator attached.
DENOMINATOR = ("escaped + caught, i.e. incidents the gate actually ruled on. "
               "NOT all incidents (UNKNOWN is excluded) and NOT all merges "
               "(the merge count is not a quantity this repo measures).")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ recording

def record(log_path, sha, date, severity, description, missing_cases):
    """Append one incident. Append is the ONLY write mode this module has.

    A monitor you can rewrite is a monitor that agrees with you, so there is no
    update path and no delete path -- not as policy, as missing code.
    """
    if len(sha) < 7 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        raise SystemExit("::error::sha must be at least 7 HEX chars; a shorter prefix "
                         "matches more than one commit and the join would lie, and "
                         "'_' and '%' are LIKE wildcards that match every row in the "
                         "cache -- which files junk as a permanent fake CAUGHT, since "
                         "this log has no delete path")
    try:
        datetime.strptime(date, "%Y-%m-%d")  # a typo'd date is in the log forever
    except ValueError:
        raise SystemExit(f"::error::--date must be YYYY-MM-DD, got {date!r}")
    if severity not in SEVERITIES:
        raise SystemExit(f"::error::severity must be one of {SEVERITIES}")
    if not description.strip():
        raise SystemExit("::error::an incident with no description is not a record")
    rec = {"recorded_at": now(), "sha": sha, "date": date, "severity": severity,
           "description": description.strip(),
           # The actionable field: what to ADD to the golden set. An escape whose
           # missing_cases is empty is an escape you have not diagnosed yet.
           "missing_cases": sorted(set(missing_cases or []))}
    with open(log_path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def load(log_path):
    """(records, count of lines that are not records this module wrote).

    A concurrent append can truncate a line and a human can edit the file; the
    log is append-only, so there is no repair path either. Dying on one bad line
    would make a monitor red the job it runs in, so bad lines are skipped and
    COUNTED -- a skipped line is reported, never silently absent.
    """
    if not os.path.exists(log_path):
        return [], 0
    recs, bad = [], 0
    with open(log_path) as f:
        for ln in f:
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                rec = None
            if (isinstance(rec, dict) and isinstance(rec.get("missing_cases"), list)
                    # the ELEMENTS too: report() sorts them across incidents and
                    # render() joins them, so one hand-edited [17] raises inside
                    # report() and the whole monitor output becomes ::error::.
                    and all(isinstance(c, str) for c in rec["missing_cases"])
                    and all(isinstance(rec.get(k), str)
                            for k in ("sha", "date", "severity", "description"))):
                recs.append(rec)
            else:
                bad += 1
    return recs, bad


# ------------------------------------------------------- the join to the gate

def open_cache(path):
    """Read-only handle on the gate's verdict cache, or None if there is none.

    mode=ro rather than verdict_cache.connect(): connect() runs the DDL, which is
    a write, and a monitor holding a writable handle on the thing it audits is a
    control path that has not been used yet.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("SELECT 1 FROM verdict_cache LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        # os.path.exists only proves a file is there. Two sqlite paths are live
        # in this repo (regressgate.db for drift, verdicts.db for the gate) and
        # --db vs --cache-db is one letter apart, so the wrong one WILL get
        # passed. Wrong schema, not-a-database and unreadable all land here, and
        # all of them mean "no verdict cache", never a clean bill of health.
        return None
    return db


def verdicts_for(db, sha):
    """Every cached decision for this commit. Prefix-tolerant: incidents get
    filed with the short sha a human copied out of a PR, the cache holds the
    full one."""
    if db is None:
        return []
    # No except here on purpose. open_cache() already proved the table reads, so
    # anything raised now is a real failure of THIS read (a locked database while
    # the gate writes). Swallowing it would turn one failed join into a clean
    # UNKNOWN for one incident while the rest classify normally -- a real escape
    # would leave the numerator and nothing would say so.
    rows = db.execute(
        "SELECT decision FROM verdict_cache WHERE candidate_sha = ?"
        " OR candidate_sha LIKE ? || '%' OR ? LIKE candidate_sha || '%'",
        (sha, sha, sha)).fetchall()
    return [r["decision"] for r in rows]


def classify(decisions):
    """PASS/COMMENT -> ESCAPED, BLOCK alone -> CAUGHT, anything else -> UNKNOWN.

    PASS/COMMENT is checked FIRST, and that order is the whole rule, not an
    accident of branch order (PLAN section 7 item 3 -- this repo already paid for
    classifying a row by which if came first). The cache key is (candidate_sha,
    baseline_sha, judge_snapshot), so ONE commit legitimately holds several rows:
    main advances or the judge is re-pinned, CI re-runs, the key differs, the
    cache does not replay and the gate re-rolls -- the retry-until-green sequence
    verdict_cache.py exists to document. If ANY of those rolls said PASS or
    COMMENT, the gate let the commit through and it then broke production. That
    is an escape. Calling it CAUGHT because some other roll said BLOCK puts it in
    the denominator as a catch, biases the rate DOWN, and drops its missing_cases
    from "add to the golden set" -- flattering the gate exactly where the
    project's headline attack lands.

    "Anything else" is load-bearing. gate.py caches only PASS/COMMENT/BLOCK, so a
    REFUSE or a HARNESS_ERROR never reaches this table -- but if CACHEABLE is ever
    widened, a refusal must not become CAUGHT. A gate that declined to measure did
    not catch anything.
    """
    if {"PASS", "COMMENT"} & set(decisions):
        return "ESCAPED"
    if "BLOCK" in decisions:
        return "CAUGHT"
    return "UNKNOWN"


def wilson(k, n, z=1.96):
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (centre - half), 100 * (centre + half))


def report(log_path, cache_db):
    db = open_cache(cache_db)
    records, malformed = load(log_path)
    incidents = []
    for inc in records:
        d = verdicts_for(db, inc["sha"])
        incidents.append(dict(inc, verdicts=sorted(d), bucket=classify(d)))
    counts = {b: sum(i["bucket"] == b for i in incidents)
              for b in ("ESCAPED", "CAUGHT", "UNKNOWN")}
    ruled_on = counts["ESCAPED"] + counts["CAUGHT"]
    enough = ruled_on >= MIN_RULED_ON
    missing = sorted({c for i in incidents if i["bucket"] == "ESCAPED"
                      for c in i["missing_cases"]})
    return {
        "generated_at": now(),
        "cache_present": db is not None,
        "total_incidents": len(incidents),
        "malformed_lines": malformed,
        "counts": counts,
        "denominator": DENOMINATOR,
        "denominator_n": ruled_on,
        "escape_rate_pct": round(100.0 * counts["ESCAPED"] / ruled_on, 1) if enough else None,
        "escape_rate_ci_pct": [round(x, 1) for x in wilson(counts["ESCAPED"], ruled_on)]
                              if enough else None,
        "sample_too_small": not enough,
        "min_ruled_on": MIN_RULED_ON,
        "missing_cases": missing,
        "incidents": incidents,
    }


def render(r):
    L = [f"incidents recorded : {r['total_incidents']}",
         f"  ESCAPED          : {r['counts']['ESCAPED']}  (gate said PASS or COMMENT)",
         f"  CAUGHT           : {r['counts']['CAUGHT']}  (gate said BLOCK; the incident "
         f"came from somewhere else)",
         f"  UNKNOWN          : {r['counts']['UNKNOWN']}  (no gate verdict for that sha -- "
         f"counted separately, never folded into either bucket)"]
    if not r["cache_present"]:
        L.append("  note             : no readable verdict cache (missing, wrong file, or "
                 "locked), so EVERY incident is UNKNOWN")
    if r["malformed_lines"]:
        L.append(f"  note             : {r['malformed_lines']} unreadable log line(s) skipped "
                 "-- they are in NO bucket above")
    L.append(f"denominator        : {r['denominator_n']} -- {r['denominator']}")
    if r["sample_too_small"]:
        L.append(f"escape rate        : NOT REPORTED -- {r['denominator_n']} ruled-on incident(s) "
                 f"is below {r['min_ruled_on']}; the sample is too small to be a rate.")
        if r["denominator_n"]:
            # PLAN section 7 item 9: recompute at print time. The width that
            # justifies the refusal must be the width of the sample in hand --
            # a baked-in constant describes some other sample.
            lo, hi = (round(x, 1) for x in wilson(r["counts"]["ESCAPED"], r["denominator_n"]))
            L.append(f"                     Your own {r['denominator_n']}: 95% interval {lo}% to "
                     f"{hi}%, {hi - lo:.0f}pp wide. Counts above are the whole finding.")
        else:
            L.append("                     A rate over 3 incidents has a 95% interval 73pp "
                     "wide. Counts above are the whole finding.")
    else:
        lo, hi = r["escape_rate_ci_pct"]
        L.append(f"escape rate        : {r['escape_rate_pct']}%  (95% CI {lo}% to {hi}%, "
                 f"n={r['denominator_n']})")
    if r["missing_cases"]:
        L.append("add to the golden set: " + ", ".join(r["missing_cases"]))
    for i in r["incidents"]:
        L.append(f"  {i['date']} {i['severity']} {i['sha'][:12]} {i['bucket']:<7} "
                 f"{i['description'][:58]}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append one production incident (append-only)")
    rec.add_argument("--log", required=True)
    rec.add_argument("--sha", required=True, help="commit that CAUSED the incident")
    rec.add_argument("--date", required=True, help="YYYY-MM-DD")
    rec.add_argument("--severity", required=True, choices=SEVERITIES)
    rec.add_argument("--description", required=True)
    rec.add_argument("--missing-cases", nargs="*", default=[],
                     help="case ids that WOULD have caught it -- what to add to the suite")

    rep = sub.add_parser("report", help="join incidents to gate verdicts and count")
    rep.add_argument("--log", required=True)
    rep.add_argument("--cache-db", help="gate verdict cache; opened read-only")
    rep.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "record":
        r = record(a.log, a.sha, a.date, a.severity, a.description, a.missing_cases)
        print(f"recorded {r['sha'][:12]} {r['severity']} {r['date']} -> {a.log}")
        return 0

    # Always 0, even with escapes on the board and even when the report itself
    # blows up. A monitor that fails a build is a control path with extra steps,
    # and section 5 says this one has none -- so a mistyped --cache-db must not
    # red the job it runs in.
    try:
        r = report(a.log, a.cache_db)
        print(json.dumps(r, indent=2) if a.json else render(r))
    except Exception as e:                                   # noqa: BLE001
        print(f"::error::escape monitor could not report: {e!r}. Exiting 0 anyway; "
              "a monitor that fails a build is a control path with extra steps.")
    return 0


def _selfcheck():
    import contextlib
    import io as _io
    import tempfile
    import verdict_cache            # imported HERE only: the main path must never
                                    # hold a handle that can write the cache.

    def run(args):
        """main() with stdout captured -- the EXIT CODE is the thing under test."""
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(args)
        return rc, buf.getvalue()

    d = tempfile.mkdtemp()
    log, cdb = os.path.join(d, "escapes.jsonl"), os.path.join(d, "vc.db")

    w = verdict_cache.connect(cdb)
    ev, T = {"b": 1, "c": 2, "n": 300}, "2026-09-01T00:00:00Z"
    for sha, dec in (("a" * 40, "PASS"), ("b" * 40, "BLOCK"), ("c" * 40, "COMMENT"),
                     ("d" * 40, "REFUSE")):
        verdict_cache.store(w, sha, "base1", "judge-A", dec, ev, T)
    w.close()

    # the cache handle this module opens must be STRUCTURALLY unable to write
    ro = open_cache(cdb)
    try:
        ro.execute("DELETE FROM verdict_cache")
        raise AssertionError("the monitor opened the gate's cache writable")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e), e

    # Every bucket carries missing_cases, including the ones that must NOT reach
    # "add to the golden set": you do not write a regression case for a gap the
    # gate already closes, and an UNKNOWN has not been ruled on at all.
    record(log, "a" * 40, "2026-09-10", "sev2", "truncated answers in prod", ["c17"])
    record(log, "b" * 40, "2026-09-11", "sev1", "unrelated outage", ["c-already-caught"])
    record(log, "e" * 40, "2026-09-12", "sev3", "never evaluated by the gate",
           ["c-never-ruled-on"])
    record(log, "c" * 40, "2026-09-13", "sev2", "tone regression", ["c42"])
    record(log, "d" * 40, "2026-09-14", "sev2", "gate refused to compare", [])

    r = report(log, cdb)
    buckets = {i["sha"][0]: i["bucket"] for i in r["incidents"]}
    assert buckets["a"] == "ESCAPED", buckets          # PASS  -> escaped
    assert buckets["c"] == "ESCAPED", buckets          # COMMENT merges too
    assert buckets["b"] == "CAUGHT", buckets           # BLOCK -> not an escape
    assert buckets["e"] == "UNKNOWN", buckets          # no verdict at all
    assert buckets["d"] == "UNKNOWN", buckets          # REFUSE is not a catch
    assert r["counts"] == {"ESCAPED": 2, "CAUGHT": 1, "UNKNOWN": 2}, r["counts"]
    # UNKNOWN is out of the denominator entirely, in neither direction
    assert r["denominator_n"] == 3, r["denominator_n"]
    assert r["total_incidents"] == 5
    print(f"  buckets: {r['counts']}  denominator={r['denominator_n']} of "
          f"{r['total_incidents']} incidents")

    # a tiny sample refuses the percentage, and says so where a human reads it
    assert r["escape_rate_pct"] is None and r["sample_too_small"], r
    text = render(r)
    assert "NOT REPORTED" in text and "too small" in text, text
    assert "66.7%" not in text and "67%" not in text, "it printed the flattering rate anyway"
    # ...and the width that justifies the refusal is THIS sample's, computed, not
    # a constant describing some other n (PLAN section 7 item 9).
    assert "20.8% to 93.9%" in text, text
    assert "over 3 incidents" not in text, text
    # EXACT equality, not a subset: the CAUGHT and UNKNOWN incidents above both
    # have cases, and neither may appear here.
    assert r["missing_cases"] == ["c17", "c42"], r["missing_cases"]
    print("  3 ruled-on incidents: rate refused, counts printed")

    # append-only: a second record never rewrites the first, and a repeat sha is
    # a second incident, not an update of the first.
    first = open(log).readline()
    record(log, "a" * 40, "2026-09-20", "sev1", "it happened again", ["c18"])
    lines = open(log).read().splitlines()
    assert len(lines) == 6, len(lines)
    assert lines[0] == first.rstrip("\n"), "the first record was rewritten"
    assert json.loads(lines[-1])["description"] == "it happened again"

    # ...and the refusal is not unconditional: at MIN_RULED_ON it does report,
    # with the interval, so the caller can see how wide 10 samples still is.
    for i in range(9):
        record(log, f"{i:040d}".replace("0", "a", 1), "2026-09-21", "sev2", f"escape {i}", [])
    w = verdict_cache.connect(cdb)
    for i in range(9):
        verdict_cache.store(w, f"{i:040d}".replace("0", "a", 1), "base1", "judge-A",
                            "PASS", ev, T)
    w.close()
    r2 = report(log, cdb)
    assert r2["denominator_n"] == 13, r2["denominator_n"]
    # THE number. Everything else here is a structural invariant; this is the one
    # that gets pasted into a slide, so it is pinned by value, not by is-not-None.
    assert r2["escape_rate_pct"] == 92.3, r2["escape_rate_pct"]
    assert r2["escape_rate_ci_pct"] == [66.7, 98.6], r2["escape_rate_ci_pct"]
    lo, hi = r2["escape_rate_ci_pct"]
    assert hi - lo > 30, (lo, hi)      # still 30pp+ wide at n=13 -- that is the point
    print(f"  {r2['denominator_n']} ruled-on: rate {r2['escape_rate_pct']}% "
          f"(95% CI {lo}% to {hi}%, {hi - lo:.1f}pp wide)")

    # ---- the refusal boundary itself, from both sides -----------------------
    # MIN_RULED_ON is the whole of "no rate on a tiny sample", and one `>=`
    # decides whether the headline output exists at all. Both the value and the
    # comparison are pinned, because a threshold nobody tests at its edge can be
    # moved anywhere between the samples that do exist.
    assert MIN_RULED_ON == 10, MIN_RULED_ON

    def sample(n_ruled, n_escaped):
        """A throwaway log+cache with exactly n_ruled ruled-on incidents."""
        sd = tempfile.mkdtemp()
        slog, scdb = os.path.join(sd, "escapes.jsonl"), os.path.join(sd, "vc.db")
        sw = verdict_cache.connect(scdb)
        for i in range(n_ruled):
            sha = f"{i:x}" * 8          # 8 hex chars, and no sha is another's prefix
            verdict_cache.store(sw, sha, "base1", "judge-A",
                                "PASS" if i < n_escaped else "BLOCK", ev, T)
            record(slog, sha, "2026-09-15", "sev2", f"incident {i}", [])
        sw.close()
        return report(slog, scdb)

    at_10, at_9 = sample(10, 2), sample(9, 2)
    assert at_10["denominator_n"] == 10 and at_9["denominator_n"] == 9
    assert not at_10["sample_too_small"], "the rate vanished AT exactly MIN_RULED_ON"
    assert at_10["escape_rate_pct"] == 20.0, at_10["escape_rate_pct"]
    assert at_10["escape_rate_ci_pct"] == [5.7, 51.0], at_10["escape_rate_ci_pct"]
    assert at_9["sample_too_small"], "9 ruled-on is below the threshold; it must refuse"
    assert at_9["escape_rate_pct"] is None, at_9["escape_rate_pct"]
    assert "below 10" in render(at_9), render(at_9)
    print(f"  boundary: n=9 refuses, n=10 reports {at_10['escape_rate_pct']}% "
          f"(95% CI {at_10['escape_rate_ci_pct'][0]}% to "
          f"{at_10['escape_rate_ci_pct'][1]}% -- 45pp wide, which is the point)")

    # `report` ALWAYS exits 0 -- with 12 escapes on the board, and with a
    # --cache-db that is not a database. An exit code is the only surface on
    # which this module could become a control path, so it gets a tripwire like
    # the other three invariants, not just a comment.
    rc, out = run(["report", "--log", log, "--cache-db", cdb])
    assert rc == 0, rc
    # The WHOLE line, not a substring of the number: the interval is what stops
    # the rate being a percentage with a decimal point, and it is printed right
    # beside it on the one line a human pastes into a slide.
    rate_line = next(l for l in out.splitlines() if l.startswith("escape rate"))
    assert rate_line == "escape rate        : 92.3%  (95% CI 66.7% to 98.6%, n=13)", rate_line
    assert run(["report", "--log", log, "--cache-db", cdb, "--json"])[0] == 0

    # ---- a read that fails MID-report is LOUD, and still exits 0 ------------
    # open_cache()'s probe proves the table read ONCE; the gate can take its
    # write lock the moment after. Swallowing that per incident would turn a
    # real escape into a quiet UNKNOWN and drop its cases, with a full-looking
    # report printed over the hole -- so verdicts_for() lets it out, and main()
    # turns it into ::error:: and exit 0 rather than reding the job.
    class LockedAfterOneRead:
        def __init__(self, db):
            self.db, self.reads = db, 0

        def execute(self, *a):
            self.reads += 1
            if self.reads > 1:
                raise sqlite3.OperationalError("database is locked")
            return self.db.execute(*a)

    real_open_cache = open_cache
    globals()["open_cache"] = lambda p: LockedAfterOneRead(real_open_cache(p))
    try:
        rc, out = run(["report", "--log", log, "--cache-db", cdb])
    except BaseException as e:
        raise AssertionError(f"a mid-report read failure escaped main(): {e!r} -- exit 0 "
                             "is the contract; a monitor that reds the job is a "
                             "control path with extra steps")
    finally:
        globals()["open_cache"] = real_open_cache
    assert rc == 0, rc
    assert "::error::" in out and "locked" in out, \
        "a failed per-incident read was swallowed: the report printed as if whole"
    assert "escape rate" not in out, out
    print("  a locked cache mid-report -> ::error::, no partial report, still exit 0")

    # a missing cache is honest UNKNOWN, never a clean bill of health
    r3 = report(log, os.path.join(d, "nope.db"))
    assert r3["counts"]["ESCAPED"] == 0 and r3["counts"]["UNKNOWN"] == r3["total_incidents"]
    assert not r3["cache_present"] and "EVERY incident is UNKNOWN" in render(r3)
    print("  no verdict cache -> every incident UNKNOWN, rate still refused")

    # ONE sha, TWO cached rows -- blocked in the morning against one baseline,
    # passed in the afternoon against the next. The gate let it through, so it is
    # an ESCAPE; BLOCK must not win on branch order. Plus both directions of the
    # prefix-tolerant join, which is the DOCUMENTED primary workflow (a human
    # pastes 7 chars out of a PR) and was otherwise untested.
    w = verdict_cache.connect(cdb)
    verdict_cache.store(w, "9" * 40, "baseMORNING", "judge-A", "BLOCK", ev, T)
    verdict_cache.store(w, "9" * 40, "baseAFTERNOON", "judge-A", "PASS", ev, T)
    # ...and the SAME sha with the rows the other way round. Ordering-independence
    # is the claim, so it needs both orderings: "classify the last verdict" is a
    # design this repo rejected and it passes a one-ordering fixture.
    verdict_cache.store(w, "6" * 40, "baseMORNING", "judge-A", "PASS", ev, T)
    verdict_cache.store(w, "6" * 40, "baseAFTERNOON", "judge-A", "BLOCK", ev, T)
    verdict_cache.store(w, "8" * 40, "base1", "judge-A", "PASS", ev, T)  # long in cache
    verdict_cache.store(w, "7" * 7, "base1", "judge-A", "PASS", ev, T)   # short in cache
    # A row that mentions an incident's sha only as the BASELINE it was compared
    # AGAINST. The gate never ruled on that commit, so it joins to nothing.
    verdict_cache.store(w, "5" * 40, "4" * 40, "judge-A", "BLOCK", ev, T)
    w.close()
    record(log, "9" * 40, "2026-09-22", "sev1", "blocked at 09:00, passed at 16:00", ["c99"])
    record(log, "6" * 40, "2026-09-22", "sev1", "passed at 09:00, blocked at 16:00", ["c66"])
    record(log, "8" * 7, "2026-09-22", "sev2", "filed with the short sha", ["c7"])
    record(log, "7" * 40, "2026-09-22", "sev2", "filed with the long sha", ["c8"])
    record(log, "4" * 40, "2026-09-22", "sev2", "only ever a baseline, never a candidate", [])
    r4 = report(log, cdb)
    b4 = {i["sha"]: (i["bucket"], i["verdicts"]) for i in r4["incidents"]}
    assert b4["9" * 40] == ("ESCAPED", ["BLOCK", "PASS"]), b4["9" * 40]
    assert b4["6" * 40] == ("ESCAPED", ["BLOCK", "PASS"]), b4["6" * 40]
    assert b4["8" * 7][0] == "ESCAPED", b4["8" * 7]      # short incident, long cache row
    assert b4["7" * 40][0] == "ESCAPED", b4["7" * 40]    # long incident, short cache row
    assert b4["4" * 40] == ("UNKNOWN", []), \
        "a row joined on baseline_sha: the gate never ruled on that commit"
    # and the actionable field survives: a mixed sha must not drop its cases
    assert {"c7", "c8", "c66", "c99"} <= set(r4["missing_cases"]), r4["missing_cases"]
    print(f"  BLOCK+PASS on one sha -> {b4['9' * 40][0]} in EITHER order; "
          "short/long sha join both ways; baseline-only sha stays UNKNOWN")

    # a file that is not the verdict cache is NOT a cache: cache_present must go
    # False so the note fires, instead of a silent explanation-free all-UNKNOWN.
    other = os.path.join(d, "other.db")
    ow = sqlite3.connect(other)
    ow.execute("CREATE TABLE runs (x)")          # store's db, no verdict_cache
    ow.commit()
    ow.close()
    r5 = report(log, other)
    assert not r5["cache_present"], "a wrong-schema file was accepted as the cache"
    assert r5["counts"]["UNKNOWN"] == r5["total_incidents"], r5["counts"]
    assert "EVERY incident is UNKNOWN" in render(r5)
    junk = os.path.join(d, "junk.txt")
    open(junk, "w").write("this is not a database\n")
    assert not report(log, junk)["cache_present"]
    assert run(["report", "--log", log, "--cache-db", junk])[0] == 0
    print("  wrong-schema and non-sqlite --cache-db -> cache_present False, exit 0")

    # record() is the only write path and there is no delete path, so a bad
    # value it accepts is in the log forever. Every guard gets its own rejection,
    # NOT just the sha one -- argparse backstops severity and nothing at all
    # checks --date or --description, and record() is a public function anyway.
    ok = dict(sha="a" * 40, date="2026-09-22", severity="sev2",
              description="junk", missing_cases=[])
    for why, bad in (
            ("'_' is a LIKE wildcard matching every cached row", dict(sha="_______")),
            ("'%' is a LIKE wildcard matching every cached row", dict(sha="%%%%%%%")),
            ("3 chars match more than one commit", dict(sha="abc")),
            ("6 hex is one short of the minimum", dict(sha="abcdef")),
            ("not hex at all", dict(sha="not-hex")),
            ("month 13", dict(date="2026-13-01")),
            ("not YYYY-MM-DD, and it sorts wrong forever", dict(date="20260-9-1")),
            ("no date", dict(date="")),
            ("not a severity this repo has", dict(severity="sev9")),
            ("whitespace is not a description", dict(description="   ")),
    ):
        try:
            record(log, **{**ok, **bad})
            raise AssertionError(f"record() accepted {bad} -- {why}")
        except SystemExit:
            pass
    # ...and the 7-char boundary is an ACCEPT: the documented workflow is a human
    # pasting the short sha out of a PR, so the guard must not eat it.
    assert record(log, "abcdef0", "2026-09-22", "sev2", "7 hex is enough", [])

    # A line this module did not write is not an incident, whatever it parses
    # to -- and render() reads fields only the shape check guarantees, so one
    # accepted stray line replaces the ENTIRE report with a single ::error::.
    # Bad lines must not take the monitor down and must not vanish quietly.
    before = report(log, cdb)["total_incidents"]
    with open(log, "a") as f:
        f.write('{"sha": "aaaaaaa", trunc\n')                      # truncated JSON
        f.write('{"level":"info","msg":"deploy finished"}\n')      # valid JSON, foreign
        f.write('["sha", "date"]\n')                               # JSON, not an object
        f.write(json.dumps(dict(ok, sha="a" * 40, missing_cases="c17")) + "\n")  # not a list
        f.write(json.dumps(dict(ok, sha="a" * 40, missing_cases=[17])) + "\n")   # not strings
    # load() is checked first: report() would die on the missing fields instead.
    recs, bad = load(log)
    assert len(recs) == before, \
        "a line this module did not write was accepted as an incident"
    assert bad == 5, f"only {bad} of the 5 non-record lines were rejected"
    r6 = report(log, cdb)
    assert r6["malformed_lines"] == 5 and r6["total_incidents"] == before, r6["counts"]
    assert "5 unreadable log line(s) skipped" in render(r6)
    assert run(["report", "--log", log, "--cache-db", cdb])[0] == 0
    print("  every record() guard rejects; 5 kinds of non-record log line skipped "
          "and counted")

    print("escape_monitor selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    sys.exit(main())
