# labelpi — Specification

## 1. Goal

Print labels on a Brother PT-P300BT and a **Phomemo** D30 from a browser or
from scripts, without a phone, via a small service on a Pi Zero W on the LAN.

> Earlier drafts said "Niimbot D30". The printer we own is a Phomemo D30 —
> different company, different protocol. See §10.

## 2. Features

1. **Print text** — one or more lines (`\n` separates lines), auto-sized to fit.
2. **Print an image** — upload PNG/JPEG/GIF/BMP; it is scaled to fit the chosen
   label and converted to black and white.
3. **Templates** — ready-made labels with blanks to fill in and dates worked
   out for you (e.g. Freezer: *item*, frozen *today*, use by *today + 3
   months*). Starters are built in; add, edit and delete them on the page.
   See §12.
4. **Preview** — any of the above can be previewed as a PNG instead of printed.
5. **Label sizes** — per-printer lists; the UI only shows sizes that belong to
   the selected printer. Managing them from the page is Milestone 9.
6. **Busy handling** — one job per printer at a time; a second request to a busy
   printer gets `409` immediately. Printers are independent of each other.
7. **Printer setup from the UI** (Milestone 7) — find and pair printers, and
   calibrate label position, without SSH. See §11.

## 3. Configuration — `config/printers.toml`

See `config/printers.example.toml` for the full annotated example.

Rules:
- Adding a label size = adding a `[[printers.labels]]` block and restarting.
- Printer `type` is one of `brother_pt`, `phomemo`, `mock`.
- Brother labels are **continuous** (length follows content) and defined by
  `tape_width_mm` + `print_height_px`. The printer always takes a 128-px
  raster; `print_height_px` is the band that reaches the tape, centred in it
  (12 mm tape: 64 px, measured).
- Phomemo labels are **fixed die-cut** and defined by `width_mm` × `length_mm`,
  plus an optional `offset_mm` (where on the label printing starts; set by
  calibration, §11).
- Label and printer `id`s must be unique; validation fails loudly at startup
  with a clear message (file, printer, field).
- `[[templates]]` (optional) adds templates to the built-in starters; the same
  `id` replaces a starter. They only seed the list — see §12.
- `config/printers.toml` is gitignored (it holds MAC addresses); the
  `.example.toml` is committed. `config/settings.json` (written by the page)
  is gitignored too.

## 4. Rendering (`render.py`)

**Target canvas per label:**
- Continuous (Brother): height = `print_height_px`; width = content width +
  margins, or fixed if the request gives `length_mm`.
- Fixed (Phomemo): `width_mm × length_mm` at `dpi` (203, i.e. 8 px/mm; the
  D30 head is 96 px = 12 mm wide).

**Text:**
- Bundled font `DejaVuSans-Bold.ttf`.
- Font size chosen by binary search: the largest size where all lines fit the
  printable height (and width, for fixed labels). Optional `font_size`
  overrides it; if it doesn't fit → `400`.
- `align`: `left | center | right`, default `center`.

**Images:**
- Scale to fit (contain), preserving aspect ratio. Never crop.
- Transparency is composited onto white.
- Convert to 1-bit: threshold at 128 by default; `dither=true` uses
  Floyd–Steinberg (better for photos).
- `invert=true` optional.

**Backend `prepare()`** then does printer-specific rotation/padding. Rendering
code must not contain printer-specific rotation logic.

## 5. REST API

All endpoints are under `/api`. JSON in/out except image upload (multipart) and
previews (PNG).

### `GET /api/printers`
```json
[{ "id": "brother", "name": "Brother PT-P300BT", "busy": false,
   "labels": [{ "id": "tze-12", "name": "12 mm tape", "continuous": true }] }]
```

### Templates
```
GET    /api/templates           -> [{ "id": "freezer", "name": "Freezer",
                                      "text": "{field:Item}\nFrozen {date:%d %b %Y}\n...",
                                      "fields": ["Item"] }]
POST   /api/templates           { "name": "Jar", "text": "{field:Contents}" } -> 201 + template
PUT    /api/templates/<id>      { "name": "...", "text": "..." }             -> template
DELETE /api/templates/<id>                                                   -> 204
```
Bad template text or name → `400`; unknown id → `404`. New ids come from the
name (`"Freezer bag"` → `freezer-bag`, `freezer-bag-2` if taken).

### `POST /api/print/text`
```json
{ "printer": "brother", "label": "tze-12", "text": "Hello\nworld",
  "align": "center", "font_size": null, "length_mm": null }
```

### `POST /api/print/image` — `multipart/form-data`
Fields: `printer`, `label`, `file`, optional `dither`, `invert`, `length_mm`.

### `POST /api/print/template`
```json
{ "printer": "d30", "label": "12x40", "template": "freezer",
  "fields": { "Item": "Chicken soup" }, "align": "center" }
```
Instead of `"template": id`, `"text": "<template text>"` prints/previews an
unsaved template (the page's editor uses this for its live preview). Missing
fields print as blank lines.

### Preview
Add `?preview=1` to any `print/*` endpoint → `200 image/png` of exactly what
would be sent to the printer (after `prepare()`), nothing printed. Preview does
**not** take the printer lock.

### Responses
| Status | When | Body |
|---|---|---|
| `200` | printed | `{"status": "printed", "printer": "...", "ms": 4210}` |
| `400` | bad input, text doesn't fit, bad image | `{"error": "bad_request", "detail": "..."}` |
| `404` | unknown printer/label/template | `{"error": "not_found", "detail": "..."}` |
| `409` | printer busy | `{"error": "busy", "detail": "brother is printing"}` |
| `413` | upload too large | `{"error": "too_large", "detail": "..."}` |
| `503` | printer off / out of range / connection timeout | `{"error": "unavailable", "detail": "..."}` |
| `500` | printer reported an error or unexpected failure | `{"error": "printer_error", "detail": "..."}` |

The request blocks until the print finishes (a few seconds). Connection timeout:
15 s. Waitress runs with `threads = 4`, enough for two concurrent prints plus UI
requests.

### Automation example
```bash
curl -X POST http://labelpi.lan:8080/api/print/text \
  -H 'Content-Type: application/json' \
  -d '{"printer":"brother","label":"tze-12","text":"Server rack 2"}'

curl -X POST http://labelpi.lan:8080/api/print/image \
  -F printer=d30 -F label=12x40 -F file=@logo.png
```

## 6. Web UI

One page, responsive from ~360 px phone width up to desktop.

- Printer selector (shows busy state) → label selector filtered by printer.
- Segmented control: **Text | Image | Templates**.
  - Text: textarea, align buttons.
  - Image: file picker (plus drag-drop on desktop), dither/invert toggles.
  - Templates: one button per template; a text box for each `{field:...}`;
    "Edit this template" / "+ New template" open an editor (name, text,
    syntax help, Save / Cancel / Delete) whose text previews live.
    Picking a template selects and previews it; Print prints it.
- Live preview image, refreshed (debounced ~400 ms) from `?preview=1`.
- **Print** button, disabled while a request is in flight.
- Plain-English status line: "Printed", "Brother is busy — try again in a
  moment", "D30 not reachable — is it on?"
- Follows the system light/dark theme. No external fonts or scripts.

## 7. Bluetooth

Both printers are connected **per job, by MAC address**; no scanning is needed
once the address is known (the D30 connected fine even after BlueZ had dropped
it from its device list).

- **Brother PT-P300BT — Bluetooth Classic, stdlib RFCOMM socket, channel 1.**
  Needs pairing + trust once. No `rfcomm bind`. The socket adapter must make
  `read(n)` loop until n bytes or timeout (a raw `recv` returns partial data
  and breaks the 32-byte status read). The printer reports "printing
  completed" — wait for it before disconnecting.
- **Phomemo D30 — BLE (GATT), via `bleak`.** No pairing needed. Write to
  service `FF00` / characteristic `FF02` in 128-byte chunks with response;
  notifications arrive on `FF03` (the printer acks every chunk with `01 01`).
  It advertises as `D30` with service `AF30`.
  Classic RFCOMM (channel 1) also connects and answers status queries, but a
  job sent in one burst **did not print**. A paced-chunk Classic attempt was
  sent but its result was never checked, so treat Classic as unproven and use BLE.
- The service user must be allowed to use BlueZ (the `bluetooth` group, or
  the default desktop policy). RFCOMM sockets themselves need no special rights.

## 8. Deployment

- Code lives in a **public GitHub repo** (MIT). The Pi clones it over HTTPS
  and needs no credentials. Anything private (MAC addresses, calibration)
  stays in gitignored files on the Pi.
- The Pi deploys whatever is on `main`, so `main` must always be runnable:
  do work on branches and merge when tests pass.
- `deploy/deploy.sh`, run by cron every minute as the service user:
  1. Take a lock (`flock`) so runs never overlap.
  2. `git fetch --quiet`; if `HEAD == @{u}`, exit.
  3. Remember the current commit, then `git pull --ff-only`.
  4. If `requirements.txt` changed, `.venv/bin/pip install -r requirements.txt`
     (slow on a Zero — only when needed).
  5. Smoke check: `.venv/bin/python -c "import labelpi.app"`. On failure,
     `git reset --hard <previous commit>` and log an error.
  6. `sudo systemctl restart labelpi` (allowed by `deploy/labelpi.sudoers`).
  7. Log each step with `logger -t labelpi-deploy` (visible in `journalctl`).
- `deploy/labelpi.service`: runs `run.py` from the venv as a non-root user,
  `Restart=on-failure`, `After=bluetooth.target network-online.target`.

## 9. Milestones

0. **Hardware spike — done** (2026-09-28). See §10.
1. Config loading + validation, render module, mock backend, tests — **done**.
2. API with mock backend, tests — **done**.
3. Web UI — **done**.
4. Real Brother backend — **done** (own encoder; no vendored code, so no licence issue).
5. Real Phomemo backend (small in-house ESC/POS encoder + BLE via `bleak`).
6. Deploy script, systemd unit, Pi setup doc verified end to end.
7. Printer setup from the UI: discovery, pairing, calibration (§11).
8. Templates with fill-in fields and date maths, editable on the page (§12)
   — **done**.
9. Label sizes managed from the page, per printer (saved in settings.json).

Build order agreed 2026-09-28: 8 → 9 → 5 → 7 → 6. Each is its own branch
and PR, merged when tests pass.

## 10. Milestone 0 findings

Run on a lab Pi (Raspberry Pi OS trixie, Python 3.13, BlueZ 5.82), not the
Zero W — protocol results carry over; timings and ARMv6 installs must be
re-checked on the Zero. Scripts are in `spike/`.

| | Brother PT-P300BT | Phomemo D30 |
|---|---|---|
| Transport | Classic RFCOMM ch. 1, stdlib socket | BLE GATT, `bleak` (FF00/FF02/FF03) |
| Pairing | pair + trust once | none needed |
| Connect | 3.3–4.2 s | BLE 1.8–4 s (Classic 0.4–0.9 s, but see §7) |
| Print | 63 mm label: ~8.8 s, confirmed by printer | 40 mm: ~1 s to send; no completion message seen over BLE |
| Protocol | Ircama/PT-P300BT (`labelmaker.do_print_job`) | ESC/POS: `1F 11 24 00`, `ESC @`, `GS v 0` (12 bytes × rows), `ESC d 0` |
| Geometry | 128-px raster; 12 mm tape prints rows 32–95 (64 px), measured | 96 px wide, 8 px/mm; image rotated 270° |
| Status | model `0x72`, tape width/type, errors, phases | `1F 11 xx` queries → `1A xx yy`: paper `06 89`, cover `05 98`, temp `03 a8`, serial `08 …`, firmware `07 02 00 03` |

**Open items**
- ~~Licence~~ — resolved: Milestone 4 is our own encoder; nothing of
  Ircama's is vendored (the spike still imports it from a clone for reference).
- **D30 label geometry.** A 40 mm image started ~12 mm into the label and ran
  off its far end. The roll may be 12 × 50 mm rather than 12 × 40, so the
  real size is unconfirmed. Needs calibration (§11).
- **Brother 9 mm / 6 mm tape**: 48 / 32 px are Ircama's numbers, unverified.
- **D30 over Classic**: a single burst didn't print; paced chunks were sent
  once, but nobody checked whether that label printed. Only worth revisiting
  to drop the `bleak` dependency.
- A jammed D30 label silently swallowed jobs while still reporting "paper
  present", so a successful send ≠ a printed label.

## 11. Printer setup from the UI (planned, Milestone 7)

Goal: set up and tune printers from the web page instead of SSH.

- **Discover**: scan for nearby printers and list likely matches (names
  `PT-P300BT…`, `D30`).
- **Pair**: pair + trust the Brother; the D30 only needs its address stored.
- **Calibrate**: print a ruler (see `spike/spike_phomemo.py --calibrate`,
  `spike/spike_brother.py --calibrate`), the user reads off where it lands,
  and the UI stores the offset / band height for that label.

Design questions to settle before building it:
- ~~Where do settings live?~~ — decided: `config/settings.json`, see §12.
- **Pairing from a service** means driving BlueZ over D-Bus (`bleak` already
  pulls in `dbus-fast`) and needs the service user's BlueZ permissions.
- This loosens the "no auth" rule's safety margin: anyone on the LAN could
  pair devices. Probably fine on a home LAN — decide explicitly.

## 12. Templates and page-saved settings

### Template syntax (`labelpi/templates.py`)

| Placeholder | Becomes |
|---|---|
| `{field:Item}` | whatever is typed in the page's "Item" box (same name twice = same value) |
| `{date:%d %b %Y}` | today, strftime-formatted (`28 Sep 2026`) |
| `{time:%H:%M}` | same as `date`; the name just reads better |
| `{date+3d:...}` `{date-1w:...}` `{date+3m:...}` `{date+1y:...}` | today shifted by days / weeks / months / years; a bare number means days. Months keep the day where possible (31 Jan + 1m → 28/29 Feb) |

Anything else in braces is an error, reported when the template is saved.
Built-in starters: **Today's date**, **Opened**, **Food** (made / use by +3
days), **Freezer** (frozen / use by +3 months), **Container** (contents +
date).

### Settings file (`labelpi/settings.py`)

- `config/settings.json`, next to `printers.toml` (override with
  `$LABELPI_SETTINGS`). Gitignored. `printers.toml` stays hand-written and
  read-only; the page never edits it.
- Until the page saves anything, templates = starters + `[[templates]]` from
  the TOML. The first add/edit/delete writes the full list to settings.json,
  which is the only source from then on.
- Writes are atomic (temp file + `fsync` + rename) under a lock, so waitress's
  threads and power cuts can't corrupt it. An unreadable file is moved aside
  as `settings.json.broken-<timestamp>` and the app starts fresh, rather than
  refusing to start on a headless Pi.
- Milestones 9 and 7 will add label sizes, printer addresses and calibration
  to the same file.
