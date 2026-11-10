"""``billing_accounts/{id}`` documents, read once a minute per process.

Shared by ``block`` and ``allowance`` so a submission that runs both checks pays
for one Firestore read. A block, an unblock or a plan change therefore takes up
to ``CACHE_TTL_SECONDS`` to reach every instance.
"""

from __future__ import annotations

import time
from typing import Callable
from typing import Optional

CACHE_TTL_SECONDS = 60
_CACHE_MAX = 10_000

_cache: dict[str, tuple[float, Optional[dict]]] = {}


def cached_account(account_id: str, client_factory: Callable) -> Optional[dict]:
    """The ``billing_accounts/{account_id}`` document, or None if it does not exist.

    ``client_factory`` returns a Firestore client for the accounts database; it
    is only called on a cache miss. Raises if the read fails - callers decide
    how to fail (every check in this package fails open).

    A caller with no billing account bills to its own identity, which has no
    document: that is None, and resolves to the free plan.
    """
    now = time.monotonic()
    hit = _cache.get(account_id)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    db = client_factory()
    snap = db.collection("billing_accounts").document(account_id).get()
    account = snap.to_dict() if snap.exists else None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[account_id] = (now, account)
    return account


def clear_cache() -> None:
    """Forget every cached account. For tests, and for a service that must see a change now."""
    _cache.clear()
