"""Minimal mail.com web client reconstructed from the supplied HAR captures."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

from curl_cffi import requests
from curl_cffi.requests import exceptions as requests_exceptions


LOGIN_PAGE_URL = "https://www.mail.com/"
LOGIN_URL = "https://login.mail.com/login"
OAUTH_URL = "https://oauthbridge.navigator-lxa.mail.com/navigator/oauth2/token"
CISS_JUMP_URL = "https://navigator-lxa.mail.com/navigator/jump/to/ciss"
MAIL_LIST_URL = "https://maillist.mail.com/Mailbox/Mail"
MAIL_BODY_URL = "https://webmail-cats-live.mail.com/mailbox/primary/mailbody/{mail_id}/Body"
FOLDERS_URL = "https://webmail-cats-live.mail.com/mailbox/primary/folders"
QUOTA_URL = "https://webmail-cats-live.mail.com/mailbox/primary/quota"
SETTINGS_ADDRESSES_URL = "https://settings-cats.mail.com/mailaccount/primary/emailAddresses"
SETTINGS_VALIDATE_URL = "https://settings-cats.mail.com/mailaccount/emailAddressValidations"
SETTINGS_DOMAINS_URL = "https://settings-cats.mail.com/domains"

MAIL_SCOPE = "mail_mailbox_r"
SETTINGS_SCOPE = "mail_mailbox_w webmailer_setting_r webmailer_setting_w mail_confix_w"
# Keep the captured IDs as defaults, but make frontend rotations deploy-time
# configurable so a canary can test a new bundle without rewriting code.
MAIL_CLIENT_ID = os.getenv(
    "MAIL_API_MAIL_CLIENT_ID", "mailcom_webmailermaillist_passport_live"
)
SETTINGS_CLIENT_ID = os.getenv(
    "MAIL_API_SETTINGS_CLIENT_ID", "mailcom_mailset_root_live"
)
MAIL_UI_APP = os.getenv(
    "MAIL_API_MAIL_UI_APP", "mailcom.webmailer.mail-list/6.6.3"
)
SETTINGS_UI_APP = os.getenv(
    "MAIL_API_SETTINGS_UI_APP", "mailcom.mailset-compose/1.0.5-build.335"
)
# This is the public SPA secret shipped in mail.com's frontend bundle.  It is
# not an account credential; allow an environment override for future bundle
# rotations without changing the client code.
OAUTH_PUBLIC_SECRET = os.getenv("MAIL_OAUTH_PUBLIC_SECRET", "*******")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
STATISTICS_RE = re.compile(r'name=["\']statistics["\'][^>]*value=["\']([^"\']*)', re.I)
CISS_SRTTKN_RE = re.compile(r"srttkn=([0-9a-fA-F-]{36})")
CISS_CHANGE_LINK_RE = re.compile(r'href="([^"]*changePasswordLink[^"]*)"')
CISS_FORM_ACTION_RE = re.compile(r'<form[^>]*action="([^"]*passwordChange[^"]*)"')


class MailComError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "upstream_error", status: int = 502):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(slots=True)
class MailMessage:
    mail_id: str
    subject: str
    sender: str
    recipients: list[str]
    date_ms: int
    folder: str


class MailComClient:
    def __init__(
        self,
        username: str,
        password: str,
        *,
        state: dict[str, Any] | None = None,
        timeout: float = 25.0,
        proxy_url: str = "",
    ) -> None:
        self.username = username.strip().lower()
        self.password = password
        self.timeout = timeout
        # Public code reads may reuse a saved sid/token, but must never turn a
        # traffic burst into password POSTs. Service recovery and explicit
        # account operations leave this enabled.
        self.allow_password_login = True
        # mail.com sends non-browser TLS clients to its assistance flow even
        # when the credentials and form fields are valid.
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({"User-Agent": USER_AGENT})
        # Never inherit a process-wide proxy implicitly. Account routing must
        # be explicit so a failed binding cannot silently fall back to egress.
        self.session.trust_env = False
        try:
            self.login_retries = max(
                1, min(4, int(os.environ.get("MAIL_API_LOGIN_RETRIES", "4")))
            )
        except ValueError:
            self.login_retries = 1
        try:
            # One short same-session retry absorbs transient OAuthBridge 400/5xx
            # responses without creating another password login.  The ensure_*
            # methods still allow at most one fresh login after this is exhausted.
            self.oauth_retries = max(
                1, min(3, int(os.environ.get("MAIL_API_OAUTH_RETRIES", "2")))
            )
        except ValueError:
            self.oauth_retries = 2
        # A proxy is an account property.  Reuse it for every upstream request;
        # rotation and fallback to another account's proxy are intentionally absent.
        self.proxy_url = proxy_url.strip()
        if self.proxy_url:
            self.session.trust_env = False
            self.session.proxies.update({"http": self.proxy_url, "https": self.proxy_url})
        state = state or {}
        self.sid = str(state.get("sid") or "")
        self.auth_id = str(state.get("auth_id") or "")
        self.tokens: dict[str, str] = dict(state.get("tokens") or {})
        cookies = state.get("cookies") or {}
        if isinstance(cookies, dict):
            self.session.cookies.update(cookies)

    def export_state(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "auth_id": self.auth_id,
            "tokens": self.tokens,
            "cookies": dict(self.session.cookies),
            "saved_at": int(time.time()),
        }

    def _transport_error_kind(self, exc: Any) -> str:
        """Classify transport failures so a dead proxy can trigger failover.

        The web client tunnels every upstream request through the account's
        fixed proxy, so proxy/connect-level curl errors are proxy failures
        (which the recovery loop swaps) rather than generic upstream network
        errors. Generic and read-level errors stay ``network`` because they
        can also be caused by a slow upstream.
        """
        if self.proxy_url:
            if isinstance(exc, requests_exceptions.ConnectTimeout):
                return "proxy_timeout"
            if isinstance(exc, requests_exceptions.ProxyError) or isinstance(
                exc, requests_exceptions.ConnectionError
            ):
                return "proxy_connect"
        return "network"

    def invalidate_session(self) -> None:
        """Drop server-side session material while retaining login cookies."""
        self.sid = ""
        self.auth_id = ""
        self.tokens.clear()

    def reset_auth_session(self) -> None:
        """Start a clean browser session on the account's fixed proxy.

        A stale cookie jar can make a fresh password login appear to succeed
        while OAuthBridge still returns ``NO_SESSION``.  Rebuild only the HTTP
        session; credentials and the account's proxy binding stay unchanged.
        """
        try:
            self.session.close()
        except AttributeError:
            pass
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.trust_env = False
        if self.proxy_url:
            self.session.proxies.update(
                {"http": self.proxy_url, "https": self.proxy_url}
            )
        self.invalidate_session()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Make an upstream request and normalize transport failures.

        curl_cffi raises several ``RequestException`` subclasses for DNS,
        proxy, TLS, and read timeouts.  Letting those escape the client makes
        the HTTP handler return a generic 500 and records inconsistent status
        values.  Keep the original exception chained for local diagnostics,
        but expose only a stable, credential-free ``MailComError`` to callers.
        """
        try:
            return getattr(self.session, method)(url, **kwargs)
        except requests.RequestsError as exc:
            raise MailComError(
                "无法连接 mail.com 上游服务",
                kind=self._transport_error_kind(exc),
            ) from exc

    def _settings_request(
        self,
        method: str,
        url: str,
        *,
        content_type: str,
        accept: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Issue a settings request, refreshing one stale session once.

        Settings endpoints can return 401 after the OAuth token expires even
        though token acquisition succeeded earlier in the request.  A single
        invalidate-and-login retry avoids transient alias/split failures while
        keeping persistent authorization failures bounded.  A 403 is returned
        to the operation because alias creation also uses it for quota/limit
        failures; treating every 403 as an expired session causes needless
        password logins when an account has reached its alias limit.
        """
        response: Any = None
        for attempt in range(2):
            token = self.ensure_settings_token()
            request_kwargs = dict(kwargs)
            request_kwargs["headers"] = self._settings_headers(
                token, content_type, accept=accept
            )
            response = self._request(method, url, **request_kwargs)
            if response.status_code != 401 or attempt:
                return response
            self.invalidate_session()
        return response

    @staticmethod
    def decode_token(token: str) -> dict[str, Any]:
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError
            return payload
        except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MailComError("mail.com 返回了无法解析的 access token") from exc

    @classmethod
    def token_expiry(cls, token: str) -> float:
        value = float(cls.decode_token(token).get("exp") or 0)
        # mail.com currently emits epoch milliseconds, unlike conventional JWT seconds.
        return value / 1000.0 if value > 10_000_000_000 else value

    @staticmethod
    def _timezone_hours() -> int:
        local = time.localtime()
        offset = -time.timezone if not local.tm_isdst else -time.altzone
        return int(offset // 3600)

    def login(self, retries: int | None = None) -> None:
        if not self.allow_password_login:
            raise MailComError(
                "saved mail.com session is unavailable",
                kind="session_expired",
                status=401,
            )
        retries = self.login_retries if retries is None else max(1, min(3, retries))
        last_error: MailComError | None = None
        for attempt in range(retries):
            try:
                self._login_once()
                self.tokens.clear()
                return
            except MailComError as exc:
                last_error = exc
                # This redirect class is ambiguous: mail.com uses the same
                # flow for throttling, browser challenges, and invalid
                # credentials. Defer retries to the bounded recovery loop.
                if exc.kind in {
                    "bad_credentials",
                    "blocked",
                    "login_challenged",
                    "login_redirect",
                    "rate_limited",
                }:
                    break
                if attempt + 1 < retries:
                    # mail.com may briefly return its assistance redirect while
                    # the fixed proxy is warming up. Match the HAR client's
                    # bounded 3/6/9 second backoff without retrying credentials.
                    time.sleep(3 * (attempt + 1))
        raise last_error or MailComError("mail.com 登录失败", kind="login_failed")

    def _login_once(self) -> None:
        try:
            page = self.session.get(LOGIN_PAGE_URL, timeout=self.timeout)
        except requests.RequestsError as exc:
            raise MailComError(
                "mail.com login bootstrap request failed",
                kind=self._transport_error_kind(exc),
            ) from exc
        if not page.ok:
            raise MailComError(
                f"mail.com login bootstrap failed (HTTP {page.status_code})",
                kind="login_bootstrap_failed",
                status=page.status_code,
            )
        match = STATISTICS_RE.search(page.text)
        if not match or not match.group(1):
            # An empty or stale statistics value produces the same redirect as
            # credential and anti-abuse failures. Stop before sending a password.
            raise MailComError(
                "mail.com login page did not provide a valid statistics value",
                kind="login_challenged",
                status=503,
            )
        statistics = match.group(1)

        form = {
            "username": self.username,
            "password": self.password,
            "service": "mailint",
            "uasServiceID": "mc_starter_mailcom",
            "successURL": "https://$(clientName)-$(dataCenter).mail.com/login",
            "loginFailedURL": "https://www.mail.com/logout?ls=wd",
            "loginErrorURL": "https://www.mail.com/logout?ls=te",
            "edition": "US",
            "lang": "en",
            "usertype": "standard",
            "ibaInfo": "abd=false",
            "statistics": statistics,
        }
        try:
            response = self.session.post(
                LOGIN_URL,
                data=form,
                allow_redirects=False,
                timeout=self.timeout,
                headers={"Origin": LOGIN_PAGE_URL.rstrip("/"), "Referer": LOGIN_PAGE_URL},
            )
        except requests.RequestsError as exc:
            raise MailComError(
                "无法连接 mail.com 登录服务",
                kind=self._transport_error_kind(exc),
            ) from exc

        location = response.headers.get("Location", "")
        if response.status_code == 429:
            raise MailComError("mail.com 登录频率受限", kind="rate_limited", status=429)
        if response.status_code == 403:
            raise MailComError("mail.com 拒绝了当前网络的登录请求", kind="blocked", status=403)
        if response.status_code in (302, 303) and "ott=" not in location:
            # ``logout?ls=wd`` is not proof of a bad password. The original
            # HAR client observed the same redirect under login throttling.
            kind = "login_challenged" if "logout?ls=wd" in location else "login_redirect"
            raise MailComError("登录未返回一次性令牌", kind=kind, status=401)
        if response.status_code != 303 or "ott=" not in location:
            raise MailComError(f"登录响应异常 (HTTP {response.status_code})", kind="login_failed")

        callback = urljoin(LOGIN_URL, location)
        parsed = urlparse(callback)
        query = parsed.query + ("&" if parsed.query else "") + f"tz={self._timezone_hours()}"
        halogin = urlunparse((parsed.scheme, parsed.netloc, "/halogin", "", query, ""))
        try:
            exchanged = self.session.get(halogin, allow_redirects=False, timeout=self.timeout)
        except requests.RequestsError as exc:
            raise MailComError(
                "无法完成 mail.com 会话交换",
                kind=self._transport_error_kind(exc),
            ) from exc
        location = exchanged.headers.get("Location", "")
        sid = (parse_qs(urlparse(location).query).get("sid") or [""])[0]
        if exchanged.status_code not in (302, 303) or not sid:
            raise MailComError("mail.com 会话交换未返回 sid", kind="session_rejected", status=401)
        self.sid = sid

    def _token_key(self, client_id: str, scope: str) -> str:
        return f"{client_id}|{scope}"

    @staticmethod
    def _response_error_code(response: Any) -> str:
        """Extract only a bounded machine-readable upstream error code.

        Response bodies can contain request/session data, so callers must not
        persist or log them.  OAuthBridge and settings APIs expose stable error
        identifiers in a small set of JSON fields; retain only identifier-safe
        characters from those fields.
        """
        try:
            payload = response.json()
        except (AttributeError, TypeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        candidates: list[Any] = []
        candidates.extend(
            payload.get(key) for key in ("error_description", "errorDescription")
        )
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend(
                error.get(key) for key in ("code", "type", "errorCode", "name")
            )
        else:
            candidates.append(error)
        candidates.extend(
            payload.get(key) for key in ("code", "type", "errorCode", "name")
        )
        for value in candidates:
            if not isinstance(value, str):
                continue
            raw = value.strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}", raw):
                return raw
        return ""

    @staticmethod
    def _oauth_error_category(code: str) -> str:
        normalized = code.lower()
        if any(
            marker in normalized
            for marker in (
                "wrong_public_secret",
                "invalid_client",
                "invalid_scope",
                "unauthorized_client",
                "unsupported_grant_type",
            )
        ):
            return "config"
        if any(
            marker in normalized
            for marker in (
                "invalid_grant",
                "invalid_session",
                "session_not_found",
                "unknown_session",
                "no_session",
                "unauthorized_user",
            )
        ):
            return "session"
        return ""

    @staticmethod
    def _oauth_retry_delay(attempt: int) -> float:
        return min(0.75, 0.25 * (3**max(0, attempt)))

    @staticmethod
    def _coded_error_message(prefix: str, status: int, code: str) -> str:
        suffix = f", {code}" if code else ""
        return f"{prefix} (HTTP {status}{suffix})"

    def _raise_upstream_response(
        self,
        response: Any,
        prefix: str,
        *,
        default_kind: str,
    ) -> None:
        """Raise a stable, recoverable classification for an HTTP response."""

        status = int(response.status_code)
        code = self._response_error_code(response)
        if status == 401:
            kind = "session_expired"
        elif status == 403:
            kind = "session_rejected"
        elif status == 429:
            kind = "rate_limited"
        elif status in {408, 425} or 500 <= status < 600:
            kind = "network"
        else:
            kind = default_kind
        raise MailComError(
            self._coded_error_message(prefix, status, code),
            kind=kind,
            status=status,
        )

    @staticmethod
    def _oauth_headers(client_id: str) -> dict[str, str]:
        """Return the browser application context expected by OAuthBridge.

        OAuthBridge chooses the public SPA client from the request origin and
        UI application.  Using the webmailer context for settings scopes
        causes WRONG_PUBLIC_SECRET even when the mailbox session is valid.
        """
        if client_id == SETTINGS_CLIENT_ID:
            context = {
                "Origin": "https://mailset-root.mail.com",
                "Referer": "https://mailset-root.mail.com/",
            }
        else:
            context = {
                "Origin": "https://webmailer.mail.com",
                "Referer": "https://webmailer.mail.com/",
                "x-ui-app": MAIL_UI_APP,
            }
        basic = base64.b64encode(f"{client_id}:{OAUTH_PUBLIC_SECRET}".encode()).decode()
        context["Authorization"] = f"Basic {basic}"
        return context

    def get_token(self, scope: str, client_id: str, *, force: bool = False) -> str:
        key = self._token_key(client_id, scope)
        token = self.tokens.get(key, "")
        if token and not force:
            try:
                if self.token_expiry(token) > time.time() + 60:
                    return token
            except MailComError:
                pass
        if not self.sid:
            self.login()

        response: Any = None
        for attempt in range(self.oauth_retries):
            try:
                response = self.session.post(
                    OAUTH_URL,
                    params={"sid": self.sid},
                    data={"grant_type": "urn:mam:oauth:grant-type:spa", "scope": scope},
                    timeout=self.timeout,
                    headers=self._oauth_headers(client_id),
                )
            except requests.RequestsError as exc:
                if attempt + 1 < self.oauth_retries:
                    time.sleep(self._oauth_retry_delay(attempt))
                    continue
                raise MailComError(
                    "无法连接 mail.com OAuth 服务",
                    kind=self._transport_error_kind(exc),
                ) from exc

            status = int(response.status_code)
            code = self._response_error_code(response)
            category = self._oauth_error_category(code)
            if status == 200:
                break
            if status == 429:
                raise MailComError(
                    self._coded_error_message("mail.com token 请求频率受限", status, code),
                    kind="rate_limited",
                    status=429,
                )
            if status in {408, 425} or 500 <= status < 600:
                if attempt + 1 < self.oauth_retries:
                    time.sleep(self._oauth_retry_delay(attempt))
                    continue
                raise MailComError(
                    self._coded_error_message("mail.com OAuth 服务暂时不可用", status, code),
                    kind="network",
                    status=status,
                )
            if category == "config":
                raise MailComError(
                    self._coded_error_message("mail.com OAuth 客户端配置被拒绝", status, code),
                    kind="oauth_config_error",
                    status=502,
                )
            if category == "session":
                raise MailComError(
                    self._coded_error_message("mail.com 会话已失效或被拒绝", status, code),
                    kind="session_expired",
                    status=401,
                )
            if status == 401:
                raise MailComError(
                    self._coded_error_message("mail.com 会话已失效或被拒绝", status, code),
                    kind="session_expired",
                    status=401,
                )
            if status == 403:
                raise MailComError(
                    self._coded_error_message("mail.com 会话被拒绝", status, code),
                    kind="session_rejected",
                    status=403,
                )
            if status == 400 and attempt + 1 < self.oauth_retries:
                time.sleep(self._oauth_retry_delay(attempt))
                continue
            raise MailComError(
                self._coded_error_message("token 请求失败", status, code),
                kind="oauth_failed",
                status=status,
            )
        if response is None:
            raise MailComError("无法连接 mail.com OAuth 服务", kind="network")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            token = str(payload["access_token"])
        except (TypeError, ValueError, KeyError) as exc:
            raise MailComError("token 响应缺少 access_token", kind="oauth_failed") from exc
        payload = self.decode_token(token)
        self.auth_id = str(payload.get("auth_id") or self.auth_id)
        self.tokens[key] = token
        return token

    def ensure_mail_token(self) -> str:
        try:
            return self.get_token(MAIL_SCOPE, MAIL_CLIENT_ID)
        except MailComError as exc:
            if exc.kind not in {"session_expired", "oauth_failed"} or (
                exc.kind == "oauth_failed" and exc.status != 400
            ):
                raise
            self.reset_auth_session()
            self.login()
            return self.get_token(MAIL_SCOPE, MAIL_CLIENT_ID, force=True)

    def _mail_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.1and1.mms.unified-maillist-v1+json; charset=utf-8",
            "Content-Type": "application/vnd.1and1.mms.inboxadrequest-v1+json; charset=utf-8",
            "Origin": "https://webmailer.mail.com",
            "Referer": "https://webmailer.mail.com/",
            "x-ui-app": MAIL_UI_APP,
        }

    def query_messages(self, recipient: str, *, amount: int = 20) -> list[MailMessage]:
        token = self.ensure_mail_token()
        params: dict[str, Any] = {
            "folderTypeOrId": "INBOX",
            "offset": "0",
            "amount": str(max(1, min(amount, 50))),
            "orderBy": "INTERNALDATE DESC",
            "no_cache": self.auth_id,
            "condition": f"mail.header:subject,to,from,cc:{recipient}",
        }
        try:
            response = self.session.post(
                MAIL_LIST_URL, params=params, data=b"", headers=self._mail_headers(token), timeout=self.timeout
            )
        except requests.RequestsError as exc:
            raise MailComError(
                "无法连接 mail.com 邮件列表服务",
                kind=self._transport_error_kind(exc),
            ) from exc
        if response.status_code == 401:
            self.tokens.pop(self._token_key(MAIL_CLIENT_ID, MAIL_SCOPE), None)
            token = self.ensure_mail_token()
            try:
                response = self.session.post(
                    MAIL_LIST_URL, params=params, data=b"", headers=self._mail_headers(token), timeout=self.timeout
                )
            except requests.RequestsError as exc:
                raise MailComError(
                    "无法连接 mail.com 邮件列表服务",
                    kind=self._transport_error_kind(exc),
                ) from exc
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "查询收件箱失败", default_kind="mail_query_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            elements = payload.get("mailListElements") or []
        except (TypeError, ValueError) as exc:
            raise MailComError("邮件列表响应不是 JSON", kind="mail_query_failed") from exc
        if not isinstance(elements, list):
            raise MailComError("邮件列表响应结构无效", kind="mail_query_failed")
        messages: list[MailMessage] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            raw = element.get("rawData") or {}
            if not isinstance(raw, dict):
                continue
            attr = raw.get("attribute") or {}
            header = raw.get("mailHeader") or {}
            if not isinstance(attr, dict) or not isinstance(header, dict):
                continue
            mail_id = attr.get("mailIdentifier")
            if not mail_id:
                continue
            recipients = header.get("to") or []
            if isinstance(recipients, str):
                recipients = [recipients]
            try:
                date_ms = int(header.get("date") or attr.get("internalDate") or 0)
            except (TypeError, ValueError):
                date_ms = 0
            messages.append(
                MailMessage(
                    mail_id=str(mail_id),
                    subject=str(header.get("subject") or ""),
                    sender=str(header.get("from") or ""),
                    recipients=[str(value) for value in recipients],
                    date_ms=date_ms,
                    folder=str(attr.get("folderType") or ""),
                )
            )
        return messages

    def query_mailbox(
        self,
        *,
        folder: str | None = "INBOX",
        keyword: str = "",
        exclude_folders: tuple[str, ...] = (),
        amount: int = 50,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Query mailbox pages using the same parameters as the web client."""
        token = self.ensure_mail_token()
        page_size = max(1, min(int(amount), 50))
        page_limit = max(1, min(int(max_pages), 100))
        params: dict[str, Any] = {
            "offset": "0",
            "amount": str(page_size),
            "orderBy": "INTERNALDATE DESC",
            "no_cache": self.auth_id,
        }
        if folder:
            params["folderTypeOrId"] = str(folder)
        if exclude_folders:
            params["excludeFolderTypeOrId"] = list(exclude_folders)
        if keyword:
            params["condition"] = f"mail.header:subject,to,from,cc:{keyword}"
        headers = self._mail_headers(token)
        mails: list[dict[str, Any]] = []
        total = unread = 0
        for page in range(page_limit):
            params["offset"] = str(page * page_size)
            try:
                response = self.session.post(
                    MAIL_LIST_URL,
                    params=params,
                    data=b"",
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestsError as exc:
                raise MailComError(
                    "无法连接 mail.com 邮件列表服务",
                    kind=self._transport_error_kind(exc),
                ) from exc
            if response.status_code == 401 and page == 0:
                self.tokens.pop(self._token_key(MAIL_CLIENT_ID, MAIL_SCOPE), None)
                token = self.ensure_mail_token()
                headers = self._mail_headers(token)
                # Keep the second attempt under the same transport-error
                # normalization as the first request.  Otherwise a proxy
                # timeout here escapes as a generic HTTP 500.
                response = self._request(
                    "post",
                    MAIL_LIST_URL,
                    params=params,
                    data=b"",
                    headers=headers,
                    timeout=self.timeout,
                )
            if response.status_code != 200:
                self._raise_upstream_response(
                    response, "查询收件箱失败", default_kind="mail_query_failed"
                )
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError
            except (TypeError, ValueError) as exc:
                raise MailComError("邮件列表响应不是 JSON", kind="mail_query_failed") from exc
            try:
                total = max(0, int(payload.get("totalCount") or 0))
                unread = max(0, int(payload.get("unreadCount") or 0))
            except (TypeError, ValueError) as exc:
                raise MailComError("邮件列表计数无效", kind="mail_query_failed") from exc
            elements = payload.get("mailListElements") or []
            if not isinstance(elements, list):
                raise MailComError("邮件列表响应结构无效", kind="mail_query_failed")
            for element in elements:
                if not isinstance(element, dict):
                    continue
                raw = element.get("rawData") or {}
                if not isinstance(raw, dict):
                    continue
                attr = raw.get("attribute") or {}
                header = raw.get("mailHeader") or {}
                if not isinstance(attr, dict) or not isinstance(header, dict):
                    continue
                mail_id = attr.get("mailIdentifier")
                if not mail_id or "attribute" not in raw:
                    continue
                recipients = header.get("to") or []
                if isinstance(recipients, str):
                    recipients = [recipients]
                elif not isinstance(recipients, list):
                    recipients = [str(recipients)]
                cc = header.get("cc") or []
                if isinstance(cc, str):
                    cc = [cc]
                elif not isinstance(cc, list):
                    cc = [str(cc)]
                attachments = raw.get("attachments") or {}
                mails.append(
                    {
                        "mailIdentifier": str(mail_id),
                        "folderType": attr.get("folderType"),
                        "subject": header.get("subject"),
                        "from": header.get("from"),
                        "to": recipients,
                        "cc": cc,
                        "date": header.get("date"),
                        "internalDate": attr.get("internalDate"),
                        "size": attr.get("size"),
                        "read": attr.get("read"),
                        "flagged": attr.get("flagged"),
                        "hasAttachments": bool(
                            attachments.get("attachment")
                            if isinstance(attachments, dict)
                            else False
                        ),
                        "mailURI": raw.get("mailURI"),
                        "mailBodyURI": raw.get("mailBodyURI"),
                        "messageId": header.get("messageId"),
                    }
                )
            if len(elements) < page_size or len(mails) >= total or not elements:
                break
        return {"mails": mails, "totalCount": total, "unreadCount": unread}

    def list_folders(self) -> list[dict[str, Any]]:
        token = self.ensure_mail_token()
        response = self._request(
            "get",
            FOLDERS_URL,
            params={"absoluteURI": "false", "no_cache": self.auth_id},
            headers={**self._mail_headers(token), "Accept": "application/vnd.ui.trinity.folders-v4+json; charset=utf-8"},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "获取文件夹失败", default_kind="mail_query_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError) as exc:
            raise MailComError("文件夹响应不是 JSON", kind="mail_query_failed") from exc
        folders: list[dict[str, Any]] = []

        def walk(items: Any) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                attr = item.get("attribute") or {}
                if not isinstance(attr, dict):
                    attr = {}
                folders.append(
                    {
                        "folderType": attr.get("folderType"),
                        "name": attr.get("folderName") or attr.get("folderFullname"),
                        "fullFolderName": attr.get("folderFullname"),
                        "folderIdentifier": item.get("folderIdentifier"),
                        "folderURI": item.get("folderURI"),
                    }
                )
                children = item.get("folders") or []
                if children:
                    walk(children)

        walk(payload.get("folders") or [])
        return folders

    def get_quota(self) -> dict[str, Any]:
        token = self.ensure_mail_token()
        try:
            response = self.session.get(
                QUOTA_URL,
                params={"absoluteURI": "false", "no_cache": self.auth_id},
                headers={**self._mail_headers(token), "Accept": "application/json; charset=utf-8"},
                timeout=self.timeout,
            )
        except requests.RequestsError as exc:
            raise MailComError(
                "无法连接 mail.com 配额服务",
                kind=self._transport_error_kind(exc),
            ) from exc
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "获取配额失败", default_kind="mail_query_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            return payload
        except (TypeError, ValueError) as exc:
            raise MailComError("配额响应不是 JSON", kind="mail_query_failed") from exc

    def get_body(self, mail_id: str) -> str:
        token = self.ensure_mail_token()
        try:
            response = self.session.get(
                MAIL_BODY_URL.format(mail_id=mail_id),
                params={"absoluteURI": "false", "no_cache": self.auth_id},
                headers={**self._mail_headers(token), "Accept": "text/plain"},
                timeout=self.timeout,
            )
        except requests.RequestsError as exc:
            raise MailComError(
                "无法连接 mail.com 邮件正文服务",
                kind=self._transport_error_kind(exc),
            ) from exc
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "获取邮件正文失败", default_kind="mail_body_failed"
            )
        return response.text

    def change_password(self, new_password: str) -> dict[str, bool]:
        """Change the account password through the navigator SSO + CISS flow.

        Reuses ``self.sid`` and ``self.session`` (no second password login).
        Must be the final operation on this client: the old password stops
        working as soon as ``saveChanges`` succeeds.
        """
        if not self.sid:
            self.login()

        overview = self._request(
            "get",
            CISS_JUMP_URL,
            params={"sid": self.sid},
            allow_redirects=True,
            timeout=self.timeout,
        )
        if overview.status_code != 200:
            self._raise_upstream_response(
                overview, "进入账户设置失败", default_kind="upstream_error"
            )

        match = CISS_SRTTKN_RE.search(overview.url) or CISS_SRTTKN_RE.search(overview.text)
        if not match:
            raise MailComError(
                "账户设置页缺少 srttkn", kind="session_rejected", status=401
            )
        srttkn = match.group(1)

        link_match = CISS_CHANGE_LINK_RE.search(overview.text)
        if not link_match:
            raise MailComError(
                "账户设置页缺少改密码入口", kind="session_rejected", status=401
            )
        form = self._request(
            "get",
            urljoin(overview.url, html.unescape(link_match.group(1))),
            allow_redirects=True,
            timeout=self.timeout,
        )
        if form.status_code != 200:
            self._raise_upstream_response(
                form, "打开改密码表单失败", default_kind="upstream_error"
            )

        action_match = CISS_FORM_ACTION_RE.search(form.text)
        if not action_match:
            raise MailComError(
                "改密码表单缺失", kind="session_rejected", status=401
            )
        post_url = urljoin(str(form.url), html.unescape(action_match.group(1)))
        post_url += "&saveChanges=x" if "?" in post_url else "?saveChanges=x"

        response = self._request(
            "post",
            post_url,
            data={
                "editPanel:username": self.username,
                "editPanel:currentPasswordPanel:topWrapper:inputWrapper:input": self.password,
                "editPanel:newPasswordFieldPanel:topWrapper:inputWrapper:input": new_password,
                "editPanel:retypeNewPasswordFieldPanel:topWrapper:inputWrapper:input": new_password,
            },
            headers={
                "Origin": "https://account-lxa.mail.com",
                "Referer": str(form.url),
            },
            allow_redirects=False,
            timeout=self.timeout,
        )

        if response.status_code in (302, 303):
            location = response.headers.get("Location", "")
            if "passwordChangedSuccessfully=true" in location:
                return {"ok": True}
            raise MailComError(
                "改密码提交未确认", kind="session_rejected", status=response.status_code
            )
        self._raise_upstream_response(
            response, "改密码失败", default_kind="upstream_error"
        )

    def remove_alias(self, address: str) -> None:
        endpoint = (
            "https://settings-cats.mail.com/mailaccount/primary/"
            f"emailAddressesRemovals/{quote(address, safe='')}/removals"
        )
        response = self._settings_request(
            "post",
            endpoint,
            params={"absoluteURI": "false"},
            data=b"",
            content_type="text/plain;charset=UTF-8",
            accept="text/plain;charset=UTF-8",
            timeout=self.timeout,
        )
        if response.status_code not in (200, 204):
            self._raise_upstream_response(
                response, "删除邮箱地址失败", default_kind="settings_failed"
            )

    def list_domains(self) -> list[str]:
        response = self._settings_request(
            "get",
            SETTINGS_DOMAINS_URL,
            params={
                "absoluteURI": "false",
                "q.state.eq": "ACTIVE",
                "q.owner.eq": "gmx_MAILCOM",
            },
            content_type="application/json",
            timeout=self.timeout,
        )
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "读取域名列表失败", default_kind="settings_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            rows = payload.get("domains") or []
        except (TypeError, ValueError) as exc:
            raise MailComError("域名列表响应不是 JSON", kind="settings_failed") from exc
        if not isinstance(rows, list):
            raise MailComError("域名列表响应结构无效", kind="settings_failed")
        return list(
            dict.fromkeys(
                str(row.get("domain") or "").lower()
                for row in rows
                if isinstance(row, dict) and row.get("domain")
            )
        )

    def _settings_headers(
        self, token: str, content_type: str, *, accept: str | None = None
    ) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept or content_type,
            "Content-Type": content_type,
            "Origin": "https://mailset-root.mail.com",
            "Referer": "https://mailset-root.mail.com/",
            "x-ui-app": SETTINGS_UI_APP,
        }

    def ensure_settings_token(self) -> str:
        try:
            return self.get_token(SETTINGS_SCOPE, SETTINGS_CLIENT_ID)
        except MailComError as exc:
            if exc.kind not in {"session_expired", "oauth_failed"} or (
                exc.kind == "oauth_failed" and exc.status != 400
            ):
                raise
            self.reset_auth_session()
            self.login()
            return self.get_token(SETTINGS_SCOPE, SETTINGS_CLIENT_ID, force=True)

    def list_aliases(self) -> list[str]:
        response = self._settings_request(
            "get",
            SETTINGS_ADDRESSES_URL,
            params={"absoluteURI": "false", "q.state.in": "ACTIVE", "q.type.in": "MANAGED,DOMAIN_HOSTING"},
            content_type="application/vnd.ui.trinity.mailaddress.list-v5+json",
            timeout=self.timeout,
        )
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "读取邮箱地址失败", default_kind="settings_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError) as exc:
            raise MailComError("邮箱地址响应不是 JSON", kind="settings_failed") from exc
        rows = payload.get("mailaddresslist", [])
        if not isinstance(rows, list):
            raise MailComError("邮箱地址响应结构无效", kind="settings_failed")
        return [
            str(row.get("address") or "").lower()
            for row in rows
            if isinstance(row, dict) and row.get("address")
        ]

    def list_alias_rows(self) -> list[dict[str, str]]:
        """Return every managed/domain-hosting address with its state.

        Unlike ``list_aliases`` this does not filter ``q.state.in=ACTIVE``, so
        INACTIVE (and any other state) rows are included and their ``state`` is
        preserved.  Callers that only need active addresses should keep using
        ``list_aliases``.
        """
        response = self._settings_request(
            "get",
            SETTINGS_ADDRESSES_URL,
            params={"absoluteURI": "false", "q.type.in": "MANAGED,DOMAIN_HOSTING"},
            content_type="application/vnd.ui.trinity.mailaddress.list-v5+json",
            timeout=self.timeout,
        )
        if response.status_code != 200:
            self._raise_upstream_response(
                response, "读取邮箱地址失败", default_kind="settings_failed"
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
        except (TypeError, ValueError) as exc:
            raise MailComError("邮箱地址响应不是 JSON", kind="settings_failed") from exc
        rows = payload.get("mailaddresslist", [])
        if not isinstance(rows, list):
            raise MailComError("邮箱地址响应结构无效", kind="settings_failed")
        return [
            {
                "address": str(row.get("address") or "").lower(),
                "state": str(row.get("state") or ""),
            }
            for row in rows
            if isinstance(row, dict) and row.get("address")
        ]

    def add_alias(self, address: str) -> None:
        validation = self._settings_request(
            "post",
            SETTINGS_VALIDATE_URL,
            params={"absoluteURI": "false"},
            json=[address],
            content_type="application/vnd.ui.trinity.email-address-validation-request+json",
            accept="application/vnd.ui.trinity.email-address-validation-response+json",
            timeout=self.timeout,
        )
        if validation.status_code != 200:
            status = int(validation.status_code)
            code = self._response_error_code(validation)
            if status == 429:
                raise MailComError(
                    self._coded_error_message("邮箱地址校验频率受限", status, code),
                    kind="rate_limited",
                    status=429,
                )
            if status in (400, 404, 409, 422):
                raise MailComError(
                    self._coded_error_message("邮箱地址或域名不可用", status, code),
                    kind="alias_invalid",
                    status=400,
                )
            self._raise_upstream_response(
                validation,
                "邮箱地址校验失败",
                default_kind="settings_failed",
            )
        response = self._settings_request(
            "post",
            SETTINGS_ADDRESSES_URL,
            params={"absoluteURI": "false"},
            json={
                "address": address,
                "deletable": True,
                "pgpEnabled": False,
                "defaultSenderAddress": False,
                "defaultReceiverAddress": False,
                "state": "ACTIVE",
            },
            content_type="application/vnd.ui.trinity.minimalmailaddress-v3+json",
            timeout=self.timeout,
        )
        if response.status_code != 201:
            status = int(response.status_code)
            code = self._response_error_code(response)
            if status == 429:
                raise MailComError(
                    self._coded_error_message("添加邮箱地址频率受限", status, code),
                    kind="rate_limited",
                    status=429,
                )
            if status in (400, 404, 422):
                raise MailComError(
                    self._coded_error_message("邮箱地址或域名不可用", status, code),
                    kind="alias_invalid",
                    status=400,
                )
            if status in (403, 409):
                raise MailComError(
                    self._coded_error_message("邮箱别名数量已达限制或地址冲突", status, code),
                    kind="alias_limit",
                    status=400,
                )
            self._raise_upstream_response(
                response,
                "添加邮箱地址失败",
                default_kind="settings_failed",
            )
