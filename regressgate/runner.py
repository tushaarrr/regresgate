"""Subprocess wrapper + exit-code contract for promptfoo eval.

Exit-code ground truth (promptfoo 0.123.1):
  0   passRate >= threshold           (ALSO: zero tests -- NaN < 100 is false)
  100 passRate <  threshold           where passRate = successes/(successes+failures+errors)
  1   failEvalRun -- 11 call sites, incl. MISSING PROVIDER API KEY and YAML typo
  130 SIGINT
So exit 100 alone cannot separate a 429 storm from a quality drop, and exit 1 alone
cannot separate a rotated CI secret from a bad config. We gate in python and read
stats.errors vs stats.failures to decide.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

OK = "OK"
TESTS_FAILED = "TESTS_FAILED"
HARNESS_ERROR = "HARNESS_ERROR"
INTERRUPTED = "INTERRUPTED"


def classify(stats: dict, expected_rows: int, exit_code: int) -> tuple[str, str]:
    """(outcome, one-line reason) from the JSON, never from the exit code alone.

    NOTE THE UNIT: expected_rows counts ROWS, i.e. cases x repeats. It is not the
    case count. promptfoo stats are per row, so anything compared against them
    must be too -- the run store keeps the case count in its own column.

    This is the single classifier. When the nightly recorder had its own copy,
    the two disagreed about whether an errored row was a failure, which is how a
    provider outage gets filed as a quality regression.
    """
    successes = stats.get("successes", 0)
    failures = stats.get("failures", 0)
    errors = stats.get("errors", 0)
    n = successes + failures + errors

    # The zero-tests hole: a typo'd --filter-pattern evaluates nothing and exits 0.
    if n != expected_rows:
        return HARNESS_ERROR, (f"expected {expected_rows} test results, got {n} "
                               f"(successes={successes} failures={failures} errors={errors})")
    if exit_code == 1:
        return HARNESS_ERROR, ("promptfoo failEvalRun (bad config, missing provider "
                               "API key, unreadable file, ...)")
    if errors and not failures:
        # exit 100 purely from provider errors -- infra, not a quality regression.
        return HARNESS_ERROR, (f"all {errors} non-passing rows are provider errors, "
                               "no assertion failures: infrastructure, not quality")
    if failures or errors:
        return TESTS_FAILED, f"{failures} assertion failure(s), {errors} provider error(s)"
    if exit_code != 0:
        return HARNESS_ERROR, f"clean stats but exit {exit_code}"
    return OK, f"{successes}/{n} passed"


@dataclass
class Result:
    outcome: str
    exit_code: int
    detail: str
    stats: dict[str, Any]
    out_path: str
    row_count: int
    warnings: list[str]
    stderr_tail: str


def build_argv(node: str, entry: str, cfg: str, out: str, repeat: int | None,
               env_file: str | None) -> list[str]:
    argv = [node, entry, "eval", "-c", cfg, "-o", out,
            "--no-progress-bar", "--no-table", "--no-write", "--no-cache"]
    if repeat:
        # --no-cache is REQUIRED with --repeat: within a run the cache is namespaced
        # per repeat index, but ACROSS runs an identical re-run replays every value,
        # and bumping N replays STALE values for repeats 0..N_old-1.
        argv += ["--repeat", str(repeat)]
    if env_file:
        argv += ["--env-file", env_file]
    return argv


def run(cfg: str, out: str, config_dir: str, expected_tests: int,
        repeat: int | None = None, env_file: str | None = None,
        node: str = os.environ.get("REGRESSGATE_NODE", "node"),
        entry: str = os.environ.get("REGRESSGATE_PROMPTFOO", ""),
        timeout_s: float = 900.0) -> Result:
    warnings: list[str] = []
    if env_file:
        # --env-file is loaded AFTER the ambient env and OVERRIDES an already-exported
        # PROMPTFOO_CONFIG_DIR -> concurrent CI jobs silently share one config dir.
        warnings.append(
            f"--env-file {env_file} can override PROMPTFOO_CONFIG_DIR={config_dir}; "
            "audit the env file or concurrent runs will collide in one dir"
        )

    env = dict(os.environ)
    env["PROMPTFOO_CONFIG_DIR"] = config_dir
    env["PROMPTFOO_DISABLE_TELEMETRY"] = "1"
    # Gating is ours. A typo'd threshold silently becomes 100; a 0 silently disables it.
    env.pop("PROMPTFOO_PASS_RATE_THRESHOLD", None)
    env.pop("PROMPTFOO_FAILED_TEST_EXIT_CODE", None)
    os.makedirs(config_dir, exist_ok=True)

    argv = build_argv(node, entry, cfg, out, repeat, env_file)
    # MEASURED: promptfoo does NOT always exit on a bad config. `providers:
    # [file://does_not_exist.py]` hung indefinitely instead of exiting 1. A CI gate
    # with no wall clock is a gate that can never fail. Always time it out.
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        return Result(HARNESS_ERROR, -1, f"timed out after {timeout_s}s (promptfoo can hang "
                      "on an unresolvable provider instead of exiting)", {}, out, 0, warnings,
                      (e.stderr or b"").decode(errors="replace")[-500:] if isinstance(e.stderr, bytes)
                      else (e.stderr or "")[-500:])
    code = p.returncode
    tail = "\n".join(p.stderr.strip().splitlines()[-5:])

    if code == 130:
        return Result(INTERRUPTED, code, "SIGINT", {}, out, 0, warnings, tail)
    # MEASURED: SIGINT *during* an eval does NOT give 130. promptfoo caught it, still
    # wrote a full output file, recorded the in-flight row as an ERROR row
    # ("Error: Worker shutting down", failureReason=2) and exited 100. An interrupted
    # run is therefore indistinguishable from a provider-error run by exit code alone.
    # The expected-count check and the errors-without-failures rule below are what stop
    # it being reported as a quality regression.

    try:
        with open(out) as f:
            doc = json.load(f)
        rows = doc["results"]["results"]
        stats = doc["results"].get("stats", {})
    except (OSError, json.JSONDecodeError, KeyError) as e:
        return Result(HARNESS_ERROR, code, f"no parseable output: {e!r}", {}, out, 0, warnings, tail)

    outcome, detail = classify(stats, expected_tests, code)
    return Result(outcome, code, detail, stats, out, len(rows), warnings, tail)


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--config", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--config-dir", required=True)
    a.add_argument("--expected-tests", type=int, required=True)
    a.add_argument("--repeat", type=int)
    a.add_argument("--env-file")
    a.add_argument("--node", default=os.environ.get("REGRESSGATE_NODE", "node"))
    a.add_argument("--promptfoo", default=os.environ.get("REGRESSGATE_PROMPTFOO", ""))
    a.add_argument("--timeout", type=float, default=900.0)
    ns = a.parse_args()
    r = run(ns.config, ns.out, ns.config_dir, ns.expected_tests, ns.repeat,
            ns.env_file, ns.node, ns.promptfoo, ns.timeout)
    for w in r.warnings:
        print(f"WARN: {w}", file=sys.stderr)
    print(json.dumps({"outcome": r.outcome, "exit_code": r.exit_code, "detail": r.detail,
                      "row_count": r.row_count, "stats": r.stats}, indent=2))
    return {OK: 0, TESTS_FAILED: 100, HARNESS_ERROR: 1, INTERRUPTED: 130}[r.outcome]


if __name__ == "__main__":
    sys.exit(main())
