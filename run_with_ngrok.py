#!/usr/bin/env python3
"""
run_with_ngrok.py
------------------
One-command launcher for local M-Pesa testing:

    python3 run_with_ngrok.py

What it does:
    1. Starts ngrok pointed at port 5000 (installs the pyngrok wrapper if
       needed - you still need ngrok itself, see below).
    2. Grabs the public https:// URL ngrok gives it.
    3. Writes that URL into .env as DARAJA_CALLBACK_URL, so Safaricom
       can actually reach your /mpesa/callback route.
    4. Starts the Flask app (app.py) with the updated environment.

One-time setup before running this:
    1. Sign up (free) at https://dashboard.ngrok.com/signup
    2. Get your authtoken from https://dashboard.ngrok.com/get-started/your-authtoken
    3. pip install pyngrok --break-system-packages   (or plain pip install)
    4. Run once:  python3 -c "from pyngrok import ngrok; ngrok.set_auth_token('YOUR_TOKEN_HERE')"

Then every time you want to test M-Pesa locally, just run this script
instead of `python3 app.py` directly.
"""

import os
import re
import sys

DOTENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def update_env_callback_url(new_url: str):
    """Rewrites the DARAJA_CALLBACK_URL line in .env in place."""
    callback = f"{new_url}/mpesa/callback"
    if not os.path.exists(DOTENV_PATH):
        print(f"ERROR: {DOTENV_PATH} not found. Create it first (see README).")
        sys.exit(1)

    with open(DOTENV_PATH, "r") as f:
        content = f.read()

    if "DARAJA_CALLBACK_URL=" in content:
        content = re.sub(
            r"DARAJA_CALLBACK_URL=.*",
            f"DARAJA_CALLBACK_URL={callback}",
            content,
        )
    else:
        content += f"\nDARAJA_CALLBACK_URL={callback}\n"

    with open(DOTENV_PATH, "w") as f:
        f.write(content)

    print(f"Updated .env -> DARAJA_CALLBACK_URL={callback}")
    return callback


def main():
    try:
        from pyngrok import ngrok
    except ImportError:
        print(
            "pyngrok is not installed.\n"
            "Run:  pip install pyngrok --break-system-packages\n"
            "Then set your authtoken (one-time):\n"
            "  python3 -c \"from pyngrok import ngrok; "
            "ngrok.set_auth_token('YOUR_TOKEN_HERE')\"\n"
        )
        sys.exit(1)

    print("Starting ngrok tunnel on port 5000...")
    tunnel = ngrok.connect(5000, bind_tls=True)
    public_url = tunnel.public_url
    print(f"ngrok tunnel live at: {public_url}")

    update_env_callback_url(public_url)

    print("\n" + "=" * 60)
    print("Starting SmartPark (Flask)...")
    print(f"Local:    http://localhost:5000")
    print(f"Public:   {public_url}")
    print(f"Callback: {public_url}/mpesa/callback")
    print("=" * 60 + "\n")

    # Import app AFTER writing the .env update, and re-load dotenv so this
    # process also has the fresh DARAJA_CALLBACK_URL before daraja.py reads it.
    from dotenv import load_dotenv
    load_dotenv(DOTENV_PATH, override=True)

    from app import app
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        ngrok.disconnect(public_url)
        ngrok.kill()


if __name__ == "__main__":
    main()
