# labelpi

Print labels to a **Brother PT-P300BT** and a **Phomemo D30** over Bluetooth,
from a browser or a script, via a tiny Flask service on a Raspberry Pi.

- **Text** — auto-sized to fit, one or more lines, left/centre/right.
- **Images** — PNG/JPEG/GIF/BMP scaled to the label, optional dithering.
- **Templates** — labels with blanks and dates worked out for you, e.g.
  *Freezer: Chicken soup · frozen 28 Sep 2026 · use by Dec 2026*. Starters
  built in; add and edit your own on the page.
- **Live preview** of exactly what will print; one page that works on a phone.
- **REST API** for scripts — the web page uses the same endpoints.

Both printers print for real (the D30's label position still needs
calibrating). Current state and next steps: [`docs/STATUS.md`](docs/STATUS.md).

## Use it

- Web UI: `http://<pi>:8080`
- API: `POST /api/print/text`, `/api/print/image`, `/api/print/template`
  (add `?preview=1` to get a PNG instead of printing)
- Templates: `GET/POST /api/templates`, `PUT/DELETE /api/templates/<id>`

```bash
curl -X POST http://<pi>:8080/api/print/text \
  -H 'Content-Type: application/json' \
  -d '{"printer":"brother","label":"tze-12","text":"Server rack 2"}'
```

## Docs

- [`docs/STATUS.md`](docs/STATUS.md) — where things stand, what's verified, what's next
- [`docs/SPEC.md`](docs/SPEC.md) — features, config, API, hardware findings
- [`docs/PI_SETUP.md`](docs/PI_SETUP.md) — one-time Pi setup
- [`CLAUDE.md`](CLAUDE.md) — conventions and working agreements for coding in this repo

## Develop

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp config/printers.example.toml config/printers.toml
LABELPI_MOCK=1 .venv/bin/python run.py --dev   # no printers needed; PNGs go to ./out/
.venv/bin/python -m pytest
```

On Windows use `.venv\Scripts\python`. With real printers paired to the
machine, set their addresses in `config/printers.toml` and drop `LABELPI_MOCK=1`.

## Deploy

Push to `main` on GitHub; the Pi checks every minute and redeploys itself
(Milestone 6 — not built yet).

## Dependencies

**Runtime** (`requirements.txt`; all install on a Pi without compiling):

| Package | Used for | Licence |
|---|---|---|
| [Flask](https://flask.palletsprojects.com/) | web page and REST API | BSD-3-Clause |
| [waitress](https://github.com/Pylons/waitress) | production web server | ZPL 2.1 |
| [Pillow](https://python-pillow.org/) | drawing text and images into label bitmaps | MIT-CMU |
| [bleak](https://github.com/hbldh/bleak) | Bluetooth Low Energy for the Phomemo D30 | MIT |

Pulled in by those: Werkzeug, Jinja2, itsdangerous, Click, MarkupSafe
(BSD-3-Clause), Blinker (MIT), and on Linux
[dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast) (MIT), which
bleak uses to talk to BlueZ.

**Development** (`requirements-dev.txt`): [pytest](https://pytest.org/) (MIT),
[ruff](https://github.com/astral-sh/ruff) (MIT).

**From the system:** Python 3.11+ standard library — `socket` for Bluetooth
Classic RFCOMM to the Brother, `tomllib` for the config — and BlueZ.

**Bundled:** the [DejaVu Sans](https://dejavu-fonts.github.io/) Bold font, so
labels render the same everywhere. Bitstream Vera licence, DejaVu changes in
the public domain — see `labelpi/fonts/DejaVu-LICENSE`.

## Credits

Both printer drivers in `labelpi/printers/` are our own code — nothing
third-party is copied in — but they only exist because these people worked
out how the printers talk. Thank you.

**Brother PT-P300BT**

- [Ircama/PT-P300BT](https://github.com/Ircama/PT-P300BT) by Ircama — the
  Python driver our Milestone 0 spike printed with (imported from a local
  clone, never copied), and our reference for the command sequence, the
  32-byte status layout and the 64-px printable band on 12 mm tape. Our
  encoder's output was checked bit-for-bit against it. The repository
  publishes no licence, which is why none of its code is included here.
- The gists Ircama's work grew from, by
  [stecman](https://gist.github.com/stecman/ee1fd9a8b1b6f0fdd170ee87ba2ddafd),
  [dogtopus](https://gist.github.com/dogtopus/64ae743825e42f2bb8ec79cea7ad2057)
  and [vsigler](https://gist.github.com/vsigler/98eafaf8cdf2374669e590328164f5fc).

**Phomemo D30**

- [polskafan/phomemo_d30](https://github.com/polskafan/phomemo_d30) (MIT) —
  the start-of-job bytes sniffed from the phone app, the image header and
  the 96-px head width.
- [odensc/phomemo-d30-web-bluetooth](https://github.com/odensc/phomemo-d30-web-bluetooth)
  (Apache-2.0) — the BLE service and characteristic, 128-byte writes and the
  end-of-job command.
- [theacodes/phomemo_m02s](https://github.com/theacodes/phomemo_m02s) (MIT) —
  where polskafan's work started.
- [sgrankin/phomemo](https://github.com/sgrankin/phomemo) (MIT) — protocol notes
  for the status replies (labels loaded, cover, temperature).
- [vivier/phomemo-tools](https://github.com/vivier/phomemo-tools) (GPL-3.0) —
  protocol notes and its D30 driver, read for reference only.
- [daehyeok/d30-printer](https://github.com/daehyeok/d30-printer) (AGPL-3.0) —
  read only, to confirm the same byte sequence.

**Consulted but not used.** Until the manual said "Phomemo", we thought the
D30 was a Niimbot, so the spike first looked at
[AndBondStyle/niimprint](https://github.com/AndBondStyle/niimprint) (MIT,
originally by kjy00302),
[MultiMote/niimbluelib](https://github.com/MultiMote/niimbluelib) (MIT) and the
[NIIMBOT community wiki](https://printers.niim.blue/). For the Brother,
[piksel/pytouch-cube](https://github.com/piksel/pytouch-cube) (MIT) was the
fallback if writing our own encoder hadn't worked out.

## How this was built

This project is **mostly written by [Claude Code](https://claude.com/claude-code)**
(Anthropic's AI coding agent), directed and reviewed by a human who owns the
printers. The hardware findings in `docs/SPEC.md` §10 come from real test
prints; the code and docs were largely generated in those sessions. Commits
made by Claude carry a `Co-Authored-By: Claude` trailer.

## Licence

MIT — see `LICENSE`. The bundled DejaVu font has its own licence
(`labelpi/fonts/DejaVu-LICENSE`); dependencies and credits are listed above.
