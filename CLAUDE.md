# CLAUDE.md — labelpi

Guidance for Claude Code when working in this repository. Read this first,
then **`docs/STATUS.md`** (where things stand, what's verified, what's next —
the handoff between sessions) and `docs/SPEC.md` (full behaviour and API).

## What this is

A small, self-hosted label printing service that runs on a dedicated
**Raspberry Pi Zero W** (original, ARMv6, 512 MB RAM) and prints over Bluetooth to
two label printers:

| Printer | Protocol / library | Transport |
|---|---|---|
| Brother P-touch Cube **PT-P300BT** | in-house raster encoder (`printers/brother.py`) | Bluetooth Classic, stdlib RFCOMM socket, channel 1 |
| **Phomemo** D30 (not Niimbot!) | in-house ESC/POS raster encoder (~50 lines) | BLE GATT via `bleak` (service FF00, write FF02) |

It exposes a REST API (for automation) and a single responsive web page that uses
that same API. The owner is a C#/JavaScript developer, not a Python regular —
keep the Python plain, explicit and well-commented.

## Hard constraints

- **Must run on an original Pi Zero W (ARMv6).** No dependencies that need a
  compiler on the Pi or that lack ARMv6 wheels (piwheels) or Debian packages.
  No Node, no .NET, no Docker on the Pi.
- **Pure Python where possible.** Allowed runtime deps: `flask`, `waitress`,
  `pillow`, `bleak` (the D30 only printed over BLE in Milestone 0).
  `pyserial`/`packbits` are not needed — both printers use sockets/BLE
  directly and PackBits is ~20 lines in `brother.py`. Justify any addition.
- **No build step.** Front end is one `index.html`, one `app.js`, one `style.css`.
  Vanilla JS, no frameworks, no bundler, no CDN dependencies (the Pi may be
  offline-ish; serve everything locally).
- **No queue, no database, no background workers.** (Settings saved from the
  page go in one JSON file, `config/settings.json` — not a database.) Each printer has its own
  in-memory lock. If a printer is busy, the API returns `409 Busy` immediately.
  The two printers are independent — one can print while the other is busy.
- **Config is TOML, read with the stdlib `tomllib`** (Python ≥ 3.11). No YAML.
- **No auth.** LAN-only service. Don't add login or API keys unless asked.
- Keep memory use low: don't hold uploaded images longer than the request,
  cap upload size (see SPEC).

## Project layout (target)

```
labelpi/
  __init__.py
  app.py            # Flask app factory: create_app(config_path)
  api.py            # /api blueprint — all endpoints
  config.py         # load + validate config/printers.toml
  render.py         # text -> image, fit image to label
  templates.py      # template syntax: {field:..}, {date+3d:..}; starter templates
  settings.py       # config/settings.json: what the page saves (templates, ...)
  printers/
    __init__.py     # registry: build printers from config, per-printer locks
    base.py         # Printer interface (abstract base class)
    brother.py      # PT-P300BT backend (our own raster encoder)
    phomemo.py      # Phomemo D30 backend (ESC/POS raster over BLE)
    mock.py         # writes PNGs to ./out/ instead of printing
  vendor/           # vendored third-party printer code, with LICENSE files
  fonts/            # bundled DejaVuSans-Bold.ttf (deterministic rendering)
  static/
    index.html
    app.js
    style.css
config/
  printers.toml     # real config (MACs etc.) — gitignored
  printers.example.toml
deploy/
  deploy.sh         # cron-driven pull-and-restart script
  labelpi.service   # systemd unit
  labelpi.sudoers   # allows the deploy user to restart the service only
tests/
docs/
  SPEC.md
  PI_SETUP.md
run.py              # entry point: waitress in prod, flask dev server with --dev
requirements.txt
requirements-dev.txt
```

## Architecture rules

- **Everything becomes an image.** Text and filled-in templates are rendered to a 1-bit
  Pillow image by `render.py`; backends only ever receive a ready-to-print image.
  Backends do not know about text.
- **Backends implement `base.Printer`:**
  - `id`, `display_name`, `labels` (from config)
  - `prepare(image, label) -> Image` — fit/rotate/convert for this printer
  - `print(image, label) -> None` — connect, send, disconnect; raise
    `PrinterUnavailable` / `PrinterError` on failure
  - `status() -> dict` (optional, best-effort)
- **Connect per job, disconnect after.** Don't hold Bluetooth connections open
  between jobs; the printers auto-sleep and stale connections are the #1 source
  of flakiness.
- **Locking lives in the registry, not in backends.** Use
  `threading.Lock.acquire(blocking=False)`; always release in `finally`.
- The web UI must go through the public API — no private endpoints for the UI.
- Preview uses the exact same rendering path as print (`?preview=1` returns the
  PNG instead of printing), so what you see is what prints.

## Commands

Dev (any Linux box or the Pi; on Windows use `.venv/Scripts/...`):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp config/printers.example.toml config/printers.toml   # then set the real MACs
LABELPI_MOCK=1 .venv/bin/python run.py --dev   # no printers: PNGs go to ./out/
.venv/bin/python -m pytest                     # run tests (no hardware)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

With real printers (on a Pi they're paired to), drop `LABELPI_MOCK=1`:
`.venv/bin/python run.py` serves waitress on the `[server]` host/port.
In production systemd runs it (Milestone 6).

Setting `LABELPI_MOCK=1` forces every printer to the mock backend regardless of
config — use this for local dev and in tests. `LABELPI_CONFIG` and
`LABELPI_SETTINGS` override the config and settings file paths.

## Testing

- `pytest`, no hardware. Tests always run with the mock backend.
- Cover: text rendering sizes/fitting, image fitting per label type, template
  expansion (dates with a frozen clock), config loading/validation errors, and
  API behaviour via Flask's test client (happy path, 400/404/409, preview).
- Test the busy path by holding a printer's lock in the test and asserting 409.
- No integration tests against real printers. Hardware checks are manual — see
  `docs/PI_SETUP.md`.

## Style

- Python 3.11+, type hints on public functions, `dataclasses` for config objects.
- Standard library first. `logging` (not print) — systemd captures stdout to the
  journal.
- Small functions, docstrings that explain *why*. Assume the reader knows C#
  and JS but not Python idioms — avoid clever one-liners.
- Format with `ruff format`, lint with `ruff check` (dev dependency only).

## Hardware facts and open risks (Milestone 0 done — details in `docs/SPEC.md` §10)

1. **The D30 is a Phomemo, not a Niimbot.** niimprint / niimbluelib don't
   apply. Protocol refs: polskafan/phomemo_d30 (MIT),
   odensc/phomemo-d30-web-bluetooth (Apache-2.0). daehyeok/d30-printer is
   AGPL — read it, never copy from it.
2. **Ircama/PT-P300BT has no licence** — never vendor or copy it. Milestone 4
   avoided it with our own encoder; keep it that way.
3. **A sent job is not a printed label.** The D30 sends no completion message
   over BLE and swallowed jobs silently when a label was jammed. The Brother
   does confirm completion — wait for it before disconnecting.
4. **D30 label position is uncalibrated** (printing started ~12 mm into the
   label); label size may be 12 × 50, not 12 × 40. Calibration comes with the
   setup UI (Milestone 7).
5. **Licences.** Check the licence of each piece of code before vendoring it and
   keep its LICENSE file alongside it in `vendor/`.
6. The `spike/` scripts are superseded by the real backends (Milestones 4–5)
   but their `--calibrate` rulers are the reference for the calibration UI
   (Milestone 7). Delete the folder once that exists.

## Working agreements (how the owner wants this repo run)

- **Public repo** (github.com/topicomha/labelpi, MIT). **Never commit private
  data**: real Bluetooth MACs, IP addresses, hostnames, serial numbers, emails.
  Real values live only in the gitignored `config/printers.toml` and
  `config/settings.json`. Before every push, grep the tracked files for any
  real MAC/IP you've seen this session (`git grep -n -I -iE "<mac>|<ip>"`).
- **Commit identity** for this repo: `David Boyd
  <10187806+topicomha@users.noreply.github.com>` (the GitHub noreply
  address). Set it with repo-local `git config user.name/user.email` in a
  fresh clone — never use the owner's real email addresses.
- **Credit Claude Code**: the README says the project is mostly written by
  Claude Code; commits Claude makes end with a `Co-Authored-By: Claude …`
  trailer; PR bodies end with the Claude Code line.
- **One branch per milestone/feature**, created from the latest `main`; open a
  PR; merge (merge commit, delete branch) once tests + ruff pass and, for
  printer changes, a real print has been checked. `main` must always run —
  the Pi deploys whatever is on it.
- **Hardware tests need the owner** at the printers: ask them to switch the
  printer on (the D30 sleeps; a phone connected to it hides it), then ask what
  came out. Don't claim a D30 print worked — it sends no confirmation.
- On the lab Pi: prefer `sudo` for one-off Bluetooth commands over changing
  group membership; a Bluetooth **keyboard** is paired to it — never
  `remove`, power off Bluetooth, or restart `bluetoothd`.
- The owner is a C#/JS developer: explain Python idioms briefly, keep code
  plain.

## Out of scope — don't build these

Label designer / WYSIWYG layout, job queue or history,
user accounts, barcode/QR generation (maybe later), USB printing, multiple
copies in one request (callers can loop).
