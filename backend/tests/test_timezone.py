"""Timezone handling: wall-clock times must map to the correct UTC instant,
including DST-observing zones, and be exposed back in the ride's zone."""

from datetime import date, datetime, time

from backend.timeutil import to_utc, from_utc, combine_local
from backend.tests.conftest import make_ride


def test_combine_local_ist_to_utc():
    dt = to_utc(datetime(2027, 6, 15, 9, 0), "Asia/Kolkata")
    assert dt == datetime(2027, 6, 15, 3, 30)


def test_roundtrip_kolkata():
    now = datetime(2027, 6, 15, 9, 0)
    utc = to_utc(now, "Asia/Kolkata")
    # from_utc yields a naive wall-clock in the zone
    wall = from_utc(utc, "Asia/Kolkata")
    assert wall == now


def test_dst_zone_new_york():
    # July: EDT (UTC-4) -> 9AM EDT == 13:00 UTC
    utc = to_utc(datetime(2027, 7, 15, 9, 0), "America/New_York")
    assert utc == datetime(2027, 7, 15, 13, 0)
    # January: EST (UTC-5) -> 9AM EST == 14:00 UTC
    utc = to_utc(datetime(2027, 1, 15, 9, 0), "America/New_York")
    assert utc == datetime(2027, 1, 15, 14, 0)


def test_midnight_boundary_kolkata():
    # just after midnight IST = 18:30Z the previous day
    utc = to_utc(datetime(2027, 6, 15, 0, 15), "Asia/Kolkata")
    assert utc == datetime(2027, 6, 14, 18, 45)


def test_ride_stored_and_served_in_zone(client, db, driver, vehicle):
    r = make_ride(client, driver["auth"], vehicle, day=15, hh=9, mm=0, tz="Asia/Kolkata")
    assert r.status_code == 201, r.get_json()
    ride = r.get_json()["ride"]
    from backend.timeutil import combine_local, iso_utc

    y, m, d = (int(p) for p in ride["departure_date"].split("-"))
    expected = iso_utc(combine_local(date(y, m, d), time(9, 0), "Asia/Kolkata"))
    assert ride["departure_at"] == expected
    # fields always expose ISO-8601 UTC
    assert ride["departure_at"].endswith("Z") or "+00:00" in ride["departure_at"]


def test_invalid_timezone_rejected(client, db, driver, vehicle):
    r = make_ride(client, driver["auth"], vehicle, tz="Not/AZone")
    assert r.status_code == 422
    assert r.get_json()["error"]["code"] == "validation_error"