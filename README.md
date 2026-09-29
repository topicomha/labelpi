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

## How this was built

This project is **mostly written by [Claude Code](https://claude.com/claude-code)**
(Anthropic's AI coding agent), directed and reviewed by a human who owns the
printers. The hardware findings in `docs/SPEC.md` §10 come from real test
prints; the code and docs were largely generated in those sessions. Commits
made by Claude carry a `Co-Authored-By: Claude` trailer.

## Licence

MIT — see `LICENSE`. The bundled DejaVu font has its own licence
(`labelpi/fonts/DejaVu-LICENSE`). Printer protocol notes credit their sources
in `docs/SPEC.md` §10.
