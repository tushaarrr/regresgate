def call_api(prompt, options, context):
    v = context.get("vars", {})
    return {
        "output": f"answer:{v.get('case_id')}",
        "metadata": {
            "repeatIndex": v.get("__repeatIndex"),
            "modelId": "fixture-model-2026-01",
        },
    }
