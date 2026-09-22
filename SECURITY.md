# Security

## Reporting

Open a [private security advisory](https://github.com/tushaarrr/regresgate/security/advisories/new).
Please do not open a public issue for anything exploitable.

## What this project handles

The gate itself is stdlib-only Python that reads JSON and writes SQLite. It has
no network code. The exposure is in what it runs *around*: an eval harness that
holds a provider API key and posts to a pull request.

### `sharing:` in a promptfoo config exfiltrates the whole eval

Measured against promptfoo 0.123.1 with a local counting server: a top-level
`sharing:` key POSTs the entire eval — **every var and every model output** — to
whatever host the config names. It exits 0 and warns about nothing, and nothing
downstream can detect that it happened. No promptfoo document mentions this.

`regressgate/preflight.py` refuses to run a config that sets it. If you vendor
this harness, keep that check. It is the highest-severity finding in the repo.

### Anything you feed the gate is untrusted

`pair.py`, `gate.py` and `triage.py` read a promptfoo export, which contains
model output. Model output is data. Specifically:

- `triage.py` opens its store read-only (`mode=ro`) and only ever appends to its
  log, enforced by an AST guard, so a judge rationale cannot rewrite history.
- `gate.py` renders model-derived text into a pull request comment. Case ids are
  rendered in backticks; rationales are not rendered at all. If you extend the
  comment, remember that a case can make the model emit markdown.
- `adjudicate.py` pools a fixed K and never takes the best of k, because
  best-of-k is the attack rather than the defence.

### Secrets in CI

- `.env`, `.env.*` and `eval/.env.ci` are gitignored. This repo is public and the
  gate needs a live provider key, so that block is load-bearing: one `git add -A`
  without it publishes the key.
- `--env-file` **overrides** an already-exported `PROMPTFOO_CONFIG_DIR`, verified.
  A checked-in env file that sets it makes concurrent CI jobs share one config
  directory and corrupt each other's SQLite. `gate.yml` refuses to run if
  `eval/.env.ci` contains that key.
- The live eval job needs `secrets.OPENAI_API_KEY`. Secrets are **absent on pull
  requests from forks**, so that job fails rather than skips on a pull request: a
  skipped required check counts as passing in branch protection, which would be a
  bypass of the gate.
- Scheduled workflows in a public repository are disabled by GitHub after 60 days
  of repository inactivity. The dead-man's switch will then alarm every 30 hours —
  correctly, but about GitHub rather than about the model.

### What is not defended

- **The verdict cache is not authenticated.** It is keyed on
  `(candidate sha, baseline sha, judge snapshot)` and stored in a CI cache.
  Anyone who can write that cache can pin a verdict. On GitHub, cache writes are
  scoped to the branch and its base, so a pull request cannot poison another's,
  but a compromised default branch can poison everything downstream. If that is
  in your threat model, store verdicts somewhere you control.
- **The baseline store is not signed.** A promoted baseline is a JSON file. The
  contract key stops an *accidental* comparison across a changed suite; it does
  not stop a deliberate one.
- **The judge is a third-party model.** Pinning a snapshot bounds the drift; it
  does not make the judge trustworthy. That is what the churn ceiling and the
  power floor are for, and they are the only guardrails that catch a degrading
  judge at the source.
