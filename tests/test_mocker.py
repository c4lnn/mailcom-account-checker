import os
import time
from cryptography.fernet import Fernet
import pytest
from curl_cffi import requests as curl_requests

from account_console.mocker import MAIL_ORIGINS, MockAccount, MockMailService, MockMailState
from account_console.security import SecretBox
from account_console.store import Store
from account_console.worker import run_job
from account_console.db import Database
from account_console.api import create_app
from imap_fallback import ImapFolderClearClient
from mailcom_client import MailComClient, MailComError, SETTINGS_CLIENT_ID, SETTINGS_SCOPE
from account_console.security import SessionSigner
from check_and_clean_accounts import check_pairs_parallel
from imap_fallback import ImapFallbackError
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

@pytest.mark.parametrize("kind", ["proxy_connect", "proxy_timeout"])
def test_proxy_configuration_errors_stop_but_transport_errors_retry(kind):
    from check_and_clean_accounts import _status

    assert _status(ImapFallbackError("proxy_required")) == "proxy_required"
    assert _status(ImapFallbackError(kind)) == "proxy_error"

    proxies = [(1, "http://127.0.0.1:8081"), (2, "http://127.0.0.1:8082")]
    preflights = []
    attempts = []

    class Client:
        def __init__(self, proxy_url):
            self.proxy_url = proxy_url

        def connect(self):
            attempts.append(self.proxy_url)
            if len(attempts) == 1:
                raise ImapFallbackError(kind)

        def clear_folders(self, _folders):
            return {folder: {"after": 0} for folder in (
                "Drafts", "INBOX", "OUTBOX", "Sent", "Spam", "Trash",
            )}

        def close(self):
            pass

    result = check_pairs_parallel(
        [(1, "good@example.test", "password")], workers=1,
        proxy_pool=proxies,
        client_factory=lambda _email, _password, *, proxy_url: Client(proxy_url),
        transport_probe=lambda proxy: preflights.append(proxy),
        exit_lookup=lambda _proxy: {
            "ip": "127.0.0.1", "country_code": "LO", "country_name": "Local",
        },
    )
    assert result[0]["ok"] is True
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    assert len(preflights) == 2


def test_proxy_required_stops_without_consuming_another_candidate():
    proxies = [(1, "http://127.0.0.1:8081"), (2, "http://127.0.0.1:8082")]
    preflights = []

    class Client:
        def connect(self):
            raise ImapFallbackError("proxy_required")

        def close(self):
            pass

    result = check_pairs_parallel(
        [(1, "good@example.test", "password")], workers=1,
        proxy_pool=proxies,
        client_factory=lambda *_args, **_kwargs: Client(),
        transport_probe=lambda proxy: preflights.append(proxy),
        exit_lookup=lambda _proxy: {
            "ip": "127.0.0.1", "country_code": "LO", "country_name": "Local",
        },
    )
    assert result[0]["error"] == "proxy_required"
    assert len(preflights) == 1

def test_manual_mocker_mode_routes_default_worker_locally_and_seeds_synthetic_accounts(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CONSOLE_PASSWORD", "console-test-password")
    monkeypatch.setenv("MAIL_MOCKER_MODE", "1")
    monkeypatch.setenv("ACCOUNT_CHECKER_DB", str(tmp_path / "manual-mocker.sqlite3"))
    monkeypatch.setenv("ACCOUNT_CHECKER_ENCRYPTION_KEY", key)
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    database = Database(tmp_path / "manual-mocker.sqlite3")
    app = create_app(database, secrets_box=SecretBox(key),
                     signer=SessionSigner("m" * 48))
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        assert client.post("/api/auth/login", json={"password": "console-test-password"},
                           headers=headers).status_code == 200
        assert client.get("/api/runtime-config").json() == {"mocker_enabled": True}
        rows = client.get("/api/accounts?status=pending").json()["items"]
        assert {row["email"] for row in rows} == {"good@example.test", "bad@example.test"}
        rejected = client.post("/api/accounts/import", json={
            "content": "real@example.com----secret\n",
        }, headers=headers).json()
        assert rejected == {"imported": 0, "errors": [
            {"line": 1, "error": "mocker_requires_synthetic_account"},
        ]}
        good_id = next(row["id"] for row in rows if row["email"] == "good@example.test")
        response = client.post("/api/jobs", json={
            "account_ids": [good_id], "workers": 1, "confirm_mailbox_cleanup": True,
        }, headers=headers)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job
        assert job["results"][0]["result"]["ok"] is True
        assert job["results"][0]["result"]["proxy_index"] in {1, 2, 3, 4}
        assert not any(event["payload"].get("error") == "proxy_required"
                       for event in client.get(f"/api/jobs/{job_id}/logs").json()["items"] )


def test_seed_plan_is_stable_and_bound_to_account_operation_sequence():
    from account_console.mocker_scenarios import build_plan

    config = {
        "mode": "random", "seed": "seed-42", "operations": ["login", "imap_select"],
        "probability": 1,
    }
    first = build_plan(config, ["B@example.test", "a@example.test", "a@example.test"])
    second = build_plan(config, ["a@example.test", "B@example.test"])
    assert first == second
    assert first["version"] == 1
    assert {item["email"] for item in first["faults"]} == {"a@example.test", "b@example.test"}
    assert {item["operation"] for item in first["faults"]} == {"login", "imap_select"}
    assert {item["sequence"] for item in first["faults"]} == set(range(1, 13))


def test_proxy_fault_plans_are_stable_and_candidate_bound():
    from account_console.mocker_scenarios import build_plan

    config = {
        "mode": "random", "seed": "proxy-seed", "operations": [
            "proxy_preflight", "proxy_transport",
        ], "probability": 1,
    }
    first = build_plan(config, ["b@example.test", "a@example.test"])
    second = build_plan(config, ["a@example.test", "b@example.test"])
    assert first == second
    preflight = [item for item in first["faults"] if item["operation"] == "proxy_preflight"]
    transport = [item for item in first["faults"] if item["operation"] == "proxy_transport"]
    assert {item["candidate"] for item in preflight} == {1, 2, 3, 4}
    assert {(item["email"], item["candidate"]) for item in transport} == {
        (email, candidate) for email in {"a@example.test", "b@example.test"}
        for candidate in range(1, 5)
    }


def test_mocker_proxy_fault_scenarios_retry_exhaust_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MOCKER_MODE", "1")
    monkeypatch.setenv("CONSOLE_PASSWORD", "console-test-password")
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    database = Database(tmp_path / "mocker-proxy.sqlite3")
    key = Fernet.generate_key().decode("ascii")
    app = create_app(database, secrets_box=SecretBox(key),
                     signer=SessionSigner("p" * 48),
                     launcher=lambda job_id: run_job(
                         job_id, database=database, secrets_box=SecretBox(key),
                     ), mark_orphans_on_start=False)

    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        assert client.post("/api/auth/login", json={
            "password": "console-test-password",
        }, headers=headers).status_code == 200
        account = next(row for row in client.get("/api/accounts").json()["items"]
                       if row["email"] == "good@example.test")

        def run_scenario(scenario):
            config = {"mode": "scenario", "scenario": scenario, "seed": "",
                      "operations": [], "probability": 0.1}
            assert client.put("/api/mocker/config", json=config, headers=headers).status_code == 200
            response = client.post("/api/jobs", json={
                "account_ids": [account["id"]], "workers": 1,
                "confirm_mailbox_cleanup": True,
            }, headers=headers)
            assert response.status_code == 202, response.text
            job_id = response.json()["id"]
            for _ in range(200):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in {"completed", "failed", "interrupted"}:
                    break
                time.sleep(0.05)
            assert job["status"] == "completed", job
            logs = client.get(f"/api/jobs/{job_id}/logs").json()["items"]
            return job, logs

        preflight_job, preflight_logs = run_scenario("proxy_preflight_failure")
        assert preflight_job["results"][0]["result"]["ok"] is True
        assert preflight_job["results"][0]["result"]["proxy_index"] == 2
        assert any(item["payload"].get("error") == "proxy_connect" for item in preflight_logs)

        transport_job, transport_logs = run_scenario("proxy_transport_failure")
        assert transport_job["results"][0]["result"]["ok"] is True
        assert transport_job["results"][0]["result"]["proxy_index"] == 2
        assert any(item["payload"].get("error") == "proxy_error" for item in transport_logs)
        plan = transport_job["options"]["mocker_plan"]
        replay = client.post(
            f"/api/jobs/{transport_job['id']}/replay", headers=headers,
        )
        assert replay.status_code == 202, replay.text
        replay_id = replay.json()["id"]
        for _ in range(200):
            replay_job = client.get(f"/api/jobs/{replay_id}").json()
            if replay_job["status"] in {"completed", "failed", "interrupted"}:
                break
            time.sleep(0.05)
        assert replay_job["status"] == "completed", replay_job
        assert replay_job["options"]["mocker_plan"] == plan
        assert replay_job["results"][0]["result"]["proxy_index"] == 2

        exhausted_job, exhausted_logs = run_scenario("proxy_candidates_exhausted")
        assert exhausted_job["results"][0]["result"]["error"] == "proxy_unavailable"
        assert sum(item["payload"].get("operation") == "CONNECT/TLS"
                   and item["payload"].get("status") == "失败"
                   for item in exhausted_logs) == 4


def test_mocker_config_and_job_plan_are_authenticated_persisted_and_replayable(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MOCKER_MODE", "1")
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    database = Database(tmp_path / "mocker-api.sqlite3")
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    launched = []
    app = create_app(database, secrets_box=box, signer=SessionSigner("r" * 48),
                     launcher=launched.append, mark_orphans_on_start=False)
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        assert client.get("/api/mocker/config").status_code == 401
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        config = {
            "mode": "scenario", "scenario": "oauth_temporary_failure",
            "seed": "", "operations": ["login"], "probability": 0.1,
        }
        response = client.put("/api/mocker/config", json=config, headers=headers)
        assert response.status_code == 200
        assert "oauth_temporary_failure" in response.json()["scenarios"]
        account = next(row for row in client.get("/api/accounts").json()["items"]
                       if row["email"] == "good@example.test")
        created = client.post("/api/jobs", json={
            "account_ids": [account["id"]], "confirm_mailbox_cleanup": True,
        }, headers=headers)
        assert created.status_code == 202
        job_id = created.json()["id"]
        saved = client.get(f"/api/jobs/{job_id}").json()
        plan = saved["options"]["mocker_plan"]
        assert plan["scenario"] == "oauth_temporary_failure"
        assert plan["faults"] == [{
            "email": "good@example.test", "operation": "oauth", "sequence": 1, "fault": "503",
        }]
        app.state.store.set_job_status(job_id, "completed")
        replayed = client.post(f"/api/jobs/{job_id}/replay", headers=headers)
        assert replayed.status_code == 202
        replay_id = replayed.json()["id"]
        assert app.state.store.job_options(replay_id)["mocker_plan"] == plan
        app.state.store.set_job_status(replay_id, "completed")
        assert launched == [job_id, replay_id]

        unsupported_id = app.state.store.create_job([account["id"]], {
            "mocker_config": config, "mocker_plan": {**plan, "version": 999},
        })
        app.state.store.set_job_status(unsupported_id, "completed")
        unsupported = client.post(f"/api/jobs/{unsupported_id}/replay", headers=headers)
        assert unsupported.status_code == 422
        assert unsupported.json()["detail"] == "mocker_plan_unavailable"


def test_mocker_configuration_api_is_hidden_outside_test_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MAIL_MOCKER_MODE", raising=False)
    monkeypatch.setenv("CONSOLE_PASSWORD", "console-test-password")
    app = create_app(
        Database(tmp_path / "normal-mode.sqlite3"),
        signer=SessionSigner("n" * 48), launcher=lambda _job_id: None,
        mark_orphans_on_start=False,
    )
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        assert client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers).status_code == 200
        assert client.get("/api/runtime-config").json() == {"mocker_enabled": False}
        assert client.get("/api/mocker/config").status_code == 404
        assert client.put("/api/mocker/config", json={"scenario": "success"}, headers=headers).status_code == 404



def test_invalid_mocker_mode_prevents_api_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MOCKER_MODE", "true")
    database = Database(tmp_path / "invalid-mode.sqlite3")
    app = create_app(
        database, signer=SessionSigner("v" * 48), mark_orphans_on_start=False,
    )
    with pytest.raises(RuntimeError, match="mocker_mode_invalid"):
        with TestClient(app):
            pass
    assert not database.path.exists()

def test_worker_interruption_is_test_only_and_marks_the_job_interrupted(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    db_path = tmp_path / "worker-interruption.sqlite3"
    monkeypatch.setenv("MAIL_MOCKER_MODE", "1")
    monkeypatch.setenv("ACCOUNT_CHECKER_DB", str(db_path))
    monkeypatch.setenv("ACCOUNT_CHECKER_ENCRYPTION_KEY", key)
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", "0")
    app = create_app(Database(db_path), secrets_box=SecretBox(key),
                     signer=SessionSigner("i" * 48), mark_orphans_on_start=False)
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        account = next(row for row in client.get("/api/accounts").json()["items"]
                       if row["email"] == "good@example.test")
        configured = client.put("/api/mocker/config", json={
            "mode": "scenario", "scenario": "worker_interrupted",
        }, headers=headers)
        assert configured.status_code == 200
        response = client.post("/api/jobs", json={
            "account_ids": [account["id"]], "confirm_mailbox_cleanup": True,
        }, headers=headers)
        assert response.status_code == 202
        job_id = response.json()["id"]
        for _ in range(200):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "interrupted":
                break
            time.sleep(0.05)
        assert job["status"] == "interrupted", job
        assert job["options"]["mocker_plan"]["lifecycle"] == "worker_interrupt"


def test_worker_network_guard_blocks_http_proxy_redirect_dns_and_sockets(block_nonlocal_network):
    import socket
    from account_console.worker import _install_local_network_guard

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_request = curl_requests.Session.request
    session = curl_requests.Session()
    class RedirectResponse:
        status_code = 302
        headers = {"Location": "https://example.com/redirected"}

    curl_requests.Session.request = lambda *_args, **_kwargs: RedirectResponse()
    try:
        _install_local_network_guard()
        with pytest.raises(RuntimeError, match="mocker_network_egress_blocked"):
            session.get("http://127.0.0.1:1/start")
        with pytest.raises(RuntimeError, match="mocker_network_egress_blocked"):
            session.get("https://example.com")
        with pytest.raises(RuntimeError, match="mocker_network_egress_blocked"):
            session.get("http://127.0.0.1:1", proxies={"http": "http://example.com:8080"})
        with pytest.raises(AssertionError, match="test network egress blocked"):
            socket.create_connection(("203.0.113.10", 993), timeout=0.1)
        with pytest.raises(AssertionError, match="test network egress blocked"):
            socket.create_connection(("example.com", 443), timeout=0.1)
        block_nonlocal_network.clear()
        with socket.socket() as connection:
            with pytest.raises(RuntimeError, match="mocker_network_egress_blocked"):
                connection.connect(("203.0.113.10", 993))
        assert not block_nonlocal_network
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.getaddrinfo = original_getaddrinfo
        curl_requests.Session.request = original_request
        session.close()


@pytest.mark.parametrize(("target", "expected_error"), [
    ({}, "mocker_configuration_missing"),
    ({origin: "https://mail.com" for origin in MAIL_ORIGINS},
     "mocker_target_not_local"),
])
def test_worker_rejects_missing_or_nonlocal_mocker_targets_before_network(
        tmp_path, monkeypatch, target, expected_error):
    service, store, box, job_id = _job(tmp_path)
    monkeypatch.setenv("MAIL_MOCKER_MODE", "1")
    monkeypatch.setenv("MAIL_MOCKER_IMAP_HOST", "127.0.0.1")
    monkeypatch.setenv("MAIL_MOCKER_IMAP_PORT", str(service.imap_port))
    monkeypatch.setenv("MAIL_MOCKER_CERT", str(service._server_certificate))
    monkeypatch.delenv("MAIL_API_ENDPOINT_OVERRIDES", raising=False)
    try:
        with pytest.raises(RuntimeError, match=expected_error):
            run_job(job_id, database=store.db, secrets_box=box, endpoint_overrides=target)
        job = store.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error"] == expected_error
        assert any(event["payload"].get("error") == expected_error
                   for event in store.read_logs(job_id))
    finally:
        service.close()
