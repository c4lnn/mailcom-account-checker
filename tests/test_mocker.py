from cryptography.fernet import Fernet
import pytest
from curl_cffi import requests as curl_requests

from account_console.db import Database
from account_console.mocker import MockAccount, MockMailService, MockMailState
from account_console.security import SecretBox
from account_console.store import Store
from account_console.worker import run_job
from account_console.api import create_app
from imap_fallback import ImapFolderClearClient
from mailcom_client import MailComClient, MailComError, SETTINGS_CLIENT_ID, SETTINGS_SCOPE
from account_console.security import SessionSigner
from fastapi.testclient import TestClient


def _job(tmp_path, password="OldPassword!", *, folders=None, aliases=None,
         change_password=False, remove_aliases=False, proxies=None, encryption_key=None):
    state = MockMailState({
        "local@example.test": MockAccount(
            password,
            aliases=aliases or [{"address": "local@example.test", "state": "ACTIVE"}],
            folders=folders or {"Drafts": 2, "INBOX": 1, "OUTBOX": 0,
                                "Sent": 0, "Spam": 0, "Trash": 0},
        )
    })
    service = MockMailService(state)
    service.start()
    db = Database(tmp_path / "integration.sqlite3")
    db.initialize()
    box = SecretBox(encryption_key or Fernet.generate_key().decode("ascii"))
    store = Store(db, box)
    store.import_accounts([("local@example.test", password)])
    with db.connect() as connection:
        account_id = connection.execute("SELECT id FROM imported_accounts").fetchone()[0]
    job_id = store.create_job([account_id], {
        "workers": 1, "proxies": proxies or ["http://127.0.0.1:8080"],
        "change_password": change_password, "remove_aliases": remove_aliases,
    })
    return service, store, box, job_id


def _run(service, store, box, job_id):
    def imap_factory(username, password, *, proxy_url):
        return ImapFolderClearClient(
            username, password, proxy_url=proxy_url,
            host="127.0.0.1", port=service.imap_port,
            connection_factory=service.imap_connection_factory,
        )

    def mail_factory(username, password, *, proxy_url):
        return MailComClient(
            username, password, proxy_url="",
            endpoint_overrides=service.endpoint_overrides,
        )

    return run_job(
        job_id,
        endpoint_overrides=service.endpoint_overrides,
        client_factory=imap_factory,
        alias_client_factory=mail_factory,
        transport_probe=lambda _proxy: None,
        exit_lookup=lambda _proxy: {"ip": "127.0.0.1", "country_code": "LO", "country_name": "Local"},
        database=store.db,
        secrets_box=box,
    )


def test_production_protocol_clients_login_clear_and_change_password(tmp_path):
    service, store, box, job_id = _job(tmp_path, change_password=True)
    service.state.password_change_mode = "success"
    try:
        result = _run(service, store, box, job_id)
        assert result[0]["ok"] is True, result[0]
        assert service.state.accounts["local@example.test"].password != "OldPassword!"
        assert service.state.count("password_change") == 1
        assert all(count == 0 for count in service.state.accounts["local@example.test"].folders.values())
        assert store.candidates(email="local@example.test", job_id=job_id)[0]["status"] == "confirmed"
    finally:
        service.close()


def test_committed_password_change_with_lost_response_is_not_retried(tmp_path):
    service, store, box, job_id = _job(tmp_path, change_password=True)
    service.state.password_change_mode = "commit_then_drop"
    try:
        result = _run(service, store, box, job_id)
        assert result[0].get("password_status") == "password_change_uncertain", result[0]
        assert service.state.count("password_change") == 1
        candidate = store.candidates(email="local@example.test", job_id=job_id)[0]
        assert candidate["status"] == "uncertain"
        assert candidate["password"] == service.state.accounts["local@example.test"].password
    finally:
        service.close()


def test_uncommitted_password_change_with_lost_response_is_not_retried(tmp_path):
    service, store, box, job_id = _job(tmp_path, change_password=True)
    service.state.password_change_mode = "drop_before_commit"
    try:
        result = _run(service, store, box, job_id)
        assert result[0].get("password_status") == "password_change_uncertain", result[0]
        assert service.state.count("password_change") == 1
        assert service.state.accounts["local@example.test"].password == "OldPassword!"
        assert store.candidates(email="local@example.test", job_id=job_id)[0]["status"] == "uncertain"
    finally:
        service.close()


def test_mail_client_reports_explicit_password_rejection(tmp_path):
    service, _store, _box, _job_id = _job(tmp_path)
    service.state.password_change_mode = "reject"
    client = MailComClient(
        "local@example.test", "OldPassword!", proxy_url="",
        endpoint_overrides=service.endpoint_overrides,
    )
    try:
        client.login(retries=1)
        try:
            client.change_password("NewPassword!")
        except MailComError as exc:
            assert exc.kind == "session_rejected"
        else:
            raise AssertionError("explicit rejection must not be reported as success")
        assert service.state.count("password_change") == 1
        assert service.state.accounts["local@example.test"].password == "OldPassword!"
    finally:
        client.session.close()
        service.close()


def test_mail_settings_alias_listing_and_removal_are_account_scoped(tmp_path):
    service, _store, _box, _job_id = _job(tmp_path, aliases=[
        {"address": "local@example.test", "state": "ACTIVE"},
        {"address": "alias@example.test", "state": "ACTIVE"},
    ])
    client = MailComClient(
        "local@example.test", "OldPassword!", endpoint_overrides=service.endpoint_overrides,
    )
    try:
        assert "alias@example.test" in [row["address"] for row in client.list_alias_rows()]
        client.remove_alias("alias@example.test")
        assert [row["address"] for row in client.list_alias_rows()] == ["local@example.test"]
        assert service.state.count("alias_remove") == 1
    finally:
        client.session.close()
        service.close()


def test_imap_cleanup_and_recheck_failures_stop_without_replaying_deletes(tmp_path):
    for operation, faults in (("imap_store", ["503"]), ("imap_select", [None, "503"])):
        case_path = tmp_path / operation
        case_path.mkdir()
        service, store, box, job_id = _job(case_path)
        service.state.faults[operation] = list(faults)
        try:
            result = _run(service, store, box, job_id)
            assert result[0]["status"] == "imap_clear_failed"
            assert service.state.count("imap_store") <= 1
            assert service.state.count("imap_expunge") <= 1
            if operation == "imap_store":
                assert service.state.count("imap_store") == 1
                assert service.state.count("imap_expunge") == 0
            else:
                assert service.state.count("imap_store") == 1
                assert service.state.count("imap_expunge") == 1
        finally:
            service.close()


def test_integration_endpoints_and_imap_factory_are_local_only(tmp_path):
    service, store, box, job_id = _job(tmp_path)
    try:
        assert set(service.endpoint_overrides) == {
            "https://www.mail.com", "https://login.mail.com",
            "https://oauthbridge.navigator-lxa.mail.com",
            "https://navigator-lxa.mail.com", "https://maillist.mail.com",
            "https://webmail-cats-live.mail.com", "https://settings-cats.mail.com",
            "https://account-lxa.mail.com",
        }
        assert all(url.startswith("http://127.0.0.1:")
                   for url in service.endpoint_overrides.values())
        _run(service, store, box, job_id)
        assert service.state.count("imap_login") == 1
        assert service.state.count("login") == 1
        assert service.state.count("oauth") >= 1
    finally:
        service.close()


def test_test_network_guard_rejects_external_http_and_socket_egress(block_nonlocal_network):
    import socket
    session = curl_requests.Session()
    try:
        with pytest.raises(AssertionError, match="test HTTP egress blocked"):
            session.get("https://example.com")
        with pytest.raises(AssertionError, match="test network egress blocked"):
            socket.create_connection(("example.com", 443))
        with socket.socket() as connection:
            with pytest.raises(AssertionError, match="test network egress blocked"):
                connection.connect(("203.0.113.1", 993))
        assert len(block_nonlocal_network) == 3
        block_nonlocal_network.clear()  # 本例显式测试阻断；其他用例不能吞掉违规。
    finally:
        session.close()


def test_login_rejection_and_alias_read_failure_are_persisted_without_side_effects(tmp_path):
    for scenario in ("login_rejected", "alias_read_failed"):
        case_path = tmp_path / scenario
        case_path.mkdir()
        service, store, box, job_id = _job(case_path)
        if scenario == "login_rejected":
            service.state.rejected_logins.add("local@example.test")
        else:
            service.state.faults["alias_list"] = ["503"]
        try:
            result = _run(service, store, box, job_id)
            assert result[0]["ok"] is False
            assert store.get_job(job_id)["completed_count"] == 1
            if scenario == "login_rejected":
                assert service.state.count("login") == 1
                assert service.state.count("oauth") == 0
            else:
                assert service.state.count("alias_list") == 1
                assert service.state.count("alias_remove") == 0
                assert service.state.count("password_change") == 0
        finally:
            service.close()


def test_transient_oauth_failure_recovers_on_next_proxy_before_side_effects(tmp_path):
    service, store, box, job_id = _job(tmp_path, proxies=["http://127.0.0.1:8080", "http://127.0.0.1:8081"])
    service.state.faults["oauth"] = ["503"]
    try:
        result = _run(service, store, box, job_id)
        assert result[0]["ok"] is True
        assert service.state.count("oauth") >= 2
        assert store.get_job(job_id)["success_count"] == 1
        assert service.state.count("imap_store") == 2  # 每个非空文件夹仅清理一次。
        assert service.state.count("password_change") == 0
    finally:
        service.close()


def test_explicit_password_rejection_keeps_candidate_and_verified_password(tmp_path):
    service, store, box, job_id = _job(tmp_path, change_password=True)
    service.state.password_change_mode = "reject"
    with store.db.transaction() as connection:
        connection.execute("INSERT INTO successful_accounts(email,password_enc) VALUES (?,?)",
                           ("local@example.test", box.encrypt("PreviouslyVerified!")))
    try:
        results = _run(service, store, box, job_id)
        assert results[0]["ok"] is False
        assert service.state.count("password_change") == 1
        assert store.candidates(job_id=job_id)[0]["status"] == "failed"
        assert store.get_job(job_id)["failure_count"] == 1
        assert service.state.accounts["local@example.test"].password == "OldPassword!"
        with store.db.connect() as connection:
            encrypted = connection.execute("SELECT password_enc FROM successful_accounts").fetchone()[0]
        assert box.decrypt(encrypted) == "PreviouslyVerified!"
    finally:
        service.close()


def test_alias_delete_failure_is_not_replayed(tmp_path):
    service, store, box, job_id = _job(
        tmp_path, aliases=[{"address": "alias@example.test", "state": "ACTIVE"}],
        remove_aliases=True,
    )
    service.state.faults["alias_remove"] = ["503"]
    try:
        result = _run(service, store, box, job_id)
        assert result[0]["ok"] is False
        assert result[0]["removed_count"] == 0
        assert service.state.count("alias_remove") == 1
        assert service.state.accounts["local@example.test"].aliases
    finally:
        service.close()


def test_alias_recheck_failure_after_delete_is_not_replayed(tmp_path):
    service, store, box, job_id = _job(tmp_path,
        aliases=[{"address": "alias@example.test", "state": "ACTIVE"}],
        remove_aliases=True, change_password=True,
        proxies=["http://127.0.0.1:8080", "http://127.0.0.1:8081"])
    service.state.faults["alias_list"] = [None, "503"]
    try:
        results = _run(service, store, box, job_id)
        assert results[0]["ok"] is False
        assert service.state.count("alias_list") == 2
        assert service.state.count("alias_remove") == 1
        assert service.state.count("password_change") == 0
        assert not store.candidates(job_id=job_id)
    finally:
        service.close()


def test_password_submission_timeout_is_uncertain_without_replay(tmp_path):
    service, store, box, job_id = _job(tmp_path, change_password=True)
    service.state.password_change_mode = "commit_then_wait"

    def client(username, password, *, proxy_url):
        return MailComClient(username, password, timeout=0.2, endpoint_overrides=service.endpoint_overrides)

    try:
        run_job(job_id, database=store.db, secrets_box=box,
                imap_connection_factory=service.imap_connection_factory,
                alias_client_factory=client, transport_probe=lambda _: None,
                exit_lookup=lambda _: {"ip": "127.0.0.1", "country_code": "LO", "country_name": "Local"})
        assert service.state.count("password_change") == 1
        assert store.candidates(job_id=job_id)[0]["status"] == "uncertain"
    finally:
        service.close()


@pytest.mark.parametrize("termination", ["kill", "parent_pipe_closed"])
def test_killed_worker_preserves_submitted_candidate_and_verified_password(tmp_path, termination):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    key = Fernet.generate_key().decode()
    service, store, box, job_id = _job(tmp_path, change_password=True, encryption_key=key)
    service.state.password_change_mode = "commit_then_wait"
    with store.db.transaction() as connection:
        connection.execute("INSERT INTO successful_accounts(email,password_enc) VALUES (?,?)",
                           ("local@example.test", box.encrypt("OldPassword!")))
    process_code = r'''
import imaplib, json, os, socket, ssl
from mailcom_client import MailComClient
from ipaddress import ip_address
from urllib.parse import urlsplit
from curl_cffi import requests
from account_console.worker import run_job, watch_parent

watch_parent()

connect = socket.socket.connect
request = requests.Session.request
def guard_connect(self, address):
    if isinstance(address, tuple) and not ip_address(address[0]).is_loopback: os._exit(91)
    return connect(self, address)
def guard_request(self, method, url, **kwargs):
    if not ip_address(urlsplit(str(url)).hostname).is_loopback: os._exit(92)
    kwargs['allow_redirects'] = False
    return request(self, method, url, **kwargs)
socket.socket.connect = guard_connect
requests.Session.request = guard_request
def imap_factory(*args, **kwargs):
    return imaplib.IMAP4_SSL('127.0.0.1', int(os.environ['MOCK_IMAP_PORT']),
                            ssl_context=ssl.create_default_context(cafile=os.environ['MOCK_CERT']))
def mail_factory(username, password, *, proxy_url):
    return MailComClient(username, password, endpoint_overrides=json.loads(os.environ['MOCK_ENDPOINTS']))
run_job(os.environ['MOCK_JOB'], endpoint_overrides=json.loads(os.environ['MOCK_ENDPOINTS']),
        imap_connection_factory=imap_factory,
        alias_client_factory=mail_factory,
        transport_probe=lambda _: None,
        exit_lookup=lambda _: {'ip': '127.0.0.1', 'country_code': 'LO', 'country_name': 'Local'})
'''
    environment = {**os.environ, "ACCOUNT_CHECKER_DB": str(store.db.path),
                   "ACCOUNT_CHECKER_ENCRYPTION_KEY": key, "MOCK_JOB": job_id,
                   "MOCK_CERT": str(service._server_certificate), "MOCK_IMAP_PORT": str(service.imap_port),
                   "MOCK_ENDPOINTS": json.dumps(service.endpoint_overrides)}
    process = subprocess.Popen([sys.executable, "-c", process_code], env=environment,
                               cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert service.state.password_submitted.wait(8), (process.poll(), store.get_job(job_id))
        if termination == "kill":
            process.kill()
        else:
            process.stdin.close()
            process.stdin = None
        process.communicate(timeout=5)
        # 重新打开数据库并执行 API 启动时的恢复流程。
        reopened = Store(Database(store.db.path), box)
        reopened.mark_orphans_interrupted()
        candidate = reopened.candidates(job_id=job_id)[0]
        assert candidate["status"] == "uncertain"
        assert candidate["password"] == service.state.accounts["local@example.test"].password
        assert reopened.get_job(job_id)["status"] == "interrupted"
        assert service.state.count("password_change") == 1
        with reopened.db.connect() as connection:
            encrypted = connection.execute("SELECT password_enc FROM successful_accounts").fetchone()[0]
        assert box.decrypt(encrypted) == "OldPassword!"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        service.close()


def test_api_to_worker_end_to_end_persists_success_failure_and_uncertain_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    monkeypatch.setenv("CONSOLE_PASSWORD", "console-test-password")
    service = MockMailService(MockMailState({
        "good@example.test": MockAccount("GoodPassword!"),
        "bad@example.test": MockAccount("ServerPassword!"),
        "uncertain@example.test": MockAccount("OldPassword!"),
    })).start()
    service.state.password_change_mode = {"uncertain@example.test": "commit_then_drop"}
    database = Database(tmp_path / "api-e2e.sqlite3")
    database.initialize()
    box = SecretBox(Fernet.generate_key().decode("ascii"))

    def launch(job_id):
        def imap_factory(username, password, *, proxy_url):
            return ImapFolderClearClient(
                username, password, proxy_url=proxy_url, host="127.0.0.1",
                port=service.imap_port, connection_factory=service.imap_connection_factory,
            )

        def mail_factory(username, password, *, proxy_url):
            return MailComClient(
                username, password, endpoint_overrides=service.endpoint_overrides,
            )

        run_job(
            job_id, client_factory=imap_factory, alias_client_factory=mail_factory,
            endpoint_overrides=service.endpoint_overrides,
            transport_probe=lambda _proxy: None,
            exit_lookup=lambda _proxy: {"ip": "127.0.0.1", "country_code": "LO", "country_name": "Local"},
            database=database, secrets_box=box,
        )

    app = create_app(
        database, secrets_box=box, signer=SessionSigner("e" * 48),
        launcher=launch, mark_orphans_on_start=False,
    )
    try:
        with TestClient(app) as client:
            headers = {"Origin": "http://testserver"}
            assert client.post("/api/auth/login", json={"password": "console-test-password"},
                               headers=headers).status_code == 200
            imported = client.post("/api/accounts/import", json={"content":
                "uncertain@example.test----OldPassword!\n"
                "good@example.test----GoodPassword!\n"
                "bad@example.test----WrongPassword!"}, headers=headers)
            assert imported.json()["imported"] == 3
            account_ids = [row["id"] for row in client.get("/api/accounts?status=pending").json()["items"]]
            proxy_text = "127.0.0.1:8080\n127.0.0.1:8081\n127.0.0.1:8082\n127.0.0.1:8083"
            assert client.put("/api/proxies", json={"proxies": proxy_text}, headers=headers).status_code == 200
            response = client.post("/api/jobs", json={
                "account_ids": account_ids, "workers": 1,
                "change_password": True, "confirm_mailbox_cleanup": True,
                "confirm_change_password": True,
            }, headers=headers)
            assert response.status_code == 202
            job_id = response.json()["id"]
            job = client.get(f"/api/jobs/{job_id}").json()
            assert job["status"] == "completed"
            assert job["completed_count"] == 3
            assert sum(row["result"]["ok"] is True for row in job["results"]) == 1
            assert [row["email"] for row in client.get("/api/accounts?status=successful").json()["items"]] == ["good@example.test"]
            failed = client.get("/api/accounts?status=failed").json()["items"]
            assert {row["email"] for row in failed} == {"bad@example.test", "uncertain@example.test"}
            candidates = client.get("/api/password-candidates?email=uncertain@example.test").json()["items"]
            assert len(candidates) == 1, (job["results"], service.state.calls)
            assert candidates[0]["status"] == "uncertain"
            assert candidates[0]["password"] == service.state.accounts["uncertain@example.test"].password
            assert service.state.count("password_change") == 2
            assert client.get(f"/api/jobs/{job_id}/logs").json()["items"]
    finally:
        service.close()
