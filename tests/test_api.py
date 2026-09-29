from __future__ import annotations

import io
from datetime import datetime

import pytest
from conftest import MINIMAL_TOML
from PIL import Image

from labelpi.app import create_app
from labelpi.printers import PrinterError, PrinterUnavailable


@pytest.fixture
def app(write_config, tmp_path):
    app = create_app(write_config(MINIMAL_TOML), out_dir=tmp_path / "out")
    app.config["TESTING"] = True
    app.config["LABELPI_NOW"] = lambda: datetime(2026, 9, 28, 14, 5)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def registry(app):
    return app.extensions["labelpi"].registry


def png_bytes(size=(200, 50), color="black") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, data=None, preview=False, **fields):
    form = {"printer": "die", "label": "12x40", "file": (io.BytesIO(data or png_bytes()), "a.png")}
    form.update(fields)
    url = "/api/print/image" + ("?preview=1" if preview else "")
    return client.post(url, data=form, content_type="multipart/form-data")


def assert_error(response, status, error):
    assert response.status_code == status, response.get_json()
    body = response.get_json()
    assert body["error"] == error and body["detail"]


# --- read-only endpoints ------------------------------------------------------
def test_list_printers(client):
    body = client.get("/api/printers").get_json()
    assert [p["id"] for p in body] == ["tape", "die"]
    tape, die = body
    assert tape["busy"] is False
    assert tape["labels"] == [
        {"id": "tze-12", "name": "tze-12", "continuous": True, "tape_width_mm": 12.0}
    ]
    assert die["labels"][0] == {
        "id": "12x40",
        "name": "12x40",
        "continuous": False,
        "width_mm": 12.0,
        "length_mm": 40.0,
    }


def test_printers_show_busy(client, registry):
    with registry.claim("tape"):
        body = client.get("/api/printers").get_json()
    assert [p["busy"] for p in body] == [True, False]


def test_list_templates_includes_starters_and_config(client):
    body = client.get("/api/templates").get_json()
    by_id = {t["id"]: t for t in body}
    assert list(by_id) == ["today", "opened", "food", "freezer", "container"]
    assert by_id["today"]["text"] == "{date:%Y-%m-%d}"  # config version replaced the starter
    assert by_id["freezer"]["fields"] == ["Item"]


# --- print text -------------------------------------------------------------
def test_print_text(client, registry):
    response = client.post(
        "/api/print/text", json={"printer": "tape", "label": "tze-12", "text": "Hi"}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "printed" and body["printer"] == "tape" and body["ms"] >= 0
    [path] = registry.get("tape").printed
    assert Image.open(path).height == 64


def test_preview_text_returns_png_and_prints_nothing(client, registry):
    response = client.post(
        "/api/print/text?preview=1",
        json={"printer": "die", "label": "12x40", "text": "Hi\nthere", "align": "left"},
    )
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert Image.open(io.BytesIO(response.data)).size == (320, 96)
    assert registry.get("die").printed == []


def test_preview_works_while_busy(client, registry):
    with registry.claim("die"):
        response = client.post(
            "/api/print/text?preview=1", json={"printer": "die", "label": "12x40", "text": "x"}
        )
    assert response.status_code == 200


def test_busy_printer_gives_409(client, registry):
    with registry.claim("tape"):
        response = client.post(
            "/api/print/text", json={"printer": "tape", "label": "tze-12", "text": "x"}
        )
    assert_error(response, 409, "busy")
    assert "tape is printing" in response.get_json()["detail"]


def test_other_printer_still_prints_while_one_is_busy(client, registry):
    with registry.claim("tape"):
        response = client.post(
            "/api/print/text", json={"printer": "die", "label": "12x40", "text": "x"}
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "body, status, error",
    [
        ({"printer": "nope", "label": "x", "text": "x"}, 404, "not_found"),
        ({"printer": "tape", "label": "nope", "text": "x"}, 404, "not_found"),
        ({"label": "tze-12", "text": "x"}, 400, "bad_request"),
        ({"printer": "tape", "text": "x"}, 400, "bad_request"),
        ({"printer": "tape", "label": "tze-12"}, 400, "bad_request"),
        ({"printer": "tape", "label": "tze-12", "text": 5}, 400, "bad_request"),
        ({"printer": "tape", "label": "tze-12", "text": ""}, 400, "bad_request"),
        ({"printer": "tape", "label": "tze-12", "text": "x", "align": "up"}, 400, "bad_request"),
        (
            {"printer": "tape", "label": "tze-12", "text": "x", "font_size": "big"},
            400,
            "bad_request",
        ),
        ({"printer": "tape", "label": "tze-12", "text": "x", "font_size": 500}, 400, "bad_request"),
        (
            {"printer": "tape", "label": "tze-12", "text": "x", "font_size": True},
            400,
            "bad_request",
        ),
        ({"printer": "die", "label": "12x40", "text": "x", "length_mm": 30}, 400, "bad_request"),
    ],
)
def test_print_text_errors(client, body, status, error):
    assert_error(client.post("/api/print/text", json=body), status, error)


def test_length_mm_on_tape(client):
    response = client.post(
        "/api/print/text?preview=1",
        json={"printer": "tape", "label": "tze-12", "text": "x", "length_mm": 50},
    )
    assert Image.open(io.BytesIO(response.data)).width == round(50 * 180 / 25.4)


def test_body_must_be_json_object(client):
    assert_error(client.post("/api/print/text", data="text=hi"), 400, "bad_request")
    assert_error(client.post("/api/print/text", json=["a"]), 400, "bad_request")


# --- print template -----------------------------------------------------------
def preview(client, **body):
    body = {"printer": "die", "label": "12x40", **body}
    return client.post("/api/print/template?preview=1", json=body)


def test_print_template(client, registry):
    response = client.post(
        "/api/print/template",
        json={
            "printer": "die",
            "label": "12x40",
            "template": "freezer",
            "fields": {"Item": "Soup"},
        },
    )
    assert response.status_code == 200, response.get_json()
    assert len(registry.get("die").printed) == 1


def test_template_uses_the_clock(app, client):
    first = preview(client, template="today").data
    app.config["LABELPI_NOW"] = lambda: datetime(2030, 1, 1)
    second = preview(client, template="today").data
    assert first != second  # different date -> different picture


def test_fields_change_the_picture(client):
    soup = preview(client, template="freezer", fields={"Item": "Soup"}).data
    stew = preview(client, template="freezer", fields={"Item": "Beef stew"}).data
    assert soup != stew


def test_preview_unsaved_template_text(client):
    response = preview(client, text="{field:X}\n{date+1w:%d %b}", fields={"X": "hi"})
    assert response.mimetype == "image/png"


@pytest.mark.parametrize(
    "body, status",
    [
        ({"template": "nope"}, 404),
        ({}, 400),  # neither template nor text
        ({"text": "{nope}"}, 400),
        ({"template": "freezer", "fields": ["Soup"]}, 400),
        ({"template": "freezer", "fields": {"Item": 5}}, 400),
        ({"template": "freezer", "fields": {"Item": "x" * 500}}, 400),
    ],
)
def test_print_template_errors(client, body, status):
    assert preview(client, **body).status_code == status


# --- manage templates ------------------------------------------------------------
def test_create_edit_delete_template(client, app):
    created = client.post("/api/templates", json={"name": "Jar", "text": "{field:Contents}"})
    assert created.status_code == 201
    template = created.get_json()
    assert template == {
        "id": "jar",
        "name": "Jar",
        "text": "{field:Contents}",
        "fields": ["Contents"],
    }

    edited = client.put("/api/templates/jar", json={"name": "Big jar", "text": "{date:%Y}"})
    assert edited.get_json()["name"] == "Big jar" and edited.get_json()["fields"] == []

    assert client.delete("/api/templates/jar").status_code == 204
    assert "jar" not in [t["id"] for t in client.get("/api/templates").get_json()]


def test_template_changes_are_saved_to_settings_file(client, tmp_path):
    client.post("/api/templates", json={"name": "Jar", "text": "x"})
    assert (tmp_path / "settings.json").exists()  # next to printers.toml


@pytest.mark.parametrize(
    "body",
    [
        {"text": "x"},  # no name
        {"name": "", "text": "x"},
        {"name": "Bad", "text": "{dat:%Y}"},
        {"name": "No text"},
    ],
)
def test_create_template_errors(client, body):
    assert_error(client.post("/api/templates", json=body), 400, "bad_request")


def test_edit_or_delete_unknown_template(client):
    assert_error(
        client.put("/api/templates/nope", json={"name": "x", "text": "x"}), 404, "not_found"
    )
    assert_error(client.delete("/api/templates/nope"), 404, "not_found")


# --- print image -------------------------------------------------------------
def test_print_image(client, registry):
    response = upload(client)
    assert response.status_code == 200, response.get_json()
    assert len(registry.get("die").printed) == 1


def test_preview_image(client):
    response = upload(client, preview=True, dither="true", invert="false")
    assert response.mimetype == "image/png"
    assert Image.open(io.BytesIO(response.data)).size == (320, 96)


def test_image_on_tape_with_length(client):
    response = upload(client, preview=True, printer="tape", label="tze-12", length_mm="60")
    assert Image.open(io.BytesIO(response.data)).width == round(60 * 180 / 25.4)


def test_image_missing_file(client):
    response = client.post(
        "/api/print/image",
        data={"printer": "die", "label": "12x40"},
        content_type="multipart/form-data",
    )
    assert_error(response, 400, "bad_request")


def test_not_an_image(client):
    assert_error(upload(client, data=b"definitely not a png"), 400, "bad_request")


def test_unsupported_image_format(client):
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buffer, format="TIFF")
    response = upload(client, data=buffer.getvalue())
    assert_error(response, 400, "bad_request")
    assert "TIFF" in response.get_json()["detail"]


def test_bad_bool_field(client):
    assert_error(upload(client, dither="maybe"), 400, "bad_request")


def test_bad_length_field(client):
    assert_error(
        upload(client, printer="tape", label="tze-12", length_mm="long"), 400, "bad_request"
    )


def test_upload_too_large(write_config, tmp_path):
    app = create_app(
        write_config("[server]\nmax_upload_mb = 0.01\n" + MINIMAL_TOML), out_dir=tmp_path
    )
    noise = Image.effect_noise((200, 200), 100)  # noise doesn't compress: ~40 KB PNG
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    assert_error(upload(app.test_client(), data=buffer.getvalue()), 413, "too_large")


# --- printer failures --------------------------------------------------------
@pytest.mark.parametrize(
    "exception, status, error",
    [
        (PrinterUnavailable("D30 not reachable"), 503, "unavailable"),
        (PrinterError("tape jammed"), 500, "printer_error"),
        (RuntimeError("bug"), 500, "printer_error"),
    ],
)
def test_printer_failures(client, registry, monkeypatch, exception, status, error):
    def broken_print(image, label):
        raise exception

    monkeypatch.setattr(registry.get("die"), "print", broken_print)
    response = client.post(
        "/api/print/text", json={"printer": "die", "label": "12x40", "text": "x"}
    )
    assert_error(response, status, error)
    assert not registry.is_busy("die")  # lock released even after a failure


# --- routing -----------------------------------------------------------------
def test_unknown_api_url_is_json_404(client):
    assert_error(client.get("/api/nope"), 404, "not_found")


def test_wrong_method_is_json_405(client):
    assert_error(client.get("/api/print/text"), 405, "method_not_allowed")
