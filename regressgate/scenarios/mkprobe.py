"""Two scenarios that separate one-sided from two-sided McNemar in the GATE's verdict."""
import json, sys

CK = {"dataset_sha": "d41d8c", "judge_snapshot": "j1", "promptfoo_version": "0.123.1", "suite": "s"}

def doc(n, b, c, name):
    pairs = []
    for i in range(n):
        cid = f"case-{i:04d}"
        if i < c:      bp, hp = True, False     # broken
        elif i < c + b: bp, hp = False, True    # fixed
        else:          bp, hp = True, True
        pairs.append({"pair_id": cid + "#0", "case_id": cid, "baseline_pass": bp, "head_pass": hp})
    return {"harness_error": None, "n_cases_expected": n, "quarantined": [],
            "baseline": {"eval_id": "base", "contract_key": CK, "errors": 0},
            "head": {"eval_id": "head", "contract_key": CK, "errors": 0}, "pairs": pairs}

out = sys.argv[1]
json.dump(doc(60, 0, 5, "H"), open(f"{out}/H_marginal_b0_c5_n60.json", "w"))
json.dump(doc(300, 17, 4, "I"), open(f"{out}/I_big_improvement.json", "w"))
print("wrote H_marginal_b0_c5_n60.json (b=0 c=5 n=60) and I_big_improvement.json (b=17 c=4 n=300)")
