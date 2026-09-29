"""The deploy/ files: shell syntax, placeholders, and what sudo allows.

The scripts themselves are exercised on a real Pi (docs/PI_SETUP.md); these
checks catch typos before they reach it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from conftest import ROOT

DEPLOY = ROOT / "deploy"
SCRIPTS = ["install.sh", "deploy.sh"]
TEMPLATES = ["labelpi.service", "labelpi-deploy.service", "labelpi-deploy.timer", "labelpi.sudoers"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse(name):
    result = subprocess.run(["bash", "-n", str(DEPLOY / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="no exec bit on Windows")
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_executable(name):
    assert os.access(DEPLOY / name, os.X_OK)


@pytest.mark.parametrize("name", TEMPLATES)
def test_templates_only_use_known_placeholders(name):
    text = (DEPLOY / name).read_text(encoding="utf-8")
    assert set(re.findall(r"@[A-Z]+@", text)) <= {"@USER@", "@DIR@"}
    assert "labelpi" in text


def test_sudoers_only_allows_restarting_labelpi():
    rules = [
        line
        for line in (DEPLOY / "labelpi.sudoers").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules == ["@USER@ ALL=(root) NOPASSWD: /usr/bin/systemctl restart labelpi"]


def test_install_script_installs_every_template():
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    for name in TEMPLATES:
        assert name in install, name


def test_deploy_script_restarts_exactly_what_sudoers_allows():
    deploy = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
    assert "sudo -n systemctl restart labelpi\n" in deploy
