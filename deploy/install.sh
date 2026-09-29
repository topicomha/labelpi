#!/usr/bin/env bash
# One-time setup of labelpi on a Raspberry Pi (Zero W). Run it from the
# clone, as the user the service should run as (not root):
#
#     sudo apt install -y git
#     git clone https://github.com/topicomha/labelpi.git ~/labelpi
#     ~/labelpi/deploy/install.sh
#
# It asks for your password (sudo) for the system parts. Safe to run again:
# every step checks what's already there. After it, updates are automatic -
# merge to main on GitHub and the Pi follows within a minute.
set -euo pipefail

DIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=$(id -un)
cd "$DIR"

step() { printf '\n==> %s\n' "$*"; }

if [ "$USER_NAME" = "root" ]; then
    echo "Run this as your normal user (it uses sudo where needed), not as root." >&2
    exit 1
fi

step "Checking Python (3.11 or newer needed)"
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || {
    echo "Python $(python3 --version) is too old; use Raspberry Pi OS Bookworm or newer." >&2
    exit 1
}
python3 --version

step "Installing system packages"
# python3-pil brings Pillow's native libraries; pip then installs Pillow
# itself from piwheels (prebuilt for the Pi, no compiling).
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pil bluez

step "Letting $USER_NAME use Bluetooth"
sudo usermod -aG bluetooth "$USER_NAME"

step "Creating the Python environment (.venv) and installing requirements"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r requirements.txt

step "Printer config"
if [ -f config/printers.toml ]; then
    echo "config/printers.toml already exists - keeping it."
else
    cp config/printers.example.toml config/printers.toml
    echo "Created config/printers.toml from the example."
    echo "!! Put your printers' Bluetooth addresses in it: nano $DIR/config/printers.toml"
fi

# Copy a file from deploy/, filling in @USER@ and @DIR@.
install_template() { # source, destination, mode
    local tmp
    tmp=$(mktemp)
    sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" "deploy/$1" >"$tmp"
    sudo install -m "$3" -o root -g root "$tmp" "$2"
    rm -f "$tmp"
}

step "Allowing auto-deploy to restart the service (sudoers)"
tmp=$(mktemp)
sed -e "s|@USER@|$USER_NAME|g" deploy/labelpi.sudoers >"$tmp"
sudo visudo -cq -f "$tmp" # never install a sudoers file that doesn't parse
sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/labelpi
rm -f "$tmp"

step "Installing the services"
install_template labelpi.service /etc/systemd/system/labelpi.service 644
install_template labelpi-deploy.service /etc/systemd/system/labelpi-deploy.service 644
install_template labelpi-deploy.timer /etc/systemd/system/labelpi-deploy.timer 644
sudo systemctl daemon-reload
sudo systemctl enable --now labelpi-deploy.timer
sudo systemctl enable labelpi
sudo systemctl restart labelpi

step "Done"
sleep 3
port=$(.venv/bin/python -c "from labelpi.config import load_config; print(load_config('config/printers.toml').server.port)" 2>/dev/null || echo 8080)
if systemctl is-active --quiet labelpi; then
    echo "labelpi is running:  http://$(hostname).local:$port/  (or http://$(hostname -I | awk '{print $1}'):$port/)"
else
    echo "labelpi didn't start - see: journalctl -u labelpi -n 50"
fi
cat <<EOF

Next:
  - Printer addresses: nano $DIR/config/printers.toml, then: sudo systemctl restart labelpi
  - Pair the Brother once (the D30 needs no pairing) - see docs/PI_SETUP.md
  - Logs:        journalctl -u labelpi -f
  - Deploy logs: journalctl -t labelpi-deploy -f
  - Bluetooth group membership takes effect after a reboot: sudo reboot
EOF
