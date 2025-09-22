#!/usr/bin/env bash
set -euo pipefail

ENVFILE="/opt/python/.env"
LOGFILE="/var/log/stripe_listen.log"
FORWARD_TO="http://127.0.0.1:8000/stripe/webhook"
EVENTS="checkout.session.completed,payment_intent.succeeded"

# load .env if present
if [ -f "$ENVFILE" ]; then
  export $(grep -v '^#' "$ENVFILE" | xargs)
fi

exec stripe listen --api-key "${STRIPE_SECRET_KEY:-}" \
  --forward-to "$FORWARD_TO" \
  --events "$EVENTS" 2>&1 | tee -a "$LOGFILE"
