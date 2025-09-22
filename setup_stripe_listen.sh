#!/usr/bin/env bash
set -euo pipefail

# === CONFIG ===
APP_DIR="/opt/python"
ENV_FILE="$APP_DIR/.env"
STRIPE_BIN="/usr/bin/stripe"
FORWARD_TO="http://127.0.0.1:8000/stripe/webhook"
EVENTS="checkout.session.completed,payment_intent.succeeded"
LISTEN_SCRIPT="$APP_DIR/run_stripe_listen.sh"
UNIT_FILE="/etc/systemd/system/stripe-listen.service"
LOGFILE="/var/log/stripe_listen.log"
USER="webhook"

echo ">>> Step 1: Ensure webhook user exists"
if ! id -u "$USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$USER"
  usermod -aG sudo "$USER"
fi

echo ">>> Step 2: Install Stripe CLI if missing"
if ! command -v stripe >/dev/null 2>&1; then
  curl -L https://github.com/stripe/stripe-cli/releases/latest/download/stripe_$(uname -s)_$(uname -m).deb -o /tmp/stripe-cli.deb
  apt install -y /tmp/stripe-cli.deb
fi

echo ">>> Step 3: Create wrapper script at $LISTEN_SCRIPT"
cat > "$LISTEN_SCRIPT" <<SH
#!/usr/bin/env bash
set -euo pipefail

ENVFILE="$ENV_FILE"
LOGFILE="$LOGFILE"
STRIPE_BIN="$STRIPE_BIN"
FORWARD_TO="$FORWARD_TO"
EVENTS="$EVENTS"

# load .env vars if present
if [ -f "\$ENVFILE" ]; then
  export \$(grep -v '^#' "\$ENVFILE" | xargs)
fi

exec "\$STRIPE_BIN" listen \\
  --api-key "\${STRIPE_SECRET_KEY:-}" \\
  --forward-to "\$FORWARD_TO" \\
  --events "\$EVENTS" 2>&1 | tee -a "\$LOGFILE"
SH

chmod +x "$LISTEN_SCRIPT"
chown "$USER:$USER" "$LISTEN_SCRIPT"

echo ">>> Step 4: Create systemd unit $UNIT_FILE"
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Stripe CLI listener (forwards events to local webhook)
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$LISTEN_SCRIPT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

echo ">>> Step 5: Reload systemd and start service"
systemctl daemon-reload
systemctl enable --now stripe-listen

echo ">>> Step 6: Tail logs for signing secret (Ctrl+C when done)"
sleep 2
journalctl -u stripe-listen -n 20 --no-pager
echo
echo "⚠️  Look for a line like: 'Your webhook signing secret is whsec_...' above."
echo "👉 Copy that whsec_... and put it into $ENV_FILE as STRIPE_WEBHOOK_SECRET"
