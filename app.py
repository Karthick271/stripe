import os
import asyncio
import logging
from io import BytesIO

from flask import Flask, request, send_file, jsonify, abort
from dotenv import load_dotenv
import stripe
from playwright.async_api import async_playwright

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Load environment
load_dotenv("/opt/python/.env")

app = Flask(__name__)

# Stripe config
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
WEBHOOK_LOGFILE = os.getenv("WEBHOOK_LOGFILE", "/var/log/stripe_webhook.log")

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


if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=8000)
