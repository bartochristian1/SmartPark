"""
daraja.py
---------
Safaricom Daraja API integration - Lipa Na M-Pesa Online (STK Push).

This lets a driver pay their parking fee by receiving an "Enter M-Pesa PIN"
prompt on their phone, instead of cash at the barrier.

SETUP:
    1. Create an app at https://developer.safaricom.co.ke/ to get a sandbox
       Consumer Key / Consumer Secret.
    2. Set these environment variables before running the app:

        DARAJA_CONSUMER_KEY     - from the Daraja app
        DARAJA_CONSUMER_SECRET  - from the Daraja app
        DARAJA_SHORTCODE        - Paybill/Till number (sandbox default: 174379)
        DARAJA_PASSKEY          - Lipa Na M-Pesa passkey (sandbox one is public,
                                   see the Daraja docs "Lipa na M-Pesa Sandbox")
        DARAJA_CALLBACK_URL     - a PUBLICLY reachable HTTPS URL that points at
                                   this app's /mpesa/callback route (Safaricom
                                   cannot reach localhost - use ngrok or similar
                                   while testing, e.g.
                                   https://<your-ngrok-id>.ngrok-free.app/mpesa/callback)
        DARAJA_ENV              - "sandbox" (default) or "production"

    None of these are hard-coded here on purpose - never commit real
    consumer key/secret/passkey values to source control.

Flow used by this app (see app.py + algorithms.py):
    1. Driver looks up their fee on the checkout page and enters their phone.
    2. stk_push() is called -> Safaricom pushes a PIN prompt to that phone
       and returns a CheckoutRequestID immediately (this is NOT the result
       of the payment yet, just confirmation the prompt was sent).
    3. The frontend polls /checkout/status/<checkout_request_id>.
    4. Safaricom calls our /mpesa/callback route asynchronously once the
       driver enters their PIN (or cancels/times out); that route updates
       the session's payment_status and, on success, frees the slot.
"""

import os
import base64
from datetime import datetime

import requests

CONSUMER_KEY = os.environ.get("DARAJA_CONSUMER_KEY", "")
CONSUMER_SECRET = os.environ.get("DARAJA_CONSUMER_SECRET", "")
SHORTCODE = os.environ.get("DARAJA_SHORTCODE", "174379")
PASSKEY = os.environ.get("DARAJA_PASSKEY", "")
CALLBACK_URL = os.environ.get("DARAJA_CALLBACK_URL", "https://example.com/mpesa/callback")
ENV = os.environ.get("DARAJA_ENV", "sandbox")

BASE_URL = "https://sandbox.safaricom.co.ke" if ENV == "sandbox" else "https://api.safaricom.co.ke"


class DarajaError(Exception):
    """Raised when Daraja can't be reached or rejects a request (e.g. missing
    credentials, invalid phone number, sandbox outage)."""


def is_configured():
    """True once real credentials have been set via environment variables."""
    return bool(CONSUMER_KEY and CONSUMER_SECRET and PASSKEY)


def get_access_token():
    """OAuth token required by every other Daraja call. Short-lived (~1hr),
    so we simply fetch a fresh one per STK push rather than caching it."""
    if not CONSUMER_KEY or not CONSUMER_SECRET:
        raise DarajaError("Daraja consumer key/secret are not configured.")
    url = f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
    resp = requests.get(url, auth=(CONSUMER_KEY, CONSUMER_SECRET), timeout=15)
    if resp.status_code != 200:
        raise DarajaError(f"Daraja token request failed ({resp.status_code}): {resp.text}")
    return resp.json()["access_token"]


def _password_and_timestamp():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{SHORTCODE}{PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def normalize_phone(phone: str) -> str:
    """Converts common Kenyan phone formats (07xx, 01xx, +2547xx) into the
    2547XXXXXXXX / 2541XXXXXXXX format Daraja requires."""
    phone = "".join(phone.strip().split()).replace("+", "")
    if phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]
    elif phone.startswith("7") or phone.startswith("1"):
        phone = "254" + phone
    return phone


def stk_push(phone_number: str, amount: float, account_reference: str,
             transaction_desc: str = "Parking fee"):
    """
    Sends an STK ("Sim Toolkit") push -> the driver's phone shows an
    "Enter M-Pesa PIN" prompt for `amount`.

    Returns the parsed JSON from Daraja, which includes CheckoutRequestID -
    the id we track to later match the async callback back to this session.
    """
    phone_number = normalize_phone(phone_number)
    token = get_access_token()
    password, timestamp = _password_and_timestamp()

    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": max(1, int(amount)),  # Daraja rejects an amount of 0
        "PartyA": phone_number,
        "PartyB": SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc,
    }
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(
        f"{BASE_URL}/mpesa/stkpush/v1/processrequest",
        json=payload, headers=headers, timeout=15,
    )
    if resp.status_code != 200:
        raise DarajaError(f"STK push failed ({resp.status_code}): {resp.text}")
    return resp.json()


def parse_callback(payload: dict):
    """
    Pulls the bits we care about out of the raw JSON body Safaricom POSTs to
    /mpesa/callback. Returns a dict:
        {
            "checkout_request_id": str,
            "result_code": int,       # 0 == success
            "result_desc": str,
            "mpesa_receipt": str | None,
            "amount": float | None,
            "phone_number": str | None,
        }
    """
    stk = payload.get("Body", {}).get("stkCallback", {})
    result = {
        "checkout_request_id": stk.get("CheckoutRequestID"),
        "result_code": stk.get("ResultCode"),
        "result_desc": stk.get("ResultDesc"),
        "mpesa_receipt": None,
        "amount": None,
        "phone_number": None,
    }
    items = stk.get("CallbackMetadata", {}).get("Item", [])
    lookup = {item.get("Name"): item.get("Value") for item in items}
    result["mpesa_receipt"] = lookup.get("MpesaReceiptNumber")
    result["amount"] = lookup.get("Amount")
    result["phone_number"] = lookup.get("PhoneNumber")
    return result
