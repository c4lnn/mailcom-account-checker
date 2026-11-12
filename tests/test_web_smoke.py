"""构建后运行：RUN_WEB_SMOKE=1 python -m pytest tests/test_web_smoke.py -q。"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

from cryptography.fernet import Fernet
import httpx
import pytest
import uvicorn

from account_console.api import create_app
from account_console.db import Database
from account_console.mocker import MockAccount, MockMailService, MockMailState
from account_console.security import SecretBox, SessionSigner
from account_console.worker import run_job
from mailcom_client import MailComClient


pytestmark = pytest.mark.skipif(os.environ.get("RUN_WEB_SMOKE") != "1", reason="需要先构建 Next.js，再显式运行跨服务烟测")


def local_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_next_api_worker_local_mail_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MOCKER_MODE", "0")
    monkeypatch.setenv("CONSOLE_PASSWORD", "console-test-password")
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    root = Path(__file__).resolve().parents[1]
    api_port, web_port = local_port(), local_port()
    box = SecretBox(Fernet.generate_key().decode())
    database = Database(tmp_path / "web-smoke.sqlite3")
    workers = []
    service = MockMailService(MockMailState({
        "uncertain@example.test": MockAccount("Old!"),
        "good@example.test": MockAccount("Good!"),
        "bad@example.test": MockAccount("Server!"),
    })).start()
    service.state.password_change_mode = {"uncertain@example.test": "commit_then_drop"}

    def launch(job_id):
        def execute():
            run_job(job_id, database=database, secrets_box=box,
                    imap_connection_factory=service.imap_connection_factory,
                    alias_client_factory=lambda user, password, **_: MailComClient(user, password, endpoint_overrides=service.endpoint_overrides),
                    transport_probe=lambda _: None,
                    exit_lookup=lambda _: {"ip": "127.0.0.1", "country_code": "LO", "country_name": "Local"})
        worker = threading.Thread(target=execute)
        workers.append(worker)
        worker.start()

    app = create_app(database, secrets_box=box, signer=SessionSigner("w" * 48), launcher=launch)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error"))
    api_thread = threading.Thread(target=server.run)
    api_thread.start()
    environment = {**os.environ, "API_INTERNAL_URL": f"http://127.0.0.1:{api_port}",
                   "NEXT_TELEMETRY_DISABLED": "1", "NODE_ENV": "production",
                   "HOSTNAME": "127.0.0.1", "PORT": str(web_port)}
    if os.name == "nt":
        environment["NODE_OPTIONS"] = "--require=" + str(root / ".node-userinfo-polyfill.cjs")
    output = (tmp_path / "next.log").open("w", encoding="utf-8")
    process = subprocess.Popen([shutil.which("node"), ".next/standalone/server.js"],
                               cwd=root / "web", env=environment, stdout=output, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{web_port}", timeout=15, trust_env=False) as client:
            for _ in range(100):
                try:
                    if server.started and client.get("/login").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("本地 Web/API 未就绪")
            paths = ["dashboard", "current-task", "accounts", "logs", "settings"]
            for path in paths:
                response = client.get("/" + path)
                assert response.status_code in {200, 307}
                assert "/login" in (response.headers.get("location", "") + response.text)
            assert client.get("/api/settings").status_code == 401
            headers = {"Origin": str(client.base_url).rstrip("/")}
            login = client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
            assert login.status_code == 200, login.text
            assert "httponly" in login.headers["set-cookie"].lower()
            assert client.get("/api/runtime-config").json() == {"mocker_enabled": False}
            assert client.get("/mocker").status_code == 404
            for path in paths:
                response = client.get("/" + path)
                assert response.status_code == 200
                assert "主导航" in response.text
            imported = client.post("/api/accounts/import", json={"content":
                "uncertain@example.test----Old!\ngood@example.test----Good!\nbad@example.test----Wrong!"}, headers=headers)
            assert imported.json()["imported"] == 3
            ids = [row["id"] for row in client.get("/api/accounts").json()["items"]]
            proxy_text = "127.0.0.1:8080\n127.0.0.1:8081\n127.0.0.1:8082\n127.0.0.1:8083"
            assert client.put("/api/proxies", json={"proxies": proxy_text}, headers=headers).status_code == 200
            created = client.post("/api/jobs", json={"account_ids": ids, "workers": 1,
                "change_password": True, "confirm_mailbox_cleanup": True}, headers=headers)
            assert created.status_code == 202
            job_id = created.json()["id"]
            with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
                assert response.status_code == 200
                lines = list(response.iter_lines())
            assert "event: complete" in lines
            event_ids = [int(line[4:]) for line in lines if line.startswith("id: ")]
            replay = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": str(event_ids[-2])})
            assert replay.text.count("id: ") == 1
            assert client.get(f"/api/jobs/{job_id}").json()["completed_count"] == 3
            assert len(client.get("/api/accounts?status=successful").json()["items"]) == 1
            assert len(client.get("/api/accounts?status=failed").json()["items"]) == 2
            candidate = client.get(f"/api/password-candidates?job_id={job_id}").json()["items"]
            uncertain = next(row for row in candidate if row["email"] == "uncertain@example.test")
            assert uncertain["status"] == "uncertain"
            assert uncertain["password"] == service.state.accounts["uncertain@example.test"].password
            assert service.state.count("password_change") == 2
            client.cookies.clear()
            assert client.get(f"/api/jobs/{job_id}/logs").status_code == 401
        reopened = Database(database.path)
        with reopened.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM job_results").fetchone()[0] == 3
            assert connection.execute("SELECT COUNT(*) FROM job_logs").fetchone()[0] > 3
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.should_exit = True
        api_thread.join(timeout=10)
        for worker in workers:
            worker.join(timeout=10)
        service.close()
        output.close()
