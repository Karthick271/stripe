import os
import asyncio
import logging
from io import BytesIO

from flask import Flask, request, send_file, jsonify, abort
from dotenv import load_dotenv
import stripe
from playwright.async_api import async_playwright
import requests
from datetime import datetime


from flask import Flask, request, send_file, jsonify, abort, render_template, session, redirect

# replace this line:
# load_dotenv("/opt/python/.env")
# with:
import os
from dotenv import load_dotenv
load_dotenv(os.getenv("DOTENV_FILE", ".env"))

# set a secret key (needed for session):
import secrets
app = Flask(__name__)
# at top (config)
DISABLE_OAUTH_STATE = os.getenv("DISABLE_OAUTH_STATE", "true").lower() == "true"

# import zoho oauth helpers + constants
from zoho_oauth import (
    zoho_authorize_url,
    zoho_exchange_code_for_tokens,
    zoho_revoke_token,
    save_tokens_for_user,
    get_access_token_by_user,
    get_latest_row_for_user,
    get_zoho_api_base,
    # constants used by your template/routes:
    ZOHO_USER_ID,
    ZOHO_OAUTH_SCOPE,
    ZOHO_REDIRECT_URI,
    ZOHO_ACCOUNTS_URL,
    ZOHO_API_DOMAIN,
)
# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

import requests as _requests
import logging

# save original implementation
_orig_request = _requests.Session.request

def _logging_request(self, method, url, **kwargs):
    body_preview = None
    if "json" in kwargs:
        try:
            import json
            body_preview = json.dumps(kwargs["json"], ensure_ascii=False)
        except Exception:
            body_preview = str(kwargs["json"])
    elif "data" in kwargs:
        body_preview = str(kwargs["data"])
    elif "params" in kwargs:
        body_preview = f"params={kwargs['params']}"

    logging.info(f"[HTTP-REQ] {method.upper()} {url}")
    if body_preview:
        logging.info(f"[HTTP-REQ-BODY] {body_preview}")

    resp = _orig_request(self, method, url, **kwargs)

    text_preview = resp.text
    if len(text_preview) > 400:
        text_preview = text_preview[:400] + "…"

    logging.info(f"[HTTP-RESP] {resp.status_code} {url} -> {text_preview}")
    return resp

# 🔥 monkey-patch globally
_requests.Session.request = _logging_request
# Load environment
load_dotenv("/opt/python/.env")

app = Flask(__name__)

# Stripe config
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
WEBHOOK_LOGFILE = os.getenv("WEBHOOK_LOGFILE", "/var/log/stripe_webhook.log")

# Zoho config
ZOHO_ACCESS_TOKEN = os.getenv("ZOHO_ACCESS_TOKEN")  # generate & paste from Zoho
ZOHO_API_DOMAIN = os.getenv("ZOHO_API_DOMAIN", "www.zohoapis.com")


if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    logging.warning("STRIPE_SECRET_KEY not set — Stripe API calls will not work")

# ----------------------------
# HTML -> PDF ROUTE (unchanged)
# ----------------------------
async def generate_pdf_from_html(html_content: str) -> BytesIO:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html_content, wait_until="load")

        pdf_bytes = await page.pdf(format="A4", print_background=True)
        await browser.close()

        return BytesIO(pdf_bytes)

@app.route('/htmltopdf', methods=['POST'])
def convert_html_to_pdf():
    try:
        logging.info(f"Request method: {request.method}")
        logging.info(f"Content-Type: {request.content_type}")
        logging.info(f"Headers: {dict(request.headers)}")
        logging.info(f"Remote Address: {request.remote_addr}")
        html_content = request.json.get("content", "")

        pdf_file = asyncio.run(generate_pdf_from_html(html_content))
        logging.info("HTML to PDF converted successfully using Playwright!")

        return send_file(pdf_file, as_attachment=True, download_name="output.pdf", mimetype="application/pdf")

    except Exception as ex:
        logging.error(f"Error while converting HTML to PDF: {str(ex)}")
        return {"error": "Failed to generate PDF"}, 500

# ----------------------------
# STRIPE WEBHOOK ROUTE (fixed)
# ----------------------------


def zoho_headers():
    token = get_access_token_by_user(ZOHO_USER_ID)
    if not token:
        abort(401, description="Zoho not connected")
    return {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }


def create_contact(customer_details: dict) -> str:
    url = f"https://{ZOHO_API_DOMAIN}/crm/v2/Contacts"
    payload = {
        "data": [{
            "Last_Name": customer_details.get("name") or "Unknown",
            "Email": customer_details.get("email"),
            "Phone": customer_details.get("phone"),
        }]
    }
    resp = requests.post(url, json=payload, headers=zoho_headers())
    resp.raise_for_status()
    return resp.json()["data"][0]["details"]["id"]

def create_deal(contact_id: str, session_obj: dict) -> str:
    url = f"https://{ZOHO_API_DOMAIN}/crm/v2/Deals"
    payload = {
        "data": [{
            "Deal_Name": f"Payment {session_obj.get('id')}",
            "Amount": session_obj.get("amount_total", 0) / 100,
            "Closing_Date": datetime.utcnow().strftime("%Y-%m-%d"),
            "Contact_Name": {"id": contact_id}
        }]
    }
    resp = requests.post(url, json=payload, headers=zoho_headers())
    resp.raise_for_status()
    return resp.json()["data"][0]["details"]["id"]

def update_deal(deal_id: str, session_obj: dict, contact_id: str):
    url = f"https://{ZOHO_API_DOMAIN}/crm/v2/Deals/{deal_id}"
    payload = {
        "data": [{
            # ⚠️ Replace with your actual Zoho field API names
            "Contribution_Amount": session_obj.get("amount_total", 0) / 100,
            "Payment_Status": session_obj.get("payment_status"),
            "Payment_Method": ",".join(session_obj.get("payment_method_types", [])),
            "Donation_Name": session_obj.get("metadata", {}).get("donation_name", ""),
            "Sub_Pipeline_and_Stage": "Closed Won",  # replace with your stage field API name
            "Contact_Name": {"id": contact_id}
        }]
    }
    resp = requests.put(url, json=payload, headers=zoho_headers())
    resp.raise_for_status()
    return resp.json()



@app.before_request
def log_request_info():
    try:
        logging.info("===== Incoming Request =====")
        logging.info(f"Path: {request.path}")
        logging.info(f"Method: {request.method}")
        logging.info(f"Headers: {dict(request.headers)}")
        logging.info(f"Remote Addr: {request.remote_addr}")

        # Raw body
        raw_data = request.get_data(as_text=True)
        if raw_data:
            logging.info(f"Raw Body: {raw_data}")

        # Parsed JSON (if any)
        if request.is_json:
            logging.info(f"JSON: {request.get_json(silent=True)}")

        logging.info("===== End Request =====")
    except Exception as ex:
        logging.error(f"Failed to log request: {ex}")

VERIFY_SIGNATURE = os.getenv("STRIPE_VERIFY_SIGNATURE", "true").lower() == "true"


@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    # raw body bytes for signature verification
    payload_bytes = request.get_data()
    try:
        payload_text = payload_bytes.decode("utf-8")
    except Exception:
        payload_text = payload_bytes.decode("utf-8", errors="replace")

    sig_header = request.headers.get('Stripe-Signature')
    event = None

    if VERIFY_SIGNATURE:
        if not STRIPE_WEBHOOK_SECRET:
            logging.error("STRIPE_WEBHOOK_SECRET not configured — cannot verify signatures")
            return abort(500, description="webhook not configured")
        if not sig_header:
            logging.warning("Missing Stripe-Signature header")
            return abort(400)

        try:
            event = stripe.Webhook.construct_event(
                payload=payload_bytes,
                sig_header=sig_header,
                secret=STRIPE_WEBHOOK_SECRET
            )
        except Exception:
            logging.exception("Signature verification failed")
            return abort(400)
    else:
        logging.warning("⚠️ Skipping signature verification (DEBUG MODE)")
        try:
            event = request.get_json(force=True)
        except Exception:
            logging.exception("Failed to parse JSON payload without signature check")
            return abort(400)

    # get id/type safely
    if isinstance(event, dict):
        event_id = event.get("id", "no-id")
        event_type = event.get("type", "no-type")
    else:
        event_id = getattr(event, "id", "no-id")
        event_type = getattr(event, "type", "no-type")

    # Build and append full payload to logfile
    try:
        with open(WEBHOOK_LOGFILE, "a") as f:
            f.write("\n==== STRIPE WEBHOOK ====\n")
            f.write(f"Timestamp: {__import__('datetime').datetime.utcnow().isoformat()}Z\n")
            f.write(f"Remote: {request.remote_addr}\n")
            f.write(f"Event ID: {event_id}\n")
            f.write(f"Event Type: {event_type}\n")
            f.write(f"Headers: {dict(request.headers)}\n")
            f.write("Payload:\n")
            f.write(payload_text)
            f.write("\n==== END WEBHOOK ====\n\n")
    except Exception:
        logging.exception(f"Failed to write full webhook log: {WEBHOOK_LOGFILE}")

    logging.info(f"Received Stripe event: {event_id} {event_type}")
    logging.info(f"Payload logged to {WEBHOOK_LOGFILE}")
 # --- NEW ZOHO INTEGRATION ---
    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {}) or {}

        deal_id = metadata.get("deal_id")
        contact_id = metadata.get("contact_id")

        # If no contact → create
        if not contact_id:
            contact_id = create_contact(session.get("customer_details", {}))

        # If no deal → create
        if not deal_id:
            deal_id = create_deal(contact_id, session)

        # Always update the deal
        update_deal(deal_id, session, contact_id)

    return jsonify({"received": True}), 200

# ----------------------------
# HEALTH CHECK
# ----------------------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200

# Optional: default success/cancel URLs (use your front-end URLs)
DEFAULT_SUCCESS_URL = os.getenv("SUCCESS_URL", "https://example.com/success")
DEFAULT_CANCEL_URL = os.getenv("CANCEL_URL", "https://example.com/cancel")

def _validate_currency(curr: str) -> str:
    if not curr:
        return "usd"
    return curr.lower()

def _validate_amount_cents(amount) -> int:
    # Accept dollars (float/int) or cents (int) - normalize to cents
    if isinstance(amount, (int,)) and amount > 0:
        return int(amount)  # assume cents
    try:
        # if passed as float (dollars) or string "10.50"
        amt = float(amount)
        if amt <= 0:
            raise ValueError("amount must be > 0")
        return int(round(amt * 100))
    except Exception:
        raise ValueError("invalid amount; provide cents (int) or dollars (float)")
@app.route("/api/create_payment_link", methods=["POST"])
def create_payment_link():
    """
    Create a Stripe Payment Link.
    Request JSON:
    {
      "name": "Awesome Product",
      "amount": 9.99,        # dollars (float) or cents (int)
      "currency": "usd",     # optional (default usd)
      "metadata": {          # optional
        "order_id": "12345",
        "customer": "centilio"
      }
    }
    Response:
    { "url": "https://buy.stripe.com/..." }
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 500

    data = request.get_json(force=True, silent=True) or {}

    try:
        # --- basic input ---
        name = data.get("name", "Payment")
        currency = (data.get("currency") or "usd").lower()

        amount = data.get("amount")
        if amount is None:
            return jsonify({"error": "amount is required"}), 400

        # Normalize to cents
        if isinstance(amount, int):
            amount_cents = amount  # assume already in cents
        else:
            amount_cents = int(round(float(amount) * 100))

        # metadata (must be dict of strings)
        metadata = {}
        if isinstance(data.get("metadata"), dict):
            metadata = {str(k): str(v) for k, v in data["metadata"].items()}

        # --- call Stripe ---
        payment_link = stripe.PaymentLink.create(
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "product_data": {"name": name},
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            metadata=metadata
        )

        return jsonify({"url": payment_link.url}), 200

    except Exception as ex:
        logging.exception("Failed to create payment link")
        return jsonify({"error": str(ex)}), 500





# ------------------------------------------------------------------------------
# Zoho: minimal UI + routes
# ------------------------------------------------------------------------------
@app.route("/zoho", methods=["GET"])
def zoho_page():
    row = get_latest_row_for_user(ZOHO_USER_ID)
    connected = row is not None
    return render_template(
        "zoho.html",
        connected=connected,
        accounts_url=ZOHO_ACCOUNTS_URL,
        scope=ZOHO_OAUTH_SCOPE,
        redirect_uri=ZOHO_REDIRECT_URI
    )

# /auth
@app.route("/auth", methods=["GET"])
def auth():
    state = "nostate"
    return redirect(zoho_authorize_url(state))

# /callback
@app.route("/callback", methods=["GET"])
def callback():
    if request.args.get("error"):
        return abort(400, description=request.args["error"])
    code = request.args.get("code")
    if not code:
        return abort(400, description="missing code")
    # no state check in prototype
    token_res = zoho_exchange_code_for_tokens(code)
    if not token_res.get("access_token") or not token_res.get("refresh_token"):
        return abort(500, description="Token exchange failed")
    save_tokens_for_user(ZOHO_USER_ID, token_res)
    return redirect("/zoho")

@app.route("/disconnect", methods=["POST"])
def disconnect():
    row = get_latest_row_for_user(ZOHO_USER_ID)
    if row and row.get("Refresh_Token"):
        try:
            status, body = zoho_revoke_token(row["Refresh_Token"])
            logging.info("Zoho revoke status=%s body=%s", status, body)
        except Exception:
            logging.exception("Zoho revoke failed (continuing to clear local tokens)")
    clear_tokens_for_user(ZOHO_USER_ID)
    return redirect("/zoho")

@app.route("/zoho/me", methods=["GET"])
def zoho_me():
    token = get_access_token_by_user(ZOHO_USER_ID)
    if not token:
        return abort(401, description="Zoho not connected")

    base = get_zoho_api_base()
    url = f"https://accounts.zoho.eu/oauth/user/info"

    resp = requests.get(url, headers={"Authorization": f"Zoho-oauthtoken {token}"}, timeout=20)

    if resp.status_code >= 400:
        return abort(resp.status_code, resp.text)

    return jsonify(resp.json())

    
if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=8000)
