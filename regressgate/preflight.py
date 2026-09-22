"""Refuse to run an eval whose config or invocation is unsafe. Stdlib only.

Three MEASURED holes no other component guards:
  1. `sharing:` in the config POSTs the whole eval (vars + outputs) to a host the
     config names. Measured: an arbitrary apiBaseUrl received a gzipped POST
     containing the test vars, with NO cli flag and exit 0.
  2. An -o path ending in .junit.xml is matched by endsWith() and writes a
     DIFFERENT schema [outputFormats.ts:9]; every parser here would then see
     "no parseable output" or worse, a silently empty run.
  3. PROMPTFOO_PASS_RATE_THRESHOLD / PROMPTFOO_FAILED_TEST_EXIT_CODE inherited
     from the CI environment silently rewrite the exit-code contract.

ponytail: text scan, not a YAML parse -- PyYAML is not in this project's deps and
a top-level `sharing:` key is unambiguous at column 0. Comment lines are stripped
so a doc-comment warning about sharing does not trip the check (the gate-ci agent
hit exactly that bug grepping its own workflows).
"""

import argparse
import os
import re
import sys

_SHARING = re.compile(r"^sharing\s*:", re.M)


def check(config_text: str, out_path: str, env=None) -> list[str]:
    env = os.environ if env is None else env
    src = "\n".join(l for l in config_text.splitlines() if not l.lstrip().startswith("#"))
    bad = []
    if _SHARING.search(src):
        bad.append("config sets `sharing:` -- results (vars AND outputs) are POSTed off-box; "
                   "remove it or the run leaks the golden set")
    if out_path.endswith(".junit.xml"):
        bad.append(f"-o {out_path} routes to the JUnit writer, not the OutputFile object")
    for k in ("PROMPTFOO_PASS_RATE_THRESHOLD", "PROMPTFOO_FAILED_TEST_EXIT_CODE"):
        if k in env:
            bad.append(f"{k}={env[k]!r} is set; it silently rewrites the exit-code contract")
    return bad


def _selfcheck():
    assert check("sharing:\n  apiBaseUrl: http://evil\n", "o.json", {})
    assert check("# never set sharing: true\nprompts: [x]\n", "o.json", {}) == []
    assert check("prompts: [x]\n", "o.junit.xml", {})
    assert check("prompts: [x]\n", "o.json", {"PROMPTFOO_PASS_RATE_THRESHOLD": "0"})
    assert check("prompts: [x]\n", "o.json", {}) == []
    print("preflight self-check OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="out.json")
    a = ap.parse_args(argv)
    with open(a.config) as f:
        bad = check(f.read(), a.out)
    for b in bad:
        print(f"::error::preflight: {b}", file=sys.stderr)
    if not bad:
        print(f"preflight OK: {a.config} -> {a.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selfcheck() if "--selfcheck" in sys.argv else main())
