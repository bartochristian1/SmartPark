# SmartPark — Modern Parking System

A functional, web-based parking management prototype built for the
"modern parking system" brief: drivers see live slot availability,
vehicles are recorded on arrival, and on exit the system calculates
duration + fee automatically before the barrier opens. Drivers can pay
by M-Pesa (Safaricom Daraja STK Push) or cash. The car park currently
operates **50 slots** (configurable via `TOTAL_SLOTS` in `database.py`).

Stack: **Python 3 + Flask** (web layer) + **SQLite** (dynamic database) + **Safaricom Daraja API** (M-Pesa STK Push).
Chosen because it needs no external DB server to install for a class
submission, while still being a real relational database with proper
tables, keys and growth — not a flat file.

---

## 1. Use cases identified

| Actor            | Use case                                              |
|-------------------|--------------------------------------------------------|
| Driver             | View available slots before entering                  |
| Gate attendant/system | Record vehicle arrival, assign a slot                |
| Driver / attendant | Request exit, view fee owed                            |
| System              | Calculate fee based on duration                        |
| Driver / attendant | Confirm payment                                          |
| Barrier            | Open on successful payment                              |
| Manager            | View history/log of all sessions                        |

## 2. Modules proposed

1. **Slot Management Module** — tracks every slot's state (available /
   occupied) and drives the visual display.
2. **Vehicle Entry Module** — validates and records an arriving vehicle,
   allocates it a slot.
3. **Fee Calculation Module** — pure function that turns
   (entry_time, exit_time) into a fee using the client's tiers.
4. **Vehicle Exit & Payment Module** — looks up the active session,
   shows the fee, and finalises the session on payment (cash, or once
   Daraja confirms an M-Pesa payment).
5. **Barrier Control Module** — simulated: opens (returns `"OPEN"`)
   only after payment is confirmed. On real hardware this would send a
   signal to a relay/controller instead of returning a string.
6. **M-Pesa Payment Module (`daraja.py`)** — integrates Safaricom's
   Daraja API: sends an STK Push ("Enter M-Pesa PIN") to the driver's
   phone, and an async callback route finalises the session and opens
   the barrier the moment Safaricom confirms the payment.

All of these live in `algorithms.py` (the algorithms) and `app.py`
(the web routes that expose them), with clear docstrings explaining
each step.

## 3. Algorithms (see `algorithms.py` for full comments)

- **Slot allocation**: pop a free slot number from the front of a
  FIFO queue — O(1) — rather than scanning every slot for the first
  free one (O(n)). Freed slots are pushed to the back of the queue,
  spreading wear evenly.
- **Fee calculation**: tiered decision logic matching the client's
  exact bands (free ≤30 min, Kshs 50 ≤2h, Kshs 100 ≤4h, Kshs 300 ≤6h,
  Kshs 500 beyond) — O(1).
- **Session lookup on exit**: hash map keyed by plate number gives
  O(1) lookup instead of scanning the sessions log.

## 4. Data structures and why

| Structure          | Used for                          | Why this one |
|---------------------|-------------------------------------|----------------|
| `list` (array)       | Ordered slots for the visual grid  | Matches the physical, numbered layout; O(1) indexed access for rendering |
| `collections.deque` (queue) | Available slot numbers      | O(1) allocate (`popleft`) and release (`append`); FIFO fairness across bays |
| `dict` (hash map)     | Active sessions keyed by plate     | O(1) lookup on exit instead of O(n) scan of the sessions log |
| SQLite tables         | Durable state + full history       | Survives restarts; supports reporting/history; relational integrity via foreign key |

## 5. Dynamic database design

```
slots
-----
id            INTEGER PRIMARY KEY
slot_number   TEXT UNIQUE     -- e.g. "S01"
status        TEXT            -- 'available' | 'occupied'

sessions
--------
id            INTEGER PRIMARY KEY
plate_number  TEXT
slot_id       INTEGER  -> FK slots.id
entry_time    TEXT (ISO datetime)
exit_time     TEXT (ISO datetime, NULL while active)
fee           REAL (NULL until paid)
status        TEXT            -- 'active' | 'completed'
phone_number  TEXT            -- driver's M-Pesa number, if paid via M-Pesa
checkout_request_id TEXT      -- Daraja's STK push tracking id
payment_status TEXT           -- 'pending' | 'paid' | 'failed' | 'cash' | NULL (free)
mpesa_receipt TEXT            -- Safaricom's M-Pesa receipt number, once paid
```

Why it's "dynamic": `sessions` is append-only and grows with every
car that passes through (this is your history/audit trail for free).
`slots` can be scaled up at any time by changing `TOTAL_SLOTS` in
`database.py` and calling `ensure_slot_count()` — existing data is
never touched, only new rows are added.

## 6. M-Pesa (Daraja) setup

M-Pesa payment is optional — if it isn't configured, drivers simply pay
cash and an attendant clicks "Confirm Cash Payment". To enable real
STK Push:

1. Register at [developer.safaricom.co.ke](https://developer.safaricom.co.ke/)
   and create an app to get a sandbox **Consumer Key** and **Consumer
   Secret**.
2. Expose your local Flask app on a public HTTPS URL so Safaricom can
   reach `/mpesa/callback` (e.g. `ngrok http 5000` while developing).
3. Set these environment variables before starting the app:

```bash
export DARAJA_CONSUMER_KEY="your-consumer-key"
export DARAJA_CONSUMER_SECRET="your-consumer-secret"
export DARAJA_SHORTCODE="174379"                 # sandbox default
export DARAJA_PASSKEY="your-lipa-na-mpesa-passkey"
export DARAJA_CALLBACK_URL="https://<your-ngrok-id>.ngrok-free.app/mpesa/callback"
export DARAJA_ENV="sandbox"                       # or "production" when live
```

Never commit real keys/secrets to source control — they're read from
the environment only (see `daraja.py`). A `.env` file is already
included with your sandbox Consumer Key/Secret filled in and is
gitignored, so it stays local.

### Easiest way to test: `run_with_ngrok.py`

Safaricom needs a public HTTPS URL to reach `/mpesa/callback` — it can
never reach `localhost`. Instead of setting up ngrok by hand every
time, use the included launcher:

```bash
pip install -r requirements.txt

# one-time: sign up free at https://dashboard.ngrok.com/signup, then
# grab your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken
python3 -c "from pyngrok import ngrok; ngrok.set_auth_token('YOUR_TOKEN_HERE')"

# every time you want to test M-Pesa:
python3 run_with_ngrok.py
```

This starts an ngrok tunnel, automatically writes the live public URL
into `.env` as `DARAJA_CALLBACK_URL`, and starts the Flask app — all in
one command. Watch the terminal for the printed public URL.

To test a real STK Push, use Safaricom's official sandbox test number
**254708374149** on the checkout page — it always "succeeds" without a
real phone receiving anything.

## 7. How to run it

```bash
cd parking_system
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 app.py
```

Then open **http://localhost:5000** in your browser. This is enough
for cash-only testing (entry, fee calculation, dashboard, history).
For M-Pesa testing, use `python3 run_with_ngrok.py` instead — see
section 6 above.

- The dashboard shows the live slot grid (green = available, red =
  occupied) and a form to record an arrival.
- "Exit / Pay" looks up a plate, shows the fee owed, and lets the
  driver either **Pay with M-Pesa** (STK Push to their phone) or an
  attendant can **Confirm Cash Payment** — either way the barrier
  opens and a receipt is shown.
- "History" lists the last 100 sessions, including how each one was
  paid (cash / M-Pesa receipt number).

The database file `parking.db` is created automatically on first run
in the project folder — nothing else to configure.

## 8. Pushing to GitHub

```bash
git init
git add .
git commit -m "Modern Parking System prototype"
git branch -M main
git remote add origin <your-repo-url>
git push -u origin main
```

(`.gitignore` already excludes `parking.db`, `venv/` and `__pycache__/`.)

## 9. What to extend next

- **Authentication** for attendants/managers before they can view history.
- **Number-plate recognition (ANPR)** camera integration instead of
  manual plate entry, feeding straight into `park_vehicle()`.
- **Reserved/VIP slots**: add a `slot_type` column and a second queue
  so reserved bays aren't handed out by the general FIFO.
- **Real barrier hardware**: replace the `"barrier": "OPEN"` string in
  `checkout_vehicle()` with a GPIO/serial signal to an actual relay.
- **M-Pesa production go-live**: swap `DARAJA_ENV` to `production` and
  use your live Shortcode/Passkey once Safaricom approves the app.
- **Multi-level/multi-branch support**: add a `level` or `branch_id`
  column to `slots` and partition the queue per level.
