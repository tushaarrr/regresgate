# Model Regression Detection — Build Plan (rev. 2)

Revised 2026-09-21. Supersedes the Sep 21 draft.

Every claim in the original draft was tested against promptfoo `0.123.1+29` (tag `0.123.1` plus
29 commits) by execution, and every finding was re-checked at the released tag. The harness in
this repo was then built and run against the real binary. **This revision is driven by what broke.**

---

## 0. What changed, and why

Five things in the original plan were wrong or unsafe. In descending order of consequence:

| # | The draft said | Measured reality | Consequence |
|---|---|---|---|
| 1 | *(nothing)* | **Re-running CI ships a −5pp regression 58% of the time after 5 attempts** (`retry_sim.py`) | The entire N-vs-churn apparatus was decoration. Fixed by verdict caching. |
| 2 | Build an outcome-learning loop that recalibrates thresholds | **A loop is dominated by a zero-label policy, and its recommended substitute goes blind while reporting itself calibrated** | The RHCL layer is cut to four guardrails. |
| 3 | `--repeat` + cache replays one value N times (issue #360) | **False since 0.121.4.** Cache is namespaced per repeat index | The trap is real but *cross-run*, and nastier than described. |
| 4 | Read `__repeatIndex` in a Python assertion | **It is `None` there, stripped by design** | The documented workaround cannot work. Use provider echo. |
| 5 | EWMA gives ~1 false alarm per 2.4 years | **~1.24–1.53 years** | The alert-fatigue argument is weaker than claimed. State the real number. |

Two claims survived exactly: the 0.57pp-vs-1.73pp σ argument (simulated 0.5753) and the 78.5%
30-night false-alarm figure (78.5361%). The sizing table reproduces as *power-0.80 Connor numbers
× the 1.15 buffer the draft states* — it is self-consistent and stays.

---

## 1. Positioning

promptfoo is a test executor. Everything after the run is unbuilt, and that gap is the product.
The gap table, restated so it survives a reviewer who greps the source:

| Capability | promptfoo status (verified) |
|---|---|
| Run-over-run baseline comparison | No `compare` subcommand and no CLI-native diff. Comparison exists **only** in the web UI and local HTTP API (`comparisonEvalIds`), is presentational — no delta, no verdict, no exit code — and is dataset-locked |
| Delta gating | Absent. `PROMPTFOO_PASS_RATE_THRESHOLD` is an absolute percent with a strict `<` |
| Scheduled runs | Absent. No cron, no interval runner, no scheduler dependency. `eval --watch` is *file*-triggered, not time-triggered |
| Alerting | Absent. `slack`/`webhook` are **target providers**; `webhook` is also a grader. No notification sink |
| Statistical significance | Absent. No p-value, CI, or significance test anywhere. `prompt.metrics.score` is a **sum**, not a mean |
| Model-version drift | Absent |

### The one claim

> **We tell you whether the drop is real, and we catch the drop that no commit caused.**

Both halves remain true and neither is reachable from promptfoo. Lead with McNemar and the drift
cron, never with "golden dataset + Slack alert" — that phrase returns nine near-identical
zero-star repos.

**Be precise, not maximal.** "No run-over-run comparison anywhere" is over-broad and a reviewer
who finds `comparisonEvalIds` will discount the rest of the table. The narrow claim is stronger.

---

## 2. Architecture

Unchanged in shape: promptfoo is a subprocess you call and parse. Python core, one pinned npm
dependency, no TypeScript, raw `sqlite3`, no ORM.

```
Trigger (Actions push + schedule)
  → preflight.py (refuse `sharing:`, .junit.xml, inherited threshold env)
  → Orchestrator (runner.py: argv, env discipline, classify() — the ONE classifier)
    → promptfoo CLI (pinned, --no-cache, --no-write)
      → parse.py (rows → cases; assert-set leaves; UNSCORED ≠ FAILED)
        → fetch_baseline.py (baseline keyed by CONTRACT HASH; a miss is a REFUSE)
        → pair.py (per-sample pairs; contract_key; manifest + quarantine + power)
          → gate.py (McNemar exact + CI + quarantine) ── verdict_cache ──┐
            → alert (PR comment / Slack webhook you write)               │
                                                                 retry returns stored verdict

  nightly only: drift_monitor.py record → store.py → check (EWMA + model id + dead-man)
                                                     ^ deliberately NOT behind the cache
```

The **verdict cache sits in front of the gate**, not behind it. See §6.

---

## 3. promptfoo integration surface (corrected)

```bash
PROMPTFOO_DISABLE_TELEMETRY=1 \
promptfoo eval -c config.yaml -o out.json \
  --no-progress-bar --no-table --no-write --no-cache --repeat 3
```

Parse `out["results"]["results"]` — the doubled key. Guard every access with `.get()`.

### Exit codes — the contract is weaker than the draft assumed

| Code | Fires when | Your response |
|---|---|---|
| 0 | `passRate >= threshold` **or zero tests ran** | Continue — *after* asserting the case count |
| 100 | `passRate < threshold` | **Includes provider errors.** Read `stats.errors` vs `stats.failures` |
| 1 | Harness error **and missing API keys** (`failEvalRun`, 11 call sites) | Infra alarm; cannot distinguish rotated secret from YAML typo |
| 130 | SIGINT | Not in the original table |

`passRate = successes / (successes + failures + errors)`, so **a 429 storm is indistinguishable
from a quality regression by exit code.** The JSON is the only discriminator.

`PROMPTFOO_FAILED_TEST_EXIT_CODE` truncates rather than rejects (`7.5` → 7); `0` disables the gate.
`PROMPTFOO_PASS_RATE_THRESHOLD` typos silently become 100; `0` silently disables gating. Leave both
unset — gate in Python.

### Traps, revised

**`--fail-on-error` does not exist.** Confirmed: `unknown option`. It appears in promptfoo's own
docs in 8 places across 6 files. Do not build gating on it.

**`--repeat` + cache — not what the draft said.** Within a run, repeats are cache-namespaced
(`repeat:N`) and do *not* replay. The real trap is **across** runs: an identical re-run replays
everything from cache (0 HTTP requests). Worse, frozen values are keyed by *repeat index*, so
`--repeat 5` over a cache warmed by `--repeat 3` refreshes only indices 3–4 and silently mixes
stale and fresh samples into one distribution. Keep `--no-cache`; cite the cross-run reason.
`stats.tokenUsage.numRequests` counts rows, not requests, so it cannot detect staleness.

**`testIdx` is a row index, and `testIdx % N` is unsafe.** It breaks on per-test
`options: {repeat: K}` and on string-array vars that expand into K combinations (numeric arrays
don't expand). Both fail silently with plausible numbers.

**`__repeatIndex` is assertion-invisible by design.** In a Python `get_assert` it is `None`. Echo
it from the provider instead:

```python
def call_api(prompt, options, context):
    return {"output": ..., "metadata": {"repeatIndex": context["vars"]["__repeatIndex"],
                                        "model_id": <provider-echoed model>}}
```

Lands at both `row.metadata.*` and `row.response.metadata.*`. Group on an explicit `case_id` var
plus `row.promptIdx` — never `prompt.label`, which is the raw unrendered template.

**`componentResults` under `assert-set` is flattened**, children duplicated, and the wrapper has no
`assertion` key — `c["assertion"]["type"]` raises `TypeError`. Keep leaves only.

**`sharing:` in a config POSTs your entire eval off-box, exit 0, no warning.** Verified against a
local counting server: two gzipped POSTs carrying the test payload. `preflight.py` blocks it. This
is the highest-severity finding in the integration surface and it is not in any promptfoo doc.

**Nine `-o` extensions, not eight.** `junit.xml` is matched by `endsWith` on the whole filename and
yields a completely different schema. Never template an output path that could end `.junit.xml`.

**`--no-write` still creates the DB** — libSQL opens, WAL enables, all 26 migrations run
unconditionally. It only suppresses row writes.

**`--env-file` overrides an exported `PROMPTFOO_CONFIG_DIR`**, so concurrent CI jobs can collide in
one directory. Set the config dir *after* env-file load.

**Pin the grader.** `llm-rubric` picks its judge from whichever API keys are in the environment, so
the judge changes silently between machines. Use `--grader` or `defaultTest.options.provider`.
A recorded `judge_snapshot` that nobody enforced is an intention, not a measurement.

---

## 4. Statistical design (corrected)

Paired comparison, exact McNemar on the discordant pairs. **State the sidedness in code with a
test** — it is the difference between two different gates.

### The sidedness correction

The draft's worked example (`b=0, c=5`: χ² says p=0.025, exact says 0.0625) uses the **two-sided**
exact value. The gate uses **one-sided**, where the same case is **p=0.03125 and does fire** at
α=0.05. The example does not support the rule it illustrates. Either state it two-sided or drop it.

Worse, the naive one-sided implementation is **direction-blind** — computing the tail from
`min(b,c)` scores a pure improvement (b=17, c=4) at **p=0.0036**, identical to a 17-case
regression. Use the directional form:

```python
p = sum(comb(n, k) for k in range(c, n + 1)) / 2**n   # n = b + c
```

Measured: improvement → p=0.999 (correctly ignored); genuine small-suite drop (b=0, c=5, n=60) →
p=0.03125, CI upper −1.34pp → correctly blocks.

### Sizing

The draft's table stands (power 0.80 × the stated 1.15 buffer). Two corrections:

- The unpaired comparator is **1372** (pooled SE), not 1362. The 4.8× pairing advantage holds.
- **N=281 assumes 2% churn**, not zero. At churn 0% the paired requirement is 155.

**Build 300 cases.** Unchanged.

**The 1pp materiality floor was never sized — and it is the criterion that binds most.** The claim
here used to be that at N=300 the smallest detectable shift is ≈−2.0pp, so condition (2)
"essentially never binds on its own". Both halves were wrong. The smallest shift the one-sided
exact test can call at N=300 is **−1.67pp** (b=0, c=5, p=0.031), whose CI upper is −0.22pp — so
condition (2) turns it into a COMMENT, which is condition (2) binding. Measured over the whole
distribution by `retry_sim.py` at N=292 and the suite's measured 1.27% churn, of the 16.0% of true
−5pp regressions that survive a single run, **13.4 points are the materiality bar and 2.6 points
are the significance test**; at −3pp it is 38.9 against 24.9. The retry attack in §5 exists mostly
because (2) binds. Keep the floor, and size the suite for it deliberately; do not raise it to 2pp
on the belief that it is inert.

### The nightly drift monitor

EWMA, λ=0.2, L=3.0, σ estimated empirically. **Use MR/d₂ (moving range), not trailing SD** — it is
one line and is flat at every regression arrival rate, where trailing SD carries a static ~34%
inflation.

Corrected operating characteristics:

| Quantity | Draft claimed | Measured |
|---|---|---|
| ARL₀, σ known | 2.4 years (876 nights) | **1.53 years (555 nights)** |
| ARL₀, σ empirical | — | **1.24 years (452 nights)** |
| Detection, 3pp break | ~5 nights | **~1.4 nights** |

False alarms are ~1.6–1.9× more frequent than the draft claims; detection is *faster*. Report both.

The σ argument is confirmed: binomial says 1.7321pp, the fixed-set flip model says 0.5745pp,
simulation gives 0.5753pp — **3.02× too wide**, which would make limits three times too loose.

**There is no σ-contamination ratchet.** Over 3000 nights under both the recommended and a
deliberately hostile policy, σ̂ ends where it started and detection stays at 99.9%. Strike that
concern from the design.

---

## 5. RHCL — reinforcement calibrated learning

**Verdict: do not build the loop.** This was simulated four ways and it lost every time.

### Why not

1. **An FPR-targeting controller is structurally unstable** — and *not because of censoring*. The
   three-part rule's true FPR is 0.02–1.1%, permanently below any 5% setpoint, so the integrator
   only ever loosens. It saturates at maximum looseness by week 40 **even when fed oracle labels
   with every counterfactual handed to it free.**
2. **The gate censors its own training data, multiplicatively.** ~90% of the blocked row is never
   labelled, and `naive_FPR / true_FPR ≈ P(override | blocked, not-real)` — always biased downward,
   so FPR always looks cheaper than it is. IPW needs a propensity whose denominator is the censored
   cell. Circular.
3. **Holdout cannot buy the precision.** Five years at h=1.0 (every gate disabled) still gives
   ±103% relative precision on FPR, at a cost of 122 merged regressions.
4. **A loop is dominated by a zero-label policy.** Under churn drift 2%→10% over three years:
   LOOP 0.681 misses/wk at FDR 0.094; REPLAY (quarterly re-derivation from the A/A replays you
   already run) 0.683 at 0.091. Identical — with no censoring, no delay, and correct on day one.
5. **And REPLAY itself goes blind while reporting success.** As a judge degrades (churn 2%→20%),
   the reported null-fire rate stays inside its 0.5% budget **every quarter** while power@−5pp
   falls 0.878 → 0.192. It *tightens* rather than loosens, and ends worse than a frozen threshold.

> A calibration budget expressed as a false-positive rate is **not a safety property**. Pair every
> FPR budget with a power floor, or do not ship the budget.

6. **The data does not exist.** 0.0159 labelled events per PR — one per 63 PRs. A 200-event window
   is 12,590 PRs. Accounting for ~75% of near-threshold adjudications returning inconclusive, the
   full version yields **~6 usable labels per year.**

### What to build instead — four guardrails, no loop

**A. Verdict caching, keyed on (candidate SHA, baseline SHA, judge snapshot).**
Without it a −5pp regression ships 58% of the time after five re-runs and a −3pp ships 95% after
three. No threshold calibration touches this — the developer samples the same distribution the
gate samples. Any genuine re-measurement must **pool** repetitions; best-of-k *is* the attack.
Highest-value item in the project, and it is an afternoon. Implemented: `verdict_cache.py`.

**B. A published, enforced power floor.** Publish `power@−5pp` on the *current* suite at the
*current* churn every quarter; hard-fail to comment-only below 0.6. This is the **only** metric
that goes red in either composed failure — churn, FPR, null-fire rate and required-N all move in
the reassuring direction while the gate goes blind. Implemented: `check_power_floor()`.

**C. A hard churn ceiling (≤6%) that stops the gate, not a target that informs it.** It is the only
guardrail that catches judge degradation at the source, firing before power halves.

**D. One capped, append-only exclusion list.** The 10–15% cap must cover the **union** of the
quarantine set and condition (3)'s known-flaky watchlist. Uncapped separately, the watchlist alone
reaches 30 cases in a year of ordinary operation and costs 11 points of power at −5pp.

### Explicitly cut

- The outcome-driven recalibration loop, and any self-adjusting *decision* threshold. It converges
  to the human override rate, not to anything true.
- **Any "was this a false alarm?" button or override-derived label.** Not merely biased — a live
  attack surface. Clicking it on every override drives the threshold −1.00pp → −4.09pp, where
  power@−5pp is 0.049. A normal 10% override rate already reaches −2.96pp.
- Sequential stopping in the adjudicator (inflates the false-"real" rate and reintroduces selection
  on effect size). Fixed K=20, one test.
- The κ ≥ 0.6 ship gate. At a 90% base rate, 50 labels give a 0.68-wide CI on a 0.1-wide decision,
  and a judge sitting exactly on 0.6 implies 17.5% churn — which drops N=300 power to 0.21.
  Replace with **churn ≤ 6%** (free, from the five A/A replays) plus an upper 95% bound on
  P(truly FAIL | judge said pass) < 5% from 50–100 verdict-stratified labels.
- Boundary-prioritised label sampling — 1.6× *worse* than random (the inverse-probability weights
  explode on the clean majority). Neyman stratification on the free flip-count signal is the
  correct version and buys 1.40×.

### What may be logged, never controlled

Fixed-K adjudication by replication is genuinely unbiased for *sampling* error — 99.3% accuracy at
2.0 reps, ~$1.20 per blocked PR, no humans, no merges. Its ceiling is honest: it converges on
`d_measured`, not `d_true`, so at a 2pp construct gap its accuracy is 67%. **Construct validity is
the binding constraint and no loop can learn it.** Log adjudications and the escape rate as
monitors. Do not wire them to a parameter.

---

## 6. Expected test results (measured, not projected)

All figures below are literal observed output from this repo against the real promptfoo build.

### Contract suite — the upgrade tripwire

```
$ PROMPTFOO_BIN="node …/dist/src/main.js" PROMPTFOO_PIN=0.123.1 \
  python3 -m unittest -v tests.test_promptfoo_contract

Ran 12 tests in 12.217s
OK
```

Twelve named tests, each asserting one promptfoo behaviour the harness depends on, so a failure
names the exact broken contract: exit codes 0/100/1/42, error-rows-are-errors-not-failures,
zero-tests-exits-0, JSON shape, assert-set flattening, repeat-cache namespacing **and** cross-run
replay (server-side hit counts), python-provider never replays, `__repeatIndex` provider-visible /
assertion-invisible, and the version pin.

### Gate decisions

| Scenario | b, c, n | p (one-sided) | CI (pp) | Decision |
|---|---|---|---|---|
| A clean pass | 2, 1, 300 | 0.875 | (−0.80, +1.46) | PASS |
| B real regression | 4, 17, 300 | 0.003599 | (−7.29, −1.38) | **BLOCK** |
| C significant, immaterial | 0, 6, 1000 | 0.01562 | (−1.08, −0.12) | COMMENT |
| D explained by quarantine | 1, 14, 300 | 0.000488 | (−6.82, −1.85) | COMMENT |
| Small-suite drop | 0, 5, 60 | 0.03125 | (−15.33, −1.34) | **BLOCK** |
| Pure improvement | 17, 4, 300 | 0.999255 | (+1.38, +7.29) | PASS |
| B, below the power floor | 4, 17, 300 | 0.003599 | (−7.29, −1.38) | COMMENT |

The last two are the regression tests for the sidedness bug. Under the direction-blind
implementation they were PASS and COMMENT respectively — i.e. a real regression shipped and an
improvement was flagged.

### Guardrails

```
retry-until-green: 3 re-runs all returned BLOCK (retries=3)
new candidate and new judge snapshot both correctly MISS
  churn=   2% N= 300  power@-5pp=0.962  OK
  churn=   2% N= 145  power@-5pp=0.669  OK
  churn=  20% N= 300  power@-5pp=0.489  BELOW FLOOR -> comment-only
```

### Store and drift

Model drift fires at **flat pass rate** — 100.0% → 100.0%, `served-model-2026-01` →
`served-model-2026-03`, `quality_moved: false`. That is the observation the project exists to make.
The dead-man's switch returns `ALERT_NO_NIGHTLY` before tonight's run and `OK` after.

### End-to-end, against the real binary

`e2e_test.sh` runs the assembled pipeline and asserts 16 outcomes — `0 failure(s)`:

```
  ok   real regression -> BLOCK, exit 20
  ok   re-run of the same commit replayed the stored BLOCK
  ok   new commit re-measured -> PASS
  ok   below the power floor -> COMMENT, with the reason in the comment
  ok   a changed assertion -> REFUSE, exit 30 (no delta invented)
  ok   served model swapped at a FLAT pass rate and the monitor caught it
```

Its first run found a crash no module self-check covered: `read_quarantine`
returned a bare `[]` with no quarantine file while its caller unpacked two values.
That is the argument for the phase, not a footnote to it.

### Workflows

`ALL WORKFLOW ASSERTIONS PASSED`, and `actionlint` reports 0 findings on both files. — both files parse; telemetry disabled at workflow scope;
`PROMPTFOO_PASS_RATE_THRESHOLD` never set; `--fail-on-error` never used; per-run
`PROMPTFOO_CONFIG_DIR`; `cancel-in-progress: false` on drift; 30h dead-man's switch present.

---

## 7. Engineering practices

Each of these exists because something broke without it.

1. **Assemble and run end-to-end before planning.** Five components were each individually correct
   and did not compose: `gate.py` + the real stats module was an `ImportError`, and its scenario
   suite had been passing against its own stub. Integration is a phase, not a formality.
2. **Never read promptfoo's exit code through a pipe.** Measured wrong twice, in two components,
   producing two wrong conclusions. `foo | tail` gives you `tail`'s status.
3. **One parser, imported.** Two modules classified the identical row differently (`PASSED` vs
   `UNSCORED`) because of branch order, making one module's `UNSCORED` state dead code.
4. **Canonicalise field names at the boundary.** `model_id` vs `modelId` made drift detection fail
   **open** — each accessor returned `None` on the other's data, and `None == None` reads as
   "no drift." Assert non-null at ingest.
5. **One unit per column name.** "Expected count" meant rows in one module and cases in another,
   against one schema column; changing `--repeat` silently changes the units mid-history.
6. **Pin three things and record all three on every run:** the model snapshot under test, the judge
   snapshot, the promptfoo version. Any change invalidates the baseline.
7. **Run the contract test on every dependency bump**, and never bump the pin to silence it.
8. **State conventions in code with a test** — CI sidedness (z=1.96 vs 1.645 is a materially
   different gate), one-sided vs two-sided, pp vs fraction.
9. **Recompute operating characteristics at print time.** Four independent analyses disagreed by 2×
   on P(block | −2pp) because of undocumented differences in how a regression displaces churn mass.
   A report that bakes in a number from a superseded model is worse than no report.
10. **`actionlint` in CI.** The workflows had never been schema-checked.
11. **Preflight every config for `sharing:`** before it reaches the CLI.

---

## 8. Build phases (revised)

**All phases 0–7 are built, run end to end against the published binary, and measured.** The suite
is the 300-case Meridian Support Assistant set (`eval/`, [PHASE0.md](eval/PHASE0.md)); five A/A
replays give churn 1.27%, 8 quarantined cases and power@−5pp = 0.968, so the gate blocks rather
than comments. What follows records what each phase turned out to be, including where the plan was
wrong.

**Phase 0 — pick the feature. BUILT** (`eval/policy.md`, `eval/system_prompt.md`,
`eval/provider.py`, `eval/PHASE0.md`). Meridian is a fictional B2B billing product whose every
claim traces to a line of a policy file, chosen so that most assertions can be deterministic string
checks — assertion churn is the entire power budget. Ten failure categories, 300 cases, 20 named
bad inputs.

**Phase 1 — golden set + run store. BUILT** (`parse.py`, `store.py`, `eval/`), *placeholder data.*
Schema carries the `UNSCORED` state and a nullable `repeat_index` fed by provider echo; `store.py`
grew a non-destructive `open_db()` beside the destructive demo `connect()`, because a nightly that
wipes the series it extends reports OK forever. `eval/provider.py` pins the three things a real
provider must keep: echo `__repeatIndex`, echo the **served** model id, keep `case_id` explicit.
*Done:* the 300 real cases replaced the placeholder twelve. The 12-case fixture survives as
`eval/offline/`, which is what `e2e_test.sh` runs with no API key.

**Phase 2 — flaky quarantine + churn. BUILT** (`quarantine.py`). Five A/A self-runs in, churn +
quarantine + power@−5pp out, with the 6% ceiling, the 15% union cap and the 0.60 floor enforced as
exit codes. Power is computed in **cases, not paired samples** — repeats of one case are not
independent draws and counting them as such overstates it.
*Done when:* churn ≤ 6% **and** power@−5pp ≥ 0.6 on a real gating suite.

**Phase 3 — the pairer. BUILT** (`pair.py`, `fetch_baseline.py`, `cases.manifest.json`,
`quarantine.json`). Pairs are keyed `(case_id, prompt_idx, repeat_slot)`. Two rules the draft did
not settle:

- `repeat_slot` is the echoed `__repeatIndex` when *every* row in the group has one, else the
  ordinal position within the group. Repeats are i.i.d. draws, so which head repeat meets which
  baseline repeat carries no information *even when the index is echoed*; what is never safe is
  **inferring** the slot from the row index. A duplicated echoed index is a harness error, not a
  silently dropped row.
- Suite identity rides in `contract_key` (`dataset_sha` and `assertions_sha` kept apart so a
  reviewer sees which half moved). Baselines are stored under the hash of that key, so the lookup
  itself enforces comparability and a drifted contract simply misses → REFUSE.

*Done:* a real two-run pairing gives 36 pairs over 12 cases × 3 repeats, b/c verified by hand.

**Phase 4 — the gate. BUILT** (`gate.py` behind `verdict_cache.py`). Directional exact McNemar +
closed-form CI + the three-part rule; only `PASS`/`COMMENT`/`BLOCK` are cached, because `REFUSE`,
`HARD_FAIL` and `HARNESS_ERROR` describe a fixable state of the world and must re-measure after the
fix. Guardrail B is wired in here rather than left as a quarterly report: below the power floor —
**or last measured over 100 days ago**, since "publish it quarterly" is not enforcement unless
staleness counts as unmeasured — `BLOCK` degrades to `COMMENT` with the reason in the comment.
*Done:* a real regression BLOCKs; re-running the same commit replays the cached BLOCK **even after
the code is secretly fixed**; a new commit re-measures to PASS.

**Phase 5 — the drift cron. BUILT** (`drift_monitor.py`), *awaiting real nights.* EWMA λ=0.2 L=3.0
with MR/d₂ σ, model drift on echoed identity, dead-man at 30h. A harness-errored night is a **hole**
in the series, never a low point: averaging an outage in drags the centre line down and then stops
alarming once the outage is the new normal.
*Done in simulation:* 40 in-control nights raise no alarm at σ̂ = 0.44pp, an injected −5pp break is
caught in 1 night, and a served-model swap fires at 100.0% → 100.0%. *Done for real when:* 40
nights of your own history and a fitted σ from your own data.

**Phase 6 — triage agent. BUILT** (`triage.py`), *not wired.* Reads the judge rationale the store
already keeps and appends a grouped section under the comment the gate rendered. Grouping is the
failing assertion type plus a digit-stripped rationale prefix — no clustering library, no model
call, nothing that can itself be wrong in an interesting way. Cases the store has already seen flip
are split out and explicitly *not* recommended for quarantine, citing the 0.866 → 0.405 power cost.
Read-only holds by construction, not intention: the store is opened `mode=ro`, the comment `"a"`,
and `main()` returns 0 on every path so a reading aid can never emit an exit code the gate owns.
*Not wired on purpose:* nothing on the PR path records the head run into the store
(`store.ingest` runs only from the nightly), and `icontains` reasons are not rationale. Wire it
once Phase 0 lands a real suite with a pinned judge.

**Phase 7 — logged, never controlled. BUILT** (`adjudicate.py`, `escape_monitor.py`). Fixed K=20,
no sequential stopping, all replicates **pooled** into one tally and tested once — best-of-k is the
attack, and pooling one measurement K times is amplification, so K distinct paths *and* K distinct
head eval_ids are required. Every record carries the honest ceiling: the adjudicator converges on
`d_measured`, not `d_true`, so at a 2pp construct gap even a perfect adjudicator is 67% accurate.
`escape_monitor.py` appends incidents and joins them to the verdict cache on `candidate_sha`;
UNKNOWN is its own bucket, never folded into CAUGHT (which flatters the gate) or ESCAPED (which
slanders it), and below 10 ruled-on incidents it prints counts and refuses a percentage.
Neither module writes to `verdict_cache`, edits `quarantine.json`, or touches a threshold.

**Newly learned in the build — four things worth carrying forward.**

1. **Integration found what unit tests could not.** The first end-to-end run crashed on a path no
   self-check covered: `read_quarantine` returned a bare `[]` when the file was absent while its
   caller unpacked two values. Every module's own assertions were green. `e2e_test.sh` now runs the
   whole pipeline against the real binary and asserts 16 outcomes.
2. **One classifier, one validator, one unit.** `runner.classify()` is now the single place a run
   becomes OK/TESTS_FAILED/HARNESS_ERROR, so the nightly recorder cannot disagree with the gate
   about whether an errored row is a failure. `validate_workflows.py` resolves against the repo
   root, not the cwd — it had been silently unrunnable from anywhere but one directory.
   `--expected-tests` counts **rows**; the store's `n_cases_*` columns count **cases**; the
   docstring now says so where the two meet.
3. **A self-check that has never failed is not a test.** Phases 6 and 7 were built, then put
   through three adversarial reviews and two mutation audits: 157 single-line mutations, of which
   36 broke real logic while the self-check still printed OK. Only two were production defects —
   the rest were a test suite that looked thorough and pinned nothing. The recurring shapes:
   no *positive* assertion on rendered output (so emptying it passes); a guard covered only by an
   all-or-nothing revert (so deleting any one conjunct passes); a fixture symmetric in the two
   numbers being printed (so swapping them passes); and a constant with no assertion anywhere
   (so `K = 20 → 1` passes). Write the mutation down, watch the check fail, then fix it.
4. **The drift cron must not use the verdict cache.** On a quiet week `main` does not move, so the
   cache key is identical every night and night one's verdict would replay forever. The cache
   defends a PR against re-rolling; the nightly *must* re-roll. Same function, opposite requirement.

**Cut from the original plan:** the DSPy/GEPA auto-fix phase is unchanged in principle but moves
behind Phase 7 — it depends on the rationale corpus, which depends on the judge being pinned and
the parser keeping `reason` text.

---

## 9. Risks

**The judge is worse than the thing it judges.** Still the most likely project-ending failure, but
the *detector* changed: κ is unmeasurable at the label budget of a one-person project. Use churn
≤6% plus a verdict-stratified miss-rate bound.

**Construct validity.** No amount of replication learns whether the golden set measures what
production cares about. At a 2pp construct gap, even a perfect adjudicator is 67% accurate. Refresh
the golden set periodically; treat the escape-rate monitor as the only external check.

**The suite silently shrinks.** Naive "quarantine on any flip" drains a 400-case suite to 145 in a
year — power@−5pp 0.866 → 0.405 — while churn *improves* to 0.43% and FPR goes to zero. Only the
power floor catches it. Cap the union of exclusion lists at 10–15%.

**promptfoo breaks under you.** Pre-1.0 at 0.123.x, with documented behaviour already contradicting
reality in at least two places. Mitigated by the 12-test contract suite and an exact pin.

**API cost.** Unchanged: build against a cheap model, use the disk cache during development, and
disable it only for runs that must measure variance.

**Scope creep into observability.** Unchanged. Consume Langfuse; do not build one.

---

## 10. Open questions

**Answered by building it:**

- *Can the golden cases be public?* Yes, and they are — the feature under test is fictional, so the
  suite carries no customer data. That is why Meridian exists.
- *One repo or two?* One. The harness and the example suite ship together because the suite is what
  proves the harness; `eval/` is an example, not the product.
- *Is the OSS path wanted?* Yes — MIT, public repo.
- *pass/fail vs 0–1 rubric?* Pass/fail. Graded scores are 2–5× cheaper in cases, but the
  judge-calibration budget dominates that saving. 15 of 300 cases use `llm-rubric` against a
  pinned judge; the rest are deterministic string checks.

**Still open, and now a live risk because the repo is public:** GitHub disables a scheduled
workflow in a public repository after **60 days with no repository activity**. The nightly drift
job would stop silently, and the dead-man's switch would then alarm every 30 hours — correctly,
but about GitHub rather than about the model. Any commit resets the clock; `workflow_dispatch` is
on the workflow as a manual fallback. A repo that goes quiet for two months needs either a keepalive
commit or an external scheduler.
