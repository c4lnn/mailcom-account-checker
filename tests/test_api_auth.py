import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

os.environ.setdefault("CONSOLE_PASSWORD", "console-test-password")
os.environ.setdefault("CONSOLE_SESSION_SECRET", "s" * 48)
os.environ.setdefault("CONSOLE_COOKIE_SECURE", "0")
os.environ.setdefault("ACCOUNT_CHECKER_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

from account_console.api import create_app
from account_console.db import Database
from account_console.security import SecretBox, SessionSigner


def make_app(tmp_path):
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    app = create_app(
        Database(tmp_path / "api.sqlite3"),
        secrets_box=box,
        signer=SessionSigner("t" * 48),
        launcher=lambda _job_id: None,
        mark_orphans_on_start=False,
    )
    return app, box


def test_private_api_requires_login_and_import_does_not_return_password(tmp_path):
    app, box = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/dashboard").status_code == 401

        response = client.post("/api/auth/login", json={"password": "console-test-password"},
                               headers={"Origin": "http://testserver"})
        assert response.status_code == 200
        assert response.headers["set-cookie"].startswith("session=")

        response = client.post(
            "/api/accounts/import",
            json={"content": "one@example.com----KnownPassword!\nmalformed"},
            headers={"Origin": "http://testserver"},
        )
        assert response.json() == {
            "imported": 1,
            "errors": [{"line": 2, "error": "account_format"}],
        }
        items = client.get("/api/accounts?status=pending").json()["items"]
        assert items[0]["email"] == "one@example.com"
        assert "password" not in items[0]

        with app.state.store.db.connect() as connection:
            stored = connection.execute(
                "SELECT password_enc FROM imported_accounts"
            ).fetchone()[0]
        assert b"KnownPassword!" not in stored
        assert box.decrypt(stored) == "KnownPassword!"


@pytest.mark.parametrize(("secure", "expected"), [("0", False), ("1", True)])
def test_login_cookie_secure_attribute_matches_deployment_mode(tmp_path, monkeypatch, secure, expected):
    monkeypatch.setenv("CONSOLE_COOKIE_SECURE", secure)
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"password": "console-test-password"},
                               headers={"Origin": "http://testserver"})
    cookie = response.headers["set-cookie"].lower()
    assert response.status_code == 200
    assert ("; secure" in cookie) is expected


def test_mutations_require_same_origin_and_destructive_confirmations(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "console-test-password"},
                    headers={"Origin": "http://testserver"})
        client.post("/api/accounts/import", json={"content": "one@example.com----pw"},
                    headers={"Origin": "http://testserver"})
        account_id = client.get("/api/accounts?status=pending").json()["items"][0]["id"]

        response = client.post(
            "/api/jobs",
            json={"account_ids": [account_id]},
            headers={"Origin": "http://attacker.invalid"},
        )
        assert response.status_code == 403

        response = client.post(
            "/api/jobs",
            json={"account_ids": [account_id]},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "mailbox_cleanup_confirmation_required"

        payload = {
            "account_ids": [account_id],
            "confirm_mailbox_cleanup": True, "remove_aliases": True,
        }
        response = client.post("/api/jobs", json=payload,
                               headers={"Origin": "http://testserver"})
        assert response.status_code == 422
        assert response.json()["detail"] == "alias_removal_confirmation_required"

        payload["remove_aliases"] = False
        payload["change_password"] = True
        response = client.post("/api/jobs", json=payload,
                               headers={"Origin": "http://testserver"})
        assert response.status_code == 422
        assert response.json()["detail"] == "password_change_confirmation_required"
        assert app.state.store.list_jobs() == []


def test_password_candidate_requires_auth_and_can_be_verified(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        store = app.state.store
        store.import_accounts([("candidate@example.com", "OldPassword!")])
        with store.db.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM imported_accounts"
            ).fetchone()[0]
        job_id = store.create_job([account_id], {"workers": 1})
        candidate_id = store.create_candidate(job_id, "candidate@example.com", "CandidatePassword!")
        store.update_candidate(candidate_id, "submitted")
        store.update_candidate(candidate_id, "uncertain", "response_lost")

        assert client.get("/api/password-candidates?email=candidate@example.com").status_code == 401
        client.post("/api/auth/login", json={"password": "console-test-password"},
                    headers={"Origin": "http://testserver"})
        response = client.get("/api/password-candidates?job_id=" + job_id)
        assert response.status_code == 200
        assert response.json()["items"][0]["password"] == "CandidatePassword!"
        assert response.json()["items"][0]["status"] == "uncertain"

        response = client.post(
            f"/api/password-candidates/{candidate_id}/verify",
            json={"applied": True},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 200
        successful = store.accounts("successful")
        assert successful[0]["email"] == "candidate@example.com"
        assert client.post(f"/api/password-candidates/{candidate_id}/verify",
                           json={"applied": False}, headers={"Origin": "http://testserver"}).status_code == 409


def test_in_flight_candidate_cannot_be_manually_verified(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        store = app.state.store
        store.import_accounts([("candidate@example.test", "pw")])
        job_id = store.create_job([store.accounts("pending")[0]["id"]], {})
        candidate = store.create_candidate(job_id, "candidate@example.test", "new")
        store.update_candidate(candidate, "submitted")
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        assert client.post(f"/api/password-candidates/{candidate}/verify",
                           json={"applied": True}, headers=headers).status_code == 409
        assert not store.accounts("successful")


def test_job_start_uses_global_proxy_snapshot_and_rejects_second_job(tmp_path):
    launched = []
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    app = create_app(
        Database(tmp_path / "jobs.sqlite3"), secrets_box=box,
        signer=SessionSigner("j" * 48), launcher=launched.append,
        mark_orphans_on_start=False,
    )
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        client.post("/api/accounts/import", json={"content": "job@example.com----pw"}, headers=headers)
        account_id = client.get("/api/accounts?status=pending").json()["items"][0]["id"]
        app.state.store.set_setting("proxies", "http://127.0.0.1:8080", is_secret=True)
        payload = {"account_ids": [account_id], "workers": 1, "confirm_mailbox_cleanup": True}

        rejected = client.post("/api/jobs", json={**payload, "proxies": "http://evil:1"}, headers=headers)
        assert rejected.status_code == 422
        first = client.post("/api/jobs", json=payload, headers=headers)
        assert first.status_code == 202
        first_id = first.json()["id"]
        assert launched == [first_id]
        first_options = app.state.store.job_options(first_id)
        assert first_options["proxies"] == ["http://127.0.0.1:8080"]
        assert first_options["workers"] == 1

        assert client.post("/api/jobs", json=payload, headers=headers).status_code == 409
        app.state.store.set_job_status(first_id, "interrupted")
        app.state.store.set_setting("proxies", "http://127.0.0.1:8081", is_secret=True)
        second = client.post("/api/jobs", json={**payload, "workers": 2, "verbose": True}, headers=headers)
        assert second.status_code == 202
        second_options = app.state.store.job_options(second.json()["id"])
        assert second_options["proxies"] == ["http://127.0.0.1:8081"]
        assert second_options["workers"] == 2
        assert second_options["verbose"] is True
        assert first_options["proxies"] == ["http://127.0.0.1:8080"]


def test_job_requires_global_proxy_configuration(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        client.post("/api/accounts/import", json={"content": "one@example.com----pw"}, headers=headers)
        account_id = client.get("/api/accounts?status=pending").json()["items"][0]["id"]
        response = client.post("/api/jobs", json={
            "account_ids": [account_id], "confirm_mailbox_cleanup": True,
        }, headers=headers)
        assert response.status_code == 422
        assert response.json()["detail"] == "proxy_file_empty"
        assert app.state.store.list_jobs() == []
def test_sse_replays_only_events_after_last_event_id(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "console-test-password"},
                    headers={"Origin": "http://testserver"})
        store = app.state.store
        store.import_accounts([("events@example.com", "pw")])
        with store.db.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM imported_accounts"
            ).fetchone()[0]
        job_id = store.create_job([account_id], {"workers": 1})
        store.append_log(job_id, {"stage": "first", "status": "ok"})
        second_id = store.append_log(job_id, {"stage": "second", "status": "ok"})
        store.set_job_status(job_id, "completed")

        response = client.get(
            f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "1"}
        )
        assert response.status_code == 200
        assert "id: " + str(second_id) in response.text
        assert '"stage": "second"' in response.text
        assert '"stage": "first"' not in response.text
        assert "event: complete" in response.text


def test_blank_ipinfo_key_keeps_existing_secret(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        app.state.store.set_setting("ipinfo_key", "preserve-this-key", is_secret=True)
        response = client.put("/api/settings", json={"ipinfo_key": ""}, headers=headers)
        assert response.status_code == 200
        assert app.state.store.get_setting("ipinfo_key") == "preserve-this-key"


def test_startup_removes_legacy_job_defaults_but_preserves_shared_secrets(tmp_path):
    app, _box = make_app(tmp_path)
    app.state.store.db.initialize()
    for key, value in {
        "workers": "4", "verbose": "true", "remove_aliases": "true",
        "change_password": "true", "proxies": "http://127.0.0.1:8080",
        "ipinfo_key": "keep-this-key",
    }.items():
        app.state.store.set_setting(key, value, is_secret=key in {"proxies", "ipinfo_key"})

    with TestClient(app):
        values = app.state.store.settings()
        assert values == {"proxies": "http://127.0.0.1:8080", "ipinfo_key": "keep-this-key"}

def test_ipinfo_key_reveal_requires_login_and_is_not_cached(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        app.state.store.set_setting("ipinfo_key", "saved-ipinfo-secret", is_secret=True)
        assert client.get("/api/settings/ipinfo-key").status_code == 401
        client.post("/api/auth/login", json={"password": "console-test-password"},
                    headers={"Origin": "http://testserver"})

        settings = client.get("/api/settings")
        assert "saved-ipinfo-secret" not in settings.text
        response = client.get("/api/settings/ipinfo-key")
        assert response.status_code == 200
        assert response.json() == {"ipinfo_key": "saved-ipinfo-secret"}
        assert response.headers["cache-control"] == "no-store, private"


def test_global_proxy_api_is_authenticated_validated_and_write_only(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/proxies").status_code == 401
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        proxy = "http://user:proxy-secret@127.0.0.1:8080"
        assert client.put("/api/proxies", json={"proxies": proxy}, headers=headers).status_code == 200
        response = client.get("/api/proxies")
        assert response.json() == {"configured": True, "count": 1}
        assert "proxy-secret" not in response.text
        assert client.put("/api/proxies", json={"proxies": "invalid"}, headers=headers).status_code == 422
        assert client.put("/api/proxies", json={"proxies": proxy}, headers={
            "Origin": "http://attacker.invalid",
        }).status_code == 403
        assert app.state.store.get_setting("proxies") == proxy

def test_saved_proxies_are_write_only_and_can_be_reused_for_a_job(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/api/auth/login", json={"password": "console-test-password"}, headers=headers)
        proxy = "http://user:proxy-secret@127.0.0.1:8080"
        assert client.put("/api/proxies", json={"proxies": proxy}, headers=headers).status_code == 200
        response = client.get("/api/settings")
        assert response.json() == {"ipinfo_key_configured": False}
        assert "proxy-secret" not in response.text
        proxy_status = client.get("/api/proxies")
        assert proxy_status.json() == {"configured": True, "count": 1}
        client.post("/api/accounts/import", json={"content": "local@example.test----pw"}, headers=headers)
        account = client.get("/api/accounts").json()["items"][0]
        created = client.post("/api/jobs", json={"account_ids": [account["id"]],
                              "confirm_mailbox_cleanup": True}, headers=headers)
        assert created.status_code == 202
        assert app.state.store.job_options(created.json()["id"])["proxies"] == [proxy]
        assert "proxy-secret" not in client.get("/api/jobs/" + created.json()["id"]).text


def test_completed_sse_drains_all_batches_and_preserves_raw_sensitive_logs(tmp_path):
    app, _box = make_app(tmp_path)
    with TestClient(app) as client:
        store = app.state.store
        store.import_accounts([("local@example.test", "pw")])
        job_id = store.create_job([store.accounts("pending")[0]["id"]], {})
        for index in range(603):
            store.append_log(job_id, {"index": index, "password": "raw-password"})
        store.set_job_status(job_id, "completed")
        assert client.get(f"/api/jobs/{job_id}/events").status_code == 401
        assert client.get(f"/api/jobs/{job_id}/logs").status_code == 401
        client.post("/api/auth/login", json={"password": "console-test-password"},
                    headers={"Origin": "http://testserver"})
        response = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "2"})
        assert response.text.count("\nid: ") + response.text.startswith("id: ") == 601
        assert response.text.count("raw-password") == 601
        assert '"index": 602' in response.text
        assert response.text.rstrip().endswith('data: {"status": "completed"}')
