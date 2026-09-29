"""Ratings: post-trip peer reviews with integrity guards.

Rules
-----
- Only the two parties of a confirmed booking may participate (driver and the
  rider on the booking), and each party rates the OTHER party once.
- Ratings are only accepted after the ride has departed and is not cancelled.
- A partial-unique index prevents duplicate ratings even under concurrency.
- The rated user's aggregate score is recomputed after every insert.
"""

from flask import Blueprint, g, request

from pymongo.errors import DuplicateKeyError

from ..db import get_db, to_object_id, utcnow
from ..errors import APIError
from ..ratelimit import rate_limit
from ..security import require_auth
from ..states import BOOKING_CONFIRMED, RIDE_CANCELLED
from ..timeutil import is_past
from ..validators import as_int, as_str, body

bp = Blueprint("ratings", __name__, url_prefix="/api/ratings")


def _recompute(db, user_id):
    score = db["ratings"].aggregate([
        {"$match": {"rated_user_id": user_id}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "count": {"$sum": 1}}},
    ])
    row = next(score, None)
    if row is None:
        db.users.update_one({"_id": user_id}, {"$set": {"rating": 0.0, "rating_count": 0, "updated_at": utcnow()}})
        return
    db.users.update_one({"_id": user_id}, {"$set": {
        "rating": round(row["avg"], 1),
        "rating_count": row["count"],
        "updated_at": utcnow(),
    }})


@bp.post("")
@require_auth
@rate_limit("default")
def create_rating():
    db = get_db()
    data = body()
    booking_id = to_object_id(data.get("booking_id") or "", "booking")
    rated_id = to_object_id(data.get("rated_user_id") or "", "user")
    rating = as_int(data.get("rating"), "rating", required=True)
    review = as_str(data.get("review", ""), "review", max_len=500)
    if rating < 1 or rating > 5:
        raise APIError("Rating must be an integer between 1 and 5.", 422, code="validation_error")

    reviewer = db.users.find_one({"_id": g.user["_id"]})
    booking = db.bookings.find_one({"_id": booking_id})
    if not booking:
        raise APIError("Booking not found.", 404, code="not_found")
    if booking.get("status") != BOOKING_CONFIRMED:
        raise APIError("Only confirmed bookings can be rated.", 409, code="not_eligible")

    ride = db.rides.find_one({"_id": booking.get("ride_id")}, {"owner_id": 1, "status": 1, "departure_at": 1})
    if not ride:
        raise APIError("Booking has no ride.", 409, code="not_eligible")
    if ride.get("status") == RIDE_CANCELLED:
        raise APIError("Cancelled rides cannot be rated.", 409, code="not_eligible")
    if not is_past(ride.get("departure_at"), utcnow()):
        raise APIError("Ratings open once the ride departs.", 409, code="not_departed")

    owner_id = ride["owner_id"]
    rider_id = booking.get("rider_id")
    if str(g.user["_id"]) == str(owner_id):
        if str(rated_id) != str(rider_id):
            raise APIError("You can only rate the rider on this booking.", 422, code="validation_error")
    elif str(g.user["_id"]) == str(rider_id):
        if str(rated_id) != str(owner_id):
            raise APIError("You can only rate the driver on this booking.", 422, code="validation_error")
    else:
        raise APIError("Only the rider and driver on this booking can rate it.", 403, code="forbidden")

    doc = {
        "booking_id": booking_id,
        "reviewer_id": g.user["_id"],
        "rated_user_id": rated_id,
        "rating": rating,
        "review": review or "",
        "created_at": utcnow(),
    }
    try:
        db.ratings.insert_one(doc)
    except DuplicateKeyError:
        raise APIError("You have already rated this trip.", 409, code="already_rated")

    _recompute(db, rated_id)
    return {
        "ok": True,
        "rating": {
            "id": str(doc["_id"]),
            "booking_id": str(booking_id),
            "rated_user_id": str(rated_id),
            "rating": rating,
            "review": review or "",
            "created_at": doc["created_at"].isoformat(),
        },
    }, 201


@bp.get("/user/<uid>")
@require_auth
@rate_limit("default")
def user_ratings(uid):
    db = get_db()
    target = to_object_id(uid, "user")
    rows = list(
        db.ratings.find({"rated_user_id": target})
        .sort("created_at", -1)
        .limit(50)
    )
    return {
        "ok": True,
        "data": [{
            "id": str(r["_id"]),
            "booking_id": str(r["booking_id"]),
            "reviewer_id": str(r["reviewer_id"]),
            "rating": r["rating"],
            "review": r.get("review", ""),
            "created_at": r["created_at"].isoformat(),
        } for r in rows],
    }