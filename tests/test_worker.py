from cryptography.fernet import Fernet

from account_console.db import Database
from account_console.security import SecretBox
from account_console.store import Store
from account_console.worker import run_job
from check_and_clean_accounts import CLEAR_FOLDERS
from mailcom_client import MailComError


class FakeImapClient:
    def __init__(self, username, password, *, proxy_url):
        self.username = username
        self.password = password
        self.proxy_url = proxy_url

    def set_event_handler(self, handler):
        self.handler = handler

    def connect(self):
        self.handler({"stage": "登录", "status": "成功"})

    def clear_folders(self, folders):
        assert tuple(folders) == CLEAR_FOLDERS
        return {folder: {"before": 0, "after": 0} for folder in folders}

    def close(self):
        pass


class FakeMailClient:
    def __init__(self, username, password, *, proxy_url, mode="success"):
        self.username = username
        self.password = password
        self.mode = mode
        self.change_calls = 0
        self.server_password = password

    def list_alias_rows(self):
        return [{"address": self.username, "state": "ACTIVE"}]

    def change_password(self, new_password, *, on_submit=None):
        self.change_calls += 1
        on_submit()
        if self.mode == "committed_response_lost":
            self.server_password = new_password
            raise MailComError("response lost", kind="password_change_uncertain")
        if self.mode == "not_committed_response_lost":
            raise MailComError("connection lost", kind="password_change_uncertain")
        if self.mode == "rejected":
            raise MailComError("rejected", kind="bad_credentials", status=401)
        self.server_password = new_password
        return {"ok": True}


def make_job(tmp_path, options):
    database = Database(tmp_path / "worker.sqlite3")
    database.initialize()
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    store = Store(database, box)
    store.import_accounts([("worker@example.com", "OldPassword!")])
    with database.connect() as connection:
        account_id = connection.execute("SELECT id FROM imported_accounts").fetchone()[0]
    job_id = store.create_job([account_id], {
        "workers": 1,
        "proxies": ["http://127.0.0.1:8080"],
        "change_password": True,
        **options,
    })
    return store, job_id, account_id, box


def worker_kwargs(mail_client):
    return {
        "client_factory": FakeImapClient,
        "alias_client_factory": lambda username, password, *, proxy_url: mail_client,
        "transport_probe": lambda _proxy: None,
        "exit_lookup": lambda _proxy: {
            "ip": "127.0.0.1", "country_code": "US", "country_name": "United States"
        },
    }


def test_worker_persists_logs_and_successful_result(tmp_path):
    mail = FakeMailClient("worker@example.com", "OldPassword!", proxy_url="http://local")
    store, job_id, _account_id, box = make_job(tmp_path, {})

    results = run_job(job_id, **worker_kwargs(mail), database=store.db,
                      secrets_box=box, checker=__import__("check_and_clean_accounts").check_pairs_parallel)

    job = store.get_job(job_id)
    assert results[0]["ok"] is True
    assert job["status"] == "completed"
    assert job["completed_count"] == 1
    assert store.accounts("successful")[0]["email"] == "worker@example.com"
    assert any(event["payload"].get("stage") == "登录"
               for event in store.read_logs(job_id))


def test_uncertain_password_change_is_not_retried_and_old_password_survives(tmp_path):
    mail = FakeMailClient("worker@example.com", "OldPassword!", proxy_url="http://local",
                          mode="committed_response_lost")
    store, job_id, account_id, box = make_job(tmp_path, {})
    store.save_result(job_id, {"email": "worker@example.com", "ok": True,
                               "status": "ready"}, account_id)
    store.set_job_status(job_id, "completed")
    job_id = store.create_job([account_id], {
        "workers": 1,
        "proxies": ["http://127.0.0.1:8080"],
        "change_password": True,
    })

    results = run_job(job_id, **worker_kwargs(mail), database=store.db,
                      secrets_box=box, checker=__import__("check_and_clean_accounts").check_pairs_parallel)

    assert mail.change_calls == 1
    assert mail.server_password != "OldPassword!"
    assert results[0]["password_status"] == "password_change_uncertain"
    candidate = store.candidates(email="worker@example.com", job_id=job_id)[0]
    assert candidate["status"] == "uncertain"
    assert candidate["password"] != "OldPassword!"
    with store.db.connect() as connection:
        encrypted = connection.execute(
            "SELECT password_enc FROM successful_accounts WHERE email=?",
            ("worker@example.com",),
        ).fetchone()[0]
    assert box.decrypt(encrypted) == "OldPassword!"


def test_job_creation_rejects_concurrent_queued_job(tmp_path):
    store, first_job, account_id, _box = make_job(tmp_path, {"change_password": False})

    try:
        store.create_job([account_id], {"workers": 1})
    except RuntimeError as exc:
        assert str(exc) == "job_already_running"
    else:
        raise AssertionError("second queued job should be rejected")

    store.set_job_status(first_job, "interrupted")
    assert store.create_job([account_id], {"workers": 1})


def test_restart_marks_in_flight_job_interrupted_and_password_uncertain(tmp_path):
    store, job_id, _account_id, _box = make_job(tmp_path, {})
    candidate_id = store.create_candidate(job_id, "worker@example.com", "CandidatePassword!")
    store.update_candidate(candidate_id, "submitted")

    store.mark_orphans_interrupted()

    assert store.get_job(job_id)["status"] == "interrupted"
    assert store.candidates(job_id=job_id)[0]["status"] == "uncertain"
