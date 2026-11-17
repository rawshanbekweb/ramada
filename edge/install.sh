#!/usr/bin/env bash
# Mini-PC (Ubuntu/Debian) ga edge agentni o'rnatish va systemd xizmati qilish.
#   sudo bash edge/install.sh
set -euo pipefail

DIR=/opt/shaffof-edge
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv ffmpeg >/dev/null

id -u shaffof >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$DIR" shaffof
mkdir -p "$DIR"
cp -r "$SRC/edge" "$DIR/"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q httpx numpy opencv-python-headless

if [ ! -f "$DIR/edge/config.toml" ]; then
  cp "$DIR/edge/config.example.toml" "$DIR/edge/config.toml"
  echo ">>> $DIR/edge/config.toml ni tahrirlang (server_url, edge_secret, kameralar), keyin:"
  echo ">>>   sudo systemctl restart shaffof-edge"
fi
chown -R shaffof "$DIR"
chmod 600 "$DIR/edge/config.toml"

install -m 644 "$SRC/edge/shaffof-edge.service" /etc/systemd/system/shaffof-edge.service
systemctl daemon-reload
systemctl enable --now shaffof-edge
systemctl --no-pager status shaffof-edge | head -5
echo "Loglar: journalctl -u shaffof-edge -f"
