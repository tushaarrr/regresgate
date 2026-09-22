#!/usr/bin/env bash
# Assemble and run the WHOLE pipeline against the real promptfoo binary.
#
# This exists because five components were each individually correct and did not
# compose: the gate imported a stats module it disagreed with, two modules
# classified the same row differently, and one module's scenario suite had been
# passing against its own stub. Unit self-checks cannot see any of that.
#
#   PROMPTFOO_BIN="node /path/to/promptfoo/dist/src/main.js" bash e2e_test.sh
#
# Never read promptfoo's exit code through a pipe: `foo | tail` gives you tail's
# status. Every invocation below captures to a file and reads $? directly.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PF_BIN=${PROMPTFOO_BIN:-promptfoo}
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
ok()  { echo "  ok   $1"; }
bad() { echo "  FAIL $1"; fails=$((fails+1)); }

pf() { # pf <out> [env assignments already exported] ; echoes the exit code
  local out=$1; shift
  PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_CONFIG_DIR="$T/cfg-$(basename "$out" .json)" \
    $PF_BIN eval -c "$ROOT/eval/offline/promptfooconfig.yaml" --no-cache --repeat 3 \
    --no-progress-bar --no-table --no-write -o "$out" >"$T/pf.log" 2>&1
  echo $?
}
decision() { grep -o 'DECISION=[A-Z_]*' "$1" | head -1 | cut -d= -f2; }

cd "$HERE" || exit 1

# The OFFLINE fixture, not the real suite. This test proves the HARNESS
# composes -- pair, gate, cache, quarantine, drift -- and for that it needs a
# provider whose output it can break on demand and whose answers never change
# for reasons of their own. The real 300-case suite is measured against a real
# model and cannot do either. Both configs go through the same code path.
echo "== preflight =="
python3 preflight.py --config "$ROOT/eval/offline/promptfooconfig.yaml" --out h.json >"$T/pre.log" 2>&1
[ $? -eq 0 ] && ok "config carries no sharing:, no junit path, no threshold env" \
             || { bad "preflight rejected the config"; cat "$T/pre.log"; }

echo "== baseline run, promoted =="
code=$(pf "$T/base.json")
[ "$code" = 0 ] && ok "clean baseline exits 0" || bad "baseline exited $code"
python3 fetch_baseline.py --head "$T/base.json" --dir "$T/bl" --promote \
  --git-sha base-sha >"$T/promote.log" 2>&1
[ $? -eq 0 ] && ok "baseline promoted under its contract hash" || bad "promote failed"

# The fixture's OWN manifest, generated from its own baseline run. Borrowing
# regressgate/cases.manifest.json would compare a 12-case fixture against the
# 300-case real suite and HARD_FAIL on the count -- which is the gate working,
# but it is not what this test is asking.
python3 pair.py --head "$T/base.json" --write-manifest "$T/manifest.json" >/dev/null 2>&1
[ $? -eq 0 ] && ok "fixture manifest generated from its own baseline" || bad "manifest failed"

echo "== head run with a real regression =="
REGRESSGATE_DEMO_BREAK=2 code=$(REGRESSGATE_DEMO_BREAK=2 pf "$T/head.json")
[ "$code" = 100 ] && ok "failing head exits 100 (not 1, not 0)" || bad "head exited $code"
python3 fetch_baseline.py --head "$T/head.json" --dir "$T/bl" --out "$T/baseline.json" \
  >"$T/fetch.log" 2>&1
[ $? -eq 0 ] && ok "head found its baseline by contract" || bad "baseline lookup missed"

# A healthy-suite quarantine file, so the BLOCK path is exercised. The REAL one
# for this 12-case placeholder reports power@-5pp = 0.001 and is used below.
echo '{"quarantined":[],"watchlist":[],"power_ok":true,"measured_at":"'"$(date -u +%FT%TZ)"'"}' \
  > "$T/q_healthy.json"

python3 pair.py --head "$T/head.json" --baseline "$T/baseline.json" \
  --manifest "$T/manifest.json" --quarantine "$T/q_healthy.json" \
  --head-sha head-sha-1 --baseline-sha base-sha --out "$T/pairing.json" >"$T/pair.log" 2>&1
[ $? -eq 0 ] && ok "pairer built a pairing doc" || { bad "pair.py failed"; cat "$T/pair.log"; }
grep -q '"paired": 36' "$T/pairing.json" \
  && ok "36 paired samples (12 cases x 3 repeats), none unpaired" \
  || bad "pairing coverage is not 36: $(grep coverage -A3 "$T/pairing.json" | tr -d '\n')"

echo "== the gate =="
python3 gate.py "$T/pairing.json" --cache-db "$T/v.db" >"$T/gate1.log" 2>&1
rc=$?
[ "$rc" = 20 ] && [ "$(decision "$T/gate1.log")" = BLOCK ] \
  && ok "real regression -> BLOCK, exit 20" || bad "expected BLOCK/20, got $(decision "$T/gate1.log")/$rc"

echo "== retry-until-green: same commit, code secretly FIXED, re-run CI =="
code=$(pf "$T/head_fixed.json")
python3 pair.py --head "$T/head_fixed.json" --baseline "$T/baseline.json" \
  --manifest "$T/manifest.json" --quarantine "$T/q_healthy.json" \
  --head-sha head-sha-1 --baseline-sha base-sha --out "$T/pairing_retry.json" >/dev/null 2>&1
python3 gate.py "$T/pairing_retry.json" --cache-db "$T/v.db" >"$T/gate2.log" 2>&1
rc=$?
[ "$rc" = 20 ] && grep -q "CACHED verdict" "$T/gate2.log" \
  && ok "re-run of the same commit replayed the stored BLOCK" \
  || bad "the re-run was re-measured (exit $rc); the retry defence is off"

echo "== a new commit is a genuinely new measurement =="
python3 pair.py --head "$T/head_fixed.json" --baseline "$T/baseline.json" \
  --manifest "$T/manifest.json" --quarantine "$T/q_healthy.json" \
  --head-sha head-sha-2 --baseline-sha base-sha --out "$T/pairing_new.json" >/dev/null 2>&1
python3 gate.py "$T/pairing_new.json" --cache-db "$T/v.db" >"$T/gate3.log" 2>&1
rc=$?
[ "$rc" = 0 ] && [ "$(decision "$T/gate3.log")" = PASS ] \
  && ok "new commit re-measured -> PASS" || bad "expected PASS/0, got $(decision "$T/gate3.log")/$rc"

echo "== guardrail B: the real 12-case suite cannot see -5pp, so it must not block =="
python3 pair.py --head "$T/head.json" --baseline "$T/baseline.json" \
  --manifest "$T/manifest.json" --quarantine quarantine.json \
  --head-sha head-sha-3 --baseline-sha base-sha --out "$T/pairing_weak.json" >/dev/null 2>&1
python3 gate.py "$T/pairing_weak.json" >"$T/gate4.log" 2>&1
rc=$?
[ "$rc" = 0 ] && [ "$(decision "$T/gate4.log")" = COMMENT ] \
  && grep -q "power@-5pp" "$T/gate4.log" \
  && ok "below the power floor -> COMMENT, with the reason in the comment" \
  || bad "expected a degraded COMMENT, got $(decision "$T/gate4.log")/$rc"

echo "== contract drift is refused, not measured =="
python3 - "$T/head.json" "$T/drifted.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
# Mutate the RESOLVED test on the rows, which is what editing a case file
# actually produces -- promptfoo re-runs and every row carries the new assert.
# Mutating config.tests alone would not do it: the published promptfoo leaves
# that key as raw file:// strings, which is the bug this pairing was fixed for.
for r in d["results"]["results"]:
    if (r.get("testCase", {}).get("vars") or {}).get("case_id") == "refund-window":
        r["testCase"]["assert"][0]["value"] = "something else entirely"
t = d["config"].get("tests")
if isinstance(t, list) and t and isinstance(t[0], dict):
    t[0]["assert"][0]["value"] = "something else entirely"
json.dump(d, open(sys.argv[2], "w"))
PY
python3 pair.py --head "$T/drifted.json" --baseline "$T/baseline.json" \
  --manifest "$T/manifest.json" --head-sha d1 --baseline-sha base-sha \
  --out "$T/pairing_drift.json" >/dev/null 2>&1
python3 gate.py "$T/pairing_drift.json" >"$T/gate5.log" 2>&1
rc=$?
[ "$rc" = 30 ] && ok "a changed assertion -> REFUSE, exit 30 (no delta invented)" \
              || bad "expected REFUSE/30, got $(decision "$T/gate5.log")/$rc"

echo "== phase 2: A/A replays -> churn, quarantine, power =="
for i in 1 2 3; do code=$(pf "$T/aa$i.json"); done
python3 quarantine.py --runs "$T"/aa*.json --out "$T/q.json" >"$T/q.log" 2>&1
rc=$?
[ "$rc" = 0 ] && ok "A/A churn under the 6% ceiling, exclusions under the cap" \
             || { bad "quarantine guardrails failed"; cat "$T/q.log"; }
grep -q '"churn": 0.0' "$T/q.json" && ok "deterministic placeholder provider -> 0% churn" \
                                   || bad "unexpected churn in an A/A replay"

echo "== phase 5: nightly store, EWMA, model drift, dead-man =="
for i in 1 2 3; do
  python3 drift_monitor.py record --export "$T/aa$i.json" --db "$T/runs.db" \
    --git-sha "n$i" --repeat 3 --run-id "night-$i" >"$T/rec.log" 2>&1
done
REGRESSGATE_DEMO_MODEL=demo-model-2026-03 code=$(REGRESSGATE_DEMO_MODEL=demo-model-2026-03 pf "$T/swap.json")
python3 drift_monitor.py record --export "$T/swap.json" --db "$T/runs.db" \
  --git-sha n4 --repeat 3 --run-id night-4 >>"$T/rec.log" 2>&1
python3 drift_monitor.py check --db "$T/runs.db" >"$T/mon.log" 2>&1
grep -q "demo-model-2026-01 -> demo-model-2026-03" "$T/mon.log" \
  && grep -q "quality_moved=False" "$T/mon.log" \
  && ok "served model swapped at a FLAT pass rate and the monitor caught it" \
  || { bad "model drift not detected"; cat "$T/mon.log"; }
grep -q "dead-man's switch : OK" "$T/mon.log" \
  && ok "dead-man's switch OK with a fresh nightly" || bad "dead-man's switch wrong"

echo
echo "e2e_test: $fails failure(s)"
exit $((fails > 0))
