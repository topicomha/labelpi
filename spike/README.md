# Milestone 0 — hardware spike (done)

Throwaway scripts that proved both printers work from a Pi. The findings are in
`docs/SPEC.md` §10. Keep this folder as a working reference until the real
backends (Milestones 4–5) replace it, then delete it.

| Script | Printer | How it talks | Result |
|---|---|---|---|
| `spike_brother.py` | Brother PT-P300BT | Ircama's code (imported from a clone, **unlicensed**) over a stdlib RFCOMM socket | prints; `--calibrate` measured the 64 px band |
| `spike_phomemo.py` | Phomemo D30 | own ESC/POS encoder, BLE via `bleak` (or Classic) | prints over BLE; `--calibrate` prints a mm ruler |

## Re-running on a Pi

```bash
# from the repo root on your PC (forward slashes, even on Windows)
scp -r spike <user>@<pi>:~/

# on the Pi
sudo apt install -y git python3-venv bluez fonts-dejavu-core
cd ~/spike
git clone https://github.com/Ircama/PT-P300BT.git libs/PT-P300BT
git -C libs/PT-P300BT checkout 96d3cf44fd952eba940cc0dc669115d95389d05d
python3 -m venv .venv
.venv/bin/pip install pillow pyserial packbits bleak
```

Pair the Brother first (`docs/PI_SETUP.md` step 3). The D30 needs no pairing.

```bash
# Brother
.venv/bin/python spike_brother.py --dry-run
.venv/bin/python spike_brother.py --address AA:BB:CC:DD:EE:FF
.venv/bin/python spike_brother.py --address AA:BB:CC:DD:EE:FF --calibrate
.venv/bin/python spike_brother.py --address AA:BB:CC:DD:EE:FF --repeat 2   # reconnect check

# Phomemo D30
.venv/bin/python spike_phomemo.py --scan --info-only                   # find it by BLE name, query only
.venv/bin/python spike_phomemo.py --address 11:22:33:44:55:66 --transport ble
.venv/bin/python spike_phomemo.py --address 11:22:33:44:55:66 --transport ble --calibrate
.venv/bin/python spike_phomemo.py --address 11:22:33:44:55:66 --transport classic --classic-chunk 128
```

Each run ends with a `=== SPIKE REPORT ===` block.

## Things that caught us out

- **The "D30" is a Phomemo**, not a Niimbot. Check the manual or brand before
  picking a library.
- **The D30 disappears from scans** while a phone is connected to it or when it
  has gone to sleep. Phone Bluetooth off, power-cycle the printer.
- **A jammed D30 label swallows jobs silently.** The printer still reports
  "paper present" and sends no error. If nothing comes out, reseat the roll.
- **Classic RFCOMM to the D30 connects but a one-burst job didn't print.**
  BLE works. Paced Classic (`--classic-chunk 128`) was sent once, but nobody
  checked whether that label printed.
- **Linux's `TIOCOUTQ` on a Bluetooth socket returns free buffer space**, not
  queued bytes. Don't use it to decide whether data was sent.
- **Windows paths:** `spike\file.py` breaks in Git Bash (`\` is an escape). Use
  forward slashes.

## Licences

- Ircama/PT-P300BT: **no licence** — imported from a clone here, never copied.
  Must be resolved before Milestone 4 (SPEC §10).
- Phomemo protocol bytes are facts taken from polskafan/phomemo_d30 (MIT) and
  odensc/phomemo-d30-web-bluetooth (Apache-2.0); no code copied.
  daehyeok/d30-printer (AGPL) was read only.
