# Pi setup (one time)

Everything after this is automatic: push to `main` on GitHub, the Pi picks it
up within a minute.

## 1. Hardware and OS

- **Pi Zero W** (or Zero 2 W). The plain Pi Zero without "W" has no Bluetooth.
- Flash **Raspberry Pi OS Lite (32-bit)** with Raspberry Pi Imager. In the
  imager settings: hostname `labelpi`, enable SSH, create user `labelpi`,
  set Wi-Fi.
- Consider a DHCP reservation in pfSense so the address never changes.

## 2. Packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pil bluez
python3 --version          # must be 3.11 or newer
sudo usermod -aG bluetooth labelpi
```

`python3-pil` pulls in Pillow's native libraries; pip will still install
Pillow into the venv from piwheels (prebuilt for ARMv6) — no compiling.

## 3. Pair the printers

Turn each printer on, **switch your phone's Bluetooth off** (a connected phone
hides the printers from scans), then:

```bash
bluetoothctl
  agent on
  default-agent
  scan on              # wait for "PT-P300BT…" and "D30" to appear, note both MACs
  scan off
  pair  AA:BB:CC:DD:EE:FF    # Brother only
  trust AA:BB:CC:DD:EE:FF
  quit
```

The Phomemo D30 needs **no pairing** — it's reached over BLE by address. It
only advertises while nothing else is connected and while it's awake, so
switch it off and on right before scanning if it doesn't show up.

Put both MACs into `config/printers.toml` (step 5). Milestone 7 will move this
into the web UI.

## 4. Repo access

The repo is public, so the Pi clones it over HTTPS with **no credentials at
all**. The Pi can only pull, never push. Nothing private lives in the repo:
your MAC addresses stay in the gitignored `config/printers.toml` on the Pi.

## 5. Install the app

```bash
cd ~
git clone https://github.com/topicomha/labelpi.git labelpi
cd labelpi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/printers.example.toml config/printers.toml
nano config/printers.toml       # set MAC addresses
.venv/bin/python run.py         # quick manual test, Ctrl+C to stop
```

Open `http://labelpi.lan:8080` and print a test label from each printer.

## 6. Run it as a service

```bash
sudo cp deploy/labelpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now labelpi
journalctl -u labelpi -f        # watch the logs
```

## 7. Auto-deploy

Let the deploy script restart the service without a password:
```bash
sudo cp deploy/labelpi.sudoers /etc/sudoers.d/labelpi
sudo chmod 440 /etc/sudoers.d/labelpi
sudo visudo -c                  # must say "parsed OK"
```

Add the cron job (`crontab -e` as `labelpi`):
```
* * * * * /home/labelpi/labelpi/deploy/deploy.sh
```

Watch deploys: `journalctl -t labelpi-deploy -f`

## Troubleshooting

| Symptom | Check |
|---|---|
| `503 unavailable` | Printer on? In range? `bluetoothctl info <MAC>` shows Paired/Trusted? |
| Works once, then fails | Printer went to sleep mid-connection — backend should reconnect per job |
| D30 "prints" but nothing comes out | Label jammed — open the lid, reseat the roll, press the button to feed. The printer still reports "paper present" |
| D30 not found in scans | Phone still connected to it, or it's asleep — phone Bluetooth off, power-cycle the D30 |
| Permission denied on Bluetooth | `groups labelpi` includes `bluetooth`? Log out/in after `usermod` |
| Deploy not happening | `journalctl -t labelpi-deploy`; run `deploy/deploy.sh` by hand |
