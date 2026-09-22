"""regressgate store + recall layer. Raw sqlite3, stdlib only.

Run store (runs / case_results / assertion_results) plus the recall layer
(decisions / baselines) that makes "why is the baseline what it is?" answerable
from the DB instead of from someone's memory.

    python3 store.py            # DDL, fixtures, all four queries, self-check
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(hours):
    return iso(NOW - timedelta(hours=hours))


# --------------------------------------------------------------------------
# (a) DDL
# --------------------------------------------------------------------------
# Every column tagged [pf:...] exists ONLY because of a measured promptfoo
# behavior. Without that behavior the column would be dead weight.
DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE runs (
  run_id                    TEXT PRIMARY KEY,
  git_sha                   TEXT NOT NULL,
  branch                    TEXT NOT NULL,
  started_at                TEXT NOT NULL,           -- ISO-8601 UTC
  ended_at                  TEXT,
  trigger                   TEXT NOT NULL CHECK (trigger IN ('pr','nightly','manual')),
  promptfoo_version         TEXT NOT NULL,
  model_under_test_snapshot TEXT NOT NULL,           -- what the CONFIG asked for
  judge_snapshot            TEXT NOT NULL,           -- [pf] grader is env-key-picked; pin + record
  golden_set_hash           TEXT NOT NULL,
  assertion_config_hash     TEXT NOT NULL,
  config_dir                TEXT NOT NULL,           -- [pf] --env-file can override PROMPTFOO_CONFIG_DIR
  exit_code                 INTEGER,                 -- [pf] raw code; 1 conflates YAML typo & missing key
  outcome                   TEXT NOT NULL CHECK (outcome IN ('OK','TESTS_FAILED','HARNESS_ERROR','INTERRUPTED')),
  n_cases_expected          INTEGER NOT NULL,        -- [pf] zero tests exits 0; expected!=seen is the only way to catch it
  n_cases_seen              INTEGER NOT NULL,
  successes                 INTEGER NOT NULL,
  failures                  INTEGER NOT NULL,
  errors                    INTEGER NOT NULL,        -- [pf] errors are folded into passRate; keep separate
  pass_rate                 REAL
);
CREATE INDEX runs_baseline_idx ON runs (branch, outcome, started_at DESC);
CREATE INDEX runs_nightly_idx  ON runs (trigger, started_at DESC);

CREATE TABLE case_results (
  id                        INTEGER PRIMARY KEY,
  run_id                    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  case_id                   TEXT NOT NULL,           -- [pf] explicit var; testIdx is a ROW index, not a case index
  prompt_idx                INTEGER NOT NULL,
  repeat_index              INTEGER,                 -- [pf] NULL unless the provider echoes __repeatIndex
  row_idx                   INTEGER,                 -- [pf] testIdx, provenance only. NEVER group on row_idx % N
  status                    TEXT NOT NULL CHECK (status IN ('PASSED','FAILED','UNSCORED','ERROR')),
  score                     REAL,
  output                    TEXT,
  judge_reason              TEXT,
  provider_echoed_model_id  TEXT,                    -- [pf] the only trustworthy model id: from the response, not the config
  latency_ms                INTEGER,
  cost                      REAL
);
CREATE INDEX case_run_idx   ON case_results (run_id, case_id, prompt_idx);
CREATE INDEX case_model_idx ON case_results (run_id, provider_echoed_model_id);

CREATE TABLE assertion_results (
  id             INTEGER PRIMARY KEY,
  case_result_id INTEGER NOT NULL REFERENCES case_results(id) ON DELETE CASCADE,
  leaf_idx       INTEGER NOT NULL,
  type           TEXT NOT NULL,
  pass           INTEGER NOT NULL CHECK (pass IN (0,1)),
  score          REAL,
  reason         TEXT
);
CREATE INDEX assert_case_idx ON assertion_results (case_result_id);

CREATE TABLE decisions (
  decision_id   INTEGER PRIMARY KEY,
  made_at       TEXT NOT NULL,
  run_id        TEXT REFERENCES runs(run_id),
  kind          TEXT NOT NULL CHECK (kind IN (
                  'BASELINE_SET','GATE_BLOCK','GATE_PASS','DRIFT_ALERT',
                  'QUARANTINE','JUDGE_CHANGE','REBASELINE','MODEL_DRIFT')),
  verdict       TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  rationale     TEXT NOT NULL,
  superseded_by INTEGER REFERENCES decisions(decision_id)
);
CREATE INDEX decisions_run_idx ON decisions (run_id, kind, made_at DESC);

CREATE TABLE baselines (
  compat_key  TEXT PRIMARY KEY,                      -- golden_set_hash|judge_snapshot|assertion_config_hash
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  set_at      TEXT NOT NULL,
  decision_id INTEGER NOT NULL REFERENCES decisions(decision_id)
);
"""


def open_db(path):
    """Open (creating the schema once) WITHOUT destroying existing history.

    Nightly drift is a time series; a run that wipes the series it is meant to
    extend reports OK forever. connect() below is the fixture/demo path and is
    destructive on purpose -- these two must stay separate.
    """
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    have = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='runs'"
                      ).fetchone()
    if not have:
        db.executescript(DDL)
    else:
        db.execute("PRAGMA foreign_keys = ON")
    return db


def connect(path):
    """Destructive: drops any existing DB. For fixtures and the demo only."""
    if os.path.exists(path):
        os.remove(path)
    return open_db(path)


def compat_key(r):
    return "|".join([r["golden_set_hash"], r["judge_snapshot"], r["assertion_config_hash"]])


def decide(db, run_id, kind, verdict, evidence, rationale):
    cur = db.execute(
        "INSERT INTO decisions (made_at, run_id, kind, verdict, evidence_json, rationale)"
        " VALUES (?,?,?,?,?,?)",
        (iso(NOW), run_id, kind, verdict, json.dumps(evidence, sort_keys=True), rationale),
    )
    return cur.lastrowid


# --------------------------------------------------------------------------
# ingest: real promptfoo export -> case_results / assertion_results
# --------------------------------------------------------------------------
def leaves(grading_result):
    """Flat componentResults under an assert-set contains the WRAPPER plus the
    promoted children. A leaf has an 'assertion' and no componentResults of its own."""
    comps = (grading_result or {}).get("componentResults") or []
    return [c for c in comps if c.get("assertion") and not c.get("componentResults")]


def ingest(db, run_id, export_path):
    rows = json.load(open(export_path))["results"]["results"]
    for r in rows:
        gr = r.get("gradingResult")
        leaf = leaves(gr)
        # failureReason: NONE=0, ASSERT=1, ERROR=2. ONLY 2 is infra.
        # row["error"] is ALSO populated on a plain assertion failure (it holds the
        # assertion reason), so `if row.get("error")` would misclassify every FAILED
        # row as ERROR and silently destroy the infra-vs-quality split.
        fr = r.get("failureReason")
        if fr == 2 or (fr is None and r.get("error")):
            status = "ERROR"
        elif not leaf:
            status = "UNSCORED"          # e.g. gradingResult.reason == "No assertions"
        else:
            status = "PASSED" if r.get("success") else "FAILED"
        md = (r.get("response") or {}).get("metadata") or {}
        cur = db.execute(
            "INSERT INTO case_results (run_id, case_id, prompt_idx, repeat_index, row_idx,"
            " status, score, output, judge_reason, provider_echoed_model_id, latency_ms, cost)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, r["vars"].get("case_id"), r.get("promptIdx"), md.get("repeatIndex"),
             r.get("testIdx"), status, r.get("score"), str(r.get("response", {}).get("output")),
             (gr or {}).get("reason"), (md.get("modelId") or md.get("model_id")), r.get("latencyMs"), r.get("cost")),
        )
        for i, c in enumerate(leaf):
            db.execute(
                "INSERT INTO assertion_results (case_result_id, leaf_idx, type, pass, score, reason)"
                " VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, i, c["assertion"]["type"], 1 if c.get("pass") else 0,
                 c.get("score"), c.get("reason")),
            )
    return len(rows)


# --------------------------------------------------------------------------
# (b) baseline selection, with an explicit INCOMPATIBLE verdict
# --------------------------------------------------------------------------
BASELINE_SQL = """
WITH cand AS (SELECT * FROM runs WHERE run_id = :cand),
     base AS (
       SELECT r.* FROM runs r, cand c
       WHERE r.branch = 'main' AND r.outcome = 'OK' AND r.started_at < c.started_at
       ORDER BY r.started_at DESC LIMIT 1
     )
SELECT b.run_id AS baseline_run, b.started_at, b.pass_rate,
       CASE WHEN b.golden_set_hash = c.golden_set_hash
             AND b.judge_snapshot  = c.judge_snapshot
             AND b.assertion_config_hash = c.assertion_config_hash
            THEN 'COMPATIBLE' ELSE 'INCOMPATIBLE' END AS verdict,
       rtrim(
         CASE WHEN b.golden_set_hash      <> c.golden_set_hash      THEN 'golden_set_hash ' ELSE '' END ||
         CASE WHEN b.judge_snapshot       <> c.judge_snapshot       THEN 'judge_snapshot ' ELSE '' END ||
         CASE WHEN b.assertion_config_hash<> c.assertion_config_hash THEN 'assertion_config_hash ' ELSE '' END
       ) AS differing_fields
FROM base b, cand c;
"""


def select_baseline(db, cand):
    row = db.execute(BASELINE_SQL, {"cand": cand}).fetchone()
    if row is None:
        return {"verdict": "NO_BASELINE", "baseline_run": None, "differing_fields": ""}
    return dict(row)


# --------------------------------------------------------------------------
# (c) model drift between consecutive nightly runs, independent of quality
# --------------------------------------------------------------------------
MODEL_DRIFT_SQL = """
WITH n AS (
  SELECT r.run_id, r.started_at, r.pass_rate,
         (SELECT group_concat(m, ',') FROM
            (SELECT DISTINCT provider_echoed_model_id AS m FROM case_results
             WHERE run_id = r.run_id AND provider_echoed_model_id IS NOT NULL ORDER BY 1)
         ) AS models
  FROM runs r WHERE r.trigger = 'nightly'
), lagged AS (
  SELECT run_id, started_at, pass_rate, models,
         LAG(run_id)    OVER w AS prev_run,
         LAG(models)    OVER w AS prev_models,
         LAG(pass_rate) OVER w AS prev_pass_rate
  FROM n WINDOW w AS (ORDER BY started_at)
)
SELECT * FROM lagged
WHERE prev_models IS NOT NULL AND models IS NOT NULL AND models <> prev_models;
"""


def detect_model_drift(db):
    out = []
    # Detection is a read, but logging the decision is a write, so a re-dispatch
    # of the nightly would file the same swap twice. One decision per run.
    logged = {r[0] for r in db.execute(
        "SELECT run_id FROM decisions WHERE kind='MODEL_DRIFT'")}
    for d in db.execute(MODEL_DRIFT_SQL).fetchall():
        if d["run_id"] in logged:
            continue
        quality_moved = abs((d["pass_rate"] or 0) - (d["prev_pass_rate"] or 0)) > 1e-9
        decide(
            db, d["run_id"], "MODEL_DRIFT",
            "SERVED_MODEL_CHANGED",
            {"prev_run": d["prev_run"], "prev_models": d["prev_models"], "models": d["models"],
             "prev_pass_rate": d["prev_pass_rate"], "pass_rate": d["pass_rate"],
             "quality_moved": quality_moved},
            "provider-echoed model id changed between consecutive nightlies; "
            "fires on identity, not on score, so a silent swap is caught even at flat pass rate.",
        )
        out.append(dict(d) | {"quality_moved": quality_moved})
    return out


# --------------------------------------------------------------------------
# (d) dead-man's switch
# --------------------------------------------------------------------------
DEADMAN_SQL = """
SELECT (SELECT max(started_at) FROM runs WHERE trigger='nightly') AS last_nightly,
       :now AS now_utc,
       CASE WHEN (SELECT max(started_at) FROM runs WHERE trigger='nightly')
                 IS NULL
                 OR (SELECT max(started_at) FROM runs WHERE trigger='nightly') < :cutoff
            THEN 'ALERT_NO_NIGHTLY' ELSE 'OK' END AS verdict;
"""


def deadman(db, hours=30):
    return dict(db.execute(DEADMAN_SQL,
                           {"now": iso(NOW), "cutoff": ago(hours)}).fetchone())


# --------------------------------------------------------------------------
# fixtures + demo
# --------------------------------------------------------------------------
RUN_COLS = ("run_id git_sha branch started_at ended_at trigger promptfoo_version "
            "model_under_test_snapshot judge_snapshot golden_set_hash assertion_config_hash "
            "config_dir exit_code outcome n_cases_expected n_cases_seen successes failures "
            "errors pass_rate").split()


def add_run(db, **kw):
    kw.setdefault("promptfoo_version", "0.123.1+29")
    kw.setdefault("config_dir", os.environ.get("PROMPTFOO_CONFIG_DIR", ""))
    kw.setdefault("ended_at", kw["started_at"])
    db.execute("INSERT INTO runs ({}) VALUES ({})".format(
        ",".join(RUN_COLS), ",".join("?" * len(RUN_COLS))),
        [kw.get(c) for c in RUN_COLS])
    return kw["run_id"]


def add_cases(db, run_id, model_id, n_pass, n_fail):
    for i in range(n_pass + n_fail):
        db.execute("INSERT INTO case_results (run_id, case_id, prompt_idx, repeat_index, row_idx,"
                   " status, score, provider_echoed_model_id) VALUES (?,?,?,?,?,?,?,?)",
                   (run_id, f"case{i}", 0, 0, i,
                    "PASSED" if i < n_pass else "FAILED", 1.0 if i < n_pass else 0.0, model_id))


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    db = connect(os.path.join(here, "regressgate.db"))

    hr("(a) SCHEMA  (sqlite_schema, i.e. `.schema`)")
    for (s,) in db.execute(
            "SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY rootpage"):
        print(s + ";")

    hr("INGEST of a REAL promptfoo export (--repeat 2, assert-set, one un-asserted case)")
    exp = os.path.join(os.path.dirname(here), "export.json")
    if os.path.exists(exp):
        add_run(db, run_id="r_ing", git_sha="deadbee", branch="main", started_at=ago(200),
                trigger="manual", model_under_test_snapshot="fixture", judge_snapshot="none",
                golden_set_hash="G_ING", assertion_config_hash="A_ING", exit_code=0,
                outcome="TESTS_FAILED", n_cases_expected=3, n_cases_seen=3,
                successes=4, failures=2, errors=0, pass_rate=66.67)
        print("rows ingested:", ingest(db, "r_ing", exp))
        for r in db.execute(
                "SELECT case_id, repeat_index, row_idx, status, judge_reason,"
                " (SELECT count(*) FROM assertion_results a WHERE a.case_result_id=c.id) AS leaves"
                " FROM case_results c WHERE run_id='r_ing' ORDER BY row_idx"):
            print(dict(r))
        err = os.path.join(os.path.dirname(here), "export_err.json")
        if os.path.exists(err):
            add_run(db, run_id="r_err", git_sha="deadbee", branch="main", started_at=ago(199),
                    trigger="manual", model_under_test_snapshot="fixture", judge_snapshot="none",
                    golden_set_hash="G_ING", assertion_config_hash="A_ING", exit_code=100,
                    outcome="TESTS_FAILED", n_cases_expected=1, n_cases_seen=1,
                    successes=0, failures=0, errors=1, pass_rate=0.0)
            ingest(db, "r_err", err)
            for r in db.execute("SELECT case_id, repeat_index, status, judge_reason,"
                                " provider_echoed_model_id FROM case_results"
                                " WHERE run_id='r_err'"):
                print(dict(r))
    else:
        print("SKIP: no export.json")

    # ---- fixture timeline -------------------------------------------------
    G1, J1, A1 = "G_sha_aaa", "judge_gpt4o_2026_02", "A_sha_111"
    add_run(db, run_id="r1", git_sha="1111111", branch="main", started_at=ago(120),
            trigger="nightly", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=0, outcome="OK", n_cases_expected=40, n_cases_seen=40,
            successes=40, failures=0, errors=0, pass_rate=100.0)
    add_cases(db, "r1", "served-model-2026-01", 40, 0)

    add_run(db, run_id="r2", git_sha="2222222", branch="main", started_at=ago(96),
            trigger="nightly", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=0, outcome="OK", n_cases_expected=40, n_cases_seen=40,
            successes=40, failures=0, errors=0, pass_rate=100.0)
    add_cases(db, "r2", "served-model-2026-01", 40, 0)

    # r3: SAME pass rate, DIFFERENT served model -> drift must fire on identity alone
    add_run(db, run_id="r3", git_sha="3333333", branch="main", started_at=ago(40),
            trigger="nightly", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=0, outcome="OK", n_cases_expected=40, n_cases_seen=40,
            successes=40, failures=0, errors=0, pass_rate=100.0)
    add_cases(db, "r3", "served-model-2026-03", 40, 0)

    # r4: main run that FAILED the gate -> must never be chosen as a baseline
    add_run(db, run_id="r4", git_sha="4444444", branch="main", started_at=ago(36),
            trigger="manual", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=100, outcome="TESTS_FAILED", n_cases_expected=40, n_cases_seen=40,
            successes=30, failures=10, errors=0, pass_rate=75.0)

    # r5: PR candidate, identical hashes -> COMPATIBLE against r3
    add_run(db, run_id="r5", git_sha="5555555", branch="pr/1234", started_at=ago(2),
            trigger="pr", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=0, outcome="OK", n_cases_expected=40, n_cases_seen=40,
            successes=39, failures=1, errors=0, pass_rate=97.5)

    # r6: PR candidate that grew the golden set AND swapped the judge -> INCOMPATIBLE
    add_run(db, run_id="r6", git_sha="6666666", branch="pr/1235", started_at=ago(1),
            trigger="pr", model_under_test_snapshot="cfg:model-x",
            judge_snapshot="judge_claude_2026_06", golden_set_hash="G_sha_bbb",
            assertion_config_hash=A1, exit_code=0, outcome="OK",
            n_cases_expected=52, n_cases_seen=52,
            successes=50, failures=2, errors=0, pass_rate=96.15)
    db.commit()

    hr("(b) BASELINE SELECTION")
    for cand in ("r5", "r6"):
        res = select_baseline(db, cand)
        print(f"candidate={cand} -> {res}")
        if res["verdict"] == "COMPATIBLE":
            did = decide(db, cand, "BASELINE_SET", "COMPATIBLE",
                         {"baseline_run": res["baseline_run"],
                          "baseline_pass_rate": res["pass_rate"],
                          "compat_key": compat_key(db.execute(
                              "SELECT * FROM runs WHERE run_id=?", (cand,)).fetchone())},
                         "last main run with outcome=OK and identical golden/judge/assertion hashes")
            db.execute("INSERT OR REPLACE INTO baselines VALUES (?,?,?,?)",
                       (compat_key(db.execute("SELECT * FROM runs WHERE run_id=?",
                                              (cand,)).fetchone()),
                        res["baseline_run"], iso(NOW), did))
        else:
            decide(db, cand, "GATE_BLOCK", "REFUSED_INCOMPATIBLE_COMPARISON",
                   {"baseline_run": res["baseline_run"],
                    "differing_fields": res["differing_fields"]},
                   "refuse to diff runs whose golden set / judge / assertions differ; "
                   "a pass-rate delta across them is not a measurement")
    db.commit()
    print("\nbaselines table:")
    for r in db.execute("SELECT * FROM baselines"):
        print(dict(r))

    hr("(c) MODEL DRIFT (consecutive nightlies, independent of quality)")
    for d in detect_model_drift(db):
        print(d)
    db.commit()
    print("\nMODEL_DRIFT decision rows:")
    for r in db.execute("SELECT decision_id, run_id, kind, verdict, evidence_json"
                        " FROM decisions WHERE kind='MODEL_DRIFT'"):
        print(dict(r))

    hr("(d) DEAD-MAN'S SWITCH (no nightly in the last 30h)")
    d1 = deadman(db)
    print("before tonight's run:", d1)
    if d1["verdict"] != "OK":
        decide(db, None, "DRIFT_ALERT", d1["verdict"], d1,
               "nightly scheduler is external to promptfoo (it has none); "
               "absence of a run is invisible to any exit code, so the store must assert it")
    add_run(db, run_id="r7", git_sha="7777777", branch="main", started_at=ago(0.5),
            trigger="nightly", model_under_test_snapshot="cfg:model-x",
            judge_snapshot=J1, golden_set_hash=G1, assertion_config_hash=A1,
            exit_code=0, outcome="OK", n_cases_expected=40, n_cases_seen=40,
            successes=40, failures=0, errors=0, pass_rate=100.0)
    add_cases(db, "r7", "served-model-2026-03", 40, 0)
    db.commit()
    d2 = deadman(db)
    print("after  tonight's run:", d2)

    hr("RECALL: the full decision log (what a future 'why?' reads)")
    for r in db.execute("SELECT decision_id, run_id, kind, verdict, rationale FROM decisions"):
        print(dict(r))

    # ---- self-check -------------------------------------------------------
    assert select_baseline(db, "r5")["baseline_run"] == "r3", "must skip TESTS_FAILED r4"
    assert select_baseline(db, "r6")["verdict"] == "INCOMPATIBLE"
    assert "golden_set_hash" in select_baseline(db, "r6")["differing_fields"]
    assert d1["verdict"] == "ALERT_NO_NIGHTLY" and d2["verdict"] == "OK"
    drift = db.execute("SELECT count(*) FROM decisions WHERE kind='MODEL_DRIFT'").fetchone()[0]
    assert drift == 1, drift
    ev = json.loads(db.execute("SELECT evidence_json FROM decisions WHERE kind='MODEL_DRIFT'"
                               ).fetchone()[0])
    assert ev["quality_moved"] is False, "drift must fire with pass rate unchanged"
    if os.path.exists(exp):
        st = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, count(*) n FROM case_results WHERE run_id='r_ing' GROUP BY status")}
        assert st == {"PASSED": 2, "FAILED": 2, "UNSCORED": 2}, st
        nleaf = db.execute("SELECT count(*) FROM assertion_results").fetchone()[0]
        assert nleaf == 6, nleaf   # alpha 2 leaves x2 rows + beta 1 leaf x2 rows
        err_st = db.execute("SELECT status, repeat_index FROM case_results"
                            " WHERE run_id='r_err'").fetchall()
        assert [tuple(r) for r in err_st] == [("ERROR", None)], [tuple(r) for r in err_st]
    print("\nSELF-CHECK OK")


if __name__ == "__main__":
    sys.exit(main())
