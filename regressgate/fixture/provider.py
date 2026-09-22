def call_api(prompt, options, context):
    v = context["vars"]
    if v.get("case_id") == "gamma":
        raise RuntimeError("simulated provider blowup for gamma")
    return {
        "output": v.get("payload", ""),
        "metadata": {
            "repeatIndex": v.get("__repeatIndex"),
            "model_id": "fake-model-v7",
        },
    }
