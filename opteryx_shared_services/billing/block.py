"""Refuse work billed to an account that is suspended or over its spending cap.

The rule is control.opteryx's: it owns the account document and the fields read
here.

Who writes them:
- ``billing_status`` - control's Stripe webhook, ``suspended`` once Stripe stops
  retrying a failed invoice.
- ``cap_reached_period`` - xb500's hourly ``spend_check``, when month-to-date
  spend reaches the cap; control clears it when the cap is raised.

Fails open: an unreadable account document allows the query. The block is a
commercial control, not a security boundary, and a Firestore hiccup must not
refuse every query on the platform.
"""

from __future__ import annotations

import datetime
from typing import Callable
from typing import Optional

from opteryx_shared_services.billing.accounts import cached_account
from opteryx_shared_services.billing.plans import HOUSE_ACCOUNT_ID
from opteryx_shared_services.logging import get_logger

logger = get_logger()

BLOCK_PAYMENT_FAILED = "payment_failed"
BLOCK_SPEND_CAP = "spend_cap_reached"

MESSAGES = {
    BLOCK_PAYMENT_FAILED: (
        "Queries are paused for this billing account because an invoice could not be "
        "paid. A billing admin can update the card on the billing page, which pays it "
        "and resumes queries."
    ),
    BLOCK_SPEND_CAP: (
        "This billing account has reached its monthly spending cap. A billing admin can "
        "raise the cap on the billing page; otherwise queries resume next month."
    ),
}


def billing_period(now: Optional[datetime.datetime] = None) -> str:
    """The calendar month a spending cap applies to, ``YYYYMM`` in UTC."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).strftime("%Y%m")


def billing_block_reason(
    account_id: Optional[str], account: Optional[dict], now: Optional[datetime.datetime] = None
) -> Optional[str]:
    """Why work billed to this account must be refused, or None. Pure."""
    if account_id is None or account is None or account_id == HOUSE_ACCOUNT_ID:
        return None
    if account.get("billing_status") == "suspended":
        return BLOCK_PAYMENT_FAILED
    reached = account.get("cap_reached_period")
    if reached and reached == billing_period(now):
        return BLOCK_SPEND_CAP
    return None


def check(account_id: Optional[str], client_factory: Callable) -> Optional[str]:
    """Why ``account_id``'s work must be refused, or None. Fails open on a read error."""
    if not account_id or account_id == HOUSE_ACCOUNT_ID:
        return None
    try:
        account = cached_account(account_id, client_factory)
    except Exception as exc:  # noqa: BLE001 - fail open, see the module docstring
        logger.warning(f"billing block check for {account_id} failed, allowing: {exc}")
        return None
    return billing_block_reason(account_id, account)
