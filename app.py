"""
app.py
------
Web layer (Flask). Ties the database + algorithms modules together and
exposes them as a browser-based system, per the brief's "running as a
web-based system" requirement.

Routes:
    GET  /                    -> dashboard: visual slot display + entry form
    POST /entry                -> Vehicle Entry module (park a car)
    GET  /checkout              -> preview fee for a plate before paying
    POST /checkout/confirm      -> Vehicle Exit + Cash Payment + Barrier module
    POST /checkout/mpesa        -> Vehicle Exit + M-Pesa (Daraja STK Push)
    GET  /checkout/status/<id>  -> polled by the browser while waiting on M-Pesa
    POST /mpesa/callback        -> Safaricom calls this once the PIN is entered
    GET  /history               -> log of completed sessions (from the DB)
"""

from dotenv import load_dotenv
load_dotenv()  # must run before `daraja` is imported, since it reads os.environ at import time

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from database import init_db, get_conn
from algorithms import SlotManager

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-in-production"

# One SlotManager instance shared for the app's lifetime - it is the live
# in-memory brain of the car park, backed by SQLite (see algorithms.py).
init_db()
manager = SlotManager()


@app.route("/")
def dashboard():
    slots = manager.get_slot_display()
    return render_template(
        "index.html",
        slots=slots,
        available=manager.available_count(),
        total=len(slots),
    )


@app.route("/entry", methods=["POST"])
def entry():
    plate = request.form.get("plate_number", "")
    if not plate.strip():
        flash("Please enter a number plate.", "error")
        return redirect(url_for("dashboard"))

    ok, result = manager.park_vehicle(plate)
    if ok:
        flash(f"Vehicle {plate.strip().upper()} parked in slot {result}.", "success")
    else:
        flash(result, "error")
    return redirect(url_for("dashboard"))


@app.route("/checkout", methods=["GET"])
def checkout_preview():
    plate = request.args.get("plate_number", "")
    preview = manager.preview_fee(plate) if plate else None
    if plate and not preview:
        flash("No active session found for that plate.", "error")
    return render_template("checkout.html", preview=preview, plate=plate)


@app.route("/checkout/confirm", methods=["POST"])
def checkout_confirm():
    """Cash payment path: attendant confirms cash was received (or the fee
    is free) and the barrier opens immediately."""
    plate = request.form.get("plate_number", "")
    ok, result = manager.checkout_vehicle(plate)
    if not ok:
        flash(result, "error")
        return redirect(url_for("dashboard"))
    return render_template("receipt.html", receipt=result)


@app.route("/checkout/mpesa", methods=["POST"])
def checkout_mpesa():
    """M-Pesa path: kicks off a Daraja STK Push to the driver's phone."""
    plate = request.form.get("plate_number", "")
    phone = request.form.get("phone_number", "")
    if not phone.strip():
        flash("Please enter the phone number to charge.", "error")
        return redirect(url_for("checkout_preview", plate_number=plate))

    ok, result = manager.initiate_mpesa_payment(plate, phone)
    if not ok:
        flash(result, "error")
        return redirect(url_for("checkout_preview", plate_number=plate))

    # Fee was 0 -> already finalized and a receipt dict came back.
    if "barrier" in result:
        return render_template("receipt.html", receipt=result)

    return render_template("mpesa_wait.html", payment=result)


@app.route("/checkout/status/<checkout_request_id>")
def checkout_status(checkout_request_id):
    """Polled via JS by mpesa_wait.html while the driver enters their PIN."""
    return jsonify(manager.get_payment_status(checkout_request_id))


@app.route("/receipt/mpesa/<checkout_request_id>")
def mpesa_receipt(checkout_request_id):
    """Shown after mpesa_wait.html detects a successful payment."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.plate_number, sl.slot_number, s.entry_time, s.exit_time,
                   s.fee, s.mpesa_receipt
            FROM sessions s
            JOIN slots sl ON sl.id = s.slot_id
            WHERE s.checkout_request_id = ? AND s.status = 'completed'
            """,
            (checkout_request_id,),
        ).fetchone()
    if not row:
        flash("Receipt not found or payment still processing.", "error")
        return redirect(url_for("dashboard"))

    from datetime import datetime as _dt
    receipt = {
        "plate_number": row["plate_number"],
        "slot_number": row["slot_number"],
        "entry_time": _dt.fromisoformat(row["entry_time"]),
        "exit_time": _dt.fromisoformat(row["exit_time"]),
        "minutes": round((_dt.fromisoformat(row["exit_time"]) - _dt.fromisoformat(row["entry_time"])).total_seconds() / 60, 1),
        "fee": row["fee"],
        "mpesa_receipt": row["mpesa_receipt"],
        "barrier": "OPEN",
    }
    return render_template("receipt.html", receipt=receipt)


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    """
    Safaricom Daraja posts here once the STK push is resolved (paid,
    cancelled, or timed out). Must always return HTTP 200 with
    ResultCode 0 in the body, or Daraja will retry the callback.
    """
    payload = request.get_json(force=True, silent=True) or {}
    manager.handle_mpesa_callback(payload)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/history")
def history():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.plate_number, sl.slot_number, s.entry_time, s.exit_time,
                   s.fee, s.status, s.payment_status, s.mpesa_receipt
            FROM sessions s
            JOIN slots sl ON sl.id = s.slot_id
            ORDER BY s.id DESC
            LIMIT 100
            """
        ).fetchall()
    return render_template("history.html", rows=rows)


if __name__ == "__main__":
    # host=0.0.0.0 so it's reachable on a local network (e.g. a barrier
    # kiosk / display screen at the gate), debug=True for development only.
    app.run(host="0.0.0.0", port=5000, debug=True)
