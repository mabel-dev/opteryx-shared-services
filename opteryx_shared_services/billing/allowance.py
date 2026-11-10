"""The free tier's daily allowance: refuse a capped account's work once today's is used.

The commercial model is control.opteryx's ``docs/design/rate-cards.md``: every
allowance is DAILY, resets at 00:00 UTC, and does not carry forward. An unbilled
caller (no billing account, or an account whose effective plan is ``free``) is
capped, ``on_exceed = 'block'``; a billed account's card says ``bill``, so it is
never refused here. Spend caps and suspension are ``block``'s job.

Every query front door counts onto the same counter, so one day's allowance is
shared between them:

    billing_accounts/{account}/usage/{YYYYMMDD}    (UTC day, accounts database)
    { queries: int, bytes_processed: int, updated_at }

A missing document reads as zero - on a daily grain that is the common case.
Writes are ``Increment``s, never read-modify-write. Queries are counted when a
door accepts one (``count_admission``); processed bytes are added once the
query finishes (``add_bytes``), from the engine telemetry's ``billing_bytes``.
No reservations are taken, so nothing here can lock an account out by leaking
one - the cost is that queries in flight at once can overshoot the data
allowance by what they process.

Refusal is on ALREADY being at an allowance, never on a prediction of what the
next query will cost (see jobs.opteryx ``docs/design/billing-enforcement.md``).

Limits come from the rate card projection ``rate_cards/{rate_card_id}`` that
control.opteryx is to write; until it exists, the standard cards below - which
must match the live ``platform.pricing.rate_card`` - are used.

Fails OPEN: a service whose read fails lets the query run and logs at error
level. The allowance is a commercial control, not a security boundary.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Optional

from opteryx_shared_services.billing.plans import HOUSE_ACCOUNT_ID  # noqa: F401 - re-exported
from opteryx_shared_services.billing.plans import PLAN_FREE
from opteryx_shared_services.billing.plans import effective_plan  # noqa: F401 - re-exported
from opteryx_shared_services.logging import get_logger

logger = get_logger()

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"

METER_QUERIES = "query_execution"
METER_PROCESSED = "data_processed"

# The ledger and the cost model page use decimal gigabytes.
BYTES_PER_GIGABYTE = 1_000_000_000

STANDARD_INDIVIDUAL = "standard-individual"
STANDARD_ACCOUNT = "standard-account"

# rate-cards.md, Migration. Only the fields enforcement reads. The data
# allowance was raised from 0.167 to 1 GB/day (5 GB/day for an account) on
# 2026-11-10.
STANDARD_CARDS = {
    STANDARD_INDIVIDUAL: {
        METER_QUERIES: {
            "included_units": 100,
            "included_unit": "query",
            "allowance_basis": "daily_total",
            "on_exceed": "block",
        },
        METER_PROCESSED: {
            "included_units": 1.0,
            "included_unit": "gigabyte",
            "allowance_basis": "daily_total",
            "on_exceed": "block",
        },
    },
    STANDARD_ACCOUNT: {
        METER_QUERIES: {
            "included_units": 500,
            "included_unit": "query",
            "allowance_basis": "daily_total",
            "on_exceed": "bill",
        },
        METER_PROCESSED: {
            "included_units": 5.0,
            "included_unit": "gigabyte",
            "allowance_basis": "daily_total",
            "on_exceed": "bill",
        },
    },
}

REASON_QUERIES = "daily_query_allowance"
REASON_PROCESSED = "daily_data_allowance"

CARD_CACHE_TTL_SECONDS = 300
_card_cache: dict[str, tuple[float, dict]] = {}


def normalise_mode(raw) -> str:
    """`off` | `shadow` | `enforce`; anything unrecognised enforces."""
    value = str(raw or "").strip().lower()
    return value if value in (MODE_OFF, MODE_SHADOW) else MODE_ENFORCE


def usage_day(now: Optional[datetime.datetime] = None) -> str:
    """The counter's document id: the UTC calendar day, as the ledger groups it."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc).strftime("%Y%m%d")


def next_reset(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    """The next 00:00 UTC, when every daily allowance starts again."""
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    return (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_reset(now: Optional[datetime.datetime] = None) -> int:
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    return max(1, int((next_reset(now) - now).total_seconds()))


def card_id_for(plan: str, account: Optional[dict]) -> str:
    """A negotiated card if the account carries one, else the standard card its plan selects.

    Same COALESCE rate-cards.md's `charges_daily` join uses.
    """
    negotiated = (account or {}).get("rate_card_id")
    if isinstance(negotiated, str) and negotiated:
        return negotiated
    return STANDARD_INDIVIDUAL if plan == PLAN_FREE else STANDARD_ACCOUNT


def read_card(db, card_id: str) -> dict:
    """The card's meters, from the projection if present, else the standard card.

    Cached for `CARD_CACHE_TTL_SECONDS`: a card changes rarely. A read failure
    raises - the caller fails open.
    """
    now = time.monotonic()
    hit = _card_cache.get(card_id)
    if hit is not None and now - hit[0] < CARD_CACHE_TTL_SECONDS:
        return hit[1]
    snap = db.collection("rate_cards").document(card_id).get()
    projected = snap.to_dict() if snap.exists else None
    meters = (projected or {}).get("meters")
    if not isinstance(meters, dict) or not meters:
        # No projection for this card (CTRL-12 not built, or an unknown
        # negotiated id): the standard card, never "no limit".
        meters = STANDARD_CARDS.get(card_id) or STANDARD_CARDS[STANDARD_INDIVIDUAL]
    _card_cache[card_id] = (now, meters)
    return meters


@dataclass(frozen=True)
class Usage:
    queries: int = 0
    bytes_processed: int = 0


@dataclass(frozen=True)
class Refusal:
    reason: str
    message: str
    retry_after: int


def counter_ref(db, account_id: str, day: str):
    return db.collection("billing_accounts").document(account_id).collection("usage").document(day)


def read_usage(db, account_id: str, day: str) -> Usage:
    """Today's settled usage. A missing document is zero. Not cached."""
    snap = counter_ref(db, account_id, day).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    return Usage(
        queries=int(data.get("queries") or 0),
        bytes_processed=int(data.get("bytes_processed") or 0),
    )


def _allowance(meter: dict) -> Optional[float]:
    """The meter's daily allowance in the counter's own units, or None if it does not block."""
    if not isinstance(meter, dict):
        return None
    if meter.get("on_exceed") != "block" or meter.get("allowance_basis") != "daily_total":
        return None
    try:
        units = float(meter.get("included_units"))
    except (TypeError, ValueError):
        return None
    if meter.get("included_unit") == "gigabyte":
        return units * BYTES_PER_GIGABYTE
    return units


def _format_wait(seconds: int) -> str:
    hours, minutes = divmod(max(seconds, 60) // 60, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _format_gb(nbytes: float) -> str:
    gb = nbytes / BYTES_PER_GIGABYTE
    return f"{gb:.3f}".rstrip("0").rstrip(".") if gb < 10 else f"{gb:.1f}"


def decide(
    plan: str, card: dict, usage: Usage, now: Optional[datetime.datetime] = None
) -> Optional[Refusal]:
    """Whether to refuse a submission. Pure: no IO.

    Only `free` is capped by a daily allowance. `payg` and `house` are uncapped;
    `prepaid` is capped by its credit balance, which is not enforced here.
    Refuses when the account is ALREADY at an allowance - never on a prediction
    of what this query will cost (see the design doc for why).
    """
    if plan != PLAN_FREE:
        return None

    retry_after = seconds_until_reset(now)
    resets = f"The allowance resets at 00:00 UTC, in {_format_wait(retry_after)}."
    lift = "To keep querying before then, set up billing on the billing page."

    allowed_queries = _allowance(card.get(METER_QUERIES))
    if allowed_queries is not None and usage.queries >= allowed_queries:
        return Refusal(
            reason=REASON_QUERIES,
            message=(
                f"Daily query allowance used: you have run {usage.queries} of today's "
                f"{int(allowed_queries)} free queries. {resets} {lift}"
            ),
            retry_after=retry_after,
        )

    allowed_bytes = _allowance(card.get(METER_PROCESSED))
    if allowed_bytes is not None and usage.bytes_processed >= allowed_bytes:
        return Refusal(
            reason=REASON_PROCESSED,
            message=(
                f"Daily data allowance used: your queries have processed "
                f"{_format_gb(usage.bytes_processed)} GB of today's "
                f"{_format_gb(allowed_bytes)} GB free allowance. {resets} {lift}"
            ),
            retry_after=retry_after,
        )

    return None


def count_admission(db, account_id: str, day: str) -> None:
    """One more accepted query on today's counter."""
    from google.cloud import firestore

    counter_ref(db, account_id, day).set(
        {"queries": firestore.Increment(1), "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )


def add_bytes(db, account_id: str, day: str, nbytes: int) -> None:
    """Settle a finished query's processed bytes onto the counter of the day it was admitted."""
    from google.cloud import firestore

    counter_ref(db, account_id, day).set(
        {"bytes_processed": firestore.Increment(int(nbytes)), "updated_at": firestore.SERVER_TIMESTAMP},
        merge=True,
    )


# --- reading usage back, for the billing page --------------------------------


def daily_allowances(card: dict) -> dict:
    """`{"queries": n | None, "bytes_processed": n | None}` - None where the card does not block."""
    queries = _allowance(card.get(METER_QUERIES))
    processed = _allowance(card.get(METER_PROCESSED))
    return {
        "queries": int(queries) if queries is not None else None,
        "bytes_processed": int(processed) if processed is not None else None,
    }


def month_days(now: Optional[datetime.datetime] = None) -> list[str]:
    """Every counter day of the current UTC month, up to and including today."""
    today = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc)
    first = today.replace(day=1)
    return [usage_day(first + datetime.timedelta(days=i)) for i in range(today.day)]


def read_usage_days(db, account_id: str, days: list[str]) -> dict[str, Usage]:
    """Usage for each of `days`, in one batched read. A missing day is zero."""
    refs = [counter_ref(db, account_id, day) for day in days]
    found = {}
    for snap in db.get_all(refs):
        if snap.exists:
            data = snap.to_dict() or {}
            found[snap.id] = Usage(
                queries=int(data.get("queries") or 0),
                bytes_processed=int(data.get("bytes_processed") or 0),
            )
    return {day: found.get(day, Usage()) for day in days}


def clear_cache() -> None:
    """Forget cached rate cards. For tests, and for a service that must see a change now."""
    _card_cache.clear()
