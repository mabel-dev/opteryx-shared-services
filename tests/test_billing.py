"""The billing rules every query front door enforces.

Pure rules are table-tested; the Firestore reads and writes run against a small
path-keyed fake that understands subcollections and ``Increment``.
"""

import datetime

import pytest
from google.cloud.firestore_v1.transforms import Increment

from opteryx_shared_services.billing import accounts
from opteryx_shared_services.billing import allowance as ua
from opteryx_shared_services.billing import block
from opteryx_shared_services.billing import plans

UTC = datetime.timezone.utc
NOON = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NOV = datetime.datetime(2026, 11, 9, 12, tzinfo=UTC)
GB = ua.BYTES_PER_GIGABYTE
INDIVIDUAL = ua.STANDARD_CARDS[ua.STANDARD_INDIVIDUAL]
ACCOUNT = ua.STANDARD_CARDS[ua.STANDARD_ACCOUNT]


@pytest.fixture(autouse=True)
def _clear_caches():
    accounts.clear_cache()
    ua.clear_cache()
    yield
    accounts.clear_cache()
    ua.clear_cache()


# --- plans -------------------------------------------------------------------


@pytest.mark.parametrize(
    "account_id, account, expected",
    [
        (None, None, "free"),
        ("acct", None, "free"),
        ("acct", {"plan": "payg", "payment_method": None}, "free"),
        ("acct", {"plan": "prepaid", "payment_method": None}, "free"),
        ("acct", {"plan": "payg", "payment_method": {"last4": "4242"}}, "payg"),
        ("acct", {"plan": "prepaid", "payment_method": {"last4": "4242"}}, "prepaid"),
        ("acct", {"plan": "free", "payment_method": {"last4": "4242"}}, "free"),
        ("acct", {"plan": "nonsense", "payment_method": {"last4": "4242"}}, "free"),
        ("acct", {"plan": "house"}, "free"),
        ("opteryx", {"plan": "house"}, "house"),
    ],
)
def test_effective_plan(account_id, account, expected):
    assert plans.effective_plan(account_id, account) == expected


# --- block -------------------------------------------------------------------


@pytest.mark.parametrize(
    "account_id, account, expected",
    [
        (None, None, None),
        ("acct", None, None),
        ("acct", {}, None),
        ("acct", {"billing_status": "active"}, None),
        ("acct", {"billing_status": "past_due"}, None),
        ("acct", {"billing_status": "suspended"}, "payment_failed"),
        ("acct", {"cap_reached_period": "202611"}, "spend_cap_reached"),
        ("acct", {"cap_reached_period": "202610"}, None),
        ("acct", {"billing_status": "suspended", "cap_reached_period": "202611"}, "payment_failed"),
        ("opteryx", {"billing_status": "suspended", "cap_reached_period": "202611"}, None),
    ],
)
def test_block_reason(account_id, account, expected):
    assert block.billing_block_reason(account_id, account, now=NOV) == expected


def test_block_check_reads_once_and_fails_open():
    fs = _Firestore()
    fs.account("acct_1", billing_status="suspended")
    calls = []

    def factory():
        calls.append(1)
        return fs

    assert block.check("acct_1", factory) == "payment_failed"
    assert block.check("acct_1", factory) == "payment_failed"
    assert len(calls) == 1, "the account read is cached"

    def broken():
        raise RuntimeError("firestore unavailable")

    accounts.clear_cache()
    assert block.check("acct_1", broken) is None
    assert block.check("opteryx", broken) is None, "the house is never read"


# --- allowance: the rule -------------------------------------------------------


@pytest.mark.parametrize(
    "plan, card, usage, reason",
    [
        ("free", INDIVIDUAL, ua.Usage(), None),
        ("free", INDIVIDUAL, ua.Usage(bytes_processed=int(0.99 * GB)), None),
        ("free", INDIVIDUAL, ua.Usage(bytes_processed=GB), ua.REASON_PROCESSED),
        ("free", INDIVIDUAL, ua.Usage(bytes_processed=10 * GB), ua.REASON_PROCESSED),
        # A card-less account is `free` on the account card: capped at 5x, not billed.
        ("free", ACCOUNT, ua.Usage(bytes_processed=int(4.99 * GB)), None),
        ("free", ACCOUNT, ua.Usage(bytes_processed=5 * GB), ua.REASON_PROCESSED),
        ("payg", ACCOUNT, ua.Usage(bytes_processed=10 * GB), None),
        ("payg", INDIVIDUAL, ua.Usage(bytes_processed=10 * GB), None),
        ("prepaid", INDIVIDUAL, ua.Usage(bytes_processed=10 * GB), None),
        ("house", INDIVIDUAL, ua.Usage(bytes_processed=10 * GB), None),
    ],
)
def test_decide(plan, card, usage, reason):
    refusal = ua.decide(plan, card, usage, NOON)
    assert (refusal.reason if refusal else None) == reason


@pytest.mark.parametrize(
    "usage, query_bytes, reason",
    [
        # No figure: the old rule, one query's overshoot allowed.
        (ua.Usage(bytes_processed=2 * GB), None, None),
        (ua.Usage(bytes_processed=2 * GB), 3 * GB, None),  # lands exactly on it
        (ua.Usage(bytes_processed=2 * GB), 3 * GB + 1, ua.REASON_QUERY_PROCESSED),
        (ua.Usage(bytes_processed=2 * GB), 22 * GB, ua.REASON_QUERY_PROCESSED),
        (ua.Usage(), 6 * GB, ua.REASON_QUERY_PROCESSED),  # bigger than a whole day
        (ua.Usage(), 0, None),
        # Already over still says so, not "this query".
        (ua.Usage(bytes_processed=5 * GB), 1, ua.REASON_PROCESSED),
    ],
)
def test_decide_on_the_query_figure(usage, query_bytes, reason):
    refusal = ua.decide("free", ACCOUNT, usage, NOON, query_bytes=query_bytes)
    assert (refusal.reason if refusal else None) == reason


def test_the_query_figure_never_refuses_a_paying_plan():
    assert ua.decide("payg", ACCOUNT, ua.Usage(), NOON, query_bytes=100 * GB) is None


def test_query_refusal_says_how_much_and_how_much_is_left():
    refusal = ua.decide("free", ACCOUNT, ua.Usage(bytes_processed=2 * GB), NOON, query_bytes=22 * GB)
    assert "would process 22.0 GB" in refusal.message
    assert "only 3 GB of today's 5 GB" in refusal.message
    assert refusal.retry_after == 12 * 3600


def test_standard_cards_match_the_live_rate_card():
    """1 GB/day individual, 5 GB/day account - as platform.pricing.rate_card since 2026-11-10."""
    assert ua.daily_allowances(INDIVIDUAL) == {"bytes_processed": GB}
    assert ua.daily_allowances(ACCOUNT) == {"bytes_processed": 5 * GB}
    assert ACCOUNT[ua.METER_PROCESSED]["included_units"] == 5.0
    # Usage is data stored and data processed only: no query-count meter (OPX-3).
    assert set(INDIVIDUAL) == set(ACCOUNT) == {ua.METER_PROCESSED}


def test_refusal_says_which_limit_and_when_it_resets():
    refusal = ua.decide("free", INDIVIDUAL, ua.Usage(bytes_processed=int(1.2 * GB)), NOON)
    assert "processed 1.2 GB of today's 1 GB" in refusal.message
    assert "00:00 UTC, in 12 h 0 min" in refusal.message
    assert refusal.retry_after == 12 * 3600


def test_the_day_is_the_utc_day():
    late_in_new_york = datetime.datetime(
        2026, 11, 10, 23, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))
    )
    assert ua.usage_day(late_in_new_york) == "20261111"
    assert ua.usage_day(NOON) == "20261110"
    assert ua.next_reset(NOON) == datetime.datetime(2026, 11, 11, tzinfo=UTC)
    assert ua.seconds_until_reset(datetime.datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC)) == 30
    assert ua.month_days(datetime.datetime(2026, 11, 3, 5, tzinfo=UTC)) == [
        "20261101",
        "20261102",
        "20261103",
    ]


def test_card_selection():
    assert ua.card_id_for("free", None) == ua.STANDARD_INDIVIDUAL
    assert ua.card_id_for("payg", {}) == ua.STANDARD_ACCOUNT
    assert ua.card_id_for("free", {"rate_card_id": "trial-2026"}) == "trial-2026"


def test_cardless_account_is_capped_at_the_account_allowance():
    # A billing account with no payment method resolves to `free` (it cannot be
    # charged) but is still the 5x tier: capped at 5 GB, never billed.
    account = {"plan": "payg", "payment_method": None}
    plan = ua.effective_plan("acct_1", account)
    assert plan == "free"
    card_id = ua.card_id_for(plan, account)
    assert card_id == ua.STANDARD_ACCOUNT
    card = ua.STANDARD_CARDS[card_id]
    assert ua.daily_allowances(card) == {"bytes_processed": 5_000_000_000}
    assert ua.decide(plan, card, ua.Usage(bytes_processed=int(4.99 * GB)), NOON) is None
    refusal = ua.decide(plan, card, ua.Usage(bytes_processed=5 * GB), NOON)
    assert refusal is not None and refusal.reason == ua.REASON_PROCESSED


@pytest.mark.parametrize("raw, mode", [(None, "enforce"), ("SHADOW", "shadow"), (" off ", "off"), ("x", "enforce")])
def test_normalise_mode(raw, mode):
    assert ua.normalise_mode(raw) == mode


# --- allowance: Firestore ------------------------------------------------------


class _Snap:
    def __init__(self, data, doc_id):
        self._data, self.exists, self.id = data, data is not None, doc_id

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, fs, path):
        self._fs, self._path = fs, path

    def collection(self, name):
        return _Coll(self._fs, self._path + (name,))

    def get(self):
        return _Snap(self._fs.docs.get(self._path), self._path[-1])

    def set(self, doc, merge=False):
        current = dict(self._fs.docs.get(self._path) or {}) if merge else {}
        for key, value in doc.items():
            current[key] = (
                current.get(key, 0) + value._value if isinstance(value, Increment) else value
            )
        self._fs.docs[self._path] = current


class _Coll:
    def __init__(self, fs, path):
        self._fs, self._path = fs, path

    def document(self, key):
        return _Doc(self._fs, self._path + (key,))


class _Firestore:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _Coll(self, (name,))

    def get_all(self, refs):
        return [ref.get() for ref in refs]

    def account(self, account_id, **doc):
        self.docs[("billing_accounts", account_id)] = doc


def test_counter_reads_and_increments():
    fs = _Firestore()
    assert ua.read_usage(fs, "alice", "20261110") == ua.Usage()

    ua.add_bytes(fs, "alice", "20261110", 30_000_000)
    ua.add_bytes(fs, "alice", "20261110", 10_000_000)

    assert ua.read_usage(fs, "alice", "20261110") == ua.Usage(bytes_processed=40_000_000)
    assert ("billing_accounts", "alice", "usage", "20261110") in fs.docs

    # A counter day written before OPX-3 still carries `queries`; it is ignored.
    fs.docs[("billing_accounts", "alice", "usage", "20261109")] = {"queries": 7, "bytes_processed": 5}
    days = ua.read_usage_days(fs, "alice", ["20261108", "20261109", "20261110"])
    assert days == {
        "20261108": ua.Usage(),
        "20261109": ua.Usage(bytes_processed=5),
        "20261110": ua.Usage(bytes_processed=40_000_000),
    }


def test_read_card_prefers_the_projection_and_falls_back_to_standard():
    fs = _Firestore()
    assert ua.read_card(fs, ua.STANDARD_INDIVIDUAL) is ua.STANDARD_CARDS[ua.STANDARD_INDIVIDUAL]
    assert ua.read_card(fs, "unknown-negotiated") is ua.STANDARD_CARDS[ua.STANDARD_INDIVIDUAL]

    ua.clear_cache()
    projected = {ua.METER_PROCESSED: dict(INDIVIDUAL[ua.METER_PROCESSED], included_units=5)}
    fs.docs[("rate_cards", ua.STANDARD_INDIVIDUAL)] = {"meters": projected}
    assert ua.read_card(fs, ua.STANDARD_INDIVIDUAL) == projected


def test_cached_account_reads_once_per_ttl():
    fs = _Firestore()
    fs.account("acct_1", plan="payg")
    calls = []

    def factory():
        calls.append(1)
        return fs

    assert accounts.cached_account("acct_1", factory) == {"plan": "payg"}
    assert accounts.cached_account("acct_1", factory) == {"plan": "payg"}
    assert accounts.cached_account("nobody", factory) is None
    assert len(calls) == 2
