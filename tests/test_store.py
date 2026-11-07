import json
import sqlite3

from cryptography.fernet import Fernet

from account_console.db import Database
from account_console.backup import backup_database
from account_console.security import SecretBox
from account_console.store import Store


def make_store(tmp_path):
    database = Database(tmp_path / "store.sqlite3")
    database.initialize()
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    return Store(database, box), database, box


def import_ids(store, database, rows):
    store.import_accounts(rows)
    with database.connect() as connection:
        return [row[0] for row in connection.execute(
            "SELECT id FROM imported_accounts ORDER BY id"
        )]


def test_failed_attempt_never_replaces_verified_password(tmp_path):
    store, database, box = make_store(tmp_path)
    good_id, wrong_id = import_ids(
        store, database,
        [("same@example.com", "VerifiedPassword!"), ("same@example.com", "WrongPassword!")],
    )
    good_job = store.create_job([good_id], {"workers": 1})
    store.save_result(good_job, {"email": "same@example.com", "ok": True,
                                 "status": "ready"}, good_id)
    store.set_job_status(good_job, "completed")
    failed_job = store.create_job([wrong_id], {"workers": 1})
    store.save_result(failed_job, {"email": "same@example.com", "ok": False,
                                   "status": "bad_credentials", "error": "bad_credentials"},
                      wrong_id)

    with database.connect() as connection:
        verified = connection.execute(
            "SELECT password_enc FROM successful_accounts WHERE email=?",
            ("same@example.com",),
        ).fetchone()[0]
        attempts = connection.execute(
            "SELECT password_enc FROM failed_accounts WHERE job_id=?", (failed_job,)
        ).fetchall()
    assert box.decrypt(verified) == "VerifiedPassword!"
    assert [box.decrypt(row[0]) for row in attempts] == ["WrongPassword!"]


def test_candidate_is_encrypted_queryable_and_only_updates_on_confirmation(tmp_path):
    store, database, box = make_store(tmp_path)
    (account_id,) = import_ids(store, database, [("candidate@example.com", "OldPassword!")])
    job_id = store.create_job([account_id], {"workers": 1})
    store.save_result(job_id, {"email": "candidate@example.com", "ok": True,
                               "status": "ready"}, account_id)
    candidate_id = store.create_candidate(job_id, "candidate@example.com", "NewPassword!")
    store.update_candidate(candidate_id, "submitted")
    store.update_candidate(candidate_id, "uncertain", "response_lost")

    candidate = store.candidates(email="candidate@example.com")[0]
    assert candidate["password"] == "NewPassword!"
    assert candidate["status"] == "uncertain"
    with database.connect() as connection:
        encrypted = connection.execute(
            "SELECT password_enc FROM password_change_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()[0]
        verified = connection.execute(
            "SELECT password_enc FROM successful_accounts WHERE email=?",
            ("candidate@example.com",),
        ).fetchone()[0]
        assert b"NewPassword!" not in encrypted
    assert box.decrypt(verified) == "OldPassword!"

    store.update_candidate(candidate_id, "confirmed")
    with database.connect() as connection:
        verified = connection.execute(
            "SELECT password_enc FROM successful_accounts WHERE email=?",
            ("candidate@example.com",),
        ).fetchone()[0]
    assert box.decrypt(verified) == "NewPassword!"


def test_job_options_and_results_do_not_store_secrets_as_plaintext(tmp_path):
    store, database, _box = make_store(tmp_path)
    (account_id,) = import_ids(store, database, [("safe@example.com", "SourcePassword!")])
    job_id = store.create_job([account_id], {
        "workers": 1,
        "proxies": ["http://user:secret@127.0.0.1:8080"],
        "ipinfo_key": "token-secret",
    })
    store.save_result(job_id, {"email": "safe@example.com", "ok": True,
                               "status": "ready", "new_password": "ChangedPassword!"}, account_id)

    with database.connect() as connection:
        options = connection.execute(
            "SELECT options_json FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        result = connection.execute(
            "SELECT result_json FROM job_results WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert b"secret" not in options
    assert b"token-secret" not in options
    assert "ChangedPassword!" not in result
    assert "new_password" not in json.loads(result)


def test_database_backup_preserves_rows(tmp_path):
    store, database, box = make_store(tmp_path)
    store.import_accounts([("backup@example.com", "BackupPassword!")])
    backup = backup_database(tmp_path / "backup" / "console.sqlite3", database)

    with sqlite3.connect(backup) as connection:
        encrypted = connection.execute(
            "SELECT password_enc FROM imported_accounts"
        ).fetchone()[0]
    assert box.decrypt(encrypted) == "BackupPassword!"
