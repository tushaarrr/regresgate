#!/usr/bin/env python3
"""Does this golden case actually test anything?

A regression gate is only as good as the assertions underneath it. An assertion
that passes on a wrong answer costs nothing on the day it is written and then
silently removes a case from the suite forever: it never fails, so it never
reports a regression, and the power calculation still counts it. That is worse
than deleting the case, because the case count says the suite is fine.

This lint builds adversarial probes FROM EACH CASE'S OWN ASSERTIONS -- no
domain knowledge, no per-case fixtures, nothing to keep in sync -- and runs
them through promptfoo 0.123.1's exact matching semantics. A probe that passes
is a wrong answer the case would accept.

    P1  topic word    a positive literal of one or two words carries no
                      verdict, so an answer asserting the OPPOSITE still
                      contains it. `icontains: accepted` is satisfied by
                      "not accepted"; `icontains: documentation` by "no
                      documentation is needed". Literals that contain their own
                      verb ("ACH is accepted") are not probed -- their negation
                      does not contain them.
    P2  parrot        the question text, echoed back as the answer. Catches
                      `icontains: Team` on a question that says "Team".
    P3  non-answer    "I don't have that in the policy, I'll pass you to a
                      human." Only for cases that are NOT supposed to refuse.
    P4  wrong number  every number in the assertions shifted (19 -> 19.99,
                      190, 119). Catches `regex: 19(?!\\d)` accepting "$119".
    P5  empty         "" and a bare "Yes." Nothing should pass these.

    python3 lint_cases.py                  # report
    python3 lint_cases.py --strict         # exit 1 if anything is flagged
    python3 lint_cases.py --selfcheck

Cases whose only remaining assertion is `llm-rubric` are reported separately:
a judge may well catch the probe, and this lint cannot know.
"""

import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# promptfoo 0.123.1 semantics, verified against src/assertions/: the icontains
# family lowercases BOTH sides; regex is `new RegExp(value).test(output)`, which
# is case-sensitive and unanchored; the `not-` prefix inverts the result.
SUBSTRING = {"icontains", "contains", "icontains-any", "contains-any",
             "icontains-all", "contains-all"}
GRADED = {"llm-rubric", "model-graded-closedqa", "answer-relevance", "factuality"}


def check(a, out):
    """-> True if this one assertion passes on `out`, or None if not checkable."""
    t = a.get("type", "")
    neg = t.startswith("not-")
    base = t[4:] if neg else t
    v = a.get("value")
    if base in GRADED:
        return None
    if base in ("contains", "icontains"):
        r = (str(v).lower() in out.lower()) if base[0] == "i" else (str(v) in out)
    elif base in ("contains-any", "icontains-any"):
        r = any((str(x).lower() in out.lower()) if base[0] == "i" else (str(x) in out)
                for x in v)
    elif base in ("contains-all", "icontains-all"):
        r = all((str(x).lower() in out.lower()) if base[0] == "i" else (str(x) in out)
                for x in v)
    elif base == "regex":
        try:
            r = re.search(str(v), out) is not None
        except re.error:
            return None
    elif base == "equals":
        r = out == str(v)
    else:
        return None
    return (not r) if neg else r


def passes(asserts, out):
    """-> (all checkable assertions passed, how many were checkable)."""
    res = [check(a, out) for a in asserts]
    hard = [r for r in res if r is not None]
    return (all(hard) if hard else False), len(hard)


def positives(asserts):
    """Every substring the case requires to be PRESENT."""
    out = []
    for a in asserts:
        t = a.get("type", "")
        if t.startswith("not-") or t not in SUBSTRING:
            continue
        v = a.get("value")
        out += [str(x) for x in (v if isinstance(v, list) else [v])]
    return out


def numbers(asserts):
    """Numbers the case REQUIRES to be present. Numbers inside a `not-` guard are
    what a wrong answer would say, so shifting them proves nothing."""
    out = set()
    for a in asserts:
        if a.get("type", "").startswith("not-"):
            continue
        for x in ([a.get("value")] if not isinstance(a.get("value"), list)
                  else a["value"]):
            out |= set(re.findall(r"\d+(?:[.,]\d+)?", str(x)))
    return sorted(out)


def probes(case):
    """[(probe_name, answer_text)] built only from this case's own assertions."""
    v, asserts = case.get("vars") or {}, case.get("assert") or []
    q = str(v.get("question") or "")
    pos = positives(asserts)
    out = [("P5 empty", ""), ("P5 bare-yes", "Yes.")]
    if q:
        out.append(("P2 parrot", q))
    for p in pos[:8]:
        if not _topic_word(p):
            continue
        out.append((f"P1 topic-word[{p}]", f"No, that is not {p} under our policy."))
        out.append((f"P1 topic-word[{p}]", f"No {p} is needed or available here."))
    if not _is_refusal_case(case):
        out.append(("P3 non-answer",
                    "I don't have that in the policy. I can pass this to a human."))
    for n in numbers(asserts)[:4]:
        bare = n.replace(",", "")
        for wrong in (f"{bare}.99", f"{bare}0", f"1{bare}"):
            out.append((f"P4 wrong-number[{n}->{wrong}]",
                        f"That is ${wrong} per seat per month, according to the policy."))
    return out


# A SINGLE bare noun or adjective carries no verdict: "accepted" survives "not
# accepted", "documentation" survives "no documentation is needed". A literal
# with more than one word usually pins enough of the sentence that its natural
# negation no longer contains it ("ACH is accepted" -> "ACH is not accepted"),
# and probing it would only produce gibberish, so P1 stays out of its way.
# "yes"/"no" are excluded because a wrong answer that denies the claim does not
# contain "yes" -- the probe would be a false positive.
# Polarity and refusal markers ARE the verdict for a refusal case ("cannot",
# "unable"), so a probe that denies them is gibberish rather than a wrong answer.
_STOP = {"yes", "no", "not", "none", "n/a", "cannot", "can't", "cant", "won't",
         "don't", "doesn't", "unable", "never", "nor", "neither", "sorry"}


def _topic_word(v):
    v = str(v).strip().strip(".,:;!?")
    if len(v.split()) != 1 or v.lower() in _STOP:
        return False
    return re.fullmatch(r"[A-Za-z][A-Za-z'-]*", v) is not None


def _is_refusal_case(case):
    """Cases whose CORRECT answer is 'not in the policy' must not be probed with
    P3: for them the non-answer is the right answer."""
    cid = str((case.get("vars") or {}).get("case_id") or "")
    if cid.split("-")[0] in ("nip", "oos", "clr", "auth", "saf"):
        return True
    for p in positives(case.get("assert") or []):
        if re.search(r"(?i)don'?t have|not in (the |our )?policy|pass(ing)? (you|this)"
                     r"|route|human|can'?t|cannot|unable", p):
            return True
    return False


def load(path):
    """Minimal reader for THIS file format: a flat YAML list of
    `- vars: {...}` / `assert: [...]` with flow mappings. Avoids a pyyaml
    dependency in the eval path, which is otherwise stdlib-only."""
    import subprocess
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        with open(path) as f:
            return yaml.safe_load(f) or []
    r = subprocess.run([sys.executable, "-c",
                        "import sys,yaml,json;json.dump(yaml.safe_load(open(sys.argv[1])),sys.stdout)",
                        path], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"lint_cases needs PyYAML to read {path}: pip install pyyaml")
    return json.loads(r.stdout) or []


# promptfoo compiles a regex with JS `new RegExp`. These compile in Python and
# either throw or mean something else in JS, so a regex that works under this
# lint would silently not work in the actual eval. Measured: JS has no inline
# flags, no named group with (?P<>), no \A/\Z/\z, no comment groups, and its
# \b is ASCII-only.
_PY_ONLY = [(re.compile(r"\(\?[aiLmsux]+\)"), "inline flags (?i) -- JS has none; "
             "use a character class like [Nn]o"),
            (re.compile(r"\(\?P[<=]"), "(?P<name>) -- JS spells it (?<name>)"),
            (re.compile(r"\\[AZz]"), "\\A / \\Z / \\z -- JS has ^ and $ only"),
            (re.compile(r"\(\?#"), "(?#comment) -- JS has no comment group")]


def js_incompatible(asserts):
    """-> [(value, why)] for regexes that would not behave the same under JS."""
    out = []
    for a in asserts:
        if not str(a.get("type", "")).endswith("regex"):
            continue
        v = str(a.get("value"))
        for pat, why in _PY_ONLY:
            if pat.search(v):
                out.append((v, why))
    return out


def lint(paths):
    flagged, rubric_only, total = [], [], 0
    for path in paths:
        for case in load(path):
            if not isinstance(case, dict) or "assert" not in case:
                continue
            total += 1
            cid = (case.get("vars") or {}).get("case_id", "?")
            hits = [("JS-incompatible regex: " + why, v)
                    for v, why in js_incompatible(case["assert"])]
            for name, text in probes(case):
                ok, n_hard = passes(case["assert"], text)
                if ok and n_hard:
                    hits.append((name, text))
                elif ok and not n_hard:
                    rubric_only.append((os.path.basename(path), cid))
            if hits:
                flagged.append({"file": os.path.basename(path), "case_id": cid,
                                "n_probes": len(hits), "probes": hits[:3]})
    return {"total": total, "flagged": flagged,
            "rubric_only": sorted(set(rubric_only))}


def _selfcheck():
    # a tautology: "accepted" is a substring of "not accepted"
    bad = {"vars": {"case_id": "x-1", "question": "Is ACH accepted on Team?"},
           "assert": [{"type": "icontains-any", "value": ["accepted", "yes"]}]}
    hits = [n for n, t in probes(bad) if passes(bad["assert"], t)[0]]
    assert any("topic-word" in h for h in hits), f"topic-word probe must catch this: {hits}"
    assert any("parrot" in h for h in hits), f"parrot probe must catch this: {hits}"
    # the discrimination P1 rests on
    assert _topic_word("accepted") and _topic_word("documentation") and _topic_word("Team")
    assert not _topic_word("ACH is accepted"), "a multi-word literal pins the sentence"
    assert not _topic_word("2 months free") and not _topic_word("Team and Enterprise only")
    assert not _topic_word("$19"), "numbers are P4's job"
    assert not _topic_word("yes") and not _topic_word("no"), "a denial contains neither"
    assert not _topic_word("cannot"), "a refusal marker IS the verdict"
    assert numbers([{"type": "not-icontains", "value": "$49"}]) == [], \
        "a number inside a not- guard is not a required number"
    assert numbers([{"type": "regex", "value": r"19(?!\d)"}]) == ["19"]
    # the two measured criticals this probe exists for
    d = [{"type": "icontains", "value": "documentation"}]
    assert any(passes(d, t)[0] for n, t in probes({"vars": {"case_id": "z"}, "assert": d})
               if "topic-word" in n), "must catch `icontains: documentation`"
    a = [{"type": "icontains-any", "value": ["accepted", "yes"]}]
    assert any(passes(a, t)[0] for n, t in probes({"vars": {"case_id": "z"}, "assert": a})
               if "topic-word" in n), "must catch `accepted` vs `not accepted`"

    # the same case, guarded, must come back clean
    good = {"vars": {"case_id": "x-2", "question": "Is ACH accepted on Team?"},
            "assert": [{"type": "icontains-any", "value": ["ACH is accepted", "ACH works"]},
                       {"type": "not-icontains", "value": "not accepted"},
                       {"type": "not-icontains", "value": "Is ACH accepted on Team"}]}
    hits = [n for n, t in probes(good) if passes(good["assert"], t)[0]]
    assert not hits, f"a guarded case must be clean: {hits}"

    # the measured number-boundary hole: 19(?!\d) accepts $19.99 and $119
    num = {"vars": {"case_id": "n-1", "question": "price?"},
           "assert": [{"type": "regex", "value": r"19(?!\d)"}]}
    hits = [n for n, t in probes(num) if passes(num["assert"], t)[0]]
    assert any("wrong-number" in h for h in hits), f"boundary probe must fire: {hits}"
    fixed = {"vars": {"case_id": "n-2", "question": "price?"},
             "assert": [{"type": "regex", "value": r"(?<![\d.,])19(?!\d|[.,]\d)"}]}
    assert not [n for n, t in probes(fixed) if passes(fixed["assert"], t)[0]], \
        "a guarded number regex must be clean"

    # a regex that works here but not in the eval is worse than no regex
    assert js_incompatible([{"type": "regex", "value": "(?i)no"}])
    assert js_incompatible([{"type": "not-regex", "value": r"(?P<n>x)"}])
    assert js_incompatible([{"type": "regex", "value": r"\Afoo"}])
    assert not js_incompatible([{"type": "regex", "value": r"(?<![\d.,])19(?!\d)"}]), \
        "JS supports lookbehind and lookahead"
    assert not js_incompatible([{"type": "icontains", "value": "(?i)"}]), "not a regex"

    # nothing may pass on an empty answer
    assert not passes([{"type": "icontains", "value": "x"}], "")[0]
    # promptfoo semantics: icontains lowercases both sides, regex does not
    assert check({"type": "icontains", "value": "ACH"}, "ach here") is True
    assert check({"type": "regex", "value": "ACH"}, "ach here") is False
    assert check({"type": "not-icontains", "value": "ACH"}, "ach here") is False
    assert check({"type": "llm-rubric", "value": "..."}, "anything") is None
    print("lint_cases selfcheck OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*", default=None)
    ap.add_argument("--strict", action="store_true", help="exit 1 if any case is flagged")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.selfcheck:
        return _selfcheck()

    paths = a.paths or sorted(glob.glob(os.path.join(HERE, "cases", "*.yaml")))
    r = lint(paths)
    if a.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        for f in r["flagged"]:
            print(f"{f['file']}:{f['case_id']}  accepts {f['n_probes']} wrong answer(s)")
            for name, text in f["probes"]:
                print(f"    {name:34s} {text[:78]!r}")
        if r["rubric_only"]:
            print(f"\n{len(r['rubric_only'])} case(s) rest entirely on llm-rubric; "
                  "this lint cannot judge them")
        print(f"\n{len(r['flagged'])} of {r['total']} cases accept a wrong answer "
              f"built from their own assertions")
    return 1 if (a.strict and r["flagged"]) else 0


if __name__ == "__main__":
    sys.exit(main())
