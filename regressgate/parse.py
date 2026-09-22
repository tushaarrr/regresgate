"""JSON -> per-case rows normalizer for promptfoo `eval -o out.json` (version 3).

Ground truth this file is built to (all measured against promptfoo 0.123.1):
  - rows live at out["results"]["results"] (doubled key)
  - gradingResult and gradingResult.componentResults can BOTH be absent on error rows
  - under an assert-set, componentResults is FLATTENED: every child appears twice
    (nested + promoted) and the wrapper entry has NO 'assertion' key
  - nothing on a row identifies the repeat; __repeatIndex is stripped from graders
    by design, so the provider must echo it in its metadata
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

# Row-level verdict. UNSCORED is deliberately distinct from FAILED: a row with no
# gradingResult (or a grader that produced nothing parseable) is not evidence of a
# quality regression, and must never be counted as one.
PASSED = "PASSED"
FAILED = "FAILED"
ERROR = "ERROR"
UNSCORED = "UNSCORED"


@dataclass
class Assertion:
    type: str | None
    passed: bool | None
    score: float | None
    reason: str | None  # the judge's feedback text -- this is the feedback corpus


@dataclass
class Row:
    case_id: str | None
    prompt_idx: int | None
    repeat_index: int | None
    state: str
    success: bool | None
    score: float | None
    model_id: str | None
    latency_ms: int | None
    cost: float | None
    error: str | None
    assertions: list[Assertion] = field(default_factory=list)


def _meta(row: dict[str, Any], key: str):
    """Provider-echoed metadata lands at BOTH row.metadata and row.response.metadata."""
    for holder in (row.get("metadata"), (row.get("response") or {}).get("metadata")):
        if isinstance(holder, dict) and holder.get(key) is not None:
            return holder[key]
    return None


def leaf_component_results(grading_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Leaves only: skips assert-set wrappers and the duplicate nested copies.

    A leaf HAS an 'assertion' key and does NOT carry its own componentResults.
    Wrappers have componentResults but no 'assertion' -- touching
    wrapper["assertion"]["type"] raises TypeError, which is how naive parsers die.
    """
    if not isinstance(grading_result, dict):
        return []
    out: list[dict[str, Any]] = []
    for c in grading_result.get("componentResults") or []:
        if not isinstance(c, dict):
            continue
        if c.get("assertion") is None:
            continue  # assert-set wrapper
        if c.get("componentResults"):
            continue  # nested parent; its children are promoted alongside it
        out.append(c)
    return out


def normalize_row(row: dict[str, Any]) -> Row:
    gr = row.get("gradingResult")
    comps = leaf_component_results(gr)

    # MEASURED TRAP: row["error"] is populated on ORDINARY ASSERTION FAILURES too
    # (beta, failureReason=1, error="Expected output to contain \"goodbye\"").
    # Truth is failureReason: 0=NONE 1=ASSERT 2=ERROR [src/types/index.ts:381-388].
    reason_code = row.get("failureReason")
    raw_err = row.get("error") or (row.get("response") or {}).get("error")

    if reason_code == 2 or (reason_code is None and gr is None and raw_err):
        state, err = ERROR, raw_err
    elif row.get("success") is True:
        state, err = PASSED, None
    elif gr is None and not comps:
        # ran, produced no grader verdict of any kind -> NOT a quality regression
        state, err = UNSCORED, None
    elif row.get("success") is False:
        state, err = FAILED, None
    else:
        state, err = UNSCORED, None

    return Row(
        case_id=(row.get("vars") or {}).get("case_id"),
        prompt_idx=row.get("promptIdx"),
        # NEVER testIdx % N: breaks on per-test `options.repeat` and on string-array
        # vars that expand into extra combinations. Provider echo or nothing.
        repeat_index=_meta(row, "repeatIndex"),
        state=state,
        success=row.get("success"),
        score=row.get("score"),
        # Accept both spellings: providers echo either, and a None here makes
        # model-drift detection fail OPEN (None == None reads as "no drift").
        model_id=_meta(row, "model_id") or _meta(row, "modelId"),
        latency_ms=row.get("latencyMs"),
        cost=row.get("cost"),
        error=err,
        assertions=[
            Assertion(
                type=(c.get("assertion") or {}).get("type"),
                passed=c.get("pass"),
                score=c.get("score"),
                reason=c.get("reason"),
            )
            for c in comps
        ],
    )


def parse(path: str) -> list[Row]:
    with open(path) as f:
        out = json.load(f)
    return [normalize_row(r) for r in out["results"]["results"]]


def group_key(r: Row) -> tuple:
    """Safe grouping for repeated samples of the same case."""
    return (r.case_id, r.prompt_idx)


def to_json(rows: list[Row]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=2)


def _selfcheck() -> None:
    # assert-set flattening: wrapper (no 'assertion'), nested parent, two promoted leaves
    leaf_a = {"assertion": {"type": "contains"}, "pass": True, "score": 1.0, "reason": "ok"}
    leaf_b = {"assertion": {"type": "icontains"}, "pass": False, "score": 0.0, "reason": "nope"}
    gr = {
        "componentResults": [
            {"pass": False, "score": 0.5, "componentResults": [leaf_a, leaf_b]},  # wrapper
            leaf_a,
            leaf_b,
        ]
    }
    assert [c["assertion"]["type"] for c in leaf_component_results(gr)] == ["contains", "icontains"]
    assert leaf_component_results(None) == []
    assert leaf_component_results({}) == []

    r = normalize_row({"vars": {"case_id": "x"}, "promptIdx": 0, "failureReason": 2, "error": "boom"})
    assert r.state == ERROR and r.repeat_index is None
    r = normalize_row({"vars": {}, "promptIdx": 0, "metadata": {"repeatIndex": 1, "model_id": "m"}})
    assert (r.state, r.repeat_index, r.model_id) == (UNSCORED, 1, "m")
    # an assertion failure is FAILED, not ERROR, even though row["error"] is set
    r = normalize_row({"vars": {}, "promptIdx": 0, "failureReason": 1, "success": False,
                       "error": "Expected output to contain \"x\"",
                       "gradingResult": {"componentResults": [leaf_b]}})
    assert r.state == FAILED and r.error is None, r
    print("parse selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
