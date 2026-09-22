"""Retry-until-green defence, and the power floor.

Measured attack. A developer who just re-runs CI ships a regression with
probability (N=292 gating cases, churn 1.27% -- both measured on this suite by
quarantine.py -- 40k trials, seed 20260922):

    true drop   1 run   2 runs  3 runs  5 runs
      -2pp      0.880   0.985   0.998   1.000
      -3pp      0.638   0.869   0.953   0.994
      -5pp      0.160   0.294   0.407   0.582   <- the design effect size
     -10pp      0.000   0.001   0.001   0.001

Regenerate with `python3 retry_sim.py`, which scores every simulated run with
gate.significant() itself. An earlier version of this table was written down
from a simulation that was never committed and could not be reproduced: it
claimed 0.268 / 0.789 at the -5pp row for N=300 and churn 2%, where the real
figures at those parameters are 0.200 / 0.673. A number quoted in four files
needs a script behind it.

The gate is a random variable; re-rolling it is free and socially encouraged
("flaky CI, kick it"). No threshold calibration touches this, because the
attacker samples the same distribution the gate samples. So: the verdict is a
pure function of (candidate, baseline, judge) and is cached. A re-run returns
the stored verdict.

Genuine re-measurement must POOL repetitions (the adjudicator), never take the
best of k. Pooling is the fix; best-of-k IS the attack.
"""

import json
import sqlite3

DDL = """
CREATE TABLE IF NOT EXISTS verdict_cache (
  candidate_sha   TEXT NOT NULL,
  baseline_sha    TEXT NOT NULL,
  judge_snapshot  TEXT NOT NULL,
  decision        TEXT NOT NULL,          -- BLOCK | COMMENT | PASS | REFUSE
  evidence_json   TEXT NOT NULL,          -- b, c, n, p, ci -- what justified it
  first_seen      TEXT NOT NULL,
  hits            INTEGER NOT NULL DEFAULT 0,   -- >0 proves a retry was attempted
  PRIMARY KEY (candidate_sha, baseline_sha, judge_snapshot)
);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    return db


def key(candidate_sha, baseline_sha, judge_snapshot):
    return (candidate_sha, baseline_sha, judge_snapshot)


def lookup(db, candidate_sha, baseline_sha, judge_snapshot):
    """Return the stored verdict, or None. Increments the retry counter."""
    k = key(candidate_sha, baseline_sha, judge_snapshot)
    row = db.execute(
        "SELECT * FROM verdict_cache WHERE candidate_sha=? AND baseline_sha=?"
        " AND judge_snapshot=?", k).fetchone()
    if row is None:
        return None
    db.execute(
        "UPDATE verdict_cache SET hits = hits + 1 WHERE candidate_sha=?"
        " AND baseline_sha=? AND judge_snapshot=?", k)
    db.commit()
    return {"decision": row["decision"], "evidence": json.loads(row["evidence_json"]),
            "first_seen": row["first_seen"], "retries": row["hits"] + 1}


def store(db, candidate_sha, baseline_sha, judge_snapshot, decision, evidence, now):
    db.execute(
        "INSERT OR IGNORE INTO verdict_cache (candidate_sha, baseline_sha,"
        " judge_snapshot, decision, evidence_json, first_seen) VALUES (?,?,?,?,?,?)",
        (*key(candidate_sha, baseline_sha, judge_snapshot), decision,
         json.dumps(evidence, sort_keys=True), now))
    db.commit()


# ---------------------------------------------------------------- power floor

POWER_FLOOR = 0.60
DESIGN_EFFECT_PP = 5.0


def power_at(delta_pp, churn, n_cases, alpha=0.05, trials=4000, seed=20260921):
    """P(one-sided exact McNemar detects `delta_pp`) on the CURRENT suite.

    This is the ONLY quantity that goes red in both measured composed failures.
    Churn, FPR, null-fire-rate and required-N all move the reassuring way while
    the gate goes blind:
      naive quarantine : power 0.866 -> 0.405 while churn "improves" 1.92% -> 0.43%
      judge degrading  : power 0.878 -> 0.192 while the reported null rate stays
                         inside its 0.5% budget every single quarter.
    """
    import random
    from stats.adapter import mcnemar_one_sided_worse

    rng = random.Random(seed)
    # delta_pp is signed and negative for a regression. A drop of |d| means
    # |d|*N more cases broke than were fixed, so the mass moves INTO c.
    d = abs(delta_pp) / 100.0
    # churn is the two-way flip mass, split evenly between the two directions.
    p_b = churn / 2.0
    p_c = churn / 2.0 + d
    if p_c < 0 or p_b + p_c > 1:
        raise ValueError(f"infeasible: churn={churn} delta_pp={delta_pp}")
    hits = 0
    for _ in range(trials):
        b = c = 0
        for _ in range(n_cases):
            u = rng.random()
            if u < p_b:
                b += 1
            elif u < p_b + p_c:
                c += 1
        if mcnemar_one_sided_worse(b, c) < alpha:
            hits += 1
    return hits / trials


def check_power_floor(churn, n_cases, floor=POWER_FLOOR):
    """Returns (ok, power). Below the floor the gate must degrade to COMMENT-only."""
    p = power_at(-DESIGN_EFFECT_PP, churn, n_cases)
    return (p >= floor, p)


if __name__ == "__main__":
    import tempfile, os
    db = connect(os.path.join(tempfile.mkdtemp(), "vc.db"))
    ev = {"b": 4, "c": 17, "n": 300, "p": 0.0036}
    assert lookup(db, "cand1", "base1", "judge-A") is None
    store(db, "cand1", "base1", "judge-A", "BLOCK", ev, "2026-09-21T00:00:00Z")

    # The attack: same candidate, re-run. Must return the SAME verdict.
    for expect_retry in (1, 2, 3):
        got = lookup(db, "cand1", "base1", "judge-A")
        assert got["decision"] == "BLOCK", got
        assert got["retries"] == expect_retry, got
    print(f"retry-until-green: 3 re-runs all returned BLOCK (retries={got['retries']})")

    # A new commit is a genuinely new measurement -> no cache hit.
    assert lookup(db, "cand2", "base1", "judge-A") is None
    # Changing the judge invalidates the verdict (it is a different measurement).
    assert lookup(db, "cand1", "base1", "judge-B") is None
    print("new candidate and new judge snapshot both correctly MISS")

    # Power floor: the quantity that goes red when nothing else does.
    for churn, n in ((0.02, 300), (0.02, 145), (0.20, 300)):
        ok, p = check_power_floor(churn, n)
        print(f"  churn={churn:>5.0%} N={n:>4}  power@-5pp={p:.3f}  "
              f"{'OK' if ok else 'BELOW FLOOR -> comment-only'}")
    assert check_power_floor(0.02, 300)[0], "healthy suite must clear the floor"
    assert not check_power_floor(0.20, 300)[0], "degraded judge must trip the floor"
    print("verdict_cache self-check OK")
