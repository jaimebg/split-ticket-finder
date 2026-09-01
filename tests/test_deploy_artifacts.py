"""The deployment artifacts have to agree with each other.

They were adapted from another bot's copies, and the failure mode of that kind
of edit is a name or a path left pointing at the repository they came from: the
unit starts, runs the wrong checkout or restarts the wrong service, and nothing
says so until a deploy goes somewhere unexpected. Nothing else in the suite
reads these files, so this is the only place that catches it.

The assertions are deliberately about *consistency between* the files rather
than about their literal contents -- renaming the service or moving the
checkout should keep them passing, as long as every file learns about it.
"""

import configparser
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

SERVICE = "split-ticket-finder"
INSTALL_DIR = "/opt/split-ticket-finder"
RUN_AS = "stfbot"


def _unit(name: str) -> configparser.ConfigParser:
    """Parse a unit file. systemd's format is INI with case-sensitive keys."""
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string((DEPLOY / name).read_text())
    return parser


@pytest.fixture(scope="module")
def update_sh() -> str:
    return (DEPLOY / "update.sh").read_text()


def test_every_artifact_is_present():
    assert sorted(p.name for p in DEPLOY.iterdir()) == [
        f"{SERVICE}-update.service",
        f"{SERVICE}-update.timer",
        f"{SERVICE}.service",
        "update.sh",
    ]


def test_update_script_is_executable():
    # systemd's ExecStart= runs the file directly; a non-executable script
    # fails the unit with a bare 203/EXEC that says nothing about why.
    assert (DEPLOY / "update.sh").stat().st_mode & 0o111


def test_update_script_is_valid_bash(update_sh):
    subprocess.run(["bash", "-n", DEPLOY / "update.sh"], check=True)


def test_bot_unit_runs_the_checkout_as_the_service_account():
    service = _unit(f"{SERVICE}.service")["Service"]

    assert service["User"] == RUN_AS
    assert service["WorkingDirectory"] == INSTALL_DIR
    assert service["EnvironmentFile"] == f"{INSTALL_DIR}/.env"
    assert service["ExecStart"] == f"{INSTALL_DIR}/.venv/bin/python bot.py"


def test_bot_unit_can_write_its_database():
    # ProtectSystem=strict makes the whole filesystem read-only, so without an
    # explicit exception the bot cannot open flight_finder.db for writing --
    # and it resolves that path relative to WorkingDirectory.
    service = _unit(f"{SERVICE}.service")["Service"]

    assert service["ProtectSystem"] == "strict"
    assert INSTALL_DIR in service["ReadWritePaths"].split()


def test_update_unit_runs_the_script_from_the_checkout_it_updates():
    service = _unit(f"{SERVICE}-update.service")["Service"]

    assert service["Type"] == "oneshot"
    assert service["ExecStart"] == f"{INSTALL_DIR}/deploy/update.sh"
    # It rewrites that checkout in place, so it needs it writable for the same
    # reason the bot unit does.
    assert INSTALL_DIR in service["ReadWritePaths"].split()


def test_timer_drives_the_update_unit_it_ships_beside():
    # A timer takes its unit from its own filename, so the two names have to
    # stay in step: split-ticket-finder-update.timer runs
    # split-ticket-finder-update.service.
    assert (DEPLOY / f"{SERVICE}-update.timer").exists()
    assert (DEPLOY / f"{SERVICE}-update.service").exists()

    timer = _unit(f"{SERVICE}-update.timer")
    assert "Timer" in timer
    assert timer["Install"]["WantedBy"] == "timers.target"


@pytest.mark.parametrize(
    ("variable", "expected"),
    [
        ("REPO_DIR", INSTALL_DIR),
        ("SERVICE", SERVICE),
        ("RUN_AS", RUN_AS),
        ("BRANCH", "main"),
        ("API_REPO", "jaimebg/split-ticket-finder"),
    ],
)
def test_update_script_defaults_match_the_units(update_sh, variable, expected):
    # Every one of these is overridable through a systemd drop-in, but the
    # default is what runs, and it has to describe *this* deployment.
    assert f'{variable}=${{{variable}:-{expected}}}' in update_sh


def test_update_script_installs_this_project_not_a_requirements_file(update_sh):
    # This repository has no requirements.txt -- it declares its dependencies
    # in pyproject.toml and is installed as a package. A copied
    # `pip install -r requirements.txt` line would fail every deploy.
    assert "requirements.txt" not in update_sh
    assert ".venv/bin/pip install" in update_sh
    assert " -e ." in update_sh


def test_update_script_refuses_a_commit_whose_ci_has_not_passed(update_sh):
    # The whole point of the updater is that a red build never reaches the
    # server; the check defaults on, and a drop-in has to opt out of it.
    assert "REQUIRE_GREEN_CI=${REQUIRE_GREEN_CI:-1}" in update_sh
    assert "--ff-only" in update_sh
