#!/bin/bash
# deploy.sh — One-shot setup for Factiva Exporter on Ubuntu 22.04
# Run as root: bash deploy.sh

set -e
echo "==> Updating system..."
apt-get update -q && apt-get upgrade -y -q

echo "==> Installing Python 3.11 + tools..."
apt-get install -y -q python3.12 python3.12-venv python3-pip git curl unzip

echo "==> Cloning repo..."
cd /opt
if [ -d factiva ]; then
  cd factiva && git pull
else
  git clone https://github.com/YOUR_GITHUB/factiva.git factiva || {
    echo "  (no git repo — copying files manually, see instructions)"
    mkdir -p /opt/factiva
  }
  cd factiva
fi

echo "==> Creating virtualenv..."
python3.12 -m venv venv
source venv/bin/activate

echo "==> Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q flask python-dotenv playwright anthropic tweepy striprtf gunicorn

echo "==> Installing Playwright browsers..."
playwright install chromium
playwright install-deps chromium

echo "==> Creating exports directory..."
mkdir -p /opt/factiva/exports

echo "==> Creating .env if missing..."
if [ ! -f /opt/factiva/.env ]; then
cat > /opt/factiva/.env << 'EOF'
FACTIVA_USER=
FACTIVA_PASS=

ANTHROPIC_API_KEY=

TWITTER_API_KEY=
TWITTER_API_SECRET=
TWITTER_ACCESS_TOKEN=
TWITTER_ACCESS_SECRET=
EOF
echo "  !! Fill in /opt/factiva/.env with your credentials !!"
fi

echo "==> Creating systemd service..."
cat > /etc/systemd/system/factiva.service << 'EOF'
[Unit]
Description=Factiva Exporter
After=network.target

[Service]
User=root
WorkingDirectory=/opt/factiva
Environment=PATH=/opt/factiva/venv/bin
ExecStart=/opt/factiva/venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 --timeout 600 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable factiva
systemctl restart factiva

echo ""
echo "======================================"
echo " Done! App running at http://$(curl -s ifconfig.me):5000"
echo " Edit credentials: nano /opt/factiva/.env"
echo " Restart after edit: systemctl restart factiva"
echo " Logs: journalctl -u factiva -f"
echo "======================================"
