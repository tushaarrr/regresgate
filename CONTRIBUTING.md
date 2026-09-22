# Contributing

## The one rule

**Every change needs a check that fails when the change is reverted.** Not a
test that passes — a test that *notices*. An adversarial audit of this repo
found 36 mutations that broke real logic while every test stayed green; only 2
were production bugs. The other 34 were tests that did not pin their code.

So before you open a pull request, mutate your own change and watch the check
fail:

```bash
cp regressgate/gate.py /tmp/gate.orig
# break the thing you just fixed, one line, by hand
python3 regressgate/scenarios.py          # must now report MISMATCHES > 0
cp /tmp/gate.orig regressgate/gate.py
```

If it stays green, the check is decoration. The recurring shapes are: no
positive assertion on rendered output (so emptying it passes), a guard covered
only by an all-or-nothing revert (so deleting one conjunct passes), a fixture
symmetric in the two values being printed (so swapping them passes), and a
constant asserted nowhere (so `K = 20 → K = 1` passes).

## Setup

```bash
python3 regressgate/doctor.py      # says exactly what is missing and how to fix it
```

Python 3.10+ and no dependencies for the harness itself. `pyyaml` is needed only
by `regressgate/validate_workflows.py` and `eval/lint_cases.py`, which are dev
tools. Node and promptfoo are needed only to run a real eval; everything else
self-checks without them.

## Before you push

```bash
cd regressgate
for m in parse preflight pair fetch_baseline quarantine drift_monitor \
         triage adjudicate escape_monitor retry_sim; do python3 $m.py --selfcheck; done
python3 scenarios.py                      # the ten gate verdicts + repeat invariance
PYTHONPATH=. python3 stats/adapter.py
python3 verdict_cache.py
bash e2e_test.sh                          # the whole pipeline, no API key
bash contract_test.sh                     # promptfoo's observable behaviour
cd ../eval && python3 lint_cases.py --strict
```

CI runs all of it. The live eval job is skipped without `OPENAI_API_KEY` on a
push and **fails** on a pull request, because a skipped required check counts as
passing in branch protection and that would be a fork-PR bypass of the gate.

## Test against the published artifact, never your checkout

The two worst bugs in this project's history were both the same mistake:

- `.nvmrc` pinned node 20. The **published** promptfoo enforces
  `engines.node >= 22.22.0` at startup and the **git checkout does not**, so a
  local run was green while CI was red, and `promptfoo --version` printed
  nothing at all.
- `pair.py` hashed `config.tests`. The published package leaves
  `tests: [file://…]` as raw strings; the checkout resolves them. The hash was
  computed over an empty list, so every suite produced the same contract key,
  silently.

Run the contract tests against what npm installs:

```bash
npm install -g "promptfoo@$(cat regressgate/promptfoo.version)"
python3 -m unittest -v tests.test_promptfoo_contract
```

## Changing a promptfoo assumption

`tests/test_promptfoo_contract.py` has one named test per behaviour the harness
depends on, and `regressgate/contract_test.sh` has the shell equivalents. If you
bump the pin in `regressgate/promptfoo.version`, run both. A failure names the
broken assumption; it is not something to work around.

## Changing the golden set

Any change to a case, its assertions, the judge or the promptfoo pin **changes
the contract key**, which invalidates every promoted baseline and the
calibration in `quarantine.json`. That is deliberate — comparing across a
changed suite is not a comparison — but it means:

1. `cd eval && python3 lint_cases.py --strict` must be clean. It builds
   adversarial probes from each case's own assertions and tells you whether the
   case can distinguish a right answer from a wrong one.
2. Re-run five A/A replays and regenerate the calibration:
   ```bash
   for i in 1 2 3 4 5; do
     promptfoo eval -c eval/promptfooconfig.yaml --no-cache -o /tmp/aa$i.json
   done
   python3 regressgate/quarantine.py --runs /tmp/aa*.json --out regressgate/quarantine.json
   ```
   Until that lands, `pair.py` treats the old calibration as unmeasured and the
   gate degrades to comment-only.

Never tighten an assertion without checking that two *correct* paraphrases still
pass. An over-tightened case is worse than a tautological one: it burns the case
and it looks like a regression.

## Numbers in prose

Any number quoted in a document needs a committed script behind it. `79%` lived
in six files for a week, sourced to a simulation that was never committed; when
someone finally re-derived it, the real figure was 60%. `retry_sim.py` exists so
that cannot happen again. If you cannot regenerate it, do not write it.

## Style

Standard library only in `regressgate/`. No ORM, no test framework, no
dependency added for what a few lines do. Comments explain *why*, and
specifically why the obvious thing is wrong — most of the comments in this repo
are load-bearing measurements, not description.
