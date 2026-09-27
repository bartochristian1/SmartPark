"""
algorithms.py
-------------
This is the "brain" of the system: the modules/algorithms the brief asks
you to design, plus the in-memory data structures that back them and the
reasons each one was chosen.

Modules implemented here:
    1. Slot Management        -> SlotManager class
    2. Vehicle Entry (arrival) -> SlotManager.park_vehicle()
    3. Fee Calculation         -> calculate_fee()
    4. Vehicle Exit / Payment  -> SlotManager.checkout_vehicle() (cash/free)
                                   SlotManager.initiate_mpesa_payment() +
                                   SlotManager.handle_mpesa_callback() (M-Pesa)
    5. Barrier Control         -> simulated by the return value of checkout

Data structures used, and WHY:
    - Array/List (self.slots): a fixed-order list of Slot objects mirrors
      the physical, numbered layout of the car park. Rendering the visual
      display (green/red grid) needs an ordered, indexable sequence -
      a list gives O(1) access by position and is the natural fit.

    - Queue / deque (self.available_queue): free slot numbers are held in
      a collections.deque. Allocating a slot on arrival = popleft() and
      releasing a slot on exit = append(), both O(1). A deque (rather than
      a plain list) avoids the O(n) cost of list.pop(0), and using a QUEUE
      (FIFO) instead of, say, always picking the lowest-numbered free slot
      spreads wear evenly across the physical bays - a fairer real-world
      policy for a client operating 24/7.

    - Hash map / dict (self.active_sessions): keyed by plate_number, this
      gives O(1) lookup when a car exits ("which slot is plate KAA123Z in,
      and when did it arrive?") instead of scanning every session - critical
      once the car park has been running for months and the sessions log
      is large.

    - Hash map / dict (self.pending_payments): keyed by Daraja's
      CheckoutRequestID, this lets the async M-Pesa callback (which only
      knows the CheckoutRequestID, not the plate number) find its way back
      to the right session in O(1) when Safaricom calls /mpesa/callback.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from database import get_conn
import daraja


@dataclass
class Slot:
    id: int
    slot_number: str
    status: str  # 'available' | 'occupied'
    plate_number: str = None  # who is parked here right now (if occupied)


# ---------------------------------------------------------------------------
# Module 3: Fee Calculation Algorithm
# ---------------------------------------------------------------------------
def calculate_fee(entry_time: datetime, exit_time: datetime) -> float:
    """
    Tiered fee algorithm, exactly as specified by the client:
        <= 30 minutes  : free        (Kshs. 0)
        <= 2 hours     : Kshs. 50
        <= 4 hours     : Kshs. 100
        <= 6 hours     : Kshs. 300
        > 6 hours      : Kshs. 500

    Using elif bands is a deliberate, simple, O(1) decision-tree rather
    than a per-minute rate: it matches the client's flat-tier pricing
    exactly and is trivial to audit/change if tiers are revised later.
    """
    duration_minutes = (exit_time - entry_time).total_seconds() / 60

    if duration_minutes <= 30:
        fee = 0
    elif duration_minutes <= 120:
        fee = 50
    elif duration_minutes <= 240:
        fee = 100
    elif duration_minutes <= 360:
        fee = 300
    else:
        fee = 500

    return fee, duration_minutes


# ---------------------------------------------------------------------------
# Modules 1, 2, 4: Slot Management / Entry / Exit / M-Pesa Payment
# ---------------------------------------------------------------------------
class SlotManager:
    """
    Keeps a fast in-memory view of the car park (array + queue + hash map)
    and writes every change straight through to SQLite so the two never
    drift apart and nothing is lost on restart.
    """

    def __init__(self):
        self.slots = []                 # ARRAY: ordered list of Slot objects
        self.available_queue = deque()  # QUEUE: free slot_numbers, FIFO
        self.active_sessions = {}       # HASH MAP: plate_number -> session dict
        self.pending_payments = {}      # HASH MAP: checkout_request_id -> plate_number
        self._load_from_db()

    # -- setup -------------------------------------------------------------
    def _load_from_db(self):
        """Rebuild the in-memory structures from the database on startup."""
        self.slots.clear()
        self.available_queue.clear()
        self.active_sessions.clear()
        self.pending_payments.clear()

        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM slots ORDER BY id").fetchall()
            for row in rows:
                slot = Slot(id=row["id"], slot_number=row["slot_number"], status=row["status"])
                self.slots.append(slot)
                if slot.status == "available":
                    self.available_queue.append(slot.slot_number)

            active = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active'"
            ).fetchall()
            for row in active:
                self.active_sessions[row["plate_number"]] = {
                    "session_id": row["id"],
                    "slot_id": row["slot_id"],
                    "entry_time": row["entry_time"],
                    "checkout_request_id": row["checkout_request_id"],
                    "payment_status": row["payment_status"],
                }
                if row["checkout_request_id"]:
                    self.pending_payments[row["checkout_request_id"]] = row["plate_number"]
                for slot in self.slots:
                    if slot.id == row["slot_id"]:
                        slot.plate_number = row["plate_number"]

    # -- Module 1: read-only view for the visual display --------------------
    def get_slot_display(self):
        """Returns the ordered slot list for rendering the green/red grid."""
        return self.slots

    def available_count(self):
        return len(self.available_queue)

    # -- Module 2: Vehicle Entry (arrival) -----------------------------------
    def park_vehicle(self, plate_number: str):
        """
        Algorithm:
          1. Reject if this plate is already parked (no double entry).
          2. Reject if no slot is free (queue empty -> car park full).
          3. Pop a free slot number from the FRONT of the queue - O(1).
          4. Record entry_time = now, insert a new session row.
          5. Mark the slot occupied in both memory and DB.
        """
        plate_number = plate_number.strip().upper()

        if plate_number in self.active_sessions:
            return False, "This vehicle is already parked inside."

        if not self.available_queue:
            return False, "Car park is full. No slots available."

        slot_number = self.available_queue.popleft()
        slot = next(s for s in self.slots if s.slot_number == slot_number)
        entry_time = datetime.now()

        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (plate_number, slot_id, entry_time, status) "
                "VALUES (?, ?, ?, 'active')",
                (plate_number, slot.id, entry_time.isoformat()),
            )
            session_id = cur.lastrowid
            conn.execute("UPDATE slots SET status='occupied' WHERE id=?", (slot.id,))

        slot.status = "occupied"
        slot.plate_number = plate_number
        self.active_sessions[plate_number] = {
            "session_id": session_id,
            "slot_id": slot.id,
            "entry_time": entry_time.isoformat(),
            "checkout_request_id": None,
            "payment_status": None,
        }
        return True, slot.slot_number

    # -- preview fee without ending the session (for the checkout screen) --
    def preview_fee(self, plate_number: str):
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return None
        entry_time = datetime.fromisoformat(session["entry_time"])
        fee, minutes = calculate_fee(entry_time, datetime.now())
        return {
            "plate_number": plate_number,
            "slot_id": session["slot_id"],
            "entry_time": entry_time,
            "minutes": round(minutes, 1),
            "fee": fee,
        }

    # -- Module 4a: Vehicle Exit + Cash Payment + Barrier --------------------
    def checkout_vehicle(self, plate_number: str):
        """
        Cash / free-exit path (fee == 0, or an attendant confirming a cash
        payment manually).
        Algorithm:
          1. Look up the active session by plate - O(1) via hash map.
          2. exit_time = now; fee = calculate_fee(entry_time, exit_time).
          3. Close the session and free the slot straight away.
          4. Push the freed slot_number to the BACK of the queue - O(1) -
             and simulate the barrier opening.
        """
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return False, "No active session found for this plate."

        entry_time = datetime.fromisoformat(session["entry_time"])
        exit_time = datetime.now()
        fee, minutes = calculate_fee(entry_time, exit_time)

        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET exit_time=?, fee=?, status='completed', "
                "payment_status=? WHERE id=?",
                (exit_time.isoformat(), fee, "paid" if fee == 0 else "cash",
                 session["session_id"]),
            )
            conn.execute(
                "UPDATE slots SET status='available' WHERE id=?", (session["slot_id"],)
            )

        return True, self._finalize_slot(session, plate_number, entry_time, exit_time,
                                          fee, minutes)

    # -- Module 4b: Vehicle Exit + M-Pesa (Daraja) Payment -------------------
    def initiate_mpesa_payment(self, plate_number: str, phone_number: str):
        """
        Starts the Daraja STK Push flow for a session's fee.
          1. Look up the active session and compute the current fee.
          2. If fee == 0, there's nothing to pay - finalize immediately.
          3. Otherwise ask Daraja to push a PIN prompt to `phone_number`,
             store the returned CheckoutRequestID against the session so
             the later async callback can find it again in O(1), and mark
             payment_status='pending'.
        Returns (ok: bool, result: dict | str) where result is either an
        error message or a dict with the fee + checkout_request_id.
        """
        plate_number = plate_number.strip().upper()
        session = self.active_sessions.get(plate_number)
        if not session:
            return False, "No active session found for this plate."

        entry_time = datetime.fromisoformat(session["entry_time"])
        fee, minutes = calculate_fee(entry_time, datetime.now())

        if fee == 0:
            ok, receipt = self.checkout_vehicle(plate_number)
            return ok, receipt

        if not daraja.is_configured():
            return False, ("M-Pesa is not configured yet. Set DARAJA_CONSUMER_KEY, "
                            "DARAJA_CONSUMER_SECRET and DARAJA_PASSKEY, or use cash payment.")

        try:
            response = daraja.stk_push(
                phone_number=phone_number,
                amount=fee,
                account_reference=plate_number,
                transaction_desc=f"Parking fee - {plate_number}",
            )
        except daraja.DarajaError as exc:
            return False, str(exc)

        checkout_request_id = response.get("CheckoutRequestID")
        if not checkout_request_id:
            return False, response.get("errorMessage", "Daraja did not return a CheckoutRequestID.")

        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET phone_number=?, checkout_request_id=?, "
                "payment_status='pending', fee=? WHERE id=?",
                (daraja.normalize_phone(phone_number), checkout_request_id, fee,
                 session["session_id"]),
            )

        session["checkout_request_id"] = checkout_request_id
        session["payment_status"] = "pending"
        self.pending_payments[checkout_request_id] = plate_number

        return True, {
            "plate_number": plate_number,
            "fee": fee,
            "minutes": round(minutes, 1),
            "checkout_request_id": checkout_request_id,
            "customer_message": response.get("CustomerMessage",
                                               "Check your phone and enter your M-Pesa PIN."),
        }

    def get_payment_status(self, checkout_request_id: str):
        """Polled by the frontend while waiting for the Daraja callback."""
        plate_number = self.pending_payments.get(checkout_request_id)
        if not plate_number:
            # Might already have completed and been cleaned up - check the DB.
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT status, payment_status FROM sessions WHERE checkout_request_id=?",
                    (checkout_request_id,),
                ).fetchone()
            if not row:
                return {"status": "unknown"}
            return {"status": "paid" if row["status"] == "completed" else row["payment_status"]}

        session = self.active_sessions.get(plate_number)
        return {"status": session["payment_status"] if session else "unknown"}

    def handle_mpesa_callback(self, payload: dict):
        """
        Called by app.py's /mpesa/callback route with the raw JSON Safaricom
        POSTs once the driver has entered their PIN (or cancelled/timed out).
          - ResultCode 0  -> payment succeeded: finalize checkout, open barrier.
          - ResultCode != 0 -> payment failed/cancelled: mark 'failed' so the
            driver can retry or pay cash; the slot stays occupied.
        """
        info = daraja.parse_callback(payload)
        checkout_request_id = info["checkout_request_id"]
        plate_number = self.pending_payments.get(checkout_request_id)
        if not plate_number:
            return False, "Unknown CheckoutRequestID (already processed or unrecognized)."

        session = self.active_sessions.get(plate_number)
        if not session:
            return False, "Session no longer active."

        if info["result_code"] == 0:
            entry_time = datetime.fromisoformat(session["entry_time"])
            exit_time = datetime.now()
            fee, minutes = calculate_fee(entry_time, exit_time)

            with get_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET exit_time=?, fee=?, status='completed', "
                    "payment_status='paid', mpesa_receipt=? WHERE id=?",
                    (exit_time.isoformat(), fee, info["mpesa_receipt"], session["session_id"]),
                )
                conn.execute(
                    "UPDATE slots SET status='available' WHERE id=?", (session["slot_id"],)
                )

            self._finalize_slot(session, plate_number, entry_time, exit_time, fee, minutes)
            del self.pending_payments[checkout_request_id]
            return True, "Payment confirmed, barrier opened."
        else:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET payment_status='failed' WHERE id=?",
                    (session["session_id"],),
                )
            session["payment_status"] = "failed"
            del self.pending_payments[checkout_request_id]
            return True, f"Payment not completed: {info['result_desc']}"

    # -- shared cleanup: free the slot + return a receipt dict ---------------
    def _finalize_slot(self, session, plate_number, entry_time, exit_time, fee, minutes):
        slot = next(s for s in self.slots if s.id == session["slot_id"])
        slot.status = "available"
        slot.plate_number = None
        self.available_queue.append(slot.slot_number)
        self.active_sessions.pop(plate_number, None)

        return {
            "plate_number": plate_number,
            "slot_number": slot.slot_number,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "minutes": round(minutes, 1),
            "fee": fee,
            "barrier": "OPEN",
        }
