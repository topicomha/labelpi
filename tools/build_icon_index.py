"""
Build labelpi/vendor/icons.json - the searchable list of bundled icons.

Dev-only: run it when updating an icon font, then commit the fonts and the
new icons.json together. The Pi never runs this; it only reads icons.json.

Each library's npm package has a metadata file that maps icon names to the
font's code points (plus search words). Download and unpack them first:

    npm pack @fortawesome/fontawesome-free@6.7.2   # fonts: webfonts/*.ttf
    npm pack @tabler/icons-webfont@3.48.0          # fonts: dist/fonts/*.ttf
    npm pack @tabler/icons@3.48.0                  # icons.json (names, tags)
    npm pack @mdi/font@7.4.47                      # fonts/materialdesignicons-webfont.ttf
    npm pack @mdi/svg@7.4.47                       # meta.json (names, aliases)
    (or curl the tarballs from registry.npmjs.org and `tar xz` each one)

    python tools/build_icon_index.py \\
        --fontawesome <dir>/package --tabler <dir>/package --mdi <dir>/package

Font Awesome 7 ships only WOFF2 fonts, which Pillow on the Pi may not read,
and its licence forbids modified fonts from using its name - so we stay on
Font Awesome 6, which still ships plain TTF files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "labelpi" / "vendor" / "icons.json"

# Each style is one font file. "font" is relative to labelpi/vendor/.
STYLES = [
    {"id": "fa-solid", "label": "Font Awesome solid", "font": "fontawesome/fa-solid-900.ttf"},
    {"id": "fa-regular", "label": "Font Awesome regular", "font": "fontawesome/fa-regular-400.ttf"},
    {"id": "fa-brands", "label": "Font Awesome brands", "font": "fontawesome/fa-brands-400.ttf"},
    {"id": "tabler", "label": "Tabler outline", "font": "tabler/tabler-icons.ttf"},
    {"id": "tabler-filled", "label": "Tabler filled", "font": "tabler/tabler-icons-filled.ttf"},
    {"id": "mdi", "label": "Material Design Icons", "font": "mdi/materialdesignicons-webfont.ttf"},
]


def words(*parts: object) -> str:
    """Lower-case search words, each once, as one space-separated string."""
    seen: list[str] = []
    for part in parts:
        for word in str(part).lower().replace("-", " ").replace("/", " ").split():
            if word not in seen:
                seen.append(word)
    return " ".join(seen)


def fontawesome(package: Path) -> dict[str, list]:
    data = json.loads((package / "metadata" / "icon-families.json").read_text())
    out: dict[str, list] = {"fa-solid": [], "fa-regular": [], "fa-brands": []}
    for name, icon in data.items():
        search = words(icon.get("label", ""), *icon.get("search", {}).get("terms", []))
        for family_style in icon.get("familyStylesByLicense", {}).get("free", []):
            if family_style["family"] != "classic":
                continue
            style = "fa-" + family_style["style"]
            if style in out:
                out[style].append([name, int(icon["unicode"], 16), search])
    return out


def tabler(package: Path) -> dict[str, list]:
    data = json.loads((package / "icons.json").read_text())
    out: dict[str, list] = {"tabler": [], "tabler-filled": []}
    for name, icon in data.items():
        search = words(icon.get("category", ""), *icon.get("tags", []))
        for style, key in (("outline", "tabler"), ("filled", "tabler-filled")):
            if style in icon.get("styles", {}):
                out[key].append([name, int(icon["styles"][style]["unicode"], 16), search])
    return out


def mdi(package: Path) -> dict[str, list]:
    data = json.loads((package / "meta.json").read_text())
    icons = []
    for icon in data:
        if icon.get("deprecated"):
            continue
        search = words(*icon.get("aliases", []), *icon.get("tags", []))
        icons.append([icon["name"], int(icon["codepoint"], 16), search])
    return {"mdi": icons}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fontawesome", type=Path, required=True)
    parser.add_argument("--tabler", type=Path, required=True, help="the @tabler/icons package")
    parser.add_argument("--mdi", type=Path, required=True, help="the @mdi/svg package")
    args = parser.parse_args()

    icons = {**fontawesome(args.fontawesome), **tabler(args.tabler), **mdi(args.mdi)}
    for style in icons.values():
        style.sort(key=lambda icon: icon[0])
    index = {"styles": STYLES, "icons": icons}
    # One icon per line keeps diffs readable when a library is updated.
    lines = ['{"styles": ' + json.dumps(STYLES) + ', "icons": {']
    for n, (style, entries) in enumerate(icons.items()):
        lines.append(f"{json.dumps(style)}: [")
        lines.append(",\n".join(json.dumps(entry) for entry in entries))
        lines.append("]" + ("," if n < len(icons) - 1 else ""))
    lines.append("}}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.loads(OUT.read_text())  # sanity check: it parses
    counts = ", ".join(f"{k} {len(v)}" for k, v in index["icons"].items())
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB): {counts}")


if __name__ == "__main__":
    main()
