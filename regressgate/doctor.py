#!/usr/bin/env python3
"""Check that this machine can actually run the gate, and say exactly what to
fix when it cannot.

Written after a reviewer tried the quick start and got `exit 127` fifteen times
in a row, because promptfoo was absent and the active node was 20.20.2 while
.nvmrc asks for 22.22.0. Neither fact was in the output. Every check here
prints the command that fixes it.

    python3 doctor.py            # report, exit 1 if anything is a hard failure
    python3 doctor.py --quiet    # only the failures
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def _pin():
    with open(os.path.join(HERE, "promptfoo.version")) as f:
        return f.read().strip()


def _nvmrc():
    p = os.path.join(ROOT, ".nvmrc")
    with open(p) as f:
        return f.read().strip()


def check_python():
    v = sys.version_info
    if v < (3, 10):
        return FAIL, f"python {v.major}.{v.minor}", \
            "the harness uses match-free 3.10+ syntax (X | Y type unions); install python 3.10 or newer"
    return OK, f"python {v.major}.{v.minor}.{v.micro}", ""


def check_node():
    want = _nvmrc()
    exe = shutil.which("node")
    if not exe:
        return FAIL, "node not on PATH", \
            f"install node {want}: `nvm install {want} && nvm use {want}`"
    code, out, _ = _run(["node", "--version"])
    got = out.lstrip("v")
    if code or not got:
        return FAIL, f"node at {exe} did not report a version", f"reinstall node {want}"
    if tuple(int(x) for x in got.split(".")[:3]) < tuple(int(x) for x in want.split(".")[:3]):
        return FAIL, f"node {got}, need >= {want}", \
            (f"promptfoo {_pin()} declares engines.node >= {want} and the PUBLISHED bin "
             f"enforces it at startup: below it `promptfoo --version` prints NOTHING, so a "
             f"version check compares against an empty string. `nvm install {want} && nvm use {want}`")
    return OK, f"node {got} (>= {want})", ""


def check_promptfoo():
    want = _pin()
    exe = os.environ.get("PROMPTFOO_BIN") or shutil.which("promptfoo")
    if not exe:
        return FAIL, "promptfoo not on PATH", \
            f"npm install -g promptfoo@{want}   (or set PROMPTFOO_BIN=/path/to/promptfoo)"
    cmd = exe.split() if " " in exe else [exe]
    code, out, err = _run(cmd + ["--version"])
    got = out.split("\n")[0].strip()
    if not got:
        return FAIL, f"`{exe} --version` printed nothing", \
            (f"almost always the node version: the published promptfoo exits before printing "
             f"when node < {_nvmrc()}. stderr said: {err[:160] or '(nothing)'}")
    if got != want:
        return WARN, f"promptfoo {got}, pinned {want}", \
            (f"the contract tests were verified against {want} only. "
             f"npm install -g promptfoo@{want}, or re-run "
             f"`bash regressgate/contract_test.sh` and update the pin if it is clean")
    return OK, f"promptfoo {got} at {exe}", ""


def check_key():
    if os.environ.get("OPENAI_API_KEY"):
        return OK, "OPENAI_API_KEY is set", ""
    return WARN, "OPENAI_API_KEY is not set", \
        ("only the live eval needs it. The whole harness self-checks without one: "
         "`cd regressgate && bash e2e_test.sh`")


def check_yaml():
    try:
        import yaml  # noqa: F401
        return OK, "pyyaml present", ""
    except ImportError:
        return WARN, "pyyaml absent", \
            ("only validate_workflows.py and eval/lint_cases.py need it; the gate itself "
             "is stdlib-only. `python3 -m pip install pyyaml`")


def check_quarantine():
    p = os.path.join(HERE, "quarantine.json")
    if not os.path.exists(p):
        return WARN, "no quarantine.json", \
            ("churn and power are unmeasured, so every BLOCK degrades to a comment. "
             "Run five A/A replays and `python3 quarantine.py --runs ... --out quarantine.json`")
    with open(p) as f:
        q = json.load(f)
    bits = (f"churn {100 * q.get('churn', 0):.2f}%, "
            f"power@-5pp {q.get('power_at_5pp', 0):.3f}, "
            f"{len(q.get('quarantined') or [])} quarantined")
    if not q.get("power_ok"):
        return WARN, f"power floor NOT met ({bits})", \
            "the gate will comment, never block, until power@-5pp >= 0.60"
    if not q.get("churn_ok"):
        return WARN, f"churn over the ceiling ({bits})", \
            "the gate runs comment-only until the suite is stabilised"
    age = _age_days(q.get("measured_at"))
    if age is None or age > 100:
        return WARN, f"measured {q.get('measured_at')} ({bits})", \
            "over 100 days old; pair.py treats a stale measurement as unmeasured. Re-run the replays"
    return OK, f"{bits}, measured {age} day(s) ago", ""


def _age_days(ts):
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - t).days


def check_baselines():
    d = os.path.join(HERE, "baselines")
    idx = os.path.join(d, "index.json")
    if not os.path.exists(idx):
        return WARN, "no promoted baseline", \
            ("the first run REFUSEs, which is correct -- there is nothing to compare against. "
             "A push to main promotes one. `python3 fetch_baseline.py --head run.json --promote`")
    with open(idx) as f:
        n = len(json.load(f))
    return OK, f"{n} promoted baseline(s)", ""


CHECKS = [("python", check_python), ("node", check_node), ("promptfoo", check_promptfoo),
          ("pyyaml", check_yaml), ("api key", check_key),
          ("calibration", check_quarantine), ("baselines", check_baselines)]


def _selfcheck():
    assert _age_days("2020-01-01T00:00:00Z") > 2000
    assert _age_days(None) is None and _age_days("garbage") is None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _age_days(now) == 0
    assert _wrap("a b c", 3) == ["a b", "c"]
    assert _wrap("") == []
    # a doctor must never be the reason a run fails: every check returns a
    # triple, and main() catches anything that raises
    for name, fn in CHECKS:
        st, msg, fix = fn()
        assert st in (OK, WARN, FAIL), (name, st)
        assert isinstance(msg, str) and isinstance(fix, str), name
    print("doctor selfcheck OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quiet", action="store_true", help="only warnings and failures")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        return _selfcheck()

    bad = 0
    for name, fn in CHECKS:
        try:
            st, msg, fix = fn()
        except Exception as e:                      # a doctor must not need a doctor
            st, msg, fix = FAIL, f"check raised {type(e).__name__}: {e}", ""
        bad += st == FAIL
        if a.quiet and st == OK:
            continue
        print(f"  {st}  {name:12s} {msg}")
        if fix:
            for i, line in enumerate(_wrap(fix)):
                print(f"        {'-> ' if i == 0 else '   '}{line}")
    if bad:
        print(f"\n{bad} hard failure(s): the gate cannot run until these are fixed.")
    elif not a.quiet:
        print("\nReady. `cd regressgate && bash e2e_test.sh` proves the harness with no API key.")
    return 1 if bad else 0


def _wrap(s, w=74):
    words, line, out = s.split(), "", []
    for x in words:
        if len(line) + len(x) + 1 > w:
            out.append(line)
            line = x
        else:
            line = f"{line} {x}".strip()
    return out + ([line] if line else [])


if __name__ == "__main__":
    sys.exit(main())
