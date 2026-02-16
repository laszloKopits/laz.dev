#!/bin/bash
set -e
cd /home/laszlokopits/laz.dev
git pull origin main
sudo systemctl restart laz-api
sudo systemctl reload caddy
echo "deployed."
