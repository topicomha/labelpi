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
   months*), laid out with a background (colour, frame, picture) and
   positioned text, icons, boxes, circles, lines and pictures. Starters are
   built in; add, edit and delete them on the page. See §12.
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
                                      "layout": { ... }, "fields": ["Item"] }]
POST   /api/templates           { "name": "Jar", "layout": { ... } }        -> 201 + template
POST   /api/templates           { "name": "Jar", "text": "{field:Contents}" } -> 201 + template
PUT    /api/templates/<id>      { "name": "...", "layout": { ... } }          -> template
DELETE /api/templates/<id>                                                   -> 204
```
A template has either `"layout"` (§12) or plain `"text"` (every line centred,
as large as fits), never both. Layouts come back with every default filled
in. Bad template or name → `400`; unknown id → `404`. New ids come from the
name (`"Freezer bag"` → `freezer-bag`, `freezer-bag-2` if taken).

### Export / import
```
GET  /api/templates/export[?id=freezer&id=food]  -> attachment labelpi-templates-<date>.json
POST /api/templates/import   multipart "file", or the file's JSON as the body
                             -> 201 { "imported": [templates], "skipped": ["Freezer", ...] }
```
The file: `{"format": "labelpi-templates", "version": 1, "exported": "...",
"templates": [{"name", "layout" | "text"}], "pictures": {"<id>": "<base64 PNG>"}}`
— pictures a layout uses travel inside it, and get re-stored (and the
layouts' ids rewritten) on import. Import adds templates next to the
existing ones and never replaces any: a taken name gets a new id, and a
template identical to one already here (same name and design) is skipped.
All or nothing: an invalid template → `400` naming it, nothing added.

### Icons and pictures (for layouts)
```
GET    /api/icons?q=snow&style=fa-solid&limit=60
         -> { "styles": [{ "id": "fa-solid", "label": "...", "font": "/vendor/....ttf" }],
              "icons":  [{ "id": "fa-solid:snowflake", "style": "fa-solid",
                           "name": "snowflake", "codepoint": 62172 }] }
GET    /api/assets              -> [{ "id", "width", "height", "bytes", "url" }]
POST   /api/assets              multipart "file" (PNG/JPEG/GIF/BMP) -> 201 + asset
GET    /api/assets/<id>         -> image/png
DELETE /api/assets/<id>         -> 204; 409 "in_use" if a template uses it
```
Icon search matches names and keywords, best first; empty `q` lists
alphabetically; `limit` ≤ 200. `/vendor/<library>/<font>.ttf` serves the icon
fonts so the page can show the icons it will print.

### `POST /api/print/text`
```json
{ "printer": "brother", "label": "tze-12", "text": "Hello\nworld",
  "align": "center", "font_size": null, "length_mm": null }
```

### Chain printing and feed (tape printers)
```
GET  /api/printers                 -> [{ ..., "can_chain": true, "auto_feed": true }]
PUT  /api/printers/<id>/settings   { "auto_feed": false }  -> printer (saved in settings.json)
POST /api/printers/<id>/feed       -> { "status": "fed", "printer": "brother", "ms": 2400 }
```
The Brother's cutter is ~24 mm past its print head, so after each label it
normally feeds the tape out so the label can be cut, which leaves ~24 mm of
blank tape before the next one. With `auto_feed: false` labels print back to
back instead (~2–3 mm apart), each with a dashed cut line at both ends (also
in the preview), and `POST .../feed` pushes the finished strip out to the
cutter (a one-line blank job). Every print endpoint also takes `"auto_feed"`
(JSON) or an `auto_feed` form field to override the saved setting for one
label; print responses say `"fed": true/false`. Printers that can't chain
(the D30's die-cut labels) ignore it; setting it or feeding them is a `400`.

### `POST /api/print/ruler`
```json
{ "printer": "d30", "label": "12x50", "length_mm": null }
```
Prints a calibration ruler along the whole label: a solid bar at 0 (the first
column of the image), a tick every mm, numbers every 10 mm, lines along both
long edges. Label margins are ignored; the printer's `prepare()` (and so the
D30's `offset_mm`) is applied. Compare it with the physical label and adjust
`length_mm` / `offset_mm`. `length_mm` is for tape only (default 50).

### `POST /api/print/image` — `multipart/form-data`
Fields: `printer`, `label`, `file`, optional `dither`, `invert`, `length_mm`.

### `POST /api/print/template`
```json
{ "printer": "d30", "label": "12x50", "template": "freezer",
  "fields": { "Item": "Chicken soup" }, "length_mm": null }
```
Instead of `"template": id`, `"layout": {...}` or `"text": "..."` prints/
previews an unsaved template (the page's editor uses this for its live
preview). Missing fields print as blanks. `length_mm` sets the length on
continuous tape (default: the layout's `tape_length_mm`, else auto; text
templates are as long as their text). `"align"` applies to text templates only.

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
  -F printer=d30 -F label=12x50 -F file=@logo.png
```

## 6. Web UI

One page, responsive from ~360 px phone width up to desktop.

- Printer selector (shows busy state) → label selector filtered by printer.
- Segmented control: **Text | Image | Templates**.
  - Text: textarea, align buttons.
  - Image: file picker (plus drag-drop on desktop), dither/invert toggles.
  - Templates: one button per template; a text box for each `{field:...}`;
    "Duplicate" copies the selected one into the editor as "… copy";
    "Import…" / "Export all" load and save template files (see §5);
    "Edit this template" / "+ New template" open the layout editor: name;
    background (colour, frame, frame thickness, picture, length on tape);
    the element list (one collapsible card per element with its settings,
    move up/down, duplicate, remove; "Add" Text / Icon / Box / Circle / Line
    / Picture); an icon search that shows the icons in their own fonts; a
    picture chooser with upload; an "Edit as JSON" view; Save / Cancel /
    Delete. Every change previews live. Old text templates open as one text
    element and are saved as layouts. No drag-and-drop: positions are typed.
    Picking a template selects and previews it; Print prints it.
- Tape printers: a "Feed out after each label" checkbox (saved on the Pi)
  and, when it's off, a **Feed & cut** button next to Print.
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
5. Real Phomemo backend (in-house ESC/POS encoder + BLE via `bleak`) — **done**;
   real print verified with the calibration ruler (2026-09-28).
6. Deploy script, systemd unit, Pi setup doc verified end to end.
7. Printer setup from the UI: discovery, pairing, calibration (§11).
8. Templates with fill-in fields and date maths, editable on the page (§12)
   — **done**.
9. Label sizes managed from the page, per printer (saved in settings.json).
10. Template layouts: backgrounds, shapes, icons and pictures (§12) — **done**.

Build order agreed 2026-09-28: 8 → 9 → 5 → 7 → 6; 10 was added and done
before 9. Each is its own branch
and PR, merged when tests pass.

## 10. Milestone 0 findings

Run on a lab Pi (Raspberry Pi OS trixie, Python 3.13, BlueZ 5.82), not the
Zero W — protocol results carry over; timings and ARMv6 installs must be
re-checked on the Zero. The spike scripts were deleted once the real
backends covered them (see git history for `spike/`); `POST
/api/print/ruler` replaced their `--calibrate` rulers.

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
  Ircama's is vendored.
- ~~D30 label geometry~~ — resolved 2026-09-28: the labels are 12 × 50 mm
  (ruler print, measured gap to gap). A 50 mm image lands on the label with
  its 0 ~0.5 mm before the leading edge and 50 at the far end; the scale is
  true (203 dpi). The earlier "starts ~12 mm in" was a 40 mm image on a
  50 mm label: the printer aligns the image with the label's end.
  `length_mm = 50`, `offset_mm = 0`.
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
- **Calibrate**: print a ruler (`POST /api/print/ruler`), the user reads off
  where it lands, and the UI stores the offset for that label. (The Brother's
  band height, 64 px on 12 mm tape, was measured once with a 128-row
  staircase from the Milestone 0 spike; bring that back only if another tape
  width is added.)

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
Built-in starters (layouts, `labelpi/starters.py`): **Today's date**
(framed), **Opened** (black "OPENED" badge + date), **Food** (icon, name,
made / use by +3 days), **Freezer** (snowflake, item, frozen / use by +3
months), **Container** (double frame, contents + date).

### Layouts (`labelpi/layout.py`)

A layout template is a background plus a list of elements, drawn in order
(later ones on top):

```json
{ "tape_length_mm": null,
  "background": { "fill": "white", "frame": "rounded", "frame_mm": 0.4,
                  "radius_mm": 1.5, "image": null },
  "elements": [
    { "type": "icon", "x": 0, "y": 0, "w": 14, "h": 100,
      "icon": "fa-solid:snowflake", "color": "black" },
    { "type": "text", "x": 17, "y": 0, "w": 83, "h": 55, "text": "{field:Item}",
      "align": "left", "valign": "middle", "font": "bold", "size_mm": 0,
      "color": "black" },
    { "type": "line", "x1": 17, "y1": 60, "x2": 100, "y2": 60, "stroke_mm": 0.3 }
  ] }
```

- **Units.** `x`/`y`/`w`/`h` (and a line's `x1`…`y2`) are % of the printable
  area — the label minus its margins; on tape, the band between the end
  margins — so one template fits every label size. x runs along the label,
  y across it, as you read it. Thicknesses and radii are in mm.
- **Background:** `fill` white/black; `frame` none/line/rounded/double,
  drawn in the opposite colour at the edge of the printable area; `image`
  `{asset, fit, dither, invert}` fills the area behind everything.
- **Elements:** `text` (template syntax as above; shrinks to fit its box,
  `size_mm` caps it, 0 = as big as fits; `font` bold/regular/condensed;
  empty text draws nothing), `rect` (`fill` none/black/white, outline
  `color` + `stroke_mm`, 0 = no outline; `radius_mm`), `ellipse`, `line`,
  `icon` (`"<style>:<name>"`, as large as fits its box, centred, shape kept),
  `image` (`asset` id; `fit` contain/cover/stretch; `dither`, `invert`).
  Colours are black or white — it's a 1-bit label.
- **Checking.** Unknown settings, wrong types, out-of-range numbers,
  unknown icons and (when saving) missing pictures are `400`s naming the
  element: `element 2 (text): unknown setting 'colour'`. At most 40 elements.
- **Tape length.** Continuous tape uses the request's `length_mm`, else
  `tape_length_mm`, else **auto** (the default, `null`): each text element at
  the largest size its box's height allows (capped by `size_mm`), the label
  just long enough for it to fit its box's share of the length (w %); icons,
  pictures and shapes don't push the length, they scale into their boxes
  (their width is a % of that same length). At least 25 mm. Die-cut labels
  always have their fixed size.
- **Icons** (`labelpi/icons.py`): Font Awesome Free 6.7.2 (solid, regular,
  brands), Tabler Icons 3.48 (outline, filled) and Material Design Icons
  7.4.47, bundled as their unmodified TTF files under `labelpi/vendor/` with
  their licences. `vendor/icons.json` (names, code points, search words) is
  made by `tools/build_icon_index.py` — rerun it when updating a font. FA 7
  ships only WOFF2 fonts, which Pillow on the Pi may not read, so FA stays
  on 6.
- **Pictures** (`labelpi/assets.py`): uploads are stored once each (named by
  a hash of the content) as greyscale PNGs, at most 1200 px a side, in
  `config/assets/` (gitignored; `$LABELPI_ASSETS` overrides). At most 200.

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
