"""Blueprint registry."""

from . import (
    admin,
    apisetu,
    auth,
    bookings,
    geo,
    kyc,
    notify,
    profile,
    ratings,
    realtime,
    rides,
    safety,
    uploads,
    vehicles,
)

ALL_BLUEPRINTS = (
    auth.bp,
    profile.bp,
    vehicles.bp,
    rides.bp,
    bookings.bp,
    uploads.bp,
    kyc.bp,
    geo.bp,
    notify.bp,
    admin.bp,
    apisetu.bp,
    realtime.bp,
    ratings.bp,
    safety.bp,
)
