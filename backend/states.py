"""Explicit state machines for rides, bookings, and payments.

All state transitions MUST be validated server-side here. The frontend can
never set a status directly — mutation endpoints only request a transition
(e.g. cancel), and this module decides whether it is legal.
"""

# ---------------------------------------------------------------- ride states
RIDE_DRAFT = "draft"
RIDE_PUBLISHED = "published"
RIDE_FULL = "full"
RIDE_DRIVER_EN_ROUTE = "driver_en_route"
RIDE_BOARDING = "boarding"
RIDE_IN_PROGRESS = "in_progress"
RIDE_COMPLETED = "completed"
RIDE_CANCELLED = "cancelled"
RIDE_EXPIRED = "expired"

# Legacy value "active" is treated as PUBLISHED for migration compatibility.
_LEGACY = {"active": RIDE_PUBLISHED}

RIDE_STATES = {
    RIDE_DRAFT,
    RIDE_PUBLISHED,
    RIDE_FULL,
    RIDE_DRIVER_EN_ROUTE,
    RIDE_BOARDING,
    RIDE_IN_PROGRESS,
    RIDE_COMPLETED,
    RIDE_CANCELLED,
    RIDE_EXPIRED,
}

# States in which customers may book a seat.
RIDE_BOOKABLE = {RIDE_PUBLISHED, RIDE_FULL, RIDE_DRIVER_EN_ROUTE, RIDE_BOARDING}

# States that should surface as "active/upcoming" to the existing UI.
RIDE_UPCOMING = {RIDE_PUBLISHED, RIDE_FULL, RIDE_DRIVER_EN_ROUTE, RIDE_BOARDING}

# States that consume vehicle time (used for overlap prevention).
RIDE_OCCUPIES_VEHICLE = {
    RIDE_PUBLISHED,
    RIDE_FULL,
    RIDE_DRIVER_EN_ROUTE,
    RIDE_BOARDING,
    RIDE_IN_PROGRESS,
    RIDE_DRAFT,
}

_RIDE_TRANSITIONS = {
    RIDE_DRAFT: {RIDE_PUBLISHED, RIDE_CANCELLED},
    RIDE_PUBLISHED: {RIDE_FULL, RIDE_CANCELLED, RIDE_EXPIRED, RIDE_DRIVER_EN_ROUTE, RIDE_PUBLISHED},
    RIDE_FULL: {RIDE_PUBLISHED, RIDE_CANCELLED, RIDE_DRIVER_EN_ROUTE, RIDE_IN_PROGRESS},
    RIDE_DRIVER_EN_ROUTE: {RIDE_BOARDING, RIDE_IN_PROGRESS, RIDE_CANCELLED},
    RIDE_BOARDING: {RIDE_IN_PROGRESS, RIDE_CANCELLED},
    RIDE_IN_PROGRESS: {RIDE_COMPLETED},
    RIDE_COMPLETED: set(),
    RIDE_CANCELLED: set(),
    RIDE_EXPIRED: set(),
}


def normalize_ride_status(status):
    """Map legacy/wire statuses into canonical ride states."""
    if status in RIDE_STATES:
        return status
    return _LEGACY.get(status, RIDE_PUBLISHED)


def can_transition_ride(source, target):
    return target in _RIDE_TRANSITIONS.get(normalize_ride_status(source), set())


# ------------------------------------------------------------- booking states
BOOKING_PENDING_PAYMENT = "pending_payment"
BOOKING_CONFIRMED = "confirmed"
BOOKING_CANCELLED = "cancelled"
BOOKING_REFUND_PENDING = "refund_pending"
BOOKING_REFUNDED = "refunded"
BOOKING_NO_SHOW = "no_show"
BOOKING_COMPLETED = "completed"
# Driver has finished the trip; the passenger has not confirmed it yet. The
# money is collected and ledgered but NOT payable -- see completion.py.
BOOKING_AWAITING_COMPLETION = "awaiting_completion"
# The passenger disputed the trip. Terminal for settlement purposes: earnings
# are held until an admin resolves it.
BOOKING_DISPUTED = "disputed"

BOOKING_STATES = {
    BOOKING_PENDING_PAYMENT,
    BOOKING_CONFIRMED,
    BOOKING_CANCELLED,
    BOOKING_REFUND_PENDING,
    BOOKING_REFUNDED,
    BOOKING_NO_SHOW,
    BOOKING_COMPLETED,
    BOOKING_AWAITING_COMPLETION,
    BOOKING_DISPUTED,
}

# Booking states that are terminal.
_BOOKING_TERMINAL = {BOOKING_REFUNDED, BOOKING_NO_SHOW, BOOKING_COMPLETED}

# Terminal for the *ride*, but still open for confirmation/dispute.
_BOOKING_SETTLED = {BOOKING_COMPLETED, BOOKING_DISPUTED}

_BOOKING_CONSUMES_SEATS = {BOOKING_PENDING_PAYMENT, BOOKING_CONFIRMED,
                           BOOKING_AWAITING_COMPLETION}

_BOOKING_TRANSITIONS = {
    BOOKING_PENDING_PAYMENT: {
        BOOKING_CONFIRMED,        # payment verified / demo settled
        BOOKING_CANCELLED,        # payment never completed (seats released)
    },
    BOOKING_CONFIRMED: {
        BOOKING_REFUND_PENDING,   # user/driver cancellation too close to departure
        BOOKING_CANCELLED,        # cancellation with full refund
        BOOKING_AWAITING_COMPLETION,  # driver finished the trip
        BOOKING_NO_SHOW,
    },
    # The confirmation window. A booking can only leave this state by being
    # confirmed (passenger or auto-complete) or disputed, or by being
    # cancelled/refunded if the admin resolves a dispute that way.
    BOOKING_AWAITING_COMPLETION: {
        BOOKING_COMPLETED,        # passenger confirmed, or auto-confirmed
        BOOKING_DISPUTED,         # passenger raised a dispute
        BOOKING_REFUND_PENDING,
    },
    BOOKING_DISPUTED: {
        BOOKING_COMPLETED,        # dispute resolved in the driver's favour
        BOOKING_REFUND_PENDING,   # dispute resolved with a refund
    },
    BOOKING_REFUND_PENDING: {BOOKING_REFUNDED, BOOKING_CANCELLED},
    BOOKING_CANCELLED: set(),
    BOOKING_REFUNDED: set(),
    BOOKING_NO_SHOW: set(),
    BOOKING_COMPLETED: set(),
}


def can_transition_booking(source, target):
    return target in _BOOKING_TRANSITIONS.get(source, set())


def is_booking_terminal(status):
    """No further transition is possible from here."""
    return status in _BOOKING_TERMINAL


def is_ride_over(status):
    """The trip itself is over, whether or not money is free to move.

    Distinct from `is_booking_terminal`: a disputed booking is over but still
    open to an admin decision, so treating it as terminal would make the
    resolution path unreachable.
    """
    return status in _BOOKING_SETTLED


def booking_blocks_settlement(status):
    """Earnings are held in this state and must not be paid out.

    The single place that answers "may this booking's money move?". Anything
    added here must also be representable in the completion state machine in
    `completion.py`, and payout creation must consult `completion.is_payable`.
    """
    return status in {BOOKING_AWAITING_COMPLETION, BOOKING_DISPUTED,
                      BOOKING_PENDING_PAYMENT, BOOKING_REFUND_PENDING}


def booking_consumes_seats(status):
    return status in _BOOKING_CONSUMES_SEATS


# -------------------------------------------------------------- payment states
PAYMENT_CREATED = "created"             # order created, not paid
PAYMENT_PENDING = "pending"             # gateway processing
PAYMENT_SUCCESS = "success"             # authorized/verified
PAYMENT_FAILED = "failed"
PAYMENT_CANCELLED = "cancelled"         # order expired/abandoned
PAYMENT_REFUNDED = "refunded"

PAYMENT_STATES = {
    PAYMENT_CREATED,
    PAYMENT_PENDING,
    PAYMENT_SUCCESS,
    PAYMENT_FAILED,
    PAYMENT_CANCELLED,
    PAYMENT_REFUNDED,
}

# ------------------------------------------------------------------- accounts
LEDGER_CREDIT = "credit"
LEDGER_DEBIT = "debit"  # reserved for future money-out; debits here are reversals

ACCOUNT_PASSENGER = "passenger"
ACCOUNT_PLATFORM = "platform"
ACCOUNT_DRIVER = "driver"

class BookingClosed(RuntimeError):
    """A transition was requested that the state machine rejects."""


def ride_transition_error(status):
    return f"The ride cannot change from '{status}' to the requested state."


def booking_transition_error(status):
    return f"The booking cannot change from '{status}' to the requested state."