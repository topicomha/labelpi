# Status and handoff

Where labelpi stands, what has been checked on real hardware, and what's next.
Written so a new Claude Code session (or a human) can pick up without the
previous conversation. Update it at the end of each working session.

_Last updated: 2026-09-28._

## Milestones

| # | What | State |
|---|---|---|
| 0 | Hardware spike | done — findings in SPEC §10 |
| 1 | Config, rendering, mock printer, tests | done |
| 2 | REST API (Flask + waitress) | done |
| 3 | Web UI | done |
| 4 | Brother PT-P300BT backend (own encoder) | done — **real prints verified** |
| 8 | Templates: fill-in fields, date maths, editor on the page | done |
| 5 | Phomemo D30 backend (BLE) | merged — **printed physical label not yet checked** (see below) |
| 9 | Label sizes managed from the page | next |
| 7 | Printer setup from the page: discovery, pairing, calibration | after 9 |
| 6 | Deploy to the Pi Zero W: systemd, auto-deploy, Pi setup verified | last |

Agreed build order: 8 → 9 → 5 → 7 → 6. Milestone 5 was pulled forward at the
owner's request. Every milestone is its own branch + PR (see CLAUDE.md,
"Working agreements").

## Verified on real hardware

Tested on a desktop "lab" Pi (Raspberry Pi OS trixie, 64-bit, Python 3.13,
BlueZ 5.82) that both printers are paired/known to — **not** yet on the Pi
Zero W.

- **Brother PT-P300BT** — printed through the API and the web page with the
  real backend. Connect ~4 s, send ~0.4 s, print ~8 s for a 63 mm label; the
  printer confirms completion. 12 mm tape band = 64 px (rows 32–95),
  measured with the calibration strip. Raster output is bit-identical to the
  spike encoder that printed correctly.
- **Phomemo D30** — the Milestone 5 backend connected over BLE (~2.7 s),
  got paper/cover status, and the printer acknowledged all 3,857 bytes of a
  Freezer-template job. **Nobody has looked at the resulting label yet.** The
  spike's BLE print (same bytes) was readable, but started ~12 mm into the
  label and ran off its far end.

## Open questions — resolve first

1. **Check the D30 label from the Milestone 5 test**: readable? right way
   round? where on the label? Measure one label's real length (gap to gap).
   Then set `length_mm` / `offset_mm` for it in `config/printers.toml`
   (`offset_mm` shifts the print along the label; + = later) and correct
   `config/printers.example.toml` (12 × 40 is unconfirmed — may be 12 × 50).
   Use `spike/spike_phomemo.py --calibrate` to print a mm ruler if needed.
2. Three-line templates (e.g. Freezer) are small on 12 mm Brother tape (all
   lines share ~9 mm). Owner may want one-line, tape-friendly variants.
3. Brother 9 mm / 6 mm bands (48 / 32 px) are unverified guesses.
4. Along the tape the Brother prints 0.149 mm per line (~170 dpi) while
   config says 180 dpi, so tape labels come out ~5 % longer than
   `length_mm` asks for. Minor; fix if it matters.

## Next: Milestone 9 — label sizes from the page

Add / edit / delete label sizes per printer in the UI, saved in
`config/settings.json` (same store and rules as templates: seeded from
`printers.toml`, atomic writes). Validate like `config.py` does (continuous:
`tape_width_mm` + `print_height_px`; fixed: `width_mm` × `length_mm`,
optional `offset_mm`). Keep the registry/printers reading labels from one
place so the API and backends see edits without a restart.

## Then: Milestone 7 — setup from the page (SPEC §11)

Discovery (BLE scan via bleak; Classic via BlueZ D-Bus), pairing + trust for
the Brother (the D30 needs none), storing addresses in settings.json, and a
calibration flow (print a ruler, user reads it off, store `offset_mm` /
`print_height_px`). Decide explicitly whether LAN-without-login is OK for
pairing devices.

## Last: Milestone 6 — the Pi Zero W

`deploy/labelpi.service`, `deploy/deploy.sh` (cron: fetch `main` from the
public GitHub repo over HTTPS — no credentials — pull, install requirements
if changed, smoke test, restart), `deploy/labelpi.sudoers`. Verify on the
real Zero: Python ≥ 3.11, **bleak / dbus-fast install from piwheels on
ARMv6 without compiling**, Pillow wheels, memory, timings.

## Where things are

- Repo: https://github.com/topicomha/labelpi (public, MIT, branch `main`).
- On the lab Pi a throwaway test checkout, `~/labelpi-test`, may still be
  running `run.py` on **port 8099** with a real `config/printers.toml` (both
  printers' addresses). To reuse that config in a fresh clone:
  `cp ~/labelpi-test/config/printers.toml <clone>/config/`. Stop the old
  server before starting another one that talks to the same printers:
  `pkill -f '^\.venv/bin/python run\.py$'` (run from any directory).
  Delete `~/labelpi-test` when done with it.
- Finding printer addresses: `bluetoothctl devices` — the Brother shows as
  `PT-P300BT…` (paired + trusted); the D30 advertises as `D30` over BLE and
  needs no pairing. Real addresses never go in the repo.

## Starting a session on the Pi

```bash
git clone https://github.com/topicomha/labelpi.git ~/labelpi
cd ~/labelpi
git config user.name  "David Boyd"
git config user.email "10187806+topicomha@users.noreply.github.com"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp ~/labelpi-test/config/printers.toml config/   # or copy the example and fill in MACs
.venv/bin/python -m pytest                       # 216 tests, no hardware
```

To push from the Pi you need GitHub credentials there, e.g. `gh auth login`
(HTTPS) — install with `sudo apt install gh`.

## Decisions log

- **The "D30" is a Phomemo D30**, not a Niimbot (the manual says Phomemo).
  niimprint/niimbluelib are irrelevant.
- **Own encoders for both printers.** Ircama/PT-P300BT has no licence, so
  nothing of it is vendored; the Phomemo job format comes from MIT/Apache
  sources. No `vendor/` directory exists.
- **D30 over BLE (bleak)**; Classic RFCOMM connected but a one-burst job
  didn't print. **Brother over Classic RFCOMM** with a stdlib socket.
- **Page-saved settings in `config/settings.json`** (gitignored);
  `printers.toml` stays hand-written and read-only.
- **Templates** replaced "shortcuts" (API and config key renamed);
  syntax in SPEC §12.
- **Public GitHub repo, MIT**, deploy from `main` without credentials;
  commits use the owner's GitHub noreply address.
- Template buttons select + preview; printing is always the Print button (no
  accidental one-tap prints on a phone).

## Gotchas learned the hard way

- A jammed D30 label swallows jobs silently while still reporting "paper
  present". If nothing comes out, reseat the roll.
- The D30 disappears from scans while a phone is connected to it or when it
  sleeps.
- A plain `recv()` returns partial data; the Brother's 32-byte status needs a
  read-exact loop (`brother.read_exact`).
- Linux's `TIOCOUTQ` on a Bluetooth socket returns *free* buffer space, not
  queued bytes.
- `pkill -f "python run.py"` over SSH can kill the SSH command itself (its
  command line contains the same text) — anchor the pattern as above.
- In Pillow 12, `getpixel` on a mode-"1" image returns 0/1, not 0/255.
