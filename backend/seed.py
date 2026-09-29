"""Demo data seeder. Run from backend/:  python -m backend.seed [--reset]

Post-hardening schema: canonical ride statuses, timezone-correct departure_at
UTC instants, verification flags on users, and consistent payment + ledger
records so the admin console and driver balances look real.
"""

import sys
from datetime import date, timedelta, time

from .app import create_app
from .db import get_db, utcnow
from .security import hash_password
from .timeutil import combine_local
from .ledger import record_payment_ledger
from . import db as dbmodule

PASSWORD = "Password123"
TIMEZONE = "Asia/Kolkata"

LOC = {
    "Marathahalli": (12.9569, 77.7011),
    "Whitefield": (12.9698, 77.7500),
    "Koramangala": (12.9352, 77.6245),
    "Electronic City": (12.8452, 77.6602),
    "HSR Layout": (12.9121, 77.6446),
    "Majestic": (12.9752, 77.6040),
    "Indiranagar": (12.9719, 77.6412),
    "Hebbal": (13.0358, 77.5970),
    "Silk Board": (12.9171, 77.6239),
    "Bellandur": (12.9283, 77.6756),
    "MG Road": (12.9757, 77.6058),
    "Jayanagar": (12.9250, 77.5838),
    "KR Puram": (13.0117, 77.6950),
}


def label(short):
    parts = {
        "Marathahalli": "Marathahalli, Bengaluru",
        "Whitefield": "Whitefield, Bengaluru",
        "Koramangala": "Koramangala, Bengaluru",
        "Electronic City": "Electronic City, Bengaluru",
        "HSR Layout": "HSR Layout, Bengaluru",
        "Majestic": "Majestic (Kempegowda Bus Stand), Bengaluru",
        "Indiranagar": "Indiranagar 100ft Road, Bengaluru",
        "Hebbal": "Manyata Tech Park Hebbal, Bengaluru",
        "Silk Board": "Silk Board Junction, Bengaluru",
        "Bellandur": "Bellandur, Bengaluru",
        "MG Road": "MG Road Metro, Bengaluru",
        "Jayanagar": "Jayanagar 4th Block, Bengaluru",
        "KR Puram": "KR Puram, Bengaluru",
    }
    return parts[short]


def point(short, lat, lng):
    return {"label": label(short), "address": label(short), "lat": lat, "lng": lng}


def local_departure(days, hour, minute):
    """Wall-clock departure (tomorrow+offset) in Asia/Kolkata -> naive UTC."""
    local_date = date.today() + timedelta(days=days + 1)
    return combine_local(local_date, time(hour, minute), TIMEZONE)


def distance_km(loc_o, loc_d):
    o, d = LOC[loc_o], LOC[loc_d]
    return round(((o[0] - d[0]) ** 2 + (o[1] - d[1]) ** 2) ** 0.5 * 111.0, 1)


DEMO_USERS = [
    {"name": "Rahul Sharma", "email": "rahul@ridemate.app", "age": 29, "gender": "male",
     "phone": "+919845001234", "bio": "Software engineer at a startup. Prefer morning rides.",
     "rating": 4.8, "total_rides": 42},
    {"name": "Priya Nair", "email": "priya@ridemate.app", "age": 32, "gender": "female",
     "phone": "+919845005678", "bio": "Design lead. Full-fare negotiable, AC on ideal.",
     "rating": 4.9, "total_rides": 61},
    {"name": "Arjun Mehta", "email": "arjun@ridemate.app", "age": 27, "gender": "male",
     "phone": "+919845009012", "bio": "Two-wheeler commuter. Quick rides to the airport.",
     "rating": 4.6, "total_rides": 18},
    {"name": "Sneha Iyer", "email": "sneha@ridemate.app", "age": 30, "gender": "female",
     "phone": "+919845003456", "bio": "Product manager. Weekend trips to Cubbon Park.",
     "rating": 4.7, "total_rides": 33},
    {"name": "Vikram Singh", "email": "vikram@ridemate.app", "age": 35, "gender": "male",
     "phone": "+919845007890", "bio": "Sales director. Comfortable SUV, ample luggage space.",
     "rating": 5.0, "total_rides": 74},
    {"name": "Divya Rao", "email": "divya@ridemate.app", "age": 26, "gender": "female",
     "phone": "+919845001111", "bio": "Junior analyst. Evening commutes to Electronic City.",
     "rating": 4.5, "total_rides": 12},
]

DEMO_RIDERS = [
    {"name": "Kiran Naik", "email": "kiran@ridemate.app", "age": 28, "gender": "male",
     "phone": "+919845002222", "bio": "Demo rider account.", "rating": 4.4, "total_rides": 9},
    {"name": "Meera Krishnan", "email": "meera@ridemate.app", "age": 24, "gender": "female",
     "phone": "+919845003333", "bio": "Demo rider account.", "rating": 4.7, "total_rides": 5},
]

# driver index -> list of (origin_short, dest_short, day_offset, hour, minute, fare, seats)
RIDE_PLANS = {
    0: [("Marathahalli", "Whitefield", 1, 8, 0, 90, 3),
        ("Koramangala", "Marathahalli", 2, 18, 30, 80, 3),
        ("HSR Layout", "Bellandur", 4, 9, 15, 70, 3)],
    1: [("Indiranagar", "Hebbal", 1, 7, 45, 120, 4),
        ("MG Road", "Jayanagar", 3, 17, 40, 110, 4),
        ("Majestic", "Silk Board", 6, 8, 30, 100, 4)],
    2: [("Whitefield", "Bellandur", 2, 9, 0, 60, 1),
        ("Indiranagar", "Marathahalli", 5, 19, 0, 65, 1)],
    3: [("Silk Board", "Electronic City", 1, 8, 45, 85, 3),
        ("Jayanagar", "Whitefield", 3, 7, 30, 130, 3),
        ("Koramangala", "Electronic City", 6, 18, 0, 95, 3)],
    4: [("Hebbal", "Majestic", 2, 8, 15, 140, 5),
        ("Bellandur", "KR Puram", 4, 20, 0, 120, 5)],
    5: [("Electronic City", "Marathahalli", 2, 19, 30, 110, 3),
        ("HSR Layout", "Koramangala", 5, 9, 30, 75, 3)],
}


def build_users(db):
    created = {}
    for spec in DEMO_USERS + DEMO_RIDERS:
        now = utcnow()
        doc = {
            "name": spec["name"],
            "email": spec["email"],
            "password_hash": hash_password(PASSWORD),
            "age": spec["age"],
            "phone": spec["phone"],
            "gender": spec["gender"],
            "bio": spec["bio"] or "",
            "photo_url": "",
            "auth_provider": "local",
            "role": "user",
            "rating": spec.get("rating", 0),
            "total_rides": spec.get("total_rides", 0),
            "token_version": 0,
            "email_verified": True,
            "phone_verified": True,
            "driver_verified": spec in DEMO_USERS,
            "licence_verified": spec in DEMO_USERS,
            "insurance_verified": spec in DEMO_USERS,
            "created_at": now - timedelta(days=30),
            "updated_at": now,
            "last_login_at": now,
            "last_logout_at": None,
        }
        db.users.insert_one(doc)
        created[spec["email"]] = doc
    return created


def build_vehicles(db, users):
    specs_base = [
        {"type": "4-wheeler", "number": "KA01AB1234", "model": "Honda City",
         "dl": "KA0519930123456", "insurance": "IN-2024-88312", "seats": 4, "color": "Pearl White"},
        {"type": "4-wheeler", "number": "KA03CD5678", "model": "Hyundai Creta",
         "dl": "KA0319940098765", "insurance": "IN-2024-11945", "seats": 5, "color": "Coral Blue"},
        {"type": "2-wheeler", "number": "KA05EF9012", "model": "Royal Enfield Classic",
         "dl": "KA0519920045678", "insurance": "IN-2025-22110", "seats": 1, "color": "Gunmetal Grey"},
        {"type": "4-wheeler", "number": "KA02GH3456", "model": "Maruti Suzuki Baleno",
         "dl": "KA0219950065432", "insurance": "IN-2024-99034", "seats": 4, "color": "Nexa Blue"},
        {"type": "4-wheeler", "number": "KA06KL2345", "model": "Toyota Innova Crysta",
         "dl": "KA0619910076543", "insurance": "IN-2025-33217", "seats": 6, "color": "Silver"},
        {"type": "4-wheeler", "number": "KA07MN6789", "model": "Tata Nexon EV",
         "dl": "KA0719960011223", "insurance": "IN-2025-78102", "seats": 4, "color": "Empire Blue"},
    ]
    vehicles = []
    for i, spec in enumerate(specs_base):
        driver_email = DEMO_USERS[i]["email"]
        now = utcnow()
        doc = {
            "user_id": users[driver_email]["_id"],
            "vehicle_type": spec["type"],
            "vehicle_number": spec["number"],
            "vehicle_model": spec["model"],
            "dl_number": spec["dl"],
            "insurance_number": spec["insurance"],
            "seat_count": spec["seats"],
            "color": spec["color"],
            "notes": "",
            "home_location": None,
            "dl_document": None,
            "insurance_document": None,
            "verification_status": "verified",
            "created_at": now - timedelta(days=20),
            "updated_at": now,
        }
        db.vehicles.insert_one(doc)
        vehicles.append(doc)
    return vehicles


def build_rides(db, users, vehicles):
    rides = []
    for driver_idx, plans in RIDE_PLANS.items():
        owner = users[DEMO_USERS[driver_idx]["email"]]
        vehicle = vehicles[driver_idx]
        for o, d, days, hour, minute, fare, seats in plans:
            dep = local_departure(days, hour, minute)
            origin = point(o, *LOC[o])
            destination = point(d, *LOC[d])
            now = utcnow()
            ride = {
                "owner_id": owner["_id"],
                "vehicle_id": vehicle["_id"],
                "vehicle": {"type": vehicle["vehicle_type"],
                            "model": vehicle["vehicle_model"],
                            "number": vehicle["vehicle_number"],
                            "color": vehicle["color"] or "",
                            "seat_count": vehicle["seat_count"]},
                "origin": origin,
                "origin_location": {"type": "Point", "coordinates": [origin["lng"], origin["lat"]]},
                "destination": destination,
                "destination_location": {"type": "Point", "coordinates": [destination["lng"], destination["lat"]]},
                "departure_date": dep.date().isoformat(),
                "departure_time": f"{hour:02d}:{minute:02d}",
                "timezone": TIMEZONE,
                "departure_at": dep,
                "duration_minutes": 60,
                "seats_total": seats,
                "seats_available": seats,
                "fare_per_seat": fare,
                "distance_km": distance_km(o, d),
                "notes": "",
                "status": "published",
                "earnings": 0,
                "created_at": now - timedelta(hours=4),
                "updated_at": now,
            }
            db.rides.insert_one(ride)
            rides.append(ride)
    return rides


def build_bookings(db, users, rides):
    kiran = users["kiran@ridemate.app"]
    meera = users["meera@ridemate.app"]
    plans = [
        (kiran, rides[0], 2),   # Marathahalli -> Whitefield
        (kiran, rides[4], 1),   # Indiranagar -> Hebbal
        (meera, rides[0], 1),
        (meera, rides[9], 2),   # Jayanagar -> Whitefield
    ]
    now = utcnow()
    for idx, (rider, ride, seats) in enumerate(plans):
        amount = ride["fare_per_seat"] * seats
        db.rides.update_one({"_id": ride["_id"]},
                            {"$inc": {"seats_available": -seats, "earnings": amount}})
        booking = {
            "ride_id": ride["_id"],
            "rider_id": rider["_id"],
            "owner_id": ride["owner_id"],
            "seats": seats,
            "amount": amount,
            "fare_per_seat": ride["fare_per_seat"],
            "status": "confirmed",
            "payment": {"provider": "demo", "reference": f"SEED{idx:03d}",
                        "order_id": f"seed_order_{idx:03d}",
                        "amount": amount, "status": "success",
                        "paid_at": now - timedelta(hours=3)},
            "ride_snapshot": {"origin": ride["origin"], "destination": ride["destination"],
                              "departure_date": ride["departure_date"],
                              "departure_time": ride["departure_time"],
                              "timezone": TIMEZONE,
                              "departure_at": ride["departure_at"], "vehicle": ride["vehicle"]},
            "refundable": True,
            "refund_amount": None,
            "refunded": False,
            "details": None,
            "created_at": now - timedelta(hours=3),
            "updated_at": now - timedelta(hours=3),
            "cancelled_at": None,
        }
        booking_result = db.bookings.insert_one(booking)
        booking["_id"] = booking_result.inserted_id
        payment = {
            "booking_id": booking["_id"],
            "order_id": f"seed_order_{idx:03d}",
            "provider": "demo",
            "provider_reference": f"SEED{idx:03d}",
            "reference": f"SEED{idx:03d}",
            "amount": amount,
            "currency": "INR",
            "status": "success",
            "paid_at": now - timedelta(hours=3),
            "created_at": now - timedelta(hours=3),
            "updated_at": now - timedelta(hours=3),
        }
        pid = db.payments.insert_one(payment)
        payment["_id"] = pid.inserted_id
        record_payment_ledger(booking, payment)


def main():
    reset = "--reset" in sys.argv
    app = create_app()
    db = get_db()

    if reset:
        for name in ("users", "vehicles", "rides", "bookings", "payments",
                     "refunds", "ledger_entries", "payouts", "refresh_tokens",
                     "notifications", "notification_preferences", "locations",
                     "audit_logs"):
            db[name].delete_many({})
        print("[seed] cleared all collections")

    if db.users.count_documents({}):
        print("[seed] data already present. Use --reset to rebuild.")
        return

    print("[seed] creating demo users, vehicles, rides, bookings, payments ...")
    users = build_users(db)
    vehicles = build_vehicles(db, users)
    rides = build_rides(db, users, vehicles)
    build_bookings(db, users, rides)

    print(f"[seed] done: {len(users)} users, {len(vehicles)} vehicles, "
          f"{len(rides)} rides, 4 bookings, 4 payments, 12 ledger entries")
    print()
    print("Demo credentials (all accounts):")
    print(f"  password : {PASSWORD}")
    for spec in DEMO_USERS + DEMO_RIDERS:
        print(f"  login   : {spec['email']}")
    print(f"  timezone: {TIMEZONE}")
    print()


if __name__ == "__main__":
    main()