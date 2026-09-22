#!/usr/bin/env python3
"""Resolve the baseline eval a head run is allowed to be compared against.

A baseline is an export promoted from `main`, stored under its CONTRACT HASH --
the hash of the same contract_key gate.py diffs. So the lookup itself enforces
comparability: a suite that changed its golden set, its assertions, its judge or
its promptfoo pin simply misses, and the gate says REFUSE instead of reporting a
delta measured across two different experiments.

A miss is not an error condition to be papered over. Exit 1, no output file, and
let gate.py render the refusal.

    python3 fetch_baseline.py --head head.json --out baseline.json
    python3 fetch_baseline.py --head main.json --promote
    python3 fetch_baseline.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import pair

DEFAULT_DIR = os.environ.get(
    "REGRESSGATE_BASELINE_DIR", os.path.join(pair.HERE, "baselines"))


def contract_hash(key: dict) -> str:
    return pair.sha(key)


def head_contract(head_path: str) -> dict:
    return pair.contract_key(pair.load(head_path)["config"])


def find(key: dict, dirpath: str):
    p = os.path.join(dirpath, f"{contract_hash(key)}.json")
    return p if os.path.exists(p) else None


def promote(head_path: str, dirpath: str, git_sha=None) -> str:
    key = head_contract(head_path)
    os.makedirs(dirpath, exist_ok=True)
    h = contract_hash(key)
    dest = os.path.join(dirpath, f"{h}.json")
    # One baseline per contract, overwritten in place. The set of live contracts
    # is the natural bound on this directory.
    # ponytail: no retention policy; add --prune-keep if stale contracts pile up.
    shutil.copyfile(head_path, dest)
    index = {}
    ipath = os.path.join(dirpath, "index.json")
    if os.path.exists(ipath):
        with open(ipath) as f:
            index = json.load(f)
    index[h] = {"contract_key": key, "git_sha": pair.git_sha(git_sha),
                "eval_id": pair.load(head_path)["eval_id"],
                "promoted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    with open(ipath, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--head")
    ap.add_argument("--out")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--git-sha")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--github-output", action="store_true",
                    help="append baseline_sha / baseline_contract to $GITHUB_OUTPUT")
    a = ap.parse_args(argv)

    if a.list:
        ipath = os.path.join(a.dir, "index.json")
        print(open(ipath).read() if os.path.exists(ipath) else "{}  (no baselines)")
        return 0
    if not a.head:
        ap.error("--head is required")

    try:
        key = head_contract(a.head)
    except pair.Integrity as e:
        # An uncomparable head cannot have a baseline. Say why, and let pair.py
        # turn the same condition into a harness_error on the PR.
        print(f"::warning::cannot derive a contract key from the head run: {e}",
              file=sys.stderr)
        return 1

    if a.promote:
        dest = promote(a.head, a.dir, a.git_sha)
        print(f"promoted {a.head} -> {dest}\n  contract {json.dumps(key, sort_keys=True)}")
        return 0

    src = find(key, a.dir)
    if a.github_output and os.environ.get("GITHUB_OUTPUT"):
        # The baseline's commit is half the verdict cache key, and only this
        # module knows which baseline was actually chosen.
        meta = {}
        ipath = os.path.join(a.dir, "index.json")
        if os.path.exists(ipath):
            with open(ipath) as f:
                meta = json.load(f).get(contract_hash(key)) or {}
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"baseline_sha={meta.get('git_sha') or ''}\n")
            f.write(f"baseline_contract={contract_hash(key)}\n")
            f.write(f"missing={'1' if src is None else ''}\n")
    if src is None:
        print(f"::warning::no baseline for contract {contract_hash(key)} "
              f"in {a.dir}: {json.dumps(key, sort_keys=True)}", file=sys.stderr)
        return 1
    if a.out:
        shutil.copyfile(src, a.out)
    print(f"baseline {src} (contract {contract_hash(key)})")
    return 0


def _selfcheck(tmp):
    import tempfile
    d = os.path.join(tmp, "baselines")
    sample = os.path.join(tmp, "head.json")
    cfg = {"description": "s", "tests": [{"vars": {"case_id": "a"},
                                          "assert": [{"type": "contains", "value": "x"}]}]}
    with open(sample, "w") as f:
        json.dump({"evalId": "e1", "config": cfg,
                   "results": {"results": [], "stats": {"errors": 0}}}, f)
    key = head_contract(sample)
    assert find(key, d) is None, "empty dir must miss"
    assert main(["--head", sample, "--dir", d]) == 1, "a miss must exit 1"
    promote(sample, d)
    assert find(key, d) is not None, "promoted baseline must be found"
    assert main(["--head", sample, "--dir", d, "--out", os.path.join(tmp, "b.json")]) == 0

    # a changed assertion changes the contract -> the old baseline must NOT match
    moved = os.path.join(tmp, "head2.json")
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["tests"][0]["assert"][0]["value"] = "y"
    with open(moved, "w") as f:
        json.dump({"evalId": "e2", "config": cfg2,
                   "results": {"results": [], "stats": {"errors": 0}}}, f)
    assert find(head_contract(moved), d) is None, "contract drift must miss the baseline"
    print("fetch_baseline selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import tempfile
        sys.exit(_selfcheck(tempfile.mkdtemp()))
    sys.exit(main())
