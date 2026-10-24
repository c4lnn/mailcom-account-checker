"""检查账号并永久清空 Drafts、INBOX、OUTBOX、Sent、Spam、Trash；同时检查别名数量。"""

from __future__ import annotations

import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import random
import secrets
import string
import sys
import time
from datetime import datetime
from queue import Queue
from threading import Condition, Lock, local
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from curl_cffi import CurlOpt, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from imap_fallback import ImapFallbackError, ImapFolderClearClient, probe_imap_transport
from mailcom_client import MailComClient, MailComError


MAX_ATTEMPTS = 3
CLEAR_FOLDERS = ("Drafts", "INBOX", "OUTBOX", "Sent", "Spam", "Trash")
MAX_INACTIVE_ALIASES = 90
_SYSTEM_RANDOM = random.SystemRandom()
_PASSWORD_CHARSET = string.ascii_uppercase + string.ascii_lowercase + string.digits
ProxyEntry = tuple[int, str]
Account = tuple[int, str, str]
AccountInput = Account | tuple[int, str, str, str, int]
_EXIT_MAX_BYTES = 16 * 1024
_GEO_TOTAL_TIMEOUT = 20.0
_GEO_PROVIDER_TIMEOUT = 7.0
_LOG_ERROR_KINDS = {
    "bad_credentials", "login_challenged", "rate_limited", "proxy_error", "network_error",
    "imap_login_error", "imap_error", "alias_error", "imap_clear_failed", "imap_timeout",
    "imap_network", "imap_protocol", "imap_auth_failed", "imap_tls", "proxy_required",
    "proxy_invalid", "proxy_auth", "proxy_connect", "proxy_timeout",
    "proxy_unavailable", "geo_timeout", "geo_http", "geo_invalid", "geo_too_large", "geo_network",
    "geo_unavailable",
}


def _log_text(value: object) -> str:
    return "".join(
        char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        for char in str(value)
    )


def generate_new_password(length: int = 12) -> str:
    """Generate a random password containing upper, lower and digits."""
    if length < 3:
        raise ValueError("password_too_short")
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    chars += [secrets.choice(_PASSWORD_CHARSET) for _ in range(length - 3)]
    _SYSTEM_RANDOM.shuffle(chars)
    return "".join(chars)


class _ProgressReporter:
    """单行写入并立即刷新；锁只保护过程日志出口。"""

    def __init__(self, stream=None, *, verbose: bool = False, tee=None) -> None:
        self.stream = sys.stderr if stream is None else stream
        self.tee = tee
        self.verbose = verbose
        self.lock = Lock()
        self.enabled = True

    def emit(self, worker: int, email: str | None, event: dict[str, object]) -> None:
        prefix = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] [worker#{worker}]"
        if email is not None:
            prefix += f"[{_log_text(email)}]"
        prefix += f"[{_log_text(event['stage'])}]"
        if event.get("folder"):
            prefix += f"[{_log_text(event['folder'])}]"
        fields: list[str] = []
        labels = {
            "attempt": "attempt", "proxy_index": "proxy", "operation": "操作",
            "status": "状态", "count": "邮件数",
            "error": "错误", "reason": "原因", "next_proxy": "下一代理",
            "alias_count": "别名数", "active_alias_count": "ACTIVE别名",
            "inactive_alias_count": "非ACTIVE别名", "address": "别名", "result": "结果", "failure_stage": "失败阶段",
            "failure_folder": "失败文件夹", "failure_operation": "失败操作",
            "before": "初始", "after": "剩余", "action": "处理",
            "ip": "IP", "country_code": "国家代码", "country_name": "国家",
            "provider": "提供方", "fallback_count": "备用次数",
            "old_password": "旧密码", "new_password": "新密码",
        }
        for key, label in labels.items():
            if (not self.verbose and key in {"attempt", "proxy_index"}
                    and event["stage"] not in {"账号检查", "代理检测"} and "重试" not in event["stage"]
                    and event.get("status") != "失败"):
                continue
            if key not in event or event[key] is None:
                continue
            value = event[key]
            if key == "attempt":
                value = f"{value}/{MAX_ATTEMPTS}"
            elif key == "error" and value not in _LOG_ERROR_KINDS:
                value = "unknown_error"
            if key == "count" and event["stage"] == "复查":
                label = "剩余"
            fields.append(f"{label}={_log_text(value)}")
        line = prefix + (" " + " ".join(fields) if fields else "") + "\n"
        with self.lock:
            if self.enabled:
                try:
                    self.stream.write(line)
                    self.stream.flush()
                    if self.tee is not None:
                        self.tee.write(line)
                        self.tee.flush()
                except (OSError, ValueError):
                    self.enabled = False


class _AccountProgress:
    """账号任务私有上下文，重试时更新编号，邮箱保持完整。"""

    def __init__(self, reporter: _ProgressReporter, worker: int, email: str, proxy_index: int) -> None:
        self.reporter = reporter
        self.worker = worker
        self.email = email.strip().lower()
        self.proxy_index = proxy_index
        self.attempt = 1
        self.failure: dict[str, object] | None = None
        self.folder_started = 0.0
        self.folder_before: int | None = None
        self.connection_elapsed = 0.0

    def __call__(self, event: dict[str, object]) -> None:
        if event.get("status") == "失败":
            self.failure = event.copy()
        if not self.reporter.verbose:
            event = self._compact(event)
            if event is None:
                return
        self.reporter.emit(self.worker, self.email, {
            **event, "attempt": self.attempt, "proxy_index": self.proxy_index,
        })

    def _compact(self, event: dict[str, object]) -> dict[str, object] | None:
        stage, status = event["stage"], event["status"]
        if status == "失败":
            return event
        if stage == "连接":
            if status == "开始":
                return {**event, "stage": "登录", "operation": "CONNECT/TLS + LOGIN"}
            self.connection_elapsed = float(event.get("elapsed", 0))
            return None
        if stage == "登录":
            if status == "开始":
                return None
            return {**event, "operation": "CONNECT/TLS + LOGIN",
                    "elapsed": self.connection_elapsed + float(event.get("elapsed", 0))}
        if stage == "首次查询":
            if status == "开始":
                self.folder_started = time.monotonic()
                self.folder_before = None
            elif status == "成功":
                self.folder_before = event.get("count")
            return None
        if stage == "清空":
            if event.get("operation") == "STORE" and status == "开始":
                return {"stage": "清空", "folder": event["folder"], "status": "开始",
                        "count": self.folder_before}
            return None
        if stage == "复查":
            if status == "开始":
                return None
            return {"stage": "文件夹" if self.folder_before == 0 else "复查",
                    "folder": event["folder"], "status": status,
                    "before": self.folder_before, "after": event.get("count"),
                    "action": "跳过清空" if self.folder_before == 0 else "清空",
                    "elapsed": time.monotonic() - self.folder_started}
        return event

    def report(self, stage: str, status: str, **fields) -> None:
        self({"stage": stage, "status": status, **fields})

    def begin_attempt(self, attempt: int, proxy_index: int) -> None:
        self.attempt = attempt
        self.proxy_index = proxy_index
        self.failure = None
        self.connection_elapsed = 0.0
        if attempt > 1 and not self.reporter.verbose:
            self.report("重试", "开始")


def _read_records(path: str, *, accounts: bool) -> list[str]:
    values: list[str] = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        values.append(line)
    if accounts:
        for value in values:
            if "----" not in value:
                raise ValueError("account_format")
            email, password = value.split("----", 1)
            if not email.strip() or not password.strip():
                raise ValueError("account_format")
    return values


def _normalize_proxy(value: str) -> str:
    try:
        text = value.strip().replace("：", ":")
        if "://" not in text:
            parts = text.split(":", 3)
            if len(parts) == 2:
                text = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                host, port, username, password = parts
                text = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
            else:
                raise ValueError
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy_format")
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        return text
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("proxy_format") from None


def load_proxy_pool(proxies_path: str) -> list[ProxyEntry]:
    proxy_rows = [_normalize_proxy(value) for value in _read_records(proxies_path, accounts=False)]
    if not proxy_rows:
        raise ValueError("proxy_file_empty")
    return _unique_proxies([(index, proxy) for index, proxy in enumerate(proxy_rows, 1)])


def load_pairs(
    accounts_path: str,
    proxies_path: str,
    *,
    proxy_pool: list[ProxyEntry] | None = None,
) -> list[Account]:
    """只加载账号；代理参数保留供旧调用适配，不预先绑定账号。"""
    account_rows = _read_records(accounts_path, accounts=True)
    pairs: list[Account] = []
    seen: set[tuple[str, str]] = set()
    for account in account_rows:
        email, password = account.split("----", 1)
        key = (email.strip(), password.strip())
        if key in seen:
            continue
        seen.add(key)
        line_no = len(pairs) + 1
        pairs.append((line_no, key[0].lower(), key[1]))
    return pairs


def _pool_for_pairs(
    pairs: list[AccountInput],
    proxy_pool: list[ProxyEntry] | None,
) -> list[ProxyEntry]:
    if proxy_pool is not None:
        return _unique_proxies(proxy_pool)
    return _unique_proxies([(pair[4], pair[3]) for pair in pairs if len(pair) == 5])


def _unique_proxies(pool: list[ProxyEntry]) -> list[ProxyEntry]:
    seen = set()
    unique = []
    for index, proxy in pool:
        parsed = urlsplit(proxy)
        identity = (parsed.scheme.lower(), (parsed.hostname or "").lower(),
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    unquote(parsed.username or ""), unquote(parsed.password or ""))
        if identity not in seen:
            seen.add(identity)
            unique.append((index, proxy))
    return unique


def _exit_fields(data: object) -> dict[str, object]:
    fields = {"ip": None, "country_code": None, "country_name": None}
    if not isinstance(data, dict) or data.get("error"):
        return fields
    try:
        if isinstance(data.get("ip"), str):
            fields["ip"] = str(ipaddress.ip_address(data["ip"]))
    except (ValueError, TypeError):
        pass
    code = data.get("country_code")
    if (isinstance(code, str) and len(code) == 2 and code.isascii() and code.isalpha()
            and code.upper() not in {"XX", "ZZ"}):
        fields["country_code"] = code.upper()
    name = data.get("country_name")
    if (isinstance(name, str) and 0 < len(name.strip()) <= 80
            and name.strip().lower() not in {"-", "unknown", "n/a", "null"}
            and all(char.isprintable() for char in name)):
        fields["country_name"] = name.strip()
    return fields


def load_config() -> dict[str, object]:
    """Read and validate CWD config.json; all eight fields are required."""
    try:
        data = json.loads(Path("config.json").read_text(encoding="utf-8"))
    except OSError:
        raise
    except ValueError:
        raise ValueError("config_error") from None
    if not isinstance(data, dict):
        raise ValueError("config_error")
    for field in ("accounts", "proxies", "output_dir", "ipinfo_key"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config_error")
    for field in ("verbose", "remove_aliases", "change_password"):
        if not isinstance(data.get(field), bool):
            raise ValueError("config_error")
    workers = data.get("workers")
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise ValueError("config_error")
    return data


def _geo_provider_specs(ipinfo_key: str | None) -> tuple[dict[str, object], ...]:
    providers: list[dict[str, object]] = []
    if ipinfo_key:
        providers.append({
            "name": "IPinfo Lite",
            "url": "https://api.ipinfo.io/lite/me?token=" + quote(ipinfo_key, safe=""),
            "kind": "ipinfo",
        })
    providers.extend((
        {"name": "ipwho.is", "url": "https://ipwho.is/", "kind": "ipwho"},
        {"name": "ipapi.co", "url": "https://ipapi.co/json/", "kind": "ipapi"},
    ))
    return tuple(providers)


def _normalize_geo_payload(kind: str, data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return _exit_fields(None)
    if kind == "ipwho" and data.get("success") is not True:
        return _exit_fields(None)
    if kind == "ipinfo":
        data = {**data, "country_name": data.get("country")}
    elif kind == "ipwho":
        data = {**data, "country_name": data.get("country")}
    return _exit_fields(data)


def _lookup_geo_provider(
    proxy_url: str,
    provider: dict[str, object],
    *,
    timeout: float,
    session_factory=None,
) -> dict[str, object]:
    """Query one provider through the explicit proxy with bounded response data."""
    session = response = None
    body = bytearray()
    too_large = False

    def receive(chunk):
        nonlocal too_large
        if len(body) + len(chunk) > _EXIT_MAX_BYTES:
            too_large = True
            return CURL_WRITEFUNC_ERROR
        body.extend(chunk)
        return len(chunk)

    result = _exit_fields(None)
    try:
        # NOPROXY 显式清空，防止 NO_PROXY 环境变量绕过指定代理。
        session = (session_factory or requests.Session)(
            trust_env=False, curl_options={CurlOpt.NOPROXY: ""},
        )
        response = session.get(
            provider["url"], proxy=proxy_url, timeout=timeout, verify=True,
            allow_redirects=False, content_callback=receive,
        )
        if too_large:
            result["error"] = "geo_too_large"
        elif not 200 <= response.status_code < 300:
            result["error"] = "geo_http"
        else:
            data = json.loads(body)
            result = _normalize_geo_payload(str(provider["kind"]), data)
            if any(result[key] is None for key in ("country_code", "country_name")):
                result["error"] = "geo_invalid"
    except requests.exceptions.Timeout:
        result["error"] = "geo_timeout"
    except (ValueError, UnicodeError):
        result["error"] = "geo_invalid"
    except Exception:
        result["error"] = "geo_too_large" if too_large else "geo_network"
    finally:
        for resource in (response, session):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    return result


def lookup_proxy_exit(
    proxy_url: str,
    *,
    timeout: float = _GEO_TOTAL_TIMEOUT,
    ipinfo_key: str | None = None,
    session_factory=None,
) -> dict[str, object]:
    """经指定代理按优先级观测出口国家，所有提供方共享一个总截止时间。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    attempts: list[dict[str, object]] = []
    for provider in _geo_provider_specs(ipinfo_key):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.monotonic()
        try:
            observed = _lookup_geo_provider(
                proxy_url, provider,
                timeout=min(_GEO_PROVIDER_TIMEOUT, remaining),
                session_factory=session_factory,
            )
        except Exception:
            observed = {"error": "geo_network"}
        elapsed = time.monotonic() - started
        error = observed.get("error")
        attempt = {
            "provider": provider["name"],
            "elapsed": elapsed,
            "error": error,
        }
        attempts.append(attempt)
        if error is None and all(observed.get(key) is not None for key in ("country_code", "country_name")):
            observed["provider"] = provider["name"]
            observed["attempts"] = attempts
            observed["fallback_count"] = len(attempts) - 1
            return observed
    result = _exit_fields(None)
    result["error"] = "geo_unavailable"
    result["attempts"] = attempts
    return result


class _ProbeProgress:
    def __init__(self, reporter, worker: int, email: str | None = None):
        self.reporter = reporter
        self.worker = worker
        self.email = email

    def report(self, stage: str, status: str, **fields):
        self.reporter.emit(self.worker, self.email, {"stage": stage, "status": status, **fields})

    def check(self, entry, transport_probe, exit_lookup) -> bool:
        index, proxy = entry
        fields = {"proxy_index": index}
        started = time.monotonic()
        self.report("代理检测", "开始", **fields)
        if self.reporter.verbose:
            self.report("代理检测", "开始", operation="CONNECT/TLS", **fields)
        try:
            transport_probe(proxy)
        except Exception as exc:
            error = exc.kind if isinstance(exc, ImapFallbackError) else "proxy_connect"
            self.report("代理检测", "失败", operation="CONNECT/TLS", error=error,
                        elapsed=time.monotonic() - started, **fields)
            return False
        if self.reporter.verbose:
            self.report("代理检测", "成功", operation="CONNECT/TLS", **fields)
        try:
            observed = exit_lookup(proxy)
            if not isinstance(observed, dict):
                observed = {"error": "geo_invalid"}
            country = _exit_fields({key: observed.get(key) for key in ("ip", "country_code", "country_name")})
            error = observed.get("error")
            if error not in {"geo_timeout", "geo_http", "geo_invalid", "geo_too_large", "geo_network", "geo_unavailable"}:
                error = "geo_invalid" if error else None
        except Exception:
            observed = {}
            country = _exit_fields(None)
            error = "geo_network"
        passed = error is None and all(country[key] is not None for key in ("country_code", "country_name"))
        if not passed and error is None:
            error = "geo_invalid"
        attempts = observed.get("attempts") if isinstance(observed, dict) else None
        if self.reporter.verbose and isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                attempt_error = attempt.get("error")
                self.report("代理检测", "成功" if attempt_error is None else "失败",
                            operation="出口国家查询", provider=attempt.get("provider"),
                            error=attempt_error, elapsed=attempt.get("elapsed"), **fields)
        self.report("代理检测", "成功" if passed else "失败",
                    operation="出口国家查询" if not passed else None,
                    provider=observed.get("provider") if isinstance(observed, dict) else None,
                    fallback_count=observed.get("fallback_count") if isinstance(observed, dict) else None,
                    error=error, elapsed=time.monotonic() - started,
                    **{key: value or "未知" for key, value in country.items()}, **fields)
        return passed


class _Dispatch:
    """候选只出不进；匿名名额保证预检不提前占用具体账号。"""
    def __init__(self, pairs, pool, proxy_choice, transport_probe, exit_lookup):
        self.accounts = deque(pairs)
        self.available = _unique_proxies(pool)
        self.states = {index: "available" for index, _proxy in self.available}
        self.condition = Condition()
        self.reservations = 0
        self.proxy_choice = proxy_choice
        self.transport_probe = transport_probe
        self.exit_lookup = exit_lookup
        self.exhausted_logged = False

    def reserve(self, progress: _ProbeProgress) -> bool:
        waiting = False
        started = time.monotonic()
        while True:
            with self.condition:
                if not self.accounts or not self.available:
                    reserved = False
                    break
                if self.reservations < len(self.accounts):
                    self.reservations += 1
                    reserved = True
                    break
                if waiting:
                    self.condition.wait()
                    continue
            progress.report("调度等待", "开始", reason="等待首次检查名额")
            waiting = True
        if waiting:
            progress.report("调度等待", "成功" if reserved else "停止",
                            elapsed=time.monotonic() - started)
        return reserved

    def cancel_reservation(self):
        with self.condition:
            self.reservations -= 1
            self.condition.notify_all()

    def claim_account(self):
        with self.condition:
            pair = self.accounts.popleft()
            self.reservations -= 1
            self.condition.notify_all()
            return pair

    def has_candidates(self):
        with self.condition:
            return bool(self.available)

    def spend(self, entry):
        if entry is not None:
            with self.condition:
                self.states[entry[0]] = "spent"
                self.condition.notify_all()

    def verified(self, progress: _ProbeProgress) -> ProxyEntry | None:
        while True:
            with self.condition:
                if not self.available:
                    return None
                entry = self.proxy_choice(self.available)
                self.available.remove(entry)
                self.states[entry[0]] = "claimed"
                self.condition.notify_all()
            passed = False
            try:
                passed = progress.check(entry, self.transport_probe, self.exit_lookup)
                if passed:
                    return entry
            finally:
                if not passed:
                    self.spend(entry)

    def exhausted(self, progress: _ProbeProgress):
        with self.condition:
            report = (not self.available and not self.exhausted_logged
                      and (bool(self.accounts) or progress.email is not None))
            if report:
                self.exhausted_logged = True
        if report:
            progress.report("代理检测", "停止", error="proxy_unavailable",
                            reason="候选已耗尽，停止新派发，已有持有者继续收尾")


class _ProxyLease:
    def __init__(self, dispatch: _Dispatch, entry: ProxyEntry, progress: _ProbeProgress):
        self.dispatch = dispatch
        self.entry: ProxyEntry | None = entry
        self.progress = progress
        self.used_indexes: set[int] = set()

    def close(self):
        entry, self.entry = self.entry, None
        self.dispatch.spend(entry)

    def replace(self, progress: _AccountProgress, stage: str, error: str) -> bool:
        if not self.dispatch.has_candidates():
            self.dispatch.exhausted(self.progress)
            progress.report(stage, "停止", error=error, reason="无未领取候选代理")
            return False
        progress.report(stage, "开始", error=error, reason="先预检新代理")
        self.close()
        self.entry = self.dispatch.verified(self.progress)
        if self.entry is None:
            self.dispatch.exhausted(self.progress)
            progress.report(stage, "停止", error=error, reason="无通过预检的新代理")
        return self.entry is not None


def _status(exc: ImapFallbackError) -> str:
    kind = exc.kind
    if kind in {"imap_auth_failed"}:
        return "bad_credentials"
    if kind in {"login_challenged", "imap_challenged"}:
        return "login_challenged"
    if kind in {"rate_limited", "imap_rate_limited"}:
        return "rate_limited"
    if kind.startswith("proxy_"):
        return "proxy_error"
    if kind in {"imap_timeout", "imap_network"}:
        return "network_error"
    if kind == "imap_login_unknown":
        return "imap_login_error"
    return "imap_error"


def _alias_status(exc: MailComError) -> str:
    kind = exc.kind
    if kind == "bad_credentials":
        return "bad_credentials"
    if kind in {"login_challenged", "login_redirect", "blocked", "session_rejected"}:
        return "login_challenged"
    if kind == "rate_limited":
        return "rate_limited"
    if kind.startswith("proxy_"):
        return "proxy_error"
    if kind == "network":
        return "network_error"
    return "alias_error"


def _close_web_client(client: object | None) -> None:
    session = getattr(client, "session", None)
    close = getattr(session, "close", None)
    if callable(close):
        close()


def _folders_empty(counts: object) -> bool:
    if not isinstance(counts, dict):
        return False
    return all(
        isinstance(counts.get(folder), dict)
        and type(counts[folder].get("after")) is int
        and counts[folder]["after"] == 0
        for folder in CLEAR_FOLDERS
    )


def _failed(status: str, proxy_index=None) -> dict[str, object]:
    return {"ok": False, "status": status, "error": status, "proxy_index": proxy_index}


def _close_imap_client(client):
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _check_imap(
    pair: AccountInput, client_factory, *, lease: _ProxyLease, progress: _AccountProgress,
) -> dict[str, object]:
    _line_no, email, password = pair[:3]
    last_error = "proxy_unavailable"
    proxy_index = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if lease.entry is None:
            break
        proxy_index, proxy = lease.entry
        lease.used_indexes.add(proxy_index)
        progress.begin_attempt(attempt, proxy_index)
        client = None
        clearing_started = False
        try:
            client = client_factory(email, password, proxy_url=proxy)
            set_handler = getattr(client, "set_event_handler", None)
            if callable(set_handler):
                set_handler(progress)
            client.connect()
            clearing_started = True
            counts = client.clear_folders(CLEAR_FOLDERS)
            cleared = _folders_empty(counts)
            result = {
                "ok": cleared, "status": "ready" if cleared else "imap_clear_failed",
                "folder_counts": counts, "inbox_count": counts.get("INBOX", {}).get("after"),
                "proxy_index": proxy_index,
            }
            if not cleared:
                result["error"] = "imap_clear_failed"
                folder = next(folder for folder in CLEAR_FOLDERS
                              if not isinstance(counts.get(folder), dict)
                              or type(counts[folder].get("after")) is not int
                              or counts[folder]["after"] != 0)
                progress.report("复查", "失败", folder=folder, operation="SELECT",
                                error="imap_clear_failed", reason="文件夹未全部为空")
            return result
        except Exception as exc:
            last_error = "imap_clear_failed" if clearing_started else (
                _status(exc) if isinstance(exc, ImapFallbackError) else "imap_error")
            if isinstance(exc, ImapFallbackError) and exc.kind == "imap_clear_failed":
                last_error = "imap_clear_failed"
            if progress.failure is None:
                progress.report("文件夹处理" if clearing_started else "连接/登录", "失败", error=last_error)
        finally:
            _close_imap_client(client)
        if last_error == "imap_clear_failed":
            progress.report("IMAP重试", "停止", reason="文件夹处理失败不可重放", error=last_error)
            break
        if attempt == MAX_ATTEMPTS:
            progress.report("IMAP重试", "停止", reason="尝试次数已用完", error=last_error)
            break
        if not lease.replace(progress, "IMAP重试", last_error):
            break
    return _failed(last_error, proxy_index)


def _change_password_with_retry(
    pair: AccountInput, first_client, client_factory, *, lease: _ProxyLease,
    progress: _AccountProgress, new_password_factory,
) -> dict[str, object]:
    """改密码：首尝试复用 first_client（不二次登录），失败仅对连接类错误换代理重试。

    服务端明确拒绝（session/bad_credentials 等）立即判失败，避免
    saveChanges 已生效但响应丢失时用旧密码重试导致误判。
    """
    _line_no, email, password = pair[:3]
    proxy_index = lease.entry[0] if lease.entry else None
    # 循环外生成一次：重试复用同一新密码，避免每次尝试提交不同密码加重 replay 风险
    new_password = new_password_factory()

    def attempt_change(client) -> str:
        progress.report("改密码", "开始", old_password=password, new_password=new_password)
        client.change_password(new_password)
        progress.report("改密码", "成功", old_password=password, new_password=new_password)
        return new_password

    def classify(exc: Exception) -> tuple[str, bool]:
        if isinstance(exc, MailComError):
            retryable = exc.kind in {"proxy_timeout", "proxy_connect", "network"}
            return _alias_status(exc), retryable
        return "alias_error", False

    try:
        new_password = attempt_change(first_client)
        return {"ok": True, "status": "password_changed", "new_password": new_password,
                "proxy_index": proxy_index}
    except Exception as exc:
        last_error, retryable = classify(exc)
        progress.report("改密码", "失败", error=last_error)
        if not retryable:
            return _failed(last_error, proxy_index)

    for attempt in range(2, MAX_ATTEMPTS + 1):
        if not lease.replace(progress, "改密码重试", last_error):
            break
        proxy_index, proxy = lease.entry
        lease.used_indexes.add(proxy_index)
        progress.begin_attempt(attempt, proxy_index)
        client = None
        try:
            client = client_factory(email, password, proxy_url=proxy)
            if hasattr(client, "login_retries"):
                client.login_retries = 1
            new_password = attempt_change(client)
            return {"ok": True, "status": "password_changed", "new_password": new_password,
                    "proxy_index": proxy_index}
        except Exception as exc:
            last_error, retryable = classify(exc)
            progress.report("改密码", "失败", error=last_error)
            if not retryable:
                break
        finally:
            _close_web_client(client)
    return _failed(last_error, proxy_index)


def _check_aliases(
    pair: AccountInput, client_factory, *, lease: _ProxyLease, progress: _AccountProgress,
    remove_aliases: bool = False, change_password: bool = False, new_password_factory=None,
) -> dict[str, object]:
    _line_no, email, password = pair[:3]
    last_error = "proxy_unavailable"
    proxy_index = None
    success_client = None
    success_result = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if lease.entry is None:
            progress.report("Web别名检查", "失败", error="proxy_unavailable", reason="无持有代理可开始检查")
            break
        proxy_index, proxy = lease.entry
        lease.used_indexes.add(proxy_index)
        progress.begin_attempt(attempt, proxy_index)
        client = None
        started = time.monotonic()
        progress.report("Web别名检查", "开始")
        try:
            client = client_factory(email, password, proxy_url=proxy)
            if hasattr(client, "login_retries"):
                client.login_retries = 1
            if hasattr(client, "oauth_retries"):
                client.oauth_retries = 1
            normalized_primary = email.strip().lower()

            def fetch_rows() -> list[tuple[str, str]]:
                if hasattr(client, "list_alias_rows"):
                    return [
                        (str(row.get("address") or "").strip().lower(), str(row.get("state") or ""))
                        for row in client.list_alias_rows()
                        if isinstance(row, dict) and row.get("address")
                    ]
                addresses = client.list_all_aliases() if hasattr(client, "list_all_aliases") else client.list_aliases()
                rows: list[tuple[str, str]] = []
                for item in addresses:
                    address = (
                        str(item.get("address") or "").strip().lower()
                        if isinstance(item, dict)
                        else str(item).strip().lower()
                    )
                    if address:
                        rows.append((address, "ACTIVE"))
                return rows

            def summarize(rows: list[tuple[str, str]]) -> tuple[list[str], int, int]:
                active_addrs: list[str] = []
                active = inactive = 0
                for address, state in rows:
                    if address == normalized_primary:
                        continue
                    if state == "ACTIVE":
                        active_addrs.append(address)
                        active += 1
                    else:
                        inactive += 1
                return active_addrs, active, inactive

            rows = fetch_rows()
            before_addrs, active_before, inactive_before = summarize(rows)
            if not remove_aliases:
                progress.report(
                    "Web别名检查", "成功",
                    alias_count=active_before + inactive_before,
                    active_alias_count=active_before, inactive_alias_count=inactive_before,
                    elapsed=time.monotonic() - started,
                )
                success_result = {
                    "ok": True, "status": "ready",
                    "alias_count": active_before + inactive_before,
                    "active_alias_count": active_before,
                    "inactive_alias_count": inactive_before,
                    "removed_count": 0, "before_count": active_before,
                    "proxy_index": proxy_index,
                }
                success_client = client
                client = None
                break
            progress.report(
                "别名检查", "成功",
                alias_count=active_before + inactive_before,
                active_alias_count=active_before, inactive_alias_count=inactive_before,
                elapsed=time.monotonic() - started,
            )
            removed_count = 0
            for address in before_addrs:
                try:
                    client.remove_alias(address)
                    removed_count += 1
                    progress.report("别名删除", "成功", address=address)
                except Exception as exc:
                    last_error = _alias_status(exc) if isinstance(exc, MailComError) else "alias_error"
                    progress.report("别名删除", "失败", error=last_error, elapsed=time.monotonic() - started)
            rows = fetch_rows()
            _after_addrs, active_after, inactive_after = summarize(rows)
            progress.report(
                "Web别名检查", "成功",
                alias_count=active_after + inactive_after,
                active_alias_count=active_after, inactive_alias_count=inactive_after,
                removed_count=removed_count, before_count=active_before,
                elapsed=time.monotonic() - started,
            )
            success_result = {
                "ok": True, "status": "ready",
                "alias_count": active_after + inactive_after,
                "active_alias_count": active_after,
                "inactive_alias_count": inactive_after,
                "removed_count": removed_count, "before_count": active_before,
                "proxy_index": proxy_index,
            }
            success_client = client
            client = None
            break
        except Exception as exc:
            last_error = _alias_status(exc) if isinstance(exc, MailComError) else "alias_error"
            progress.report("Web别名检查", "失败", error=last_error, elapsed=time.monotonic() - started)
        finally:
            try:
                _close_web_client(client)
            except Exception:
                pass
        if attempt == MAX_ATTEMPTS:
            progress.report("Web重试", "停止", reason="尝试次数已用完", error=last_error)
            break
        if not lease.replace(progress, "Web重试", last_error):
            break
    if success_result is None:
        return _failed(last_error, proxy_index)
    if change_password and new_password_factory is not None:
        pwd_result = _change_password_with_retry(
            pair, success_client, client_factory, lease=lease, progress=progress,
            new_password_factory=new_password_factory,
        )
        success_result["password_ok"] = bool(pwd_result["ok"])
        if pwd_result["ok"]:
            success_result["new_password"] = pwd_result["new_password"]
            success_result["password_status"] = pwd_result.get("status")
        else:
            success_result["password_error"] = pwd_result.get("error")
        if pwd_result.get("proxy_index") is not None:
            success_result["proxy_index"] = pwd_result["proxy_index"]
    try:
        _close_web_client(success_client)
    except Exception:
        pass
    return success_result


def _check_one(
    pair: AccountInput, client_factory, *, lease: _ProxyLease, progress: _AccountProgress,
    check_aliases: bool = False, alias_client_factory=MailComClient,
    remove_aliases: bool = False, change_password: bool = False,
    new_password_factory=generate_new_password,
) -> dict[str, object]:
    line_no, email, _password = pair[:3]
    started = time.monotonic()
    failure = None
    progress.report("账号检查", "开始")
    result: dict[str, object] = {
        "line": line_no, "email": email, "proxy_index": lease.entry[0], "ok": False,
    }
    try:
        imap_result = _check_imap(pair, client_factory, lease=lease, progress=progress)
        result["proxy_index"] = imap_result.get("proxy_index", result["proxy_index"])
        if not imap_result.get("ok"):
            failure = progress.failure
            result.update(imap_result)
            return result
        if not check_aliases:
            result.update(imap_result)
            return result
        alias_result = _check_aliases(pair, alias_client_factory, lease=lease, progress=progress,
                                      remove_aliases=remove_aliases, change_password=change_password,
                                      new_password_factory=new_password_factory)
        if alias_result.get("proxy_index") is not None:
            result["proxy_index"] = alias_result["proxy_index"]
        imap_ok, alias_ok = bool(imap_result.get("ok")), bool(alias_result.get("ok"))
        password_ok = alias_result.get("password_ok")
        overall_ok = imap_ok and alias_ok and (password_ok is not False)
        result.update({
            "ok": overall_ok,
            "imap_ok": imap_ok, "imap_status": imap_result.get("status"),
            "alias_ok": alias_ok, "alias_status": alias_result.get("status"),
        })
        for key in ("inbox_count", "folder_counts"):
            if key in imap_result:
                result[key] = imap_result[key]
        for key in ("alias_count", "active_alias_count", "inactive_alias_count",
                    "removed_count", "before_count", "new_password", "password_ok",
                    "password_status"):
            if key in alias_result:
                result[key] = alias_result[key]
        if overall_ok:
            result["status"] = "ready"
        else:
            result["status"] = "partial_failure"
            if not imap_ok:
                result["error"] = imap_result.get("error")
            elif not alias_ok:
                result["error"] = alias_result.get("error")
            else:
                result["error"] = alias_result.get("password_error")
    except Exception:
        result.update({"status": "imap_error", "error": "imap_error"})
    finally:
        failure = failure or progress.failure
        details = {
            f"failure_{key}": failure[key]
            for key in ("stage", "folder", "operation") if failure is not None and key in failure
        }
        progress.report(
            "账号结束", "成功" if result.get("ok") else "失败",
            error=result.get("error"),
            elapsed=time.monotonic() - started,
            **(details if not result.get("ok") else {}),
        )
    return result


def check_pairs(
    pairs: list[AccountInput], client_factory=ImapFolderClearClient, *,
    check_aliases: bool = False, alias_client_factory=MailComClient,
    remove_aliases: bool = False, change_password: bool = False,
    new_password_factory=generate_new_password,
    proxy_pool: list[ProxyEntry] | None = None, proxy_choice=_SYSTEM_RANDOM.choice,
    reporter: _ProgressReporter | None = None, transport_probe=None, exit_lookup=None,
    ipinfo_key: str | None = None,
) -> list[dict[str, object]]:
    return check_pairs_parallel(
        pairs, workers=1, client_factory=client_factory, check_aliases=check_aliases,
        alias_client_factory=alias_client_factory, remove_aliases=remove_aliases,
        change_password=change_password, new_password_factory=new_password_factory,
        proxy_pool=proxy_pool, proxy_choice=proxy_choice,
        reporter=reporter, transport_probe=transport_probe, exit_lookup=exit_lookup,
        ipinfo_key=ipinfo_key,
    )


def check_pairs_parallel(
    pairs: list[AccountInput], *, workers: int = 8, client_factory=ImapFolderClearClient,
    emit=None, check_aliases: bool = False, alias_client_factory=MailComClient,
    remove_aliases: bool = False, change_password: bool = False,
    new_password_factory=generate_new_password,
    proxy_pool: list[ProxyEntry] | None = None, proxy_choice=_SYSTEM_RANDOM.choice,
    reporter: _ProgressReporter | None = None, transport_probe=None, exit_lookup=None,
    ipinfo_key: str | None = None,
) -> list[dict[str, object]]:
    if type(workers) is not int or not 1 <= workers <= 256:
        raise ValueError("workers_invalid")
    if not pairs:
        return []
    pool = _pool_for_pairs(pairs, proxy_pool)
    reporter = reporter or _ProgressReporter()
    if exit_lookup is None and ipinfo_key is not None:
        exit_lookup = lambda proxy: lookup_proxy_exit(proxy, ipinfo_key=ipinfo_key)
    dispatch = _Dispatch(pairs, pool, proxy_choice, transport_probe or probe_imap_transport,
                         exit_lookup or lookup_proxy_exit)
    results: Queue = Queue()
    completed: list[dict[str, object]] = []
    worker_context = local()
    worker_ids = iter(range(1, workers + 1))
    worker_lock = Lock()

    def initialize_worker():
        with worker_lock:
            worker_context.number = next(worker_ids)

    def run_worker():
        context = _ProbeProgress(reporter, worker_context.number)
        try:
            while True:
                context.email = None
                if not dispatch.reserve(context):
                    dispatch.exhausted(context)
                    return
                reserved = True
                entry = None
                lease = None
                pair = None
                try:
                    entry = dispatch.verified(context)
                    if entry is None:
                        dispatch.exhausted(context)
                        return
                    pair = dispatch.claim_account()
                    reserved = False
                    context.email = pair[1]
                    lease = _ProxyLease(dispatch, entry, context)
                    progress = _AccountProgress(reporter, context.worker, pair[1], entry[0])
                    result = _check_one(
                        pair, client_factory, lease=lease, progress=progress,
                        check_aliases=check_aliases, alias_client_factory=alias_client_factory,
                        remove_aliases=remove_aliases, change_password=change_password,
                        new_password_factory=new_password_factory,
                    )
                    results.put(result)
                except Exception:
                    if pair is not None:
                        results.put({"line": pair[0], "email": pair[1], **_failed("imap_error", entry[0])})
                    else:
                        return
                finally:
                    if lease is not None:
                        lease.close()
                    else:
                        dispatch.spend(entry)
                    if reserved:
                        dispatch.cancel_reservation()
        finally:
            results.put(None)

    def accept(result):
        completed.append(result)
        if emit is not None:
            emit(result)

    count = min(workers, len(pairs))
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="imap-check", initializer=initialize_worker) as executor:
        futures = [executor.submit(run_worker) for _ in range(count)]
        finished = 0
        while finished < count:
            result = results.get()
            if result is None:
                finished += 1
            else:
                accept(result)
        for future in futures:
            future.result()
    # 所有在途名额已释放或转换，剩余账号只由协调器终结一次。
    while dispatch.accounts:
        pair = dispatch.accounts.popleft()
        accept({"line": pair[0], "email": pair[1], **_failed("proxy_unavailable")})
    return completed


def _ensure_distinct_output(path: Path, inputs: tuple[Path, ...]) -> None:
    resolved = path.resolve()
    if any(resolved == item.resolve() for item in inputs):
        raise ValueError("output_path_conflict")


def _is_filtered_account(result: dict[str, object], *, check_aliases: bool) -> bool:
    if not _folders_empty(result.get("folder_counts")):
        return False
    if check_aliases:
        return (
            result.get("imap_ok") is True
            and result.get("imap_status") == "ready"
            and result.get("alias_ok") is True
            and result.get("alias_status") == "ready"
            and result.get("active_alias_count") == 0
            and (result.get("inactive_alias_count") or 0) <= MAX_INACTIVE_ALIASES
        )
    return result.get("ok") is True and result.get("status") == "ready"


class _StreamingWriter:
    """以 `<name>.partial` 逐行写盘；`finalize` rename 为最终名，`abort` 保留部分文件。"""

    def __init__(self, path: str, *, inputs: tuple[str, ...] = ()) -> None:
        final = Path(path)
        _ensure_distinct_output(final, tuple(Path(item) for item in inputs))
        self._final = final
        self._partial = final.with_name(final.name + ".partial")
        self._fh = self._partial.open("w", encoding="utf-8", newline="")
        self._closed = False

    def write_line(self, line: str) -> None:
        self._fh.write(line)
        self._fh.flush()

    def finalize(self) -> None:
        if not self._closed:
            self._fh.close()
            self._partial.rename(self._final)
            self._closed = True

    def abort(self) -> None:
        if not self._closed:
            self._fh.close()
            self._closed = True


def main() -> int:
    try:
        config = load_config()
    except OSError:
        print(json.dumps({"ok": False, "status": "config_file_error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(json.dumps({"ok": False, "status": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        proxy_pool = load_proxy_pool(config["proxies"])
        pairs = load_pairs(config["accounts"], config["proxies"], proxy_pool=proxy_pool)
    except OSError:
        print(json.dumps({"ok": False, "status": "input_file_error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except ValueError as exc:
        status = str(exc) if str(exc) in {"account_format", "proxy_format", "proxy_file_empty"} else "input_error"
        print(json.dumps({"ok": False, "status": status}, ensure_ascii=False), file=sys.stderr)
        return 2
    output_root = Path(config["output_dir"])
    run_dir = output_root / datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    good_path = run_dir / "good_accounts.txt"
    error_path = run_dir / "error_accounts.txt"
    input_paths = (config["accounts"], config["proxies"])
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir()
        log_file = (run_dir / "log.txt").open("w", encoding="utf-8", newline="")
    except OSError:
        print(json.dumps({"ok": False, "status": "output_file_error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    passwords = {pair[0]: pair[2] for pair in pairs}
    good_writer = None
    error_writer = None
    try:
        good_writer = _StreamingWriter(str(good_path), inputs=input_paths)
        error_writer = _StreamingWriter(str(error_path), inputs=input_paths)

        def emit_result(result):
            line_no = result.get("line")
            email = str(result.get("email") or "").strip().lower()
            if not email or not isinstance(line_no, int) or line_no not in passwords:
                return
            password = result.get("new_password") or passwords[line_no]
            if _is_filtered_account(result, check_aliases=True):
                good_writer.write_line(f"{email}----{password}\n")
            else:
                error = result.get("error") or result.get("status") or "unknown"
                error_writer.write_line(f"{email}----{password}----{error}\n")

        results = check_pairs_parallel(
            pairs,
            workers=config["workers"],
            check_aliases=True,
            remove_aliases=config["remove_aliases"],
            change_password=config["change_password"],
            proxy_pool=proxy_pool,
            reporter=_ProgressReporter(verbose=config["verbose"], tee=log_file),
            ipinfo_key=config["ipinfo_key"],
            emit=emit_result,
        )
    except (OSError, ValueError) as exc:
        if good_writer is not None:
            good_writer.abort()
        if error_writer is not None:
            error_writer.abort()
        print(json.dumps({"ok": False, "status": str(exc) if isinstance(exc, ValueError) else "output_file_error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        log_file.close()
    try:
        good_writer.finalize()
        error_writer.finalize()
    except OSError:
        print(json.dumps({"ok": False, "status": "output_file_error"}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0 if all(bool(item["ok"]) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
