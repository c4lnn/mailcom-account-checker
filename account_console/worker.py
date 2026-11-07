from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Callable

from .db import Database
from .security import SecretBox
from .store import Store


def run_job(
    job_id: str,
    *,
    checker: Callable[..., list[dict[str, Any]]] | None = None,
    client_factory: Callable[..., Any] | None = None,
    alias_client_factory: Callable[..., Any] | None = None,
    transport_probe: Callable[[str], None] | None = None,
    exit_lookup: Callable[[str], dict[str, Any]] | None = None,
    endpoint_overrides: dict[str, str] | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    imap_connection_factory: Callable[..., Any] | None = None,
    database: Database | None = None,
    secrets_box: SecretBox | None = None,
) -> list[dict[str, Any]]:
    if checker is None:
        import check_and_clean_accounts as checker_module

        checker = checker_module.check_pairs_parallel
        if client_factory is None:
            def client_factory(username: str, password: str, *, proxy_url: str):
                from imap_fallback import ImapFolderClearClient

                kwargs: dict[str, Any] = {"proxy_url": proxy_url}
                if imap_host is not None:
                    kwargs["host"] = imap_host
                if imap_port is not None:
                    kwargs["port"] = imap_port
                if imap_connection_factory is not None:
                    kwargs["connection_factory"] = imap_connection_factory
                return ImapFolderClearClient(username, password, **kwargs)
        if alias_client_factory is None:
            from mailcom_client import MailComClient

            configured_endpoints = endpoint_overrides
            if configured_endpoints is None:
                raw = os.environ.get("MAIL_API_ENDPOINT_OVERRIDES", "")
                configured_endpoints = json.loads(raw) if raw else {}

            def alias_client_factory(username: str, password: str, *, proxy_url: str):
                return MailComClient(
                    username, password, proxy_url=proxy_url,
                    endpoint_overrides=configured_endpoints,
                )

    db = database or Database()
    db.initialize()
    store = Store(db, secrets_box or SecretBox())
    options = store.job_options(job_id)
    accounts = store.selected_accounts(options["account_ids"])
    if not accounts:
        store.set_job_status(job_id, "failed", "job_has_no_accounts")
        raise ValueError("job_has_no_accounts")

    proxies = options.get("proxies") or []
    proxy_pool = [(index, proxy) for index, proxy in enumerate(proxies, 1)]
    pairs = [(index, row["email"], row["password"])
             for index, row in enumerate(accounts, 1)]
    account_ids = {index: row["id"] for index, row in enumerate(accounts, 1)}
    store.set_job_status(job_id, "running")
    store.append_log(job_id, {"stage": "任务", "status": "开始", "total": len(pairs)})

    class Reporter:
        verbose = bool(options.get("verbose"))

        def emit(self, worker: int, email: str | None, event: dict[str, Any]) -> None:
            store.append_log(job_id, {"worker": worker, "email": email, **event})

    candidate_ids: dict[int, int] = {}
    candidate_lock = threading.Lock()

    def password_change_event(event: str, line_no: int, email: str,
                              password: str, error: str | None) -> None:
        with candidate_lock:
            if event == "prepared":
                candidate_ids[line_no] = store.create_candidate(job_id, email, password)
                return
            candidate_id = candidate_ids.get(line_no)
            if candidate_id is None:
                raise RuntimeError("password_candidate_not_prepared")
            status = {
                "submitted": "submitted", "confirmed": "confirmed",
                "uncertain": "uncertain", "failed": "failed",
            }[event]
            store.update_candidate(candidate_id, status, error)

    def save_result(result: dict[str, Any]) -> None:
        line_no = result.get("line")
        if not isinstance(line_no, int) or line_no not in account_ids:
            raise ValueError("job_result_account_invalid")
        store.save_result(job_id, result, account_ids[line_no])

    try:
        kwargs: dict[str, Any] = {
            "workers": int(options.get("workers", 8)),
            "client_factory": client_factory,
            "alias_client_factory": alias_client_factory,
            "check_aliases": True,
            "remove_aliases": bool(options.get("remove_aliases")),
            "change_password": bool(options.get("change_password")),
            "proxy_pool": proxy_pool,
            "reporter": Reporter(),
            "emit": save_result,
            "ipinfo_key": str(options.get("ipinfo_key") or ""),
            "password_change_callback": password_change_event,
        }
        if transport_probe is not None:
            kwargs["transport_probe"] = transport_probe
        if exit_lookup is not None:
            kwargs["exit_lookup"] = exit_lookup
        results = checker(pairs, **kwargs)
        store.append_log(job_id, {"stage": "任务", "status": "完成",
                                  "completed": len(results)})
        store.set_job_status(job_id, "completed")
        return results
    except Exception as exc:
        store.append_log(job_id, {"stage": "任务", "status": "失败",
                                  "error": type(exc).__name__})
        store.set_job_status(job_id, "failed", type(exc).__name__)
        raise


def watch_parent() -> None:
    def stop_on_eof():
        sys.stdin.buffer.read(1)
        # API 崩溃或关闭写端后立即停止整个 worker，包括仍在请求中的线程。
        os._exit(1)
    threading.Thread(target=stop_on_eof, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) == 2 and values[1] == "--watch-parent":
        watch_parent()
        values = values[:1]
    if len(values) != 1:
        print("usage: python -m account_console.worker JOB_ID", file=sys.stderr)
        return 2
    run_job(values[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
