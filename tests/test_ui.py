"""The web UI is static files; these tests check they're served and self-contained."""

from __future__ import annotations

import re

import pytest
from conftest import MINIMAL_TOML, ROOT

from labelpi.app import create_app

STATIC = ROOT / "labelpi" / "static"


@pytest.fixture
def client(write_config, tmp_path):
    return create_app(write_config(MINIMAL_TOML), out_dir=tmp_path).test_client()


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert '<script src="/static/app.js"' in html
    assert '<link rel="stylesheet" href="/static/style.css">' in html


@pytest.mark.parametrize(
    "path, mimetypes",
    [
        # The .js type comes from the OS: Linux says text/, Windows application/.
        ("/static/app.js", {"text/javascript", "application/javascript"}),
        ("/static/style.css", {"text/css"}),
    ],
)
def test_assets_are_served(client, path, mimetypes):
    response = client.get(path)
    assert response.status_code == 200
    assert response.mimetype in mimetypes


def test_non_api_404_is_not_json(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.mimetype == "text/html"


@pytest.mark.parametrize("name", ["index.html", "app.js", "style.css"])
def test_no_external_resources(name):
    """The Pi may be offline-ish: nothing may load from a CDN or font service."""
    text = (STATIC / name).read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=\"(https?:)?//", text)
    assert "@import" not in text
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", text)


def test_ui_only_uses_public_api():
    """The page must go through the same /api endpoints scripts use."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    urls = set(re.findall(r"[\"'`](/[a-z/]+)", js))
    assert urls, "expected some API URLs in app.js"
    assert all(url.startswith("/api/") for url in urls), urls


def test_every_element_id_used_by_js_exists():
    """Catch typos between app.js $("id") calls and index.html."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\(\"([a-z-]+)\"\)", js))
    defined = set(re.findall(r'id="([a-z-]+)"', html))
    assert used and used <= defined, used - defined
