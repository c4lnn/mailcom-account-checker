from ipaddress import ip_address
import socket
from urllib.parse import urljoin, urlsplit
import os

import pytest
from curl_cffi import requests as curl_requests
from cryptography.fernet import Fernet

os.environ.setdefault("CONSOLE_PASSWORD", "console-test-password")
os.environ.setdefault("CONSOLE_SESSION_SECRET", "s" * 48)
os.environ.setdefault("CONSOLE_COOKIE_SECURE", "0")
os.environ.setdefault("ACCOUNT_CHECKER_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_nonlocal_network(monkeypatch):
    original_connect = socket.create_connection
    original_socket_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_request = curl_requests.Session.request
    violations = []

    def reject(message):
        violations.append(message)
        raise AssertionError(message)

    def check_address(address):
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            reject(f"test network egress blocked: {address[0]}")

    def guarded_connect(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if not _is_loopback(host):
            reject(f"test network egress blocked: {host}")
        return original_connect(address, *args, **kwargs)

    def guarded_socket_connect(self, address):
        check_address(address)
        return original_socket_connect(self, address)

    def guarded_connect_ex(self, address):
        check_address(address)
        return original_connect_ex(self, address)

    def guarded_request(self, method, url, *args, **kwargs):
        follow = kwargs.pop("allow_redirects", True)
        for _ in range(20):
            host = urlsplit(str(url)).hostname
            if not _is_loopback(host):
                reject(f"test HTTP egress blocked: {host}")
            proxies = kwargs.get("proxies") or self.proxies or {}
            for proxy in [kwargs.get("proxy"), *proxies.values()]:
                if proxy and not _is_loopback(urlsplit(proxy).hostname):
                    reject("test HTTP proxy egress blocked")
            response = original_request(self, method, url, *args, allow_redirects=False, **kwargs)
            if not follow or response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("Location"):
                return response
            url = urljoin(str(url), response.headers["Location"])
            if response.status_code == 303 or (response.status_code in {301, 302} and method.upper() == "POST"):
                method = "GET"
                kwargs.pop("data", None)
                kwargs.pop("json", None)
        raise AssertionError("mock redirect limit exceeded")

    monkeypatch.setattr(socket, "create_connection", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect", guarded_socket_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(curl_requests.Session, "request", guarded_request)
    yield violations
    assert not violations, "意外外连（即使被业务捕获也使测试失败）: " + repr(violations)
