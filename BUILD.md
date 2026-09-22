# How this was built

This repo was built with Claude Code, much of it through multi-agent
workflows. The numbers below are what those runs actually produced, including
the parts that went badly. They are recorded because the failure data is the
useful half and almost nobody publishes it.

## The shape that worked

Not "spawn agents and merge the output". Every fan-out had the same three
beats, and the third is the one that mattered:

```
author  →  adversarial review  →  mutation audit
```

- **author** — one agent per unit of work, each owning exactly one file so
  concurrent agents could not collide.
- **adversarial review** — N independent reviewers per unit, each given a
  *different lens* rather than the same instructions N times. For the phase 6/7
  modules the lenses were `control-path` ("does this create any route from
  output back to a decision parameter?"), `correctness`, and `integration`
  ("verify by executing, not by reading"). Redundancy finds the same things
  three times; diversity finds three different things.
- **mutation audit** — an agent whose only job was to break the code one line
  at a time and check whether the self-check noticed. This is the step that
  earned its cost.

## What that produced

| Run | Agents | Subagent tokens | Outcome |
|---|---|---|---|
| Build phases 6 & 7 | 12 | 1.06M | 4 critical, 16 major findings |
| Harden against them | 6 | 0.60M | 157 mutations tried, **36 survived** |
| Close the survivors | 6 | 0.58M | 49 mutations replayed, all now caught |
| Author the golden set | 15 | 1.24M | 10 of 15 killed by a session limit |

**All four critical findings were the same defect in different costumes: the
self-check passed on broken code.** Not one was a logic error a reviewer would
catch by reading. Examples the auditors demonstrated rather than asserted:

- an AST guard enforcing "this file only ever appends" was blind to
  `open(path, mode="w")` passed as a *keyword* — a truncating write that
  destroys the whole log sailed through it;
- a self-check still printed `OK` with either half of `real = sig and material`
  deleted, because the only case it ever judged cleared both bars hard;
- nothing stopped the same replicate file being pooled K times, which is
  amplification — precisely the attack that module exists to prevent;
- mutating a join from `candidate_sha` to `baseline_sha` still passed, which
  would have filed a real escape as a catch.

## The number worth arguing about

**36 mutations broke real logic while the tests stayed green. Only 2 were
production bugs.**

So ~94% of what the adversarial pass produced was not "your code is wrong" but
"your tests do not pin your code". That is a less exciting finding and a more
useful one. The recurring shapes, which are worth checking in any test suite:

- no *positive* assertion on rendered output, so emptying it passes;
- a guard covered only by an all-or-nothing revert, so deleting any single
  conjunct passes;
- a fixture symmetric in the two numbers being printed, so swapping them passes;
- a constant asserted nowhere, so `K = 20 → K = 1` passes.

## What went wrong

- **An agent overwrote `regressgate/quarantine.json`** with a stub, destroying
  a measurement that costs real API spend to reproduce. It was caught only
  because `git status` was checked before committing. Every later fan-out got
  an explicit "do not touch any file but your own", and the verifiers were told
  to report `git status` verbatim. Unsupervised agents in a shared working tree
  will eventually do this.
- **A session limit killed 10 of 15 agents mid-run.** Three had already written
  their files and died before returning a result, so the workflow's own summary
  said five categories were missing when eight were on disk. **The orchestrator
  report is not ground truth; the filesystem is.** The remaining two files were
  written by hand rather than by retrying.
- **Reviewers over-report when told to be adversarial.** Several "findings"
  were style opinions or risks that could not be demonstrated. Requiring a
  concrete failing input, and allowing an explicit `rejected` list with
  reasoning, cut the noise — and one agent correctly rejected a suggested fix
  as a redesign rather than accepting it.

## What the agents did not find

The two most consequential bugs in the project were found by **running the real
artifact**, not by review:

1. `.nvmrc` pinned Node 20. promptfoo 0.123.1 requires `>= 22.22.0`, and the
   **published** package enforces it at startup while the **git checkout does
   not**. Below it `promptfoo --version` prints nothing, so a version assert
   compared against an empty string and failed naming the wrong cause.
2. `pair.py` hashed `config.tests` to build its contract key. The published
   package leaves `tests: [file://…]` as raw strings; the git checkout resolves
   them. So the hash was computed over an **empty list** — every suite produced
   the same contract key, silently. It surfaced only because the first manifest
   generated from a real run said "0 cases" next to 300 rows.

Same lesson twice: the local checkout is not the artifact CI installs, and no
amount of review substitutes for running the thing you actually ship.

## The second audit, and what it cost to be wrong

A later pass pointed eight agents at the finished repo with one instruction:
find claims that are false. Five lenses on the golden set, one on the
statistics, one on the CI, one on every factual claim about promptfoo. Each had
to demonstrate a finding by running something, and could return an explicit
`rejected` list. They produced 133 findings and rejected 35 of their own.

Two of those findings were in the load-bearing path and neither was visible by
reading:

- **The gate counted `--repeat` samples as independent observations.** In
  production (`--repeat 3`) a deterministically broken case contributed three.
  Six broken cases read COMMENT at `--repeat 1` and **BLOCK** at `--repeat 3`;
  one broken case at `--repeat 10` reached p = 0.00098 on its own. The repeat
  count is a cost knob and it was moving verdicts. Worse, `quarantine.py`
  already measured power in *cases*, so the floor was guarding a different
  experiment from the one that ran.
- **The flagship number was not reproducible.** "Re-running CI ships a −5pp
  regression 79% of the time" appeared in six files, sourced to a simulation
  nobody committed. An independent re-derivation got 0.205 per run where the
  docstring said 0.268. Rebuilt as `retry_sim.py`, which scores every simulated
  run with the gate's own function: at the parameters the old table claimed, the
  real figure is **67%**, and at this suite's measured churn, **60%**.

The second one is the more embarrassing and the more general. The number was
not a guess — someone ran something. But the something was not committed, so a
year later nobody could tell a transcription error from a modelling difference.
**A number quoted in prose needs a script behind it, in the repo, or it is
folklore.**

### The suite could not see the regression it was built for

A pull request carrying a deliberately weakened system prompt — rule 1 changed
from "say you do not have that in the policy" to "answer from the closest policy
that does" — was merged after the gate measured −0.22pp, p = 0.44, and said
"merge away". The gate was right. The *instrument* was not pointed at the
change: the not-in-policy cases assert on the phrase "I don't have that in the
policy", and the regressed prompt still emitted that phrase before going on to
answer anyway. 26 of 30 passed, against 27 on the baseline.

That produced `eval/lint_cases.py`, which asks a question no eval tool asks:
**can this case tell a right answer from a wrong one?** It builds adversarial
probes from each case's own assertions — a bare topic word that survives
negation, the question echoed back as the answer, a shifted number, an empty
string — and runs promptfoo's exact matching semantics over them. It found that
**58 of 300 cases accepted at least one wrong answer**, including six that could
not distinguish their own named failure. `icontains: accepted` is satisfied by
"not accepted". `icontains: documentation` is satisfied by "no documentation is
needed". `regex: 19(?!\d)` is satisfied by "$19.99" and "$119".

Six agents, one per case file, fixed all 58 with the lint as the oracle and a
hard rule that two *correct* paraphrases must still pass — an over-tightened
assertion burns the case and looks like a regression, which is worse than the
tautology. All 300 cases now survive their own probes.

## What the tools were used for

| tool | where it earned its cost |
|---|---|
| mutation audit | the only step that found tests not pinning their code; 36 survivors, 2 real bugs |
| diverse-lens review | three different lenses found three different things; three identical reviewers found one thing three times |
| adversarial refutation | killed 35 findings the finders believed; the refuters had to reproduce, not opine |
| running the published artifact | found the two worst bugs, both invisible to every reviewer |

The last row is the whole lesson. Review is a filter on what someone already
suspects. The node version and the contract-key collapse were found by typing
`npm install -g promptfoo` and watching what came out.

## Cost

~3.5M subagent tokens and ~820 tool calls across four workflows, plus a few
dollars of OpenAI spend for the golden set and its validation. The mutation
audit was roughly a third of that and produced the only findings that changed
what the code does under failure.
