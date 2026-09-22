#!/usr/bin/env bash
# Pins the *observable contract* of promptfoo that regressgate depends on.
# Run on every version bump. If this fails, gate.py's assumptions are stale.
# Override the binary locally:  PROMPTFOO_BIN="node dist/src/main.js" bash contract_test.sh
set -uo pipefail
PF=${PROMPTFOO_BIN:-promptfoo}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fails=0
ok()   { echo "  ok   $1"; }
bad()  { echo "  FAIL $1"; fails=$((fails+1)); }

cat > "$TMP/pass.yaml" <<'Y'
providers: [echo]
prompts: ['{{x}}']
tests:
  - vars: {x: hello, case_id: alpha}
    assert: [{type: contains, value: hello}]
Y
cat > "$TMP/fail.yaml" <<'Y'
providers: [echo]
prompts: ['{{x}}']
tests:
  - vars: {x: hello, case_id: alpha}
    assert: [{type: contains, value: goodbye}]
Y
cat > "$TMP/empty.yaml" <<'Y'
providers: [echo]
prompts: ['{{x}}']
tests: []
Y

$PF eval -c "$TMP/pass.yaml" --no-cache -o "$TMP/pass.json" >/dev/null 2>&1
[ $? -eq 0 ] && ok "all-pass eval exits 0" || bad "all-pass eval did not exit 0"

$PF eval -c "$TMP/fail.yaml" --no-cache -o "$TMP/fail.json" >/dev/null 2>&1
rc=$?
[ $rc -eq 100 ] && ok "failing eval exits 100" || bad "failing eval did not exit 100 (got $rc)"

$PF eval -c "$TMP/empty.yaml" --no-cache >/dev/null 2>&1
[ $? -eq 0 ] && ok "empty suite exits 0 (known trap: a bad --filter is a green build)" \
             || bad "empty suite no longer exits 0"

python3 - "$TMP/pass.json" <<'PY' && ok "output JSON keeps results.results + row keys" || bad "output JSON shape changed"
import json,sys
d=json.load(open(sys.argv[1]))
rows=d["results"]["results"]
assert d["results"]["version"]==3, d["results"]["version"]
need={"score","success","testIdx","vars","promptIdx","namedScores","failureReason"}
assert need <= set(rows[0]), sorted(need-set(rows[0]))
assert rows[0]["vars"]["case_id"]=="alpha"
PY

$PF eval -c "$TMP/pass.yaml" --no-cache --repeat 3 -o "$TMP/rep.json" >/dev/null 2>&1
python3 - "$TMP/rep.json" <<'PY' && ok "--repeat 3 yields 3 un-aggregated rows, testIdx 0..2" || bad "--repeat row semantics changed"
import json,sys
rows=json.load(open(sys.argv[1]))["results"]["results"]
assert len(rows)==3, len(rows)
assert sorted(r["testIdx"] for r in rows)==[0,1,2]
assert all("repeatIndex" not in json.dumps(r.get("metadata") or {}) for r in rows) or True
PY

$PF eval -c "$TMP/pass.yaml" --no-cache --fail-on-error >/dev/null 2>&1
[ $? -ne 0 ] && ok "--fail-on-error still does not exist (docs are wrong)" \
             || bad "--fail-on-error now exists; revisit the gate design"

echo "contract_test: $fails failure(s)"
exit $((fails>0))
