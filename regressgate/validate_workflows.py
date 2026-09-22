"""Parse the two workflows and assert the environment discipline is actually encoded."""
import os, sys, yaml

# Resolve against the repo root, not the cwd. A validator that only runs from
# one directory is a validator that quietly does not run.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ON = True  # PyYAML 1.1: the key `on:` parses as the boolean True, not the string "on".

def check(path, want_schedule):
    raw = open(os.path.join(ROOT, path)).read()
    d = yaml.safe_load(raw)
    # Scan CODE only -- both files discuss these traps in comments on purpose.
    code = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#"))
    print(f"--- {path}")
    print(f"  parsed OK; top-level keys: {sorted(map(str, d))}")
    trig = d[ON]
    print(f"  triggers: {sorted(map(str, trig))}")
    env = d.get("env", {})
    assert env.get("PROMPTFOO_DISABLE_TELEMETRY") == "1", "telemetry not disabled"
    print("  PROMPTFOO_DISABLE_TELEMETRY='1' at workflow scope: yes")
    assert "PROMPTFOO_PASS_RATE_THRESHOLD" not in code, "threshold var must never be set"
    print("  PROMPTFOO_PASS_RATE_THRESHOLD never set: yes")
    assert "--fail-on-error" not in code, "--fail-on-error does not exist"
    print("  --fail-on-error never used: yes")
    assert code.count("--no-cache") >= 1
    print(f"  --no-cache occurrences: {code.count('--no-cache')}")
    n = code.count("PROMPTFOO_CONFIG_DIR: ${{ runner.temp }}")
    assert n >= 1, "no per-run config dir"
    print(f"  per-run PROMPTFOO_CONFIG_DIR (runner.temp, run_id+attempt): {n}")
    cc = d.get("concurrency", {})
    print(f"  concurrency: group={cc.get('group')!r} cancel-in-progress={cc.get('cancel-in-progress')}")
    if want_schedule:
        assert "schedule" in trig and "workflow_dispatch" in trig
        print(f"  cron: {[s['cron'] for s in trig['schedule']]}")
        assert cc.get("cancel-in-progress") is False, "drift must not cancel in progress"
        assert "108000" in code, "30h dead-man's switch missing"
        print("  dead-man's switch threshold 108000s (30h): present")
        assert "date -u +%F" in code, "date-keyed idempotency missing"
        print("  date-keyed idempotency marker: present")
    for job, spec in d["jobs"].items():
        print(f"  job {job}: {len(spec.get('steps', []))} steps, needs={spec.get('needs')}")

check(".github/workflows/gate.yml", False)
check(".github/workflows/drift.yml", True)
print("\nALL WORKFLOW ASSERTIONS PASSED")
