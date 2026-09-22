"""Meridian Support Assistant -- the feature under test.

Stdlib only, on purpose: urllib rather than the openai package, so CI installs
nothing and the provider cannot drift underneath the suite when a client
library ships a new default.

THREE ECHO CONTRACTS, all load-bearing. Breaking any of them does not break the
eval -- it silently breaks the harness that reads it:

  1. metadata.repeatIndex <- context.vars.__repeatIndex. promptfoo strips
     __repeatIndex from graders by design (it is None inside a python
     assertion), so the provider is the ONLY place it is observable. Without it
     pair.py cannot tell one repeat from another across two runs.
  2. metadata.modelId <- the `model` field OF THE RESPONSE, never the model we
     asked for. Asking for "gpt-4o-mini" and being served
     "gpt-4o-mini-2024-07-18" is exactly the drift drift_monitor.py exists to
     catch, and it is invisible if you echo the request.
  3. case_id stays an explicit var. Row indices are not case ids.

Errors RAISE. A raised exception becomes a promptfoo ERROR row (failureReason
2, counted in stats.errors, verified by tests/test_promptfoo_contract.py::
test_05), which the harness reports as an infrastructure alarm rather than as
a quality regression. Returning a string like "API error" instead would be
scored as a wrong answer and would look exactly like a real regression.
"""

import json
import os
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("REGRESSGATE_API", "https://api.openai.com/v1/chat/completions")
MODEL = os.environ.get("REGRESSGATE_MODEL", "gpt-4o-mini")
# A support assistant answering from a fixed policy is run cold. Higher
# temperature buys nothing here and costs power: churn eats the suite's ability
# to see a real regression, and the 6% ceiling in quarantine.py is a hard stop.
TEMPERATURE = float(os.environ.get("REGRESSGATE_TEMPERATURE", "0"))
TIMEOUT = float(os.environ.get("REGRESSGATE_TIMEOUT", "60"))
RETRIES = int(os.environ.get("REGRESSGATE_RETRIES", "4"))

_PROMPT = None


def system_prompt():
    """system_prompt.md with {policy} filled in from policy.md.

    Read once per process. Both files are hashed into the contract key through
    the config, so editing either one re-baselines rather than silently
    changing what the suite measures.
    """
    global _PROMPT
    if _PROMPT is None:
        with open(os.path.join(HERE, "system_prompt.md")) as f:
            tpl = f.read()
        with open(os.path.join(HERE, "policy.md")) as f:
            policy = f.read()
        _PROMPT = tpl.replace("{policy}", policy)
    return _PROMPT


def _post(body, key):
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def call_api(prompt, options, context):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        # Raise rather than return: a missing key is infrastructure, and the
        # harness must never score it as 300 wrong answers.
        raise RuntimeError("OPENAI_API_KEY is not set")

    v = (context or {}).get("vars", {}) or {}
    body = {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "max_tokens": 400,
        "messages": [{"role": "system", "content": system_prompt()},
                     {"role": "user", "content": prompt}],
    }

    last = None
    for attempt in range(RETRIES):
        try:
            data = _post(body, key)
            break
        except urllib.error.HTTPError as e:
            last = e
            # 429 and 5xx are transient; 4xx otherwise is our bug and retrying
            # it just burns quota against the same wrong request.
            if e.code != 429 and e.code < 500:
                raise RuntimeError(f"OpenAI {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"OpenAI unreachable after {RETRIES} attempts: {last!r}")

    return {
        "output": data["choices"][0]["message"]["content"],
        "metadata": {
            "repeatIndex": v.get("__repeatIndex"),
            # THE SERVED MODEL, from the response. Not MODEL.
            "modelId": data.get("model"),
        },
        "tokenUsage": {
            "prompt": (data.get("usage") or {}).get("prompt_tokens"),
            "completion": (data.get("usage") or {}).get("completion_tokens"),
            "total": (data.get("usage") or {}).get("total_tokens"),
        },
    }


if __name__ == "__main__":
    # Smoke test: one real call, printing exactly what the harness will read.
    out = call_api("How long is the free trial?", {}, {"vars": {"case_id": "smoke",
                                                                "__repeatIndex": 0}})
    print("served model :", out["metadata"]["modelId"])
    print("repeatIndex  :", out["metadata"]["repeatIndex"])
    print("output       :", out["output"][:300])
    assert out["metadata"]["modelId"], "provider must echo the SERVED model id"
    assert out["metadata"]["repeatIndex"] == 0, "provider must echo __repeatIndex"
    print("provider smoke OK")
