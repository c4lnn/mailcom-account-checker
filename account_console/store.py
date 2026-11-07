from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .db import Database
from .security import SecretBox


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, database: Database, secrets_box: SecretBox):
        self.db = database
        self.secrets = secrets_box

    def import_accounts(self, rows: list[tuple[str, str]]) -> tuple[int, list[dict[str, Any]]]:
        imported = 0
        errors: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        with self.db.transaction() as connection:
            for line, (email, password) in enumerate(rows, 1):
                normalized = email.strip().lower()
                key = normalized, password
                if key in seen:
                    continue
                seen.add(key)
                connection.execute(
                    "INSERT INTO imported_accounts(email, password_enc) VALUES (?, ?)",
                    (normalized, self.secrets.encrypt(password)),
                )
                imported += 1
        return imported, errors

    def accounts(self, status: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            if status == "successful":
                rows = connection.execute(
                    "SELECT email, updated_at AS created_at FROM successful_accounts "
                    "ORDER BY updated_at DESC LIMIT ?", (limit,),
                ).fetchall()
                return [dict(row, status="successful") for row in rows]
            if status == "failed":
                rows = connection.execute(
                    "SELECT id, email, job_id, error, created_at FROM failed_accounts "
                    "ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
                return [dict(row, status="failed") for row in rows]
            rows = connection.execute(
                "SELECT id, email, created_at FROM imported_accounts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row, status="pending") for row in rows]

    def create_job(self, account_ids: list[int], options: dict[str, Any]) -> str:
        account_ids = list(dict.fromkeys(account_ids))
        job_id = str(uuid4())
        with self.db.transaction() as connection:
            running = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone()
            if running:
                raise RuntimeError("job_already_running")
            placeholders = ",".join("?" for _ in account_ids)
            records = connection.execute(
                f"SELECT id FROM imported_accounts WHERE id IN ({placeholders})", account_ids
            ).fetchall() if account_ids else []
            if len(records) != len(set(account_ids)):
                raise ValueError("account_selection_invalid")
            snapshot = {**options, "account_ids": account_ids}
            connection.execute(
                "INSERT INTO jobs(id,status,options_json,total_count) VALUES (?,?,?,?)",
                (job_id, "queued", self.secrets.encrypt(
                    json.dumps(snapshot, ensure_ascii=False)
                ), len(account_ids)),
            )
        return job_id

    def job_options(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT options_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("job_not_found")
        return json.loads(self.secrets.decrypt(row[0]))

    def append_log(self, job_id: str, payload: dict[str, Any]) -> int:
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO job_logs(job_id,payload_json) VALUES (?,?)",
                (job_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )
            return int(cursor.lastrowid)

    def create_candidate(self, job_id: str, email: str, password: str) -> int:
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO password_change_candidates(job_id,email,password_enc,status) "
                "VALUES (?,?,?,'prepared')",
                (job_id, email.strip().lower(), self.secrets.encrypt(password)),
            )
            return int(cursor.lastrowid)

    def update_candidate(self, candidate_id: int, status: str, error: str | None = None,
                         *, require_uncertain: bool = False) -> None:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT email,password_enc,status FROM password_change_candidates WHERE id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError("candidate_not_found")
            if require_uncertain and row["status"] != "uncertain":
                raise ValueError("candidate_not_uncertain")
            connection.execute(
                "UPDATE password_change_candidates SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, error, candidate_id),
            )
            if status == "confirmed":
                connection.execute(
                    "INSERT INTO successful_accounts(email,password_enc,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(email) DO UPDATE SET password_enc=excluded.password_enc,updated_at=CURRENT_TIMESTAMP",
                    (row["email"], row["password_enc"]),
                )

    def candidates(self, *, email: str = "", job_id: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if email:
            clauses.append("email=?")
            values.append(email.strip().lower())
        if job_id:
            clauses.append("job_id=?")
            values.append(job_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,job_id,email,password_enc,status,error,created_at,updated_at "
                "FROM password_change_candidates" + where + " ORDER BY id DESC", values,
            ).fetchall()
        return [
            {**{key: row[key] for key in row.keys() if key != "password_enc"},
             "password": self.secrets.decrypt(row["password_enc"])}
            for row in rows
        ]

    def save_result(self, job_id: str, result: dict[str, Any], account_id: int) -> None:
        email = str(result.get("email") or "").strip().lower()
        success = result.get("ok") is True
        input_password = None
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT password_enc FROM imported_accounts WHERE id=?", (account_id,)
            ).fetchone()
        if row is None:
            raise KeyError("account_not_found")
        input_password = row[0]
        result_status = str(result.get("status") or ("ready" if success else "failed"))
        visible_result = {key: value for key, value in result.items()
                          if key not in {"new_password", "password", "old_password"}}
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO job_results(job_id,email,status,result_json) VALUES (?,?,?,?)",
                (job_id, email, result_status,
                 json.dumps(visible_result, ensure_ascii=False, separators=(",", ":"))),
            )
            if success:
                password = result.get("new_password")
                password_enc = self.secrets.encrypt(str(password)) if password else input_password
                connection.execute(
                    "INSERT INTO successful_accounts(email,password_enc,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(email) DO UPDATE SET password_enc=excluded.password_enc,updated_at=CURRENT_TIMESTAMP",
                    (email, password_enc),
                )
            else:
                connection.execute(
                    "INSERT INTO failed_accounts(email,password_enc,job_id,error) VALUES (?,?,?,?)",
                    (email, input_password, job_id,
                     str(result.get("error") or result_status)),
                )
            connection.execute(
                "UPDATE jobs SET completed_count=completed_count+1,"
                "success_count=success_count+?,failure_count=failure_count+? WHERE id=?",
                (int(success), int(not success), job_id),
            )

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{key: row[key] for key in row.keys() if key != "options_json"}
                for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("job_not_found")
            results = connection.execute(
                "SELECT id,email,status,result_json,created_at FROM job_results "
                "WHERE job_id=? ORDER BY id", (job_id,),
            ).fetchall()
        options = json.loads(self.secrets.decrypt(job["options_json"]))
        public_options = {key: value for key, value in options.items()
                          if key not in {"proxies", "ipinfo_key", "account_ids"}}
        value = {key: job[key] for key in job.keys() if key != "options_json"}
        value["options"] = public_options
        value["results"] = [
            {**dict(row), "result": json.loads(row["result_json"])} for row in results
        ]
        return value

    def read_logs(self, job_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,payload_json,created_at FROM job_logs WHERE job_id=? AND id>? "
                "ORDER BY id LIMIT ?", (job_id, after_id, limit),
            ).fetchall()
        return [{"id": row["id"], "payload": json.loads(row["payload_json"]),
                 "created_at": row["created_at"]} for row in rows]

    def dashboard(self) -> dict[str, Any]:
        with self.db.connect() as connection:
            success = connection.execute("SELECT COUNT(*) FROM successful_accounts").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM failed_accounts").fetchone()[0]
            latest = connection.execute("SELECT id,status,created_at,completed_count,total_count FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
            active = connection.execute("SELECT id,status,created_at,completed_count,total_count FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone()
        return {"successful_count": success, "failed_attempt_count": failed,
                "latest_job": dict(latest) if latest else None,
                "active_job": dict(active) if active else None}

    def mark_orphans_interrupted(self, job_id: str | None = None) -> None:
        scope = " AND id=?" if job_id else ""
        candidate_scope = " AND job_id=?" if job_id else ""
        parameters = (job_id,) if job_id else ()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status='interrupted',finished_at=CURRENT_TIMESTAMP,"
                "error='worker_interrupted' WHERE status IN ('queued','running')" + scope,
                parameters,
            )
            connection.execute(
                "UPDATE password_change_candidates SET status='uncertain',"
                "error='worker_interrupted',updated_at=CURRENT_TIMESTAMP WHERE status='submitted'" + candidate_scope,
                parameters,
            )
            connection.execute(
                "UPDATE password_change_candidates SET status='unapplied',"
                "error='worker_interrupted_before_submit',updated_at=CURRENT_TIMESTAMP WHERE status='prepared'" + candidate_scope,
                parameters,
            )

    def set_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status=?,error=?,started_at=CASE WHEN ?='running' THEN CURRENT_TIMESTAMP ELSE started_at END,"
                "finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN CURRENT_TIMESTAMP ELSE finished_at END "
                "WHERE id=?",
                (status, error, status, status, job_id),
            )

    def get_setting(self, key: str) -> str | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT value_enc,is_secret FROM settings WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        return self.secrets.decrypt(row["value_enc"])

    def set_setting(self, key: str, value: str, is_secret: bool = False) -> None:
        encrypted = self.secrets.encrypt(value)
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(key,value_enc,is_secret) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc,"
                "is_secret=excluded.is_secret,updated_at=CURRENT_TIMESTAMP",
                (key, encrypted, int(is_secret)),
            )


    def remove_job_defaults(self) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM settings WHERE key IN ('workers','verbose','remove_aliases','change_password')"
            )
    def settings(self) -> dict[str, str]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT key,value_enc FROM settings ORDER BY key").fetchall()
        return {row["key"]: self.secrets.decrypt(row["value_enc"]) for row in rows}

    def selected_accounts(self, account_ids: list[int]) -> list[dict[str, Any]]:
        if not account_ids:
            return []
        placeholders = ",".join("?" for _ in account_ids)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT id,email,password_enc FROM imported_accounts WHERE id IN ({placeholders}) "
                "ORDER BY id", account_ids,
            ).fetchall()
        return [{"id": row["id"], "email": row["email"],
                 "password": self.secrets.decrypt(row["password_enc"])} for row in rows]
