#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/python"
ENV_FILE="$APP_DIR/.env"
USER="webhook"
LISTEN_SCRIPT="$APP_DIR/run_stripe_listen.sh"
UNIT_FILE="/etc/systemd/system/stripe-listen.service"
LOGFILE="/var/log/stripe_listen.log"
FORWARD_TO="http://127.0.0.1:8000/stripe/webhook"
EVENTS="checkout.session.completed,payment_intent.succeeded"
STRIPE_TGZ="stripe_1.30.0_linux_x86_64.tar.gz"
STRIPE_URL="https://github.com/stripe/stripe-cli/releases/download/v1.30.0/$STRIPE_TGZ"

echo ">>> Step 1: Install Stripe CLI binary"
cd /tmp
wget -q "$STRIPE_URL" -O "$STRIPE_TGZ"
tar -xzf "$STRIPE_TGZ"
# the archive contains a "stripe" binary
mv stripe /usr/local/bin/stripe
chmod +x /usr/local/bin/stripe

echo ">>> Stripe CLI version:"
stripe version || { echo "Stripe CLI install failed"; exit 1; }

echo ">>> Step 2: Ensure webhook user exists"
if ! id -u "$USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$USER"
  usermod -aG sudo "$USER"
fi

echo ">>> Step 3: Create wrapper script"
cat > "$LISTEN_SCRIPT" <<SH
#!/usr/bin/env bash
set -euo pipefail

ENVFILE="$ENV_FILE"
LOGFILE="$LOGFILE"
FORWARD_TO="$FORWARD_TO"
EVENTS="$EVENTS"

# load .env if present
if [ -f "\$ENVFILE" ]; then
  export \$(grep -v '^#' "\$ENVFILE" | xargs)
fi

exec stripe listen --api-key "\${STRIPE_SECRET_KEY:-}" \\
  --forward-to "\$FORWARD_TO" \\
  --events "\$EVENTS" 2>&1 | tee -a "\$LOGFILE"
SH

chmod +x "$LISTEN_SCRIPT"
chown "$USER:$USER" "$LISTEN_SCRIPT"

echo ">>> Step 4: Create systemd unit"
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Stripe CLI listener (tar.gz install)
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

[Install]
WantedBy=multi-user.target
UNIT

echo ">>> Step 5: Start service"
systemctl daemon-reload
systemctl enable --now stripe-listen
systemctl status stripe-listen --no-pager -l

echo
echo ">>> Step 6: Check logs for your webhook signing secret (whsec_...)"
echo "Run: journalctl -u stripe-listen -f"
