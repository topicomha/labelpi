# Pi setup (one time)

Everything after this is automatic: merge to `main` on GitHub and the Pi picks
it up within a minute.

## 1. Hardware and OS

- **Pi Zero W** (or Zero 2 W). The plain Pi Zero without "W" has no Bluetooth
  or Wi-Fi.
- Flash **Raspberry Pi OS Lite (32-bit)** with Raspberry Pi Imager (a 16 GB
  card is plenty). In the imager's settings:
  - hostname `labelpi`
  - enable SSH
  - user `labelpi` with a password of your choice
  - your Wi-Fi network and password
- Boot it and give it a fixed address (a DHCP reservation in the router), so
  the page's address never changes. It also answers as `labelpi.local`.

## 2. Install labelpi

SSH in (`ssh labelpi@labelpi.local`) and run:

```bash
sudo apt install -y git
git clone https://github.com/topicomha/labelpi.git ~/labelpi
~/labelpi/deploy/install.sh
```

The repo is public, so cloning needs no GitHub login. `install.sh` asks for
your password (sudo) and then, in order:

1. checks Python is 3.11 or newer;
2. installs the system packages (`git python3-venv python3-pil bluez`);
3. adds your user to the `bluetooth` group;
4. creates `.venv` and installs `requirements.txt`. Pillow, bleak and
   dbus-fast come prebuilt from piwheels; **this is the slow step on a Zero**,
   several minutes;
5. creates `config/printers.toml` from the example, if there isn't one yet;
6. installs `/etc/sudoers.d/labelpi`: the service user may run
   `systemctl restart labelpi` and nothing else. The file is checked with
   `visudo` before it's installed;
7. installs and starts the `labelpi` service and the `labelpi-deploy.timer`
   (auto-deploy).

It's safe to run again: every step checks what's already there.

## 3. Printer addresses

Either copy the config from a machine that already has it (from that machine):

```bash
scp config/printers.toml labelpi@labelpi.local:labelpi/config/
```

or edit it on the Pi: `nano ~/labelpi/config/printers.toml`. Then run
`sudo systemctl restart labelpi`. Real addresses never go in the repo; the file
is gitignored.

## 4. Pair the Brother

Pairing belongs to each Pi's Bluetooth adapter, so a new Pi has to pair the
Brother once, even if another Pi already did. Turn the printer on and **switch
your phone's Bluetooth off** (a connected phone hides the printers), then:

```bash
bluetoothctl
  agent on
  default-agent
  scan on              # wait for "PT-P300BT…" (and "D30"), note the MACs
  scan off
  pair  AA:BB:CC:DD:EE:FF    # Brother only
  trust AA:BB:CC:DD:EE:FF
  quit
```

The Phomemo D30 needs **no pairing**: it's reached over BLE by address. It
only advertises while it's awake and nothing else is connected to it, so
switch it off and on right before scanning if it doesn't show up.

Reboot once after the first install (`sudo reboot`), so the `bluetooth` group
membership applies to the service.

## 5. Check it

Open `http://labelpi.local:8080/` and print a test label from each printer.
To check calibration, `POST /api/print/ruler` prints a mm ruler (see SPEC §5).

## How it runs

| What | Where | Logs |
|---|---|---|
| The web service (waitress, port from `[server]`) | `labelpi.service`: starts at boot, restarts if it crashes | `journalctl -u labelpi -f` |
| Auto-deploy, every minute | `labelpi-deploy.timer` → `deploy/deploy.sh` | `journalctl -t labelpi-deploy -f` |

`deploy.sh` fetches `main`. If it moved, it fast-forwards, runs
`pip install` when `requirements.txt` changed, smoke-tests (the app must
import and load its config), and restarts the service. If anything fails it
goes back to the previous commit and skips that commit until a newer one
arrives. Run it by hand to see what it does: `~/labelpi/deploy/deploy.sh`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `503 unavailable` | Printer on? Awake (Brother: green light steady; press its button)? In range? `bluetoothctl info <MAC>` shows Paired/Trusted for the Brother? |
| D30 "prints" but nothing comes out | Label jammed: open the lid, reseat the roll, press the button to feed. The printer still reports "paper present". |
| D30 garbled / shifted label, or "didn't answer the status check" | A job was cut off mid-send; switch the D30 off and on. |
| D30 not found in scans | Phone still connected to it, or it's asleep: turn phone Bluetooth off, power-cycle the D30. |
| Permission denied on Bluetooth | `groups` includes `bluetooth`? Reboot after the first install. |
| Page doesn't load | `systemctl status labelpi`, `journalctl -u labelpi -n 50` |
| Deploy not happening | `systemctl list-timers labelpi-deploy.timer`; `journalctl -t labelpi-deploy`; run `~/labelpi/deploy/deploy.sh` by hand. A commit that failed its smoke test is skipped until a newer one arrives (`.deploy-failed-commit`). |
| Deploy says "can't fast-forward" | Someone edited files in `~/labelpi` on the Pi: `git status`, then `git stash` or `git checkout .` |
