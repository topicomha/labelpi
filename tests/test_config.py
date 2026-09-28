from __future__ import annotations

import pytest
from conftest import EXAMPLE_CONFIG, MINIMAL_TOML

from labelpi.config import ConfigError, load_config


def test_example_config_is_valid():
    """The committed example must always load - people copy it."""
    config = load_config(EXAMPLE_CONFIG)
    assert [p.id for p in config.printers] == ["brother", "d30"]
    assert config.printer("brother").label("tze-12").print_height_px == 64
    assert config.printer("d30").label("12x40").continuous is False
    assert config.shortcut("today") is not None


def test_minimal_config_values(config):
    tape = config.printer("tape").label("tze-12")
    assert tape.continuous and tape.print_height_px == 64 and tape.margin_mm == 2.0
    die = config.printer("die").label("12x40")
    assert (die.width_mm, die.length_mm, die.offset_mm, die.margin_mm) == (12.0, 40.0, 0.0, 1.0)
    assert config.printer("die").display_name == "die"  # defaults to the id
    assert config.server.port == 8080  # [server] is optional


def test_lookups_return_none_for_unknown_ids(config):
    assert config.printer("nope") is None
    assert config.printer("tape").label("nope") is None
    assert config.shortcut("nope") is None


def _error(write_config, text: str) -> str:
    with pytest.raises(ConfigError) as info:
        load_config(write_config(text))
    return str(info.value)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path / "missing.toml")


def test_invalid_toml(write_config):
    assert "not valid TOML" in _error(write_config, "[[printers]\nid = ")


def test_no_printers(write_config):
    assert "at least one [[printers]]" in _error(write_config, "[server]\nport = 8080\n")


def test_error_names_file_printer_label_and_field(write_config):
    text = MINIMAL_TOML.replace("length_mm = 40", "")
    message = _error(write_config, text)
    assert "printers.toml" in message
    assert 'printers[1] (id "die")' in message
    assert 'labels[0] (id "12x40")' in message
    assert '"length_mm" is required for a fixed-size label' in message


def test_continuous_label_needs_print_height(write_config):
    text = MINIMAL_TOML.replace("print_height_px = 64", "")
    assert '"print_height_px" is required for a continuous label' in _error(write_config, text)


def test_unknown_printer_type(write_config):
    text = MINIMAL_TOML.replace('type = "phomemo"', 'type = "niimbot"')
    assert '"type" must be one of' in _error(write_config, text)


def test_bad_mac_address(write_config):
    text = MINIMAL_TOML.replace("11:22:33:44:55:66", "11-22-33")
    assert "Bluetooth MAC" in _error(write_config, text)


def test_real_printer_needs_address(write_config):
    text = MINIMAL_TOML.replace('address = "11:22:33:44:55:66"\n', "")
    assert '"address" is required for a "phomemo" printer' in _error(write_config, text)


def test_mock_printer_needs_no_address(write_config):
    config = load_config(
        write_config("""
        [[printers]]
        id = "m"
        type = "mock"
        dpi = 203
          [[printers.labels]]
          id = "a"
          continuous = false
          width_mm = 12
          length_mm = 40
    """)
    )
    assert config.printer("m").address is None


def test_wrong_value_type(write_config):
    text = MINIMAL_TOML.replace("dpi = 180", 'dpi = "180"')
    assert '"dpi" must be a whole number' in _error(write_config, text)


def test_bool_is_not_a_number(write_config):
    text = MINIMAL_TOML.replace("dpi = 180", "dpi = true")
    assert '"dpi" must be a whole number' in _error(write_config, text)


def test_negative_size(write_config):
    text = MINIMAL_TOML.replace("  width_mm = 12", "  width_mm = -12")  # not tape_width_mm
    assert '"width_mm" must be greater than 0' in _error(write_config, text)


def test_duplicate_printer_id(write_config):
    text = MINIMAL_TOML.replace('id = "die"', 'id = "tape"')
    assert 'duplicate printer id "tape"' in _error(write_config, text)


def test_duplicate_label_id_within_a_printer(write_config):
    text = (
        MINIMAL_TOML
        + """
  [[printers.labels]]
  id = "12x40"
  continuous = false
  width_mm = 12
  length_mm = 30
"""
    )
    assert 'duplicate label id "12x40"' in _error(write_config, text)


def test_same_label_id_on_two_printers_is_fine(write_config):
    text = MINIMAL_TOML.replace('id = "tze-12"', 'id = "12x40"')
    config = load_config(write_config(text))
    assert config.printer("tape").label("12x40").continuous


def test_bad_id_characters(write_config):
    text = MINIMAL_TOML.replace('id = "die"', 'id = "my printer"')
    assert "must be letters, digits" in _error(write_config, text)


def test_shortcut_with_unknown_placeholder(write_config):
    text = MINIMAL_TOML.replace("{date:%Y-%m-%d}", "{dat:%Y}")
    message = _error(write_config, text)
    assert 'shortcuts[0] (id "today")' in message
    assert "unknown placeholder" in message


def test_duplicate_shortcut_id(write_config):
    text = MINIMAL_TOML + '\n[[shortcuts]]\nid = "today"\ntext = "x"\n'
    assert 'duplicate shortcut id "today"' in _error(write_config, text)


def test_server_section(write_config):
    config = load_config(write_config("[server]\nport = 9000\nmax_upload_mb = 2\n" + MINIMAL_TOML))
    assert config.server.port == 9000
    assert config.server.max_upload_mb == 2.0
    text = "[server]\nport = 70000\n" + MINIMAL_TOML
    assert '"port" must be at most 65535' in _error(write_config, text)
