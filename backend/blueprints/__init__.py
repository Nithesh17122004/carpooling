"""Blueprint registry."""

from . import (
    admin,
    auth,
    bookings,
    geo,
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
    geo.bp,
    notify.bp,
    admin.bp,
    realtime.bp,
    ratings.bp,
    safety.bp,
)