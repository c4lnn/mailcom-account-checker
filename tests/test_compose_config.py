import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_compose_selects_mode_specific_volume_and_shared_secrets(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required for deployment configuration validation")
    available = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, check=False,
    )
    if available.returncode:
        pytest.skip("Docker Compose plugin is unavailable")

    root = Path(__file__).resolve().parents[1]
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ACCOUNT_CHECKER_ENCRYPTION_KEY=test-key\n"
        "CONSOLE_PASSWORD=test-password\n"
        "CONSOLE_SESSION_SECRET=01234567890123456789012345678901\n"
        "CONSOLE_COOKIE_SECURE=1\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    for name in (
        "ACCOUNT_CHECKER_ENCRYPTION_KEY", "CONSOLE_PASSWORD", "CONSOLE_SESSION_SECRET",
        "CONSOLE_COOKIE_SECURE", "MAIL_MOCKER_MODE",
    ):
        environment.pop(name, None)
    environment["COMPOSE_PROJECT_NAME"] = "runtime-mode-config-test"

    def render(mode=None):
        env = environment.copy()
        if mode is None:
            env.pop("MAIL_MOCKER_MODE", None)
        else:
            env["MAIL_MOCKER_MODE"] = mode
        result = subprocess.run(
            [docker, "compose", "--project-directory", str(root), "--env-file", str(env_file),
             "-f", str(root / "docker-compose.yml"), "config", "--format", "json"],
            cwd=root, env=env, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    configs = [render(), render("0"), render("1")]
    expected_modes = ["0", "0", "1"]
    volume_names = []
    secret_names = [
        "ACCOUNT_CHECKER_ENCRYPTION_KEY", "CONSOLE_PASSWORD", "CONSOLE_SESSION_SECRET",
    ]
    for config, expected_mode in zip(configs, expected_modes):
        api = config["services"]["api"]
        assert api["environment"]["MAIL_MOCKER_MODE"] == expected_mode
        assert api["volumes"] == [{
            "type": "volume", "source": "account-data", "target": "/data", "volume": {},
        }]
        volume_names.append(config["volumes"]["account-data"]["name"])
        assert api["environment"]["CONSOLE_COOKIE_SECURE"] == "1"
        assert {name: api["environment"][name] for name in secret_names} == {
            "ACCOUNT_CHECKER_ENCRYPTION_KEY": "test-key",
            "CONSOLE_PASSWORD": "test-password",
            "CONSOLE_SESSION_SECRET": "01234567890123456789012345678901",
        }

    assert volume_names[0] == volume_names[1]
    assert volume_names[1] != volume_names[2]
