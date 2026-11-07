from __future__ import annotations

import base64
import http.server
import json
import shlex
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


MAIL_ORIGINS = (
    "https://www.mail.com",
    "https://login.mail.com",
    "https://oauthbridge.navigator-lxa.mail.com",
    "https://navigator-lxa.mail.com",
    "https://maillist.mail.com",
    "https://webmail-cats-live.mail.com",
    "https://settings-cats.mail.com",
    "https://account-lxa.mail.com",
)


@dataclass
class MockAccount:
    password: str
    aliases: list[dict[str, str]] = field(default_factory=list)
    folders: dict[str, int] = field(default_factory=lambda: {
        "Drafts": 0, "INBOX": 0, "OUTBOX": 0,
        "Sent": 0, "Spam": 0, "Trash": 0,
    })


class MockMailState:
    def __init__(self, accounts: dict[str, MockAccount] | None = None):
        self.accounts = {email.lower(): account for email, account in (accounts or {}).items()}
        self.faults: dict[str, list[str]] = {}
        self.calls: dict[str, int] = {}
        self.lock = threading.RLock()
        self.password_change_mode: str | dict[str, str] = "success"
        self.recheck_counts: dict[tuple[str, str], int] = {}
        self.rejected_logins: set[str] = set()
        self.password_submitted = threading.Event()
        self.release_password_response = threading.Event()

    def count(self, operation: str) -> int:
        with self.lock:
            return self.calls.get(operation, 0)

    def record(self, operation: str) -> str | None:
        with self.lock:
            self.calls[operation] = self.calls.get(operation, 0) + 1
            queue = self.faults.get(operation)
            return queue.pop(0) if queue else None


class _MockHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state: MockMailState):
        self.state = state
        super().__init__(address, handler)


class _MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> MockMailState:
        return self.server.state

    def log_message(self, _format: str, *_args) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _reply(self, status: int, body: bytes = b"", *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, value: Any, status: int = 200, *, headers: dict[str, str] | None = None) -> None:
        self._reply(status, json.dumps(value).encode("utf-8"),
                    headers={"Content-Type": "application/json", **(headers or {})})

    def _account_email(self) -> str:
        cookies = self.headers.get("Cookie", "")
        for item in cookies.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "mock-account":
                return value.lower()
        return ""

    def _fault(self, operation: str) -> bool:
        fault = self.state.record(operation)
        if fault is None:
            return False
        if fault == "drop":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return True
        try:
            status = int(fault)
        except ValueError:
            status = 503
        self._reply(status, b"mock fault")
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._reply(200, b'<input name="statistics" value="mock-statistics">',
                        headers={"Content-Type": "text/html; charset=utf-8"})
            return
        if path == "/halogin":
            self._reply(302, headers={"Location": f"?sid={self._account_email() or 'mock-sid'}"})
            return
        if path == "/navigator/jump/to/ciss":
            self._reply(
                200,
                b'<div>srttkn=123e4567-e89b-12d3-a456-426614174000</div>'
                b'<a href="/account/changePasswordLink">Change password</a>',
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
            return
        if path == "/account/changePasswordLink":
            self._reply(
                200,
                b'<form action="/account/passwordChange?srttkn=mock"></form>',
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
            return
        if path == "/mailaccount/primary/emailAddresses":
            if self._fault("alias_list"):
                return
            username = self._account_email()
            account = self.state.accounts.get(username)
            self._json({"mailaddresslist": account.aliases if account else []})
            return
        self._reply(404, b"not found")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._body()
        if path == "/login":
            if self._fault("login"):
                return
            form = parse_qs(body.decode("utf-8", "replace"))
            email = (form.get("username") or [""])[0].strip().lower()
            password = (form.get("password") or [""])[0]
            account = self.state.accounts.get(email)
            if email in self.state.rejected_logins or account is None or account.password != password:
                self._reply(303, headers={"Location": "/logout?ls=wd"})
            else:
                self._reply(303, headers={
                    "Location": "/halogin?ott=mock-ott",
                    "Set-Cookie": f"mock-account={email}; Path=/; HttpOnly",
                })
            return
        if path == "/navigator/oauth2/token":
            if self._fault("oauth"):
                return
            payload = base64.urlsafe_b64encode(json.dumps({
                "exp": int((time.time() + 3600) * 1000), "auth_id": "mock-auth-id",
            }).encode()).decode().rstrip("=")
            self._json({"access_token": "mock." + payload + ".signature"})
            return
        if path == "/account/passwordChange":
            if self._fault("password_change"):
                return
            form = parse_qs(body.decode("utf-8", "replace"))
            email = (form.get("editPanel:username") or [""])[0].strip().lower()
            old_password = (form.get("editPanel:currentPasswordPanel:topWrapper:inputWrapper:input") or [""])[0]
            new_password = (form.get("editPanel:newPasswordFieldPanel:topWrapper:inputWrapper:input") or [""])[0]
            account = self.state.accounts.get(email)
            if account is None or account.password != old_password:
                self._reply(303, headers={"Location": "/account/settings?error=credentials"})
                return
            mode_value = self.state.password_change_mode
            mode = mode_value.get(email, "success") if isinstance(mode_value, dict) else mode_value
            if mode == "reject":
                self._reply(303, headers={"Location": "/account/settings?error=rejected"})
                return
            if mode == "drop_before_commit":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            account.password = new_password
            self.state.password_submitted.set()
            if mode == "commit_then_wait":
                self.state.release_password_response.wait(timeout=10)
            if mode == "commit_then_drop":
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self._reply(303, headers={"Location": "/account/settings?passwordChangedSuccessfully=true"})
            return
        if path.startswith("/mailaccount/primary/emailAddressesRemovals/"):
            if self._fault("alias_remove"):
                return
            email = self._account_email()
            account = self.state.accounts.get(email)
            address = unquote(path.split("/emailAddressesRemovals/", 1)[1].split("/removals", 1)[0]).lower()
            if account:
                account.aliases = [row for row in account.aliases
                                   if str(row.get("address", "")).lower() != address]
            self._reply(204)
            return
        self._reply(404, b"not found")


class _MockIMAPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state: MockMailState):
        self.state = state
        super().__init__(address, handler)


class _MockIMAPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.state: MockMailState = self.server.state
        self.wfile.write(b"* OK mail mock ready\r\n")
        user = ""
        selected = ""
        deleted = False
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            try:
                parts = shlex.split(raw.decode("utf-8", "replace"))
            except ValueError:
                self.wfile.write(b"* BAD invalid command\r\n")
                continue
            if len(parts) < 2:
                continue
            tag, command = parts[0], parts[1].upper()
            operation = "imap_" + command.lower()
            fault = self.state.record(operation)
            if fault == "drop":
                return
            if fault:
                self.wfile.write(f"{tag} NO mock fault\r\n".encode())
                continue
            if command == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n")
                self.wfile.write(f"{tag} OK CAPABILITY completed\r\n".encode())
            elif command == "LOGIN":
                email = parts[2].lower() if len(parts) > 2 else ""
                password = parts[3] if len(parts) > 3 else ""
                account = self.state.accounts.get(email)
                if account and account.password == password:
                    user = email
                    self.wfile.write(f"{tag} OK LOGIN completed\r\n".encode())
                else:
                    self.wfile.write(f"{tag} NO LOGIN failed\r\n".encode())
            elif command == "SELECT":
                selected = parts[2].strip('"') if len(parts) > 2 else "INBOX"
                account = self.state.accounts.get(user)
                if account is None or selected not in account.folders:
                    self.wfile.write(f"{tag} NO no such mailbox\r\n".encode())
                    continue
                count = account.folders[selected]
                if self.state.recheck_counts.get((user, selected)):
                    count = self.state.recheck_counts[(user, selected)]
                    self.state.recheck_counts.pop((user, selected), None)
                self.wfile.write(f"* {count} EXISTS\r\n".encode())
                self.wfile.write(f"{tag} OK [READ-WRITE] SELECT completed\r\n".encode())
            elif command == "STORE":
                deleted = True
                self.wfile.write(f"{tag} OK STORE completed\r\n".encode())
            elif command == "EXPUNGE":
                account = self.state.accounts.get(user)
                if account and selected:
                    account.folders[selected] = 0 if deleted else account.folders[selected]
                deleted = False
                self.wfile.write(f"{tag} OK EXPUNGE completed\r\n".encode())
            elif command == "LOGOUT":
                self.wfile.write(b"* BYE logging out\r\n")
                self.wfile.write(f"{tag} OK LOGOUT completed\r\n".encode())
                return
            else:
                self.wfile.write(f"{tag} BAD unsupported\r\n".encode())
            self.wfile.flush()


class MockMailService:
    """Stateful local HTTP/IMAP service used by integration tests only."""

    def __init__(self, state: MockMailState | None = None):
        self.state = state or MockMailState()
        self._temp = tempfile.TemporaryDirectory(prefix="mail-mocker-")
        self._directory = Path(self._temp.name)
        self._server_certificate = self._directory / "server.pem"
        self._server_key = self._directory / "server-key.pem"
        self._create_certificate()
        self._http = _MockHTTPServer(("127.0.0.1", 0), _MockHTTPHandler, self.state)
        self._imap = _MockIMAPServer(("127.0.0.1", 0), _MockIMAPHandler, self.state)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(self._server_certificate), str(self._server_key))
        self._imap.socket = server_context.wrap_socket(self._imap.socket, server_side=True)
        self._threads: list[threading.Thread] = []

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self._http.server_address[1]}"

    @property
    def imap_port(self) -> int:
        return int(self._imap.server_address[1])

    @property
    def endpoint_overrides(self) -> dict[str, str]:
        return {origin: self.http_url for origin in MAIL_ORIGINS}

    def imap_connection_factory(self, *_args, **_kwargs):
        import imaplib

        client_context = ssl.create_default_context(cafile=str(self._server_certificate))
        return imaplib.IMAP4_SSL(
            "127.0.0.1", self.imap_port, ssl_context=client_context
        )

    def start(self) -> "MockMailService":
        for server in (self._http, self._imap):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def close(self) -> None:
        self.state.release_password_response.set()
        for server in (self._http, self._imap):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._temp.cleanup()

    def __enter__(self) -> "MockMailService":
        return self.start()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _create_certificate(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        self._server_key.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        self._server_certificate.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
