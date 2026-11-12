from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import time
from ipaddress import ip_address
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from .db import Database
from .security import SecretBox
from .store import Store


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _install_local_network_guard() -> None:
    from curl_cffi import requests as curl_requests

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_request = curl_requests.Session.request

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        host_text = host.decode() if isinstance(host, bytes) else str(host)
        if not _is_loopback_host(host_text):
            raise RuntimeError("mocker_network_egress_blocked")
        return original_getaddrinfo(host, port, *args, **kwargs)

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and not _is_loopback_host(str(address[0])):
            raise RuntimeError("mocker_network_egress_blocked")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple) and not _is_loopback_host(str(address[0])):
            raise RuntimeError("mocker_network_egress_blocked")
        return original_connect_ex(sock, address)

    def guarded_request(session, method, url, *args, **kwargs):
        follow = kwargs.pop("allow_redirects", True)
        for _ in range(20):
            if not _is_loopback_host(urlsplit(str(url)).hostname):
                raise RuntimeError("mocker_network_egress_blocked")
            proxies = kwargs.get("proxies") or session.proxies or {}
            for proxy in [kwargs.get("proxy"), *proxies.values()]:
                if proxy and not _is_loopback_host(urlsplit(str(proxy)).hostname):
                    raise RuntimeError("mocker_network_egress_blocked")
            response = original_request(
                session, method, url, *args, allow_redirects=False, **kwargs,
            )
            location = response.headers.get("Location")
            if not follow or response.status_code not in {301, 302, 303, 307, 308} or not location:
                return response
            url = urljoin(str(url), location)
            if response.status_code == 303 or (
                response.status_code in {301, 302} and method.upper() == "POST"
            ):
                method = "GET"
                kwargs.pop("data", None)
                kwargs.pop("json", None)
        raise RuntimeError("mocker_redirect_limit_exceeded")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo
    curl_requests.Session.request = guarded_request


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
    mocker_mode = os.environ.get("MAIL_MOCKER_MODE") == "1"
    db = database or Database()
    db.initialize()
    store = Store(db, secrets_box or SecretBox())
    options = store.job_options(job_id)
    if mocker_mode:
        _install_local_network_guard()

    def fail_mocker(reason: str) -> None:
        store.append_log(job_id, {"stage": "Mocker 隔离", "status": "失败", "error": reason})
        store.set_job_status(job_id, "failed", reason)
        raise RuntimeError(reason)


    if checker is None:
        import check_and_clean_accounts as checker_module

        checker = checker_module.check_pairs_parallel
        mocker_mode = os.environ.get("MAIL_MOCKER_MODE") == "1"
        configured_endpoints = endpoint_overrides
        if configured_endpoints is None:
            raw = os.environ.get("MAIL_API_ENDPOINT_OVERRIDES", "")
            try:
                configured_endpoints = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                fail_mocker("mocker_configuration_invalid")
        if mocker_mode:
            host = os.environ.get("MAIL_MOCKER_IMAP_HOST", "")
            try:
                port = int(os.environ.get("MAIL_MOCKER_IMAP_PORT", "0"))
            except ValueError:
                fail_mocker("mocker_configuration_invalid")
            certificate = os.environ.get("MAIL_MOCKER_CERT", "")
            if (not host or not 1 <= port <= 65535 or not certificate
                    or not isinstance(configured_endpoints, dict) or not configured_endpoints):
                fail_mocker("mocker_configuration_missing")
            from .mocker import MAIL_ORIGINS

            try:
                local_targets = all(
                    origin in configured_endpoints
                    and urlsplit(configured_endpoints[origin]).scheme == "http"
                    and _is_loopback_host(urlsplit(configured_endpoints[origin]).hostname)
                    for origin in MAIL_ORIGINS
                )
            except (TypeError, ValueError):
                local_targets = False
            if not _is_loopback_host(host) or not local_targets:
                fail_mocker("mocker_target_not_local")
            if not os.path.isfile(certificate):
                fail_mocker("mocker_certificate_missing")
            from imap_fallback import ImapFallbackError

            mocker_plan = options.get("mocker_plan") or {}
            proxy_indexes = {
                proxy: index for index, proxy in enumerate(options.get("proxies", []), 1)
            }

            def planned_proxy_fault(operation: str, email: str, candidate: int | None):
                for fault in mocker_plan.get("faults", []):
                    if (fault.get("operation") == operation
                            and fault.get("candidate") == candidate
                            and (not email or fault.get("email", "").lower() == email.lower())):
                        return fault.get("fault")
                return None

            def proxy_fault_error(fault: str):
                kind = fault if fault.startswith("proxy_") else (
                    "proxy_timeout" if fault == "drop" else "proxy_connect"
                )
                return ImapFallbackError(kind)

            imap_host, imap_port = host, port
            if imap_connection_factory is None:
                import imaplib

                context = ssl.create_default_context(cafile=certificate)
                imap_connection_factory = lambda *_a, **_kw: imaplib.IMAP4_SSL(
                    host, port, ssl_context=context,
                )
            if transport_probe is None:
                def transport_probe(proxy: str) -> None:
                    candidate = proxy_indexes.get(proxy)
                    fault = planned_proxy_fault("proxy_preflight", "", candidate)
                    if fault:
                        raise proxy_fault_error(fault)
            if exit_lookup is None:
                exit_lookup = lambda _proxy: {
                    "ip": "127.0.0.1", "country_code": "LO", "country_name": "Local",
                }
            try:
                from curl_cffi import requests as curl_requests

                with curl_requests.Session() as session:
                    response = session.get(next(iter(configured_endpoints.values())),
                                           timeout=2, allow_redirects=False)
                    if response.status_code != 200:
                        raise RuntimeError("mocker_http_unavailable")
                connection = imap_connection_factory()
                connection.logout()
            except Exception:
                fail_mocker("mocker_unavailable")
        if client_factory is None:
            def client_factory(username: str, password: str, *, proxy_url: str):
                from imap_fallback import ImapFolderClearClient

                kwargs: dict[str, Any] = {"proxy_url": proxy_url}
                if imap_host is not None:
                    kwargs["host"] = imap_host
                if imap_port is not None:
                    kwargs["port"] = imap_port
                if imap_connection_factory is not None:
                    connection_factory = imap_connection_factory
                    if mocker_mode:
                        candidate = proxy_indexes.get(proxy_url)
                        base_connection_factory = imap_connection_factory

                        def connection_factory(*args, **factory_kwargs):
                            fault = planned_proxy_fault("proxy_transport", username, candidate)
                            if fault:
                                raise proxy_fault_error(fault)
                            return base_connection_factory(*args, **factory_kwargs)

                    kwargs["connection_factory"] = connection_factory
                return ImapFolderClearClient(username, password, **kwargs)
        if alias_client_factory is None:
            from mailcom_client import MailComClient

            def alias_client_factory(username: str, password: str, *, proxy_url: str):
                return MailComClient(
                    username, password, proxy_url="" if mocker_mode else proxy_url,
                    endpoint_overrides=configured_endpoints,
                )

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

    lifecycle = (options.get("mocker_plan") or {}).get("lifecycle")
    if mocker_mode and lifecycle == "worker_interrupt":
        os._exit(70)
    if mocker_mode and lifecycle == "api_restart":
        while True:
            time.sleep(60)

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
        if mocker_mode:
            kwargs["proxy_choice"] = lambda available: min(available)
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
