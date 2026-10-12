#!/bin/sh
set -eu

APP_DIR=/opt/telegram-gift-watch
SERVICE_FILE=/etc/systemd/system/gift-watch.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти установщик от root." >&2
  exit 1
fi
if [ ! -f "$APP_DIR/tonnel_watch.py" ] || [ ! -f "$APP_DIR/secrets.json" ]; then
  echo "В $APP_DIR должны находиться код проекта и secrets.json." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv ca-certificates
id giftwatch >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin giftwatch
chown -R giftwatch:giftwatch "$APP_DIR"
chmod 600 "$APP_DIR/secrets.json"

su -s /bin/sh giftwatch -c "python3 -m venv '$APP_DIR/.venv'"
su -s /bin/sh giftwatch -c "'$APP_DIR/.venv/bin/python' -m pip install --disable-pip-version-check -r '$APP_DIR/requirements.txt'"

install -m 0644 "$APP_DIR/deploy/gift-watch.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now gift-watch.service
systemctl --no-pager --full status gift-watch.service
