# regressgate

A statistical regression gate for LLM evals, built on top of
[promptfoo](https://github.com/promptfoo/promptfoo).

> **It tells you whether the drop is real, and it catches the drop that no
> commit caused.**

Four things this repo found along the way, each measured rather than assumed:

- **Re-running CI ships a −5pp regression 58% of the time after five
  attempts**, and a −3pp one 99%. A gate is a random variable and re-rolling it
  is free, so the verdict is cached per `(commit, baseline, judge)` rather than
  recomputed. Regenerate the table with `python3 regressgate/retry_sim.py`.
- **`sharing:` in a promptfoo config POSTs your entire eval off-box** — vars
  and outputs — exits 0, and warns about nothing. No promptfoo document
  mentions it. `preflight.py` refuses to run a config that sets it.
- **`--fail-on-error` appears in promptfoo's own docs in 8 places across 6
  files and does not exist.** Twelve contract tests pin the behaviours this
  harness actually depends on, so an upgrade names the broken assumption.
- **A self-calibrating version of this gate was designed, simulated four ways,
  and deleted.** It converges to the human override rate, not to anything
  true, and its "was this a false alarm?" button is an attack surface: clicking
  it on every override drives power against the design effect size to 0.049.
  [PLAN.md](PLAN.md) §5 has the argument and the numbers.

[BUILD.md](BUILD.md) covers how it was built, including the multi-agent review
that found 36 mutations which broke real logic while the tests stayed green —
of which only 2 were production bugs.

---

promptfoo runs your eval and tells you the pass rate. Everything after that is
unbuilt, and that gap is this project. A pass rate that moved from 94% to 91%
is not information: on a 300-case suite that is well inside the noise a fixed
golden set produces from one night to the next. regressgate decides whether the
move is real, refuses to answer when it cannot know, and watches for the
regression that arrives with no pull request attached to it.

## Why not just promptfoo

Verified against promptfoo `0.123.1` by reading and running it, not by reading
its docs:

| Capability | promptfoo |
|---|---|
| Run-over-run baseline comparison | No `compare` subcommand, no CLI diff. Comparison exists only in the web UI and local API (`comparisonEvalIds`), is presentational, and is dataset-locked |
| Delta gating | Absent. `PROMPTFOO_PASS_RATE_THRESHOLD` is an absolute percent with a strict `<` |
| Scheduled runs | Absent. `eval --watch` is *file*-triggered, not time-triggered |
| Alerting | Absent. `slack`/`webhook` are target providers, not notification sinks |
| Statistical significance | Absent. No p-value, no CI, no test. `prompt.metrics.score` is a **sum**, not a mean |
| Model-version drift | Absent |

## How it decides

Every case runs on both the baseline and the candidate, so every case is a
**paired** observation and only the disagreements carry information:

```
b = fixed   (failed on baseline, passes now)
c = broken  (passed on baseline, fails now)
```

The gate **blocks only if all three hold**:

1. one-sided exact McNemar **p < 0.05** — and *directional*, so a pure
   improvement cannot score the same as a regression;
2. the 95% CI upper bound is **below −1pp**, so a statistically real but
   trivial move does not stop a merge;
3. the drop is not accounted for by quarantined, known-flaky cases.

Condition 1 alone produces a comment, never a block. Exit codes are
deliberately disjoint from promptfoo's `0/1/100/130`:

| code | meaning |
|---|---|
| 0 | PASS or COMMENT — merge allowed |
| 20 | BLOCK — statistically real regression |
| 30 | REFUSE — no comparable baseline; **no verdict was computed** |
| 40 | HARD_FAIL — case count mismatch; the suite itself is wrong |
| 50 | HARNESS_ERROR — infrastructure, **not** a quality verdict |

The last two matter more than they look. promptfoo exits `0` on an empty suite,
so a typo'd filter is a silently green build — hence 40. And it folds provider
errors into `passRate` and exits `100` exactly like assertion failures, so a
429 storm is indistinguishable from a quality collapse by exit code alone —
hence 50, decided by reading `stats.errors` rather than the exit status.

## The guardrails, and why there is no learning loop

An earlier design had the thresholds recalibrate themselves from outcomes. It
was simulated four ways and lost every time: an FPR-targeting controller
saturates at maximum looseness even when fed perfect labels, the gate censors
its own training data, and the whole apparatus is dominated by a zero-label
policy that is correct on day one. Worse, an override-derived "was this a false
alarm?" signal is an attack surface — clicking it on every override drives the
threshold to where power against the design effect size is 0.049.

So there are four fixed guardrails instead:

- **Verdict caching.** The gate is a random variable, and re-running CI
  re-rolls it. Measured at this suite's own churn: without this, five re-runs
  ship a −5pp regression **58%** of the time and a −3pp one 95% after three.
  Most of that leak is the materiality bar, not the significance test. The verdict
  is a pure function of `(candidate sha, baseline sha, judge snapshot)` and is
  cached, so a re-run is a lookup. Genuine re-measurement must *pool*
  repetitions — best-of-k **is** the attack.
- **An enforced power floor.** `power@−5pp ≥ 0.60`, or the gate degrades to
  comment-only. This is the only metric that goes red in either measured
  composed failure: under naive quarantine it falls 0.866 → 0.405 while churn
  "improves", and under a degrading judge it falls 0.878 → 0.192 while the
  reported false-alarm rate stays inside budget every quarter.
- **A hard churn ceiling** of 6%, which stops the gate rather than informing
  it. It is the only guardrail that catches a degrading judge at the source.
- **One capped exclusion list.** The 15% cap covers the *union* of the
  quarantine set and the flaky watchlist. Uncapped, "quarantine on any flip"
  drains a 400-case suite to 145 in a year while every other number improves.

## Quick start

```bash
python3 regressgate/doctor.py     # what is missing, and the command that fixes it

npm install -g "promptfoo@$(cat regressgate/promptfoo.version)"   # node >= 22.22.0
export OPENAI_API_KEY=...

# 0. once per golden set: five A/A replays -> churn, exclusions, power
for i in 1 2 3 4 5; do
  promptfoo eval -c eval/promptfooconfig.yaml --no-cache -o /tmp/aa$i.json
done
python3 regressgate/quarantine.py --runs /tmp/aa*.json --out regressgate/quarantine.json

# 1. run the suite
promptfoo eval -c eval/promptfooconfig.yaml --no-cache --repeat 3 -o /tmp/head.json

# 2. find a comparable baseline, pair, and gate. Both shas are the verdict
#    cache key: leave them out and every run is a fresh roll of the dice.
python3 regressgate/fetch_baseline.py --head /tmp/head.json --out /tmp/base.json
python3 regressgate/pair.py --head /tmp/head.json --baseline /tmp/base.json \
    --manifest regressgate/cases.manifest.json \
    --quarantine regressgate/quarantine.json \
    --head-sha "$(git rev-parse HEAD)" --baseline-sha "$BASELINE_SHA" \
    --out /tmp/pairing.json          # fetch_baseline.py --list shows the baseline's sha
python3 regressgate/gate.py /tmp/pairing.json --cache-db /tmp/verdicts.db
```

### Or as a GitHub Action

You run your eval; this decides what the result means.

```yaml
- run: promptfoo eval -c eval/promptfooconfig.yaml --no-cache --repeat 3 -o run.json
- uses: tushaarrr/regresgate@main
  with:
    export: run.json
    quarantine: regressgate/quarantine.json      # or the gate only comments
    promote: ${{ github.ref == 'refs/heads/main' }}
```

Cache `.regressgate/` between runs, or the verdict cache cannot defeat a re-run.
[action.yml](action.yml) documents every input.

The whole harness self-checks with no API key and no test framework:

```bash
cd regressgate && bash e2e_test.sh
for m in parse preflight pair fetch_baseline quarantine drift_monitor \
         triage adjudicate escape_monitor; do python3 $m.py --selfcheck; done
```

## What is in here

| | |
|---|---|
| `eval/` | the feature under test: policy, system prompt, provider, and 300 cases in ten failure categories. See [eval/PHASE0.md](eval/PHASE0.md) |
| `regressgate/parse.py` | one normalizer for promptfoo's export. `UNSCORED` is a distinct state from `FAILED` |
| `regressgate/pair.py` | per-sample pairs, and the contract key that decides what is comparable |
| `regressgate/gate.py` | the three-part rule, the PR comment, the exit codes |
| `regressgate/verdict_cache.py` | the retry-until-green defence and the power floor |
| `regressgate/quarantine.py` | A/A replays → churn, exclusions, power |
| `regressgate/drift_monitor.py` | nightly EWMA, model-identity drift, dead-man's switch |
| `regressgate/store.py` | SQLite run store and the decision log |
| `regressgate/triage.py` | groups the broken cases by judge rationale. Reads only |
| `regressgate/adjudicate.py` | fixed-K **pooled** re-measurement. Logged, never controlled |
| `regressgate/escape_monitor.py` | production incidents vs what the gate said. The only external check |
| `regressgate/doctor.py` | prerequisites, with the exact command that fixes each one |
| `regressgate/retry_sim.py` | regenerates the retry-until-green table; the only source for those numbers |
| `eval/lint_cases.py` | asks whether a golden case can tell a right answer from a wrong one |
| `action.yml` | the composite Action: bring your own eval export |
| `tests/`, `regressgate/contract_test.sh` | twelve named contract tests and seven shell checks, one per promptfoo behaviour the harness depends on; both run in CI against the published binary, never a checkout |

[PLAN.md](PLAN.md) is the design document, including what was cut and why.
[CONTRIBUTING.md](CONTRIBUTING.md) has the one rule. [SECURITY.md](SECURITY.md)
says what is not defended.

## Using it with something other than promptfoo

Everything above the parser is runner-agnostic arithmetic. `parse.py` is the
only file that knows what a promptfoo export looks like; it turns one into a
list of rows, and nothing downstream ever sees the original JSON:

| field | why it is there |
|---|---|
| `case_id` | the pair key. Must be stable across runs and explicit in your data, never an index |
| `prompt_idx`, `repeat_index` | the other two thirds of the sample identity. `repeat_index` must come from the *provider*, echoed back, because promptfoo strips it before assertions run |
| `state` | `PASSED` / `FAILED` / `ERROR` / **`UNSCORED`**. The fourth is the one people leave out, and it is why a 429 storm does not read as a quality collapse |
| `model_id` | the **served** model snapshot, echoed by the provider. Without it, a silent model swap at a flat pass rate is invisible |
| `assertions[].reason` | the judge's rationale, which is what `triage.py` groups on |

A second runner means writing a `parse.py` for it, plus a `contract_key` that
identifies its suite. That is the whole boundary. There is one adapter today and
the schema is not frozen — it has changed twice while being used, so treating it
as a published standard would be premature.

## An eval-reliability failure corpus

Reproducing an eval failure is most of the work of fixing it. These are
checked in and runnable, no API key:

| | |
|---|---|
| `regressgate/scenarios/*.json` | ten pairing documents, one per gate verdict, plus repeat-invariance and quarantine-explains-it |
| `regressgate/fixture/` | a config that sets `sharing:`, one with a YAML typo, one that errors on every row, one that hangs for SIGINT, and an env file that hijacks `PROMPTFOO_CONFIG_DIR` |
| `eval/offline/` | a deterministic 12-case provider that breaks on command and swaps its served model id at a flat pass rate |
| `tests/`, `contract_test.sh` | nineteen pinned promptfoo behaviours, each of which silently breaks something if you do not know it |

`bash regressgate/e2e_test.sh` runs the pipeline against all of it.

## State

Built, green in CI, and measured. Five A/A replays of the 300-case suite with
`gpt-4o-mini-2024-07-18` under test and the judge pinned to `gpt-4o-2024-11-20`:

| | measured | guardrail | |
|---|---|---|---|
| pass rate | 93.0% – 94.0% across replays | | |
| churn | 1.27% | ≤ 6% | ok |
| quarantined | 8 cases | ≤ 45 (15% of the suite) | ok |
| power@−5pp | 0.968 | ≥ 0.60 | ok |

292 cases gate. The eight unstable ones are in
[regressgate/quarantine.json](regressgate/quarantine.json) together with the
contract key they were measured on; a changed golden set, judge or pin makes
that file inert until the replays are re-run, and churn over the ceiling makes
the gate comment-only. Both are enforced in `pair.py`, not just documented.

## promptfoo behaviours this encodes

Each of these was measured, and each one silently breaks something if you do
not know it:

- **`sharing:` in a config POSTs your entire eval off-box** — vars and outputs
  — exits 0, and warns about nothing. `preflight.py` refuses to run a config
  that sets it. This is the highest-severity finding here and it is in no doc.
- **`--fail-on-error` does not exist**, though promptfoo's own docs mention it
  in eight places across six files.
- **`--repeat` + cache**: within a run repeats are cache-namespaced, but across
  runs an identical re-run replays everything, and raising N mixes stale and
  fresh samples into one distribution. Always `--no-cache`.
- **`__repeatIndex` is invisible to assertions** by design, so the provider has
  to echo it or repeats cannot be told apart across runs.
- **`testIdx` is a row index** and `testIdx % N` breaks silently on per-test
  `options.repeat` and on string-array var expansion.
- **`componentResults` under `assert-set` is flattened**, children duplicated,
  and the wrapper has no `assertion` key — the naive access raises `TypeError`.
- **`--env-file` overrides an exported `PROMPTFOO_CONFIG_DIR`**, so concurrent
  CI jobs can collide in one directory.
- **The published package enforces `engines.node >= 22.22.0`; the git checkout
  does not.** Below it, `promptfoo --version` prints nothing at all, so a
  version assert compares against an empty string.
- **The published package leaves `tests: [file://…]` unresolved in the export;
  the git checkout resolves it.** Hashing `config.tests` therefore hashed an
  empty list, and every suite got the same contract key. `pair.py` reads
  `row.testCase` instead.

The last two are the same lesson twice: your local promptfoo checkout is not
the artifact CI installs, and local green proves nothing about CI.

One more, about CI itself rather than promptfoo: **`cancel-in-progress: true`
starves baseline promotion.** The push-to-main run is what promotes the next
baseline, the eval takes minutes, and any push inside that window kills it.
Four consecutive main runs were cancelled that way and not one promoted, after
which every pull request REFUSEs forever — silently, correctly, and for a
reason nothing reports. It is conditional on `pull_request` now, and
`validate_workflows.py` fails if anyone sets it back.
