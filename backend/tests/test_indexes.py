"""Indexes that the money path depends on.

`create_indexes` wraps most of its calls in `try/except`, which is right for a
legacy database but wrong as a way to find out whether an index exists. A
partial index that fails to build because the collection already holds
duplicates is exactly the situation where you want to hear about it, and the
worker is then silently vulnerable to a double payout.

So this file asserts the indexes are present, rather than trusting that the call
did not raise.
"""

import pytest

# (collection, index key spec) that must exist for correctness, not just speed.
_REQUIRED = [
    # A driver cannot be paid twice for one settlement period.
    ("payouts", [("user_id", 1), ("period.auto_key", 1)]),
    # The worker's auto-confirm scan reads these every pass.
    ("bookings", [("completion_status", 1), ("completion_deadline", 1)]),
    # Admin review queues.
    ("users", [("kyc_status", 1), ("kyc_submitted_at", 1)]),
    ("users", [("payout_onboarding_status", 1), ("payout_onboarding_updated_at", 1)]),
    # One identity number, one account, per KYC role. Uniqueness is enforced by
    # the database, so the duplicate check in submit_identity cannot be raced
    # past by two concurrent submissions.
    ("users", [("kyc_fingerprint", 1)]),
    ("vehicles", [("rc_document.verification_status", 1),
                  ("rc_document.uploaded_at", 1)]),
    # The dispute queue, oldest first.
    ("bookings", [("status", 1), ("disputed_at", 1)]),
    # "Every event that can move money for this trip."
    ("audit_logs", [("target_type", 1), ("target_id", 1), ("created_at", -1)]),
]


def _index_keys(db, collection):
    """Plain [(field, direction), ...] lists.

    pymongo returns the key as a SON, and a SON does not compare equal to the
    list of tuples it wraps, so the raw values would never match.
    """
    return [list(info["key"].items()) for info in db[collection].list_indexes()]


@pytest.mark.parametrize("collection,keys", _REQUIRED,
                         ids=["%s:%s" % (c, ".".join(k for k, _ in ks))
                              for c, ks in _REQUIRED])
def test_a_required_index_exists(db, collection, keys):
    found = _index_keys(db, collection)
    assert list(keys) in found, "%s is missing the index %s" % (collection, keys)


def test_the_period_index_is_unique_not_merely_present(db):
    """A non-unique index would let a check-then-insert race through."""
    unique = [info for info in db.payouts.list_indexes()
              if list(info["key"].items()) == [("user_id", 1),
                                               ("period.auto_key", 1)]]
    assert unique, "the payout-period index does not exist"
    assert unique[0].get("unique") is True, unique[0]


def test_the_identity_fingerprint_index_is_unique_and_partial(db):
    """Uniqueness is the whole point, and `partial` is what keeps it from
    rejecting every account that has never submitted identity KYC."""
    found = [info for info in db.users.list_indexes()
             if list(info["key"].items()) == [("kyc_fingerprint", 1)]]
    assert found, "the identity-fingerprint index does not exist"
    assert found[0].get("unique") is True, found[0]
    assert found[0].get("partialFilterExpression") == {"kyc_fingerprint":
                                                       {"$type": "string"}}, found[0]



def test_index_creation_is_idempotent(db):
    """Startup runs this on every boot; a second call must not raise."""
    from backend.db import create_indexes

    create_indexes()
    create_indexes()
