"""PLACEHOLDER provider for the regressgate pipeline (Phase 0 is yours to fill in).

Replace this with a call to the feature actually under test. What must survive
the replacement, because the harness depends on all three:

  1. echo `__repeatIndex` into metadata. It is stripped from graders by design,
     so a Python assertion sees None; the provider is the only place it is
     visible, and without it repeats cannot be told apart across runs.
  2. echo the SERVED model id -- what the API answered with, not what the config
     asked for. Model drift is detected on this string alone, at flat pass rate.
  3. keep `case_id` as an explicit var. Row indices are not case ids.

Running with REGRESSGATE_DEMO_BREAK=<n> makes the first n cases answer wrongly,
which is how the end-to-end test manufactures a regression with no API key.
"""

import os

ANSWERS = {
    "refund-window": "You can request a refund within 30 days of purchase.",
    "refund-partial": "Partial refunds are issued for unused subscription time.",
    "cancel-self": "You can cancel anytime from Settings > Billing > Cancel plan.",
    "cancel-refund": "Cancelling stops future charges; it does not refund past ones.",
    "invoice-vat": "Add your VAT number in Billing Details and it appears on invoices.",
    "invoice-past": "Past invoices are under Billing > Invoice history.",
    "seat-add": "Add seats from the Team page; billing is prorated immediately.",
    "seat-remove": "Removing a seat credits the unused time to your next invoice.",
    "sso-setup": "SSO is available on Enterprise; configure SAML in Security settings.",
    "sso-scim": "SCIM provisioning syncs users automatically once SAML is live.",
    "export-data": "Export your data as CSV or JSON from Settings > Data export.",
    "delete-account": "Account deletion is permanent and removes all data after 30 days.",
}
BREAK_ORDER = list(ANSWERS)


def call_api(prompt, options, context):
    v = (context or {}).get("vars", {}) or {}
    case_id = v.get("case_id")
    n_broken = int(os.environ.get("REGRESSGATE_DEMO_BREAK", "0"))
    broken = set(BREAK_ORDER[:n_broken])
    output = ("I am not able to help with that."
              if case_id in broken else ANSWERS.get(case_id, "unknown case"))
    return {
        "output": output,
        "metadata": {
            # (1) the only place the repeat index is observable
            "repeatIndex": v.get("__repeatIndex"),
            # (2) the SERVED model, echoed back from the response
            "modelId": os.environ.get("REGRESSGATE_DEMO_MODEL", "demo-model-2026-01"),
        },
    }
