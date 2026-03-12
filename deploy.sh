#!/bin/bash
set -e
cd /home/laszlokopits/laz.dev
git pull origin main

# Set up escape-the-sandbox venv if needed
GAME_DIR=games/escape-the-sandbox
if [ ! -d "$GAME_DIR/.venv" ]; then
  echo "setting up escape-the-sandbox venv..."
  python3 -m venv "$GAME_DIR/.venv"
  "$GAME_DIR/.venv/bin/pip" install -r "$GAME_DIR/requirements.txt"
fi

# Install/update service if needed
if [ ! -f /etc/systemd/system/escape-the-sandbox.service ]; then
  echo "installing escape-the-sandbox service..."
  sudo cp "$GAME_DIR/escape-the-sandbox.service" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable escape-the-sandbox
fi

sudo cp Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart laz-api
sudo systemctl restart escape-the-sandbox
sudo systemctl reload caddy
echo "deployed."
