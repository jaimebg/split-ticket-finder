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
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CI = ROOT / ".github" / "workflows" / "ci.yml"

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


@pytest.mark.parametrize(
    ("variable", "expected"),
    [
        ("REPO_DIR", INSTALL_DIR),
        ("SERVICE", SERVICE),
        ("RUN_AS", RUN_AS),
        ("BRANCH", "main"),
        ("API_REPO", "jaimebg/split-ticket-finder"),
        ("DEPLOY_CHECK", "deploy"),
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


@pytest.fixture(scope="module")
def ci_yml() -> str:
    return CI.read_text()


def _job(ci_yml: str, job: str) -> str:
    """The body of one top-level job in ci.yml, up to the next job."""
    match = re.search(rf"^  {job}:\n((?:    .*\n|\n)*)", ci_yml, re.M)
    assert match, f"no {job} job in ci.yml"
    return match.group(1)


def test_deploy_job_runs_only_after_the_tests_on_main(ci_yml):
    deploy = _job(ci_yml, "deploy")

    assert "needs: test" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "github.event_name != 'pull_request'" in deploy


def test_deploy_job_name_is_the_check_the_updater_skips(ci_yml, update_sh):
    # The updater checks CI while the deploy job that started it is still
    # running. It skips that one check run by name; if the two drift apart,
    # every push-triggered deploy waits on itself and never ships.
    name = re.search(r"^    name: (\S+)$", _job(ci_yml, "deploy"), re.M).group(1)
    assert f"DEPLOY_CHECK=${{DEPLOY_CHECK:-{name}}}" in update_sh


def _ci_is_green(tmp_path, runs) -> bool:
    """Run update.sh's ci_is_green against a stubbed GitHub API response."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f"#!/bin/sh\ncat <<'JSON'\n{json.dumps({'check_runs': runs})}\nJSON\n")
    curl.chmod(0o755)

    # Everything but the trailing `main "$@"`, so the functions load without
    # running a deploy.
    script = (DEPLOY / "update.sh").read_text().replace('\nmain "$@"\n', "\n")
    result = subprocess.run(
        ["bash", "-c", f'{script}\nci_is_green deadbeef'],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
    )
    return result.returncode == 0


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    ("runs", "green"),
    [
        ([_run("test (3.12)"), _run("deploy", "in_progress", None)], True),
        ([_run("test (3.12)", conclusion="failure"), _run("deploy", "in_progress", None)], False),
        ([_run("test (3.12)", "in_progress", None)], False),
        ([_run("deploy", "in_progress", None)], False),
        ([], False),
    ],
    ids=["tests-pass", "tests-fail", "tests-running", "only-deploy", "no-runs"],
)
def test_ci_is_green_ignores_only_the_running_deploy_job(tmp_path, runs, green):
    assert _ci_is_green(tmp_path, runs) is green


def test_env_example_parses_and_documents_the_risk_thresholds():
    """config.validate() points operators at .env.example for every setting;
    it must parse cleanly and list the ones this code reads."""
    from dotenv import dotenv_values

    path = DEPLOY.parent / ".env.example"
    lines = [ln for ln in path.read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert all("=" in ln for ln in lines), [ln for ln in lines if "=" not in ln]
    values = dotenv_values(path)
    assert values["RISK_HIGH_BELOW_HOURS"] == "2"
    assert values["RISK_MEDIUM_BELOW_HOURS"] == "4"
    assert (values["PRICE_HISTORY_DAYS"], values["SPARK_POINTS"],
            values["ALERT_MIN_CHECKS"]) == ("30", "20", "5")
    assert not [ln for ln in lines if ln.startswith("`")], "plan text pasted into the template"
