"""IMAPS/POP3S readers and an isolated, explicit IMAP folder cleaner.

This module is intentionally independent from ``server.py``.  It provides a
small, testable protocol client that can be wired into the HTTP path after a
canary rollout.  The client requires the account's fixed HTTP(S) proxy by
default and never falls back to a direct connection.

The service's readers use only read-only commands. ``ImapFolderClearClient``
is separate and used only by the explicitly run account checker. Credentials
and proxy URLs stay in memory and are not included in diagnostic values.
"""

from __future__ import annotations

import base64
import email
import email.header
import email.utils
import imaplib
import poplib
import re
import socket
import ssl
import time
from queue import Empty, Queue
from threading import Thread
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit


IMAP_HOST = "imap.mail.com"
IMAP_PORT = 993
POP3_HOST = "pop.mail.com"
POP3_PORT = 995
_MAX_PROXY_RESPONSE = 16 * 1024
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_RECIPIENT_HEADERS = (
    "To",
    "Cc",
    "Delivered-To",
    "Envelope-To",
    "X-Original-To",
    "X-Envelope-To",
)
_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.I)


class ImapFallbackError(RuntimeError):
    """Credential-free error exposed to the caller."""

    def __init__(self, kind: str, *, status: int = 502) -> None:
        self.kind = str(kind)
        self.status = int(status)
        super().__init__(self.kind)


class Pop3FallbackError(ImapFallbackError):
    """Credential-free error exposed by the optional POP3S reader."""


@dataclass(slots=True, frozen=True)
class ImapHeader:
    uid: str
    subject: str
    sender: str
    recipients: tuple[str, ...]
    date: datetime | None
    raw: bytes = field(repr=False, default=b"")


@dataclass(slots=True, frozen=True)
class ImapMail:
    uid: str
    subject: str
    sender: str
    recipients: tuple[str, ...]
    date: datetime | None
    raw: bytes = field(repr=False, default=b"")


def _proxy_parts(proxy_url: str) -> tuple[str, str, int, str, str]:
    """Parse a normalized HTTP(S) proxy without returning its credentials."""

    text = str(proxy_url or "").strip()
    if not text:
        raise ImapFallbackError("proxy_required", status=502)
    try:
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if not host or port is None or not 1 <= port <= 65535:
            raise ValueError
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        return parsed.scheme.lower(), host, port, username, password
    except (TypeError, ValueError, UnicodeError):
        raise ImapFallbackError("proxy_invalid", status=502) from None


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ImapFallbackError("proxy_timeout", status=504)
    return remaining


def _read_until_headers(sock: socket.socket, *, deadline: float | None = None) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) >= _MAX_PROXY_RESPONSE:
            raise ImapFallbackError("proxy_connect", status=502)
        try:
            if deadline is not None:
                sock.settimeout(_remaining_time(deadline))
            chunk = sock.recv(min(4096, _MAX_PROXY_RESPONSE - len(data)))
        except (socket.timeout, TimeoutError):
            raise ImapFallbackError("proxy_timeout", status=504) from None
        if not chunk:
            raise ImapFallbackError("proxy_connect", status=502)
        data.extend(chunk)
    return bytes(data)


def _open_http_connect(
    proxy_url: str,
    target_host: str,
    target_port: int,
    *,
    timeout: float,
    socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ssl_context: ssl.SSLContext | None = None,
    deadline: float | None = None,
) -> socket.socket:
    """Open a TCP tunnel through an HTTP(S) proxy.

    The CONNECT response is parsed locally.  No response body, proxy host, or
    credentials is included in an exception.
    """

    scheme, host, port, username, password = _proxy_parts(proxy_url)
    raw: socket.socket | ssl.SSLSocket | None = None
    try:
        if deadline is not None:
            timeout = min(timeout, _remaining_time(deadline))
        raw = socket_factory((host, port), timeout=timeout)
        raw.settimeout(_remaining_time(deadline) if deadline is not None else timeout)
        if scheme == "https":
            context = ssl_context or ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        if deadline is not None:
            raw.settimeout(_remaining_time(deadline))
        authority = f"{target_host}:{target_port}"
        lines = [
            f"CONNECT {authority} HTTP/1.1",
            f"Host: {authority}",
            "Proxy-Connection: Keep-Alive",
        ]
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            lines.append(f"Proxy-Authorization: Basic {token}")
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        response = _read_until_headers(raw, deadline=deadline)
        first_line = response.split(b"\r\n", 1)[0]
        match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})\b", first_line)
        if not match:
            raise ImapFallbackError("proxy_connect", status=502)
        code = int(match.group(1))
        if code != 200:
            raise ImapFallbackError("proxy_auth" if code == 407 else "proxy_connect", status=502)
        return raw
    except ImapFallbackError:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        raise
    except (socket.timeout, TimeoutError):
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        raise ImapFallbackError("proxy_timeout", status=504) from None
    except (OSError, ssl.SSLError):
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        raise ImapFallbackError("proxy_connect", status=502) from None


def _probe_socket_connection(address, *, deadline: float):
    """将系统 DNS 解析也纳入预检预算；超时后解析线程不会继续建立连接。"""
    answers = Queue(maxsize=1)

    def resolve():
        try:
            answers.put((socket.getaddrinfo(*address, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            answers.put((None, exc))

    Thread(target=resolve, daemon=True, name="imap-probe-dns").start()
    try:
        addresses, error = answers.get(timeout=_remaining_time(deadline))
    except Empty:
        raise ImapFallbackError("proxy_timeout", status=504) from None
    if error is not None:
        raise error
    if not addresses:
        raise ImapFallbackError("proxy_connect")
    family, socktype, protocol, _name, sockaddr = addresses[0]
    connection = socket.socket(family, socktype, protocol)
    try:
        connection.settimeout(_remaining_time(deadline))
        connection.connect(sockaddr)
        return connection
    except BaseException:
        connection.close()
        raise


def probe_imap_transport(
    proxy_url: str,
    *,
    timeout: float = 20.0,
    socket_factory=None,
    ssl_context: ssl.SSLContext | None = None,
) -> None:
    """仅探测代理到 IMAP 的 CONNECT/TLS，不构造账号客户端或发送 LOGIN。"""
    deadline = time.monotonic() + timeout
    raw = secured = None
    try:
        context = ssl_context or ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ImapFallbackError("imap_tls")
        raw = _open_http_connect(
            proxy_url, IMAP_HOST, IMAP_PORT, timeout=timeout,
            socket_factory=socket_factory or (lambda address, **_kwargs: _probe_socket_connection(address, deadline=deadline)),
            deadline=deadline,
        )
        raw.settimeout(_remaining_time(deadline))
        secured = context.wrap_socket(raw, server_hostname=IMAP_HOST)
        _remaining_time(deadline)
    except ImapFallbackError:
        raise
    except (socket.timeout, TimeoutError):
        raise ImapFallbackError("proxy_timeout", status=504) from None
    except ssl.SSLError:
        raise ImapFallbackError("imap_tls") from None
    except OSError:
        raise ImapFallbackError("proxy_connect") from None
    finally:
        for connection in (secured, raw):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


class _ProxiedIMAP4SSL(imaplib.IMAP4_SSL):
    """imaplib connection whose socket is established through HTTP CONNECT."""

    def __init__(
        self,
        host: str = IMAP_HOST,
        port: int = IMAP_PORT,
        *,
        proxy_url: str,
        timeout: float | None = None,
        ssl_context: ssl.SSLContext | None = None,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self._proxy_url = proxy_url
        self._socket_factory = socket_factory
        self._target_ssl_context = ssl_context or ssl.create_default_context()
        super().__init__(host, port, ssl_context=self._target_ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        raw = _open_http_connect(
            self._proxy_url,
            self.host,
            self.port,
            timeout=timeout if timeout is not None else 30.0,
            socket_factory=self._socket_factory,
        )
        try:
            return self.ssl_context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLError:
            try:
                raw.close()
            except OSError:
                pass
            raise ImapFallbackError("imap_tls", status=502) from None


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        return value


def _header_values(message: Message, names: Iterable[str]) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        for value in message.get_all(name, []):
            decoded = _decode_header(str(value))
            if decoded:
                values.append(decoded)
    return tuple(values)


def _message_date(message: Message) -> datetime | None:
    value = message.get("Date")
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_header(
    uid: str,
    raw: bytes,
    *,
    fallback_date: datetime | None = None,
) -> ImapHeader:
    message = email.message_from_bytes(raw)
    recipients = _header_values(
        message,
        _RECIPIENT_HEADERS,
    )
    return ImapHeader(
        uid=str(uid),
        subject=_decode_header(message.get("Subject")),
        sender=_decode_header(message.get("From")),
        recipients=recipients,
        date=_message_date(message) or fallback_date,
        raw=raw,
    )


def recipient_matches(header: ImapHeader, recipient: str) -> bool:
    needle = str(recipient or "").strip().casefold()
    if not needle:
        return False
    # Parse addr-spec values instead of searching raw header text.  This keeps
    # an address mentioned only in a display name or comment from matching a
    # different split route.
    return any(
        address.strip().casefold() == needle
        for _display_name, address in email.utils.getaddresses(header.recipients)
    )


def _fetch_bytes(data: Any) -> bytes:
    chunks: list[bytes] = []
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                payload = item[1]
                if isinstance(payload, (bytes, bytearray)):
                    chunks.append(bytes(payload))
            elif isinstance(item, (bytes, bytearray)):
                chunks.append(bytes(item))
    return b"".join(chunks)


def _exists_count(data: Any) -> int | None:
    raw = _fetch_bytes(data)
    match = re.search(rb"(?:^|\s)(\d+)\s+EXISTS\b", raw, re.I)
    if match is None and isinstance(data, (list, tuple)):
        for item in data:
            if isinstance(item, (bytes, bytearray)) and bytes(item).strip().isdigit():
                match = re.search(rb"(\d+)", bytes(item))
                break
    return int(match.group(1)) if match is not None else None


class ImapFolderClearClient:
    """独立的可写客户端，仅供显式运行的账号检查工具清空邮件。"""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        proxy_url: str,
        host: str = IMAP_HOST,
        port: int = IMAP_PORT,
        timeout: float = 20.0,
        connection_factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        if not str(proxy_url or "").strip():
            raise ImapFallbackError("proxy_required", status=502)
        self.username = str(username).strip().lower()
        self.password = str(password)
        self.proxy_url = str(proxy_url).strip()
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._connection_factory = connection_factory or _ProxiedIMAP4SSL
        self.connection: imaplib.IMAP4_SSL | None = None
        self._event_handler: Callable[[dict[str, object]], None] | None = None

    def set_event_handler(self, handler: Callable[[dict[str, object]], None]) -> None:
        self._event_handler = handler

    def _report(self, stage: str, status: str, **fields) -> None:
        if self._event_handler is not None:
            self._event_handler({"stage": stage, "status": status, **fields})

    def connect(self) -> None:
        stage, operation = "连接", "CONNECT/TLS"
        started = time.monotonic()
        self._report(stage, "开始", operation=operation)
        try:
            self.connection = self._connection_factory(
                self.host, self.port, proxy_url=self.proxy_url, timeout=self.timeout,
            )
            self._report(stage, "成功", operation=operation, elapsed=time.monotonic() - started)
            stage, operation = "登录", "LOGIN"
            started = time.monotonic()
            self._report(stage, "开始", operation=operation)
            typ, _ = self.connection.login(self.username, self.password)
            if typ != "OK":
                raise ImapFallbackError("imap_auth_failed", status=401)
            self._report(stage, "成功", operation=operation, elapsed=time.monotonic() - started)
        except (ImapFallbackError, imaplib.IMAP4.error, OSError) as exc:
            if isinstance(exc, ImapFallbackError):
                error = exc
            elif isinstance(exc, (socket.timeout, TimeoutError)):
                error = ImapFallbackError("imap_timeout", status=504)
            elif isinstance(exc, (imaplib.IMAP4.abort, OSError)):
                error = ImapFallbackError("imap_network", status=502)
            else:
                error = ImapFallbackError(
                    "imap_auth_failed" if stage == "登录" else "imap_protocol",
                    status=401 if stage == "登录" else 502,
                )
            self._report(stage, "失败", operation=operation, error=error.kind, elapsed=time.monotonic() - started)
            self.close()
            raise error from None

    def _folder_operation(self, stage: str, folder: str, operation: str, command, *, count=False):
        started = time.monotonic()
        self._report(stage, "开始", folder=folder, operation=operation)
        try:
            typ, data = command()
            if typ != "OK":
                raise ImapFallbackError("imap_clear_failed", status=502)
            value = _exists_count(data) if count else None
            if count and value is None:
                raise ImapFallbackError("imap_clear_failed", status=502)
        except (ImapFallbackError, imaplib.IMAP4.error, OSError, ValueError, TypeError):
            self._report(stage, "失败", folder=folder, operation=operation,
                         error="imap_clear_failed", elapsed=time.monotonic() - started)
            raise ImapFallbackError("imap_clear_failed", status=502) from None
        self._report(stage, "成功", folder=folder, operation=operation,
                     count=value, elapsed=time.monotonic() - started)
        return value

    def clear_folders(self, folders: Iterable[str]) -> dict[str, dict[str, int]]:
        """按传入顺序逐个查询、清空、复查；调用方将 Trash 放最后。"""
        if self.connection is None:
            raise ImapFallbackError("imap_clear_failed", status=502)
        conn = self.connection
        folders = tuple(folders)
        counts: dict[str, dict[str, int]] = {}
        for folder in folders:
            before = self._folder_operation(
                "首次查询", folder, "SELECT", lambda: conn.select(folder, readonly=False), count=True,
            )
            counts[folder] = {"before": before}
            if counts[folder]["before"] == 0:
                self._report("清空", "跳过", folder=folder, count=0, reason="文件夹为空")
            else:
                self._folder_operation("清空", folder, "STORE", lambda: conn.store("1:*", "+FLAGS", r"\Deleted"))
                self._folder_operation("清空", folder, "EXPUNGE", conn.expunge)
            counts[folder]["after"] = self._folder_operation(
                "复查", folder, "SELECT", lambda: conn.select(folder, readonly=False), count=True,
            )
        return counts

    def close(self) -> None:
        conn, self.connection = self.connection, None
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


class ImapReadOnlyClient:
    """Small read-only IMAPS client suitable for a bounded fallback pool."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        proxy_url: str,
        host: str = IMAP_HOST,
        port: int = IMAP_PORT,
        timeout: float = 20.0,
        connection_factory: Callable[..., imaplib.IMAP4_SSL] | None = None,
    ) -> None:
        if not str(proxy_url or "").strip():
            raise ImapFallbackError("proxy_required", status=502)
        self.username = str(username).strip().lower()
        self.password = str(password)
        self.proxy_url = str(proxy_url).strip()
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._connection_factory = connection_factory or _ProxiedIMAP4SSL
        self.connection: imaplib.IMAP4_SSL | None = None
        self.inbox_exists: int | None = None

    def connect(self) -> None:
        stage = "connect"
        self.inbox_exists = None
        try:
            self.connection = self._connection_factory(
                self.host,
                self.port,
                proxy_url=self.proxy_url,
                timeout=self.timeout,
            )
            stage = "login"
            typ, _ = self.connection.login(self.username, self.password)
            if typ != "OK":
                raise ImapFallbackError("imap_auth_failed", status=401)
            # ``imaplib`` has no public ``examine`` method. ``select`` with
            # ``readonly=True`` emits EXAMINE on the wire and keeps the mailbox
            # from being modified by later FETCH operations.
            stage = "examine"
            typ, data = self.connection.select("INBOX", readonly=True)
            if typ != "OK":
                raise ImapFallbackError("imap_examine_failed", status=502)
            self.inbox_exists = _exists_count(data)
        except ImapFallbackError:
            self.close()
            raise
        except (socket.timeout, TimeoutError):
            self.close()
            raise ImapFallbackError("imap_timeout", status=504) from None
        except imaplib.IMAP4.abort:
            self.close()
            raise ImapFallbackError("imap_network", status=502) from None
        except imaplib.IMAP4.error:
            self.close()
            if stage == "login":
                raise ImapFallbackError("imap_auth_failed", status=401) from None
            if stage == "examine":
                raise ImapFallbackError("imap_examine_failed", status=502) from None
            raise ImapFallbackError("imap_protocol", status=502) from None
        except (OSError, ssl.SSLError):
            self.close()
            raise ImapFallbackError("imap_network", status=502) from None

    def _require_connection(self) -> imaplib.IMAP4_SSL:
        if self.connection is None:
            self.connect()
        assert self.connection is not None
        return self.connection

    @staticmethod
    def _search_criteria(since: datetime | None, recipient: str = "") -> list[str]:
        criteria: list[str] = []
        if since is not None:
            criteria.extend(
                ["SINCE", since.astimezone(timezone.utc).strftime("%d-%b-%Y")]
            )
        recipient = str(recipient or "").strip()
        if recipient:
            # IMAP OR is binary and prefix-ordered. N headers therefore need
            # N-1 nested OR operators before the HEADER criteria.
            criteria.extend(["OR"] * (len(_RECIPIENT_HEADERS) - 1))
            for header in _RECIPIENT_HEADERS:
                criteria.extend(["HEADER", header, recipient])
        return criteria or ["ALL"]

    def _search_uids(self, since: datetime | None, recipient: str = "") -> list[str]:
        conn = self._require_connection()
        try:
            criteria = self._search_criteria(since, recipient)
            typ, data = conn.uid("SEARCH", None, *criteria)
            if typ != "OK" and recipient:
                # Older or non-mail.com IMAP servers may reject one of the
                # delivery header names. Preserve correctness with the broad
                # scan and keep exact local recipient validation below.
                typ, data = conn.uid(
                    "SEARCH", None, *self._search_criteria(since)
                )
            if typ != "OK":
                raise ImapFallbackError("imap_search_failed", status=502)
            raw = _fetch_bytes(data)
            return [part.decode("ascii", "ignore") for part in raw.split() if part.isdigit()]
        except ImapFallbackError:
            raise
        except (socket.timeout, TimeoutError):
            raise ImapFallbackError("imap_timeout", status=504) from None
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError):
            raise ImapFallbackError("imap_network", status=502) from None
        except imaplib.IMAP4.error:
            raise ImapFallbackError("imap_search_failed", status=502) from None

    def _fetch_header(self, uid: str) -> ImapHeader | None:
        conn = self._require_connection()
        fields = "HEADER.FIELDS (TO CC DELIVERED-TO ENVELOPE-TO X-ORIGINAL-TO X-ENVELOPE-TO FROM SUBJECT DATE)"
        try:
            typ, data = conn.uid(
                "FETCH",
                uid,
                f"(INTERNALDATE BODY.PEEK[{fields}])",
            )
            if typ != "OK":
                raise ImapFallbackError("imap_fetch_failed", status=502)
            raw = _fetch_bytes(data)
            if not raw:
                return None
            meta = b"".join(item[0] for item in data if isinstance(item, tuple) and isinstance(item[0], bytes)) if isinstance(data, list) else b""
            match = _UID_RE.search(meta)
            internal_date = None
            date_match = re.search(rb'\bINTERNALDATE\s+"([^"]+)"', meta, re.I)
            if date_match:
                try:
                    internal_date = email.utils.parsedate_to_datetime(
                        date_match.group(1).decode("ascii", "strict")
                    )
                    if internal_date.tzinfo is None:
                        internal_date = internal_date.replace(tzinfo=timezone.utc)
                    internal_date = internal_date.astimezone(timezone.utc)
                except (TypeError, ValueError, UnicodeError, OverflowError):
                    internal_date = None
            return parse_header(
                match.group(1).decode("ascii") if match else uid,
                raw,
                fallback_date=internal_date,
            )
        except (socket.timeout, TimeoutError):
            raise ImapFallbackError("imap_timeout", status=504) from None
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError):
            raise ImapFallbackError("imap_network", status=502) from None
        except imaplib.IMAP4.error:
            raise ImapFallbackError("imap_fetch_failed", status=502) from None

    def fetch_raw(self, uid: str) -> bytes:
        conn = self._require_connection()
        try:
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                raise ImapFallbackError("imap_fetch_failed", status=502)
            raw = _fetch_bytes(data)
            if not raw:
                raise ImapFallbackError("imap_fetch_failed", status=502)
            if len(raw) > _MAX_MESSAGE_BYTES:
                raise ImapFallbackError("imap_message_too_large", status=502)
            return raw
        except ImapFallbackError:
            raise
        except (socket.timeout, TimeoutError):
            raise ImapFallbackError("imap_timeout", status=504) from None
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError):
            raise ImapFallbackError("imap_network", status=502) from None
        except imaplib.IMAP4.error:
            raise ImapFallbackError("imap_fetch_failed", status=502) from None

    def query_recent(
        self,
        recipient: str,
        *,
        sender: str = "",
        since: datetime | None = None,
        max_age: int = 600,
        limit: int = 30,
    ) -> list[ImapMail]:
        """Return matching messages without changing read/unread state."""

        if since is None and max_age > 0:
            since = datetime.now(timezone.utc) - timedelta(seconds=max_age)
        uids = self._search_uids(since, recipient)
        matches: list[ImapMail] = []
        for uid in reversed(uids[-max(1, min(int(limit), 100)) :]):
            header = self._fetch_header(uid)
            if header is None or not recipient_matches(header, recipient):
                continue
            if sender and sender.casefold() not in header.sender.casefold():
                continue
            if since is not None and (header.date is None or header.date < since):
                continue
            if header.date is not None and max_age > 0:
                age = (datetime.now(timezone.utc) - header.date).total_seconds()
                if age > max_age:
                    continue
            raw = self.fetch_raw(header.uid)
            matches.append(
                ImapMail(
                    uid=header.uid,
                    subject=header.subject,
                    sender=header.sender,
                    recipients=header.recipients,
                    date=header.date,
                    raw=raw,
                )
            )
        return matches

    def close(self) -> None:
        conn, self.connection = self.connection, None
        if conn is None:
            return
        try:
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass

    def __enter__(self) -> "ImapReadOnlyClient":
        self.connect()
        return self

    def __exit__(self, _typ: Any, _value: Any, _tb: Any) -> None:
        self.close()


class _ProxiedPOP3SSL(poplib.POP3_SSL):
    """POP3S connection whose socket is established through HTTP CONNECT."""

    def __init__(
        self,
        host: str = POP3_HOST,
        port: int = POP3_PORT,
        *,
        proxy_url: str,
        timeout: float | None = None,
        ssl_context: ssl.SSLContext | None = None,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        self._proxy_url = proxy_url
        self._socket_factory = socket_factory
        self._target_ssl_context = ssl_context or ssl.create_default_context()
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=self._target_ssl_context,
        )

    def _create_socket(self, timeout: float | None) -> socket.socket:
        raw = _open_http_connect(
            self._proxy_url,
            self.host,
            self.port,
            timeout=timeout if timeout is not None else 30.0,
            socket_factory=self._socket_factory,
        )
        try:
            return self.context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLError:
            try:
                raw.close()
            except OSError:
                pass
            raise Pop3FallbackError("pop3_tls", status=502) from None


def _pop3_ok(response: Any) -> bool:
    if isinstance(response, bytes):
        return response.startswith(b"+OK")
    if isinstance(response, str):
        return response.upper().startswith("+OK")
    return False


def _pop3_lines(data: Any) -> list[bytes]:
    """Extract multiline POP3 payload lines without exposing protocol errors."""

    if not isinstance(data, (list, tuple)) or len(data) < 2:
        return []
    lines = data[1]
    if not isinstance(lines, (list, tuple)):
        return []
    result: list[bytes] = []
    for line in lines:
        if isinstance(line, (bytes, bytearray)):
            value = bytes(line)
            # poplib removes dot-stuffing, but keep this safe for injected
            # connection implementations used by tests and adapters.
            if value.startswith(b".."):
                value = value[1:]
            result.append(value)
    return result


class Pop3ReadOnlyClient:
    """Bounded, read-only POP3S reader for an optional fallback experiment.

    The client authenticates with USER/PASS, enumerates UIDL entries, inspects
    headers with TOP, and retrieves only matching messages with RETR.  It never
    sends DELE or any other mutating command.  A non-empty ``proxy_url`` routes
    the socket through an HTTP CONNECT proxy; an empty value opens a direct
    TLS connection, which fits providers whose mailbox hosts are reachable
    without a per-account proxy.  The public result shape intentionally matches
    ``ImapMail`` so a caller can choose IMAP or POP3 without changing code
    extraction logic.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        proxy_url: str,
        host: str = POP3_HOST,
        port: int = POP3_PORT,
        timeout: float = 20.0,
        max_messages: int = 30,
        max_message_bytes: int = _MAX_MESSAGE_BYTES,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.username = str(username).strip().lower()
        self.password = str(password)
        self.proxy_url = str(proxy_url).strip()
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.max_messages = max(1, min(int(max_messages), 100))
        self.max_message_bytes = max(1024, min(int(max_message_bytes), _MAX_MESSAGE_BYTES))
        self._connection_factory = connection_factory or _ProxiedPOP3SSL
        self.connection: Any | None = None

    def connect(self) -> None:
        auth_started = False
        try:
            if self.proxy_url:
                self.connection = self._connection_factory(
                    self.host,
                    self.port,
                    proxy_url=self.proxy_url,
                    timeout=self.timeout,
                )
            else:
                self.connection = poplib.POP3_SSL(
                    self.host,
                    self.port,
                    timeout=self.timeout,
                )
            conn = self.connection
            auth_started = True
            if not _pop3_ok(conn.user(self.username)):
                raise Pop3FallbackError("pop3_auth_failed", status=401)
            if not _pop3_ok(conn.pass_(self.password)):
                raise Pop3FallbackError("pop3_auth_failed", status=401)
        except Pop3FallbackError:
            self.close()
            raise
        except ImapFallbackError as exc:
            self.close()
            raise Pop3FallbackError(exc.kind, status=exc.status) from None
        except poplib.error_proto:
            self.close()
            raise Pop3FallbackError(
                "pop3_auth_failed" if auth_started else "pop3_protocol",
                status=401 if auth_started else 502,
            ) from None
        except (socket.timeout, TimeoutError):
            self.close()
            raise Pop3FallbackError("pop3_timeout", status=504) from None
        except (OSError, ssl.SSLError):
            self.close()
            raise Pop3FallbackError("pop3_network", status=502) from None

    def _require_connection(self) -> Any:
        if self.connection is None:
            self.connect()
        assert self.connection is not None
        return self.connection

    def _uidl_entries(self) -> list[tuple[int, str]]:
        conn = self._require_connection()
        try:
            response = conn.uidl()
            if (
                not isinstance(response, (list, tuple))
                or not response
                or not _pop3_ok(response[0])
            ):
                raise Pop3FallbackError("pop3_uidl_failed", status=502)
            entries: list[tuple[int, str]] = []
            for line in _pop3_lines(response):
                parts = line.split(None, 1)
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                uid = parts[1].decode("ascii", "ignore").strip()
                if uid:
                    entries.append((int(parts[0]), uid))
            return entries
        except Pop3FallbackError:
            raise
        except poplib.error_proto:
            raise Pop3FallbackError("pop3_uidl_failed", status=502) from None
        except (socket.timeout, TimeoutError):
            raise Pop3FallbackError("pop3_timeout", status=504) from None
        except OSError:
            raise Pop3FallbackError("pop3_network", status=502) from None

    def _top_header(self, message_number: int, uid: str) -> ImapHeader | None:
        conn = self._require_connection()
        try:
            response = conn.top(str(message_number), 0)
            if (
                not isinstance(response, (list, tuple))
                or not response
                or not _pop3_ok(response[0])
            ):
                raise Pop3FallbackError("pop3_top_failed", status=502)
            raw = b"\r\n".join(_pop3_lines(response))
            if len(raw) > self.max_message_bytes:
                raise Pop3FallbackError("pop3_message_too_large", status=502)
            if raw and not raw.endswith(b"\r\n"):
                raw += b"\r\n"
            return parse_header(uid, raw) if raw else None
        except Pop3FallbackError:
            raise
        except poplib.error_proto:
            raise Pop3FallbackError("pop3_top_failed", status=502) from None
        except (socket.timeout, TimeoutError):
            raise Pop3FallbackError("pop3_timeout", status=504) from None
        except (OSError, UnicodeError, ValueError):
            raise Pop3FallbackError("pop3_top_failed", status=502) from None

    def _retrieve(self, message_number: int) -> bytes:
        conn = self._require_connection()
        try:
            response = conn.retr(str(message_number))
            if (
                not isinstance(response, (list, tuple))
                or not response
                or not _pop3_ok(response[0])
            ):
                raise Pop3FallbackError("pop3_retr_failed", status=502)
            raw = b"\r\n".join(_pop3_lines(response))
            if len(raw) > self.max_message_bytes:
                raise Pop3FallbackError("pop3_message_too_large", status=502)
            return raw
        except Pop3FallbackError:
            raise
        except poplib.error_proto:
            raise Pop3FallbackError("pop3_retr_failed", status=502) from None
        except (socket.timeout, TimeoutError):
            raise Pop3FallbackError("pop3_timeout", status=504) from None
        except OSError:
            raise Pop3FallbackError("pop3_network", status=502) from None

    def query_recent(
        self,
        recipient: str,
        *,
        sender: str = "",
        since: datetime | None = None,
        max_age: int = 600,
        limit: int = 30,
    ) -> list[ImapMail]:
        if since is None and max_age > 0:
            since = datetime.now(timezone.utc) - timedelta(seconds=max_age)
        entries = self._uidl_entries()
        cap = max(1, min(int(limit), self.max_messages))
        matches: list[ImapMail] = []
        for message_number, uid in sorted(entries, reverse=True)[:cap]:
            header = self._top_header(message_number, uid)
            if header is None or not recipient_matches(header, recipient):
                continue
            if sender and sender.casefold() not in header.sender.casefold():
                continue
            if since is not None and (header.date is None or header.date < since):
                continue
            if header.date is not None and max_age > 0:
                age = (datetime.now(timezone.utc) - header.date).total_seconds()
                if age > max_age:
                    continue
            raw = self._retrieve(message_number)
            matches.append(
                ImapMail(
                    uid=uid,
                    subject=header.subject,
                    sender=header.sender,
                    recipients=header.recipients,
                    date=header.date,
                    raw=raw,
                )
            )
        return matches

    def close(self) -> None:
        conn, self.connection = self.connection, None
        if conn is None:
            return
        try:
            # QUIT releases the server-side mailbox lock; it is read-only and
            # does not delete messages.  Never call DELE or RSET here.
            conn.quit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def __enter__(self) -> "Pop3ReadOnlyClient":
        self.connect()
        return self

    def __exit__(self, _typ: Any, _value: Any, _tb: Any) -> None:
        self.close()


def create_readonly_client(
    username: str,
    password: str,
    *,
    proxy_url: str,
    protocol: str = "imap",
    timeout: float = 20.0,
    **kwargs: Any,
) -> ImapReadOnlyClient | Pop3ReadOnlyClient:
    """Create the selected read-only mailbox client.

    ``protocol`` defaults to ``imap`` so existing integrations keep their
    behavior.  Use ``pop3``/``pop3s`` only for an explicit canary; protocol-
    specific keyword arguments are forwarded to the selected constructor.
    """

    selected = str(protocol or "imap").strip().lower()
    if selected in {"imap", "imaps"}:
        return ImapReadOnlyClient(
            username,
            password,
            proxy_url=proxy_url,
            timeout=timeout,
            **kwargs,
        )
    if selected in {"pop", "pop3", "pop3s"}:
        return Pop3ReadOnlyClient(
            username,
            password,
            proxy_url=proxy_url,
            timeout=timeout,
            **kwargs,
        )
    raise ValueError("unsupported_mailbox_protocol")


__all__ = [
    "IMAP_HOST",
    "IMAP_PORT",
    "POP3_HOST",
    "POP3_PORT",
    "ImapFallbackError",
    "Pop3FallbackError",
    "ImapHeader",
    "ImapMail",
    "ImapReadOnlyClient",
    "Pop3ReadOnlyClient",
    "create_readonly_client",
    "parse_header",
    "recipient_matches",
]
