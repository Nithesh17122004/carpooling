"""Database-index startup safety regression tests."""

import pytest

from backend.db import _payment_booking_unique


class _FailingIndexCollection:
    def index_information(self):
        return {}

    def aggregate(self, pipeline, allowDiskUse=False):
        return []

    def create_index(self, *args, **kwargs):
        raise RuntimeError("index build failed")


def test_production_payment_uniqueness_index_failure_is_fatal():
    with pytest.raises(RuntimeError, match="index build failed"):
        _payment_booking_unique(_FailingIndexCollection(), strict=True)


def test_nonproduction_payment_uniqueness_index_failure_can_retry_on_restart():
    # Development keeps the existing retry-on-next-start behavior.
    _payment_booking_unique(_FailingIndexCollection(), strict=False)
