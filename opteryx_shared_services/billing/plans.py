"""The plan in force for a billing account.

control.opteryx owns the account document and the meaning of its fields; this
is the rule every service resolves a plan with, so they cannot disagree.
"""

from __future__ import annotations

from typing import Optional

PLAN_FREE = "free"
PLAN_PREPAID = "prepaid"
PLAN_PAYG = "payg"
PLAN_HOUSE = "house"

PLANS = (PLAN_FREE, PLAN_PREPAID, PLAN_PAYG, PLAN_HOUSE)

#: The house account: internal queries, system tasks, platform maintenance.
#: Matches opteryx-core's ``DEFAULT_BILLING_ACCOUNT`` and the Firestore document
#: id of the house account.
HOUSE_ACCOUNT_ID = "opteryx"


def effective_plan(account_id: Optional[str], account: Optional[dict]) -> str:
    """Resolve the plan actually in force for a caller.

    The ``plan`` field on the account document is *intent*; this is what's
    enforced. They differ because two of the transitions are driven by state
    outside that field:

    - **No billing account at all** (``account_id`` or ``account`` is None)
      resolves to ``free``.
    - **No payment method attached** resolves to ``free``, whatever ``plan``
      says - ``prepaid`` included: detaching the card drops the account back to
      free-tier limits regardless of any remaining credit balance.
    - **``house`` is reserved to the house account.** ``plan: "house"`` on any
      other document is ignored, so an errant write cannot mint uncapped usage.
      The house account is exempt from the payment-method rule.
    """
    if account_id is None or account is None:
        return PLAN_FREE

    plan = account.get("plan")

    if plan == PLAN_HOUSE:
        return PLAN_HOUSE if account_id == HOUSE_ACCOUNT_ID else PLAN_FREE

    if account.get("payment_method") is None:
        return PLAN_FREE

    return plan if plan in (PLAN_PREPAID, PLAN_PAYG) else PLAN_FREE
