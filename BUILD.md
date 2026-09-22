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

## Cost

~3.5M subagent tokens and ~820 tool calls across four workflows, plus a few
dollars of OpenAI spend for the golden set and its validation. The mutation
audit was roughly a third of that and produced the only findings that changed
what the code does under failure.
