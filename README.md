# labelpi

Print labels to a Brother PT-P300BT and a Phomemo D30 over Bluetooth, from a
browser or a script, via a tiny Flask service on a Raspberry Pi Zero W.

- Web UI: `http://labelpi.lan:8080`
- API: `POST /api/print/text`, `/api/print/image`, `/api/print/shortcut`
  (add `?preview=1` to get a PNG instead of printing)

## Docs

- `CLAUDE.md` — conventions and constraints for coding in this repo
- `docs/SPEC.md` — features, config, API, deployment
- `docs/PI_SETUP.md` — one-time Pi setup

## Develop locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp config/printers.example.toml config/printers.toml
LABELPI_MOCK=1 python run.py --dev   # prints go to ./out/*.png
pytest
```

## Deploy

Push to `main` on GitHub. The Pi checks every minute and redeploys itself.

## How this was built

This project is **mostly written by [Claude Code](https://claude.com/claude-code)**
(Anthropic's AI coding agent), directed and reviewed by a human who owns the
printers. The hardware findings in `docs/SPEC.md` §10 come from real test
prints; the code and docs were largely generated in those sessions. Commits
made by Claude carry a `Co-Authored-By: Claude` trailer.

## Licence

MIT — see `LICENSE`. Printer protocol notes credit their sources in
`docs/SPEC.md` §10.
