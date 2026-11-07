import sqlite3

import pytest

from account_console.db import Database


def test_initialize_creates_schema_and_enables_foreign_keys(tmp_path):
    database = Database(tmp_path / "console.sqlite3")

    database.initialize()

    with database.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "imported_accounts",
            "successful_accounts",
            "failed_accounts",
            "jobs",
            "job_results",
            "job_logs",
            "settings",
            "password_change_candidates",
        } <= names
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_job_child_rows_cannot_reference_missing_job(tmp_path):
    database = Database(tmp_path / "console.sqlite3")
    database.initialize()

    with database.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_logs(job_id, payload_json) VALUES ('missing', '{}')"
            )
